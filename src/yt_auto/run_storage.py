"""Reclaim the disk a finished run no longer needs.

A run keeps everything it used: the images it downloaded, the narration at
full quality, the per-beat audio, the frame checks. That is exactly right
while the video is being made and worth nothing once it is on YouTube, and it
is what fills a drive -- roughly two thirds of each run's footprint is
intermediate material.

The published video, its thumbnail and the records that prove where the
footage came from are always kept. Sources are evidence: a channel that
cannot say where a picture came from cannot answer a claim about it.

A library root can point anywhere, including a Google Drive folder, so the
reusable footage lives off this disk entirely while builds still read it as
ordinary files.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

# Kept forever: the deliverable and the paperwork behind it.
KEEP_FILES = {
    "short.mp4",
    "video.mp4",
    "thumbnail.jpg",
    "metadata.json",
    "sources.json",
    "quality_review.json",
    "subtitles.srt",
    "pinned_comment.txt",
    "short.media_validation.json",
}

# Rebuildable, and only useful while the video is being made.
DROP_DIRS = {"images", "narration_segments", "frame_check", "clips", "raw", "scenes"}
DROP_SUFFIXES = {".wav", ".log", ".png", ".jpg", ".jpeg", ".mp3"}


def _is_keeper(path: Path) -> bool:
    return path.name in KEEP_FILES


def run_size_bytes(run_dir: Path) -> int:
    total = 0
    for item in Path(run_dir).rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def reclaimable_bytes(run_dir: Path) -> int:
    """How much this run would give back, without changing anything."""
    run_dir = Path(run_dir)
    total = 0
    for item in run_dir.rglob("*"):
        if not item.is_file() or _is_keeper(item):
            continue
        if _in_drop_dir(item, run_dir) or item.suffix.lower() in DROP_SUFFIXES:
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _in_drop_dir(item: Path, run_dir: Path) -> bool:
    try:
        parts = item.relative_to(run_dir).parts
    except ValueError:
        return False
    return bool(parts) and parts[0] in DROP_DIRS


def slim_run(run_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Drop a finished run's intermediate media. Returns what was freed.

    Safe to call twice: anything already gone is simply not there.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return {"ok": False, "reason": "missing", "freed_bytes": 0, "removed": 0}

    freed = 0
    removed = 0
    for name in DROP_DIRS:
        target = run_dir / name
        if not target.is_dir():
            continue
        size = run_size_bytes(target)
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
        freed += size
        removed += 1

    for item in list(run_dir.glob("*")):
        if not item.is_file() or _is_keeper(item):
            continue
        if item.suffix.lower() not in DROP_SUFFIXES:
            continue
        try:
            size = item.stat().st_size
            if not dry_run:
                item.unlink()
        except OSError:
            continue
        freed += size
        removed += 1

    return {"ok": True, "freed_bytes": freed, "removed": removed, "dry_run": dry_run}


def slim_uploaded_runs(
    output_root: Path,
    uploaded_run_dirs: list[str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Slim every run that has already been published.

    Only runs the caller names as uploaded are touched, so a video still
    waiting for its slot keeps everything it might need to be rebuilt.
    """
    freed = 0
    slimmed = 0
    for raw in uploaded_run_dirs:
        run_dir = Path(raw)
        if not run_dir.is_absolute():
            run_dir = Path(output_root) / run_dir
        result = slim_run(run_dir, dry_run=dry_run)
        if result.get("ok") and result.get("freed_bytes"):
            freed += int(result["freed_bytes"])
            slimmed += 1
    return {"runs": slimmed, "freed_bytes": freed, "freed_gb": round(freed / (1024 ** 3), 2), "dry_run": dry_run}


def disk_report(root: Path, output_root: Path) -> dict[str, Any]:
    """What is on disk and how much of it is no longer needed."""
    usage = shutil.disk_usage(str(root))
    output_root = Path(output_root)
    runs = [p for p in output_root.rglob("*") if p.is_dir() and (p / "metadata.json").exists()]
    reclaimable = sum(reclaimable_bytes(run) for run in runs)
    return {
        "free_gb": round(usage.free / (1024 ** 3), 1),
        "total_gb": round(usage.total / (1024 ** 3), 1),
        "output_gb": round(run_size_bytes(output_root) / (1024 ** 3), 2) if output_root.exists() else 0.0,
        "runs": len(runs),
        "reclaimable_gb": round(reclaimable / (1024 ** 3), 2),
    }


def find_abandoned_runs(output_root: Path, *, keep_days: int = 1, today: str | None = None) -> list[Path]:
    """Run folders that never produced a video and are old enough to drop.

    A failed build leaves everything it downloaded behind. They are individually
    small and collectively most of the disk: 467 of them held 7 GB here, against
    0.6 GB of finished videos. Today's are left alone so a build in progress is
    never pulled out from under itself.
    """
    from datetime import datetime, timedelta

    output_root = Path(output_root)
    if not output_root.is_dir():
        return []
    now = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now()
    cutoff = now - timedelta(days=max(0, int(keep_days)))

    abandoned: list[Path] = []
    for channel in output_root.iterdir():
        if not channel.is_dir():
            continue
        for kind in channel.iterdir():
            if not kind.is_dir():
                continue
            for day in kind.iterdir():
                if not day.is_dir():
                    continue
                try:
                    if datetime.strptime(day.name, "%Y-%m-%d") > cutoff:
                        continue
                except ValueError:
                    continue
                for run in day.iterdir():
                    if run.is_dir() and not any(run.glob("*.mp4")):
                        abandoned.append(run)
    return abandoned


def purge_abandoned_runs(
    output_root: Path,
    *,
    keep_days: int = 1,
    dry_run: bool = True,
    today: str | None = None,
) -> dict[str, Any]:
    """Remove failed builds that left no video. Defaults to a dry run."""
    runs = find_abandoned_runs(output_root, keep_days=keep_days, today=today)
    freed = 0
    removed = 0
    for run in runs:
        size = run_size_bytes(run)
        if not dry_run:
            shutil.rmtree(run, ignore_errors=True)
            if run.exists():
                continue
        freed += size
        removed += 1
    return {
        "runs": removed,
        "freed_bytes": freed,
        "freed_gb": round(freed / (1024 ** 3), 2),
        "dry_run": dry_run,
    }
