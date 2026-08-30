from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt_auto.subtitles import SubtitleComposer, SubtitleSegment
from yt_auto.video_builder import (
    LongCaptionPreflightError,
    NarrationBeatTiming,
    ShortCaptionPreflightError,
    ShortNarrationPreflightError,
    VideoBuilder,
)


class _FakeAudioClip:
    def __init__(self, duration: float) -> None:
        self.duration = duration

    def close(self) -> None:
        return None


class LongCaptionPreflightTests(unittest.TestCase):
    def test_short_caption_preflight_never_falls_through_to_slow_renderer(self) -> None:
        builder = VideoBuilder(min_duration=25, max_duration=40)
        builder._build_short_fast_ffmpeg = Mock(
            side_effect=ShortCaptionPreflightError(
                "short caption preflight failed before visual encoding: flash fragment"
            )
        )

        with patch.dict(os.environ, {"YT_ALLOW_SLOW_SHORT_RENDER": "1"}, clear=False):
            with patch("yt_auto.video_builder.AudioFileClip") as fallback_audio:
                with self.assertRaises(ShortCaptionPreflightError):
                    builder.build(
                        image_paths=[Path("unused.jpg")],
                        narration_path=Path("unused.wav"),
                        music_dir=Path("unused-music"),
                        out_path=Path("unused.mp4"),
                        narration_text="Broken captions",
                        subtitles_path=Path("unused.srt"),
                        narration_beats=["Broken captions"],
                        duration_bounds=(25, 40),
                        content_kind="short",
                    )

        fallback_audio.assert_not_called()

    def test_overlong_short_narration_never_falls_through_to_slow_renderer(self) -> None:
        builder = VideoBuilder(min_duration=25, max_duration=40)
        builder._build_short_fast_ffmpeg = Mock(
            side_effect=ShortNarrationPreflightError(
                "short narration is 43.200s but the configured maximum is 40.000s; "
                "refusing to trim spoken audio"
            )
        )

        with patch.dict(os.environ, {"YT_ALLOW_SLOW_SHORT_RENDER": "1"}, clear=False):
            with patch("yt_auto.video_builder.AudioFileClip") as fallback_audio:
                with self.assertRaisesRegex(
                    ShortNarrationPreflightError,
                    "refusing to trim spoken audio",
                ):
                    builder.build(
                        image_paths=[Path("unused.jpg")],
                        narration_path=Path("narration.wav"),
                        music_dir=Path("unused-music"),
                        out_path=Path("unused.mp4"),
                        duration_bounds=(25, 40),
                        content_kind="short",
                    )

        fallback_audio.assert_not_called()

    def test_fast_short_rejects_43_second_narration_before_visual_encoding(self) -> None:
        builder = VideoBuilder(min_duration=25, max_duration=40)
        with (
            patch("yt_auto.video_builder.AudioFileClip", return_value=_FakeAudioClip(43.2)),
            patch.object(builder, "_render_fast_segment") as render_segment,
        ):
            with self.assertRaisesRegex(
                ShortNarrationPreflightError,
                r"43\.200s.*40\.000s.*refusing to trim spoken audio",
            ):
                builder._build_short_fast_ffmpeg(
                    image_paths=[Path("unused.jpg")],
                    narration_path=Path("narration.wav"),
                    out_path=Path("unused.mp4"),
                    narration_text="Complete narration.",
                    subtitles_path=Path("unused.srt"),
                    title_text="Title",
                    visual_captions=None,
                    narration_beats=["Complete narration."],
                    scene_durations=[43.2],
                    duration_bounds=(25, 40),
                    logo_path=None,
                    music_dir=Path("unused-music"),
                )

        render_segment.assert_not_called()

    def test_disabled_fast_short_path_still_refuses_to_trim_narration(self) -> None:
        builder = VideoBuilder(min_duration=25, max_duration=40)
        fake_audio = _FakeAudioClip(43.2)
        with (
            patch.dict(os.environ, {"YT_DISABLE_FAST_SHORT_RENDER": "1"}, clear=False),
            patch("yt_auto.video_builder.AudioFileClip", return_value=fake_audio),
        ):
            with self.assertRaises(ShortNarrationPreflightError):
                builder.build(
                    image_paths=[Path("unused.jpg")],
                    narration_path=Path("narration.wav"),
                    music_dir=Path("unused-music"),
                    out_path=Path("unused.mp4"),
                    duration_bounds=(25, 40),
                    content_kind="short",
                )

    def test_dense_beat_is_stretched_without_slowing_sparse_beat(self) -> None:
        builder = VideoBuilder(min_duration=100, max_duration=210)
        composer = SubtitleComposer(max_words_per_caption=7)
        beats = [
            SubtitleSegment(start=0.0, end=100.0, text=" ".join(["xxxxxx"] * 266)),
            SubtitleSegment(start=100.0, end=200.0, text=" ".join(["quiet"] * 14)),
        ]

        duration, scaled_beats, captions, voice_tempo, timing = (
            builder._plan_long_caption_timeline(
                beat_segments=beats,
                raw_duration=200.0,
                duration_bounds=(100, 210),
                composer=composer,
                include_beat_timing=True,
            )
        )

        # A global correction would take 214.37 seconds and fail this profile.
        self.assertGreater(duration, 206.0)
        self.assertLess(duration, 207.2)
        self.assertAlmostEqual(scaled_beats[0].end, timing[0].output_end, places=8)
        self.assertAlmostEqual(scaled_beats[1].start, timing[1].output_start, places=8)
        self.assertGreater(timing[0].stretch, 1.06)
        self.assertAlmostEqual(timing[1].stretch, 1.0, places=8)
        self.assertAlmostEqual(voice_tempo, 200.0 / duration, places=8)
        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))

    def test_single_beat_stretch_above_safe_limit_fails_closed(self) -> None:
        builder = VideoBuilder(min_duration=100, max_duration=240)
        composer = SubtitleComposer(max_words_per_caption=7)
        beats = [
            SubtitleSegment(start=0.0, end=100.0, text=" ".join(["xxxxxx"] * 280)),
            SubtitleSegment(start=100.0, end=200.0, text=" ".join(["quiet"] * 14)),
        ]

        with self.assertRaisesRegex(LongCaptionPreflightError, r"beat 1 requires .*per-beat limit"):
            builder._plan_long_caption_timeline(
                beat_segments=beats,
                raw_duration=200.0,
                duration_bounds=(100, 240),
                composer=composer,
                include_beat_timing=True,
            )

    def test_borderline_dense_beat_uses_safe_half_percent_headroom(self) -> None:
        builder = VideoBuilder(min_duration=200, max_duration=220)
        composer = SubtitleComposer(max_words_per_caption=7)
        raw_duration = 200.0
        beats = [
            SubtitleSegment(start=0.0, end=100.0, text=" ".join(["xxxxxx"] * 269)),
            SubtitleSegment(start=100.0, end=raw_duration, text="A sparse closing beat."),
        ]

        duration, _, captions, _, timing = builder._plan_long_caption_timeline(
            beat_segments=beats,
            raw_duration=raw_duration,
            duration_bounds=(200, 220),
            composer=composer,
            include_beat_timing=True,
        )

        self.assertGreater(timing[0].stretch, 1.08)
        self.assertLessEqual(timing[0].stretch, builder.MAX_LONG_BEAT_STRETCH)
        self.assertLessEqual(duration, 220)
        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))

    def test_profile_minimum_above_total_stretch_limit_fails_closed(self) -> None:
        builder = VideoBuilder(min_duration=217, max_duration=230)
        composer = SubtitleComposer(max_words_per_caption=7)
        beats = [
            SubtitleSegment(start=0.0, end=100.0, text="Sparse first beat."),
            SubtitleSegment(start=100.0, end=200.0, text="Sparse second beat."),
        ]

        with self.assertRaisesRegex(LongCaptionPreflightError, r"total narration stretch.*safe 8.0% limit"):
            builder._plan_long_caption_timeline(
                beat_segments=beats,
                raw_duration=200.0,
                duration_bounds=(217, 230),
                composer=composer,
                include_beat_timing=True,
            )

    def test_reported_caption_density_is_safely_stretched_below_600_seconds(self) -> None:
        builder = VideoBuilder(min_duration=480, max_duration=600)
        composer = SubtitleComposer(max_words_per_caption=7)
        raw_duration = 556.77
        # Model the reported 18.19 CPS Ancient narration density.  A single
        # unbroken token keeps the fixture deterministic while exercising the
        # same visible-character/duration calculation as production captions.
        narration = "x" * round(raw_duration * 18.19)
        beats = [SubtitleSegment(start=0.0, end=raw_duration, text=narration)]

        duration, scaled_beats, captions, voice_tempo = builder._plan_long_caption_timeline(
            beat_segments=beats,
            raw_duration=raw_duration,
            duration_bounds=(480, 600),
            composer=composer,
        )

        self.assertGreater(duration, 590.0)
        self.assertLessEqual(duration, 600.0)
        self.assertLess(duration / raw_duration, builder.MAX_LONG_NARRATION_STRETCH)
        self.assertAlmostEqual(scaled_beats[-1].end, duration, places=6)
        self.assertAlmostEqual(voice_tempo, raw_duration / duration, places=8)
        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))

    def test_sentence_safe_slow_narration_is_accelerated_into_wpm_band(self) -> None:
        builder = VideoBuilder(min_duration=480, max_duration=600)
        composer = SubtitleComposer(max_words_per_caption=8)
        raw_duration = 540.0
        sentence = "We keep consent clear today."
        beat_texts = [" ".join([sentence] * count) for count in ([12] * 19 + [2])]
        # 230 five-word sentences = 1,150 words.
        beats = []
        cursor = 0.0
        for text in beat_texts:
            end = cursor + (raw_duration / len(beat_texts))
            beats.append(SubtitleSegment(start=cursor, end=end, text=text))
            cursor = end
        beats[-1] = SubtitleSegment(start=beats[-1].start, end=raw_duration, text=beats[-1].text)

        duration, _, captions, voice_tempo, timing = builder._plan_long_caption_timeline(
            beat_segments=beats,
            raw_duration=raw_duration,
            duration_bounds=(480, 600),
            composer=composer,
            include_beat_timing=True,
            narration_word_count=1150,
        )

        self.assertAlmostEqual(duration, 500.0, delta=0.05)
        self.assertAlmostEqual(voice_tempo, 1.08, delta=0.001)
        self.assertTrue(all(1.0 <= item.tempo <= 1.10 for item in timing))
        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))

    def test_long_caption_plan_allows_readable_clause_continuations(self) -> None:
        builder = VideoBuilder(min_duration=90, max_duration=120)
        composer = SubtitleComposer(max_words_per_caption=8)
        text = (
            "Those disagreements are useful because they reveal which parts are secure, "
            "which depend on later memory, and where the strongest explanation survives."
        )
        beats = [
            SubtitleSegment(0.0, 50.0, text),
            SubtitleSegment(
                50.0,
                100.0,
                "The evidence remains specific, sourced, and understandable to the viewer.",
            ),
        ]

        duration, _, captions, _ = builder._plan_long_caption_timeline(
            beat_segments=beats,
            raw_duration=100.0,
            duration_bounds=(90, 120),
            composer=composer,
        )

        self.assertAlmostEqual(duration, 100.0, places=6)
        self.assertTrue(
            any(
                "begins in the middle of a phrase" in issue
                for issue in composer.quality_issues(captions, cps_tolerance=0.0)
            )
        )
        self.assertEqual(
            [],
            composer.quality_issues(
                captions,
                cps_tolerance=0.0,
                allow_clause_continuations=True,
            ),
        )

    def test_excessive_long_narration_acceleration_fails_closed(self) -> None:
        builder = VideoBuilder(min_duration=400, max_duration=600)
        composer = SubtitleComposer(max_words_per_caption=8)
        beats = [
            SubtitleSegment(0.0, 300.0, "Clear evidence supports careful choices."),
            SubtitleSegment(300.0, 600.0, "Consent remains explicit and revisable."),
        ]

        with self.assertRaisesRegex(LongCaptionPreflightError, "acceleration.*safe 10.0% limit"):
            builder._plan_long_caption_timeline(
                beat_segments=beats,
                raw_duration=600.0,
                duration_bounds=(400, 600),
                composer=composer,
                narration_word_count=1100,
            )

    def test_impossible_caption_track_fails_before_visual_planning_or_encoding(self) -> None:
        builder = VideoBuilder(min_duration=90, max_duration=108)
        narration = "x" * 2000
        visual_plan = Mock(side_effect=AssertionError("visual planning must not run"))
        visual_encode = Mock(side_effect=AssertionError("visual encoding must not run"))
        builder._beat_aligned_visual_plan = visual_plan
        builder._render_fast_segment = visual_encode

        with patch("yt_auto.video_builder.AudioFileClip", return_value=_FakeAudioClip(100.0)):
            with self.assertRaisesRegex(
                RuntimeError,
                r"caption preflight failed before visual encoding.*configured maximum",
            ):
                builder._build_video_fast_ffmpeg(
                    image_paths=[Path("unused.jpg")],
                    narration_path=Path("unused.wav"),
                    out_path=Path("unused.mp4"),
                    narration_text=narration,
                    subtitles_path=Path("unused.srt"),
                    title_text="Test",
                    visual_captions=[],
                    narration_beats=[narration],
                    scene_durations=[100.0],
                    duration_bounds=(90, 108),
                    logo_path=None,
                    music_dir=Path("unused-music"),
                )

        visual_plan.assert_not_called()
        visual_encode.assert_not_called()

    def test_fast_audio_filter_applies_planned_narration_tempo(self) -> None:
        builder = VideoBuilder(min_duration=480, max_duration=600)
        builder._run_ffmpeg_logged = Mock()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder._render_fast_audio_track(
                ffmpeg_path="ffmpeg",
                narration_path=root / "narration.wav",
                music_path=None,
                out_path=root / "audio.m4a",
                duration=596.0,
                music_volume=0.075,
                diagnostic_path=root / "render.log",
                timeout=30,
                voice_tempo=0.93418,
            )

        command = builder._run_ffmpeg_logged.call_args.args[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("atempo=0.93418000", filter_graph)
        self.assertIn("atrim=0:596.000", filter_graph)

    def test_fast_audio_filter_splits_and_rejoins_variable_tempo_beats(self) -> None:
        builder = VideoBuilder(min_duration=100, max_duration=240)
        builder._run_ffmpeg_logged = Mock()
        timing = [
            NarrationBeatTiming(0.0, 100.0, 0.0, 107.5, 100.0 / 107.5),
            NarrationBeatTiming(100.0, 200.0, 107.5, 207.5, 1.0),
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder._render_fast_audio_track(
                ffmpeg_path="ffmpeg",
                narration_path=root / "narration.wav",
                music_path=None,
                out_path=root / "audio.m4a",
                duration=207.5,
                music_volume=0.075,
                diagnostic_path=root / "render.log",
                timeout=30,
                beat_timing=timing,
            )

        command = builder._run_ffmpeg_logged.call_args.args[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("asplit=2[voice_src_0][voice_src_1]", filter_graph)
        self.assertIn("atrim=start=0.000000:end=100.000000", filter_graph)
        self.assertIn("atempo=0.93023256[voice_part_0]", filter_graph)
        self.assertIn("atrim=start=100.000000:end=200.000000", filter_graph)
        self.assertIn("atempo=1.00000000[voice_part_1]", filter_graph)
        self.assertIn("[voice_part_0][voice_part_1]concat=n=2:v=0:a=1", filter_graph)
        self.assertIn("atrim=0:207.500", filter_graph)

    def test_caption_preflight_failure_never_falls_through_to_slow_renderer(self) -> None:
        builder = VideoBuilder(min_duration=480, max_duration=600)
        builder._build_video_fast_ffmpeg = Mock(
            side_effect=LongCaptionPreflightError(
                "long caption preflight failed before visual encoding: impossible"
            )
        )

        with patch.dict(os.environ, {"YT_ALLOW_SLOW_LONG_RENDER": "1"}, clear=False):
            with patch("yt_auto.video_builder.AudioFileClip") as fallback_audio:
                with self.assertRaises(LongCaptionPreflightError):
                    builder.build(
                        image_paths=[Path("unused.jpg")],
                        narration_path=Path("unused.wav"),
                        music_dir=Path("unused-music"),
                        out_path=Path("unused.mp4"),
                        narration_text="Unreadable captions",
                        subtitles_path=Path("unused.srt"),
                        narration_beats=["Unreadable captions"],
                        duration_bounds=(480, 600),
                        content_kind="video",
                    )

        fallback_audio.assert_not_called()

    def test_disabling_fast_long_render_refuses_unsafe_slow_path(self) -> None:
        builder = VideoBuilder(min_duration=480, max_duration=600)

        with patch.dict(
            os.environ,
            {
                "YT_DISABLE_FAST_LONG_RENDER": "1",
                "YT_ALLOW_SLOW_LONG_RENDER": "1",
            },
            clear=False,
        ):
            with patch("yt_auto.video_builder.AudioFileClip") as fallback_audio:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"Slow long rendering is disabled.*adaptive per-beat",
                ):
                    builder.build(
                        image_paths=[Path("unused.jpg")],
                        narration_path=Path("unused.wav"),
                        music_dir=Path("unused-music"),
                        out_path=Path("unused.mp4"),
                        narration_text="A readable long narration.",
                        subtitles_path=Path("unused.srt"),
                        narration_beats=["A readable long narration."],
                        duration_bounds=(480, 600),
                        content_kind="video",
                    )

        fallback_audio.assert_not_called()

    def test_technical_fast_long_failure_does_not_use_unsafe_slow_fallback(self) -> None:
        builder = VideoBuilder(min_duration=480, max_duration=600)
        builder._build_video_fast_ffmpeg = Mock(side_effect=RuntimeError("encoder failed"))

        with patch.dict(
            os.environ,
            {
                "YT_DISABLE_FAST_LONG_RENDER": "0",
                "YT_ALLOW_SLOW_LONG_RENDER": "1",
            },
            clear=False,
        ):
            with patch("yt_auto.video_builder.AudioFileClip") as fallback_audio:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"slow long fallback was refused.*adaptive per-beat",
                ):
                    builder.build(
                        image_paths=[Path("unused.jpg")],
                        narration_path=Path("unused.wav"),
                        music_dir=Path("unused-music"),
                        out_path=Path("unused.mp4"),
                        narration_text="A readable long narration.",
                        subtitles_path=Path("unused.srt"),
                        narration_beats=["A readable long narration."],
                        duration_bounds=(480, 600),
                        content_kind="video",
                    )

        fallback_audio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
