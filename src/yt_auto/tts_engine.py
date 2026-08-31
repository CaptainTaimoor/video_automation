from __future__ import annotations

import asyncio
import concurrent.futures
import difflib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import List

import edge_tts
from moviepy.editor import AudioFileClip
try:
    import yaml
except Exception:  # YAML pronunciations are optional.
    yaml = None


class NarrationEngine:
    # Keep the continuous voice close to its natural cadence.  A small positive
    # rate correction leaves room for sentence-boundary pauses while keeping a
    # 65-70 word Short below the 40-second hard ceiling.  We refuse to trim
    # narration after synthesis, so this is safer than a post-render stretch.
    EDGE_NARRATION_RATE = "+2%"

    def __init__(
        self,
        voices: List[str],
        voice_mode: str = "mix",
        backend_preference: str = "",
        edge_rate: str | None = None,
        compact_beat_pauses: bool = False,
        compact_sentence_pauses: bool = False,
    ) -> None:
        self.voices = voices or ["en-US-JennyNeural"]
        self.voice_mode = (voice_mode or "mix").lower()
        self.backend_preference = (backend_preference or os.getenv("YT_TTS_BACKEND") or "edge").strip().lower()
        self.edge_rate = str(edge_rate or os.getenv("YT_EDGE_TTS_RATE") or self.EDGE_NARRATION_RATE).strip()
        if not re.fullmatch(r"[+-]\d+(?:\.\d+)?%", self.edge_rate):
            self.edge_rate = self.EDGE_NARRATION_RATE
        self.compact_beat_pauses = bool(compact_beat_pauses)
        self.compact_sentence_pauses = bool(compact_sentence_pauses)
        self.piper_executable = self._default_piper_executable()
        self.piper_model = self._default_piper_model()
        self.piper_config = self._default_piper_config()
        self.http_tts_url = (os.getenv("YT_TTS_API_URL") or "").strip()
        self.http_tts_voice = (os.getenv("YT_TTS_API_VOICE") or "").strip()
        self.kokoro_python = Path(
            os.getenv("YT_KOKORO_PYTHON")
            or Path.cwd() / ".runtime" / "kokoro_env" / "Scripts" / "python.exe"
        )
        self.kokoro_script = Path.cwd() / "scripts" / "kokoro_batch_tts.py"
        configured_kokoro_spec = next(
            (
                voice.split("kokoro-", 1)[1].strip()
                for voice in self.voices
                if str(voice).lower().startswith("kokoro-") and voice.split("kokoro-", 1)[1].strip()
            ),
            "",
        )
        configured_kokoro_voice, _, configured_speed = configured_kokoro_spec.partition("@")
        self.kokoro_voice = (configured_kokoro_voice or os.getenv("YT_KOKORO_VOICE") or "af_heart").strip()
        self.kokoro_speed = float(configured_speed or os.getenv("YT_KOKORO_SPEED", "0.98"))
        self.allow_edge_fallback = os.getenv("YT_ALLOW_EDGE_TTS_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}

    def _default_piper_executable(self) -> Path | None:
        candidates = [
            Path(sys.executable).with_name("piper.exe"),
            Path.cwd() / ".venv" / "Scripts" / "piper.exe",
            Path.cwd() / "venv" / "Scripts" / "piper.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        found = shutil.which("piper") or shutil.which("piper.exe")
        return Path(found) if found else None

    def _default_piper_model(self) -> Path | None:
        candidate = Path.cwd() / "assets" / "voices" / "piper" / "en_US-lessac-medium.onnx"
        return candidate if candidate.exists() else None

    def _default_piper_config(self) -> Path | None:
        candidate = Path.cwd() / "assets" / "voices" / "piper" / "en_US-lessac-medium.onnx.json"
        return candidate if candidate.exists() else None

    def _pick_voice(self) -> str:
        if self.voice_mode == "mix":
            return random.choice(self.voices)
        return self.voices[0]

    def _normalize_text(self, text: str) -> str:
        text = text or ""
        text = text.replace("&", " and ")
        text = text.replace("...", ".")
        pronunciation_overrides = {
            r"\bThermopylae\b": "thur MOP uh lee",
            r"\bXerxes\b": "zerk seez",
            r"\bAchaemenid\b": "uh kee muh nid",
            r"\bPeloponnesian\b": "pell uh puh nee zhun",
            r"\bPtolemy\b": "taw luh mee",
            r"\bPtolemaic\b": "taw luh may ik",
            r"\bSassanid\b": "sass uh nid",
            r"\bHatshepsut\b": "hat shep soot",
            r"\bThutmose\b": "thoot moh suh",
            r"\bNebuchadnezzar\b": "neb yoo kud nez er",
            r"\bHerodotus\b": "huh rod uh tus",
            r"\bDarius\b": "duh rye us",
            r"\bCyrus\b": "sigh rus",
            r"\bSumerian\b": "soo meer ee un",
            r"\bAkkadian\b": "uh kay dee un",
            r"\bCarthage's\b": "car thij",
            r"\bCarthage\b": "car thij",
            r"\bCarthaginian\b": "car thuh JIN ee un",
            r"\bCarthaginians\b": "car thuh JIN ee unz",
            r"\bHannibal\b": "han uh bul",
            r"\bPhoenician\b": "fuh nee shun",
            r"\bPhoenicians\b": "fuh nee shunz",
            r"\bBerber\b": "ber ber",
            r"\bAmazigh\b": "am uh zeeg",
            r"\bEtruscan\b": "ih trus kun",
            r"\bNefertiti\b": "nef er tee tee",
            r"\bTutankhamun\b": "too tang kah moon",
            r"\bhieroglyphics\b": "high roh glif iks",
            r"\bRosetta\b": "roh zet uh",
            r"\bMasada\b": "muh sah duh",
            r"\bZealots\b": "zell uts",
            r"\bMausoleum\b": "maw suh lee um",
            r"\bTerracotta\b": "terra kot uh",
            r"\bEuphrates\b": "you fray teez",
            r"\bTigris\b": "tie gris",
            r"\bNabataean\b": "nab uh tee un",
            r"\bNabataeans\b": "nab uh tee unz",
            r"\bTeutoburg\b": "TOY toh burg",
            r"\bAntikythera\b": "an tee KITH er uh",
            r"\bQin Shi Huang\b": "chin shur hwahng",
            r"\bByzantine\b": "bih ZAN teen",
            r"\bByzantines\b": "bih ZAN teens",
            r"\bByzantium\b": "bih ZAN tee um",
            r"\bNazca\b": "NAHZ kah",
            r"\bNasca\b": "NAHZ kah",
            r"\bMinoan\b": "mih NOH un",
            r"\bMycenaean\b": "my suh NEE un",
            r"\bVisigoth\b": "VIZ ih goth",
            r"\bVisigoths\b": "VIZ ih goths",
            r"\bJustinianic\b": "juh STIN ee AN ik",
            r"\bSasanian\b": "suh SAH nee un",
            r"\bSasanians\b": "suh SAH nee unz",
            r"\bCadaver Synod\b": "kuh dav er sin ud",
            r"\bJustinian\b": "jus tin ee un",
            r"\bAkrotiri\b": "ah kro tee ree",
            r"\bOracle of Delphi\b": "or uh kul of del fee",
            r"\bamygdala\b": "uh mig duh luh",
            r"\bprefrontal cortex\b": "pree frun tul kor teks",
            r"\bcortisol\b": "kor tuh sol",
            r"\bdopamine\b": "doh puh meen",
            r"\bserotonin\b": "sair uh toh nin",
            r"\bneuroplasticity\b": "noor oh pla stiss uh tee",
            r"\brumination\b": "roo muh nay shun",
            r"\bimpostor syndrome\b": "im pah ster sin drohm",
            r"\banxious attachment\b": "ank shus uh tach munt",
            r"\bavoidant attachment\b": "uh voy dunt uh tach munt",
            r"\bcognitive dissonance\b": "kog nuh tiv dis uh nuns",
            r"\bconfirmation bias\b": "kon fur may shun by us",
            r"\banchoring effect\b": "ang ker ing ih fekt",
        }
        if self.backend_preference == "kokoro":
            pronunciation_overrides = {}
        else:
            external = self._load_pronunciation_overrides()
            pronunciation_overrides.update(external)
        for pattern, replacement in pronunciation_overrides.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        text = text.replace("Mont-Blanc", "Mont Blanc")
        text = text.replace("Saint-Louis", "Saint Louis")
        text = re.sub(r"\bSS\s+([A-Z][\w-]*)", r"S S \1", text)
        text = re.sub(r"\bca\.\s*", "around ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bU\.S\.\b", "United States", text)
        text = re.sub(r"\bU\.K\.\b", "United Kingdom", text)
        text = re.sub(r"\bNo\.\s*(\d+)", r"Number \1", text)
        text = re.sub(r"\b(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})\b", r"\2 \1, \3", text)
        text = re.sub(r"(?<=\w)-(?=\w)", " ", text)
        text = re.sub(r"\s*[\u2013\u2014]\s*", ", ", text)
        text = re.sub(r"\s+-\s+", ", ", text)
        text = re.sub(r"\s+,", ",", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _load_pronunciation_overrides(self) -> dict[str, str]:
        path = Path.cwd() / "assets" / "voices" / "pronunciations.yaml"
        if not yaml or not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            raw_terms = data.get("terms", data)
            if not isinstance(raw_terms, dict):
                return {}
            out: dict[str, str] = {}
            for term, spoken in raw_terms.items():
                clean_term = str(term or "").strip()
                clean_spoken = str(spoken or "").strip()
                if clean_term and clean_spoken:
                    out[rf"\b{re.escape(clean_term)}\b"] = clean_spoken
            return out
        except Exception:
            return {}

    async def _render_edge(self, text: str, out_path: Path, voice: str) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=self.edge_rate,
            pitch="+0Hz",
            volume="+0%",
        )
        await communicate.save(str(out_path))

    @staticmethod
    def _is_uncompressed_pcm_wav(path: Path) -> bool:
        try:
            with wave.open(str(path), "rb") as source:
                return (
                    source.getcomptype() == "NONE"
                    and source.getnchannels() > 0
                    and source.getframerate() > 0
                    and source.getsampwidth() in {1, 2, 3, 4}
                )
        except (OSError, EOFError, wave.Error):
            return False

    def _transcode_to_pcm_wav(self, source_path: Path, out_path: Path) -> None:
        """Atomically decode any supported audio input to a real PCM WAV."""

        try:
            import imageio_ffmpeg

            ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_executable = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if not ffmpeg_executable:
            raise RuntimeError("FFmpeg is required to convert Edge narration to PCM WAV.")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{out_path.stem}.pcm-",
            suffix=".wav",
            dir=str(out_path.parent),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            result = subprocess.run(
                [
                    str(ffmpeg_executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source_path),
                    "-vn",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary_path),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0 or not self._is_uncompressed_pcm_wav(temporary_path):
                detail = (result.stderr or "FFmpeg produced no valid PCM WAV").strip()
                raise RuntimeError(f"Could not convert narration to PCM WAV: {detail}")
            temporary_path.replace(out_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _ensure_pcm_wav(self, path: Path) -> None:
        if path.suffix.lower() != ".wav" or self._is_uncompressed_pcm_wav(path):
            return
        self._transcode_to_pcm_wav(path, path)

    def _render_edge_continuous(self, text: str, out_path: Path, voice: str) -> list[dict]:
        """Stream one Edge narration while retaining its word timestamps.

        ``edge_tts`` may split very long input internally to satisfy the service
        limit, but a caller still gets one ``Communicate`` stream and one audio
        file.  Writing beside the destination and replacing it only after a
        complete stream prevents a failed request from leaving publishable-
        looking partial narration behind.
        """

        out_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{out_path.stem}.edge-",
            suffix=f"{out_path.suffix or '.mp3'}.part",
            dir=str(out_path.parent),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        word_boundaries: list[dict] = []

        try:
            async def stream_to_file() -> None:
                communicate = edge_tts.Communicate(
                    text=text,
                    voice=voice,
                    rate=self.edge_rate,
                    pitch="+0Hz",
                    volume="+0%",
                    boundary="WordBoundary",
                )
                with temporary_path.open("wb") as audio_file:
                    async for message in communicate.stream():
                        if message.get("type") == "audio":
                            data = message.get("data")
                            if data:
                                audio_file.write(data)
                        elif message.get("type") == "WordBoundary":
                            word_boundaries.append(dict(message))

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(stream_to_file())
            else:
                # This is a synchronous public API, but it is occasionally
                # invoked from notebook/service code that already owns an event
                # loop.  Run Edge in a worker rather than nesting asyncio.run.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(asyncio.run, stream_to_file()).result()

            if not temporary_path.exists() or temporary_path.stat().st_size <= 0:
                raise RuntimeError("Edge TTS returned no narration audio.")
            if out_path.suffix.lower() == ".wav":
                self._transcode_to_pcm_wav(temporary_path, out_path)
                temporary_path.unlink(missing_ok=True)
            else:
                temporary_path.replace(out_path)
            return word_boundaries
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _render_piper(self, text: str, out_path: Path) -> bool:
        if not self.piper_executable or not self.piper_executable.exists():
            return False
        if not self.piper_model or not self.piper_model.exists():
            return False

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=str(out_path.parent)) as temp_dir:
                input_path = Path(temp_dir) / "input.txt"
                input_path.write_text(text, encoding="utf-8")
                cmd = [
                    str(self.piper_executable),
                    "-m",
                    str(self.piper_model),
                    "-i",
                    str(input_path),
                    "-f",
                    str(out_path),
                    "--length-scale",
                    "0.94",
                    "--noise-scale",
                    "0.7",
                    "--noise-w-scale",
                    "0.8",
                    "--sentence-silence",
                    "0.08",
                ]
                if self.piper_config and self.piper_config.exists():
                    cmd.extend(["-c", str(self.piper_config)])
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return False
            return out_path.exists() and out_path.stat().st_size > 0
        except Exception:
            return False

    def _render_http_tts(self, text: str, out_path: Path, target_voice: str) -> bool:
        """Render using a local OpenAI-compatible TTS server, e.g. Kokoro/Chatterbox FastAPI wrappers.

        Configure with:
          YT_TTS_BACKEND=http
          YT_TTS_API_URL=http://127.0.0.1:8880/v1/audio/speech
          YT_TTS_API_VOICE=af_heart
        """
        if not self.http_tts_url:
            return False
        payload = {
            "model": os.getenv("YT_TTS_API_MODEL", "tts-1"),
            "input": text,
            "voice": self.http_tts_voice or target_voice or "default",
            "response_format": "mp3",
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.http_tts_url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "audio/mpeg, audio/wav, application/octet-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(os.getenv("YT_TTS_API_TIMEOUT", "180"))) as response:
                body = response.read()
            if not body:
                return False
            out_path.write_bytes(body)
            return out_path.exists() and out_path.stat().st_size > 1024
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False

    def _render_kokoro_batch(self, texts: List[str], out_dir: Path) -> list[dict]:
        if not self.kokoro_python.exists() or not self.kokoro_script.exists():
            return []
        out_dir.mkdir(parents=True, exist_ok=True)
        input_path = out_dir / "kokoro_input.json"
        manifest_path = out_dir / "kokoro_manifest.json"
        input_path.write_text(json.dumps(texts, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            str(self.kokoro_python),
            str(self.kokoro_script),
            "--input-json",
            str(input_path),
            "--output-dir",
            str(out_dir),
            "--manifest",
            str(manifest_path),
            "--voice",
            self.kokoro_voice,
            "--speed",
            str(self.kokoro_speed),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=max(300, 180 * len(texts)),
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            items = list(payload.get("items") or [])
            if len(items) != len(texts):
                return []
            if not all(Path(str(item.get("path") or "")).exists() for item in items):
                return []
            return items
        except Exception:
            return []

    def _render_kokoro(self, text: str, out_path: Path) -> bool:
        with tempfile.TemporaryDirectory(dir=str(out_path.parent)) as temp_dir:
            items = self._render_kokoro_batch([text], Path(temp_dir))
            if not items:
                return False
            source = Path(str(items[0]["path"]))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, out_path)
        return out_path.exists() and out_path.stat().st_size > 1024

    def _pick_offline_voice_id(self, engine, target_voice: str) -> str | None:
        try:
            available = engine.getProperty("voices") or []
        except Exception:
            return None

        target_tokens = [token.lower() for token in re.split(r"[-_]", target_voice or "") if token]
        preferred_names = ["zira", "aria", "jenny", "hazel", "susan", "samantha", "david", "guy", "mark"]
        ranked: list[tuple[int, str]] = []
        for voice in available:
            voice_id = str(getattr(voice, "id", ""))
            name = str(getattr(voice, "name", ""))
            searchable = f"{voice_id} {name}".lower()
            score = 0
            for token in target_tokens:
                if token in searchable:
                    score += 5
            for idx, preferred in enumerate(reversed(preferred_names), start=1):
                if preferred in searchable:
                    score += idx
            ranked.append((score, voice_id))

        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] or None

    def _render_pyttsx3(self, text: str, out_path: Path, target_voice: str) -> bool:
        try:
            import pyttsx3  # type: ignore
        except Exception:
            return False

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            engine = pyttsx3.init()
            voice_id = self._pick_offline_voice_id(engine, target_voice)
            if voice_id:
                engine.setProperty("voice", voice_id)
            engine.setProperty("rate", 168)
            engine.setProperty("volume", 1.0)
            engine.save_to_file(text, str(out_path))
            engine.runAndWait()
            engine.stop()
            return out_path.exists() and out_path.stat().st_size > 0
        except Exception:
            return False

    def _render_with_backend(
        self,
        text: str,
        out_path: Path,
        target_voice: str,
        backend_id: str | None = None,
    ) -> str:
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if backend_id == "piper-en_US-lessac-medium":
            if self._render_piper(text=text, out_path=out_path):
                return backend_id
            raise RuntimeError("Piper narration backend failed while rendering a locked segment.")

        if backend_id == "offline-pyttsx3":
            if self._render_pyttsx3(text=text, out_path=out_path, target_voice=target_voice):
                return backend_id
            raise RuntimeError("Offline pyttsx3 narration backend failed while rendering a locked segment.")

        if backend_id and backend_id.startswith("kokoro-"):
            if self._render_kokoro(text=text, out_path=out_path):
                return backend_id
            raise RuntimeError("Kokoro narration backend failed while rendering a locked segment.")

        if backend_id and backend_id not in {"piper-en_US-lessac-medium", "offline-pyttsx3"}:
            asyncio.run(self._render_edge(text=text, out_path=out_path, voice=backend_id))
            return backend_id

        if self.backend_preference in {"http", "api", "local-api", "kokoro", "chatterbox"}:
            if self._render_http_tts(text=text, out_path=out_path, target_voice=target_voice):
                return "http-tts-api"
            if self.backend_preference == "kokoro" and self._render_kokoro(text=text, out_path=out_path):
                return f"kokoro-{self.kokoro_voice}"
            if self.backend_preference == "kokoro":
                raise RuntimeError("Kokoro is required for this channel, but local Kokoro synthesis failed.")
            if self._render_piper(text=text, out_path=out_path):
                return "piper-en_US-lessac-medium"
            if self.allow_edge_fallback:
                try:
                    asyncio.run(self._render_edge(text=text, out_path=out_path, voice=target_voice))
                    return target_voice
                except Exception:
                    pass

        if self.backend_preference in {"piper", "local", "offline"}:
            if self._render_piper(text=text, out_path=out_path):
                return "piper-en_US-lessac-medium"
            if self.allow_edge_fallback:
                try:
                    asyncio.run(self._render_edge(text=text, out_path=out_path, voice=target_voice))
                    return target_voice
                except Exception:
                    pass
        else:
            try:
                asyncio.run(self._render_edge(text=text, out_path=out_path, voice=target_voice))
                return target_voice
            except Exception:
                pass
            if self._render_piper(text=text, out_path=out_path):
                return "piper-en_US-lessac-medium"

        if self._render_pyttsx3(text=text, out_path=out_path, target_voice=target_voice):
            return "offline-pyttsx3"

        raise RuntimeError(
            "Narration generation failed. Piper was unavailable, Edge TTS was unavailable, and offline pyttsx3 is not working. "
            "Keep the Piper model in assets/voices/piper, fix internet for Edge TTS, or install/configure pyttsx3 before building publishable videos."
        )

    def synthesize(self, text: str, out_path: Path) -> str:
        normalized = self._normalize_text(text)
        voice = self._pick_voice()
        return self._render_with_backend(text=normalized, out_path=out_path, target_voice=voice)

    def _estimate_scene_durations(self, beats: List[str], total_duration: float) -> List[float]:
        if not beats:
            return []
        weights = []
        for beat in beats:
            words = [word for word in beat.split() if word.strip()]
            punctuation_bonus = (beat.count(',') * 0.18) + (beat.count(';') * 0.28) + (0.55 if beat.rstrip().endswith(('.', '!', '?')) else 0.0)
            weights.append(max(1.0, len(words) + punctuation_bonus))

        total_weight = sum(weights) or float(len(beats))
        durations = [max(1.2, total_duration * (weight / total_weight)) for weight in weights]
        drift = total_duration - sum(durations)
        durations[-1] += drift
        if durations[-1] < 0.6:
            durations[-1] = 0.6
        return durations

    @staticmethod
    def _timing_tokens(text: str) -> list[str]:
        return [
            token.casefold().replace("\u2019", "'")
            for token in re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)*", text or "", flags=re.UNICODE)
        ]

    @staticmethod
    def _audio_duration_seconds(path: Path) -> float:
        clip = AudioFileClip(str(path))
        try:
            duration = float(clip.duration)
        finally:
            clip.close()
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(f"Narration audio has an invalid duration: {duration!r}")
        return duration

    def _continuous_estimated_durations(self, beats: List[str], total_duration: float) -> List[float]:
        """Allocate an exact audio duration without imposing impossible minima."""

        if not beats:
            return []
        if not math.isfinite(total_duration) or total_duration <= 0:
            raise RuntimeError("Continuous narration duration must be positive.")

        weights: list[float] = []
        for beat in beats:
            words = self._timing_tokens(beat)
            punctuation_bonus = (
                (beat.count(",") * 0.18)
                + (beat.count(";") * 0.28)
                + (beat.count(":") * 0.18)
                + (0.55 if beat.rstrip().endswith((".", "!", "?")) else 0.0)
            )
            weights.append(max(1.0, len(words) + punctuation_bonus))

        total_weight = sum(weights)
        durations = [total_duration * (weight / total_weight) for weight in weights]
        # Make the invariant exact even after floating-point division.  Consumers
        # build a cumulative scene timeline from this list.
        durations[-1] = total_duration - sum(durations[:-1])
        return durations

    @staticmethod
    def _compact_long_sentence_boundaries(text: str, group_size: int = 4) -> str:
        """Lighten micro-pauses without changing any spoken words."""

        sentences = [
            value.strip()
            for value in re.split(r"(?<=[.!?])\s+", str(text or "").strip())
            if value.strip()
        ]
        if len(sentences) < 2:
            return str(text or "").strip()
        group_size = max(2, int(group_size))
        period_index = 0
        rendered: list[str] = []
        for index, sentence in enumerate(sentences):
            is_last = index == len(sentences) - 1
            if not is_last and sentence.endswith("."):
                period_index += 1
                if period_index % group_size:
                    sentence = sentence[:-1].rstrip() + ","
            elif sentence.endswith(("?", "!")):
                period_index = 0
            rendered.append(sentence)
        return " ".join(rendered)

    def _durations_from_edge_boundaries(
        self,
        beats: List[str],
        word_boundaries: list[dict],
        total_duration: float,
    ) -> List[float]:
        """Map Edge word events back to beats, falling back on safe estimates.

        Edge offsets are 100-nanosecond ticks.  Exact token alignment is used
        when possible; a proportional event index handles harmless tokenizer
        differences (for example, a service splitting a contraction).  Any
        missing, non-monotonic, or out-of-range timing data fails closed to an
        exact-total text estimate instead of returning a corrupt scene clock.
        """

        estimated = self._continuous_estimated_durations(beats, total_duration)
        if len(beats) <= 1:
            return [total_duration] if beats else []

        events: list[tuple[float, list[str]]] = []
        for boundary in word_boundaries:
            try:
                offset = float(boundary.get("offset")) / 10_000_000.0
            except (TypeError, ValueError):
                continue
            tokens = self._timing_tokens(str(boundary.get("text") or ""))
            if math.isfinite(offset) and offset >= 0 and tokens:
                events.append((offset, tokens))
        if not events:
            return estimated

        # Metadata is expected in order, but sorting protects the public method
        # from a malformed/mock transport without changing valid Edge output.
        events.sort(key=lambda item: item[0])
        expected_tokens: list[str] = []
        beat_starts: list[int] = []
        for beat in beats:
            beat_starts.append(len(expected_tokens))
            expected_tokens.extend(self._timing_tokens(beat))
        if not expected_tokens or any(start >= len(expected_tokens) for start in beat_starts):
            return estimated

        observed_tokens: list[str] = []
        observed_event_indexes: list[int] = []
        for event_index, (_, tokens) in enumerate(events):
            observed_tokens.extend(tokens)
            observed_event_indexes.extend([event_index] * len(tokens))
        if not observed_tokens:
            return estimated

        expected_to_event: dict[int, int] = {}
        matcher = difflib.SequenceMatcher(
            a=expected_tokens,
            b=observed_tokens,
            autojunk=False,
        )
        matched_tokens = 0
        for block in matcher.get_matching_blocks():
            for token_offset in range(block.size):
                expected_index = block.a + token_offset
                observed_index = block.b + token_offset
                expected_to_event[expected_index] = observed_event_indexes[observed_index]
                matched_tokens += 1

        direct_alignment_is_reliable = matched_tokens >= max(1, min(len(expected_tokens), len(observed_tokens)) // 2)
        timing_starts = [0.0]
        for expected_start in beat_starts[1:]:
            event_index: int | None = None
            if direct_alignment_is_reliable:
                event_index = expected_to_event.get(expected_start)
                if event_index is None:
                    prior = [index for index in expected_to_event if index < expected_start]
                    following = [index for index in expected_to_event if index > expected_start]
                    if prior and following:
                        prior_expected = max(prior)
                        following_expected = min(following)
                        prior_event = expected_to_event[prior_expected]
                        following_event = expected_to_event[following_expected]
                        fraction = (expected_start - prior_expected) / (following_expected - prior_expected)
                        event_index = round(prior_event + ((following_event - prior_event) * fraction))

            if event_index is None:
                fraction = expected_start / float(len(expected_tokens))
                event_index = min(len(events) - 1, round(fraction * len(events)))
            event_index = max(0, min(len(events) - 1, event_index))
            timing_starts.append(events[event_index][0])

        if any(
            not (0.0 < timing_starts[index] < total_duration)
            or timing_starts[index] <= timing_starts[index - 1]
            for index in range(1, len(timing_starts))
        ):
            return estimated

        durations = [
            timing_starts[index + 1] - timing_starts[index]
            for index in range(len(timing_starts) - 1)
        ]
        durations.append(total_duration - timing_starts[-1])
        if any(duration <= 0 or not math.isfinite(duration) for duration in durations):
            return estimated
        durations[-1] = total_duration - sum(durations[:-1])
        return durations

    def synthesize_beats_continuous(
        self,
        beats: List[str],
        out_dir: Path,
        out_path: Path,
    ) -> tuple[str, List[float]]:
        """Synthesize all beats as one performance and return their durations.

        Edge is rendered through one ``Communicate`` stream so its real word
        timestamps can anchor beat boundaries.  Explicit HTTP, Kokoro, Piper,
        and offline preferences also receive the complete story in one call;
        their timings are estimated from the exact final file duration because
        those backends do not expose portable word alignment metadata.
        """

        clean_beats: list[str] = []
        for beat in beats:
            normalized = self._normalize_text(str(beat or ""))
            if normalized:
                clean_beats.append(normalized)
        if not clean_beats:
            raise RuntimeError("Narration beat list was empty.")

        out_dir = Path(out_dir)
        out_path = Path(out_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        spoken_beats = (
            [self._compact_long_sentence_boundaries(beat) for beat in clean_beats]
            if self.compact_sentence_pauses
            else clean_beats
        )
        if self.compact_beat_pauses and len(spoken_beats) > 1:
            # A period between every authored beat makes Edge insert long,
            # repeated pauses.  Keep sentence punctuation inside each beat,
            # but use a light comma at beat boundaries so the read remains
            # continuous while visual timing still comes from word events.
            boundary_safe = [beat.rstrip(" .!?;,:…") for beat in spoken_beats]
            continuous_text = ", ".join(boundary_safe[:-1]) + ". " + boundary_safe[-1]
        else:
            continuous_text = " ".join(spoken_beats)
        edge_voices = [
            str(voice)
            for voice in self.voices
            if not str(voice).lower().startswith("kokoro-")
        ]
        if edge_voices:
            target_voice = random.choice(edge_voices) if self.voice_mode == "mix" else edge_voices[0]
        else:
            target_voice = self._pick_voice()
        configured_kokoro = next(
            (
                f"kokoro-{self.kokoro_voice}"
                for voice in self.voices
                if str(voice).lower().startswith("kokoro-")
            ),
            "",
        )
        word_boundaries: list[dict] = []

        edge_controlled_preferences = {
            "http",
            "api",
            "local-api",
            "kokoro",
            "chatterbox",
            "piper",
            "local",
            "offline",
        }
        if self.backend_preference not in edge_controlled_preferences and edge_voices:
            try:
                word_boundaries = self._render_edge_continuous(
                    text=continuous_text,
                    out_path=out_path,
                    voice=target_voice,
                )
                backend_id = target_voice
            except Exception as edge_error:
                backend_id = ""
                fallback_errors: list[Exception] = [edge_error]
                fallback_ids = [
                    fallback_id
                    for fallback_id in (
                        configured_kokoro,
                        "piper-en_US-lessac-medium",
                        "offline-pyttsx3",
                    )
                    if fallback_id
                ]
                for fallback_id in fallback_ids:
                    try:
                        backend_id = self._render_with_backend(
                            text=continuous_text,
                            out_path=out_path,
                            target_voice=target_voice,
                            backend_id=fallback_id,
                        )
                        break
                    except Exception as fallback_error:
                        fallback_errors.append(fallback_error)
                if not backend_id:
                    raise RuntimeError(
                        "Continuous narration failed with Edge TTS and all local fallbacks."
                    ) from fallback_errors[-1]
        elif self.backend_preference not in edge_controlled_preferences and configured_kokoro:
            # A voice list containing only a Kokoro marker has no valid Edge
            # voice to send to the service.  Keep the story continuous and use
            # that explicitly configured local voice directly.
            backend_id = self._render_with_backend(
                text=continuous_text,
                out_path=out_path,
                target_voice=target_voice,
                backend_id=configured_kokoro,
            )
        else:
            # Existing preference and fallback policy is preserved, but receives
            # one complete story rather than one request per beat.
            backend_id = self._render_with_backend(
                text=continuous_text,
                out_path=out_path,
                target_voice=target_voice,
            )

        self._ensure_pcm_wav(out_path)
        total_duration = self._audio_duration_seconds(out_path)
        if word_boundaries:
            durations = self._durations_from_edge_boundaries(
                clean_beats,
                word_boundaries,
                total_duration,
            )
        else:
            durations = self._continuous_estimated_durations(clean_beats, total_duration)

        (out_dir / "beats.txt").write_text("\n".join(clean_beats), encoding="utf-8")
        return backend_id, durations

    def synthesize_beats(
        self,
        beats: List[str],
        out_dir: Path,
        out_path: Path,
        pause_seconds: float = 0.12,
    ) -> tuple[str, List[float]]:
        clean_beats = [self._normalize_text(beat) for beat in beats if self._normalize_text(beat)]
        if not clean_beats:
            raise RuntimeError("Narration beat list was empty.")

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        target_voice = self._pick_voice()
        segments: List[AudioFileClip] = []
        actual_durations: List[float] = []
        backend_id = None

        print(f"Synthesizing {len(clean_beats)} beats with precise timing...")
        
        try:
            kokoro_items = []
            if self.backend_preference == "kokoro" and not self.http_tts_url:
                kokoro_items = self._render_kokoro_batch(clean_beats, out_dir)
                if not kokoro_items:
                    raise RuntimeError("Kokoro batch narration failed; refusing to use a lower-quality fallback.")

            for idx, beat in enumerate(clean_beats):
                if kokoro_items:
                    segment_path = Path(str(kokoro_items[idx]["path"]))
                    current_backend = f"kokoro-{self.kokoro_voice}"
                else:
                    segment_path = out_dir / f"beat_{idx:03d}.mp3"
                    current_backend = self._render_with_backend(
                        text=beat,
                        out_path=segment_path,
                        target_voice=target_voice,
                    )
                if backend_id is None:
                    backend_id = current_backend
                
                # Load to get exact duration
                clip = AudioFileClip(str(segment_path))
                dur = float(clip.duration)
                
                # Add optional pause between beats (except last)
                if pause_seconds > 0 and idx < len(clean_beats) - 1:
                    from moviepy.audio.AudioClip import AudioArrayClip
                    import numpy as np
                    silence = AudioArrayClip(np.zeros((int(44100 * pause_seconds), 2)), fps=44100)
                    segments.append(clip)
                    segments.append(silence)
                    actual_durations.append(dur + pause_seconds)
                else:
                    segments.append(clip)
                    actual_durations.append(dur)

            # Concatenate all segments into the final narration file
            from moviepy.editor import concatenate_audioclips
            final_audio = concatenate_audioclips(segments)
            final_audio.write_audiofile(str(out_path), fps=44100, bitrate="192k", logger=None)
            final_audio.close()

            # Save the script for reference
            script_path = out_dir / "beats.txt"
            script_path.write_text("\n".join(clean_beats), encoding="utf-8")

            return backend_id or target_voice, actual_durations

        finally:
            for s in segments:
                try:
                    s.close()
                except Exception:
                    pass
