"""Hardware H.264 encode selection for FFmpeg renders.

On this project's typical laptop (Intel HD + old NVIDIA GT 720M):
- NVENC is unavailable / unsupported
- Intel Quick Sync (``h264_qsv``) works and offloads the heavy encode
- ``libx264`` remains the safe CPU fallback

Override with env ``YT_VIDEO_ENCODER=auto|qsv|nvenc|libx264``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class H264EncodeProfile:
    name: str
    codec: str

    def argv(self, *, preset: str = "veryfast", crf: str | int = 21, threads: str | int = 2) -> list[str]:
        """Return FFmpeg argv starting at ``-c:v`` for this profile."""
        crf_value = int(float(crf))
        if self.name == "qsv":
            # global_quality roughly tracks CRF; keep a usable YouTube quality.
            quality = str(max(15, min(28, crf_value)))
            return [
                "-c:v",
                "h264_qsv",
                "-global_quality",
                quality,
                "-look_ahead",
                "0",
            ]
        if self.name == "nvenc":
            return [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-rc",
                "vbr",
                "-cq",
                str(crf_value),
                "-b:v",
                "0",
            ]
        return [
            "-c:v",
            "libx264",
            "-preset",
            str(preset),
            "-crf",
            str(crf_value),
            "-threads",
            str(threads),
        ]


_LOCK = threading.Lock()
_CACHED: H264EncodeProfile | None = None
_PROBE_NOTE: str = ""


def selected_encoder_note() -> str:
    return _PROBE_NOTE


def _ffmpeg_exe(explicit: str | Path | None = None) -> str:
    if explicit:
        return str(explicit)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _probe_encoder(ffmpeg_path: str, codec: str, extra: list[str]) -> bool:
    with tempfile.TemporaryDirectory(prefix="yt_hw_encode_") as tmp:
        out_path = Path(tmp) / "probe.mp4"
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=30",
            "-t",
            "0.4",
            "-an",
            "-c:v",
            codec,
            *extra,
            str(out_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and out_path.exists() and out_path.stat().st_size > 500


def resolve_h264_encode_profile(
    ffmpeg_path: str | Path | None = None,
    *,
    force: str | None = None,
) -> H264EncodeProfile:
    """Pick the best working H.264 encoder once per process."""
    global _CACHED, _PROBE_NOTE
    forced = (force or os.getenv("YT_VIDEO_ENCODER", "auto") or "auto").strip().lower()
    with _LOCK:
        if _CACHED is not None and force is None and forced in {"", "auto"}:
            return _CACHED

        ffmpeg = _ffmpeg_exe(ffmpeg_path)
        order: list[tuple[str, str, list[str]]]
        one_shot_force = force is not None
        if forced in {"qsv", "nvenc", "libx264", "x264", "cpu"}:
            alias = {"x264": "libx264", "cpu": "libx264"}.get(forced, forced)
            if alias == "qsv":
                order = [("qsv", "h264_qsv", ["-global_quality", "23", "-look_ahead", "0"])]
            elif alias == "nvenc":
                order = [("nvenc", "h264_nvenc", ["-preset", "p4", "-cq", "23", "-rc", "vbr", "-b:v", "0"])]
            else:
                order = [("libx264", "libx264", ["-preset", "ultrafast", "-crf", "28", "-threads", "2"])]
        else:
            # Prefer Intel QSV on this hardware. Skip NVENC first only after QSV
            # fails — GT 720M class cards usually cannot run modern NVENC.
            order = [
                ("qsv", "h264_qsv", ["-global_quality", "23", "-look_ahead", "0"]),
                ("nvenc", "h264_nvenc", ["-preset", "p4", "-cq", "23", "-rc", "vbr", "-b:v", "0"]),
                ("libx264", "libx264", ["-preset", "ultrafast", "-crf", "28", "-threads", "2"]),
            ]

        for name, codec, extra in order:
            if name == "libx264" or _probe_encoder(ffmpeg, codec, extra):
                profile = H264EncodeProfile(name=name, codec=codec)
                if not one_shot_force:
                    _CACHED = profile
                    _PROBE_NOTE = f"video encoder: {profile.name} ({profile.codec})"
                return profile

        profile = H264EncodeProfile(name="libx264", codec="libx264")
        if not one_shot_force:
            _CACHED = profile
            _PROBE_NOTE = "video encoder: libx264 (fallback)"
        return profile


def reset_h264_encode_profile_cache() -> None:
    global _CACHED, _PROBE_NOTE
    with _LOCK:
        _CACHED = None
        _PROBE_NOTE = ""


def moviepy_codec_name(profile: H264EncodeProfile | None = None) -> str:
    chosen = profile or resolve_h264_encode_profile()
    # MoviePy passes this through to FFmpeg; QSV works when the binary supports it.
    return chosen.codec
