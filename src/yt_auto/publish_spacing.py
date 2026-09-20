"""Keep a channel's videos apart on the clock.

Two videos went live within 24 seconds of each other, and several more 5-10
minutes apart. Videos reach YouTube by more than one path -- the slot
publisher, the overdue publisher, a retry, a manual push -- so no single
caller can know what else is already scheduled. The only reliable moment to
check is immediately before an upload, against what YouTube itself reports.

The rule is deliberately one-directional: a video may be pushed later, never
earlier. Moving one earlier could collide with something already public, and
publishing sooner than planned is not a thing anyone asked for.

Nothing here touches the network; the caller supplies the times it already
knows about, which is what makes the rule testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# YouTube's own listing is the source of truth, and it is only accurate to the
# minute, so the gap is expressed in whole minutes.
DEFAULT_MIN_GAP_MINUTES = 30


def _as_minutes(moment: datetime) -> int:
    return int(moment.timestamp() // 60)


def is_clear(desired: datetime, taken: list[datetime], *, min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES) -> bool:
    """True when ``desired`` is at least the gap away from everything in ``taken``."""
    gap = max(1, int(min_gap_minutes))
    target = _as_minutes(desired)
    return all(abs(target - _as_minutes(other)) >= gap for other in taken)


def next_free_slot(
    desired: datetime,
    taken: list[datetime],
    *,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
    max_shift_hours: int = 12,
) -> datetime:
    """The first time at or after ``desired`` that clears every taken slot.

    Steps forward in whole gaps rather than minute by minute: the aim is a
    readable schedule, not the tightest possible packing. Gives up after
    ``max_shift_hours`` and returns the last time it tried, so a badly
    congested day delays an upload instead of dropping it.
    """
    gap = max(1, int(min_gap_minutes))
    candidate = desired.replace(second=0, microsecond=0)
    limit = candidate + timedelta(hours=max(1, int(max_shift_hours)))
    while candidate <= limit:
        if is_clear(candidate, taken, min_gap_minutes=gap):
            return candidate
        candidate = candidate + timedelta(minutes=gap)
    return candidate


def space_out(
    desired_times: list[datetime],
    *,
    already_taken: list[datetime] | None = None,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
) -> list[datetime]:
    """Push a whole list apart, earliest first, keeping the original order."""
    taken = list(already_taken or [])
    out: list[datetime] = []
    for moment in sorted(desired_times):
        placed = next_free_slot(moment, taken, min_gap_minutes=min_gap_minutes)
        out.append(placed)
        taken.append(placed)
    return out


def minutes_until_clear(
    desired: datetime,
    taken: list[datetime],
    *,
    min_gap_minutes: int = DEFAULT_MIN_GAP_MINUTES,
) -> int:
    """How far this upload has to move, in minutes. 0 when it can go as planned."""
    placed = next_free_slot(desired, taken, min_gap_minutes=min_gap_minutes)
    return max(0, _as_minutes(placed) - _as_minutes(desired.replace(second=0, microsecond=0)))
