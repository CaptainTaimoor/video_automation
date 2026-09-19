"""Compose a Brain Lens Short, then repair it against the gate's own rules.

The writer used to pick lines by one set of rules and the gate judged them by
another, so a Short could be composed and immediately rejected. This module
composes against :mod:`yt_auto.pipeline_quality` -- the same check that decides
whether the build survives -- and repairs in a loop until the script passes or
the attempts run out.
"""

from __future__ import annotations

import random

from . import brain_hooks, pipeline_quality

MAX_REPAIR_ROUNDS = 6


def pick_opener(subject: str, *, rng: random.Random | None = None) -> str:
    """Subject-keyed opener when one matches, otherwise one from the pool.

    The result is folded into a single sentence, because a Short is split into
    beats on sentence boundaries and an opener whose cue sits in a second
    sentence loses that cue from the first beat.
    """
    picker = rng or random
    lowered = (subject or "").lower()
    for terms, opener in brain_hooks.TOPIC_OPENERS:
        if any(term in lowered for term in terms):
            return brain_hooks.joined_opener(opener)
    usable = [o for o in brain_hooks.OPENERS if not brain_hooks.opener_issues(o)]
    chosen = picker.choice(usable or list(brain_hooks.OPENERS))
    return brain_hooks.joined_opener(chosen)


def pick_fact(*, rng: random.Random | None = None, exclude: list[str] | None = None) -> str:
    """A why-beat, preferring one not already in the script so beats don't repeat."""
    used = set(exclude or ())
    pool = [f for f in brain_hooks.RELATIONSHIP_FACTS if f not in used]
    return (rng or random).choice(pool or list(brain_hooks.RELATIONSHIP_FACTS))


def pick_closer(*, rng: random.Random | None = None) -> str:
    return (rng or random).choice(list(brain_hooks.CLOSERS))


def compose(subject: str, *, rng: random.Random | None = None) -> list[str]:
    """Build a hook / why / takeaway Short and repair it until the gate passes."""
    picker = rng or random
    lines = [pick_opener(subject, rng=picker), pick_fact(rng=picker), pick_closer(rng=picker)]

    for _ in range(MAX_REPAIR_ROUNDS):
        issues = pipeline_quality.brain_lens_issues(lines)
        if not issues:
            return lines
        lines = repair(lines, issues, subject, rng=picker)
    return lines


def repair(
    lines: list[str],
    issues: list[str],
    subject: str,
    *,
    rng: random.Random | None = None,
) -> list[str]:
    """Replace whichever beat the gate complained about, leaving the rest alone."""
    picker = rng or random
    out = list(lines)
    blob = " ".join(issues)
    if "opener" in blob and out:
        out[0] = pick_opener(subject, rng=picker)
    if "midpoint" in blob and len(out) >= 2:
        out[1] = pick_fact(rng=picker, exclude=out)
    if "too thin" in blob:
        # One more explanation beat is the cheapest way to add real substance.
        out.insert(max(1, len(out) - 1), pick_fact(rng=picker, exclude=out))
    if "runs long" in blob and len(out) > 3:
        del out[-2]
    return out
