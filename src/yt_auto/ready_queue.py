"""Ready Queue: plan/render ahead, upload at slot time (or gap rescue)."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from yt_auto.utils import now_in_tz, read_json, write_json

ACTIVE_STATUSES = frozenset(
    {
        "planned",
        "scripted",
        "assets_ready",
        "rendering",
        "rendered",
        "queued_upload",
        "held_quality",
        "failed",
    }
)
RENDERABLE_STATUSES = frozenset({"planned", "scripted", "assets_ready", "failed"})
UPLOADABLE_STATUSES = frozenset({"rendered", "queued_upload"})
TERMINAL_STATUSES = frozenset({"uploaded", "cancelled"})

# How many Shorts to keep rendered ahead (buffer targets).
BUFFER_SHORTS = {
    "ancient_history": 3,
    "brain_lens": 2,
}
BUFFER_VIDEOS = {
    "ancient_history": 1,
    "brain_lens": 1,
}

FLAG_PATH_REL = Path("controls") / "flags" / "ready_queue.enabled"
_lock = threading.RLock()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def flag_path(root: Path | None = None) -> Path:
    return (root or _repo_root()) / FLAG_PATH_REL


def is_ready_queue_enabled(root: Path | None = None) -> bool:
    """ON when flag file is 1/true OR env YT_READY_QUEUE is truthy.

    Explicit env ``0/false/off`` wins over a stale flag file.
    """
    env = str(os.environ.get("YT_READY_QUEUE", "") or "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        return False
    if env in {"1", "true", "yes", "on"}:
        return True
    path = flag_path(root)
    if not path.exists():
        return False
    raw = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def set_ready_queue_enabled(enabled: bool, root: Path | None = None) -> dict[str, Any]:
    root = root or _repo_root()
    path = flag_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n" if enabled else "0\n", encoding="utf-8")
    env_path = root / ".env"
    _upsert_env_key(env_path, "YT_READY_QUEUE", "1" if enabled else "0")
    os.environ["YT_READY_QUEUE"] = "1" if enabled else "0"
    return {"enabled": enabled, "flag_path": str(path)}


def _upsert_env_key(env_path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    found = False
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def queue_path(state_dir: Path, channel_id: str) -> Path:
    safe = "".join(c for c in channel_id if c.isalnum() or c in {"-", "_"}) or "channel"
    return state_dir / f"{safe}_ready_queue.json"


def day_pack_dir(root: Path, channel_id: str, day: str) -> Path:
    return root / "data" / "day_packs" / channel_id / day


def _empty_queue(channel_id: str) -> dict[str, Any]:
    return {
        "channel": channel_id,
        "updated_at": None,
        "items": [],
    }


def load_queue(state_dir: Path, channel_id: str) -> dict[str, Any]:
    path = queue_path(state_dir, channel_id)
    data = read_json(path, _empty_queue(channel_id))
    if not isinstance(data, dict):
        data = _empty_queue(channel_id)
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
    data["channel"] = channel_id
    return data


def save_queue(state_dir: Path, channel_id: str, data: dict[str, Any]) -> None:
    data = dict(data)
    data["channel"] = channel_id
    data["updated_at"] = datetime.now().isoformat()
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
    write_json(queue_path(state_dir, channel_id), data)


def _aware_now(timezone_name: str) -> datetime:
    try:
        return now_in_tz(timezone_name)
    except Exception:
        return datetime.now().astimezone()


def _now_iso(timezone_name: str) -> str:
    return _aware_now(timezone_name).isoformat()


def _new_item(
    *,
    channel_id: str,
    content_kind: str,
    target_slot: str,
    target_date: str,
    priority: int,
    timezone_name: str,
) -> dict[str, Any]:
    stamp = _now_iso(timezone_name)
    return {
        "id": f"rq_{uuid.uuid4().hex[:12]}",
        "channel": channel_id,
        "content_kind": content_kind,
        "target_slot": target_slot,
        "target_date": target_date,
        "priority": int(priority),
        "status": "planned",
        "run_dir": None,
        "video_path": None,
        "title": None,
        "topic_fingerprint": None,
        "created_at": stamp,
        "updated_at": stamp,
        "error": None,
        "attempts": 0,
        "force_flags": {
            "force_render": False,
            "force_next": False,
            "skip_slot_wait": False,
        },
    }


def list_items(
    state_dir: Path,
    channel_ids: list[str] | None = None,
    *,
    statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    channels = channel_ids or []
    if not channels:
        # Discover from existing queue files.
        for path in state_dir.glob("*_ready_queue.json"):
            name = path.name[: -len("_ready_queue.json")]
            if name:
                channels.append(name)
    out: list[dict[str, Any]] = []
    for channel_id in channels:
        data = load_queue(state_dir, channel_id)
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            if statuses and str(item.get("status") or "") not in statuses:
                continue
            out.append(dict(item))
    out.sort(
        key=lambda it: (
            str(it.get("target_date") or ""),
            str(it.get("target_slot") or ""),
            int(it.get("priority") or 0),
            str(it.get("created_at") or ""),
        )
    )
    return out


def get_item(state_dir: Path, item_id: str) -> dict[str, Any] | None:
    for path in state_dir.glob("*_ready_queue.json"):
        channel_id = path.name[: -len("_ready_queue.json")]
        data = load_queue(state_dir, channel_id)
        for item in data.get("items") or []:
            if isinstance(item, dict) and str(item.get("id") or "") == item_id:
                return dict(item)
    return None


def update_item(state_dir: Path, item_id: str, **updates: Any) -> dict[str, Any] | None:
    with _lock:
        for path in state_dir.glob("*_ready_queue.json"):
            channel_id = path.name[: -len("_ready_queue.json")]
            data = load_queue(state_dir, channel_id)
            items = data.get("items") or []
            for index, item in enumerate(items):
                if not isinstance(item, dict) or str(item.get("id") or "") != item_id:
                    continue
                item = dict(item)
                force_flags = updates.pop("force_flags", None)
                item.update(updates)
                if isinstance(force_flags, dict):
                    flags = dict(item.get("force_flags") or {})
                    flags.update(force_flags)
                    item["force_flags"] = flags
                item["updated_at"] = datetime.now().isoformat()
                items[index] = item
                data["items"] = items
                save_queue(state_dir, channel_id, data)
                return dict(item)
    return None


def cancel_item(state_dir: Path, item_id: str) -> dict[str, Any] | None:
    return update_item(state_dir, item_id, status="cancelled", error=None)


def retry_item(state_dir: Path, item_id: str) -> dict[str, Any] | None:
    item = get_item(state_dir, item_id)
    if not item:
        return None
    if str(item.get("status") or "") not in {"failed", "cancelled", "held_quality"}:
        return item
    return update_item(
        state_dir,
        item_id,
        status="planned",
        error=None,
        force_flags={"force_render": True},
    )


def clear_cancelled(state_dir: Path, channel_ids: list[str] | None = None) -> int:
    removed = 0
    channels = channel_ids or [
        p.name[: -len("_ready_queue.json")] for p in state_dir.glob("*_ready_queue.json")
    ]
    with _lock:
        for channel_id in channels:
            data = load_queue(state_dir, channel_id)
            before = len(data.get("items") or [])
            data["items"] = [
                item
                for item in (data.get("items") or [])
                if not (isinstance(item, dict) and str(item.get("status") or "") == "cancelled")
            ]
            removed += before - len(data["items"])
            save_queue(state_dir, channel_id, data)
    return removed


def channel_slots_from_config(channel: Any) -> list[tuple[str, str]]:
    """Return [(HH:MM, content_kind), ...] for a ChannelConfig-like object."""
    slots: list[tuple[str, str]] = []
    videos = getattr(channel, "videos", None)
    shorts = getattr(channel, "shorts", None)
    if videos is not None and getattr(videos, "enabled", False):
        slots.extend((str(t), "video") for t in (getattr(videos, "schedule_times", None) or []))
    if shorts is not None and getattr(shorts, "enabled", True):
        slots.extend((str(t), "short") for t in (getattr(shorts, "schedule_times", None) or []))
    if not slots:
        slots.extend((str(t), "short") for t in (getattr(channel, "schedule_times", None) or []))
    slots.sort(key=lambda item: item[0])
    return slots


def build_day_pack(
    *,
    state_dir: Path,
    root: Path,
    channel: Any,
    timezone_name: str,
    day: str | None = None,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Create today's planned queue items + day pack plan.json."""
    channel_id = str(getattr(channel, "id", "") or "")
    now = _aware_now(timezone_name)
    day = day or now.strftime("%Y-%m-%d")
    slots = channel_slots_from_config(channel)
    pack_dir = day_pack_dir(root, channel_id, day)
    pack_dir.mkdir(parents=True, exist_ok=True)

    with _lock:
        data = load_queue(state_dir, channel_id)
        items: list[dict[str, Any]] = list(data.get("items") or [])
        if rebuild:
            items = [
                item
                for item in items
                if not (
                    isinstance(item, dict)
                    and str(item.get("target_date") or "") == day
                    and str(item.get("status") or "") in ACTIVE_STATUSES - {"rendered", "queued_upload", "held_quality"}
                )
            ]

        existing_keys = {
            (str(it.get("target_date") or ""), str(it.get("target_slot") or ""), str(it.get("content_kind") or ""))
            for it in items
            if isinstance(it, dict) and str(it.get("status") or "") not in TERMINAL_STATUSES
        }

        created: list[dict[str, Any]] = []
        for priority, (hhmm, kind) in enumerate(slots):
            key = (day, hhmm, kind)
            if key in existing_keys:
                continue
            item = _new_item(
                channel_id=channel_id,
                content_kind=kind,
                target_slot=hhmm,
                target_date=day,
                priority=priority,
                timezone_name=timezone_name,
            )
            items.append(item)
            created.append(item)
            existing_keys.add(key)

        data["items"] = items
        save_queue(state_dir, channel_id, data)

    plan = {
        "channel": channel_id,
        "day": day,
        "created_at": _now_iso(timezone_name),
        "slots": [{"time": t, "content_kind": k} for t, k in slots],
        "item_ids": [
            str(it.get("id"))
            for it in items
            if isinstance(it, dict) and str(it.get("target_date") or "") == day
        ],
        "created_count": len(created),
    }
    write_json(pack_dir / "plan.json", plan)
    return plan


