"""View models for the ops dashboard queue page.

Queue rows carry machine statuses (``held_quality``, ``queued_upload``) and raw
error text. The page is read by someone who wants two answers: what is stuck,
and what happens next. Doing that shaping here keeps the wording testable
without a browser, and keeps one definition of it for the page and the tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# One dot per upload the day owes; the name says what became of it. Ordered
# worst-to-best so a slot retried several times keeps its furthest state.
SLOT_STATES = ("failed", "held", "owed", "building", "ready", "scheduled", "published")

# Tab key, label, and the queue statuses that belong to it. Order is the order
# the tabs appear on the page.
TABS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("problems", "Problems", frozenset({"failed", "held_quality"})),
    ("building", "Being made", frozenset({"rendering"})),
    ("ready", "Ready to send", frozenset({"rendered", "queued_upload"})),
    ("planned", "Planned", frozenset({"planned", "scripted", "assets_ready"})),
    ("sent", "Sent today", frozenset({"uploaded"})),
)

# The scheduler stops re-queueing a channel/kind after this many failures in a
# day; past it the row is parked rather than silently retried forever.
MAX_DAILY_ATTEMPTS = 8

PLAIN_STATUS = {
    "planned": "Waiting to build",
    "scripted": "Script written",
    "assets_ready": "Pictures ready",
    "rendering": "Building now",
    "rendered": "Ready to upload",
    "queued_upload": "Uploading",
    "held_quality": "Held back by the quality check",
    "failed": "Failed",
    "uploaded": "Sent",
    "cancelled": "Skipped",
}

# Checked in order; first substring match wins.
_CAUSE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("caption", "script", "weak", "editorial"), "Could not write a script that passed the checks"),
    (("visual", "image", "usable", "footage"), "Not enough usable pictures"),
    (("quota", "403", "youtube", "upload"), "YouTube refused the upload"),
    (("voice", "tts", "audio", "narration"), "The voice track failed"),
    (("timeout", "connection", "network", "dns"), "The internet connection dropped"),
)


def plain_cause(error: str | None) -> str:
    """Turn a raw error string into one plain sentence a non-coder can act on."""
    text = str(error or "").strip().lower()
    if not text:
        return "Stopped without saying why"
    for needles, label in _CAUSE_RULES:
        if any(needle in text for needle in needles):
            return label
    return "Something else went wrong"


def retry_wait_minutes(attempts: int, *, cooldown_minutes: int = 15) -> int:
    """Mirror the scheduler's backoff so the page promises the real wait."""
    base = max(15, int(cooldown_minutes))
    cap = max(base, 30)
    return min(cap, base * (2 ** min(3, max(0, int(attempts) - 1))))


def next_step(item: dict[str, Any], *, cooldown_minutes: int = 15) -> str:
    """What happens to this row on its own, with no one pressing anything."""
    if str(item.get("status") or "") not in {"failed", "held_quality"}:
        return ""
    attempts = int(item.get("attempts") or 0)
    if attempts >= MAX_DAILY_ATTEMPTS:
        return f"Gave up for today after {attempts} tries"
    wait = retry_wait_minutes(attempts, cooldown_minutes=cooldown_minutes)
    tried = f" · tried {attempts}×" if attempts else ""
    return f"Retrying by itself in about {wait} min{tried}"


def decorate(item: dict[str, Any], *, cooldown_minutes: int = 15) -> dict[str, Any]:
    """Add the plain-language fields the page renders, leaving the row intact."""
    row = dict(item)
    status = str(item.get("status") or "")
    row["plain_status"] = PLAIN_STATUS.get(status, status or "Unknown")
    row["cause"] = plain_cause(item.get("error")) if status in {"failed", "held_quality"} else ""
    row["next_step"] = next_step(item, cooldown_minutes=cooldown_minutes)
    row["attempts"] = int(item.get("attempts") or 0)
    return row


def _sent_today(item: dict[str, Any], today: str) -> bool:
    if not today:
        return True
    return str(item.get("updated_at") or "").startswith(today)


def problem_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse problem rows under their plain cause, worst group first."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("cause") or "Something else went wrong"), []).append(row)
    groups = [{"cause": cause, "count": len(items), "items": items} for cause, items in buckets.items()]
    groups.sort(key=lambda g: (-g["count"], g["cause"]))
    return groups


def queue_tabs(queue: dict[str, Any], *, cooldown_minutes: int = 15) -> list[dict[str, Any]]:
    """Split a ready-queue snapshot into the page's tabs, each with its count."""
    items = [i for i in (queue.get("items") or []) if isinstance(i, dict)]
    today = str(queue.get("today") or "")
    tabs: list[dict[str, Any]] = []
    for key, label, statuses in TABS:
        rows = [i for i in items if str(i.get("status") or "") in statuses]
        if key == "sent":
            rows = [i for i in rows if _sent_today(i, today)]
        decorated = [decorate(i, cooldown_minutes=cooldown_minutes) for i in rows]
        tabs.append(
            {
                "key": key,
                "label": label,
                "count": len(decorated),
                "items": decorated,
                "groups": problem_groups(decorated) if key == "problems" else [],
            }
        )
    return tabs


def slot_state(item: dict[str, Any] | None, slot_minutes: int, now_minutes: int) -> str:
    """What has become of the video owed at this slot."""
    if not item:
        return "owed"
    status = str(item.get("status") or "")
    if status == "uploaded":
        return "published" if slot_minutes <= now_minutes else "scheduled"
    if status in {"rendered", "queued_upload"}:
        return "ready"
    if status in {"scripted", "assets_ready", "rendering"}:
        return "building"
    if status == "held_quality":
        return "held"
    if status == "failed":
        return "failed"
    return "owed"


