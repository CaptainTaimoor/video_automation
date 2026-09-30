"""Describe what each library asset actually shows, using a free vision model.

The library knew what an asset was *searched for* -- "tikal", "adult couple
face to face conversation" -- and nothing about what is in the frame. That is
why footage and narration drifted apart: a scene about carved stelae could be
given a jungle skyline because both were filed under Tikal.

A one-sentence description of the actual picture fixes that at the root. The
lookup can match a narration line against what an image shows, and the
script writer can be told which pictures exist before it writes a word.

Every call carries max_price 0 and refuses provider fallback, like the script
chain, so a vision model that stops being free is refused rather than billed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import requests

# Vision-capable models, fastest first, as (provider, model, max_tokens).
# Measured on one Lascaux photograph:
#
#   groq  qwen/qwen3.8-27b                  0.6s   correct
#   or    inclusionai/ling-3.0-flash-vl     7.3s   correct
#   or    nex-agi/nex-n2.5-pro            139.7s   correct
#
# The token caps matter in both directions. Groq refuses 1,200 for this model
# ("request too large ... on output tokens per minute") and answers at 300.
# ling returned an empty reply at 400 and a correct one at 1,200 -- it spends
# budget before it writes, as the free script models did.
VISION_MODELS = (
    ("groq", "qwen/qwen3.8-27b", 160),
    ("openrouter", "inclusionai/ling-3.0-flash-vl:free", 1200),
    ("openrouter", "nex-agi/nex-n2.5-pro:free", 1200),
)

ENDPOINTS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
}

PROMPT = (
    "You are cataloguing footage for a documentary video editor. "
    "Describe exactly what is visible in this frame in one plain sentence of at "
    "most 30 words: the main subject, what it is doing or what state it is in, "
    "and the setting. Do not guess names or places that are not visible. Then "
    "give 6 short lowercase keyword tags for what is visible. Reply only as "
    'JSON: {"description": "...", "tags": ["...", "..."]}'
)

# Groq's vision model allows 8,000 tokens a minute, input included, and a
# 768px image cost about 2,500 of them -- two or three captions a minute.
# 512px is plenty to say what is in a frame and costs well under half that.
MAX_EDGE = 512


def _image_bytes(path: Path) -> bytes | None:
    """A downscaled JPEG of an image file, or None if it cannot be read."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except Exception:
        return None


def _video_frame_bytes(path: Path, at_fraction: float = 0.4) -> bytes | None:
    """One representative frame from a clip, as a downscaled JPEG."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float((probe.stdout or "0").strip() or 0)
    except Exception:
        duration = 0.0
    seek = max(0.0, duration * at_fraction)
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{seek:.2f}",
                 "-i", str(path), "-frames:v", "1", "-vf",
                 f"scale='min({MAX_EDGE},iw)':-2", str(frame), "-y"],
                capture_output=True, timeout=60,
            )
        except Exception:
            return None
        return _image_bytes(frame) if frame.is_file() else None


def _parse(text: str) -> tuple[str, list[str]]:
    """The description and tags out of a reply, tolerating chatter around it."""
    raw = str(text or "").strip()
    for match in reversed(list(re.finditer(r"\{[\s\S]*?\}", raw))):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        description = str(data.get("description") or "").strip()
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = re.split(r"[,;]", tags)
        tags = [str(t).strip().lower() for t in tags if str(t).strip()]
        if description:
            return description, tags[:10]
    # No JSON: keep the first sentence rather than nothing.
    first = re.split(r"(?<=[.!?])\s+", raw, maxsplit=1)[0].strip()
    return first[:240], []


class VisualCaptioner:
    def __init__(
        self,
        models: tuple[tuple[str, str, int], ...] = VISION_MODELS,
        spacing_seconds: float = 2.0,
        timeout: float = 90.0,
    ) -> None:
        self.models = tuple(models)
        self.spacing_seconds = max(0.0, float(spacing_seconds))
        self.timeout = float(timeout)
        self._last_call = 0.0
        self.last_model = ""
        self.last_error = ""

    def _pace(self) -> None:
        wait = self.spacing_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def describe_bytes(self, jpeg: bytes) -> tuple[str, list[str]] | None:
        if not jpeg:
            self.last_error = "no image"
            return None
        b64 = base64.b64encode(jpeg).decode()
        content = [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]
        errors = []
        for provider, model, max_tokens in self.models:
            url, key_env = ENDPOINTS[provider]
            key = (os.getenv(key_env) or "").strip()
            if not key:
                errors.append(f"{model}: no {key_env}")
                continue
            body = {
                "model": model,
                "max_tokens": int(max_tokens),
                "temperature": 0.2,
                "messages": [{"role": "user", "content": content}],
            }
            if provider == "openrouter":
                # Never spend money, and let a thinking model keep its
                # thinking out of the reply.
                body["provider"] = {"max_price": {"prompt": 0, "completion": 0},
                                    "allow_fallbacks": False}
                body["reasoning"] = {"exclude": True}
            self._pace()
            try:
                response = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json=body,
                    timeout=self.timeout,
                )
            except Exception as exc:
                errors.append(f"{model}: {exc.__class__.__name__}")
                continue
            if response.status_code != 200:
                errors.append(f"{model}: HTTP {response.status_code}")
                continue
            message = ((response.json().get("choices") or [{}])[0].get("message") or {})
            text = (message.get("content") or "").strip() or (message.get("reasoning") or "").strip()
            if not text:
                errors.append(f"{model}: empty")
                continue
            description, tags = _parse(text)
            if description:
                self.last_model = model
                self.last_error = ""
                return description, tags
            errors.append(f"{model}: unparseable")
        self.last_error = "; ".join(errors[-4:])
        return None

    def describe_file(self, path: Path, kind: str = "") -> tuple[str, list[str]] | None:
        path = Path(path)
        is_video = kind == "video" or path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
        jpeg = _video_frame_bytes(path) if is_video else _image_bytes(path)
        if jpeg is None:
            self.last_error = "could not read the file"
            return None
        return self.describe_bytes(jpeg)


def caption_library(
    library,
    *,
    channel_id: str = "",
    subjects: tuple[str, ...] = (),
    limit: int = 50,
    captioner: VisualCaptioner | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Describe up to ``limit`` library assets that have no description yet."""
    captioner = captioner or VisualCaptioner()
    wanted = {s.strip().lower() for s in subjects if s.strip()}
    todo = []
    for row in library.catalog():
        if str(row.get("description") or "").strip():
            continue
        if channel_id and str(row.get("channel") or "") != channel_id:
            continue
        if wanted and str(row.get("subject") or "").strip().lower() not in wanted:
            continue
        todo.append(row)
        if len(todo) >= limit:
            break
    done, failed = {}, 0
    for index, row in enumerate(todo, 1):
        path = library.base / str(row.get("file") or "")
        result = captioner.describe_file(path, str(row.get("kind") or ""))
        if result is None:
            failed += 1
            if progress:
                progress(f"  x {index}/{len(todo)} {row.get('file')} -- {captioner.last_error}")
            continue
        description, tags = result
        done[str(row.get("file"))] = (description, tags)
        if progress:
            progress(f"  {index}/{len(todo)} [{captioner.last_model.split('/')[-1]}] "
                     f"{description[:90]}")
        # Save as we go: a drive can drop, and a half-done batch should keep
        # what it already paid for in time.
        if len(done) % 10 == 0:
            library.update_descriptions(done)
            done = {}
    if done:
        library.update_descriptions(done)
    return {"attempted": len(todo), "failed": failed,
            "captioned": len(todo) - failed}
