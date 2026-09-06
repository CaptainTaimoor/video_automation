from __future__ import annotations

import os
import subprocess
from pathlib import Path

from yt_auto.models import PresenterConfig


class LocalPresenterService:
    def __init__(self, config: PresenterConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.project_root = Path(__file__).resolve().parents[2]

    def _resolve(self, value: str) -> Path:
        path = Path(value or "")
        return path if path.is_absolute() else self.project_root / path

    def _ready(self) -> tuple[Path, Path, Path] | None:
        python_path = self._resolve(self.config.sidecar_python)
        sidecar_root = self._resolve(self.config.sidecar_root)
        checkpoint_dir = self._resolve(self.config.checkpoint_dir)
        engine = str(self.config.engine or "").lower()
        if engine in {"audio_reactive_portrait", "rhubarb_phoneme_portrait"}:
            required = (
                python_path,
                sidecar_root,
                self.project_root / "scripts" / "audio_reactive_presenter.py",
                self.project_root / ".runtime" / "bin" / "ffmpeg.exe",
            )
            if engine == "rhubarb_phoneme_portrait":
                required += (self._resolve(self.config.rhubarb_path),)
        else:
            required = (
                python_path,
                sidecar_root / "inference.py",
                checkpoint_dir / f"SadTalker_V0.0.2_{self.config.render_size}.safetensors",
                checkpoint_dir / "mapping_00229-model.pth.tar",
            )
        if all(path.exists() for path in required):
            return python_path, sidecar_root, checkpoint_dir
        missing = ", ".join(str(path) for path in required if not path.exists())
        self.logger.warning("SYSTEM", f"Presenter renderer unavailable; missing: {missing}")
        return None

    def _trim_hook_audio(self, narration_path: Path, output_path: Path) -> bool:
        try:
            import imageio_ffmpeg

            command = [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(narration_path),
                "-t",
                str(max(3.0, float(self.config.hook_seconds))),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
            return output_path.exists() and output_path.stat().st_size > 20_000
        except Exception as exc:
            self.logger.warning("SYSTEM", f"Could not prepare presenter hook audio: {exc}")
            return False

    def render_hook(
        self,
        avatar_path: Path,
        narration_path: Path,
        run_dir: Path,
        transcript: str = "",
    ) -> Path | None:
        runtime = self._ready()
        if not runtime or not avatar_path.exists() or not narration_path.exists():
            return None
        python_path, sidecar_root, checkpoint_dir = runtime
        avatar_path = avatar_path.resolve()
        narration_path = narration_path.resolve()
        work_dir = (run_dir / "presenter_runtime").resolve()
        result_dir = work_dir / "results"
        work_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)
        hook_audio = work_dir / "presenter_hook.wav"
        if not self._trim_hook_audio(narration_path, hook_audio):
            return None

        command = [
            str(python_path),
        ]
        engine = str(self.config.engine or "").lower()
        direct_output = work_dir / "presenter_hook_audio_reactive.mp4"
        if engine in {"audio_reactive_portrait", "rhubarb_phoneme_portrait"}:
            command.extend([
                str(self.project_root / "scripts" / "audio_reactive_presenter.py"),
                "--source-image",
                str(avatar_path),
                "--audio",
                str(hook_audio),
                "--output",
                str(direct_output),
                "--sadtalker-root",
                str(sidecar_root),
                "--landmark-cache",
                str(self.project_root / ".runtime" / "presenter_landmarks" / f"{avatar_path.stem}.json"),
                "--ffmpeg",
                str(self.project_root / ".runtime" / "bin" / "ffmpeg.exe"),
                "--size",
                "512",
            ])
            if engine == "rhubarb_phoneme_portrait":
                command.extend([
                    "--rhubarb",
                    str(self._resolve(self.config.rhubarb_path)),
                    "--dialog-text",
                    transcript.strip(),
                ])
        else:
            command.extend([
                str(sidecar_root / "inference.py"),
                "--driven_audio",
                str(hook_audio),
                "--source_image",
                str(avatar_path),
                "--checkpoint_dir",
                str(checkpoint_dir),
                "--result_dir",
                str(result_dir),
                "--size",
                str(int(self.config.render_size)),
                "--preprocess",
                "crop",
                "--batch_size",
                "1",
                "--pose_style",
                str(int(self.config.pose_style)),
                "--expression_scale",
                str(float(self.config.expression_scale)),
                "--still",
                "--cpu",
            ])
        log_path = work_dir / "presenter_render.log"
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        runtime_bin = self.project_root / ".runtime" / "bin"
        if runtime_bin.exists():
            environment["PATH"] = f"{runtime_bin}{os.pathsep}{environment.get('PATH', '')}"
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                subprocess.run(
                    command,
                    cwd=sidecar_root,
                    env=environment,
                    check=True,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=max(300, int(self.config.timeout_seconds)),
                )
        except Exception as exc:
            self.logger.warning("SYSTEM", f"Presenter render failed: {exc}")
            return None

        if direct_output.exists() and direct_output.stat().st_size >= 100_000:
            return direct_output
        candidates = sorted(result_dir.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates or candidates[0].stat().st_size < 100_000:
            self.logger.warning("SYSTEM", "Presenter renderer did not produce a usable video.")
            return None
        return candidates[0]
