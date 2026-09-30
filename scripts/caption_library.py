"""Describe library assets with a free vision model.

    python scripts/caption_library.py --channel ancient_history --subject lascaux --limit 25
    python scripts/caption_library.py --channel brain_lens --limit 40

Only assets without a description are touched, so it is safe to run again
and again: each run picks up where the last one stopped.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_env()
    from yt_auto.config import load_config
    from yt_auto.visual_captioner import VisualCaptioner, caption_library
    from yt_auto.visual_library import VisualLibrary

    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="")
    parser.add_argument("--subject", action="append", default=[])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--spacing", type=float, default=3.0)
    args = parser.parse_args()

    config = load_config(ROOT / "config" / "settings.yaml")
    visuals = config.app.visuals or {}
    root = str(visuals.get("library_root") or "").strip()
    if not root:
        print("app.visuals.library_root is not set")
        return 1
    library = VisualLibrary(root)
    if not library.available:
        print(f"library not reachable at {root}")
        return 1
    result = caption_library(
        library,
        channel_id=args.channel,
        subjects=tuple(args.subject),
        limit=args.limit,
        captioner=VisualCaptioner(spacing_seconds=args.spacing),
        progress=lambda line: print(line, flush=True),
    )
    print(f"\ncaptioned {result['captioned']} of {result['attempted']} "
          f"({result['failed']} failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
