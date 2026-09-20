"""How many videos a channel owes today, and at what times.

The bot used to publish at the same fixed list of times every day and stop at
the daily minimum, so the analytics telling us when people actually watch went
unused and the upper half of each range was never reached.

Now each day draws a count from the channel's range and places those uploads
on the channel's best-performing hours. Two rules keep the result sane: an
hour needs more than one good video before it is trusted, and an unrated hour
is judged by the hours either side of it rather than discarded.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

# Nobody is watching, and a failed build at 03:00 has no daylight left to
# recover in.
QUIET_START_HOUR = 2
QUIET_END_HOUR = 6

# Shorts can sit closer together than long videos.
MIN_GAP_MINUTES = {"short": 60, "video": 120}

# An hour with a single lucky video should not outrank a steady performer, so
# its score is pulled toward the channel average until it has this many.
TRUST_THRESHOLD = 3


def draw_count(low: int, high: int, *, rng: random.Random | None = None) -> int:
    """A count inside the channel's range, redrawn each day."""
    low, high = int(low), int(high)
    if high < low:
        low, high = high, low
    return (rng or random).randint(max(0, low), max(0, high))


def is_quiet_hour(hour: int) -> bool:
    return QUIET_START_HOUR <= int(hour) < QUIET_END_HOUR


def score_hours(samples: dict[int, list[float]]) -> dict[int, float]:
    """Average each hour's results, pulling thin hours toward the overall mean.

    ``samples`` maps hour to that hour's per-video scores (views, retention --
    whatever the caller ranks by). An hour with one result is mostly the mean;
    an hour with several is mostly itself.
    """
    flat = [value for values in samples.values() for value in values]
    if not flat:
        return {}
    overall = sum(flat) / len(flat)
    scored: dict[int, float] = {}
    for hour, values in samples.items():
        if not values:
            continue
        own = sum(values) / len(values)
        count = len(values)
        # Shrink toward the overall mean in proportion to how little evidence
        # this hour has, so one result barely moves it.
        scored[int(hour)] = (own * count + overall * TRUST_THRESHOLD) / (count + TRUST_THRESHOLD)
    return scored


def fill_unrated(scored: dict[int, float]) -> dict[int, float]:
    """Give an hour with no data the average of its neighbours."""
    if not scored:
        return {}
    filled = dict(scored)
    for hour in range(24):
        if hour in filled:
            continue
        neighbours = [scored[h] for h in ((hour - 1) % 24, (hour + 1) % 24) if h in scored]
        if neighbours:
            filled[hour] = sum(neighbours) / len(neighbours)
    return filled


def best_hours(samples: dict[int, list[float]], *, limit: int = 12) -> list[int]:
    """The channel's strongest hours, best first, skipping the quiet window.

    Hours with enough videos behind them rank above thin ones outright. A
    single lucky video should not own an hour: shrinking its score is not
    enough on its own, because one big number also drags the mean it shrinks
    toward. Sorting by trust first is what actually keeps it down.
    """
    scored = score_hours(samples)
    ranked = fill_unrated(scored)
    usable = [
        (hour, value, len(samples.get(hour) or ()))
        for hour, value in ranked.items()
        if not is_quiet_hour(hour)
    ]
    usable.sort(key=lambda row: (-(row[2] >= TRUST_THRESHOLD), -row[1], row[0]))
    return [hour for hour, _, _ in usable[:limit]]


def plan_slots(
    count: int,
    hours: list[int],
    *,
    content_kind: str = "short",
    rng: random.Random | None = None,
) -> list[str]:
    """``count`` times as "HH:MM", on the best hours, correctly spaced.

    Walks the ranked hours and takes each one that is far enough from those
    already chosen. If the good hours run out before the count is met, it makes
    a second pass over every non-quiet hour so the day is still filled.
    """
    picker = rng or random
    gap = MIN_GAP_MINUTES.get(content_kind, 60)
    chosen: list[int] = []

    def fits(minute: int) -> bool:
        return all(abs(minute - other) >= gap for other in chosen)

    for hour in hours:
        if len(chosen) >= count:
            break
        # The caller may hand over every hour it has data for, so the quiet
        # window is enforced here too rather than trusted to the ranking.
        if is_quiet_hour(hour):
            continue
        minute = hour * 60 + picker.choice((0, 10, 20, 30))
        if fits(minute):
            chosen.append(minute)

    if len(chosen) < count:
        for hour in range(24):
            if len(chosen) >= count:
                break
            if is_quiet_hour(hour):
                continue
            minute = hour * 60
            if fits(minute):
                chosen.append(minute)

    chosen.sort()
    return [f"{minute // 60:02d}:{minute % 60:02d}" for minute in chosen]


def build_plan(
    *,
    low: int,
    high: int,
    hours: list[int],
    content_kind: str = "short",
    rng: random.Random | None = None,
) -> list[str]:
    """Draw today's count and place it. The whole daily decision in one call."""
    picker = rng or random
    return plan_slots(
        draw_count(low, high, rng=picker),
        hours,
        content_kind=content_kind,
        rng=picker,
    )


def plan_path(state_dir: Path, channel_id: str, day: str) -> Path:
    return Path(state_dir) / f"{channel_id}_upload_plan_{day}.json"


def save_plan(state_dir: Path, channel_id: str, day: str, plan: dict[str, list[str]]) -> Path:
    path = plan_path(state_dir, channel_id, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"day": day, "slots": plan}, indent=2), encoding="utf-8")
    return path


def saved_slots(state_dir: Path, channel_id: str, content_kind: str, day: str) -> list[str] | None:
    """Today's drawn times, or None when no plan was saved.

    The dashboard shows one dot per upload the day owes, so it has to read the
    plan that was actually drawn -- showing the configured times instead is how
    the Today page once claimed 7 Shorts on a day that planned 9.
    """
    path = plan_path(state_dir, channel_id, day)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    slots = (payload.get("slots") or {}).get(content_kind)
    return list(slots) if isinstance(slots, list) else None