def count_rendered(
    state_dir: Path,
    channel_id: str,
    content_kind: str = "short",
) -> int:
    return sum(
        1
        for item in load_queue(state_dir, channel_id).get("items") or []
        if isinstance(item, dict)
        and str(item.get("content_kind") or "") == content_kind
        and str(item.get("status") or "") in UPLOADABLE_STATUSES
        and Path(str(item.get("video_path") or "")).exists()
    )


def buffer_target(channel_id: str, content_kind: str = "short") -> int:
    if content_kind == "video":
        return int(BUFFER_VIDEOS.get(channel_id, 1))
    return int(BUFFER_SHORTS.get(channel_id, 2))


def pick_next_to_render(
    state_dir: Path,
    channel_ids: list[str],
    *,
    timezone_name: str,
    urgent_within_minutes: int = 20,
) -> dict[str, Any] | None:
    """Pick the best queue item to render next while respecting buffer targets."""
    now = _aware_now(timezone_name)
    candidates: list[tuple[tuple, dict[str, Any]]] = []

    for channel_id in channel_ids:
        rendered_shorts = count_rendered(state_dir, channel_id, "short")
        rendered_videos = count_rendered(state_dir, channel_id, "video")
        short_target = buffer_target(channel_id, "short")
        video_target = buffer_target(channel_id, "video")

        for item in load_queue(state_dir, channel_id).get("items") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            flags = item.get("force_flags") or {}
            force = bool(flags.get("force_render") or flags.get("force_next"))
            if status == "rendering":
                continue
            if status not in RENDERABLE_STATUSES and not force:
                continue
            kind = str(item.get("content_kind") or "short")
            if not force:
                if kind == "short" and rendered_shorts >= short_target:
                    # Still allow urgent near-slot renders.
                    pass
                if kind == "video" and rendered_videos >= video_target:
                    pass

            day = str(item.get("target_date") or now.strftime("%Y-%m-%d"))
            slot = str(item.get("target_slot") or "23:59")
            try:
                hour, minute = slot.split(":")
                slot_dt = now.replace(
                    year=int(day[0:4]),
                    month=int(day[5:7]),
                    day=int(day[8:10]),
                    hour=int(hour),
                    minute=int(minute),
                    second=0,
                    microsecond=0,
                )
            except Exception:
                slot_dt = now + timedelta(hours=12)

            minutes_until = (slot_dt - now).total_seconds() / 60.0
            urgent = minutes_until <= float(urgent_within_minutes)
            over_buffer = (
                (kind == "short" and rendered_shorts >= short_target)
                or (kind == "video" and rendered_videos >= video_target)
            )
            if over_buffer and not urgent and not force:
                continue

            # Lower score = higher priority.
            score = (
                0 if force else 1,
                0 if urgent else 1,
                0 if kind == "short" else 1,
                minutes_until,
                int(item.get("priority") or 0),
                str(item.get("created_at") or ""),
            )
            candidates.append((score, item))

    if not candidates:
        # Nothing new to make: go back to today's slots that failed or were
        # held. A slot used to give up after three attempts and a held video
        # was never remade, so the day ended with red and dotted slots while
        # the encoder sat idle. Each retry is a fresh build of that slot.
        return _pick_backfill(state_dir, channel_ids, today=now.strftime("%Y-%m-%d"))
    candidates.sort(key=lambda row: row[0])
    return dict(candidates[0][1])