def minutes_of(slot: str) -> int:
    """"HH:MM" as minutes past midnight, or -1 when it is not a time."""
    try:
        hour, minute = (int(part) for part in str(slot).split(":")[:2])
    except (TypeError, ValueError):
        return -1
    return hour * 60 + minute


def on_track(states: list[str], slots: list[str], now_minutes: int) -> bool:
    """True when nothing whose hour has passed is still unpublished."""
    for state, slot in zip(states, slots):
        if minutes_of(slot) < now_minutes and state in {"owed", "failed", "held"}:
            return False
    return True


def annotate_retries(
    items: list[dict[str, Any]],
    today: str,
    *,
    now: datetime | None = None,
    max_tries: int = MAX_DAILY_ATTEMPTS,
    cooldown_minutes: int = 20,
) -> dict[str, Any]:
    """Mark each problem row with what the bot will do about it next.

    "Retry all" looked broken: the rows went back to planning, failed again
    within minutes and reappeared, with nothing on the card saying so. Each
    problem now carries when it was last tried and whether the bot will try
    again by itself, and when, or has given up for the day.
    """
    moment = now or datetime.now()
    for item in items or ():
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") not in {"failed", "held_quality"}:
            continue
        item["cause"] = plain_cause(item.get("error"))
        ago = _minutes_since(item.get("updated_at"), moment)
        item["last_tried_minutes_ago"] = ago
        tries = int(item.get("attempts") or 0)
        if str(item.get("target_date") or "") != today:
            item["retry_state"] = "past_day"
        elif tries >= max_tries:
            item["retry_state"] = "gave_up"
        else:
            item["retry_state"] = "auto"
            item["next_try_in_minutes"] = max(0, cooldown_minutes - (ago or 0))
    return {"max_tries": max_tries, "cooldown_minutes": cooldown_minutes}


def _minutes_since(stamp: Any, moment: datetime) -> int | None:
    try:
        last = datetime.fromisoformat(str(stamp or ""))
    except (TypeError, ValueError):
        return None
    if last.tzinfo is not None:
        last = last.astimezone().replace(tzinfo=None)
    return max(0, int((moment - last).total_seconds() // 60))


def today_payload(
    queue: dict[str, Any],
    channel_names: dict[str, str],
    *,
    now_minutes: int,
    planned_slots: dict[tuple[str, str], list[str]] | None = None,
) -> dict[str, Any]:
    """One dot per upload the day owes, per channel and kind.

    The old page listed the times configured in settings, which is not what
    the day actually drew -- it once claimed 7 Shorts on a day that planned 9.
    This reads the drawn plan when the caller supplies it and falls back to the
    slots the queue itself holds.
    """
    today = str(queue.get("today") or "")
    items = [i for i in (queue.get("items") or []) if isinstance(i, dict)]
    channels: list[dict[str, Any]] = []

    for channel_id, display in channel_names.items():
        mine = [i for i in items if str(i.get("channel") or "") == channel_id
                and str(i.get("target_date") or "") == today]
        kinds: dict[str, Any] = {}
        for kind in ("short", "video"):
            by_slot: dict[str, dict[str, Any]] = {}
            for item in mine:
                if str(item.get("content_kind") or "short") != kind:
                    continue
                slot = str(item.get("target_slot") or "")
                current = by_slot.get(slot)
                # A slot retried during the day keeps its furthest state, so a
                # finished rebuild is not hidden behind its earlier failure.
                if current is None or SLOT_STATES.index(slot_state(item, 0, 0)) > SLOT_STATES.index(
                    slot_state(current, 0, 0)
                ):
                    by_slot[slot] = item

            planned = (planned_slots or {}).get((channel_id, kind))
            if planned is None:
                planned = sorted(slot for slot in by_slot if slot)

            slots = []
            for slot in planned:
                item = by_slot.get(slot)
                slots.append(
                    {
                        "time": slot,
                        "state": slot_state(item, minutes_of(slot), now_minutes),
                        "title": (item or {}).get("title"),
                        "item_id": (item or {}).get("id"),
                        "youtube_id": (item or {}).get("youtube_id"),
                    }
                )
            states = [s["state"] for s in slots]
            kinds[kind] = {
                "planned": len(slots),
                "published": states.count("published"),
                "scheduled": states.count("scheduled"),
                "ready": states.count("ready"),
                "building": states.count("building"),
                "owed": states.count("owed") + states.count("failed") + states.count("held"),
                "slots": slots,
            }

        every_state = [s["state"] for data in kinds.values() for s in data["slots"]]
        every_slot = [s["time"] for data in kinds.values() for s in data["slots"]]
        done = sum(data["published"] + data["scheduled"] for data in kinds.values())
        owed = sum(data["planned"] for data in kinds.values())
        upcoming = sorted(
            (
                {**slot, "kind": kind}
                for kind, data in kinds.items()
                for slot in data["slots"]
                if slot["state"] != "published" and minutes_of(slot["time"]) >= now_minutes
            ),
            key=lambda slot: minutes_of(slot["time"]),
        )[:3]

        channels.append(
            {
                "id": channel_id,
                "name": display,
                "kinds": kinds,
                "done": done,
                "planned": owed,
                "on_track": on_track(every_state, every_slot, now_minutes),
                "next": upcoming,
            }
        )

    return {"ok": True, "day": today, "channels": channels}
