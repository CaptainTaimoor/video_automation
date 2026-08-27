"""Lightweight, fail-closed quality controls for the free-only v2 pipeline.

The legacy pipeline remains available.  This package deliberately keeps its
state and validation independent so a bad external API response cannot make a
video silently fall back to boilerplate or overwrite a completed artifact.
"""

from .editorial import Claim, EditorialGate, EditorialReport, StoryBrief
from .state import QualityStateStore, RetryClass, classify_failure

__all__ = [
    "Claim",
    "EditorialGate",
    "EditorialReport",
    "QualityStateStore",
    "RetryClass",
    "StoryBrief",
    "classify_failure",
]
