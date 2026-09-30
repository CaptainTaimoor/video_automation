"""The quality gate, expressed once.

The gate and the composer used to apply their own copies of the Brain Lens
rules, which let a line pass the writer and then fail the check that decided
whether a build survived. Both now call into this module, which in turn reads
its Brain Lens rules from :mod:`yt_auto.brain_hooks`.

Nothing here touches disk or the network, so the gate can be tested directly.
"""

from __future__ import annotations

import re

from . import brain_hooks

# A Short's middle has to turn: contrast it, pay it off, or reframe it.
MIDPOINT_CUES: tuple[str, ...] = (
    " but ", " because ", " instead ", " so ", " yet ",
    " while ", " then ", " which ", " prove", " trace",
    " confirm", " reveal",
)

# Ancient History wants a surviving, physical clue rather than a vibe.
#
# The list was nine words long and matched literally, so it refused hooks
# that are exactly what it asks for. "French soldiers found the slab near
# Rashid in 1799, and scholars used repeated royal names in cartouches as
# reading keys" failed a Rosetta Stone Short: it names a dated discovery, a
# physical object and the method that read it, and contains neither "stone"
# nor "date". The words below are the same idea, spelled the ways writers
# actually spell it.
EVIDENCE_CUES: tuple[str, ...] = (
    "archaeolog", "artifact", "artefact", "date", "door", "inscription",
    "ruin", "stone", "tomb", "wall",
    # objects that survive
    "slab", "stela", "stelae", "tablet", "papyrus", "scroll", "manuscript",
    "coin", "pottery", "potsherd", "shard", "skeleton", "bones", "skull",
    "statue", "relief", "carving", "carved", "mural", "fresco", "mosaic",
    "painting", "pillar", "column", "altar", "sarcophag", "mummy", "cartouche",
    "hieroglyph", "cuneiform", "glyph", "seal", "amulet", "weapon", "sword",
    "helmet", "shield", "armor", "armour", "jewel", "gold", "bronze", "iron",
    "clay", "brick", "mortar", "pyramid", "temple", "palace", "fortress",
    "cave", "grave", "burial", "cemetery", "canal", "aqueduct", "road",
    "ship", "wreck", "map",
    # how we know
    "excavat", "unearth", "discover", "found ", "dig ", "survey",
    "radiocarbon", "carbon dating", "dated", "record", "chronicle",
    "letter", "decree", "treaty", "census", "ledger",
)

# A year is a concrete clue however it is written: "in 1799", "around 2500
# BCE", "the 4th century BC".
_YEAR = re.compile(
    r"\b(?:\d{3,4}\s*(?:bce|bc|ce|ad)?|\d{1,2}(?:st|nd|rd|th)\s+century)\b",
    re.IGNORECASE,
)


def has_concrete_clue(text: str) -> bool:
    """Whether a line points at something that physically survives or is dated."""
    lowered = f" {str(text or '').lower()} "
    return any(cue in lowered for cue in EVIDENCE_CUES) or bool(_YEAR.search(lowered))

# Spoken words per second, used to sanity-check a Short's length.
WORDS_PER_SECOND = 2.6
MIN_SHORT_WORDS = 45
MAX_SHORT_WORDS = 160


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", (text or "").lower()) if w]


def has_midpoint_turn(lines: list[str]) -> bool:
    """True when the body between hook and payoff actually turns."""
    if len(lines) < 3:
        return False
    middle = " ".join(lines[1:-1]).lower()
    return any(cue in f" {middle} " for cue in MIDPOINT_CUES)


def short_length_issues(lines: list[str]) -> list[str]:
    """Length and density, judged on the spoken word count."""
    issues: list[str] = []
    count = len(_words(" ".join(lines)))
    if count < MIN_SHORT_WORDS:
        issues.append(f"short is too thin ({count} words, needs {MIN_SHORT_WORDS})")
    elif count > MAX_SHORT_WORDS:
        issues.append(f"short runs long ({count} words, allow {MAX_SHORT_WORDS})")
    return issues


def estimated_seconds(lines: list[str]) -> float:
    """Rough spoken length, for slotting a Short against its target runtime."""
    return round(len(_words(" ".join(lines))) / WORDS_PER_SECOND, 1)


def brain_lens_issues(lines: list[str]) -> list[str]:
    """Every Brain Lens problem with this script, in plain words.

    The opener is judged by :func:`brain_hooks.opener_issues` -- the same call
    the composer repairs against -- so the two cannot disagree about whether a
    line is acceptable.
    """
    if not lines:
        return ["script is empty"]
    issues = list(brain_hooks.opener_issues(lines[0]))
    issues.extend(short_length_issues(lines))
    if not has_midpoint_turn(lines):
        issues.append("short lacks a midpoint contrast, consequence, or reframe")
    return list(dict.fromkeys(issues))


def ancient_history_issues(lines: list[str], subject_tokens: set[str]) -> list[str]:
    """Ancient History wants a concrete clue up front and the subject at the end."""
    if not lines:
        return ["script is empty"]
    issues: list[str] = []
    opener = lines[0]
    opener_tokens = set(re.findall(r"[a-z]{4,}", opener.lower()))
    if not (subject_tokens & opener_tokens) and not has_concrete_clue(opener):
        issues.append("Ancient History hook lacks its subject or a concrete surviving clue")
    payoff_tokens = set(re.findall(r"[a-z]{4,}", " ".join(lines[-2:]).lower()))
    if not (subject_tokens & payoff_tokens):
        issues.append("Ancient History payoff does not return to the core subject")
    if not has_midpoint_turn(lines):
        issues.append("short lacks a midpoint contrast, consequence, or reframe")
    return list(dict.fromkeys(issues))
