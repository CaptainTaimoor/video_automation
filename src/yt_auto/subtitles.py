from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str


class SubtitleComposer:
    DEFAULT_MAX_CPS = 17.0
    _VISIBLE_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+(?:['\u2019-][A-Za-z0-9]+)*")

    _AVOID_LINE_EDGE_WORDS = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "with",
        "without",
        "about",
    }
    # These words can be valid inside a caption, but without sentence-ending
    # punctuation they almost always introduce a complement that belongs in the
    # same cue ("what new behavior", "give access", "your worth"). Keeping
    # this separate from _AVOID_LINE_EDGE_WORDS avoids penalising natural cue
    # starts such as "Your values matter" while still rejecting stranded tails.
    _DANGLING_CAPTION_TAILS = {
        "after",
        "any",
        "be",
        "because",
        "before",
        "become",
        "becoming",
        "calm",
        "can",
        "compare",
        "could",
        "defend",
        "defending",
        "did",
        "do",
        "does",
        "during",
        "each",
        "every",
        "give",
        "giving",
        "how",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "if",
        "keep",
        "keeping",
        "larger",
        "less",
        "may",
        "might",
        "more",
        "my",
        "must",
        "new",
        "no",
        "not",
        "one",
        "our",
        "other",
        "painful",
        "proving",
        "replace",
        "reflect",
        "reflecting",
        "specific",
        "some",
        "first",
        "issue",
        "it",
        "note",
        "she",
        "that",
        "than",
        "their",
        "they",
        "these",
        "this",
        "those",
        "treat",
        "treating",
        "vague",
        "what",
        "when",
        "which",
        "while",
        "who",
        "whose",
        "will",
        "would",
        "we",
        "is",
        "are",
        "was",
        "were",
        "you",
        "your",
        # Possessives, modifiers, and transitive/phrasal-verb heads observed in
        # production narration. Without the following word these create visible
        # semantic stutters ("while its | silences", "warm | message",
        # "slows | down"). Sentence-final punctuation still exempts a genuinely
        # complete use such as "time slows." in quality_issues().
        "african",
        "connect",
        "connected",
        "connects",
        "data",
        "enclosed",
        "encloses",
        "imported",
        "its",
        "local",
        "missing",
        "political",
        "repeated",
        "royal",
        "slow",
        "slowed",
        "slows",
        "soapstone",
        "start",
        "started",
        "starts",
        "support",
        "supported",
        "supports",
        "warm",
    }
    _MIDPHRASE_START_WORDS = {
        "across",
        "after",
        "although",
        "and",
        "as",
        "are",
        "at",
        "be",
        "been",
        "being",
        "before",
        "because",
        "between",
        "but",
        "by",
        "can",
        "connect",
        "connected",
        "connects",
        "could",
        "did",
        "do",
        "does",
        "down",
        "during",
        "for",
        "from",
        "had",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "may",
        "might",
        "must",
        "of",
        "on",
        "onto",
        "or",
        "over",
        "reveal",
        "revealed",
        "reveals",
        "should",
        "support",
        "supported",
        "supports",
        "than",
        "that",
        "though",
        "through",
        "to",
        "toward",
        "towards",
        "under",
        "was",
        "were",
        "when",
        "where",
        "which",
        "while",
        "who",
        "will",
        "with",
        "without",
        "would",
    }
    _PROTECTED_PHRASES = (
        "Qin Shi Huang",
        "Terracotta Army",
        "Sea Peoples",
        "Bronze Age",
        "Roman Empire",
        "Byzantine Empire",
        "Library of Alexandria",
        "Oracle of Delphi",
        "Dead Sea Scrolls",
        "Code of Hammurabi",
        "Battle of Thermopylae",
        "Battle of Marathon",
        "Persian invasion",
        "Persian Empire",
        "Achaemenid Persian Empire",
        "King Darius I",
        "Greco-Persian Wars",
        "Great Zimbabwe",
        "Kingdom of Great Zimbabwe",
        "Teutoburg Forest",
        "Antikythera Mechanism",
        "Byzantine Greek Fire",
        "Greek Fire",
        "Carthage",
        "Carthaginian",
        "Hannibal",
        "Nazca Lines",
        "Nasca Lines",
        "Nazca Desert",
        "Nabataean",
        "Petra",
        "Amazigh",
        "Berber",
        "emotional regulation",
        "anxious attachment",
        "avoidant attachment",
        "impostor syndrome",
        "cognitive dissonance",
        "confirmation bias",
        "anchoring effect",
        "emotional regulation",
        "mixed signals",
        "Machu Picchu",
        "Eastern Cordillera",
        "red flags",
        "green flags",
        "body language",
        "texting anxiety",
        "situationship",
        "situationships",
        "first kiss",
        "kissing chemistry",
        "love bombing",
        "trauma bond",
        "secure attachment",
        "fearful avoidant",
        "dismissive avoidant",
        "dopamine loop",
        "nervous system",
        "eye contact",
        "social threat",
        "romantic attraction",
        "emotional connection",
        "the whole conversation",
        "longer eye contact",
        "softer timing",
        "warmer voice",
        "returned effort",
        "one small signal",
        "create another reason",
        "elite tombs",
        "engineering power",
        "false doors",
        "Obelisk of Axum",
        "Axum Obelisks",
        "surviving structures",
        "Nubian Pyramids",
        "Nile Valley",
        "Twenty-Fifth Dynasty",
        "power, belief, and labor",
        "Indian Ocean trade",
        "soapstone birds",
        "political power",
        "cattle wealth",
        "African capital",
        "local wealth",
        "slows down",
        "warm message",
        "data point",
        "repeated pattern",
        "royal image",
        "its construction",
        "its burning",
        "its silences",
        "starts measuring",
        "missing information",
    )

    _ABBREVIATIONS = (
        "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "St.", "vs.", "e.g.", "i.e.",
        "B.C.", "B.C.E.", "A.D.", "C.E.", "U.S.", "U.K.",
    )

    def __init__(
        self,
        max_words_per_caption: int = 7,
        max_chars_per_second: float = DEFAULT_MAX_CPS,
    ) -> None:
        self.max_words = max(2, max_words_per_caption)
        self.max_cps = max(10.0, float(max_chars_per_second))

    def _normalize(self, text: str) -> str:
        normalized = re.sub(r"\s*[\u2013\u2014]\s*", ", ", text or "")
        normalized = re.sub(r",\s*,+", ", ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def visible_word_count(cls, text: str) -> int:
        """Count reader-visible words, treating hyphenated compounds as one."""
        return len(cls._VISIBLE_WORD_PATTERN.findall(text or ""))

    def hard_word_limit(self) -> int:
        """Return the actual upload-gate ceiling used by the chunk optimizer.

        Short-form composition may target four words while allowing up to six
        when a protected name or grammatical boundary needs the extra room.
        Longer configured limits remain authoritative and never exceed eight.
        Keeping this calculation centralized prevents preflight from accepting a
        cue that the completed-render QA would later hold.
        """
        return max(4, min(8, max(6, self.max_words)))

    def _protect_phrases(self, text: str) -> str:
        protected = text
        for phrase in sorted(self._PROTECTED_PHRASES, key=len, reverse=True):
            pattern = r"\b" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"\b"
            protected = re.sub(
                pattern,
                lambda match: match.group(0).replace(" ", "\u00A0"),
                protected,
                flags=re.IGNORECASE,
            )
        protected = re.sub(
            r"\b\d{1,4}\s+(?:BC|BCE|AD|CE)\b",
            lambda match: match.group(0).replace(" ", "\u00A0"),
            protected,
            flags=re.IGNORECASE,
        )
        protected = re.sub(
            r"\b\d{1,2}(?:st|nd|rd|th)\s+centur(?:y|ies)\b",
            lambda match: match.group(0).replace(" ", "\u00A0"),
            protected,
            flags=re.IGNORECASE,
        )
        # Keep a primary/parenthetical measurement and its qualifier together:
        # "24-metre (79 ft) tall" must never become "24-metre (79" / "ft) tall".
        measurement_patterns = (
            r"\b\d+(?:[.,]\d+)?[- ]?(?:metres?|meters?|kilometres?|kilometers?|centimetres?|centimeters?|feet|foot|ft|inches?|in)\s*"
            r"\(\s*\d+(?:[.,]\d+)?\s*(?:metres?|meters?|feet|foot|ft|inches?|in)\s*\)"
            r"(?:\s+(?:tall|long|wide|high|deep))?",
            r"\b\d+(?:[.,]\d+)?\s*(?:tonnes?|tons?|kilograms?|kg|pounds?|lb|miles?|kilometres?|kilometers?)\b",
            r"\b(?:about|around|approximately|nearly|more than|less than)\s+\d+(?:[.,]\d+)?\s*"
            r"(?:tonnes?|tons?|kilograms?|kg|pounds?|lb|metres?|meters?|feet|ft|miles?|kilometres?|kilometers?)\b",
        )
        for pattern in measurement_patterns:
            protected = re.sub(
                pattern,
                lambda match: re.sub(r"\s+", "\u00A0", match.group(0)),
                protected,
                flags=re.IGNORECASE,
            )

        # Protect previously unseen multi-word proper names too. This covers names
        # supplied by research without requiring a hand-maintained global list.
        proper_name_pattern = (
            r"\b(?:[A-Z][A-Za-z'’-]+)(?:\s+(?:(?:of|the|and|de|van|von)\s+)?"
            r"[A-Z][A-Za-z'’-]+){1,4}\b"
        )
        protected = re.sub(
            proper_name_pattern,
            lambda match: match.group(0).replace(" ", "\u00A0"),
            protected,
        )
        return protected

    def _unprotect_phrases(self, text: str) -> str:
        return text.replace("\u00A0", " ")

    def _sentence_beats(self, text: str) -> List[str]:
        normalized = self._normalize(text)
        if not normalized:
            return []
        sentinel = "\u2024"
        protected = normalized
        for abbreviation in sorted(self._ABBREVIATIONS, key=len, reverse=True):
            protected = re.sub(
                rf"(?<![A-Za-z]){re.escape(abbreviation)}(?![A-Za-z])",
                lambda match: match.group(0).replace(".", sentinel),
                protected,
                flags=re.IGNORECASE,
            )
        parts = [
            part.replace(sentinel, ".").strip()
            for part in re.split(r"(?<=[.!?])\s+", protected)
            if part.strip()
        ]
        return parts or [normalized]

    def _chunk_text(self, text: str, max_words: int | None = None) -> List[str]:
        words = self._protect_phrases(self._normalize(text)).split(" ")
        if not words:
            return []
        # Production QA uses a six-word ceiling.  The dynamic program previously
        # added two extra words even at that ceiling, so a perfectly timed cue could
        # still be held after the full render.  Keep six words available when a
        # smaller composition hint is supplied (needed for names/connectors), but
        # never expand the configured production limit beyond its actual value.
        word_limit = (
            max(4, min(8, max(6, max_words)))
            if max_words is not None
            else self.hard_word_limit()
        )
        bad_endings = self._AVOID_LINE_EDGE_WORDS | self._DANGLING_CAPTION_TAILS | {
            "because", "became", "during", "first", "how", "larger", "less",
            "more", "turning", "under", "without", "was", "were", "is", "are",
            "another", "whole", "longer", "shorter", "softer", "warmer", "tiny",
            "small", "surviving", "elite", "false", "returned", "chosen", "direct",
            "linked", "show", "shows", "reveal", "reveals",
        }
        bad_starts = self._MIDPHRASE_START_WORDS | {
            "am", "and", "are", "be", "been", "being", "can", "could", "did",
            "do", "does", "from", "gives", "had", "has", "have", "is", "keeps",
            "makes", "may", "means", "might", "must", "of", "or", "reveals", "should",
            "shows", "than", "to", "under", "was", "were", "will", "with", "without",
            "would",
        }
        continuation_starts = self._MIDPHRASE_START_WORDS

        def visible_count(tokens: List[str]) -> int:
            visible = self._unprotect_phrases(" ".join(tokens))
            return self.visible_word_count(visible)

        best: List[tuple[float, List[str]] | None] = [None] * (len(words) + 1)
        best[len(words)] = (0.0, [])
        for start in range(len(words) - 1, -1, -1):
            for end in range(start + 1, len(words) + 1):
                chunk_tokens = words[start:end]
                count = visible_count(chunk_tokens)
                if count > word_limit:
                    break
                if end < len(words) and count < 2:
                    continue
                tail = chunk_tokens[-1].strip(" ,;:!?.").lower()
                next_word = words[end].strip(" ,;:!?.").lower() if end < len(words) else ""
                visible_chunk = self._unprotect_phrases(" ".join(chunk_tokens))
                ends_sentence = bool(re.search(r'[.!?]["\u2019\u201d]?$', visible_chunk.rstrip()))
                # Three-word cues are readable and pass the production quality
                # gate.  Forcing four words here can make a clean phrase boundary
                # mathematically impossible and strand a preposition at the start
                # of the next cue ("to show water", "of a longer argument").
                if end < len(words) and count < 3 and not ends_sentence:
                    continue
                # Four-word captions remain legal, but aiming for five reduces
                # subject/verb and modifier/noun fragmentation in short-form
                # narration while retaining the six-word hard ceiling.
                target_words = (
                    8
                    if self.max_words >= 8
                    else max(5, min(6, self.max_words))
                )
                penalty = abs(count - target_words) * 1.5
                if end < len(words) and count == 2:
                    penalty += 12
                if end < len(words) and not ends_sentence and tail in bad_endings:
                    # Phrase-boundary errors are more distracting than a mildly
                    # uneven word count. Keep a small escape route for impossible
                    # protected-name or measurement combinations.
                    penalty += 500
                if end < len(words) and not ends_sentence and next_word in continuation_starts:
                    # A prepositional continuation is the exact structural
                    # defect enforced by quality_issues(). Prefer a mildly
                    # less elegant auxiliary-led cue over a mid-phrase start.
                    penalty += 750
                elif end < len(words) and not ends_sentence and next_word in bad_starts:
                    penalty += 500
                # An unmatched bracket or a stranded numeric/unit component is a
                # hard semantic break, even if the word count otherwise looks neat.
                if end < len(words) and visible_chunk.count("(") != visible_chunk.count(")"):
                    penalty += 500
                if end < len(words) and re.search(r"(?:\d|\d[.,]\d+)\s*$", visible_chunk):
                    if re.match(
                        r"^(?:ft|feet|foot|in|inch|inches|m|metre|meter|km|kg|lb|ton|tonne)s?\b",
                        self._unprotect_phrases(words[end]),
                        flags=re.IGNORECASE,
                    ):
                        penalty += 500
                if chunk_tokens[-1].rstrip().endswith((",", ";", ":")):
                    penalty -= 2
                if end == len(words) and count < 3:
                    penalty += 120
                next_best = best[end]
                if next_best is None:
                    continue
                candidate = (penalty + next_best[0], [" ".join(chunk_tokens), *next_best[1]])
                if best[start] is None or candidate[0] < best[start][0]:
                    best[start] = candidate

        chunks = best[0][1] if best[0] is not None else [" ".join(words)]
        chunks = [self._unprotect_phrases(chunk).strip() for chunk in chunks if chunk.strip()]
        repaired = self._repair_caption_edges(chunks, word_limit=word_limit)

        # Edge repair can move a connector onto an already-full following cue.
        # Reapply the hard visible-word ceiling, carrying a trailing connector
        # forward while splitting so the result is both complete and bounded.
        for _ in range(3):
            if all(
                self.visible_word_count(chunk) <= word_limit
                for chunk in repaired
            ):
                break
            bounded: List[str] = []
            for chunk in repaired:
                protected_tokens = self._protect_phrases(chunk).split(" ")
                current: List[str] = []
                for token in protected_tokens:
                    proposed = [*current, token]
                    if current and visible_count(proposed) > word_limit:
                        carry: List[str] = []
                        tail = current[-1].strip(" ,;:!?.").lower()
                        ends_sentence = bool(
                            re.search(
                                r'[.!?]["\u2019\u201d]?$',
                                self._unprotect_phrases(" ".join(current)).rstrip(),
                            )
                        )
                        if len(current) > 1 and not ends_sentence and tail in bad_endings:
                            carry = [current.pop()]
                        bounded.append(self._unprotect_phrases(" ".join(current)).strip())
                        current = [*carry, token]
                    else:
                        current = proposed
                if current:
                    bounded.append(self._unprotect_phrases(" ".join(current)).strip())
            repaired = self._repair_caption_edges(
                [chunk for chunk in bounded if chunk],
                word_limit=word_limit,
            )
        return repaired

    def _repair_caption_edges(
        self,
        chunks: List[str],
        word_limit: int | None = None,
    ) -> List[str]:
        """Move stranded connectors to the phrase they introduce."""
        # Re-protect semantic units before moving words between cues.  The
        # optimizer deliberately treats names and phrases as indivisible, but
        # the old repair pass used ``split()`` (which also splits non-breaking
        # spaces) and could undo that work after the fact.
        repaired = [
            self._protect_phrases(self._normalize(chunk))
            for chunk in chunks
            if self._normalize(chunk)
        ]
        limit = word_limit or max(4, min(8, max(6, self.max_words)))
        dangling = self._AVOID_LINE_EDGE_WORDS | self._DANGLING_CAPTION_TAILS | {
            "because", "during", "linked", "show", "shows", "reveal", "reveals",
            "another",
        }
        for index in range(len(repaired) - 1):
            current = repaired[index].split(" ")
            following = repaired[index + 1].split(" ")
            moved: List[str] = []
            while len(current) > 1:
                if re.search(r'[.!?]["\u2019\u201d]?$', current[-1].rstrip()):
                    break
                tail = current[-1].strip(" ,;:!?.").lower()
                if tail not in dangling:
                    break
                moved.insert(0, current.pop())
            if moved:
                repaired[index] = " ".join(current).strip()
                repaired[index + 1] = " ".join([*moved, *following]).strip()
        repaired = [chunk for chunk in repaired if chunk]

        def visible_count(value: str) -> int:
            return self.visible_word_count(value)

        # Tail repair can leave a one- or two-word flash cue. Merge it when the
        # hard ceiling permits; otherwise borrow enough words from the previous
        # cue to restore a readable phrase and keep borrowing while that previous
        # cue would end on a dangling connector.
        index = 0
        while index < len(repaired):
            count = visible_count(repaired[index])
            if count >= 3 or len(repaired) == 1:
                index += 1
                continue
            if index + 1 < len(repaired) and count + visible_count(repaired[index + 1]) <= limit:
                repaired[index] = f"{repaired[index]} {repaired[index + 1]}".strip()
                del repaired[index + 1]
                continue
            if index > 0 and visible_count(repaired[index - 1]) + count <= limit:
                repaired[index - 1] = f"{repaired[index - 1]} {repaired[index]}".strip()
                del repaired[index]
                index = max(0, index - 1)
                continue
            if index > 0:
                previous = repaired[index - 1].split(" ")
                current = repaired[index].split(" ")
                while visible_count(" ".join(current)) < 3 and visible_count(" ".join(previous)) > 3:
                    current.insert(0, previous.pop())
                while len(previous) > 1 and visible_count(" ".join(previous)) > 3:
                    tail = previous[-1].strip(" ,;:!?.").lower()
                    if tail not in dangling:
                        break
                    current.insert(0, previous.pop())
                repaired[index - 1] = " ".join(previous).strip()
                repaired[index] = " ".join(current).strip()
            index += 1
        return [self._unprotect_phrases(chunk) for chunk in repaired if chunk]

    def _weights(self, texts: List[str]) -> List[float]:
        weights: List[float] = []
        for text in texts:
            word_count = max(1, len([word for word in text.split(" ") if word]))
            punctuation_bonus = (text.count(",") * 0.35) + (text.count(";") * 0.45)
            punctuation_bonus += 0.6 if text.rstrip().endswith((".", "!", "?")) else 0.0
            weights.append(float(word_count) + punctuation_bonus)
        return weights

    def _character_count(self, text: str) -> int:
        """CPS uses visible characters, including spaces between words."""
        return len(self._normalize(text))

    def _timeline(
        self,
        texts: List[str],
        total_duration: float,
        min_seconds: float,
        max_cps: float | None = None,
    ) -> List[SubtitleSegment]:
        normalized = [self._normalize(text) for text in texts if self._normalize(text)]
        if not normalized:
            return []

        avg = total_duration / max(1, len(normalized))
        floor = min(min_seconds, avg)
        if max_cps:
            char_counts = [self._character_count(text) for text in normalized]
            # Leave headroom for millisecond SRT rounding. A cue planned at
            # exactly 17.0 CPS can otherwise round back to 17.1 at QA time.
            planning_cps = max(10.0, max_cps - 0.2)
            required = [max(floor, count / planning_cps) for count in char_counts]
            required_total = sum(required)
            if required_total <= total_duration:
                durations = list(required)
                remaining = total_duration - required_total
                total_chars = sum(char_counts) or 1
                durations = [
                    duration + remaining * (char_counts[idx] / total_chars)
                    for idx, duration in enumerate(durations)
                ]
            else:
                # If the narration itself exceeds the ceiling, proportional
                # allocation minimizes the worst cue instead of creating one
                # extreme outlier. quality_issues() exposes the hard violation.
                total_chars = sum(char_counts) or 1
                durations = [total_duration * (count / total_chars) for count in char_counts]
        else:
            base_total = floor * len(normalized)
            extra = max(0.0, total_duration - base_total)
            weights = self._weights(normalized)
            total_weight = sum(weights) or 1.0
            durations = [
                floor + (extra * (weights[idx] / total_weight) if extra > 0 else 0.0)
                for idx in range(len(normalized))
            ]

        timeline: List[SubtitleSegment] = []
        cursor = 0.0
        for idx, text in enumerate(normalized):
            start = cursor
            end = total_duration if idx == len(normalized) - 1 else min(total_duration, cursor + durations[idx])
            timeline.append(SubtitleSegment(start=start, end=end, text=text))
            cursor = end
        return timeline

    def scene_segments(self, beats: List[str] | str, total_duration: float) -> List[SubtitleSegment]:
        if isinstance(beats, str):
            clean_beats = self._sentence_beats(beats)
        else:
            clean_beats = [self._normalize(beat) for beat in beats if self._normalize(beat)]
        if not clean_beats:
            return []
        avg = total_duration / max(1, len(clean_beats))
        min_seconds = min(3.4, max(1.6, avg * 0.7))
        return self._timeline(clean_beats, total_duration, min_seconds=min_seconds)

    def scene_segments_from_durations(
        self,
        beats: List[str] | str,
        durations: List[float],
        total_duration: float | None = None,
    ) -> List[SubtitleSegment]:
        if isinstance(beats, str):
            clean_beats = self._sentence_beats(beats)
        else:
            clean_beats = [self._normalize(beat) for beat in beats if self._normalize(beat)]
        if not clean_beats:
            return []

        usable_durations = [max(0.05, float(duration)) for duration in durations[: len(clean_beats)]]
        if len(usable_durations) < len(clean_beats):
            return self.scene_segments(clean_beats, total_duration or sum(usable_durations) or 1.0)

        timeline: List[SubtitleSegment] = []
        cursor = 0.0
        final_total = total_duration if total_duration is not None else sum(usable_durations)
        for idx, text in enumerate(clean_beats):
            start = cursor
            if idx == len(clean_beats) - 1:
                end = max(start + 0.05, final_total)
            else:
                end = start + usable_durations[idx]
            timeline.append(SubtitleSegment(start=start, end=end, text=text))
            cursor = end
        return timeline

    def caption_segments_from_scene_segments(self, scene_timeline: List[SubtitleSegment]) -> List[SubtitleSegment]:
        caption_timeline: List[SubtitleSegment] = []
        for scene in scene_timeline:
            chunks: List[str] = []
            for sentence in self._sentence_beats(scene.text):
                chunks.extend(self._chunk_text(sentence))

            def structural_edge_issues(values: List[str]) -> int:
                count = 0
                for index, value in enumerate(values):
                    visible_words = self._VISIBLE_WORD_PATTERN.findall(value or "")
                    if self.visible_word_count(value) > self.hard_word_limit():
                        count += 1
                    if len(values) > 1 and self.visible_word_count(value) < 3:
                        count += 1
                    if index + 1 < len(values) and visible_words and not re.search(
                        r'[.!?]["\u2019\u201d]?$', value.strip()
                    ):
                        tail = visible_words[-1].lower()
                        if tail in (
                            self._AVOID_LINE_EDGE_WORDS
                            | self._DANGLING_CAPTION_TAILS
                            | {"another"}
                        ):
                            count += 1
                    if index == 0:
                        continue
                    first = visible_words[0].lower() if visible_words else ""
                    if first not in self._MIDPHRASE_START_WORDS:
                        continue
                    previous_ends_sentence = bool(
                        re.search(r'[.!?]["\u2019\u201d]?$', values[index - 1].strip())
                    )
                    if not previous_ends_sentence:
                        count += 1
                return count

            # Per-sentence composition is normally clearer, but it can make a
            # clean split impossible when a short transition consumes the start
            # of the next sentence ("For Judah, it became part | of ...").  In
            # that case only, let the optimizer consider the full scene so the
            # transition can share the preceding cue. Keep the fallback strictly
            # improvement-only; ordinary sentence boundaries remain untouched.
            sentence_issue_count = structural_edge_issues(chunks)
            if sentence_issue_count:
                scene_chunks = self._chunk_text(scene.text)
                if structural_edge_issues(scene_chunks) < sentence_issue_count:
                    chunks = scene_chunks
            remaining_issue_count = structural_edge_issues(chunks)
            if remaining_issue_count and self.max_words < 6:
                # The configured target can be shorter than the production
                # hard ceiling. If a four-word target makes a grammatical
                # boundary impossible, let a six-word optimizer rebalance the
                # same sentence and accept it only when structural QA improves.
                wider = SubtitleComposer(
                    max_words_per_caption=6,
                    max_chars_per_second=self.max_cps,
                )
                wider_chunks: List[str] = []
                for sentence in self._sentence_beats(scene.text):
                    wider_chunks.extend(wider._chunk_text(sentence))
                if structural_edge_issues(wider_chunks) < remaining_issue_count:
                    chunks = wider_chunks
            if len(chunks) <= 1:
                caption_timeline.append(scene)
                continue
            scene_duration = max(0.75, scene.end - scene.start)
            chunk_timeline = self._timeline(
                chunks,
                scene_duration,
                min_seconds=min(1.0, scene_duration),
                max_cps=self.max_cps,
            )
            for segment in chunk_timeline:
                caption_timeline.append(
                    SubtitleSegment(
                        start=scene.start + segment.start,
                        end=scene.start + segment.end,
                        text=segment.text,
                    )
                )
        return caption_timeline

    def quality_issues(
        self,
        segments: List[SubtitleSegment],
        cps_tolerance: float = 0.05,
        allow_clause_continuations: bool = False,
    ) -> List[str]:
        """Return deterministic upload-gate issues for a completed caption track."""
        issues: List[str] = []
        hard_word_limit = self.hard_word_limit()
        for index, segment in enumerate(segments, start=1):
            duration = max(0.001, segment.end - segment.start)
            cps = self._character_count(segment.text) / duration
            visible_words = self.visible_word_count(segment.text)
            if len(segments) > 1 and visible_words < 3:
                issues.append(f"caption {index} is a {visible_words}-word flash fragment")
            if visible_words > hard_word_limit:
                issues.append(
                    f"caption {index} contains {visible_words} words "
                    f"(limit {hard_word_limit})"
                )
            if cps > self.max_cps + cps_tolerance + 1e-6:
                issues.append(
                    f"caption {index} reads at {cps:.1f} CPS (limit {self.max_cps:.1f})"
                )
            if segment.text.count("(") != segment.text.count(")"):
                issues.append(f"caption {index} contains an unclosed parenthetical measurement")
            edge_words = self._VISIBLE_WORD_PATTERN.findall(segment.text or "")
            tail = edge_words[-1].lower() if edge_words else ""
            current_ends_sentence = bool(
                re.search(r'[.!?]["\u2019\u201d]?$', segment.text.strip())
            )
            # A tail such as "is" or "one" can close a complete sentence
            # naturally ("more informative than it is.").  Only reject it
            # when the cue visibly continues.  This is deliberately separate
            # from the cue-start check below: a caption that starts with "of"
            # remains a mid-phrase defect even when that caption ends the
            # sentence.
            if (
                not current_ends_sentence
                and tail
                in (
                    self._AVOID_LINE_EDGE_WORDS
                    | self._DANGLING_CAPTION_TAILS
                    | {"another"}
                )
            ):
                issues.append(f"caption {index} ends on an incomplete phrase")
            if index > 1 and not allow_clause_continuations:
                previous_text = segments[index - 2].text.strip()
                first = edge_words[0].lower() if edge_words else ""
                begins_with_continuation = first in self._MIDPHRASE_START_WORDS
                previous_ends_sentence = bool(
                    re.search(r'[.!?]["\u2019\u201d]?$', previous_text)
                )
                if begins_with_continuation and not previous_ends_sentence:
                    issues.append(f"caption {index} begins in the middle of a phrase")
        return issues

    def caption_segments_from_beats(self, beats: List[str] | str, total_duration: float) -> List[SubtitleSegment]:
        return self.caption_segments_from_scene_segments(self.scene_segments(beats, total_duration))

    def segments(self, text: str, total_duration: float) -> List[SubtitleSegment]:
        return self.caption_segments_from_beats(self._sentence_beats(text), total_duration)

    def _fmt(self, seconds: float) -> str:
        millis = int(round(seconds * 1000))
        hours = millis // 3600000
        millis %= 3600000
        minutes = millis // 60000
        millis %= 60000
        secs = millis // 1000
        millis %= 1000
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def write_srt(self, path: Path, segments: List[SubtitleSegment]) -> None:
        lines = []
        for idx, seg in enumerate(segments, start=1):
            lines.append(str(idx))
            lines.append(f"{self._fmt(seg.start)} --> {self._fmt(seg.end)}")
            lines.append(seg.text)
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")

    def _font(self, size: int) -> ImageFont.ImageFont:
        for candidate in (
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
        ):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _text_width(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    def _line_score(self, lines: List[str], widths: List[int]) -> float:
        if not lines:
            return 999999.0
        score = float(max(widths) - min(widths)) if len(widths) > 1 else 0.0
        word_counts = [len(line.split()) for line in lines]
        total_words = sum(word_counts)
        target_words = total_words / max(1, len(lines))
        for idx, line in enumerate(lines):
            words = line.split()
            score += abs(len(words) - target_words) * 42
            if total_words >= 7 and len(words) <= 2:
                score += 360
            if len(words) <= 1 and len(lines) > 1:
                score += 900
            first = words[0].strip(" ,;:!?").lower() if words else ""
            last = words[-1].strip(" ,;:!?").lower() if words else ""
            if idx < len(lines) - 1 and last in self._AVOID_LINE_EDGE_WORDS:
                score += 350
            if idx > 0 and first in self._AVOID_LINE_EDGE_WORDS:
                score += 160
            if idx < len(lines) - 1 and line.rstrip().endswith((",", ";", ":")):
                score -= 70
        return score

    def _line_partitions(self, words: List[str], line_count: int) -> List[List[str]]:
        if line_count <= 1:
            return [[" ".join(words)]]
        if line_count >= len(words):
            return [[" ".join([word]) for word in words]]
        partitions: List[List[str]] = []

        def walk(start: int, remaining: int, current: List[str]) -> None:
            if remaining == 1:
                if start < len(words):
                    partitions.append(current + [" ".join(words[start:])])
                return
            max_end = len(words) - remaining + 1
            for end in range(start + 1, max_end + 1):
                walk(end, remaining - 1, current + [" ".join(words[start:end])])

        walk(0, line_count, [])
        return partitions

    def display_lines(
        self,
        text: str,
        font: ImageFont.ImageFont,
        max_width: int,
        draw: ImageDraw.ImageDraw,
        max_lines: int = 2,
        uppercase: bool = False,
    ) -> List[str]:
        clean = self._protect_phrases(self._normalize(text))
        if uppercase:
            clean = clean.upper()
        words = clean.split(" ")
        if not words:
            return []
        max_lines = max(1, min(max_lines, len(words)))
        if self._text_width(draw, clean, font) <= max_width:
            return [self._unprotect_phrases(clean)]

        best: tuple[float, List[str]] | None = None
        for line_count in range(2, max_lines + 1):
            for lines in self._line_partitions(words, line_count):
                widths = [self._text_width(draw, line, font) for line in lines]
                if any(width > max_width for width in widths):
                    continue
                score = self._line_score(lines, widths)
                if best is None or score < best[0]:
                    best = (score, lines)
            if best is not None:
                return [self._unprotect_phrases(line) for line in best[1]]

        lines: List[str] = []
        current: List[str] = []
        for word in words:
            trial = " ".join(current + [word])
            if current and self._text_width(draw, trial, font) > max_width and len(lines) < max_lines - 1:
                lines.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(" ".join(current))
        if len(lines) > max_lines:
            lines = lines[: max_lines - 1] + [" ".join(" ".join(lines[max_lines - 1:]).split())]
        return [self._unprotect_phrases(line) for line in (lines or [clean])]

    def render_caption_image(self, text: str, width: int = 920) -> np.ndarray:
        clean = self._normalize(text)
        sample = Image.new("RGBA", (width, 10), (0, 0, 0, 0))
        sample_draw = ImageDraw.Draw(sample)
        text_max_w = width - 110
        lines = [clean]
        font = self._font(52)
        for font_size in range(58, 42, -4):
            font = self._font(font_size)
            lines = self.display_lines(clean, font, text_max_w, sample_draw, max_lines=2)
            word_counts = [len(line.split()) for line in lines]
            needs_better_balance = sum(word_counts) >= 7 and min(word_counts or [0]) <= 2 and font_size > 46
            if all(self._text_width(sample_draw, line, font) <= text_max_w for line in lines) and not needs_better_balance:
                break
        wrapped = "\n".join(lines)
        line_count = max(1, len(lines))
        line_spacing = 10

        bbox = sample_draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=line_spacing)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        img_h = max(170, text_h + 86)
        base = Image.new("RGBA", (width, img_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(base)

        draw.rounded_rectangle(
            (18, 18, width - 18, img_h - 18),
            radius=28,
            fill=(6, 8, 12, 190),
            outline=(255, 255, 255, 58),
            width=2,
        )
        draw.rounded_rectangle((32, 30, 42, img_h - 30), radius=12, fill=(239, 196, 76, 255))

        tx = (width - text_w) / 2
        ty = (img_h - text_h) / 2 - 2
        draw.multiline_text((tx + 3, ty + 3), wrapped, font=font, fill=(0, 0, 0, 200), spacing=line_spacing, align="center")
        draw.multiline_text((tx, ty), wrapped, font=font, fill=(255, 255, 255, 255), spacing=line_spacing, align="center")

        return np.array(base)
