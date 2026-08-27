"""
bulk_build.py — Generate multiple YouTube Shorts in one run.

Usage:
    python bulk_build.py --count 30 --channel ancient_history [--dry-run]

Each video is built sequentially. A progress log is written to bulk_build_log.txt
and a summary is printed at the end.
"""

import argparse
import datetime
import subprocess
import sys
import time
from pathlib import Path

LOG_FILE = Path(__file__).parent / "bulk_build_log.txt"


def run_build(channel: str, dry_run: bool, index: int, total: int) -> dict:
    cmd = [sys.executable, "run.py", "build", "--channel", channel]
    if dry_run:
        cmd.append("--dry-run")

    print(f"\n{'='*60}")
    print(f"  Video {index}/{total}  |  {datetime.datetime.now().strftime('%H:%M:%S')}")
    print(f"  Channel: {channel}{'  [dry-run]' if dry_run else ''}")
    print(f"{'='*60}")

    start = time.time()
    result = subprocess.run(
        cmd,
        cwd=str(Path(__file__).parent),
        capture_output=False,  # show live output
    )
    elapsed = time.time() - start

    status = "OK" if result.returncode == 0 else f"ERROR (exit {result.returncode})"
    return {
        "index": index,
        "status": status,
        "elapsed": elapsed,
        "timestamp": datetime.datetime.now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="Bulk YouTube Shorts builder")
    parser.add_argument("--count", type=int, default=10, help="Number of videos to generate")
    parser.add_argument("--channel", type=str, default="ancient_history", help="Channel ID")
    parser.add_argument("--dry-run", action="store_true", help="Build locally without uploading")
    args = parser.parse_args()

    total = args.count
    channel = args.channel
    dry_run = args.dry_run

    print(f"\n🎬  Bulk Build — {total} videos  |  Channel: {channel}")
    print(f"    {'DRY RUN  (no upload)' if dry_run else 'LIVE (will upload)'}")
    print(f"    Estimated time: ~{total * 5} – {total * 8} minutes\n")

    results = []
    ok_count = 0
    fail_count = 0

    for i in range(1, total + 1):
        info = run_build(channel=channel, dry_run=dry_run, index=i, total=total)
        results.append(info)
        if info["status"] == "OK":
            ok_count += 1
        else:
            fail_count += 1

        eta_secs = (total - i) * (sum(r["elapsed"] for r in results) / len(results))
        print(f"\n  ✅ Done in {info['elapsed']:.0f}s  |  {ok_count} ok  {fail_count} failed  "
              f"|  ETA: {eta_secs/60:.1f} min remaining\n")

    # ── Summary ────────────────────────────────────────────────────────────────
    total_time = sum(r["elapsed"] for r in results)
    avg_time = total_time / len(results) if results else 0

    summary_lines = [
        "",
        "=" * 60,
        f"  BULK BUILD COMPLETE",
        f"  Total videos:   {total}",
        f"  Succeeded:      {ok_count}",
        f"  Failed:         {fail_count}",
        f"  Total time:     {total_time/60:.1f} min",
        f"  Avg per video:  {avg_time:.0f}s",
        "=" * 60,
        "",
        "  Per-video results:",
    ]
    for r in results:
        summary_lines.append(
            f"    Video {r['index']:>3}:  {r['status']:<18}  {r['elapsed']:.0f}s  |  {r['timestamp']}"
        )

    summary = "\n".join(summary_lines)
    print(summary)

    LOG_FILE.write_text(summary, encoding="utf-8")
    print(f"\n  Log saved to: {LOG_FILE}\n")


if __name__ == "__main__":
    main()
