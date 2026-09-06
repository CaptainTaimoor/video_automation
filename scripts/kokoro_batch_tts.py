from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--speed", type=float, default=0.98)
    parser.add_argument("--sample-rate", type=int, default=24000)
    return parser.parse_args()


def render(args: argparse.Namespace) -> None:
    input_path = Path(args.input_json).resolve()
    output_dir = Path(args.output_dir).resolve()
    manifest_path = Path(args.manifest).resolve()
    texts = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(texts, list) or not texts:
        raise ValueError("Kokoro input must be a non-empty JSON list of text strings")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(max(1, min(6, (os.cpu_count() or 2) - 1)))
    lang_code = args.voice[0].lower() if args.voice and args.voice[0].lower() in {"a", "b"} else "a"
    pipeline = KPipeline(lang_code=lang_code)
    rendered: list[dict] = []

    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        if not text:
            raise ValueError(f"Kokoro text at index {index} is empty")
        chunks: list[np.ndarray] = []
        for _, _, audio in pipeline(text, voice=args.voice, speed=float(args.speed)):
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
            if samples.size:
                chunks.append(samples)
        if not chunks:
            raise RuntimeError(f"Kokoro produced no audio for item {index}")

        samples = np.concatenate(chunks)
        fade_samples = min(int(args.sample_rate * 0.008), samples.size // 2)
        if fade_samples:
            fade = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
            samples[:fade_samples] *= fade
            samples[-fade_samples:] *= fade[::-1]
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        if peak > 0.98:
            samples *= 0.98 / peak

        output_path = output_dir / f"beat_{index:03d}.wav"
        sf.write(output_path, samples, int(args.sample_rate), subtype="PCM_16")
        rendered.append(
            {
                "index": index,
                "path": str(output_path),
                "duration": round(float(samples.size) / float(args.sample_rate), 4),
                "voice": args.voice,
                "speed": float(args.speed),
                "text": text,
            }
        )

    manifest_path.write_text(
        json.dumps({"engine": "kokoro", "items": rendered}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    render(parse_args())
