"""
reupload_brain_lens.py

After re-authenticating brain_lens with the correct channel account,
this script re-uploads all brain_lens videos that were previously
uploaded to the wrong account.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from yt_auto.config import load_config
from yt_auto.pipeline import ShortsFactory

RUNS_FILE = Path(__file__).parent.parent / "data" / "state" / "runs.jsonl"


def get_brain_lens_runs() -> list[dict]:
    runs = []
    for line in RUNS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run.get("channel") == "brain_lens":
            run_dir = Path(run.get("run_dir", ""))
            video_path = run_dir / "short.mp4"
            if not video_path.exists():
                video_path = run_dir / "video.mp4"
            if video_path.exists():
                runs.append(run)
    return runs


def main() -> None:
    print("="*60)
    print("Brain Lens Video Re-uploader")
    print("="*60)

    config = load_config(Path("config/settings.yaml"))
    factory = ShortsFactory(config)
    channel = factory._channel("brain_lens")

    runs = get_brain_lens_runs()
    print(f"Found {len(runs)} brain_lens videos with existing files to re-upload.\n")

    if not runs:
        print("No videos found to re-upload. Check the output/brain_lens directory.")
        sys.exit(0)

    confirm = input(f"Re-upload all {len(runs)} videos to Brain Lens channel? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    success = 0
    failed = []
    for i, run in enumerate(runs, 1):
        run_dir = Path(run["run_dir"])
        video_path = run_dir / "short.mp4"
        if not video_path.exists():
            video_path = run_dir / "video.mp4"

        metadata_path = run_dir / "metadata.json"
        if not metadata_path.exists():
            print(f"[{i}/{len(runs)}] SKIP - no metadata.json in {run_dir.name}")
            continue

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        title = metadata.get("title") or run.get("title", "Untitled")

        print(f"[{i}/{len(runs)}] Uploading: {title[:60]}...", end=" ", flush=True)
        try:
            thumbnail_file = metadata.get("thumbnail_file")
            thumb_path = run_dir / thumbnail_file if thumbnail_file else None
            yt_id = factory.youtube.upload(
                channel=channel,
                video_path=video_path,
                metadata=metadata,
                privacy_status="public",
                thumbnail_path=thumb_path if (thumb_path and thumb_path.exists()) else None,
                is_short=(metadata.get("content_kind") == "short"),
            )
            print(f"✓ {yt_id}")
            success += 1
            time.sleep(1.5)  # Pace uploads to avoid quota issues
        except Exception as exc:
            print(f"✗ FAILED: {exc}")
            failed.append((title, str(exc)))

    print(f"\n{'='*60}")
    print(f"Done! {success}/{len(runs)} videos successfully uploaded to Brain Lens channel.")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for title, err in failed:
            print(f"  '{title[:50]}' -> {err}")


if __name__ == "__main__":
    main()
