"""View models for the ops dashboard queue page.

Queue rows carry machine statuses (``held_quality``, ``queued_upload``) and raw
error text. The page is read by someone who wants two answers: what is stuck,
and what happens next. Doing that shaping here keeps the wording testable
without a browser, and keeps one definition of it for the page and the tests.
"""

from __future__ import annotations

from typing import Any

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
