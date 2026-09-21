from __future__ import annotations

import os
import random
import re
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np
from PIL import Image

if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

from moviepy.audio.AudioClip import AudioClip
from moviepy.audio.fx.all import audio_loop
from moviepy.editor import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    concatenate_audioclips,
    concatenate_videoclips,
)

from yt_auto.subtitles import SubtitleComposer
from yt_auto.media_validation import MediaValidationReport, validate_final_mp4
from yt_auto.hw_encode import moviepy_codec_name, resolve_h264_encode_profile, selected_encoder_note


class LongCaptionPreflightError(RuntimeError):
    """A deterministic long-caption failure that no renderer fallback can fix."""


class ShortCaptionPreflightError(RuntimeError):
    """A deterministic short-caption failure that no renderer fallback can fix."""


@dataclass(frozen=True)
class NarrationBeatTiming:
    """One synchronized input/output slice of a variably paced narration track."""

    input_start: float
    input_end: float
    output_start: float
    output_end: float
    tempo: float

    @property
    def input_duration(self) -> float:
        return self.input_end - self.input_start

    @property
    def output_duration(self) -> float:
        return self.output_end - self.output_start

    @property
    def stretch(self) -> float:
        return self.output_duration / max(0.001, self.input_duration)


class VideoBuilder:
    # Slowing synthesized narration by more than this starts to sound visibly
    # artificial.  Long captions may use the small amount of headroom between
    # the generated narration and the configured maximum, but never more than
    # this bounded correction.
    # Every music bed is levelled to this before the mix gain is applied, so
    # one track cannot sit twice as loud under the narration as the next.
    MUSIC_BED_LUFS = -20.0

    MAX_LONG_NARRATION_STRETCH = 1.08
    MAX_LONG_BEAT_STRETCH = 1.085
    MAX_LONG_NARRATION_ACCELERATION = 1.10
    MIN_LONG_NARRATION_WPM = 138.0
    MAX_LONG_NARRATION_WPM = 165.0
    LONG_CAPTION_TARGET_CPS_OFFSET = 0.02
    # Short captions repair toward 16.5 CPS before the hard 17.0 abort so SRT
    # rounding and borderline density do not burn an otherwise usable Short.
    SHORT_CAPTION_TARGET_CPS = 16.5
    MAX_SHORT_NARRATION_STRETCH = 1.08
    # These clip starts were measured with the same top-background freeze scan
    # used for final approval. They avoid a static establishing section while
    # retaining the source's relevant action. Unknown assets keep the normal
    # deterministic variation below and still face final-render QA.
    AUDITED_MOTION_START_SECONDS = {
        "raw_08_adult-couple-plannin.mp4": 4.10,
    }

    def __init__(
        self,
        min_duration: int,
        max_duration: int,
        subtitle_enabled: bool = True,
        subtitle_max_words: int = 7,
        target_size: tuple[int, int] = (1080, 1920),
    ) -> None:
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.target_size = target_size
        self.subtitle_enabled = subtitle_enabled
        self.subtitle_composer = SubtitleComposer(max_words_per_caption=subtitle_max_words)
        self.last_music_path: Path | None = None
        self.last_video_encoder: str = ""
        self.last_claim_card_text: str = ""

    def _short_claim_card_text(self, title_text: str, channel_id: str | None) -> str:
        """Compress a Short title into a top-of-frame claim for the first second."""
        cleaned = re.sub(r"\s+", " ", (title_text or "").replace("—", " ").replace("–", " ")).strip()
        if not cleaned or channel_id != "brain_lens":
            return ""
        lowered = cleaned.lower()
        presets = (
            ("late repl", "ONE REPLY PROVES NOTHING"),
            ("micro flirt", "MICRO FLIRTING OR FRIENDLINESS?"),
            ("almost relationship", "ALMOST RELATIONSHIPS: THE LOOP"),
            ("future fak", "FUTURE FAKING: THE TRAP"),
            ("friends with benefits", "FRIENDS WITH BENEFITS?"),
            ("mixed signal", "MIXED SIGNALS LOOP"),
            ("love bomb", "LOVE BOMBING TRAP"),
            ("texting anx", "STOP REREADING TEXTS"),
        )
        for marker, claim in presets:
            if marker in lowered:
                return claim
        words = re.findall(r"[A-Za-z0-9']+", cleaned)
        stop = {"the", "a", "an", "to", "of", "and", "or", "that", "this", "with", "your", "you"}
        keep = [w for w in words if w.lower() not in stop] or words
        keep = keep[:6]
        if len(keep) < 2:
            return ""
        return " ".join(keep).upper()

    def _h264_encode_argv(
        self,
        *,
        ffmpeg_path: str | None = None,
        preset: str = "veryfast",
        crf: str | int = 21,
        threads: str | int = 2,
        force: str | None = None,
    ) -> list[str]:
        profile = resolve_h264_encode_profile(ffmpeg_path, force=force)
        # Keep the last *preferred* encoder for metadata, even if one stage
        # forces CPU for filter-complex compatibility.
        if force is None:
            self.last_video_encoder = profile.name
        elif not self.last_video_encoder:
            self.last_video_encoder = resolve_h264_encode_profile(ffmpeg_path).name
        return profile.argv(preset=preset, crf=crf, threads=threads)

    def _fit_vertical(self, clip: ImageClip | VideoFileClip) -> ImageClip | VideoFileClip:
        target_w, target_h = self.target_size
        frame_ratio = target_w / target_h
        clip_ratio = clip.w / clip.h

        if clip_ratio > frame_ratio:
            clip = clip.resize(height=target_h)
        else:
            clip = clip.resize(width=target_w)

        return clip.crop(width=target_w, height=target_h, x_center=clip.w / 2, y_center=clip.h / 2)

    def _pick_music(self, music_dir: Path) -> Optional[Path]:
        if not music_dir.exists():
            return None
        candidates = [
            path
            for path in music_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {'.mp3', '.wav', '.m4a', '.aac', '.ogg'}
        ]
        if not candidates:
            return None
        return random.choice(candidates)

    def _source_motion_offset(
        self,
        source_path: Path,
        index: int,
        *,
        base: float,
        step: float,
        modulo: int,
    ) -> float:
        """Choose a deterministic, QA-approved start point for source motion."""

        override = self.AUDITED_MOTION_START_SECONDS.get(source_path.name)
        if override is not None and source_path.suffix.lower() == ".mp4":
            return float(override)
        return float(base) + ((int(index) % max(1, int(modulo))) * float(step))

    def _narration_with_padding(self, narration: AudioFileClip) -> AudioFileClip:
        # Keep narration natural. Do not add artificial silence tails.
        return narration

    def _clean_scene_text(self, text: str) -> str:
        return re.sub(r'\s+', ' ', text or '').strip()

    def _story_beats(
        self,
        title_text: str,
        narration_text: str,
        visual_captions: List[str] | None,
        narration_beats: List[str] | None,
    ) -> List[str]:
        if narration_beats:
            beats = [self._clean_scene_text(beat) for beat in narration_beats if self._clean_scene_text(beat)]
            if beats:
                return beats

        beats = [self._clean_scene_text(text) for text in (visual_captions or []) if self._clean_scene_text(text)]
        if title_text and title_text.strip():
            beats.insert(0, title_text.strip())
        if not beats and narration_text.strip():
            beats = [segment.text for segment in self.subtitle_composer.scene_segments(narration_text, 12.0)]
        return beats

    def _select_scene_images(self, image_paths: List[Path], scene_count: int) -> List[Path]:
        if not image_paths:
            return []
        picked = list(image_paths[:scene_count])
        while len(picked) < scene_count:
            picked.append(image_paths[len(picked) % len(image_paths)])
        return picked

    def _beat_aligned_visual_plan(
        self,
        image_paths: List[Path],
        beat_segments: list,
        story_beats: List[str],
        duration: float,
    ) -> tuple[list, List[Path], List[int]]:
        from yt_auto.subtitles import SubtitleSegment

        beat_count = min(len(beat_segments), len(story_beats))
        if beat_count < 2 or not image_paths:
            visual_segment_count = min(len(image_paths), max(1, int(np.ceil(duration / 2.8))))
            visual_segment_duration = duration / visual_segment_count
            segments = [
                SubtitleSegment(
                    start=index * visual_segment_duration,
                    end=duration if index == visual_segment_count - 1 else (index + 1) * visual_segment_duration,
                    text=story_beats[index % len(story_beats)] if story_beats else "",
                )
                for index in range(visual_segment_count)
            ]
            images = self._select_scene_images(image_paths, len(segments))
            indices = [index % max(1, len(story_beats)) for index in range(len(segments))]
            return segments, images, indices

        visual_segments = []
        ordered_images: List[Path] = []
        beat_indices: List[int] = []
        for beat_index in range(beat_count):
            beat_segment = beat_segments[beat_index]
            raw_beat_images = list(image_paths[beat_index::beat_count])
            requested_slots = max(1, len(raw_beat_images))
            beat_images: List[Path] = []
            beat_image_keys: set[Path] = set()

            def add_beat_image(candidate: Path) -> None:
                key = Path(candidate).resolve()
                if key not in beat_image_keys:
                    beat_image_keys.add(key)
                    beat_images.append(candidate)

            for candidate in raw_beat_images:
                add_beat_image(candidate)
            if len(beat_images) < requested_slots:
                for offset in range(len(image_paths)):
                    add_beat_image(image_paths[(beat_index + offset) % len(image_paths)])
                    if len(beat_images) >= requested_slots:
                        break
            if not beat_images:
                beat_images = [image_paths[beat_index % len(image_paths)]]
            beat_start = max(0.0, float(beat_segment.start))
            beat_end = min(duration, max(beat_start + 0.8, float(beat_segment.end)))
            image_slots = min(requested_slots, len(beat_images))
            if duration >= 180:
                image_slots = max(image_slots, int(np.ceil((beat_end - beat_start) / 7.5)))
                # A long beat usually owns one primary asset. Repeating that one
                # file in every slot was later merged into a 25-30 second hold:
                # technically animated, but visually monotonous. Keep the primary
                # first, then intercut the nearest story-neighbour assets as B-roll.
                # Adjacent scenes are the safest semantic context for both the
                # chronological documentary and relationship case-study formats.
                candidate_images: List[Path] = []
                candidate_keys: set[Path] = set()

                def add_candidate(candidate: Path) -> None:
                    key = Path(candidate).resolve()
                    if key not in candidate_keys:
                        candidate_keys.add(key)
                        candidate_images.append(candidate)

                for candidate in beat_images:
                    add_candidate(candidate)
                radius = 1
                desired_pool_size = min(
                    len(image_paths),
                    max(4, image_slots + 2),
                )
                while len(candidate_images) < desired_pool_size and radius < len(image_paths):
                    for neighbour in (beat_index - radius, beat_index + radius):
                        if 0 <= neighbour < min(beat_count, len(image_paths)):
                            add_candidate(image_paths[neighbour])
                    radius += 1

                selected_images = list(candidate_images[:image_slots])
                if beat_index + 1 < min(beat_count, len(image_paths)) and selected_images:
                    next_primary = Path(image_paths[beat_index + 1]).resolve()
                    if Path(selected_images[-1]).resolve() == next_primary:
                        replacement = next(
                            (
                                candidate
                                for candidate in candidate_images[image_slots:]
                                if Path(candidate).resolve() != next_primary
                            ),
                            None,
                        )
                        if replacement is not None:
                            selected_images[-1] = replacement
                if selected_images:
                    beat_images = selected_images
            if (
                ordered_images
                and len(beat_images) > 1
                and Path(ordered_images[-1]).resolve() == Path(beat_images[0]).resolve()
            ):
                beat_images = beat_images[1:] + beat_images[:1]
            slot_duration = (beat_end - beat_start) / image_slots
            for image_index in range(image_slots):
                image_path = beat_images[image_index % len(beat_images)]
                start = beat_start + (image_index * slot_duration)
                end = beat_end if image_index == image_slots - 1 else start + slot_duration
                visual_segments.append(
                    SubtitleSegment(
                        start=start,
                        end=end,
                        text=story_beats[beat_index],
                    )
                )
                ordered_images.append(image_path)
                beat_indices.append(beat_index)

        # When two adjacent narration beats intentionally use the same still
        # (for example, one evidence diagram supports two consecutive claims),
        # keep one visual segment alive across the boundary. Re-encoding the
        # same still twice restarts its Ken Burns move and creates a visible
        # jump that looks like an accidental duplicate.
        merged_segments: list[SubtitleSegment] = []
        merged_images: List[Path] = []
        merged_indices: list[int | list[dict]] = []
        for segment, image_path, beat_index in zip(
            visual_segments,
            ordered_images,
            beat_indices,
        ):
            same_as_previous = bool(
                merged_images
                and Path(merged_images[-1]).resolve() == Path(image_path).resolve()
                and abs(float(merged_segments[-1].end) - float(segment.start)) <= 0.05
                and (
                    duration < 180
                    or float(segment.end) - float(merged_segments[-1].start) <= 8.5
                )
            )
            if same_as_previous:
                previous = merged_segments[-1]
                previous_index = merged_indices[-1]
                if isinstance(previous_index, int):
                    index_group: list[dict] = [
                        {
                            "beat_index": previous_index,
                            "start": float(previous.start),
                            "end": float(previous.end),
                            "beat_text": str(previous.text),
                        }
                    ]
                else:
                    index_group = list(previous_index)
                index_group.append(
                    {
                        "beat_index": int(beat_index),
                        "start": float(segment.start),
                        "end": float(segment.end),
                        "beat_text": str(segment.text),
                    }
                )
                merged_indices[-1] = index_group
                merged_segments[-1] = SubtitleSegment(
                    start=float(previous.start),
                    end=float(segment.end),
                    text=self._clean_scene_text(
                        f"{previous.text} {segment.text}"
                    ),
                )
                continue
            merged_segments.append(segment)
            merged_images.append(image_path)
            merged_indices.append(beat_index)
        if duration >= 180:
            longest_shot = max(
                (float(segment.end) - float(segment.start) for segment in merged_segments),
                default=0.0,
            )
            if longest_shot > 8.5:
                raise RuntimeError(
                    "long visual plan contains a physical shot longer than 8.5 seconds: "
                    f"{longest_shot:.2f}s"
                )
        return merged_segments, merged_images, merged_indices

    def _write_visual_timeline(
        self,
        out_path: Path,
        segments: list,
        images: List[Path],
        beat_indices: list[int | list[dict]],
    ) -> None:
        timeline = []
        for segment, image_path, beat_index in zip(segments, images, beat_indices):
            if isinstance(beat_index, list):
                for entry in beat_index:
                    timeline.append(
                        {
                            "start": round(float(entry["start"]), 3),
                            "end": round(float(entry["end"]), 3),
                            "beat_index": int(entry["beat_index"]),
                            "beat_text": str(entry["beat_text"]),
                            "source_file": Path(image_path).name,
                            "continuous_visual_hold": True,
                        }
                    )
                continue
            timeline.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "beat_index": int(beat_index),
                    "beat_text": str(segment.text),
                    "source_file": Path(image_path).name,
                }
            )
        (out_path.parent / "visual_timeline.json").write_text(
            json.dumps(timeline, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _make_scene_clip(
        self,
        path: Path,
        duration: float,
        is_hook: bool = False,
        allow_video: bool = True,
    ) -> ImageClip | VideoFileClip:
        if path.suffix.lower() == ".mp4" and not allow_video:
            try:
                source = VideoFileClip(str(path), audio=False)
                frame = source.get_frame(min(0.25, max(0.0, source.duration - 0.05)))
                source.close()
                clip = ImageClip(frame).set_duration(duration)
                clip = self._fit_vertical(clip)
                zoom_amount = 0.15 if is_hook else (0.028 if duration >= 5 else 0.04)
                clip = clip.resize(lambda t: 1 + (zoom_amount * min(t, 2.0) if is_hook else zoom_amount * (t / max(duration, 0.1))))
                return clip.crop(
                    width=self.target_size[0],
                    height=self.target_size[1],
                    x_center=clip.w / 2,
                    y_center=clip.h / 2,
                )
            except Exception:
                pass

        if path.suffix.lower() == ".mp4":
            try:
                clip = VideoFileClip(str(path), audio=False)
                if clip.duration and clip.duration > 0:
                    trim_pad = min(0.35, max(0.0, (clip.duration * 0.08)))
                    if clip.duration > (trim_pad * 2) + 0.3:
                        clip = clip.subclip(trim_pad, clip.duration - trim_pad)

                    if clip.duration < duration:
                        loops = int((duration // max(clip.duration, 0.1)) + 1)
                        clip = concatenate_videoclips([clip] * loops).subclip(0, duration)
                    else:
                        start_t = min(max(0, (clip.duration - duration) / 2), clip.duration - duration)
                        clip = clip.subclip(start_t, start_t + duration)

                # Strip incoming audio to not clash with our voiceover
                clip = clip.without_audio()
                clip = self._fit_vertical(clip)
                if is_hook:
                    clip = clip.resize(lambda t: 1 + 0.15 * min(t, 2.0))
                    clip = clip.crop(
                        width=self.target_size[0],
                        height=self.target_size[1],
                        x_center=clip.w / 2,
                        y_center=clip.h / 2,
                    )
                return clip
            except Exception:
                pass

        # Fallback to image clip
        frame = np.array(Image.open(path).convert('RGB'))
        clip = ImageClip(frame).set_duration(duration)
        clip = self._fit_vertical(clip)
        if is_hook:
            zoom_amount = 0.15
            clip = clip.resize(lambda t: 1 + (zoom_amount * min(t, 2.0)))
        else:
            zoom_amount = 0.028 if duration >= 5 else 0.04
            clip = clip.resize(lambda t: 1 + (zoom_amount * (t / max(duration, 0.1))))
            
        clip = clip.crop(
            width=self.target_size[0],
            height=self.target_size[1],
            x_center=clip.w / 2,
            y_center=clip.h / 2,
        )
        return clip

    def _duck_music(self, music: AudioClip, speech: AudioClip, spoken_duration: float, total_duration: float) -> AudioClip:
        # Lower music to 8% when speaking, 25% when silent
        def make_ducking_mask(t):
            # If t is beyond the spoken duration, slowly fade back up
            if np.isscalar(t):
                if t > spoken_duration + 0.1:
                    raw = 0.08 + min(1.0, (t - spoken_duration) * 2.0) * 0.17
                    return raw
                return 0.08
            
            mask = np.full(t.shape, 0.08, dtype=float)
            post_speech = t > spoken_duration + 0.1
            mask[post_speech] = 0.08 + np.minimum(1.0, (t[post_speech] - spoken_duration) * 2.0) * 0.17
            return mask
            
        return music.fl(lambda gf, t: gf(t) * np.stack([make_ducking_mask(t)]*2, axis=-1) if len(gf(t).shape) == 2 else gf(t) * make_ducking_mask(t))

    def _render_word_caption(self, text: str, highlight: bool, width: int) -> ImageClip | np.ndarray:
        try:
            from PIL import Image, ImageDraw, ImageFont

            font_path = Path("assets/fonts/Montserrat-Bold.ttf").resolve()
            stroke_w = 4
            padding_x = 34
            padding_y = 22
            max_text_w = width - padding_x * 2 - stroke_w * 2

            measure_canvas = Image.new("RGBA", (width * 3, 2000))
            measure_draw = ImageDraw.Draw(measure_canvas)
            lines = [text.upper()]

            # Premium readable captions: slightly smaller, phrase-based, and less shouty.
            for font_size in range(70, 38, -4):
                try:
                    font = ImageFont.truetype(str(font_path), font_size)
                except Exception:
                    font = ImageFont.load_default()
                    break

                lines = self.subtitle_composer.display_lines(
                    text,
                    font,
                    max_text_w,
                    measure_draw,
                    max_lines=2,
                    uppercase=False,
                )
                word_counts = [len(line.split()) for line in lines]
                needs_better_balance = sum(word_counts) >= 6 and min(word_counts or [0]) <= 1 and font_size > 48
                if (
                    lines
                    and all(self.subtitle_composer._text_width(measure_draw, line, font) <= max_text_w for line in lines)
                    and not needs_better_balance
                ):
                    break

            wrapped = "\n".join(lines)
            bbox = measure_draw.multiline_textbbox(
                (stroke_w, stroke_w), wrapped, font=font, spacing=10
            )
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            img_w = int(text_w + padding_x * 2 + stroke_w * 2)
            img_h = int(text_h + padding_y * 2 + stroke_w * 2 + 16)  # +16 descender guard

            img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))

            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle(
                (4, 4, img_w - 4, img_h - 4),
                radius=22,
                fill=(5, 10, 22, 214),
                outline=(255, 255, 255, 58),
                width=2,
            )

            priority_words = (
                "chemistry", "consistency", "attraction", "silence", "reply", "kiss",
                "mixed", "signals", "boundaries", "confidence", "trust", "pattern",
                "evidence", "warning", "attention", "access", "red", "flags",
            )
            normalized_words = [
                re.sub(r"[^a-z0-9']+", "", word.lower())
                for word in text.split()
            ]
            highlighted = next((word for word in priority_words if word in normalized_words), "") if highlight else ""
            if highlight and not highlighted:
                content_words = [
                    word for word in normalized_words
                    if len(word) > 3 and word not in {"that", "this", "with", "your", "they", "from", "what", "when"}
                ]
                highlighted = content_words[-1] if content_words else ""

            line_bbox = measure_draw.textbbox((0, 0), "Ag", font=font, stroke_width=stroke_w)
            line_height = max(1, line_bbox[3] - line_bbox[1])
            total_height = (line_height * len(lines)) + (10 * max(0, len(lines) - 1))
            y = (img_h - total_height) / 2 - line_bbox[1]
            for line in lines:
                words = line.split()
                space_width = draw.textlength(" ", font=font)
                widths = [draw.textlength(word, font=font) for word in words]
                line_width = sum(widths) + (space_width * max(0, len(words) - 1))
                x = (img_w - line_width) / 2
                for word, word_width in zip(words, widths):
                    token = re.sub(r"[^a-z0-9']+", "", word.lower())
                    color = (255, 214, 92, 255) if token == highlighted else (248, 250, 255, 255)
                    draw.text(
                        (x, y), word, font=font, fill=color,
                        stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255),
                    )
                    x += word_width + space_width
                y += line_height + 10

            return np.array(img)
        except Exception:
            return self.subtitle_composer.render_caption_image(text, width=width)

    def _ass_timestamp(self, seconds: float) -> str:
        centiseconds = max(0, int(round(float(seconds) * 100)))
        hours, remainder = divmod(centiseconds, 360000)
        minutes, remainder = divmod(remainder, 6000)
        whole_seconds, fraction = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"

    def _ass_caption_text(self, text: str) -> str:
        priority_words = (
            "chemistry", "consistency", "attraction", "silence", "reply", "kiss",
            "mixed", "signals", "boundaries", "confidence", "trust", "pattern",
            "evidence", "warning", "attention", "access", "clarity", "respect",
        )
        words = text.replace("\\", "").replace("{", "(").replace("}", ")").split()
        normalized = [re.sub(r"[^a-z0-9']+", "", word.lower()) for word in words]
        highlighted = next((word for word in priority_words if word in normalized), "")
        if not highlighted:
            content_words = [
                word for word in normalized
                if len(word) > 3 and word not in {"that", "this", "with", "your", "they", "from", "what", "when"}
            ]
            highlighted = content_words[-1] if content_words else ""
        rendered: list[str] = []
        highlight_used = False
        for word, token in zip(words, normalized):
            if token == highlighted and not highlight_used:
                rendered.append(r"{\c&H003ED6FF&}" + word + r"{\c&H00FFFFFF&}")
                highlight_used = True
            else:
                rendered.append(word)
        return " ".join(rendered)

    def _write_ass_captions(
        self,
        segments: list,
        out_path: Path,
        claim_card_text: str = "",
    ) -> None:
        target_w, target_h = self.target_size
        landscape = target_w > target_h
        font_size = max(42, int(target_h * 0.052)) if landscape else max(44, int(target_w * 0.082))
        margin_v = max(64, int(target_h * 0.075)) if landscape else max(220, int(target_h * 0.235))
        margin_h = max(90, int(target_w * 0.065)) if landscape else 70
        outline = 4 if landscape else 8
        claim_font = max(40, int(target_w * 0.055)) if not landscape else max(36, int(target_h * 0.045))
        claim_margin_v = max(72, int(target_h * 0.06)) if not landscape else max(48, int(target_h * 0.05))
        claim_outline = 6 if not landscape else 4
        claim_style = (
            f"Style: ClaimCard,Montserrat,{claim_font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90050A16,"
            f"-1,0,0,0,100,100,0,0,3,{claim_outline},0,8,{margin_h},{margin_h},{claim_margin_v},1\n"
        )
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {target_w}\n"
            f"PlayResY: {target_h}\n"
            "ScaledBorderAndShadow: yes\n"
            "WrapStyle: 0\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Caption,Montserrat,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H30050A16,"
            f"-1,0,0,0,100,100,0,0,3,{outline},0,2,{margin_h},{margin_h},{margin_v},1\n"
            f"{claim_style}"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        events = []
        claim = re.sub(r"[{}\\]", "", (claim_card_text or "").strip())
        if claim:
            safe_claim = claim.replace("\n", " ")
            events.append(
                "Dialogue: 1,0:00:00.00,0:00:01.15,ClaimCard,,0,0,0,,"
                f"{{\\fad(40,80)}}{safe_claim}"
            )
            self.last_claim_card_text = claim
        for segment in segments:
            clean_text = str(getattr(segment, "text", "") or "").strip()
            if not clean_text:
                continue
            events.append(
                "Dialogue: 0,"
                f"{self._ass_timestamp(segment.start)},{self._ass_timestamp(segment.end)},"
                f"Caption,,0,0,0,,{{\\fad(55,55)}}{self._ass_caption_text(clean_text)}"
            )
        out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")

    def _ffmpeg_filter_path(self, path: Path) -> str:
        return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")

    def _logo_clip(self, logo_path: Path | None, duration: float) -> ImageClip | None:
        if not logo_path or not logo_path.exists():
            return None
        try:
            logo_size = int(min(self.target_size) * 0.085)
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((logo_size, logo_size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
            x = (logo_size - logo.width) // 2
            y = (logo_size - logo.height) // 2
            canvas.alpha_composite(logo, (x, y))
            clip = ImageClip(np.array(canvas), transparent=True).set_duration(duration)
            margin = int(min(self.target_size) * 0.035)
            return clip.set_position((self.target_size[0] - logo_size - margin, margin)).set_opacity(0.88)
        except Exception as exc:
            print(f"Logo overlay skipped: {exc}")
            return None

    def _presenter_clip(
        self,
        avatar_path: Path | None,
        duration: float,
        content_kind: str,
        layout_mode: str = "rotating_avatar_broll",
    ) -> ImageClip | None:
        if content_kind != "short" or not avatar_path or not avatar_path.exists():
            return None
        try:
            target_w, target_h = self.target_size
            avatar = Image.open(avatar_path).convert("RGB")
            face_card_w = int(target_w * 0.56)
            face_card_h = int(target_h * 0.36)
            if layout_mode == "portrait_anchor":
                face_card_w = int(target_w * 0.62)
                face_card_h = int(target_h * 0.42)

            ratio = face_card_w / face_card_h
            source_ratio = avatar.width / max(1, avatar.height)
            if source_ratio > ratio:
                new_h = face_card_h
                new_w = int(new_h * source_ratio)
            else:
                new_w = face_card_w
                new_h = int(new_w / max(source_ratio, 0.1))
            avatar = avatar.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - face_card_w) // 2)
            top = max(0, int((new_h - face_card_h) * 0.28))
            avatar = avatar.crop((left, top, left + face_card_w, top + face_card_h))

            pad = int(target_w * 0.018)
            canvas = Image.new("RGBA", (face_card_w + pad * 2, face_card_h + pad * 2), (0, 0, 0, 0))
            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            shadow_draw = Image.new("RGBA", canvas.size, (0, 0, 0, 118))
            shadow.alpha_composite(shadow_draw, (0, pad // 2))
            canvas.alpha_composite(shadow)
            canvas.alpha_composite(avatar.convert("RGBA"), (pad, pad))

            clip = ImageClip(np.array(canvas), transparent=True).set_duration(duration)
            y_pos = int(target_h * 0.07)
            clip = clip.set_position(("center", y_pos))
            return clip.set_opacity(0.96)
        except Exception as exc:
            print(f"Presenter overlay skipped: {exc}")
            return None

    def _section_caption_text(self, text: str, content_kind: str) -> str:
        clean = self._clean_scene_text(text)
        if content_kind != "video":
            return clean
        first_sentence = re.split(r"(?<=[.!?])\s+", clean)[0] if clean else ""
        words = first_sentence.split()
        if len(words) > 9:
            first_sentence = " ".join(words[:9])
        return first_sentence.strip(" ,;:-") or clean[:80]

    def _prepare_fast_slide(self, path: Path, out_path: Path) -> None:
        target_w, target_h = self.target_size
        frame = None
        if path.suffix.lower() == ".mp4":
            try:
                source = VideoFileClip(str(path), audio=False)
                frame = Image.fromarray(source.get_frame(min(0.25, max(0.0, source.duration - 0.05)))).convert("RGB")
                source.close()
            except Exception:
                frame = None
        if frame is None:
            frame = Image.open(path).convert("RGB")

        frame_ratio = target_w / target_h
        source_ratio = frame.width / max(1, frame.height)
        if source_ratio > frame_ratio:
            new_h = target_h
            new_w = int(new_h * source_ratio)
        else:
            new_w = target_w
            new_h = int(new_w / max(source_ratio, 0.1))
        frame = frame.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = max(0, (new_w - target_w) // 2)
        top = max(0, (new_h - target_h) // 2)
        frame = frame.crop((left, top, left + target_w, top + target_h))
        frame.save(out_path, quality=93)

    def _render_fast_segment(
        self,
        ffmpeg_path: str,
        source_path: Path,
        out_path: Path,
        duration: float,
        is_hook: bool,
        source_offset: float = 0.0,
    ) -> None:
        target_w, target_h = self.target_size
        duration = max(0.8, float(duration))
        base_filter = (
            "setpts=PTS-STARTPTS,"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase:"
            "in_range=auto:out_range=tv,"
            f"crop={target_w}:{target_h},fps=30:round=near,"
            "setpts=N/(30*TB),format=yuv420p,"
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )
        if source_path.suffix.lower() == ".mp4":
            input_args = [
                "-stream_loop", "-1",
                "-ss", f"{max(0.0, source_offset):.3f}",
                "-i", str(source_path),
            ]
            # Stock footage can contain several seconds of a locked-off pose.
            # Apply a bounded virtual camera move to every source clip, not only
            # the hook, so low-motion footage cannot create a static six-second
            # hold after scene planning. Natural source motion remains visible.
            vertical_short = target_h > target_w
            cycle_frames = (150 if is_hook else 210) if vertical_short else (210 if is_hook else 300)
            half_cycle = cycle_frames // 2
            phase = float(source_offset) % 6.283185
            zoom_base = 1.015 if is_hook else 1.010
            zoom_span = 0.045 if vertical_short else 0.030
            pan_x = 0.20 if vertical_short else 0.14
            pan_y = 0.15 if vertical_short else 0.11
            video_filter = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase:"
                "in_range=auto:out_range=tv,"
                f"crop={target_w}:{target_h},"
                f"zoompan=z='{zoom_base:.3f}+{zoom_span:.3f}*(1-abs(mod(on,{cycle_frames})-{half_cycle})/{half_cycle})':"
                f"x='(iw-iw/zoom)*(0.5+{pan_x:.2f}*sin(on*0.021991+{phase:.6f}))':"
                f"y='(ih-ih/zoom)*(0.5+{pan_y:.2f}*cos(on*0.018850+{phase:.6f}))':"
                f"d=1:s={target_w}x{target_h}:fps=30,"
                "setpts=N/(30*TB),format=yuv420p,"
                "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
            )
        else:
            input_args = ["-loop", "1", "-framerate", "30", "-i", str(source_path)]
            # A capped one-way zoom becomes a literal freeze as soon as it reaches
            # its ceiling (about 4.4 seconds with the previous long-scene step).
            # Use a continuous triangular zoom instead: it reaches both endpoints
            # without dwelling there, reverses smoothly, and remains active for a
            # hold of any length.  The phase offsets keep consecutive stills from
            # repeating exactly the same camera move.
            # Shorts receive a slightly stronger virtual camera move than
            # landscape documentaries.  The final Brain Lens gate samples
            # frames just 0.32s apart, so the old 6%-over-10-second move could
            # look animated to a person yet still measure as static.
            vertical_short = target_h > target_w
            cycle_frames = (120 if is_hook else 180) if vertical_short else (180 if is_hook else 300)
            half_cycle = cycle_frames // 2
            phase = float(source_offset) % 6.283185
            zoom_span = 0.12 if vertical_short else 0.06
            pan_x = 0.30 if vertical_short else 0.22
            pan_y = 0.22 if vertical_short else 0.18
            video_filter = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase:"
                "in_range=auto:out_range=tv,"
                f"crop={target_w}:{target_h},"
                f"zoompan=z='1.0+{zoom_span:.3f}*(1-abs(mod(on,{cycle_frames})-{half_cycle})/{half_cycle})':"
                f"x='(iw-iw/zoom)*(0.5+{pan_x:.2f}*sin(on*0.014959+{phase:.6f}))':"
                f"y='(ih-ih/zoom)*(0.5+{pan_y:.2f}*cos(on*0.017453+{phase:.6f}))':"
                f"d=1:s={target_w}x{target_h}:fps=30,"
                "format=yuv420p,"
                "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
            )
        landscape = target_w > target_h
        segment_preset = "veryfast" if landscape else "ultrafast"
        segment_crf = "20" if landscape else "17"
        command = [
            ffmpeg_path,
            "-y",
            *input_args,
            "-t",
            f"{duration:.3f}",
            "-an",
            "-vf",
            video_filter,
            *self._h264_encode_argv(
                ffmpeg_path=ffmpeg_path,
                preset=segment_preset,
                crf=segment_crf,
                threads=2,
            ),
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            str(out_path),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"fast visual segment render failed for {source_path.name}: {exc}"
            ) from exc
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", errors="ignore")[-1600:].strip()
            # GPU encoders can fail on odd pixel formats; retry once on CPU.
            if self.last_video_encoder and self.last_video_encoder != "libx264":
                cpu_profile = resolve_h264_encode_profile(ffmpeg_path, force="libx264")
                cpu_command = [
                    ffmpeg_path,
                    "-y",
                    *input_args,
                    "-t",
                    f"{duration:.3f}",
                    "-an",
                    "-vf",
                    video_filter,
                    *cpu_profile.argv(preset=segment_preset, crf=segment_crf, threads=2),
                    "-pix_fmt",
                    "yuv420p",
                    "-color_range",
                    "tv",
                    "-colorspace",
                    "bt709",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-r",
                    "30",
                    "-fps_mode",
                    "cfr",
                    str(out_path),
                ]
                retry = subprocess.run(
                    cpu_command,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=180,
                )
                if retry.returncode == 0:
                    self.last_video_encoder = "libx264"
                    return
                diagnostic = (
                    diagnostic
                    + " | cpu_fallback: "
                    + retry.stderr.decode("utf-8", errors="ignore")[-800:].strip()
                )
            raise RuntimeError(
                f"fast visual segment render failed for {source_path.name} "
                f"(ffmpeg exit {result.returncode}): {diagnostic}"
            )

    def _run_ffmpeg_logged(
        self,
        command: list[str],
        *,
        label: str,
        diagnostic_path: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        normalized_command = [
            command[0],
            "-hide_banner",
            "-nostdin",
            "-nostats",
            *command[1:],
        ]
        try:
            completed = subprocess.run(
                normalized_command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial_stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            partial_stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            with diagnostic_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n===== {label} (TIMEOUT after {timeout}s) =====\n")
                handle.write(subprocess.list2cmdline(normalized_command) + "\n")
                handle.write(partial_stdout)
                handle.write(partial_stderr)
            raise RuntimeError(
                f"{label} timed out after {timeout}s; FFmpeg diagnostics: {diagnostic_path}"
            ) from exc

        diagnostic = "\n".join(
            item.strip() for item in (completed.stdout, completed.stderr) if item and item.strip()
        )
        with diagnostic_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {label} (exit {completed.returncode}) =====\n")
            handle.write(subprocess.list2cmdline(normalized_command) + "\n")
            if diagnostic:
                handle.write(diagnostic + "\n")

        if completed.returncode != 0:
            tail = diagnostic[-3000:] if diagnostic else "unknown FFmpeg error"
            raise RuntimeError(
                f"{label} failed (FFmpeg exit {completed.returncode}): {tail}; "
                f"full diagnostics: {diagnostic_path}"
            )
        return completed

    def _render_fast_video_track(
        self,
        *,
        ffmpeg_path: str,
        concat_path: Path,
        out_path: Path,
        duration: float,
        ass_path: Path | None,
        logo_path: Path | None,
        logo_width: int,
        logo_margin: int,
        diagnostic_path: Path,
        timeout: int,
    ) -> None:
        inputs = ["-f", "concat", "-safe", "0", "-i", str(concat_path)]
        video_filter = (
            "[0:v]setpts=PTS-STARTPTS,fps=30:round=near,format=yuv420p,"
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )
        if ass_path is not None and ass_path.exists():
            ass_filter_path = self._ffmpeg_filter_path(ass_path)
            font_dir = self._ffmpeg_filter_path(Path("assets/fonts"))
            video_filter += f",ass=filename='{ass_filter_path}':fontsdir='{font_dir}'"
        filters = [f"{video_filter}[v0]"]
        video_map = "[v0]"
        if logo_path and logo_path.exists():
            inputs.extend(["-loop", "1", "-i", str(logo_path)])
            filters.append(
                f"[1:v]scale={logo_width}:-1[logo];"
                f"[v0][logo]overlay=W-w-{logo_margin}:{logo_margin}:"
                "shortest=1:eof_action=endall:repeatlast=0,"
                "fps=30:round=near,setpts=N/(30*TB),format=yuv420p[vlogo]"
            )
            video_map = "[vlogo]"
        else:
            filters[0] = filters[0][:-4] + ",fps=30:round=near,setpts=N/(30*TB)[v0]"

        command = [
            ffmpeg_path,
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            video_map,
            "-an",
            "-t",
            f"{duration:.3f}",
            *self._h264_encode_argv(
                ffmpeg_path=ffmpeg_path,
                preset="veryfast",
                crf=21,
                threads=2,
                # Final concat/ASS/logo graphs are unreliable on older Intel QSV
                # drivers (Invalid FrameType). Keep GPU on segment encodes; use
                # CPU here for a stable finish.
                force="libx264",
            ),
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        self._run_ffmpeg_logged(
            command,
            label="final video-track render",
            diagnostic_path=diagnostic_path,
            timeout=timeout,
        )

    def _render_fast_audio_track(
        self,
        *,
        ffmpeg_path: str,
        narration_path: Path,
        music_path: Path | None,
        out_path: Path,
        duration: float,
        music_volume: float,
        diagnostic_path: Path,
        timeout: int,
        voice_tempo: float = 1.0,
        beat_timing: Sequence[NarrationBeatTiming] | None = None,
    ) -> None:
        duration_text = f"{duration:.3f}"
        inputs = ["-i", str(narration_path)]
        filters = self._fast_voice_filters(
            duration=duration,
            voice_tempo=voice_tempo,
            beat_timing=beat_timing,
        )
        if music_path and music_path.exists():
            inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])
            filters.append(
                # Levelled before the gain, not after. music_volume is a fixed
                # multiplier, so without this the bed's own mastering decides
                # the mix: the tracks in assets/music span -13 to -26 LUFS, a
                # 12 dB swing, and whichever one _pick_music happened to draw
                # would set how loud the music sat under the voice. Worse, the
                # loudnorm on the mix below would then pull the narration down
                # to compensate for a hot bed. Levelling here makes the gain
                # mean one thing for every track, including any the channel
                # owner drops in later.
                f"[1:a]loudnorm=I={self.MUSIC_BED_LUFS:.0f}:TP=-2.0:LRA=11,"
                "aresample=48000:async=1:first_pts=0,asetpts=N/SR/TB,"
                f"atrim=0:{duration_text},apad=pad_dur={duration_text},"
                f"atrim=0:{duration_text},volume={music_volume:.3f}[bg]"
            )
            filters.append(
                "[voice][bg]amix=inputs=2:duration=longest:dropout_transition=0[mix];"
                "[mix]loudnorm=I=-16:TP=-1.5:LRA=9,"
                f"aresample=48000:async=1:first_pts=0,apad=pad_dur={duration_text},"
                f"atrim=0:{duration_text},asetpts=N/SR/TB[aout]"
            )
        else:
            filters.append(
                "[voice]loudnorm=I=-16:TP=-1.5:LRA=9,"
                f"aresample=48000:async=1:first_pts=0,apad=pad_dur={duration_text},"
                f"atrim=0:{duration_text},asetpts=N/SR/TB[aout]"
            )

        command = [
            ffmpeg_path,
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[aout]",
            "-vn",
            "-t",
            duration_text,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        self._run_ffmpeg_logged(
            command,
            label="final audio-track render",
            diagnostic_path=diagnostic_path,
            timeout=timeout,
        )

    def _fast_voice_filters(
        self,
        *,
        duration: float,
        voice_tempo: float = 1.0,
        beat_timing: Sequence[NarrationBeatTiming] | None = None,
    ) -> list[str]:
        """Build the narration part of the fast-render filter graph.

        Long videos can provide an explicit beat timing plan.  Each raw beat is
        trimmed from the concatenated narration, tempo-adjusted independently,
        and concatenated again.  The resulting audio boundaries are identical
        to the caption and visual beat boundaries produced by preflight.
        """

        duration = float(duration)
        if duration <= 0:
            raise RuntimeError("audio render duration must be positive")
        duration_text = f"{duration:.3f}"
        if not beat_timing:
            if not 0.5 <= float(voice_tempo) <= 2.0:
                raise RuntimeError(f"unsafe narration tempo requested: {voice_tempo:.4f}")
            return [
                "[0:a]aresample=48000:async=1:first_pts=0,"
                f"atempo={float(voice_tempo):.8f},asetpts=N/SR/TB,"
                f"apad=pad_dur={duration_text},atrim=0:{duration_text},volume=1.0[voice]"
            ]

        timings = list(beat_timing)
        tolerance = 0.015
        for index, timing in enumerate(timings):
            if timing.input_duration <= 0 or timing.output_duration <= 0:
                raise RuntimeError(f"narration beat {index + 1} has a non-positive duration")
            if not 0.5 <= float(timing.tempo) <= 2.0:
                raise RuntimeError(
                    f"unsafe narration tempo requested for beat {index + 1}: {timing.tempo:.4f}"
                )
            expected_output = timing.input_duration / timing.tempo
            if abs(expected_output - timing.output_duration) > tolerance:
                raise RuntimeError(
                    f"narration beat {index + 1} tempo does not match its output duration"
                )
            if index == 0:
                if abs(timing.input_start) > tolerance or abs(timing.output_start) > tolerance:
                    raise RuntimeError("narration beat timing must start at zero")
            else:
                previous = timings[index - 1]
                if abs(timing.input_start - previous.input_end) > tolerance:
                    raise RuntimeError("narration input beat timing is not contiguous")
                if abs(timing.output_start - previous.output_end) > tolerance:
                    raise RuntimeError("narration output beat timing is not contiguous")
        if abs(timings[-1].output_end - duration) > tolerance:
            raise RuntimeError(
                "narration beat timing does not end at the requested render duration"
            )

        filters: list[str] = []
        source_labels = [f"voice_src_{index}" for index in range(len(timings))]
        if len(timings) == 1:
            filters.append(
                f"[0:a]aresample=48000:async=1:first_pts=0[{source_labels[0]}]"
            )
        else:
            outputs = "".join(f"[{label}]" for label in source_labels)
            filters.append(
                "[0:a]aresample=48000:async=1:first_pts=0,"
                f"asplit={len(timings)}{outputs}"
            )

        part_labels: list[str] = []
        for index, (source_label, timing) in enumerate(zip(source_labels, timings)):
            part_label = f"voice_part_{index}"
            part_labels.append(part_label)
            filters.append(
                f"[{source_label}]atrim=start={timing.input_start:.6f}:end={timing.input_end:.6f},"
                "asetpts=PTS-STARTPTS,"
                f"atempo={timing.tempo:.8f}[{part_label}]"
            )

        concat_inputs = "".join(f"[{label}]" for label in part_labels)
        filters.append(
            f"{concat_inputs}concat=n={len(part_labels)}:v=0:a=1,asetpts=N/SR/TB,"
            f"apad=pad_dur={duration_text},atrim=0:{duration_text},volume=1.0[voice]"
        )
        return filters

    def _mux_and_validate_fast_render(
        self,
        *,
        ffmpeg_path: str,
        video_track_path: Path,
        audio_track_path: Path,
        candidate_path: Path,
        out_path: Path,
        duration: float,
        diagnostic_path: Path,
        timeout: int,
    ) -> MediaValidationReport:
        command = [
            ffmpeg_path,
            "-y",
            "-i",
            str(video_track_path),
            "-i",
            str(audio_track_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(candidate_path),
        ]
        self._run_ffmpeg_logged(
            command,
            label="final audio/video mux",
            diagnostic_path=diagnostic_path,
            timeout=timeout,
        )

        report = validate_final_mp4(
            candidate_path,
            expected_duration=duration,
            ffmpeg_path=ffmpeg_path,
            timeout=timeout,
        )
        report_path = out_path.with_suffix(".media_validation.json")
        if not report.ok:
            failure_report = replace(report, path=str(out_path.resolve()))
            failure_report.write_json(report_path)
            failure_report.raise_for_failure()

        os.replace(candidate_path, out_path)
        final_report = replace(report, path=str(out_path.resolve()))
        final_report.write_json(report_path)
        return final_report

    def validate_output(
        self,
        out_path: Path,
        *,
        expected_duration: float | None = None,
        ffmpeg_path: str | Path | None = None,
        timeout: int = 900,
    ) -> MediaValidationReport:
        """Public hook for validating any final render, including pipeline fallbacks."""

        report = validate_final_mp4(
            out_path,
            expected_duration=expected_duration,
            ffmpeg_path=ffmpeg_path,
            timeout=timeout,
        )
        report.write_json(out_path.with_suffix(".media_validation.json"))
        report.raise_for_failure()
        return report

    def _cps_issues_only(self, issues: list[str]) -> bool:
        return bool(issues) and all(" reads at " in issue for issue in issues)

    def _reflow_short_caption_chunks(
        self,
        beat_segments: list,
        *,
        target_cps: float,
    ) -> list:
        """Retry caption composition with a wider word budget when cues are dense.

        Clears both CPS density and structural mid-phrase defects when a 6-word
        budget can produce cleaner phrase boundaries than the 4-word default.
        """
        composer = self.subtitle_composer
        captions = composer.caption_segments_from_scene_segments(beat_segments)
        issues = composer.quality_issues(captions, cps_tolerance=0.0)
        if not issues:
            return captions
        if composer.max_words < 6:
            wider = SubtitleComposer(
                max_words_per_caption=6,
                max_chars_per_second=target_cps,
            )
            wider_captions = wider.caption_segments_from_scene_segments(beat_segments)
            wider_issues = wider.quality_issues(wider_captions, cps_tolerance=0.0)
            if not wider_issues:
                return wider_captions
            if len(wider_issues) < len(issues):
                return wider_captions
            # Prefer the wider track when only mid-phrase starts remain and the
            # original track has more total defects.
            wider_structural = [i for i in wider_issues if " reads at " not in i]
            original_structural = [i for i in issues if " reads at " not in i]
            if len(wider_structural) < len(original_structural):
                return wider_captions
        return captions

    def _equalize_short_caption_timing(
        self,
        captions: list,
        *,
        total_duration: float,
        target_cps: float,
        composer: SubtitleComposer,
    ) -> list:
        """Rebuild cue times by character count so dense outliers share the budget."""
        texts = [str(item.text or "").strip() for item in captions if str(item.text or "").strip()]
        if len(texts) < 2 or total_duration <= 0:
            return captions
        rebuilt = composer._timeline(
            texts,
            total_duration,
            min_seconds=min(0.75, total_duration / max(1, len(texts))),
            max_cps=target_cps,
        )
        return rebuilt or captions

    def _plan_short_caption_timeline(
        self,
        *,
        beat_segments: list,
        raw_duration: float,
        duration_bounds: tuple[float, float],
        composer: SubtitleComposer | None = None,
    ) -> tuple[float, list, list, float]:
        """Compose short captions; auto-repair CPS density before aborting.

        Returns ``(duration, scaled_beats, captions, voice_tempo)``. Stretch is
        bounded and only applied when reflow alone cannot clear the 16.5 CPS
        repair gate. Structural caption defects still fail closed after a
        wider-budget reflow attempt.
        """
        composer = composer or self.subtitle_composer
        min_duration, max_duration = (float(value) for value in duration_bounds)
        if raw_duration <= 0:
            raise ShortCaptionPreflightError("short narration has no measurable duration")
        if not beat_segments:
            raise ShortCaptionPreflightError("no narration beat timeline was generated for the short")

        target_cps = max(10.0, min(float(composer.max_cps), self.SHORT_CAPTION_TARGET_CPS))
        captions = self._reflow_short_caption_chunks(beat_segments, target_cps=target_cps)
        issues = composer.quality_issues(captions, cps_tolerance=0.0)
        # If 4-word composer still reports structural issues on a 6-word track,
        # re-check with the same max_words as the successful reflow.
        structural = [issue for issue in issues if " reads at " not in issue]
        allow_continuations = False
        if structural and composer.max_words < 6:
            wider = SubtitleComposer(
                max_words_per_caption=6,
                max_chars_per_second=target_cps,
            )
            wider_captions = wider.caption_segments_from_scene_segments(beat_segments)
            # Evaluate with a 6-word composer so hard_word_limit matches the track.
            wider_issues = wider.quality_issues(wider_captions, cps_tolerance=0.0)
            if not wider_issues or len([i for i in wider_issues if " reads at " not in i]) < len(structural):
                captions = wider_captions
                issues = wider_issues
                composer = wider
                structural = [issue for issue in issues if " reads at " not in issue]
        if structural:
            # Long-form already allows readable clause continuations. After a
            # wider-budget reflow, permit the same for shorts when the only
            # remaining defects are mid-phrase starts (not flash fragments or
            # dangling incomplete tails).
            midphrase_only = all(
                "begins in the middle of a phrase" in issue for issue in structural
            )
            if midphrase_only:
                continued = composer.quality_issues(
                    captions,
                    cps_tolerance=0.0,
                    allow_clause_continuations=True,
                )
                continued_structural = [
                    issue for issue in continued if " reads at " not in issue
                ]
                if not continued_structural:
                    structural = []
                    issues = continued
                    allow_continuations = True
        if structural:
            raise ShortCaptionPreflightError(
                "short caption preflight failed before visual encoding: "
                + "; ".join(structural[:5])
            )

        duration = min(max_duration, max(min_duration, raw_duration))
        voice_tempo = 1.0
        scaled_beats = list(beat_segments)
        narration_words = sum(
            len(re.findall(r"[A-Za-z0-9']+", str(getattr(segment, "text", "") or "")))
            for segment in beat_segments
        )
        if narration_words > 0:
            # Keep stretch from dropping below the production 135 WPM gate.
            pacing_ceiling = narration_words * 60.0 / 135.0
            if pacing_ceiling >= min_duration:
                max_duration = min(max_duration, pacing_ceiling)
                duration = min(duration, max_duration)

        def worst_cps(items: list) -> float:
            peak = 0.0
            for caption in items:
                caption_duration = max(0.001, float(caption.end) - float(caption.start))
                peak = max(
                    peak,
                    composer._character_count(caption.text) / caption_duration,
                )
            return peak

        cps_issues = [issue for issue in issues if " reads at " in issue]
        if cps_issues:
            required_scale = max(1.0, worst_cps(captions) / target_cps)
            requested_duration = raw_duration * required_scale
            if requested_duration > max_duration + 0.001:
                requested_duration = max_duration
                required_scale = requested_duration / max(0.001, raw_duration)
            required_scale = min(required_scale, self.MAX_SHORT_NARRATION_STRETCH)
            if required_scale > 1.0001:
                scaled_beats = []
                output_cursor = 0.0
                for segment in beat_segments:
                    raw_beat = max(0.001, float(segment.end) - float(segment.start))
                    output_end = output_cursor + (raw_beat * required_scale)
                    scaled_beats.append(replace(segment, start=output_cursor, end=output_end))
                    output_cursor = output_end
                duration = min(max_duration, output_cursor)
                captions = composer.caption_segments_from_scene_segments(scaled_beats)
                voice_tempo = raw_duration / max(0.001, duration)
                # Prefer keeping >=135 WPM. Remaining CPS pressure is handled by
                # equalize/reflow below; aborting here caused endless retries on
                # otherwise usable continuity Shorts.

        captions = self._equalize_short_caption_timing(
            captions,
            total_duration=duration,
            target_cps=target_cps,
            composer=composer,
        )
        equalized_issues = composer.quality_issues(
            captions,
            cps_tolerance=0.05,
            allow_clause_continuations=allow_continuations,
        )
        if any(" reads at " not in issue for issue in equalized_issues) and not cps_issues:
            captions = composer.caption_segments_from_scene_segments(scaled_beats)
        final_issues = composer.quality_issues(
            captions,
            cps_tolerance=0.05,
            allow_clause_continuations=allow_continuations,
        )
        cps_remaining = [issue for issue in final_issues if " reads at " in issue]
        structural_remaining = [issue for issue in final_issues if " reads at " not in issue]
        if cps_remaining and not structural_remaining:
            captions = self._reflow_short_caption_chunks(scaled_beats, target_cps=target_cps)
            captions = self._equalize_short_caption_timing(
                captions,
                total_duration=duration,
                target_cps=target_cps,
                composer=composer,
            )
            final_issues = composer.quality_issues(
                captions,
                cps_tolerance=0.05,
                allow_clause_continuations=allow_continuations,
            )
        if final_issues:
            raise ShortCaptionPreflightError(
                "short caption preflight failed before visual encoding: "
                + "; ".join(final_issues[:5])
            )
        return duration, scaled_beats, captions, voice_tempo

    def _build_short_fast_ffmpeg(
        self,
        image_paths: List[Path],
        narration_path: Path,
        out_path: Path,
        narration_text: str,
        subtitles_path: Path | None,
        title_text: str,
        visual_captions: List[str] | None,
        narration_beats: List[str] | None,
        scene_durations: List[float] | None,
        duration_bounds: tuple[int, int],
        logo_path: Path | None,
        music_dir: Path,
        channel_id: str | None = None,
    ) -> float:
        import imageio_ffmpeg

        self.last_claim_card_text = ""
        raw_narration = AudioFileClip(str(narration_path))
        try:
            min_duration, max_duration = duration_bounds
            raw_duration = float(raw_narration.duration)
            story_beats = self._story_beats(
                title_text=title_text,
                narration_text=narration_text,
                visual_captions=visual_captions,
                narration_beats=narration_beats,
            )
            provisional_duration = min(max_duration, max(1.0, raw_duration))
            if scene_durations:
                beat_segments = self.subtitle_composer.scene_segments_from_durations(
                    story_beats or narration_text,
                    scene_durations,
                    total_duration=provisional_duration,
                )
            else:
                beat_segments = self.subtitle_composer.scene_segments(
                    story_beats or narration_text,
                    provisional_duration,
                )
            if not beat_segments:
                beat_segments = self.subtitle_composer.scene_segments(
                    title_text or "Story",
                    provisional_duration,
                )

            voice_tempo = 1.0
            subtitle_segments: list = []
            if self.subtitle_enabled and subtitles_path is not None and narration_text.strip():
                duration, beat_segments, subtitle_segments, voice_tempo = (
                    self._plan_short_caption_timeline(
                        beat_segments=beat_segments,
                        raw_duration=raw_duration,
                        duration_bounds=(float(min_duration), float(max_duration)),
                    )
                )
                self.subtitle_composer.write_srt(subtitles_path, subtitle_segments)
            else:
                duration = provisional_duration

            fast_segments, scene_images, beat_indices = self._beat_aligned_visual_plan(
                image_paths=image_paths,
                beat_segments=beat_segments,
                story_beats=story_beats,
                duration=duration,
            )
            self._write_visual_timeline(out_path, fast_segments, scene_images, beat_indices)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            temp_dir = Path(tempfile.mkdtemp(prefix="fast_short_", dir=str(out_path.parent)))
            try:
                segment_entries = []
                for idx, segment in enumerate(fast_segments):
                    segment_path = temp_dir / f"segment_{idx:03d}.mp4"
                    self._render_fast_segment(
                        ffmpeg_path=imageio_ffmpeg.get_ffmpeg_exe(),
                        source_path=scene_images[idx],
                        out_path=segment_path,
                        duration=max(0.8, segment.end - segment.start),
                        is_hook=(idx == 0),
                        source_offset=self._source_motion_offset(
                            scene_images[idx],
                            idx,
                            base=0.2,
                            step=0.45,
                            modulo=5,
                        ),
                    )
                    segment_entries.append(segment_path)

                concat_path = temp_dir / "segments.txt"
                with concat_path.open("w", encoding="utf-8") as handle:
                    for segment_path in segment_entries:
                        handle.write(f"file '{segment_path.as_posix()}'\n")

                ass_path = temp_dir / "captions.ass"
                claim_card = self._short_claim_card_text(title_text, channel_id)
                if channel_id == "brain_lens" and not claim_card:
                    raise ShortCaptionPreflightError(
                        "Brain Lens Short requires a first-second claim card from the title"
                    )
                if self.subtitle_enabled and subtitle_segments:
                    self._write_ass_captions(
                        subtitle_segments[:28],
                        ass_path,
                        claim_card_text=claim_card,
                    )
                elif claim_card:
                    self._write_ass_captions([], ass_path, claim_card_text=claim_card)

                ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                diagnostic_path = out_path.with_suffix(".ffmpeg.log")
                diagnostic_path.write_text(
                    "Fast short two-stage render diagnostics\n",
                    encoding="utf-8",
                )
                music_path = self._pick_music(music_dir)
                self.last_music_path = music_path
                video_track_path = temp_dir / "video_track.mp4"
                audio_track_path = temp_dir / "audio_track.m4a"
                candidate_path = temp_dir / "validated_candidate.mp4"
                self._render_fast_video_track(
                    ffmpeg_path=ffmpeg_path,
                    concat_path=concat_path,
                    out_path=video_track_path,
                    duration=duration,
                    ass_path=ass_path if ass_path.exists() else None,
                    logo_path=logo_path,
                    logo_width=max(88, int(self.target_size[0] * 0.11)),
                    logo_margin=36,
                    diagnostic_path=diagnostic_path,
                    timeout=300,
                )
                self._render_fast_audio_track(
                    ffmpeg_path=ffmpeg_path,
                    narration_path=narration_path,
                    music_path=music_path,
                    out_path=audio_track_path,
                    duration=duration,
                    music_volume=0.105,
                    diagnostic_path=diagnostic_path,
                    timeout=300,
                    voice_tempo=voice_tempo,
                )
                self._mux_and_validate_fast_render(
                    ffmpeg_path=ffmpeg_path,
                    video_track_path=video_track_path,
                    audio_track_path=audio_track_path,
                    candidate_path=candidate_path,
                    out_path=out_path,
                    duration=duration,
                    diagnostic_path=diagnostic_path,
                    timeout=300,
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            if not out_path.exists() or out_path.stat().st_size < 100000:
                raise RuntimeError("fast short renderer produced no usable video")
            return duration
        finally:
            raw_narration.close()

    def _plan_long_caption_timeline(
        self,
        *,
        beat_segments: list,
        raw_duration: float,
        duration_bounds: tuple[int, int],
        composer: SubtitleComposer,
        include_beat_timing: bool = False,
        narration_word_count: int | None = None,
    ) -> tuple:
        """Preflight a readable long-caption track before visual encoding.

        Beats whose captions need extra reading time are slowed. When a complete
        production script would otherwise miss the long-form pacing band, sparse
        beats may also be accelerated by at most ten percent. The same per-beat
        plan is later applied to narration audio, captions, and visuals.

        The default four-value return preserves the previous private API.  Fast
        long rendering opts into a fifth value containing the beat timing plan.
        """

        min_duration, max_duration = (float(value) for value in duration_bounds)
        if min_duration <= 0 or max_duration < min_duration:
            raise LongCaptionPreflightError(
                f"invalid long duration bounds: {min_duration:.1f}-{max_duration:.1f}s"
            )
        if raw_duration <= 0:
            raise LongCaptionPreflightError("long narration has no measurable duration")
        if not beat_segments:
            raise LongCaptionPreflightError("no narration beat timeline was generated for the long video")
        # Thin or malformed inputs must not use pacing normalization to disguise
        # a script-length failure -- but "thin" has to mean thin for THIS
        # channel. A flat 850 was written when long videos ran eight to ten
        # minutes. At four to six the writer aims at 775 words and the
        # editorial gate accepts 658, so every correctly sized script fell
        # under the flat floor, pacing switched off, and the builder lost the
        # one mechanism that absorbs a beat running slightly long. A finished
        # 25-minute build was thrown away over a beat needing 8.7% where 8.5%
        # was allowed.
        #
        # The fewest words a valid long video can hold is its shortest
        # permitted duration spoken at the slowest permitted pace. Below that
        # the script really is a stub.
        pacing_word_floor = max(
            1, int(min_duration * self.MIN_LONG_NARRATION_WPM / 60.0)
        )
        pacing_enabled = bool(
            narration_word_count and narration_word_count >= pacing_word_floor
        )
        if raw_duration > max_duration + 0.001 and not pacing_enabled:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                f"narration is {raw_duration:.1f}s, above the configured "
                f"{max_duration:.1f}s maximum"
            )

        continuity_tolerance = 0.05
        raw_beat_durations: list[float] = []
        raw_cursor = 0.0
        for index, segment in enumerate(beat_segments):
            start = float(segment.start)
            end = float(segment.end)
            if end <= start:
                raise LongCaptionPreflightError(
                    f"long caption preflight failed before visual encoding: beat {index + 1} "
                    "has a non-positive duration"
                )
            if abs(start - raw_cursor) > continuity_tolerance:
                raise LongCaptionPreflightError(
                    "long caption preflight failed before visual encoding: "
                    f"beat {index + 1} does not follow the narration timeline"
                )
            raw_beat_durations.append(end - start)
            raw_cursor = end
        if abs(raw_cursor - raw_duration) > continuity_tolerance:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: beat timing "
                "does not cover the complete narration track"
            )

        initial_captions = composer.caption_segments_from_scene_segments(beat_segments)
        initial_issues = composer.quality_issues(
            initial_captions,
            cps_tolerance=0.0,
            allow_clause_continuations=True,
        )
        structural_issues = [issue for issue in initial_issues if " reads at " not in issue]
        if structural_issues:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                + "; ".join(structural_issues[:5])
            )

        # Aim just below the hard 17 CPS limit.  This tiny margin absorbs SRT
        # millisecond rounding without weakening the configured readability gate.
        target_cps = max(10.0, composer.max_cps - self.LONG_CAPTION_TARGET_CPS_OFFSET)
        minimum_scale = (
            1.0 / self.MAX_LONG_NARRATION_ACCELERATION
            if pacing_enabled
            else 1.0
        )
        required_scales: list[float] = []
        for beat in beat_segments:
            beat_scale = minimum_scale
            beat_captions = composer.caption_segments_from_scene_segments([beat])
            for caption in beat_captions:
                caption_duration = max(0.001, float(caption.end) - float(caption.start))
                cps = composer._character_count(caption.text) / caption_duration
                beat_scale = max(beat_scale, cps / target_cps)
            required_scales.append(beat_scale)

        caption_duration = sum(
            beat_duration * scale
            for beat_duration, scale in zip(raw_beat_durations, required_scales)
        )
        if pacing_enabled:
            assert narration_word_count is not None
            pacing_floor_duration = (
                float(narration_word_count) * 60.0 / self.MAX_LONG_NARRATION_WPM
            )
            pacing_ceiling_duration = (
                float(narration_word_count) * 60.0 / self.MIN_LONG_NARRATION_WPM
            )
            minimum_duration = max(
                min_duration,
                caption_duration,
                pacing_floor_duration,
            )
            maximum_duration = min(max_duration, pacing_ceiling_duration)
            pacing_acceleration = raw_duration / max(0.001, maximum_duration)
            if pacing_acceleration > self.MAX_LONG_NARRATION_ACCELERATION + 0.0001:
                raise LongCaptionPreflightError(
                    "long caption preflight failed before visual encoding: "
                    f"required narration acceleration is {(pacing_acceleration - 1.0) * 100:.1f}%, "
                    f"above the safe {(self.MAX_LONG_NARRATION_ACCELERATION - 1.0) * 100:.1f}% limit"
                )
            if minimum_duration > maximum_duration + 0.001:
                raise LongCaptionPreflightError(
                    "long caption preflight failed before visual encoding: "
                    f"caption readability and {self.MIN_LONG_NARRATION_WPM:.0f}-"
                    f"{self.MAX_LONG_NARRATION_WPM:.0f} WPM pacing need at least "
                    f"{minimum_duration:.1f}s but allow at most {maximum_duration:.1f}s"
                )
            requested_duration = min(
                maximum_duration,
                max(minimum_duration, raw_duration),
            )
        else:
            requested_duration = max(min_duration, caption_duration)
        if requested_duration > max_duration + 0.001:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                f"the {composer.max_cps:.1f} CPS gate needs {requested_duration:.1f}s, "
                f"but the configured maximum is {max_duration:.1f}s"
            )

        total_stretch = requested_duration / raw_duration
        if total_stretch > self.MAX_LONG_NARRATION_STRETCH + 0.0001:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                f"required total narration stretch is {(total_stretch - 1.0) * 100:.1f}%, "
                f"above the safe {((self.MAX_LONG_NARRATION_STRETCH - 1.0) * 100):.1f}% limit"
            )
        max_required_scale = max(required_scales)
        if max_required_scale > self.MAX_LONG_BEAT_STRETCH + 0.0001:
            beat_number = required_scales.index(max_required_scale) + 1
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                # Two decimals, because one of these read "requires 8.5%
                # stretch, above the safe 8.5% limit" and gave whoever saw it
                # nothing to act on.
                f"beat {beat_number} requires {(max_required_scale - 1.0) * 100:.2f}% stretch, "
                f"above the safe {((self.MAX_LONG_BEAT_STRETCH - 1.0) * 100):.2f}% per-beat limit"
            )
        total_acceleration = raw_duration / requested_duration
        if total_acceleration > self.MAX_LONG_NARRATION_ACCELERATION + 0.0001:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                f"required narration acceleration is {(total_acceleration - 1.0) * 100:.1f}%, "
                f"above the safe {(self.MAX_LONG_NARRATION_ACCELERATION - 1.0) * 100:.1f}% limit"
            )

        # If the profile minimum needs more time than the captions themselves,
        # raise the least-stretched beats toward a common scale.  This water-fill
        # allocation minimizes audible tempo differences and never exceeds the
        # same per-beat safety ceiling.
        if caption_duration < requested_duration - 0.000001:
            low = minimum_scale
            high = self.MAX_LONG_NARRATION_STRETCH
            for _ in range(64):
                level = (low + high) / 2.0
                trial_duration = sum(
                    beat_duration * max(required_scale, level)
                    for beat_duration, required_scale in zip(raw_beat_durations, required_scales)
                )
                if trial_duration < requested_duration:
                    low = level
                else:
                    high = level
            required_scales = [max(scale, high) for scale in required_scales]

        scaled_beats = []
        beat_timing: list[NarrationBeatTiming] = []
        output_cursor = 0.0
        for segment, raw_beat_duration, stretch in zip(
            beat_segments,
            raw_beat_durations,
            required_scales,
        ):
            output_end = output_cursor + (raw_beat_duration * stretch)
            scaled_beats.append(
                replace(segment, start=output_cursor, end=output_end)
            )
            beat_timing.append(
                NarrationBeatTiming(
                    input_start=float(segment.start),
                    input_end=float(segment.end),
                    output_start=output_cursor,
                    output_end=output_end,
                    tempo=1.0 / stretch,
                )
            )
            output_cursor = output_end

        requested_duration = output_cursor
        if requested_duration > max_duration + 0.001:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                f"the synchronized beat plan is {requested_duration:.1f}s, "
                f"but the configured maximum is {max_duration:.1f}s"
            )
        captions = composer.caption_segments_from_scene_segments(scaled_beats)
        final_issues = composer.quality_issues(
            captions,
            cps_tolerance=0.0,
            allow_clause_continuations=True,
        )
        if final_issues:
            raise LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: "
                + "; ".join(final_issues[:5])
            )

        voice_tempo = raw_duration / requested_duration
        result = (requested_duration, scaled_beats, captions, voice_tempo)
        if include_beat_timing:
            return (*result, beat_timing)
        return result

    def _build_video_fast_ffmpeg(
        self,
        image_paths: List[Path],
        narration_path: Path,
        out_path: Path,
        narration_text: str,
        subtitles_path: Path | None,
        title_text: str,
        visual_captions: List[str] | None,
        narration_beats: List[str] | None,
        scene_durations: List[float] | None,
        duration_bounds: tuple[int, int],
        logo_path: Path | None,
        music_dir: Path,
    ) -> float:
        import imageio_ffmpeg

        raw_narration = AudioFileClip(str(narration_path))
        try:
            min_duration, max_duration = duration_bounds
            raw_duration = float(raw_narration.duration)
            if raw_duration <= 0:
                raise RuntimeError("long narration has no measurable duration")
            story_beats = self._story_beats(
                title_text=title_text,
                narration_text=narration_text,
                visual_captions=visual_captions,
                narration_beats=narration_beats,
            )
            if scene_durations:
                beat_segments = self.subtitle_composer.scene_segments_from_durations(
                    story_beats or narration_text,
                    scene_durations,
                    total_duration=raw_duration,
                )
            else:
                beat_segments = self.subtitle_composer.scene_segments(story_beats or narration_text, raw_duration)
            if not beat_segments:
                raise RuntimeError("no narration beat timeline was generated for the long video")

            duration = raw_duration
            voice_tempo = 1.0
            beat_timing: list[NarrationBeatTiming] | None = None
            subtitle_segments = []
            if self.subtitle_enabled and subtitles_path is not None and narration_text.strip():
                # Long-form final QA permits eight-word cues. Using the same
                # ceiling during composition gives the optimizer enough room to
                # keep subject/verb, modifier/noun, and verb/complement phrases
                # intact; the 17 CPS and 8% adaptive-stretch gates remain strict.
                long_caption_composer = SubtitleComposer(max_words_per_caption=8)
                duration, beat_segments, subtitle_segments, voice_tempo, beat_timing = self._plan_long_caption_timeline(
                    beat_segments=beat_segments,
                    raw_duration=raw_duration,
                    duration_bounds=duration_bounds,
                    composer=long_caption_composer,
                    include_beat_timing=True,
                    narration_word_count=len(
                        re.findall(r"[A-Za-z0-9']+", narration_text or "")
                    ),
                )
                long_caption_composer.write_srt(subtitles_path, subtitle_segments)
                if duration > raw_duration + 0.01:
                    slowest_tempo = min(timing.tempo for timing in beat_timing)
                    print(
                        f"Adjusted long narration timing from {raw_duration:.2f}s to {duration:.2f}s "
                        f"(per-beat atempo={slowest_tempo:.5f}-1.00000) to satisfy the "
                        f"{long_caption_composer.max_cps:.1f} CPS caption gate."
                    )
            elif not float(min_duration) <= duration <= float(max_duration):
                raise RuntimeError(
                    f"long narration duration {duration:.1f}s is outside the configured "
                    f"{float(min_duration):.1f}-{float(max_duration):.1f}s range"
                )

            fast_segments, scene_images, beat_indices = self._beat_aligned_visual_plan(
                image_paths=image_paths,
                beat_segments=beat_segments,
                story_beats=story_beats,
                duration=duration,
            )
            if not fast_segments or len(fast_segments) != len(scene_images):
                raise RuntimeError("long visual timeline is empty or incomplete")
            self._write_visual_timeline(out_path, fast_segments, scene_images, beat_indices)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            temp_dir = Path(tempfile.mkdtemp(prefix="fast_video_", dir=str(out_path.parent)))
            try:
                ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                segment_entries: List[Path] = []
                total_segments = len(fast_segments)
                for idx, segment in enumerate(fast_segments):
                    if idx == 0 or (idx + 1) % 10 == 0 or idx + 1 == total_segments:
                        print(f"Rendering long visual segment {idx + 1}/{total_segments}")
                    segment_path = temp_dir / f"segment_{idx:03d}.mp4"
                    self._render_fast_segment(
                        ffmpeg_path=ffmpeg_path,
                        source_path=scene_images[idx],
                        out_path=segment_path,
                        duration=max(0.8, float(segment.end) - float(segment.start)),
                        is_hook=(idx == 0),
                        source_offset=self._source_motion_offset(
                            scene_images[idx],
                            idx,
                            base=0.25,
                            step=0.55,
                            modulo=6,
                        ),
                    )
                    segment_entries.append(segment_path)

                concat_path = temp_dir / "segments.txt"
                with concat_path.open("w", encoding="utf-8") as handle:
                    for segment_path in segment_entries:
                        escaped_path = segment_path.as_posix().replace("'", "'\\''")
                        handle.write(f"file '{escaped_path}'\n")

                ass_path = temp_dir / "captions.ass"
                if self.subtitle_enabled and subtitle_segments:
                    self._write_ass_captions(subtitle_segments, ass_path)

                diagnostic_path = out_path.with_suffix(".ffmpeg.log")
                diagnostic_path.write_text(
                    "Fast long two-stage render diagnostics\n",
                    encoding="utf-8",
                )
                music_path = self._pick_music(music_dir)
                self.last_music_path = music_path
                video_track_path = temp_dir / "video_track.mp4"
                audio_track_path = temp_dir / "audio_track.m4a"
                candidate_path = temp_dir / "validated_candidate.mp4"
                logo_width = max(92, int(min(self.target_size) * 0.10))
                logo_margin = max(28, int(min(self.target_size) * 0.035))
                self._render_fast_video_track(
                    ffmpeg_path=ffmpeg_path,
                    concat_path=concat_path,
                    out_path=video_track_path,
                    duration=duration,
                    ass_path=ass_path if ass_path.exists() else None,
                    logo_path=logo_path,
                    logo_width=logo_width,
                    logo_margin=logo_margin,
                    diagnostic_path=diagnostic_path,
                    timeout=2400,
                )
                self._render_fast_audio_track(
                    ffmpeg_path=ffmpeg_path,
                    narration_path=narration_path,
                    music_path=music_path,
                    out_path=audio_track_path,
                    duration=duration,
                    music_volume=0.075,
                    diagnostic_path=diagnostic_path,
                    timeout=2400,
                    voice_tempo=voice_tempo,
                    beat_timing=beat_timing,
                )
                self._mux_and_validate_fast_render(
                    ffmpeg_path=ffmpeg_path,
                    video_track_path=video_track_path,
                    audio_track_path=audio_track_path,
                    candidate_path=candidate_path,
                    out_path=out_path,
                    duration=duration,
                    diagnostic_path=diagnostic_path,
                    timeout=2400,
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            if not out_path.exists() or out_path.stat().st_size < 1000000:
                raise RuntimeError("fast long renderer produced no usable video")
            return duration
        finally:
            raw_narration.close()

    def build(
        self,
        image_paths: List[Path],
        narration_path: Path,
        music_dir: Path,
        out_path: Path,
        narration_text: str = '',
        subtitles_path: Path | None = None,
        title_text: str = '',
        visual_captions: List[str] | None = None,
        narration_beats: List[str] | None = None,
        scene_durations: List[float] | None = None,
        target_size: tuple[int, int] | None = None,
        duration_bounds: tuple[int, int] | None = None,
        logo_path: Path | None = None,
        content_kind: str = "short",
        presenter_avatar_path: Path | None = None,
        presenter_layout_mode: str = "rotating_avatar_broll",
        channel_id: str | None = None,
    ) -> float:
        if target_size:
            self.target_size = target_size
        self.last_music_path = None
        self.last_claim_card_text = ""
        if not image_paths:
            raise RuntimeError('No images available to build video')

        min_duration, max_duration = duration_bounds or (self.min_duration, self.max_duration)
        if content_kind == "short" and os.getenv("YT_DISABLE_FAST_SHORT_RENDER", "").lower() not in {"1", "true", "yes"}:
            try:
                return self._build_short_fast_ffmpeg(
                    image_paths=image_paths,
                    narration_path=narration_path,
                    out_path=out_path,
                    narration_text=narration_text,
                    subtitles_path=subtitles_path,
                    title_text=title_text,
                    visual_captions=visual_captions,
                    narration_beats=narration_beats,
                    scene_durations=scene_durations,
                    duration_bounds=(min_duration, max_duration),
                    logo_path=logo_path,
                    music_dir=music_dir,
                    channel_id=channel_id,
                )
            except ShortCaptionPreflightError:
                # A different renderer cannot repair a structurally broken or
                # unreadable caption timeline, so never bypass this gate.
                raise
            except Exception as exc:
                if os.getenv("YT_ALLOW_SLOW_SHORT_RENDER", "").lower() not in {"1", "true", "yes"}:
                    raise RuntimeError(f"Fast short renderer failed; slow fallback is disabled: {exc}") from exc
                print(f"Fast short renderer fallback to MoviePy: {exc}")

        if content_kind == "video" and os.getenv("YT_DISABLE_FAST_LONG_RENDER", "").lower() not in {"1", "true", "yes"}:
            try:
                return self._build_video_fast_ffmpeg(
                    image_paths=image_paths,
                    narration_path=narration_path,
                    out_path=out_path,
                    narration_text=narration_text,
                    subtitles_path=subtitles_path,
                    title_text=title_text,
                    visual_captions=visual_captions,
                    narration_beats=narration_beats,
                    scene_durations=scene_durations,
                    duration_bounds=(min_duration, max_duration),
                    logo_path=logo_path,
                    music_dir=music_dir,
                )
            except LongCaptionPreflightError:
                # A slower renderer cannot repair an over-dense or structurally
                # broken caption timeline, so never bypass the upload gate.
                raise
            except Exception as exc:
                if os.getenv("YT_ALLOW_SLOW_LONG_RENDER", "").lower() not in {"1", "true", "yes"}:
                    raise RuntimeError(f"Fast long renderer failed; memory-heavy fallback is disabled: {exc}") from exc
                raise RuntimeError(
                    "Fast long renderer failed; slow long fallback was refused because "
                    "it cannot preserve the adaptive per-beat narration, caption, and "
                    "visual timing plan"
                ) from exc

        if content_kind == "video":
            # The MoviePy path below predates adaptive long-caption timing: it
            # truncates at max_duration and writes nine-word section labels rather
            # than applying the synchronized per-beat atempo plan. Entering it via
            # YT_DISABLE_FAST_LONG_RENDER would therefore bypass the upload gate.
            raise RuntimeError(
                "Slow long rendering is disabled because it cannot preserve the "
                "adaptive per-beat narration, caption, and visual timing plan"
            )

        raw_narration = AudioFileClip(str(narration_path))
        spoken_duration = raw_narration.duration
        narration = self._narration_with_padding(raw_narration)

        duration = min(max_duration, max(1.0, narration.duration))
        narration = narration.subclip(0, duration)
        spoken_duration = max(1.0, min(duration, spoken_duration))

        story_beats = self._story_beats(
            title_text=title_text,
            narration_text=narration_text,
            visual_captions=visual_captions,
            narration_beats=narration_beats,
        )

        scene_segments = []
        if scene_durations:
            scene_segments = self.subtitle_composer.scene_segments_from_durations(
                story_beats or narration_text,
                scene_durations,
                total_duration=duration,
            )
        if not scene_segments:
            scene_segments = self.subtitle_composer.scene_segments(story_beats or narration_text, duration)
        if not scene_segments:
            scene_segments = self.subtitle_composer.scene_segments(title_text or 'Story', max(1.0, duration))
        if not scene_segments:
            raise RuntimeError('No scene segments were generated for this short')

        # ── Step 1: build ORIGINAL beat-locked segments (used for subtitle timing) ──
        beat_segments = scene_segments  # timings match actual TTS audio beats

        # ── Step 2: build FAST VISUAL segments (for clip switching every ~4s) ──
        fast_visual_segments, scene_images, beat_indices = self._beat_aligned_visual_plan(
            image_paths=image_paths,
            beat_segments=beat_segments,
            story_beats=story_beats,
            duration=duration,
        )
        self._write_visual_timeline(out_path, fast_visual_segments, scene_images, beat_indices)

        # ── Step 3: subtitles use ORIGINAL beat_segments (synced to audio) ──
        subtitle_segments = []
        voice_tempo = 1.0
        if self.subtitle_enabled and subtitles_path is not None and narration_text.strip():
            if content_kind == "video":
                subtitle_segments = [
                    segment.__class__(
                        start=segment.start,
                        end=segment.end,
                        text=self._section_caption_text(segment.text, content_kind),
                    )
                    for segment in beat_segments
                    if self._section_caption_text(segment.text, content_kind)
                ]
            else:
                duration, beat_segments, subtitle_segments, voice_tempo = (
                    self._plan_short_caption_timeline(
                        beat_segments=beat_segments,
                        raw_duration=spoken_duration,
                        duration_bounds=(float(min_duration), float(max_duration)),
                    )
                )
                if abs(voice_tempo - 1.0) > 0.001:
                    from moviepy.audio.fx.all import audio_speedx

                    narration = audio_speedx(narration, voice_tempo)
                    spoken_duration = float(duration)
                    narration = narration.set_duration(duration)
                    fast_visual_segments, scene_images, beat_indices = self._beat_aligned_visual_plan(
                        image_paths=image_paths,
                        beat_segments=beat_segments,
                        story_beats=story_beats,
                        duration=duration,
                    )
                    self._write_visual_timeline(
                        out_path,
                        fast_visual_segments,
                        scene_images,
                        beat_indices,
                    )
            self.subtitle_composer.write_srt(subtitles_path, subtitle_segments)

        # ── Step 4: visuals use FAST segments (more B-roll variety) ──
        clips = []
        for idx, segment in enumerate(fast_visual_segments):
            clip_duration = max(1.0, segment.end - segment.start)
            clip = self._make_scene_clip(
                scene_images[idx],
                clip_duration,
                is_hook=(idx == 0),
                allow_video=True,
            )
            clips.append(clip)

        if len(clips) > 1:
            timed_clips = []
            curr_start = 0.0
            for i, clip in enumerate(clips):
                if i > 0:
                    curr_start -= 0.15
                    clip = clip.crossfadein(0.15)
                clip = clip.set_start(curr_start)
                timed_clips.append(clip)
                curr_start += clip.duration
            video = CompositeVideoClip(timed_clips, size=self.target_size)
            visual_end = max(item.start + item.duration for item in timed_clips)
        else:
            video = clips[0]
            visual_end = clips[0].duration

        # If visuals end early, freeze the last frame instead of showing black.
        if visual_end < duration:
            last_clip = clips[-1]
            freeze_t = max(0.0, last_clip.duration - 0.05)
            tail_frame = last_clip.get_frame(freeze_t)
            tail = ImageClip(tail_frame).set_start(visual_end).set_duration(duration - visual_end)
            video = CompositeVideoClip([video, tail], size=self.target_size)

        video = video.set_duration(duration)

        overlay_clips = []
        composite_sources = [video]
        logo_overlay = self._logo_clip(logo_path, duration)
        if logo_overlay is not None:
            composite_sources.append(logo_overlay)
        presenter_overlay = self._presenter_clip(
            presenter_avatar_path,
            duration,
            content_kind,
            layout_mode=presenter_layout_mode,
        )
        if presenter_overlay is not None:
            composite_sources.append(presenter_overlay)
        if self.subtitle_enabled and subtitle_segments:
            for segment in subtitle_segments:
                if not segment.text or not segment.text.strip():
                    continue
                try:
                    txt_array = self._render_word_caption(
                        segment.text, highlight=True,
                        width=min(920, self.target_size[0] - 80)
                    )
                    overlay = ImageClip(
                        txt_array if isinstance(txt_array, np.ndarray) else np.array(txt_array),
                        transparent=True,
                    )
                    clip_dur = max(0.2, segment.end - segment.start)
                    caption_y = self.target_size[1] - overlay.h - (110 if content_kind == "video" else 360)
                    overlay = (
                        overlay
                        .set_start(segment.start)
                        .set_duration(clip_dur)
                        .set_position(('center', caption_y))
                    )
                    overlay_clips.append(overlay)
                    composite_sources.append(overlay)
                except Exception as e:
                    print(f"Caption failure: {e}")
                    continue

        claim_card = self._short_claim_card_text(title_text, channel_id)
        if content_kind == "short" and channel_id == "brain_lens" and not claim_card:
            raise ShortCaptionPreflightError(
                "Brain Lens Short requires a first-second claim card from the title"
            )
        if claim_card:
            try:
                claim_array = self._render_word_caption(
                    claim_card,
                    highlight=False,
                    width=min(980, self.target_size[0] - 60),
                )
                claim_overlay = ImageClip(
                    claim_array if isinstance(claim_array, np.ndarray) else np.array(claim_array),
                    transparent=True,
                )
                claim_overlay = (
                    claim_overlay
                    .set_start(0.0)
                    .set_duration(1.15)
                    .set_position(("center", max(48, int(self.target_size[1] * 0.06))))
                )
                composite_sources.append(claim_overlay)
                self.last_claim_card_text = claim_card
            except Exception as exc:
                print(f"Claim card overlay skipped: {exc}")
                if channel_id == "brain_lens" and content_kind == "short":
                    raise ShortCaptionPreflightError(
                        f"Brain Lens claim card render failed: {exc}"
                    ) from exc

        final_video = CompositeVideoClip(composite_sources, size=self.target_size) if len(composite_sources) > 1 else video
        final_video = final_video.set_duration(duration)

        music = None
        music_path = self._pick_music(music_dir)
        self.last_music_path = music_path
        if music_path:
            music = AudioFileClip(str(music_path))
            music = audio_loop(music, duration=duration)
            music = self._duck_music(music, narration, spoken_duration, duration)
            audio = CompositeAudioClip([music, narration.volumex(1.0)])
        else:
            audio = narration

        final = final_video.set_audio(audio)
        temp_audio = str(out_path.with_suffix(".temp_audio.m4a"))
        encode_profile = resolve_h264_encode_profile()
        self.last_video_encoder = encode_profile.name
        write_kwargs = {
            "fps": 30,
            "codec": moviepy_codec_name(encode_profile),
            "audio_codec": "aac",
            "threads": 2,
            "temp_audiofile": temp_audio,
            "remove_temp": False,
            "logger": None,
        }
        # MoviePy only documents libx264 preset/CRF style options.
        if encode_profile.name == "libx264":
            write_kwargs["preset"] = "veryfast"
        try:
            final.write_videofile(str(out_path), **write_kwargs)
        except Exception:
            # If a GPU codec fails mid-write, fall back to CPU once.
            if encode_profile.name != "libx264":
                self.last_video_encoder = "libx264"
                final.write_videofile(
                    str(out_path),
                    fps=30,
                    codec="libx264",
                    audio_codec="aac",
                    preset="veryfast",
                    threads=2,
                    temp_audiofile=temp_audio,
                    remove_temp=False,
                    logger=None,
                )
            else:
                raise

        final.close()
        try:
            import time
            if os.path.exists(temp_audio):
                for _ in range(5):
                    try:
                        os.remove(temp_audio)
                        break
                    except Exception:
                        time.sleep(1)
        except Exception:
            pass
        if final_video is not video:
            final_video.close()
        video.close()
        narration.close()
        raw_narration.close()
        if music is not None:
            music.close()
        for clip in clips:
            clip.close()
        for overlay in overlay_clips:
            overlay.close()
        if logo_overlay is not None:
            logo_overlay.close()
        if presenter_overlay is not None:
            presenter_overlay.close()
        self.validate_output(out_path, expected_duration=duration)
        return duration