# A failed or held slot is retried only while nothing else needs the encoder,
# at most this many times a day, and not sooner than the cooldown after its
# last try, so one stubborn slot cannot hold the machine all day.
BACKFILL_MAX_ATTEMPTS = 8
BACKFILL_COOLDOWN_MINUTES = 20


def _pick_backfill(
    state_dir: Path,
    channel_ids: list[str],
    *,
    today: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The earliest slot today that failed or was held and is due another try."""
    moment = now or datetime.now()
    rows: list[tuple[tuple[str, str], dict[str, Any]]] = []
    for channel_id in channel_ids:
        for item in load_queue(state_dir, channel_id).get("items") or []:
            if not isinstance(item, dict) or str(item.get("target_date") or "") != today:
                continue
            if str(item.get("status") or "") not in {"failed", "held_quality"}:
                continue
            if int(item.get("attempts") or 0) >= BACKFILL_MAX_ATTEMPTS:
                continue
            last = _naive_stamp(item.get("updated_at"))
            if last is not None and moment - last < timedelta(minutes=BACKFILL_COOLDOWN_MINUTES):
                continue
            rows.append(
                (
                    (str(item.get("target_slot") or "99:99").zfill(5), str(item.get("created_at") or "")),
                    item,
                )
            )
    if not rows:
        return None
    rows.sort(key=lambda row: row[0])
    return dict(rows[0][1])


def _naive_stamp(value: Any) -> datetime | None:
    """Parse a stored timestamp as local naive time, or None when unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def pick_ready_for_upload(
    state_dir: Path,
    channel_id: str,
    content_kind: str,
    *,
    target_slot: str | None = None,
    target_date: str | None = None,
    allow_skip_slot_wait: bool = False,
) -> dict[str, Any] | None:
    """Find a rendered item for this channel/kind, preferring matching slot."""
    items = [
        item
        for item in load_queue(state_dir, channel_id).get("items") or []
        if isinstance(item, dict)
        and str(item.get("content_kind") or "") == content_kind
        and str(item.get("status") or "") in UPLOADABLE_STATUSES
        and str(item.get("status") or "") != "held_quality"
    ]
    usable: list[dict[str, Any]] = []
    for item in items:
        video_path = Path(str(item.get("video_path") or ""))
        if not video_path.exists():
            continue
        flags = item.get("force_flags") or {}
        if allow_skip_slot_wait or flags.get("skip_slot_wait") or flags.get("force_next"):
            usable.append(item)
            continue
        if target_date and str(item.get("target_date") or "") not in {"", target_date}:
            # Prefer same-day but allow older leftover rendered.
            pass
        usable.append(item)

    if not usable:
        return None

    def sort_key(item: dict[str, Any]) -> tuple:
        slot_match = 0 if target_slot and str(item.get("target_slot") or "") == target_slot else 1
        date_match = 0 if target_date and str(item.get("target_date") or "") == target_date else 1
        return (
            slot_match,
            date_match,
            str(item.get("target_date") or ""),
            str(item.get("target_slot") or ""),
            int(item.get("priority") or 0),
            str(item.get("created_at") or ""),
        )

    usable.sort(key=sort_key)
    return dict(usable[0])


def mark_rendering(state_dir: Path, item_id: str) -> dict[str, Any] | None:
    item = get_item(state_dir, item_id)
    if not item:
        return None
    attempts = int(item.get("attempts") or 0) + 1
    return update_item(
        state_dir,
        item_id,
        status="rendering",
        attempts=attempts,
        error=None,
        force_flags={"force_render": False},
    )


def attach_render_result(
    state_dir: Path,
    item_id: str,
    *,
    run_dir: str | Path | None,
    video_path: str | Path | None,
    title: str | None,
    quality_decision: str | None = None,
    error: str | None = None,
    topic_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    decision = str(quality_decision or "").strip().lower()
    video = Path(str(video_path or "")) if video_path else None
    if error:
        status = "failed"
    elif decision and decision != "pass":
        status = "held_quality"
    elif video and video.exists():
        status = "rendered"
    else:
        status = "failed"
        error = error or "render finished without a usable video file"
    return update_item(
        state_dir,
        item_id,
        status=status,
        run_dir=str(run_dir) if run_dir else None,
        video_path=str(video) if video else None,
        title=title,
        topic_fingerprint=topic_fingerprint,
        error=error,
    )


def mark_uploaded(
    state_dir: Path,
    item_id: str,
    *,
    youtube_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    if error:
        return update_item(state_dir, item_id, status="failed", error=error)
    return update_item(
        state_dir,
        item_id,
        status="uploaded",
        error=None,
        youtube_id=youtube_id,
        force_flags={"skip_slot_wait": False, "force_next": False},
    )


def queue_summary(state_dir: Path, channel_ids: list[str], *, timezone_name: str, root: Path | None = None) -> dict[str, Any]:
    root = root or _repo_root()
    today = _aware_now(timezone_name).strftime("%Y-%m-%d")
    items = list_items(state_dir, channel_ids)
    by_status: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1

    packs = {}
    for channel_id in channel_ids:
        plan_path = day_pack_dir(root, channel_id, today) / "plan.json"
        packs[channel_id] = {
            "day": today,
            "exists": plan_path.exists(),
            "plan": read_json(plan_path, {}) if plan_path.exists() else {},
            "rendered_shorts": count_rendered(state_dir, channel_id, "short"),
            "rendered_videos": count_rendered(state_dir, channel_id, "video"),
            "buffer_shorts": buffer_target(channel_id, "short"),
            "buffer_videos": buffer_target(channel_id, "video"),
        }

    enriched = []
    for item in items:
        row = dict(item)
        video_path = Path(str(item.get("video_path") or ""))
        size_mb = None
        if video_path.exists():
            try:
                size_mb = round(video_path.stat().st_size / (1024 * 1024), 2)
            except OSError:
                size_mb = None
        row["file_size_mb"] = size_mb
        row["video_exists"] = bool(video_path.exists()) if item.get("video_path") else False
        enriched.append(row)

    return {
        "enabled": is_ready_queue_enabled(root),
        "today": today,
        "by_status": by_status,
        "packs": packs,
        "items": enriched,
        "active_count": sum(by_status.get(s, 0) for s in ACTIVE_STATUSES),
        "rendered_count": by_status.get("rendered", 0) + by_status.get("queued_upload", 0),
    }


def cleanup_old_packs(root: Path, *, keep_days: int = 2, timezone_name: str = "UTC") -> int:
    """Delete day_pack folders older than keep_days. Returns removed count."""
    base = root / "data" / "day_packs"
    if not base.exists():
        return 0
    now = _aware_now(timezone_name)
    removed = 0
    for channel_dir in base.iterdir():
        if not channel_dir.is_dir():
            continue
        for day_dir in channel_dir.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            age = (now.date() - day).days
            if age > keep_days:
                shutil.rmtree(day_dir, ignore_errors=True)
                removed += 1
    return removed


def find_latest_build_artifacts(
    *,
    output_root: Path,
    channel_id: str,
    content_kind: str,
    started_after: datetime | None = None,
) -> dict[str, Any] | None:
    """Locate newest short.mp4/video.mp4 under channel output after a build."""
    expected = "short.mp4" if content_kind == "short" else "video.mp4"
    base = output_root / channel_id / content_kind
    if not base.exists():
        return None
    best: tuple[float, Path] | None = None
    for video in base.rglob(expected):
        try:
            mtime = video.stat().st_mtime
        except OSError:
            continue
        if started_after is not None and mtime < started_after.timestamp() - 5:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, video)
    if not best:
        return None
    video_path = best[1]
    run_dir = video_path.parent
    meta = read_json(run_dir / "metadata.json", {})
    quality = read_json(run_dir / "quality_review.json", {})
    topic = read_json(run_dir / "topic.json", {})
    return {
        "run_dir": str(run_dir),
        "video_path": str(video_path),
        "title": str(meta.get("title") or topic.get("title") or run_dir.name),
        "quality_decision": str(quality.get("decision") or meta.get("quality_decision") or ""),
        "topic_fingerprint": str(topic.get("subject") or topic.get("title") or "")[:200],
        "metadata_path": str(run_dir / "metadata.json"),
    }


def dump_debug(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
