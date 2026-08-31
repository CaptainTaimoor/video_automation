from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from yt_auto.subtitles import SubtitleSegment  # noqa: E402
from yt_auto.video_builder import VideoBuilder  # noqa: E402


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _srt_seconds(values: tuple[str, ...]) -> tuple[float, float]:
    numbers = [int(value) for value in values]
    start = numbers[0] * 3600 + numbers[1] * 60 + numbers[2] + numbers[3] / 1000.0
    end = numbers[4] * 3600 + numbers[5] * 60 + numbers[6] + numbers[7] / 1000.0
    return start, end


def _read_srt(path: Path) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
    timing_pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})"
    )
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="ignore").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = timing_pattern.match(lines[timing_index])
        if not match:
            continue
        start, end = _srt_seconds(match.groups())
        text = " ".join(lines[timing_index + 1 :]).strip()
        if text and end > start:
            segments.append(SubtitleSegment(start=start, end=end, text=text))
    return segments


def _target_size(run_dir: Path) -> tuple[int, int]:
    qa_path = run_dir / "frame_check" / "final_visual_qa.json"
    if qa_path.exists():
        report = _read_json(qa_path)
        if isinstance(report, dict):
            width = int(report.get("width") or 0)
            height = int(report.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
    return 1920, 1080


def _logo_path(metadata: dict[str, object]) -> Path | None:
    raw_path = str(metadata.get("logo_file") or "").strip()
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate if candidate.is_file() else None


def rerender(run_dir: Path, output_name: str) -> Path:
    run_dir = run_dir.resolve()
    original_video = run_dir / "video.mp4"
    timeline_path = run_dir / "visual_timeline.json"
    subtitles_path = run_dir / "subtitles.srt"
    metadata_path = run_dir / "metadata.json"
    required = (original_video, timeline_path, subtitles_path, metadata_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Existing run is incomplete: {', '.join(missing)}")

    raw_timeline = _read_json(timeline_path)
    if not isinstance(raw_timeline, list) or not raw_timeline:
        raise RuntimeError("Existing visual timeline is empty")
    timeline = [entry for entry in raw_timeline if isinstance(entry, dict)]
    duration = max(float(entry.get("end") or 0.0) for entry in timeline)
    if duration <= 0.0:
        raise RuntimeError("Existing visual timeline has no usable duration")

    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict):
        raise RuntimeError("Existing metadata is invalid")
    captions = _read_srt(subtitles_path)
    if not captions:
        raise RuntimeError("Existing subtitle track is empty")

    target_size = _target_size(run_dir)
    builder = VideoBuilder(1, int(duration + 30), target_size=target_size)
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    out_path = run_dir / output_name
    diagnostic_path = out_path.with_suffix(".ffmpeg.log")
    diagnostic_path.write_text("Artifact-only long rerender diagnostics\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="rerender_long_", dir=str(run_dir)) as directory:
        temp_dir = Path(directory)
        segment_paths: list[Path] = []
        for index, entry in enumerate(timeline):
            source_name = Path(str(entry.get("source_file") or "")).name
            source_path = run_dir / "images" / source_name
            if not source_path.is_file():
                raise RuntimeError(f"Timeline source is missing: {source_path}")
            start = float(entry.get("start") or 0.0)
            end = float(entry.get("end") or 0.0)
            segment_path = temp_dir / f"segment_{index:03d}.mp4"
            if index == 0 or (index + 1) % 10 == 0 or index + 1 == len(timeline):
                print(f"Rendering preserved visual segment {index + 1}/{len(timeline)}", flush=True)
            builder._render_fast_segment(
                ffmpeg_path=ffmpeg_path,
                source_path=source_path,
                out_path=segment_path,
                duration=max(0.8, end - start),
                is_hook=index == 0,
                source_offset=builder._source_motion_offset(
                    source_path,
                    index,
                    base=0.25,
                    step=0.55,
                    modulo=6,
                ),
            )
            segment_paths.append(segment_path)

        concat_path = temp_dir / "segments.txt"
        with concat_path.open("w", encoding="utf-8") as handle:
            for segment_path in segment_paths:
                escaped_path = segment_path.as_posix().replace("'", "'\\''")
                handle.write(f"file '{escaped_path}'\n")

        ass_path = temp_dir / "captions.ass"
        builder._write_ass_captions(captions, ass_path)
        video_track_path = temp_dir / "video_track.mp4"
        audio_track_path = temp_dir / "audio_track.m4a"
        candidate_path = temp_dir / "validated_candidate.mp4"
        logo_width = max(92, int(min(target_size) * 0.10))
        logo_margin = max(28, int(min(target_size) * 0.035))
        builder._render_fast_video_track(
            ffmpeg_path=ffmpeg_path,
            concat_path=concat_path,
            out_path=video_track_path,
            duration=duration,
            ass_path=ass_path,
            logo_path=_logo_path(metadata),
            logo_width=logo_width,
            logo_margin=logo_margin,
            diagnostic_path=diagnostic_path,
            timeout=2400,
        )
        builder._run_ffmpeg_logged(
            [
                ffmpeg_path,
                "-y",
                "-i",
                str(original_video),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "copy",
                str(audio_track_path),
            ],
            label="preserved final-audio extraction",
            diagnostic_path=diagnostic_path,
            timeout=300,
        )
        builder._mux_and_validate_fast_render(
            ffmpeg_path=ffmpeg_path,
            video_track_path=video_track_path,
            audio_track_path=audio_track_path,
            candidate_path=candidate_path,
            out_path=out_path,
            duration=duration,
            diagnostic_path=diagnostic_path,
            timeout=2400,
        )
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-render a completed long video from its saved visuals, captions, and mixed audio."
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-name", default="video_repaired.mp4")
    args = parser.parse_args()
    output = rerender(args.run_dir, args.output_name)
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
