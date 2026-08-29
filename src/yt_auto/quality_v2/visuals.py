from __future__ import annotations

"""Rights, reuse and coverage checks for resilient historical visual plans."""

from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from typing import Iterable


class RightsStatus(str, Enum):
    ALLOWED = "allowed"
    REVIEW = "review"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    source_url: str
    rights: str
    license_url: str = ""
    creator: str = ""
    checksum: str = ""
    perceptual_hash: str = ""
    category: str = "archive"


@dataclass(frozen=True)
class ReuseDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class SceneVisualPlan:
    beat_id: str
    primary_asset_id: str
    fallback_kind: str
    duration_seconds: float
    visual_purpose: str = ""


_ALLOWED = (
    "cc0",
    "public domain",
    "public-domain",
    "pdm",
    "cc by",
    "cc-by",
    "pexels license",
    "pixabay license",
)
_REJECTED_PHRASES = (
    "noncommercial",
    "no derivatives",
    "all rights",
    "copyrighted",
    "unknown",
)
_REJECTED_CC_COMPONENT = re.compile(r"(?<![a-z0-9])(?:nc|nd)(?![a-z0-9])")


def validate_rights(record: AssetRecord) -> RightsStatus:
    value = " ".join((record.rights, record.license_url)).lower()
    if not record.source_url.strip() or not record.rights.strip():
        return RightsStatus.REJECTED
    if any(marker in value for marker in _REJECTED_PHRASES):
        return RightsStatus.REJECTED
    # NC and ND are meaningful only as complete license components. Raw
    # substring matching incorrectly rejected ordinary source URLs containing
    # words such as "lunch", "window", or "man-and-woman".
    if _REJECTED_CC_COMPONENT.search(value):
        return RightsStatus.REJECTED
    if "by-sa" in value or "cc by sa" in value:
        return RightsStatus.REVIEW
    return RightsStatus.ALLOWED if any(marker in value for marker in _ALLOWED) else RightsStatus.REJECTED


def canonical_asset_id(record: AssetRecord) -> str:
    if record.asset_id.strip():
        return record.asset_id.strip()
    return hashlib.sha256(record.source_url.strip().encode("utf-8")).hexdigest()[:24]


class VisualHistory:
    """Cross-episode memory for exact and perceptual asset reuse."""

    def __init__(self, records: Iterable[AssetRecord] = ()) -> None:
        self._ids: set[str] = set()
        self._checksums: set[str] = set()
        self._hashes: list[str] = []
        for record in records:
            self.add(record)

    def add(self, record: AssetRecord) -> None:
        self._ids.add(canonical_asset_id(record))
        if record.checksum:
            self._checksums.add(record.checksum.lower())
        if record.perceptual_hash:
            self._hashes.append(record.perceptual_hash.lower())

    @staticmethod
    def _distance(left: str, right: str) -> int | None:
        try:
            if len(left) != len(right):
                return None
            return (int(left, 16) ^ int(right, 16)).bit_count()
        except ValueError:
            return None

    def evaluate(self, record: AssetRecord, episode_asset_ids: set[str] | None = None) -> ReuseDecision:
        asset_id = canonical_asset_id(record)
        if asset_id in (episode_asset_ids or set()):
            return ReuseDecision(False, "duplicate within episode")
        if asset_id in self._ids:
            return ReuseDecision(False, "asset already used in published history")
        if record.checksum and record.checksum.lower() in self._checksums:
            return ReuseDecision(False, "identical asset checksum in published history")
        if record.perceptual_hash:
            for old in self._hashes:
                distance = self._distance(record.perceptual_hash.lower(), old)
                if distance is not None and distance <= 6:
                    return ReuseDecision(False, "near-duplicate visual in published history")
        return ReuseDecision(True)


def deterministic_fallback_visual(subject: str, beat_text: str) -> str:
    """Return a renderable local visual style when online media cannot qualify."""
    text = f"{subject} {beat_text}".lower()
    if any(word in text for word in ("battle", "empire", "route", "city", "island", "river", "march")):
        return "animated_map"
    if any(word in text for word in ("year", "century", "before", "after", "timeline", "dynasty")):
        return "timeline_card"
    if any(word in text for word in ("letter", "tablet", "inscription", "document", "archive")):
        return "document_highlight"
    return "evidence_card"


def validate_scene_coverage(plans: Iterable[SceneVisualPlan]) -> list[str]:
    issues: list[str] = []
    for plan in plans:
        if not plan.beat_id.strip():
            issues.append("scene has no beat id")
        if plan.duration_seconds <= 0:
            issues.append(f"scene {plan.beat_id or '?'} has invalid duration")
        if not plan.primary_asset_id.strip():
            issues.append(f"scene {plan.beat_id or '?'} has no primary visual")
        if not plan.fallback_kind.strip():
            issues.append(f"scene {plan.beat_id or '?'} has no fallback visual")
    return list(dict.fromkeys(issues))
