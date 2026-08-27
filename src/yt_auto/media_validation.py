from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Sequence


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_FRAME_RE = re.compile(
    r"\bn:\s*(\d+).*?\bpts_time:\s*(-?\d+(?:\.\d+)?)"
    r".*?\brate:\s*(\d+)\s+nb_samples:\s*(\d+)"
)
_VIDEO_FRAME_RE = re.compile(
    r"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:\s*(-?\d+(?:\.\d+)?)"
    r"\s+duration:\s*(?:-?\d+|N/A)\s+duration_time:\s*(N/A|-?\d+(?:\.\d+)?)"
)
_AUDIO_PACKET_RE = re.compile(
    r"type:audio.*?\bpkt_pts_time:\s*(-?\d+(?:\.\d+)?)"
    r".*?\bduration_time:\s*(-?\d+(?:\.\d+)?)"
)


class MediaValidationError(RuntimeError):
    """Raised when a rendered media file fails deterministic stream validation."""


@dataclass(frozen=True)
class MediaValidationReport:
    path: str
    expected_duration: float | None
    container_duration: float | None
    video_frame_count: int
    video_start: float | None
    video_end: float | None
    video_decoded_duration: float
    video_nominal_frame_duration: float
    video_max_pts_gap: float
    video_pts_gap_count: int
    video_pts_regression_count: int
    audio_frame_count: int
    audio_sample_rate: int | None
    audio_sample_count: int
    audio_start: float | None
    audio_end: float | None
    audio_decoded_sample_duration: float
    audio_pts_span: float
    audio_coverage_ratio: float
    audio_max_pts_gap: float
    audio_pts_gap_count: int
    audio_packet_count: int
    audio_max_packet_pts_gap: float
    audio_packet_pts_gap_count: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    diagnostic_tail: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload

    def write_json(self, path: Path | None = None) -> Path:
        output_path = path or Path(self.path).with_suffix(".media_validation.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return output_path

    def raise_for_failure(self) -> None:
        if self.ok:
            return
        joined = "; ".join(self.errors)
        raise MediaValidationError(f"Final MP4 validation failed for {self.path}: {joined}")


def _ffmpeg_executable(explicit_path: str | Path | None) -> str:
    if explicit_path:
        return str(explicit_path)
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_decode_probe(
    ffmpeg_path: str,
    media_path: Path,
    stream_kind: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    if stream_kind == "audio":
        stream_args = ["-map", "0:a:0", "-af", "ashowinfo", "-vn"]
        debug_args = ["-debug_ts"]
    elif stream_kind == "video":
        stream_args = ["-map", "0:v:0", "-vf", "showinfo", "-an"]
        debug_args = []
    else:
        raise ValueError(f"Unsupported stream kind: {stream_kind}")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-xerror",
        *debug_args,
        "-i",
        str(media_path),
        *stream_args,
        "-f",
        "null",
        "-",
    ]
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _duration_from_log(log_text: str) -> float | None:
    match = _DURATION_RE.search(log_text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return (int(hours) * 3600.0) + (int(minutes) * 60.0) + float(seconds)


def _diagnostic_tail(logs: Sequence[str], limit: int = 40) -> tuple[str, ...]:
    useful: list[str] = []
    for text in logs:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if "Parsed_ashowinfo" in stripped or "Parsed_showinfo" in stripped:
                continue
            if "pkt_pts" in stripped or "frame_pts" in stripped:
                continue
            if stripped.startswith("frame="):
                continue
            useful.append(stripped)
    return tuple(useful[-limit:])


def _parse_audio_frames(log_text: str) -> list[tuple[float, int, int]]:
    frames: list[tuple[float, int, int]] = []
    for line in log_text.splitlines():
        if "ashowinfo" not in line:
            continue
        match = _AUDIO_FRAME_RE.search(line)
        if match:
            _, pts_time, sample_rate, sample_count = match.groups()
            frames.append((float(pts_time), int(sample_rate), int(sample_count)))
    return frames


def _parse_video_frames(log_text: str) -> list[tuple[float, float | None]]:
    frames: list[tuple[float, float | None]] = []
    for line in log_text.splitlines():
        if "Parsed_showinfo" not in line:
            continue
        match = _VIDEO_FRAME_RE.search(line)
        if match:
            _, pts_time, duration_time = match.groups()
            parsed_duration = None if duration_time == "N/A" else float(duration_time)
            frames.append((float(pts_time), parsed_duration))
    return frames


def _parse_audio_packets(log_text: str) -> list[tuple[float, float]]:
    packets: list[tuple[float, float]] = []
    for line in log_text.splitlines():
        if "demuxer ->" not in line or "type:audio" not in line:
            continue
        match = _AUDIO_PACKET_RE.search(line)
        if match:
            pts_time, duration_time = match.groups()
            packets.append((float(pts_time), max(0.0, float(duration_time))))
    return packets


def _video_extent(frames: Sequence[tuple[float, float | None]]) -> tuple[float | None, float | None, float]:
    if not frames:
        return None, None, 0.0
    starts = [frame[0] for frame in frames]
    positive_deltas = [
        current - previous
        for previous, current in zip(starts, starts[1:])
        if current > previous
    ]
    fallback_duration = median(positive_deltas) if positive_deltas else (1.0 / 30.0)
    ends = [
        pts + (frame_duration if frame_duration is not None and frame_duration > 0 else fallback_duration)
        for pts, frame_duration in frames
    ]
    start = min(starts)
    end = max(ends)
    return start, end, max(0.0, end - start)


def validate_final_mp4(
    media_path: Path | str,
    *,
    expected_duration: float | None = None,
    ffmpeg_path: str | Path | None = None,
    min_audio_coverage: float = 0.985,
    duration_tolerance: float = 0.35,
    max_audio_pts_gap: float = 0.10,
    max_video_pts_gap_multiplier: float = 1.5,
    timeout: int = 900,
) -> MediaValidationReport:
    """Decode and validate final MP4 streams rather than trusting container metadata.

    Audio coverage is the sum of decoded samples divided by the expected video
    duration. This deliberately catches MP4s whose audio stream advertises a long
    timeline while containing only a few islands of AAC packets.
    """

    path = Path(media_path).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    empty_report = {
        "path": str(path),
        "expected_duration": expected_duration,
        "container_duration": None,
        "video_frame_count": 0,
        "video_start": None,
        "video_end": None,
        "video_decoded_duration": 0.0,
        "video_nominal_frame_duration": 0.0,
        "video_max_pts_gap": 0.0,
        "video_pts_gap_count": 0,
        "video_pts_regression_count": 0,
        "audio_frame_count": 0,
        "audio_sample_rate": None,
        "audio_sample_count": 0,
        "audio_start": None,
        "audio_end": None,
        "audio_decoded_sample_duration": 0.0,
        "audio_pts_span": 0.0,
        "audio_coverage_ratio": 0.0,
        "audio_max_pts_gap": 0.0,
        "audio_pts_gap_count": 0,
        "audio_packet_count": 0,
        "audio_max_packet_pts_gap": 0.0,
        "audio_packet_pts_gap_count": 0,
        "warnings": (),
        "diagnostic_tail": (),
    }
    if not path.exists() or not path.is_file():
        return MediaValidationReport(errors=(f"media file does not exist: {path}",), **empty_report)
    if path.stat().st_size <= 0:
        return MediaValidationReport(errors=(f"media file is empty: {path}",), **empty_report)

    executable = _ffmpeg_executable(ffmpeg_path)
    audio_result: subprocess.CompletedProcess[str] | None = None
    video_result: subprocess.CompletedProcess[str] | None = None
    audio_log = ""
    video_log = ""
    try:
        audio_result = _run_decode_probe(executable, path, "audio", timeout)
        audio_log = audio_result.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"audio decode probe could not complete: {exc}")
    try:
        video_result = _run_decode_probe(executable, path, "video", timeout)
        video_log = video_result.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"video decode probe could not complete: {exc}")

    if audio_result is not None and audio_result.returncode != 0:
        errors.append(f"audio stream failed full decode (FFmpeg exit {audio_result.returncode})")
    if video_result is not None and video_result.returncode != 0:
        errors.append(f"video stream failed full decode (FFmpeg exit {video_result.returncode})")

    container_duration = _duration_from_log(video_log) or _duration_from_log(audio_log)
    audio_frames = _parse_audio_frames(audio_log)
    audio_packets = _parse_audio_packets(audio_log)
    video_frames = _parse_video_frames(video_log)
    video_start, video_end, video_duration = _video_extent(video_frames)

    video_starts = [frame[0] for frame in video_frames]
    video_positive_deltas = [
        current - previous
        for previous, current in zip(video_starts, video_starts[1:])
        if current > previous
    ]
    video_declared_durations = [
        float(frame_duration)
        for _, frame_duration in video_frames
        if frame_duration is not None and frame_duration > 0
    ]
    video_nominal_frame_duration = median(
        video_declared_durations or video_positive_deltas
    ) if (video_declared_durations or video_positive_deltas) else 0.0
    # A continuous CFR stream advances by one nominal frame duration.  Allow
    # normal timestamp rounding, but fail on even one missing-frame interval.
    video_gap_threshold = (
        video_nominal_frame_duration * max_video_pts_gap_multiplier
        if video_nominal_frame_duration > 0
        else 0.0
    )
    video_pts_gaps: list[float] = []
    video_pts_regressions = 0
    for previous, current in zip(video_starts, video_starts[1:]):
        delta = current - previous
        if delta < -0.0005:
            video_pts_regressions += 1
        elif video_gap_threshold > 0 and delta > video_gap_threshold + 0.0001:
            video_pts_gaps.append(delta)

    reference_duration = float(expected_duration) if expected_duration is not None else 0.0
    if reference_duration <= 0:
        reference_duration = video_duration or float(container_duration or 0.0)
    if reference_duration <= 0:
        errors.append("could not determine a positive expected/video duration")

    if not video_frames:
        errors.append("no video frames were decoded")
    else:
        if expected_duration is not None and abs(video_duration - reference_duration) > duration_tolerance:
            errors.append(
                f"decoded video duration is {video_duration:.3f}s; expected {reference_duration:.3f}s "
                f"(tolerance {duration_tolerance:.3f}s)"
            )
        if video_pts_gaps:
            errors.append(
                f"video contains {len(video_pts_gaps)} frame PTS gap(s) over "
                f"{video_gap_threshold:.4f}s; largest frame interval is "
                f"{max(video_pts_gaps):.4f}s"
            )
        if video_pts_regressions:
            errors.append(
                f"video contains {video_pts_regressions} non-monotonic frame PTS transition(s)"
            )

    audio_sample_count = sum(frame[2] for frame in audio_frames)
    audio_sample_rate = audio_frames[0][1] if audio_frames else None
    inconsistent_rates = {frame[1] for frame in audio_frames}
    if len(inconsistent_rates) > 1:
        errors.append(f"decoded audio changes sample rate: {sorted(inconsistent_rates)}")
    audio_sample_duration = sum(samples / sample_rate for _, sample_rate, samples in audio_frames)
    audio_start = min((frame[0] for frame in audio_frames), default=None)
    audio_end = max(
        (pts + (samples / sample_rate) for pts, sample_rate, samples in audio_frames),
        default=None,
    )
    audio_span = (
        max(0.0, float(audio_end) - float(audio_start))
        if audio_start is not None and audio_end is not None
        else 0.0
    )
    audio_coverage = audio_sample_duration / reference_duration if reference_duration > 0 else 0.0

    pts_gaps: list[float] = []
    pts_regressions = 0
    previous_end: float | None = None
    for pts, sample_rate, samples in audio_frames:
        if previous_end is not None:
            delta = pts - previous_end
            if delta < -0.002:
                pts_regressions += 1
            elif delta > max_audio_pts_gap:
                pts_gaps.append(delta)
        previous_end = pts + (samples / sample_rate)

    packet_pts_gaps: list[float] = []
    packet_pts_regressions = 0
    previous_packet_end: float | None = None
    for pts, packet_duration in audio_packets:
        if previous_packet_end is not None:
            delta = pts - previous_packet_end
            if delta < -0.002:
                packet_pts_regressions += 1
            elif delta > max_audio_pts_gap:
                packet_pts_gaps.append(delta)
        previous_packet_end = pts + packet_duration
    positive_packet_durations = [duration for _, duration in audio_packets if duration > 0]
    nominal_packet_duration = median(positive_packet_durations) if positive_packet_durations else 0.0
    # MP4 can represent a hole by assigning the entire missing interval to the
    # preceding AAC packet rather than exposing a literal PTS jump. That packet
    # still decodes to one normal AAC frame, so treat the excess declared packet
    # duration as a packet-timeline gap.
    packet_duration_holes = [
        packet_duration - nominal_packet_duration
        for _, packet_duration in audio_packets
        if packet_duration - nominal_packet_duration > max_audio_pts_gap
    ]
    packet_timeline_gaps = [*packet_pts_gaps, *packet_duration_holes]

    if not audio_frames:
        errors.append("no audio samples were decoded")
    else:
        if audio_coverage < min_audio_coverage:
            errors.append(
                f"audio decoded sample coverage is {audio_coverage:.2%} "
                f"({audio_sample_duration:.3f}s of {reference_duration:.3f}s); "
                f"minimum is {min_audio_coverage:.2%}"
            )
        if audio_start is not None and audio_start > max_audio_pts_gap:
            errors.append(f"audio starts {audio_start:.3f}s after the video timeline")
        tail_gap = reference_duration - float(audio_end or 0.0)
        if tail_gap > max_audio_pts_gap:
            errors.append(
                f"audio ends at {float(audio_end or 0.0):.3f}s, leaving a {tail_gap:.3f}s tail gap"
            )
        if pts_gaps:
            errors.append(
                f"audio contains {len(pts_gaps)} decoded sample PTS gap(s) over {max_audio_pts_gap:.3f}s; "
                f"largest is {max(pts_gaps):.3f}s"
            )
        if pts_regressions:
            errors.append(f"audio contains {pts_regressions} non-monotonic decoded PTS transition(s)")
        if packet_timeline_gaps:
            errors.append(
                f"audio contains {len(packet_timeline_gaps)} packet PTS/duration gap(s) "
                f"over {max_audio_pts_gap:.3f}s; largest is {max(packet_timeline_gaps):.3f}s"
            )
        if packet_pts_regressions:
            errors.append(
                f"audio contains {packet_pts_regressions} non-monotonic packet PTS transition(s)"
            )
        if not audio_packets:
            warnings.append("audio packet timestamps were unavailable; decoded sample validation still ran")
        if audio_coverage > 1.02:
            warnings.append(
                f"audio decoded sample coverage is unexpectedly high ({audio_coverage:.2%})"
            )

    diagnostic_tail = _diagnostic_tail((audio_log, video_log))
    return MediaValidationReport(
        path=str(path),
        expected_duration=expected_duration,
        container_duration=container_duration,
        video_frame_count=len(video_frames),
        video_start=video_start,
        video_end=video_end,
        video_decoded_duration=video_duration,
        video_nominal_frame_duration=video_nominal_frame_duration,
        video_max_pts_gap=max(video_pts_gaps, default=0.0),
        video_pts_gap_count=len(video_pts_gaps),
        video_pts_regression_count=video_pts_regressions,
        audio_frame_count=len(audio_frames),
        audio_sample_rate=audio_sample_rate,
        audio_sample_count=audio_sample_count,
        audio_start=audio_start,
        audio_end=audio_end,
        audio_decoded_sample_duration=audio_sample_duration,
        audio_pts_span=audio_span,
        audio_coverage_ratio=audio_coverage,
        audio_max_pts_gap=max(pts_gaps, default=0.0),
        audio_pts_gap_count=len(pts_gaps),
        audio_packet_count=len(audio_packets),
        audio_max_packet_pts_gap=max(packet_timeline_gaps, default=0.0),
        audio_packet_pts_gap_count=len(packet_timeline_gaps),
        errors=tuple(errors),
        warnings=tuple(warnings),
        diagnostic_tail=diagnostic_tail,
    )
