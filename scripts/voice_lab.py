from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "voice_lab"
SAMPLE = (
    "You can feel someone pulling away before they say a word. "
    "Your brain notices the tiny delay, the colder reply, the missing warmth. "
    "But here is the twist: chasing the signal usually makes it disappear faster."
)


def run_backend(name: str, env: dict[str, str], text: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = OUT / f"{stamp}_{name}.mp3"
    code = (
        "from pathlib import Path; "
        "from yt_auto.tts_engine import NarrationEngine; "
        f"engine=NarrationEngine(['en-US-AriaNeural'],'single'); "
        f"backend=engine.synthesize({text!r}, Path(r'{out_path}')); "
        "print(backend)"
    )
    merged_env = os.environ.copy()
    existing_pythonpath = merged_env.get("PYTHONPATH", "")
    src_path = str(ROOT / "src")
    merged_env["PYTHONPATH"] = src_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    merged_env.update(env)
    start = time.time()
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    return {
        "name": name,
        "ok": result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024,
        "path": str(out_path),
        "seconds": round(time.time() - start, 2),
        "backend": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "",
        "error": result.stderr.strip()[-600:] if result.returncode else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Brain Lens voice samples for honest comparison.")
    parser.add_argument("--text", default=SAMPLE)
    parser.add_argument("--http-url", default=os.getenv("YT_TTS_API_URL", ""), help="OpenAI-compatible local TTS URL, e.g. Kokoro/Chatterbox wrapper")
    parser.add_argument("--http-voice", default=os.getenv("YT_TTS_API_VOICE", "af_heart"))
    args = parser.parse_args()

    tests = [
        ("edge_aria", {"YT_TTS_BACKEND": "edge"}),
        ("offline_piper_or_pyttsx3", {"YT_TTS_BACKEND": "offline"}),
    ]
    if args.http_url:
        tests.append(("local_http_tts", {
            "YT_TTS_BACKEND": "http",
            "YT_TTS_API_URL": args.http_url,
            "YT_TTS_API_VOICE": args.http_voice,
        }))

    results = [run_backend(name, env, args.text) for name, env in tests]
    report_path = OUT / "voice_lab_report.json"
    report_path.write_text(json.dumps({"sample_text": args.text, "results": results}, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "results": results}, indent=2))
    return 0 if any(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
