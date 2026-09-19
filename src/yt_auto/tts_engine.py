from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

import edge_tts
from moviepy.editor import AudioFileClip
try:
    import yaml
except Exception:  # YAML pronunciations are optional.
    yaml = None


class NarrationEngine:
    def __init__(self, voices: List[str], voice_mode: str = "mix", backend_preference: str = "") -> None:
        self.voices = voices or ["en-US-JennyNeural"]
        self.voice_mode = (voice_mode or "mix").lower()
        self.backend_preference = (backend_preference or os.getenv("YT_TTS_BACKEND") or "edge").strip().lower()
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
            rate="-6%",
            pitch="+0Hz",
            volume="+0%",
        )
        await communicate.save(str(out_path))

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
        # Why this reports instead of returning a bare []: the caller turns an
        # empty list into "Kokoro batch narration failed", which says nothing
        # about the cause and makes a broken voice environment look identical
        # to a bad line of text.
        self.last_kokoro_error = None
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
                self.last_kokoro_error = (
                    f"manifest has {len(items)} clips for {len(texts)} beats"
                )
                return []
            missing = [
                str(item.get("path") or "")
                for item in items
                if not Path(str(item.get("path") or "")).exists()
            ]
            if missing:
                self.last_kokoro_error = f"clip missing on disk: {missing[0]}"
                return []
            return items
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or exc.stdout or "").strip().splitlines()
            self.last_kokoro_error = tail[-1] if tail else f"exit code {exc.returncode}"
            return []
        except subprocess.TimeoutExpired:
            self.last_kokoro_error = f"timed out rendering {len(texts)} beats"
            return []
        except Exception as exc:
            self.last_kokoro_error = f"{exc.__class__.__name__}: {exc}"
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
                    reason = getattr(self, "last_kokoro_error", None)
                    raise RuntimeError(
                        "Kokoro batch narration failed; refusing to use a lower-quality fallback."
                        + (f" Reason: {reason}" if reason else "")
                    )

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
