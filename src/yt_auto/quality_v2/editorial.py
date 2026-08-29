from __future__ import annotations

"""Evidence and originality gates used before a script reaches media work.

This intentionally uses only deterministic, cheap checks.  A free LLM may
write or critique, but it never gets to waive the checks below.
"""

from dataclasses import dataclass, field
import re
from typing import Iterable, Sequence


_WORD_RE = re.compile(r"[a-z0-9']+", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_GENERIC_OPENERS = (
    "did you know",
    "have you ever",
    "the real story",
    "the true story",
    "to understand",
    "there is a fascinating reason",
    "historians keep coming back",
    "modern psychology",
)


def normalize_text(text: str) -> str:
    return " ".join(_WORD_RE.findall(str(text or "").lower()))


def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    return [item.strip() for item in _SENTENCE_RE.split(cleaned) if len(_WORD_RE.findall(item)) >= 3]


def fivegrams(text: str) -> set[tuple[str, ...]]:
    words = normalize_text(text).split()
    return {tuple(words[index:index + 5]) for index in range(max(0, len(words) - 4))}


def token_similarity(left: str, right: str) -> float:
    left_words = set(normalize_text(left).split())
    right_words = set(normalize_text(right).split())
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    source_ids: tuple[str, ...] = ()
    status: str = "approved"
    locator: str = ""
    confidence: float = 1.0

    @property
    def approved(self) -> bool:
        return self.status == "approved" and bool(self.source_ids) and bool(self.text.strip())


@dataclass(frozen=True)
class StoryBrief:
    question: str
    hook: str
    payoff: str
    claim_ids: tuple[str, ...]
    forbidden_claims: tuple[str, ...] = ()
    visual_promises: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditorialReport:
    approved: bool
    issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    repeated_sentences: tuple[str, ...] = ()
    max_sentence_similarity: float = 0.0
    fivegram_overlap: float = 0.0


@dataclass
class CorpusMemory:
    """Small, serializable memory of published narration/title material."""

    titles: list[str] = field(default_factory=list)
    narrations: list[str] = field(default_factory=list)

    def add(self, title: str, narration: str, limit: int = 500) -> None:
        if title.strip():
            self.titles.append(title.strip())
        if narration.strip():
            self.narrations.append(narration.strip())
        self.titles[:] = self.titles[-limit:]
        self.narrations[:] = self.narrations[-limit:]


class EditorialGate:
    """Fail-closed quality gate for story scripts.

    Thresholds are deliberately conservative until a labelled golden set is
    collected.  Consumers may treat warnings as holds during an early rollout.
    """

    def __init__(
        self,
        max_fivegram_overlap: float = 0.02,
        max_sentence_similarity: float = 0.88,
        max_title_similarity: float = 0.9,
    ) -> None:
        self.max_fivegram_overlap = max_fivegram_overlap
        self.max_sentence_similarity = max_sentence_similarity
        self.max_title_similarity = max_title_similarity

    @staticmethod
    def claim_issues(claims: Iterable[Claim], required_claim_ids: Iterable[str] = ()) -> list[str]:
        by_id = {claim.claim_id: claim for claim in claims}
        issues = []
        for claim_id in required_claim_ids:
            claim = by_id.get(claim_id)
            if claim is None:
                issues.append(f"missing approved claim: {claim_id}")
            elif not claim.approved:
                issues.append(f"unsupported claim: {claim_id}")
        return issues

    def review(
        self,
        title: str,
        narration: str,
        corpus: CorpusMemory | None = None,
        claims: Sequence[Claim] = (),
        brief: StoryBrief | None = None,
        content_kind: str = "short",
        history_required: bool = False,
    ) -> EditorialReport:
        issues: list[str] = []
        warnings: list[str] = []
        clean_title = normalize_text(title)
        clean_narration = normalize_text(narration)
        words = clean_narration.split()
        minimum = 65 if content_kind == "short" else 1050
        maximum = 180 if content_kind == "short" else 1200
        if not title.strip():
            issues.append("missing title")
        if len(words) < minimum or len(words) > maximum:
            issues.append(f"word count outside {minimum}-{maximum}")
        opening = normalize_text(split_sentences(narration)[0] if split_sentences(narration) else narration)
        if any(opening.startswith(phrase) for phrase in _GENERIC_OPENERS):
            issues.append("generic opener")
        if history_required:
            required = brief.claim_ids if brief else ()
            issues.extend(self.claim_issues(claims, required))
            if not claims:
                issues.append("history script has no claim ledger")
        if brief:
            payoff = normalize_text(brief.payoff)
            if payoff and payoff not in clean_narration:
                warnings.append("promised payoff wording was not found in narration")

        repeated: list[str] = []
        max_similarity = 0.0
        overlap = 0.0
        if corpus:
            for old_title in corpus.titles:
                if token_similarity(title, old_title) >= self.max_title_similarity:
                    issues.append("title too similar to published history")
                    break
            candidate_sentences = split_sentences(narration)
            candidate_grams = fivegrams(narration)
            all_old_grams: set[tuple[str, ...]] = set()
            for old_narration in corpus.narrations:
                all_old_grams |= fivegrams(old_narration)
                for sentence in candidate_sentences:
                    exact = normalize_text(sentence)
                    for old_sentence in split_sentences(old_narration):
                        if exact and exact == normalize_text(old_sentence):
                            repeated.append(sentence)
                            break
                        max_similarity = max(max_similarity, token_similarity(sentence, old_sentence))
            if candidate_grams:
                overlap = len(candidate_grams & all_old_grams) / len(candidate_grams)
            if repeated:
                issues.append("exact sentence reused from published history")
            if overlap > self.max_fivegram_overlap:
                issues.append("five-gram overlap exceeds corpus limit")
            if max_similarity > self.max_sentence_similarity:
                warnings.append("near-duplicate sentence detected")
        return EditorialReport(
            approved=not issues,
            issues=tuple(dict.fromkeys(issues)),
            warnings=tuple(dict.fromkeys(warnings)),
            repeated_sentences=tuple(dict.fromkeys(repeated)),
            max_sentence_similarity=round(max_similarity, 4),
            fivegram_overlap=round(overlap, 4),
        )
