from __future__ import annotations

import json
import math
import os
import random
import re
import time
from dataclasses import replace
from typing import Callable
from urllib.parse import urlparse

import requests

from yt_auto import brain_hooks, pipeline_quality
from yt_auto.models import ChannelConfig, ScenePlanItem, ScriptWriterConfig, TopicCandidate
from yt_auto.network_policy import validate_ollama_generate_url


class ScriptWriter:
    # ~50-59s shorts at ~142-155 WPM land around 120-145 spoken words.
    _ANCIENT_SHORT_MIN_WORDS = 120
    _ANCIENT_SHORT_MAX_WORDS = 148
    _ANCIENT_SHORT_TARGET_WORDS = 132
    _BRAIN_SHORT_MIN_WORDS = 122
    _BRAIN_SHORT_MAX_WORDS = 142
    _BRAIN_SHORT_TARGET_WORDS = 132

    _EDITORIAL_STOP_WORDS = {
        "about", "after", "again", "because", "before", "behind", "from", "have",
        "into", "just", "more", "that", "their", "there", "these", "they", "this",
        "through", "what", "when", "where", "which", "while", "with", "would", "your",
    }

    def __init__(self, cfg: ScriptWriterConfig) -> None:
        self.cfg = cfg
        # Legacy runtime pointer used by older callers. It means only that the
        # provider returned a non-empty, validator-approved response; persisted
        # provenance uses last_ai_provider and explicit content acceptance.
        self.last_provider = ""
        self.last_provider_endpoint_host = ""
        self.last_provider_model = ""
        self.last_ai_provider = ""
        self.last_ai_provider_endpoint_host = ""
        self.last_ai_provider_model = ""
        self.script_provider = ""
        self.script_provider_endpoint_host = ""
        self.script_provider_model = ""
        self.provider_attempts: list[dict] = []
        self.provider_attempt_history: list[dict] = []
        self._provider_call_index = 0
        self._provider_backoff_until: dict[str, float] = {}

    def reset_provider_audit(self) -> None:
        """Start a clean, cumulative provider audit for one build."""
        self.last_provider = ""
        self.last_provider_endpoint_host = ""
        self.last_provider_model = ""
        self.last_ai_provider = ""
        self.last_ai_provider_endpoint_host = ""
        self.last_ai_provider_model = ""
        self.script_provider = ""
        self.script_provider_endpoint_host = ""
        self.script_provider_model = ""
        self.provider_attempts = []
        self.provider_attempt_history = []
        self._provider_call_index = 0

    def _record_provider_attempts(self, attempted: list[dict]) -> None:
        self._provider_call_index += 1
        self.provider_attempts = [dict(item) for item in attempted]
        self.provider_attempt_history.extend(
            {"call_index": self._provider_call_index, **dict(item)}
            for item in attempted
        )

    def _accept_last_ai_content(self, accepted_as: str) -> dict[str, str]:
        """Mark the most recent usable response as consumed by a named task."""
        accepted_label = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(accepted_as or "").strip().lower(),
        ).strip("_")[:64] or "unspecified"
        accepted_attempt = next(
            (
                item
                for item in reversed(self.provider_attempts)
                if item.get("status") == "ok"
            ),
            None,
        )
        if not accepted_attempt:
            return {"provider": "", "endpoint_host": "", "model": ""}

        accepted_attempt["accepted_content"] = True
        accepted_attempt["accepted_as"] = accepted_label
        for item in reversed(self.provider_attempt_history):
            if (
                item.get("call_index") == self._provider_call_index
                and item.get("status") == "ok"
                and item.get("provider") == accepted_attempt.get("provider")
            ):
                item["accepted_content"] = True
                item["accepted_as"] = accepted_label
                break

        provenance = {
            "provider": str(accepted_attempt.get("provider") or ""),
            "endpoint_host": str(accepted_attempt.get("endpoint_host") or ""),
            "model": str(accepted_attempt.get("model") or ""),
        }
        self.last_ai_provider = provenance["provider"]
        self.last_ai_provider_endpoint_host = provenance["endpoint_host"]
        self.last_ai_provider_model = provenance["model"]
        if accepted_label in {"script", "scene_script"}:
            self.script_provider = provenance["provider"]
            self.script_provider_endpoint_host = provenance["endpoint_host"]
            self.script_provider_model = provenance["model"]
        return provenance

    def _ai_enabled(self) -> bool:
        """Return whether the configured mode can run guarded AI refinements."""
        provider = (self.cfg.provider or "template").lower().strip()
        return provider in {
            "ollama",
            "gemini",
            "auto",
            "gemini_ollama",
            "routed",
            "local_first",
            "openai_compatible",
            "groq",
            "mistral",
        }

    def _clean(self, text: str) -> str:
        text = text.replace("```", " ").replace("#", "")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _sentence(self, text: str) -> str:
        text = self._clean(text).strip(" ,;:-")
        text = text.replace("...", ".")
        text = re.sub(r"\s+[-\u2013\u2014]\s+", ", ", text)
        text = re.sub(r"\s+,", ",", text)
        if not text:
            return ""
        if text[0].islower():
            text = text[0].upper() + text[1:]
        if text[-1] not in ".!?":
            text += "."
        return text

    def _headline_subject(self, title: str) -> str:
        subject = (title or "").strip()
        for pattern in (
            r"^how (.+?): how .+$",
            r"^how (.+?) quietly steals your attention$",
            r"^(.+?): the brain trick shaping your next reaction$",
            r"^the first clue (.+?) is (?:taking over|bending your judgment)$",
            r"^3 signs (.+?) is running your reaction$",
            r"^why (.+?) makes small moments feel huge$",
            r"^the tiny trigger that keeps (.+?) alive$",
            r"^3 signs (.+?) is shaping your reactions$",
            r"^3 body signals that reveal (.+?)$",
            r"^how to interrupt (.+?) before it takes over$",
            r"^(.+?): the hidden loop behind your reaction$",
            r"^(.+?): the loop your brain keeps rewarding$",
        ):
            match = re.match(pattern, subject, flags=re.IGNORECASE)
            if match:
                subject = match.group(1).strip()
                break
        for prefix in ("The bizarre true story of ", "The real story behind ", "The real story of ", "The true story of ", "The psychology behind ", "Why your brain does this: ", "The behavior pattern behind ", "This mental bias changes your decisions: ", "What your brain is doing with ", "Why this feeling takes over: ", "Why you keep doing this: ", "Why your mind gets stuck here: ", "Why people act this way: ", "The lesson inside ", "What really happened in ", "What your brain is doing with "):
            if subject.lower().startswith(prefix.lower()):
                subject = subject[len(prefix):]
        if ":" in subject:
            left, right = [part.strip() for part in subject.split(":", 1)]
            if 8 <= len(left) <= 58 and len(right) >= 8:
                subject = left
        return subject.strip()

    def _display_subject(self, subject: str, channel_id: str = "") -> str:
        normalized = re.sub(r"\s+", " ", subject or "").strip()
        if not normalized:
            return ""
        if channel_id == "brain_lens":
            return normalized.lower()
        known = {
            "byzantine greek fire": "Byzantine Greek Fire",
            "greek fire": "Greek Fire",
            "carthage": "Carthage",
            "nazca lines": "Nazca Lines",
            "nasca lines": "Nazca Lines",
            "petra": "Petra",
            "qin shi huang": "Qin Shi Huang",
            "terracotta army": "Terracotta Army",
            "library of alexandria": "Library of Alexandria",
        }
        lowered = normalized.lower()
        if lowered in known:
            return known[lowered]
        small = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
        words = re.split(r"\s+", normalized)
        out = []
        for idx, word in enumerate(words):
            if idx > 0 and idx < len(words) - 1 and word.lower() in small:
                out.append(word.lower())
            else:
                out.append(word[:1].upper() + word[1:])
        return " ".join(out).strip()

    def _title_formula_issue(self, channel_id: str, title: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", title or "").strip()
        lowered = cleaned.lower()
        if not cleaned:
            return "missing title"
        weak_starts = (
            "the real story",
            "the true story",
            "the psychology behind",
            "why your brain does this",
            "why your mind gets stuck here",
            "why people act this way",
            "why you keep doing this",
            "this mental bias changes your decisions",
            "the lesson inside",
            "the hidden brain loop behind",
            "the behavior pattern behind",
            "what really happened in",
            "did you know",
            "have you ever",
        )
        if lowered.startswith(weak_starts):
            return "generic title pattern"
        if re.match(r"^what .+ does before you notice it$", lowered):
            return "generic repeated Brain Lens title"
        if re.match(r"^why .+ feels personal even when it is not$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and "quietly steals your attention" in lowered:
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and any(
            phrase in lowered
            for phrase in (
                "brain trick shaping your next reaction",
                "how this brain trick skews your choices",
                "brain trick that skews your choices",
                "makes simple choices feel heavy",
                "running your reaction",
                "reward loop your brain keeps choosing",
                "pattern people read before you speak",
                "loop your brain keeps rewarding",
                "the loop your brain",
            )
        ):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^why .+ feels urgent when nothing is wrong$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^.+: the pattern people read before you speak$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^.+: the reward loop your brain keeps choosing$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^the first clue .+ is (taking over|bending your judgment)$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and "the first clue" in lowered:
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^3 signs .+ is (running|shaping) your reaction", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and lowered.startswith("how ") and ": how " in lowered:
            return "duplicate how-title formula"
        if channel_id == "brain_lens" and re.search(
            r"\b(?:voice change|eye contact|body language|one signal|tiny signal)\b.*\b(?:reveals?|proves?)\b.*\battraction\b",
            lowered,
        ):
            return "ambiguous attraction cue framed as proof"
        if channel_id == "brain_lens" and any(
            phrase in lowered
            for phrase in (
                "your brain already knows",
                "brain already knows",
                "this one text move",
                "means they are gone",
                "means they're gone",
            )
        ):
            return "unsupported relationship certainty or withheld-subject clickbait"
        if channel_id == "ancient_history" and any(
            phrase in lowered
            for phrase in (
                "3 pieces of evidence", "3 artifacts that", "3 physical clues",
                "the artifact trail behind", "the artifact that changed how historians see",
            )
        ):
            return "overclaiming Ancient title formula"
        if re.search(r"\(?\s*\d+\s*characters?\s*\)?", lowered):
            return "AI length note leaked into title"
        return None

    def _spoken_subject(self, topic: TopicCandidate) -> str:
        subject = topic.subject or self._headline_subject(topic.title) or topic.title
        if topic.niche_id == "brain_lens":
            return subject.lower()
        return self._display_subject(subject, topic.niche_id)

    def _bad_ai_line_issue(self, text: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        lowered = cleaned.lower()
        if not cleaned:
            return "empty line"
        if re.search(r"[\u0370-\u03ff\u0400-\u052f\u0590-\u08ff\u0900-\u0fff\u3040-\u30ff\u3400-\u9fff]", cleaned):
            return "unspoken foreign-script text leaked into narration"
        bad_phrases = (
            "easy to miss status",
            "usually a practical one who controlled",
            "who controlled the place, the food, the road or",
            "what makes ",
            "the detail people skip",
            "the part most people remember",
            "is often overlooked",
            "famous story and the evidence",
            "changes the whole story",
            "points to a sharper story",
            "explains the danger",
            "less like legend and more like a warning",
            "famous version will feel too simple",
            "the important part is not just the famous headline",
            "the real story sits in the place",
            "not as a flat myth",
            "people can still.",
            "can still.",
            "it is the evidence",
            "popular version",
            "material record",
            "anchor the scene",
            "messier chain",
            "clean myth",
            "sources around this clue",
            "first clue in the evidence",
            "not the headline it is",
            "matters because control of land",
            "belonged to whoever could organize builders",
            "changed daily life through food, trade, labor",
            "the aftermath of ",
            "the tangible clues are stone blocks",
            "archaeologists still study",
            "the strongest evidence around",
            "the layout of ",
            "the real stakes of ",
            "archaeologists read ",
            "the decisive detail in ",
            "ancient accounts of ",
        )
        for phrase in bad_phrases:
            if phrase in lowered:
                return f"generic AI phrase: {phrase}"
        if re.search(r"\bafter (?:making|doing|having|seeing|hearing|feeling)\.?$", lowered):
            return "incomplete action phrase"
        if re.search(r"\b(?:something|anything|it|this|they) (?:is|are) worth\.?$", lowered):
            return "incomplete worth phrase"
        if re.search(r"\bjudgment catches\.?$", lowered):
            return "incomplete catches-up phrase"
        if re.search(r"\b(?:a|an|the|your|one) (?:safer|clearer|warmer|softer|stronger)\.?$", lowered):
            return "line ends on an incomplete adjective phrase"
        if re.search(r"\b(or|and|with|from|to|of|the|a|an|that|which|because|linked|still|inspect|what|why|how|rather|than|before|after|while|but|yet|so|instead)\.$", lowered):
            return "line ends on a dangling connector"
        if re.search(r"\b(?:they are|something is|anything is|this is)\.?$", lowered):
            return "line ends on an incomplete copula"
        if re.search(r"\b(?:in|at|from|within) what is now\.?$", lowered):
            return "line ends on an incomplete modern-location phrase"
        if "culmination of work" in lowered or "larger than the others" in lowered:
            return "vague comparison or unfinished research fragment"
        if re.search(r"\bgrew to (?:containing|having)\b", lowered):
            return "ungrammatical population phrase"
        if re.search(r"\b(?:with|by|of)(?: that of)? [a-z]\.?$", lowered):
            return "line ends on an incomplete citation initial"
        if re.match(r"^in \d{3,4},", lowered) and not re.search(
            r"\b(?:was|were|became|began|founded|established|created|opened|appointed|surveyed|formed|started|ended|published|discovered|excavated|built|ruled|spread|grew|emerged|arrived|occurred|collapsed|expanded|reached|recorded)\b",
            lowered,
        ):
            return "dated clause has no complete action"
        if re.search(r"\b(?:achaemenid|roman|persian|byzantine|maya|mauryan|assyrian|nabataean|punic|phoenician)\.?$", lowered):
            return "line ends on an incomplete historical adjective"
        if re.search(
            r"\b(?:and|or)\s+(?:demonstrates?|describes?|indicates?|records?|reveals?|shows?|suggests?)\.?$",
            lowered,
        ):
            return "line ends on a transitive verb without its object"
        if (
            "the real stakes" in lowered
            and re.search(r"\bsafety,? work,? wealth,? status,? and survival\b", lowered)
        ) or "loyalty and survival all leave traces" in lowered:
            return "generic stakes filler"
        return None

    def _captionize(self, text: str) -> str:
        text = self._sentence(text).rstrip(".!? ")
        if not text:
            return ""
        lowered = text.lower()
        if lowered.startswith("the halo effect lets one strong trait color"):
            return "One strong trait colors the whole impression"
        if lowered.startswith("name the impressive trait separately from reliability"):
            return "Separate attraction from reliability and kindness"
        text = re.sub(r"^(?:First|Then|Finally|Next),?\s*", "", text, flags=re.IGNORECASE)
        first_clause = re.split(r"[;:]", text, maxsplit=1)[0].strip()
        if "," in first_clause and len(first_clause) > 92:
            first_clause = first_clause.split(",", 1)[0].strip()
        words = first_clause.split()
        if len(words) < 4:
            first_clause = " ".join(text.split()[:10]).strip(" ,;:-")
            words = first_clause.split()
        if len(words) > 14:
            first_clause = " ".join(words[:14]).strip(" ,;:-")
        first_clause = self._complete_caption_phrase(first_clause)
        first_clause = re.sub(
            r"(?:\b(?:to|and|of|the|a|an|with|for|in|on|at|from|that|which|because|why|how|what|linked|still|inspect|rather|than|before|after|while|but|yet|so|instead)\b[ ,;:-]*)+$",
            "",
            first_clause,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        return first_clause

    def _complete_caption_phrase(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip(" ,;:-")
        if not text:
            return ""
        for marker in (" because ", " while ", " when ", " which ", " that ", " why ", " how ", " long before ", " without ", " but "):
            match = re.search(re.escape(marker), text, flags=re.IGNORECASE)
            if not match:
                continue
            before = text[: match.start()].strip(" ,;:-")
            after = text[match.end():].strip(" ,;:-")
            if len(before.split()) >= 4 and len(after.split()) <= 7:
                return re.sub(
                    r"\b(?:helps explain|reveal|reveals|show|shows|mean|means|becomes|starts|turns into|linked to|linked|people can still|can still|can)\s*$",
                    "",
                    before,
                    flags=re.IGNORECASE,
                ).strip(" ,;:-")
        return re.sub(
            r"\b(?:helps explain|reveal|reveals|show|shows|mean|means|becomes|starts|turns into|linked to|linked|people can still|can still|can)\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")

    def _tidy_scene_line(self, text: str, max_words: int = 16) -> str:
        text = self._clean(text)
        text = re.sub(r"^(?:First|Then|Finally|Next),?\s*", "", text, flags=re.IGNORECASE)
        sentence_parts = self._sentences(text)
        if len(sentence_parts) > 1 and self._word_count(sentence_parts[0]) <= max_words:
            text = sentence_parts[0]
        text = re.sub(
            r"(?:\b(?:to|and|of|the|a|an|with|for|in|on|at|from|that|which|because|why|how|what|linked|still|inspect)\b[ ,;:-]*)+$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        text = re.sub(r"\b(?:people can still|can still|can)\s*$", "", text, flags=re.IGNORECASE).strip(" ,;:-")
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]).strip(" ,;:-")
            text = re.sub(
                r"(?:\b(?:to|and|of|the|a|an|with|for|in|on|at|from|that|which|because|why|how|what|linked|or|is|was|were|be|still|inspect|rather|than|before|after|while|but|yet|so|instead)\b[ ,;:-]*)+$",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip(" ,;:-")
            text = re.sub(r"\b(?:people can still|can still|can)\s*$", "", text, flags=re.IGNORECASE).strip(" ,;:-")
            text = re.sub(r"\b(?:sudden|one|just|not|more|less|very|too)\s+\w+$", "", text, flags=re.IGNORECASE).strip(" ,;:-")
        if self._bad_ai_line_issue(text):
            return ""
        return self._sentence(text)

    def _simplify_history_line(self, text: str) -> str:
        line = self._sentence(text)
        axum_measurement = re.match(
            r"^The Obelisk of Axum is a 4th-century CE, 24-metre \(79 ft\) tall phonolite stele, weighing 160 tonnes, in the city of Axum\.$",
            line,
            flags=re.IGNORECASE,
        )
        if axum_measurement:
            return (
                "The Obelisk of Axum is a 24-metre-tall phonolite stele dating to the fourth century CE; "
                "it weighs about 160 tonnes."
            )
        fought_match = re.match(
            r"^It was fought between the citizens of (.*?), aided by (.*?), and a (.*?) force commanded by (.*?)\.$",
            line,
            flags=re.IGNORECASE,
        )
        if fought_match:
            return self._sentence(
                f"{fought_match.group(1)} and {fought_match.group(2)} fought a "
                f"{fought_match.group(3)} force commanded by {fought_match.group(4)}"
            )
        settled_match = re.match(
            r"^It was settled from around (.*?), and served as the capital of (.*?) from the (.*?)\.$",
            line,
            flags=re.IGNORECASE,
        )
        if settled_match:
            return self._sentence(
                f"Settlement began around {settled_match.group(1)}; by the {settled_match.group(3)}, "
                "it served as the kingdom's capital"
            )
        construction_match = re.match(
            r"^Major construction on the city began in the (.*?) until the (.*?) and it was abandoned in the (.*?)\.$",
            line,
            flags=re.IGNORECASE,
        )
        if construction_match:
            abandonment_period = construction_match.group(3)
            if "centur" not in abandonment_period.lower():
                abandonment_period = f"{abandonment_period} century"
            return self._sentence(
                f"Construction began in the {construction_match.group(1)}; work continued through the {construction_match.group(2)} "
                f"before {abandonment_period} abandonment"
            )
        if re.match(r"^Inside is a ring of smaller bluestones\.$", line, flags=re.IGNORECASE):
            return "A smaller ring of bluestones stands inside the larger sarsen circle."
        if re.match(
            r"^Inside these are free-standing trilithons two bulkier vertical sarsens joined by a single lintel\.$",
            line,
            flags=re.IGNORECASE,
        ):
            return "Five central trilithons used paired upright sarsens beneath horizontal lintels."
        if re.match(
            r"^It consists of an outer ring of vertical sarsen standing stones, each around 13 feet \(4\.0 m\) high.*$",
            line,
            flags=re.IGNORECASE,
        ):
            return "An outer circle of towering sarsen stones carries horizontal lintels fitted together above the ground."
        attempt_match = re.match(
            r"^The battle was the culmination of the first attempt by (.*?) under (.*?) to (.*?)\.$",
            line,
            flags=re.IGNORECASE,
        )
        if attempt_match:
            return self._sentence(
                f"{attempt_match.group(2)} launched {attempt_match.group(1)}'s first attempt to {attempt_match.group(3)}"
            )
        defeat_match = re.match(
            r"^The (.*?) inflicted a crushing defeat on the more numerous (.*?), marking (.*?)\.$",
            line,
            flags=re.IGNORECASE,
        )
        if defeat_match:
            consequence = re.sub(
                r"^a turning point in (?:the )?",
                "",
                defeat_match.group(3),
                flags=re.IGNORECASE,
            )
            return self._sentence(
                f"The {defeat_match.group(1)} defeated the more numerous {defeat_match.group(2)}; "
                f"it shifted the {consequence}"
            )
        return line

    def _word_count(self, text: str) -> int:
        return len(re.findall(r"[A-Za-z0-9']+", text or ""))

    def _target_words_for_duration(self, seconds: int, wpm: int = 150) -> int:
        # 150 wpm ~ 2.5 words/sec. Use a small buffer so we don't clip the end.
        seconds = max(1, int(seconds))
        wps = wpm / 60.0
        return max(60, int(seconds * wps * 0.94))

    # A long script is assembled by three different paths. Each one used to
    # decide its own length, so setting a channel to four minutes shortened
    # one of them and left the others writing nine minutes of narration --
    # which then needed a 43% speed-up to fit, and the voice guard refused.
    # Every path ends here instead.
    LONG_PLAN_MIN_MIDDLE = 4

    def _fit_long_plan(
        self,
        channel: ChannelConfig,
        plan: list[ScenePlanItem],
        content_kind: str = "video",
    ) -> list[ScenePlanItem]:
        """Trim a long scene plan to the channel's word budget.

        The opening and closing scenes are always kept: they carry the hook and
        the payoff, and a script without either is worse than a slightly long
        one. Enough middle is kept for a real structure before the budget is
        allowed to stop it.
        """
        if content_kind != "video" or len(plan) < 3:
            return plan
        budget = int(self._target_words(channel, content_kind="video"))
        opener, closer = plan[0], plan[-1]
        used = self._word_count(opener.narration) + self._word_count(closer.narration)
        middle: list[ScenePlanItem] = []
        for scene in plan[1:-1]:
            words = self._word_count(scene.narration)
            if len(middle) < self.LONG_PLAN_MIN_MIDDLE or used + words <= budget:
                middle.append(scene)
                used += words
            if used >= budget and len(middle) >= self.LONG_PLAN_MIN_MIDDLE:
                break
        return [opener, *middle, closer]

    def _target_words(self, channel: ChannelConfig, content_kind: str) -> int:
        if content_kind == "video":
            # Derived from the configured duration, with no fixed floor.
            # A hardcoded 1250-word minimum meant the duration setting did
            # nothing: shortening a channel to 4-6 minutes still produced a
            # nine-minute script, which the length check then rejected.
            #
            # Aim at the middle of the band, the way Shorts do below. Aiming at
            # the minimum leaves no room underneath: a script that lands even
            # slightly short then has to be stretched to reach the floor, and
            # stretching narration is exactly what makes a voice sound wrong.
            # One came back needing 16.6% against an 8% limit.
            min_s = int(getattr(channel.videos, "min_duration_seconds", 480) or 480)
            max_s = int(getattr(channel.videos, "max_duration_seconds", min_s) or min_s)
            mid_s = max(min_s, (min_s + max(min_s, max_s)) // 2)
            return max(320, self._target_words_for_duration(mid_s, wpm=165))
        # shorts — aim for the middle of the 50-59s window
        min_s = int(getattr(channel.shorts, "min_duration_seconds", 50) or 50)
        mid_s = max(
            min_s,
            int(
                (
                    float(getattr(channel.shorts, "min_duration_seconds", 50) or 50)
                    + float(getattr(channel.shorts, "max_duration_seconds", 59) or 59)
                )
                / 2.0
            ),
        )
        if channel.id == "ancient_history":
            # Mid-window target (~54s) at ~150 WPM keeps atempo inside 0.85-1.20.
            return max(
                self._ANCIENT_SHORT_TARGET_WORDS,
                self._target_words_for_duration(mid_s, wpm=150),
            )
        return max(
            self._BRAIN_SHORT_TARGET_WORDS,
            self._target_words_for_duration(mid_s, wpm=155),
        )


    def _video_expansion_lines(self, topic: TopicCandidate, subject: str) -> list[str]:
        if topic.niche_id == "ancient_history":
            factual_lines = []
            scenes = list(topic.scene_plan or [])
            middle_scenes = scenes[1:-1] if len(scenes) >= 3 else scenes
            for scene in middle_scenes:
                line = self._simplify_history_line(scene.narration or scene.visual_text)
                if (
                    not line
                    or line.lower().startswith("subscribe for")
                    or self._is_generic_opener(line)
                    or self._bad_ai_line_issue(line)
                ):
                    continue
                factual_lines.append(line)
            if factual_lines:
                return list(dict.fromkeys(factual_lines))
            return [
                f"Historians test claims about {subject} against surviving evidence and the date of each source.",
                f"The strongest account of {subject} separates what the evidence confirms from what later writers added.",
            ]
        if topic.niche_id == "brain_lens":
            lowered = f"{topic.title} {topic.subject} {subject}".lower()
            topic_specific_lines = (
                (("fear of abandonment",), [
                    "Fear of abandonment can turn one delayed reply into proof that the connection is disappearing.",
                    "The visible loop is checking, rereading, or asking for reassurance before anything has actually changed.",
                    "Quick reassurance lowers the tension briefly, which is why the checking urge can return.",
                    "Separate fact from prediction: a late reply does not mean they are leaving.",
                    "Keep your plan for the next hour instead of testing the relationship again.",
                    "Judge emotional safety by consistent behavior across days, not one quiet stretch.",
                    "A secure next move protects the connection without abandoning your routine or self-respect.",
                    "Closeness feels healthier when both people can tolerate a little distance without creating a crisis.",
                ]),
                (("avoidant attachment",), [
                    "Avoidant attachment often appears after closeness: warmth first, then distance when vulnerability rises.",
                    "Watch the pattern across several interactions instead of diagnosing one quiet day.",
                    "Space can be healthy, but unexplained push-pull behavior keeps the other person guessing.",
                    "Ask directly what pace and communication feel comfortable instead of chasing a sudden withdrawal.",
                    "Consistency matters more than one intense date followed by a confusing silence.",
                    "Protect your self-respect by matching effort, not by performing indifference.",
                    "A calmer connection makes room for both closeness and honest requests for space.",
                    "The useful question is whether distance is communicated clearly and followed by reliable action.",
                ]),
                (("anxious attachment",), [
                    "Anxious attachment can make one slow reply feel louder than a week of consistent care.",
                    "The visible loop is checking the phone, rereading tone, and searching for instant reassurance.",
                    "Each check can calm the uncertainty briefly while teaching your attention to check again.",
                    "Name the fact first: the reply is late, but the relationship has not automatically changed.",
                    "Return to one planned task before deciding what the silence means.",
                    "Judge the connection by repeated care, honest communication, and repaired misunderstandings.",
                    "Confidence grows when reassurance supports your life instead of replacing it.",
                    "Secure attraction can include uncertainty without turning every pause into an emergency.",
                ]),
                (("love bombing",), [
                    "Love bombing compresses weeks of intimacy into days, making intensity look like proven trust.",
                    "Watch for grand promises arriving before the person knows how you handle ordinary conflict.",
                    "Constant attention can feel flattering while quietly rushing your normal pace and boundaries.",
                    "Slow the timeline and see whether warmth remains when you say no or need space.",
                    "Real compatibility survives boring days, small disagreements, and limits without punishment.",
                    "Keep your routines and friendships while the relationship earns trust through consistent action.",
                    "Chemistry is information, but it is not evidence that every promise is already safe.",
                    "The strongest signal is respect for your pace after the exciting beginning fades.",
                ]),
                (("future faking",), [
                    "Future faking is not one romantic plan; it is repeated promises that keep replacing action.",
                    "The visible pattern is a vivid future, a vague calendar, and no dependable next step.",
                    "Big promises can create intimacy before trust has enough time to form.",
                    "Ask for one concrete plan and notice whether the answer becomes specific.",
                    "Compare the promise with what happens this week, not what might happen someday.",
                    "Keep your normal pace until consistent actions earn deeper access.",
                    "Real intention survives ordinary logistics, boundaries, and a slower timeline.",
                    "Chemistry gets attention; consistency earns access.",
                ]),
                (("crush idealization", "limerence"), [
                    "One charming date and three late-night messages can feel like compatibility before you know how they handle ordinary life.",
                    "Idealization fills missing information with your best guesses, so uncertainty can make the fantasy more vivid.",
                    "Make two lists: what they actually did, and what you hope those moments mean.",
                    "Then ask for one real plan and notice whether warmth becomes reliable action.",
                    "Watch how they handle a boundary, a boring day, and a small misunderstanding before calling the match perfect.",
                    "Attraction becomes safer when real information is allowed to edit the fantasy.",
                    "Keep your routines while the connection earns a larger place in your life.",
                    "Let curiosity replace certainty until shared experience catches up with the crush.",
                ]),
                (("breadcrumb",), [
                    "Breadcrumbing works through tiny bursts of attention that restart hope without building a relationship.",
                    "The visible pattern is silence, one perfect message, then another stretch of uncertainty.",
                    "An unpredictable reply can feel more exciting because your brain cannot predict the reward.",
                    "Count consistent plans and follow-through, not the emotional intensity of one late-night text.",
                    "Ask for clarity once, then let repeated behavior answer more loudly than promises.",
                    "Do not confuse renewed contact with repaired effort when nothing practical changes.",
                    "Attraction becomes safer when attention is steady enough that you do not have to decode it.",
                    "The confident move is responding to the pattern, not chasing the latest breadcrumb.",
                ]),
                (("almost kiss",), [
                    "An almost-kiss stays vivid because anticipation leaves the reward emotionally unfinished.",
                    "Your attention zooms in on distance, breath, eye contact, and the pause before either person moves.",
                    "That suspense can make one second feel more intimate than the conversation around it.",
                    "But tension alone does not prove mutual intention; notice whether they return closeness afterward.",
                    "Let the next move be clear and welcome instead of forcing the unfinished moment to mean everything.",
                    "The spark is real, but mutual effort is what turns tension into connection.",
                ]),
                (("late night texting", "texting anxiety", "reply time anxiety", "attachment anxiety after texting"), [
                    "Warm texts feel intimate at night. Attention has fewer places to escape. Silence leaves room for fear.",
                    "A slow reply can feel threatening. Nothing may have changed yet. Context matters more than one delay.",
                    "Your brain measures interest through timestamps. It watches punctuation and typing bubbles.",
                    "Use the full pattern instead. Track plans, effort, and clear words.",
                    "Put the phone down long enough for your nervous system to stop writing the missing reply.",
                    "A response time is one data point; repeated care is the answer.",
                ]),
                (("mixed signals", "mixed attachment signals", "push pull"), [
                    "Warmth followed by distance creates a loop because relief arrives right after uncertainty.",
                    "The comeback feels powerful, but part of that intensity is the tension finally switching off.",
                    "Count the whole sequence: closeness, confusion, silence, return, and whether anything improves.",
                    "Ask for clarity once instead of auditioning harder every time the signal changes.",
                    "If consistency disappears whenever intimacy grows, the pattern matters more than the latest apology.",
                    "Healthy chemistry can be exciting without making confusion the price of staying connected.",
                ]),
                (("situationship", "almost relationship", "romantic uncertainty"), [
                    "An undefined connection can feel intense because every small moment still carries possibility.",
                    "Without a clear answer, your brain keeps collecting texts, touches, and plans as evidence.",
                    "Hope stays active because the story has not ended, but it has not become secure either.",
                    "Ask what is actually being built, not only what the best moments could become.",
                    "Clarity may disappoint you once; uncertainty can control your mood every day.",
                    "Real intimacy grows through shared intention, not endless potential.",
                ]),
                (("chemistry versus compatibility", "chemistry anxiety"), [
                    "Strong chemistry can make ordinary uncertainty feel urgent, meaningful, and impossible to ignore.",
                    "Compatibility shows up later: repaired awkwardness, respected limits, reliable plans, and emotional safety.",
                    "A racing heart can mean attraction, anxiety, or both; it cannot grade the relationship alone.",
                    "Watch how you feel after the date, not only during the most electric minute.",
                    "The best spark becomes safer when consistency keeps matching it in ordinary moments.",
                    "Chemistry starts the question; compatibility answers it over time.",
                ]),
                (("eye contact attraction", "flirting body language", "micro flirting", "voice change attraction", "body language"), [
                    "Attraction can shift eye contact, timing, posture, pitch, or expressiveness, but no single cue works for everyone.",
                    "Friendliness can look similar, so treat one cue as a question rather than proof.",
                    "Look for several returned signals across the conversation instead of decoding one charged second.",
                    "The confident move is to offer one respectful signal and leave room for a free response.",
                    "Reciprocity, comfort, and clear consent make the interaction more informative than body-language guesswork.",
                    "A spark can start the question; mutual behavior over time gives the safer answer.",
                ]),
                (("emotional availability", "self respect in dating", "fear of being too much"), [
                    "When you really like someone, honesty can feel riskier than quietly shrinking what you need.",
                    "Emotional availability looks calmer: they can hear a need without turning closeness into punishment.",
                    "Say one clear truth and watch whether curiosity, respect, and follow-through come back.",
                    "You do not protect attraction by becoming easier to disappoint or harder to understand.",
                    "The right connection makes room for desire, boundaries, and an ordinary honest conversation.",
                    "Self-respect does not kill chemistry; it reveals whether chemistry can become safe.",
                ]),
                (("silent treatment",), [
                    "Sudden silence can feel physically threatening because connection disappears without an explanation or timeline.",
                    "Healthy space names the need, protects respect, and sets a clear time to reconnect.",
                    "The silent treatment uses uncertainty as pressure and leaves one person chasing basic communication.",
                    "Do not fill every blank with apologies; ask when the conversation can resume.",
                    "If silence repeatedly replaces repair, treat that pattern as information about emotional safety.",
                    "Space can calm conflict; punishment keeps control by withholding connection.",
                ]),
                (("rejection sensitivity",), [
                    "One changed plan can feel like proof of rejection before you know why it changed.",
                    "Your attention starts scanning tone, timing, and old conversations for the moment you went wrong.",
                    "That alarm is trying to protect connection, but it can turn uncertainty into a verdict.",
                    "Name the fact first, then wait for context before apologizing for something nobody accused you of.",
                    "Watch how they explain, repair, and return instead of treating one disruption as the whole pattern.",
                    "A sensitive alarm deserves care; it does not deserve control of the conclusion.",
                ]),
                (("flirting anxiety",), [
                    "Flirting anxiety can make eye contact feel like a performance instead of a conversation.",
                    "The visible cue is rushing your words, looking away, or rehearsing the perfect response.",
                    "Trying to impress them increases self-monitoring and makes natural timing harder.",
                    "Shift attention to one detail they said instead of grading every move you make.",
                    "Use one honest question and let the conversation breathe for a second.",
                    "Warm curiosity reads as more confident than a memorized line delivered perfectly.",
                    "Notice whether they return the energy instead of carrying the entire interaction alone.",
                    "Attraction feels lighter when your goal is connection, not flawless performance.",
                ]),
                (("decision fatigue",), [
                    "Decision fatigue appears when even a small choice starts feeling heavier than its consequence.",
                    "The visible loop is reopening options, comparing again, and delaying a choice you can safely change.",
                    "Every extra option consumes attention that the next decision will also need.",
                    "Shrink the menu to two acceptable choices before asking which one is perfect.",
                    "Use defaults for low-stakes routines so your best attention stays available for important work.",
                    "Set a short decision deadline, then stop collecting information that will not change the outcome.",
                    "A good-enough choice made calmly often beats a perfect choice made too late.",
                    "The reset is not more motivation; it is fewer decisions competing at the same moment.",
                ]),
                (("dopamine", "reward loop"), [
                    "Dopamine is not a pleasure button; it helps your brain learn what may be worth repeating.",
                    "The visible loop is checking again because uncertainty leaves the next reward unresolved.",
                    "An unpredictable notification can hold attention longer than a reward that arrives every time.",
                    "Move the cue out of reach before willpower has to fight it again.",
                    "Choose one check time instead of reopening the app whenever tension rises.",
                    "The urge can weaken when you notice it without rewarding it immediately.",
                    "Replace the quick hit with one visible action: stand up, write a line, or start a timer.",
                    "The goal is not zero dopamine; it is teaching attention which rewards deserve repetition.",
                ]),
                (("attachment styles",), [
                    "Attachment styles describe common ways people seek closeness, safety, and space in relationships.",
                    "The visible pattern appears during uncertainty: one person reaches out while another pulls back.",
                    "One reaction does not define a person; repeated behavior across conflict matters more.",
                    "Ask what each person needs instead of turning distance or reassurance into a character flaw.",
                    "Clear requests reduce guessing and show whether both people can repair a tense moment.",
                    "A healthier pattern makes room for closeness, boundaries, and honest space at the same time.",
                ]),
                (("emotional contagion",), [
                    "Emotional contagion describes how another person's expression, tone, and energy can shift your own state.",
                    "A tense face or rushed voice can change a room before anyone names the problem.",
                    "Your attention reads social cues quickly because other people's reactions carry useful information.",
                    "Pause and name whose emotion entered the room before treating it as your own.",
                    "Relax your jaw, slow one breath, and choose a response instead of copying the intensity.",
                    "Empathy stays useful when you understand a mood without automatically carrying it.",
                    "Compare the room with how you felt five minutes earlier before calling the new mood yours.",
                    "A slower response can stop one person's tension from setting the pace for everyone.",
                ]),
                (("emotional regulation",), [
                    "Emotional regulation is the pause between feeling a reaction and choosing what happens next.",
                    "The visible cue is speed: a sharp message makes your hands answer before judgment catches up.",
                    "Naming the feeling can create enough distance to stop one impulsive reply.",
                    "Move your body, lower the screen, and wait until the first surge changes.",
                    "Then answer the actual message instead of the worst meaning your mind added to it.",
                    "Calm does not erase emotion; it keeps emotion from making every decision alone.",
                ]),
                (("attention span",), [
                    "Attention span is not just willpower; it is shaped by cues competing for the next glance.",
                    "A nearby phone can pull focus even when the screen never lights up.",
                    "Every quick check teaches your attention that interruption may deliver something more rewarding.",
                    "Put the cue out of sight before starting the conversation or task that matters.",
                    "Choose one short focus block, then take a deliberate break instead of an accidental scroll.",
                    "Better attention begins by changing what is easy to reach, not by shaming yourself.",
                ]),
                (("fawn response",), [
                    "The fawn response describes appeasing behavior that can appear when disagreement feels unsafe.",
                    "The visible cue is an automatic yes while your face, stomach, or schedule says no.",
                    "Agreeing quickly may reduce tension now while creating resentment and confusion later.",
                    "Buy ten seconds with one sentence: let me think before I answer.",
                    "Check what you want, what you can offer, and what limit needs to be clear.",
                    "Kindness stays kind when it includes your needs instead of erasing them.",
                ]),
                (("halo effect", "first impression"), [
                    "One electric date can make you defend red flags you would instantly notice in your best friend's relationship.",
                    "That is the halo effect: chemistry lends trust to qualities it has not actually proved.",
                    "A magnetic smile can make inconsistency look mysterious instead of careless.",
                    "Separate the spark from the evidence: do they keep plans, respect limits, and repair awkward moments?",
                    "Watch who they become on an ordinary Tuesday, when nobody is performing and nothing exciting is happening.",
                    "Chemistry earns attention, but consistency is what should earn access to your life.",
                ]),
                (("impostor syndrome",), [
                    "Impostor feelings appear when success arrives but your mind credits luck, timing, or a hidden mistake.",
                    "The visible loop is overpreparing, dismissing praise, and moving the standard after every win.",
                    "That protects you from feeling arrogant while blocking accurate evidence about your ability.",
                    "Write down what you did, what skill it required, and what another person actually noticed.",
                    "Accepting credit does not mean claiming perfection; it means using the full evidence.",
                    "Confidence becomes steadier when success is information, not a test you must immediately retake.",
                ]),
                (("learned helplessness",), [
                    "Learned helplessness describes what can happen when repeated failure teaches effort to feel useless.",
                    "The visible cue is stopping before the next attempt, even after the situation has changed.",
                    "Your prediction stays tied to old outcomes instead of the options available now.",
                    "Shrink the test until one action can produce clear feedback within a few minutes.",
                    "Record what changed this time instead of asking whether the whole problem is solved.",
                    "Small control matters because new evidence has to compete with the old expectation.",
                ]),
                (("memory distortion",), [
                    "Memory distortion happens because remembering is reconstruction, not a perfect replay of the past.",
                    "Your current mood can spotlight one detail while quieter contradictions disappear from the story.",
                    "In relationships, one warm memory can make an inconsistent pattern look safer than it was.",
                    "Separate what you remember from messages, dates, or behavior you can still verify.",
                    "Ask what happened repeatedly, not which single scene feels strongest today.",
                    "A fair memory includes comfort and conflict without forcing either one to explain everything.",
                    "Write the sequence down before a strong feeling rearranges which detail seems most important.",
                    "The goal is not perfect recall; it is a story that leaves room for conflicting evidence.",
                ]),
                (("mirror neurons",), [
                    "Mirror neurons are often used to explain why observed actions activate related movement systems.",
                    "That does not mean you literally absorb another person's thoughts or emotions.",
                    "The visible effect is simpler: posture, pace, and expression can become easier to copy.",
                    "Notice your jaw, shoulders, and speaking speed after entering a tense conversation.",
                    "Reset one physical cue before deciding whether the mood actually belongs to you.",
                    "Social mirroring can support connection without turning every copied gesture into a secret signal.",
                ]),
                (("analysis paralysis",), [
                    "Analysis paralysis begins when more comparison stops improving a choice and starts delaying it.",
                    "The visible loop is reopening tabs, adding criteria, and asking for one more opinion.",
                    "Each new option creates another imagined mistake your mind wants to prevent.",
                    "Choose the two criteria that matter most and remove options that fail either one.",
                    "Set a decision deadline that matches the consequence instead of the anxiety around it.",
                    "A reversible choice needs a next step, not perfect certainty before you begin.",
                ]),
                (("anchoring effect",), [
                    "The anchoring effect makes the first number or impression a reference point for later judgment.",
                    "A high opening price can make the next price feel reasonable even when it is not.",
                    "In dating, one intense first impression can shape how later behavior gets interpreted.",
                    "Create your own standard before looking at the suggested number, label, or opinion.",
                    "Then compare the evidence with that standard instead of negotiating around the first cue.",
                    "The first fact can start the conversation without deserving control of the conclusion.",
                ]),
                (("comparison trap",), [
                    "The comparison trap starts when someone else's highlight becomes the standard for your ordinary day.",
                    "The visible loop is scrolling, shrinking your progress, then searching for another person to measure.",
                    "Your mind compares their edited outcome with your unedited effort and calls the gap evidence.",
                    "Compare one skill, habit, or result instead of comparing whole identities.",
                    "Turn envy into information by naming the specific thing you actually want to build.",
                    "A useful comparison creates a next action; a harmful one only changes your worth.",
                ]),
            )
            for terms, lines in topic_specific_lines:
                if any(term in lowered for term in terms):
                    return lines
            relationship_terms = (
                "attachment", "kiss", "flirt", "dating", "relationship", "love bombing",
                "breadcrumb", "eye contact", "attraction", "push pull", "situationship",
                "emotional availability", "texting", "crush",
            )
            if any(term in lowered for term in relationship_terms):
                return [
                    f"Notice one visible cue around {subject}.",
                    "Ask three questions. Did it repeat? Did their actions match?",
                    "Did you feel clearer afterward? One intense moment proves little.",
                    "Reliable patterns need time and consistency. Name what actually happened.",
                    "Slow your next reaction. Compare the signal with repeated behavior.",
                    "Healthy attraction can feel exciting. It should not force you to chase clarity.",
                ]
            return [
                f"{subject} usually appears as a visible behavior before it feels like a psychology term.",
                "Name the loop in one sentence: what happened, what you felt, and what you did next.",
                "Change the smallest part first: shorten the choice, move the phone, or pause before reacting.",
                "A useful explanation predicts a behavior you can actually notice in daily life.",
                "Test the idea against two real moments instead of turning one feeling into a diagnosis.",
                f"The payoff of understanding {subject} is one clearer choice, not a dramatic personality label.",
                "If the cue is real, changing the environment should make the reaction easier to interrupt.",
                "Use the pattern as information, then choose the next action deliberately.",
            ]
        return [
            f"A second layer of this story is how {subject} affects everyday choices and long-term outcomes.",
            f"That context is what makes {subject} more useful than a one-line explanation.",
        ]


    _HOOK_TEMPLATES = [
        "What history books conveniently forgot to tell you about {subject} is actually the most important part.",
        "You might think you know {subject}, but the evidence suggests something completely different.",
        "The story of {subject} usually stops at the headline, but what happened next was unexpected.",
        "Historians are still piecing together the truth about {subject}, and it changes everything.",
        "One small detail about {subject} was buried for centuries, until now.",
        "The logic behind {subject} reveals a lot more about human nature than we realize.",
    ]
    
    _CLOSER_TEMPLATES = [
        "Subscribe for more wild secrets history tried to hide.",
        "Subscribe for more shocking true stories you were never taught in school.",
        "Subscribe for more untold stories that sound impossible but are real.",
    ]

    _BRAIN_LENS_OPENERS = list(brain_hooks.OPENERS)

    _BRAIN_LENS_CLOSERS = list(brain_hooks.CLOSERS)

    _HISTORY_OPENERS = [
        "Start with the surviving artifact from {subject}, because the object tells you what the legend skips.",
        "The strongest clue in {subject} is physical: a place, a date, and a consequence.",
        "Before {subject} became a famous story, someone had to solve a practical problem under pressure.",
        "The evidence around {subject} is not abstract; it sits in stone, inscriptions, ruins, or later records.",
        "The myth of {subject} gets louder, but the material evidence is where the story turns.",
        "A single constraint inside {subject} changed what rulers, builders, or soldiers could actually do.",
        "The human part of {subject} starts with the people who had to live with the decision.",
    ]

    _BRAIN_LENS_FRAMES = list(brain_hooks.FRAMES)

    _HISTORY_FRAMES = [
        "artifact clue, political consequence, myth-versus-evidence reveal",
        "place first, power struggle second, human cost third",
        "wrong popular version, overlooked detail, why it changed the aftermath",
        "single decision, chain reaction, later rulers copying the lesson",
        "battlefield or city image, hidden constraint, unexpected outcome",
    ]

    _GENERIC_OPENER_STARTS = (
        "to understand ",
        "there is a fascinating reason",
        "if you've ever felt",
        "modern psychology has a lot to say",
        "is one of those moments",
        "the archive on ",
        "this part of history is easy to miss",
        "a psychology pattern like",
        "once you understand",
        "one tiny trigger can make",
        "the weirdest part of",
        "this is the split second where",
        "the trap in ",
        "your brain can turn",
        "this is where ",
        "the first hard clue in ",
        "start with the artifact from ",
        "the physical clue in ",
        "the strongest evidence for ",
        "before the famous version of ",
    )

    _HISTORY_CLOSERS = [
        "The surviving evidence from {subject} matters because it lets historians test power against legend.",
        "What survives from {subject} is evidence of how people organized power, labor, and belief.",
        "The record of {subject} is strongest where archaeology and written sources can be compared.",
    ]

    def _opener(self, topic: TopicCandidate) -> str:
        import random
        subject = self._spoken_subject(topic)
        if topic.niche_id == "brain_lens":
            lowered = f"{topic.title} {topic.subject} {subject}".lower()
            topic_openers = brain_hooks.TOPIC_OPENERS
            for terms, opener in topic_openers:
                if any(term in lowered for term in terms):
                    return opener
            return f"You notice {subject} in one small moment, before you have time to explain it."
        if topic.niche_id == "ancient_history":
            template = random.choice(self._HISTORY_OPENERS)
            return template.format(subject=subject)
        template = random.choice(self._HOOK_TEMPLATES)
        return template.format(subject=subject)

    def _creative_frame(self, channel: ChannelConfig, topic: TopicCandidate) -> str:
        import random
        if channel.id == "brain_lens":
            return random.choice(self._BRAIN_LENS_FRAMES)
        if channel.id == "ancient_history":
            return random.choice(self._HISTORY_FRAMES)
        return f"{topic.style} with a concrete hook, rising curiosity, and a clear payoff"

    _EXTRA_GENERIC_BITS = (
        "behind the scenes",
        "rarely make it into textbooks",
    )

    def _is_generic_opener(self, text: str) -> bool:
        cleaned = self._clean(text).lower().strip()
        if not cleaned:
            return True
        if cleaned.startswith(self._GENERIC_OPENER_STARTS):
            return True
        if any(bit in cleaned for bit in self._EXTRA_GENERIC_BITS):
            return True
        return brain_hooks.is_generic_opener(cleaned)

    def _hook_strength_score(self, topic: TopicCandidate, text: str) -> float:
        """Prefer concrete tension and evidence over a neutral definition."""
        lowered = self._clean(text).lower()
        score = 0.0
        if re.search(r"\b\d[\d,.]*(?:st|nd|rd|th)?\b", lowered):
            score += 2.0
        concrete = (
            "artifact", "body", "door", "eye", "inscription", "message", "phone",
            "reply", "ruin", "stone", "text", "tomb", "voice", "wall", "weapon",
        )
        score += min(3.0, sum(0.8 for cue in concrete if cue in lowered))
        if any(cue in lowered for cue in ("but", "instead", "until", "before", "after", "without", "then")):
            score += 1.5
        if any(cue in lowered for cue in ("cost", "collapsed", "defeated", "disappeared", "power", "risk", "warning")):
            score += 1.2
        if topic.niche_id == "brain_lens" and lowered.startswith("you"):
            score += 2.0
        if re.match(r"^(?:the )?[^,.]+ (?:is|was|are|were) (?:a|an|the)\b", lowered):
            score -= 1.0
        score -= max(0, self._word_count(text) - 18) * 0.08
        return score

    def _retention_opener(
        self,
        topic: TopicCandidate,
        base_plan: list[ScenePlanItem],
        content_kind: str,
    ) -> str:
        max_words = 22 if content_kind == "video" or topic.niche_id == "ancient_history" else 18

        if topic.niche_id == "ancient_history" and content_kind == "short":
            subject_blob = f"{topic.title} {topic.subject}".lower()
            subject_openers = (
                (("angkor wat",), "In Cambodia, Angkor Wat's five towers and broad moat turned Khmer royal power into a sacred mountain."),
                (("tikal",), "At Tikal, carved stelae name Maya kings while temple pyramids rise above reservoirs and jungle."),
                (("chichen itza",), "At Chichen Itza, one Maya center joined El Castillo, Mesoamerica's largest ball court, and a ritual cenote."),
                (("great zimbabwe",), "Great Zimbabwe rose in southern Africa. Its mortarless walls still stand. Imported beads and gold reveal trade."),
                (("axum", "aksum"), "At Axum in Ethiopia, carved granite towers rose above elite tomb chambers like stone palaces."),
                (("olmec", "colossal head"), "Seventeen colossal basalt heads survive from Olmec centers, and no two rulers wear exactly the same face."),
            )
            for markers, opener in subject_openers:
                if any(marker in subject_blob for marker in markers):
                    return opener

        candidates: list[str] = []
        if base_plan:
            candidates.extend(scene.narration for scene in base_plan[:-1] if scene.narration)

        eligible: list[str] = []
        for candidate in candidates:
            if topic.niche_id == "ancient_history":
                candidate = self._simplify_history_line(candidate)
            line = self._tidy_scene_line(candidate, max_words=max_words)
            if (
                line
                and not self._is_generic_opener(line)
                and self._word_count(line) <= max_words
                and (topic.niche_id != "brain_lens" or self._brain_lens_hook_matches(topic, line))
                and (topic.niche_id != "brain_lens" or self._brain_lens_behavior_first(line))
            ):
                eligible.append(line)

        if eligible:
            return max(eligible, key=lambda line: self._hook_strength_score(topic, line))

        return self._tidy_scene_line(self._opener(topic), max_words=max_words)

    def _brain_lens_hook_matches(self, topic: TopicCandidate, text: str) -> bool:
        combined = f"{topic.title} {topic.subject}".lower()
        hook = self._clean(text).lower()
        anchor_groups = (
            (("decision fatigue",), ("choose", "choice", "menu", "decision")),
            (("love bombing",), ("future", "attention", "affection", "intensity")),
            (("breadcrumb",), ("message", "reply", "disappear", "hope")),
            (("push pull",), ("close", "cold", "return", "pull")),
            (("emotional availability",), ("feel", "guess", "open", "emotion")),
            (("flirting anxiety",), ("flirt", "eye contact", "word", "voice", "crush")),
            (("micro flirting",), ("flirt", "eye", "lean", "smile", "gaze")),
            (("eye contact",), ("eye", "gaze", "look")),
            (("almost kiss",), ("kiss", "lean", "lips", "closer")),
            (("fear conditioning",), ("tense", "warning", "fear", "learned")),
            (("fear of abandonment",), ("reply", "silence", "leaving", "warning", "reassurance")),
            (("orbiting", "checking an ex", "ghosting", "closure"), ("story", "online", "disappear", "answer", "watch")),
            (("slow fading", "dry texting", "double texting", "canceled date"), ("reply", "message", "plan", "shorter", "vaguer")),
            (("voice note intimacy", "late night vulnerability"), ("voice", "message", "headphones", "midnight", "intimate")),
            (("attraction to unavailable", "fear of intimacy"), ("close", "closeness", "distance", "real", "safer")),
            (("apology consistency", "conflict repair"), ("apology", "conflict", "changed", "repair")),
            (("vulnerability reciprocity", "oversharing"), ("share", "honest", "care", "exposed")),
            (("friends with benefits", "consent"), ("chemistry", "question", "mutual", "arrangement")),
            (("sexual tension", "kissing chemistry", "first kiss"), ("space", "body", "kiss", "chemistry", "compatibility")),
            (("crush idealization", "limerence"), ("details", "mind", "person", "fantasy")),
            (("future faking", "benching"), ("future", "plan", "available", "build")),
            (("social media jealousy",), ("story", "like", "follow", "online", "threat")),
            (("rebound chemistry",), ("new connection", "spark", "relief", "loss")),
            (("exclusivity", "talking stage"), ("talk", "asking", "confused", "relationship")),
            (("relationship pacing", "mutual effort", "emotional safety"), ("pace", "effort", "care", "mutual")),
            (("anxious attachment",), ("reply", "text", "reassurance", "mood")),
            (("reply time anxiety", "texting anxiety"), ("timestamp", "reply", "message", "delay")),
            (("avoidant attachment",), ("close", "distance", "space", "withdraw")),
            (("attachment styles",), ("closeness", "distance", "reply", "mood")),
            (("emotional contagion",), ("room", "mood", "tense", "voice")),
            (("emotional regulation",), ("message", "reply", "body", "judgment")),
            (("attention span",), ("phone", "talking", "focus", "scroll")),
            (("fawn response",), ("agree", "disappointed", "want", "yes")),
            (("halo effect", "first impression"), ("attractive", "trait", "red flag", "impression")),
            (("impostor syndrome",), ("praised", "mistake", "credit", "success")),
            (("learned helplessness",), ("stop trying", "attempt", "failure", "proof")),
            (("memory distortion",), ("memory", "replay", "relationship", "remember")),
            (("mirror neurons",), ("jaw", "copy", "mood", "body")),
            (("analysis paralysis",), ("option", "comparison", "choice", "decide")),
            (("anchoring effect",), ("first number", "standard", "first impression", "price")),
            (("comparison trap",), ("online", "compare", "polished", "smaller")),
            (("attachment",), ("reply", "distance", "close", "mood")),
            (("dopamine", "reward loop"), ("reward", "check", "scroll", "again")),
        )
        for topic_terms, hook_terms in anchor_groups:
            if any(term in combined for term in topic_terms):
                return any(term in hook for term in hook_terms)
        subject_tokens = {
            token
            for token in re.findall(r"[a-z]{4,}", topic.subject or topic.title or "")
            if token not in {"that", "this", "your", "behind", "feels", "reveals", "attention"}
        }
        return not subject_tokens or any(token in hook for token in subject_tokens)

    def _brain_lens_behavior_first(self, text: str) -> bool:
        lowered = self._clean(text).lower()
        cues = (
            "phone", "text", "reply", "message", "scroll", "check", "reread", "eye", "gaze",
            "look", "smile", "voice", "jaw", "room", "silence", "pause", "talking", "agree",
            "praised", "mistake", "memory", "option", "choice", "number", "red flag", "distance",
            "closeness", "close", "cold", "return", "pull", "lean", "touch", "mood", "body",
            "stomach", "chest", "breath", "trying", "attempt", "story", "view", "headphones",
            "midnight", "apology", "conflict", "share", "honest", "chemistry", "question",
            "arrangement", "compatibility", "details", "fantasy", "future", "plan", "calendar",
            "available", "promise", "pace", "effort", "care", "loss", "spark", "date", "label",
        )
        actor_first = re.match(
            r"^(?:you|they|their|someone|one |closeness|chemistry|the (?:message|reply|date|phone|room|silence|space|(?:new )?connection|apology|chemistry|story|plan|future)|a (?:message|reply|date|phone|look|voice))\b",
            lowered,
        )
        return bool(actor_first) and any(cue in lowered for cue in cues)

    def editorial_quality_issues(
        self,
        topic: TopicCandidate,
        beats: list[str] | None = None,
        content_kind: str = "short",
    ) -> list[str]:
        """Model-independent editorial gate for scripts from any AI provider."""
        lines = [self._sentence(line) for line in (beats or topic.narration_beats or self._sentences(topic.narration))]
        lines = [line for line in lines if line]
        issues: list[str] = []
        if not lines:
            return ["script has no usable narration beats"]

        full_text = " ".join(lines)
        lowered = full_text.lower()
        for index, line in enumerate(lines, start=1):
            bad_line_issue = self._bad_ai_line_issue(line)
            if bad_line_issue:
                issues.append(f"beat {index} {bad_line_issue}")
        advertiser_unsafe = (
            "revenge porn", "non-consensual", "force them to", "make them obsessed",
            "seduce anyone", "pickup artist", "humiliate them", "body shame",
            "explicit sex", "naked photo", "leaked nude",
        )
        if any(term in lowered for term in advertiser_unsafe):
            issues.append("script uses advertiser-unsafe or coercive framing")
        if re.search(
            r"\b(?:this|that|one) (?:sign|cue|text|look|voice change) (?:proves|guarantees|always means)\b",
            lowered,
        ):
            issues.append("script turns an ambiguous cue into unsupported certainty")
        if re.search(r"\b(?:secretly cheating|affair exposed|caught cheating)\b", lowered):
            issues.append("script makes an unverified gossip allegation")

        normalized_lines = [
            {word for word in re.findall(r"[a-z]{4,}", line.lower()) if word not in self._EDITORIAL_STOP_WORDS}
            for line in lines
        ]
        for index in range(1, len(normalized_lines)):
            left, right = normalized_lines[index - 1], normalized_lines[index]
            union = left | right
            if union and len(left & right) / len(union) >= 0.72:
                issues.append(f"beats {index} and {index + 1} repeat the same idea")

        if content_kind == "short":
            if len(lines) < 5:
                issues.append("short needs at least five retention beats")
            opener = lines[0]
            if self._is_generic_opener(opener):
                issues.append("short opens with a generic setup")
            if topic.niche_id == "brain_lens":
                if not self._brain_lens_behavior_first(opener):
                    issues.append("Brain Lens hook must begin with a visible behavior or body cue")
                if not self._brain_lens_hook_matches(topic, opener):
                    issues.append("Brain Lens hook does not match the selected topic")
                payoff_terms = (
                    "answer", "boundary", "clarity", "confidence", "consent", "consistency",
                    "effort", "mutual", "pattern", "reciprocity", "respect", "safe", "trust",
                )
                if not any(term in lines[-1].lower() for term in payoff_terms):
                    issues.append("Brain Lens ending lacks a practical or relational payoff")
            elif topic.niche_id == "ancient_history":
                subject_tokens = {
                    token
                    for token in re.findall(r"[a-z]{4,}", self._spoken_subject(topic).lower())
                    if token not in self._EDITORIAL_STOP_WORDS
                }
                opener_tokens = set(re.findall(r"[a-z]{4,}", opener.lower()))
                evidence_cues = pipeline_quality.EVIDENCE_CUES
                if not (subject_tokens & opener_tokens) and not any(cue in opener.lower() for cue in evidence_cues):
                    issues.append("Ancient History hook lacks its subject or a concrete surviving clue")
                payoff_tokens = set(
                    re.findall(r"[a-z]{4,}", " ".join(lines[-2:]).lower())
                )
                if not (subject_tokens & payoff_tokens):
                    issues.append("Ancient History payoff does not return to the core subject")

            if not pipeline_quality.has_midpoint_turn(lines):
                issues.append("short lacks a midpoint contrast, consequence, or reframe")
        return list(dict.fromkeys(issues))

    def _brain_lens_visual_search_terms(
        self,
        subject: str,
        narration: str,
        content_kind: str = "short",
    ) -> list[str]:
        lowered = narration.lower()
        topic_blob = f"{subject} {narration}".lower()
        if any(term in lowered for term in ("perform indifference", "post for a reaction", "false power")):
            primary = "adult couple sitting apart after phone argument emotional distance"
            secondary = "adult couple calmly ending a social media conflict conversation"
        elif content_kind == "video" and any(
            term in lowered
            for term in ("strict limit", "specific behavior", "what happened more than once")
        ):
            primary = "adult couple leaving a date separately thoughtful relationship uncertainty"
            secondary = "adult couple standing at doorway after date considering next step"
        elif content_kind == "video" and any(
            term in lowered
            for term in ("rewind to the first hour", "first hour when the loop")
        ):
            primary = "adult couple walking together on an early date city street realistic"
            secondary = "adult couple arriving at cafe first date body language"
        elif content_kind == "video" and any(
            term in lowered
            for term in ("common impulse", "checking can train", "interrupt the sequence")
        ):
            primary = "young adult putting phone face down returning to daily routine after stress"
            secondary = "young adult focusing on work after unanswered phone message"
        elif content_kind == "video" and any(
            term in lowered
            for term in ("here is the reset", "catch the first cue", "reality to answer")
        ):
            primary = "adult couple walking outdoors after honest relationship decision calm"
            secondary = "adult couple leaving a park together after resolving uncertainty"
        elif any(term in lowered for term in ("bounded experiment", "seven days", "record urges")):
            primary = "adult couple planning a calm weekly routine with calendar and phone"
            secondary = "adult couple creating healthy space while keeping normal routines"
        elif any(term in lowered for term in ("professional", "therapy", "therapist", "counseling", "mental-health")):
            primary = "licensed therapist talking with young adult in calm office"
            secondary = "supportive counseling conversation adult mental health"
        elif any(term in lowered for term in ("unclench", "lengthen the exhale", "calmer body", "regulate before", "breathing exercise", "regaining composure")):
            if content_kind == "video":
                primary = "young adult doing slow breathing beside phone after emotional stress"
                secondary = "young adult walking outdoors calmly after putting phone away"
            else:
                primary = "young adult calming breathing after stressful relationship moment"
                secondary = "close up emotional adult face regaining composure"
        elif any(term in lowered for term in ("write down", "write only", "two short columns", "observable facts", "record facts", "map the sequence", "journal", "five part map")):
            if content_kind == "video":
                primary = "adult couple writing relationship facts in notebooks beside phone"
                secondary = "adult couple reviewing written notes after relationship uncertainty"
            else:
                primary = "young adult journaling relationship patterns beside phone notebook"
                secondary = "close up hands writing facts in notebook beside smartphone"
        elif any(
            term in lowered
            for term in (
                "charged reunion", "repair asks for more", "name what happened",
                "sexual tension never substitutes", "automatic repair",
            )
        ):
            primary = "adult couple rebuilding trust in a calm conversation after conflict"
            secondary = "adult couple discussing repair and accountability after an argument"
        elif any(
            term in lowered
            for term in (
                "listen to the words", "specificity", "read the answer",
                "grade the pattern", "tone alone",
            )
        ):
            primary = "adult couple listening carefully in an honest relationship conversation"
            secondary = "adult couple calm face to face discussion with eye contact"
        elif any(term in lowered for term in ("behavior scoreboard", "plans kept", "follow-through", "ordinary week", "scheduling")):
            primary = "adult couple planning weekly schedule with calendar at kitchen table"
            secondary = "adult couple reviewing a notebook and plans together at home"
        elif any(term in lowered for term in ("your worth", "not attractive enough", "impossible to love")):
            if content_kind == "video":
                primary = "young adult reconnecting with friends after dating disappointment"
                secondary = "self assured young adult walking outdoors after emotional setback"
            else:
                primary = "confident young adult reconnecting with friends after dating disappointment"
                secondary = "self assured young adult walking outdoors natural light"
        elif any(term in lowered for term in ("suddenly reappear", "a return is", "during a comeback", "the comeback", "warmth without accountability")):
            if content_kind == "video":
                primary = "adult couple emotionally distant one receiving unexpected phone message"
                secondary = "adult couple discussing an unexpected relationship message calmly"
            else:
                primary = "young adult receiving unexpected relationship message on phone calmly"
                secondary = "close up surprising phone notification thoughtful adult reaction"
        elif any(
            term in lowered
            for term in (
                "usable boundary", "boundary sounds like", "boundary under your control",
                "states what access", "set a boundary", "protects time",
            )
        ):
            if content_kind == "video":
                primary = "adult couple setting a calm boundary in relationship conversation"
                secondary = "adult couple creating respectful distance after honest conversation"
            else:
                primary = "confident young adult setting calm boundary in relationship conversation"
                secondary = "self assured young adult walking away from phone natural light"
        elif any(
            term in lowered
            for term in (
                "dating app", "open the app", "using the app", "swip", "profile", "match",
                "active conversations", "online dating", "comparing dozens",
            )
        ):
            if content_kind == "video":
                primary = "adult couple using smartphones sitting apart relationship distance"
                secondary = "adult couple looking at a dating app profile on phone together"
            else:
                primary = "young adult scrolling dating app profiles on smartphone realistic"
                secondary = "close up smartphone dating app choices thoughtful adult hands"
        elif re.search(r"\b(?:phone|text|reply|message|notification|timestamp)s?\b", lowered) or "typing bubble" in lowered:
            if any(term in lowered for term in ("night", "silence", "slow reply", "wait", "uncertainty", "missing reply")):
                if content_kind == "video":
                    primary = "adult couple emotionally distant one checking phone after unanswered relationship message"
                    secondary = "adult couple apart after ignored phone message relationship"
                else:
                    primary = "young adult alone at night checking phone waiting for relationship reply"
                    secondary = "close up smartphone unread message anxious hands realistic"
            elif any(term in lowered for term in ("put the phone down", "delay", "routine", "check messages")):
                if content_kind == "video":
                    primary = "adult couple creating calm space while one puts phone face down"
                    secondary = "adult couple returning to routine after phone tension"
                else:
                    primary = "confident young adult placing phone face down returning to work"
                    secondary = "young adult ignoring phone notification calm focused routine"
            else:
                if content_kind == "video":
                    primary = "adult couple reading a relationship text message together emotional reaction"
                    secondary = "adult couple discussing smartphone message thoughtfully"
                else:
                    primary = "young adult reading relationship text message emotional reaction"
                    secondary = "close up smartphone conversation thoughtful adult hands"
        elif any(term in lowered for term in ("ask clearly", "ask a question", "ask at a calm time", "direct question", "honest conversation", "what are you looking for", "clarity")):
            primary = "young adult couple calm honest relationship conversation eye contact"
            secondary = "stylish adult couple discussing expectations at cafe"
        elif any(term in lowered for term in ("boundary", "self respect", "protect your time", "unlimited", "access")):
            if content_kind == "video":
                primary = "adult couple setting a calm boundary in relationship conversation"
                secondary = "adult couple creating respectful distance after honest conversation"
            else:
                primary = "confident young adult setting calm boundary in relationship conversation"
                secondary = "self assured young adult walking away from phone natural light"
        elif any(term in lowered for term in ("ordinary moment", "tired evening", "scheduling", "misunderstanding", "repair")):
            primary = "young adult couple cooking dinner together ordinary home routine"
            secondary = "adult couple grocery shopping together relaxed everyday relationship"
        elif any(term in lowered for term in ("kiss", "chemistry", "magnetic", "attraction", "playful tension", "physical intimacy")):
            primary = "adult couple laughing together on romantic date outdoors playful chemistry"
            secondary = "adult couple holding hands walking romantic natural light"
        elif any(term in lowered for term in ("sleep", "work", "friendships", "cost of the loop", "daily functioning")):
            primary = "young adult distracted at work by phone relationship anxiety"
            secondary = "tired young adult awake at night phone notification"
        elif "silent treatment" in topic_blob or any(term in lowered for term in ("sudden silence", "disconnection", "withholding connection")):
            primary = "adult couple sitting apart after conflict emotional distance"
            secondary = "adult couple calmly reconnecting after disagreement"
        elif any(term in topic_blob for term in ("almost kiss", "eye contact", "flirting", "body language", "voice change", "micro flirting")):
            primary = "stylish adult couple flirting eye contact romantic tension"
            secondary = "adult couple on date subtle mutual body language"
        elif any(term in topic_blob for term in ("crush idealization", "limerence")):
            if any(term in lowered for term in ("actually did", "two lists", "best guesses", "missing information")):
                primary = "young adult couple on early date talking and getting to know each other"
                secondary = "adult couple first dates realistic conversation noticing details"
            elif any(term in lowered for term in ("real plan", "reliable action", "routine", "ordinary life")):
                primary = "young adult couple making a real date plan together calendar"
                secondary = "adult couple sharing ordinary daily life realistic relationship"
            elif any(term in lowered for term in ("boundary", "misunderstanding", "boring day")):
                primary = "young adult couple handling calm relationship boundary conversation"
                secondary = "adult couple repairing small misunderstanding at home"
            else:
                primary = "stylish adult couple first date romantic eye contact realistic"
                secondary = "young adult couple early dating chemistry thoughtful conversation"
        elif any(term in topic_blob for term in (
            "love bombing", "situationship", "romantic uncertainty", "avoidant attachment",
            "push pull", "mixed signal", "future faking", "benching", "slow fading",
            "breadcrumb", "ghosting", "orbiting", "relationship pacing", "mutual effort",
        )):
            primary = "young adult couple relationship tension serious conversation"
            secondary = "young adult couple sitting apart uncertain relationship"
        elif any(term in topic_blob for term in ("rejection sensitivity", "fear of abandonment", "anxious attachment")):
            if content_kind == "video":
                primary = "adult couple emotionally distant after canceled date checking phone"
                secondary = "adult couple calmly discussing relationship uncertainty"
            else:
                primary = "young adult worried after canceled date checking phone"
                secondary = "young adult calming down after relationship uncertainty"
        elif any(term in lowered for term in ("relationship", "closeness", "distance", "dating", "consistency", "trust")):
            primary = "adult couple realistic relationship conversation body language"
            secondary = "healthy young adult couple relaxed everyday connection"
        elif any(term in lowered for term in ("choice", "decision", "option", "focus", "attention")):
            primary = "young adult making calm decision at desk beside phone"
            secondary = "thoughtful adult reviewing notes and making a decision"
        else:
            primary = "young adult thoughtful emotional portrait"
            secondary = "young adult calm confident lifestyle portrait"
        return [
            primary,
            secondary,
            f"adult age 25 to 35 {subject} realistic lifestyle b roll",
        ]

    def _closer(self, topic: TopicCandidate) -> str:
        import random
        if topic.niche_id == "brain_lens":
            lowered = f"{topic.title} {topic.subject}".lower()
            if "voice change" in lowered:
                return "A voice shift starts the question; reciprocity and clear interest give you a safer answer."
            topic_closers = (
                (("text", "reply", "breadcrumb"), "One reply gives one data point. Repeated care reveals the pattern."),
                (("almost kiss", "eye contact", "flirt", "body language"), "Tension creates the spark; returned effort tells you whether it is mutual."),
                (("silent treatment",), "Healthy space brings clarity back; punishment makes you beg for contact."),
                (("love bombing", "mixed signal", "push pull"), "Intensity gets your attention; consistency is what deserves your trust."),
                (("situationship", "almost relationship", "romantic uncertainty"), "Potential keeps hope alive; shared intention is what creates a relationship."),
                (("attachment",), "One anxious moment is a cue; repeated care is the evidence."),
                (("chemistry", "compatibility"), "Chemistry starts the question; compatibility answers it over time."),
                (("crush idealization", "limerence"), "Let the crush stay exciting; let real behavior decide who they actually are."),
                (("self respect", "emotional availability", "too much"), "The right connection does not require you to disappear inside it."),
            )
            for terms, closer in topic_closers:
                if any(term in lowered for term in terms):
                    return closer
            return random.choice(self._BRAIN_LENS_CLOSERS)
        if topic.niche_id == "ancient_history":
            subject = self._spoken_subject(topic)
            subject_blob = f"{topic.title} {topic.subject}".lower()
            if "angkor wat" in subject_blob:
                return "At Angkor Wat, moat, galleries, towers, and bas-reliefs made Khmer kingship visible as a sacred landscape."
            if "tikal" in subject_blob:
                return "At Tikal, stelae, reservoirs, and temple pyramids made dynastic memory and urban power visible across the Maya city."
            if "chichen itza" in subject_blob:
                return "At Chichen Itza, pyramid, ball court, and cenote fused Maya architecture, ritual, and public power."
            if "great zimbabwe" in subject_blob:
                return "Walls and trade goods reveal wealth. Soapstone birds reveal local belief. Great Zimbabwe joined global exchange."
            if "olmec" in subject_blob and any(term in subject_blob for term in ("head", "colossal")):
                return "Together, basalt, transport, and individualized faces make the heads monuments to organized labor and Olmec rulership."
            if "axum" in subject_blob and any(term in subject_blob for term in ("obelisk", "stele", "stela")):
                return "At Axum, carved doors, window-like tiers, and elite tombs turned monumental stone into a public claim of royal power."
            if "nubian" in subject_blob and "pyramid" in subject_blob:
                return "At Meroe, steep-sided pyramids made Kushite royal burial traditions visible in stone above underground chambers."
            if "masada" in subject_blob:
                return "At Masada, Roman camps, the assault ramp, and Josephus's account must be tested against one another."
            if "machu picchu" in subject_blob:
                return "At Machu Picchu, drainage, terraces, and stonework made the mountain city possible by turning a steep slope into stable ground."
            if "qin" in subject_blob and any(term in subject_blob for term in ("tomb", "mausoleum")):
                return "Around the unopened tomb, soldiers, chariots, walls, and pits reveal the scale of power built for China's first emperor."
            if "sogdian" in subject_blob and "merchant" in subject_blob:
                return "Letters found near Dunhuang and murals at Samarkand preserve this diaspora in merchants' messages and diplomatic art."
            if any(term in subject_blob for term in ("battle", "war", "siege", "revolt", "sack")):
                return f"The outcome of {subject} mattered because it changed the balance of power and shaped what both sides attempted next."
            historical_blob = f"{topic.hook} {topic.narration}".lower()
            if any(term in historical_blob for term in ("temple", "palace", "city", "wall", "stone", "architecture", "built")):
                article_markers = (
                    "battle ", "code ", "indus valley civilization", "kingdom ", "library ",
                    "mausoleum ", "nubian pyramids", "oracle ", "terracotta army", "tomb ",
                    "ziggurat ",
                )
                subject_phrase = subject
                if not subject.lower().startswith("the ") and any(marker in subject.lower() for marker in article_markers):
                    subject_phrase = f"the {subject}"
                return f"Surviving structures linked to {subject_phrase} show how power, belief, and labor shaped the site."
            return random.choice(self._HISTORY_CLOSERS).format(subject=subject)
        return random.choice(self._CLOSER_TEMPLATES)

    def _curated_short_scene_plan(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        content_kind: str,
    ) -> list[ScenePlanItem]:
        """Return evidence-bounded scene plans for high-risk editorial topics."""
        if content_kind != "short":
            return []

        topic_blob = f"{topic.subject} {topic.title}".lower()
        specs: list[tuple[str, str, list[str]]] = []
        if channel.id == "ancient_history" and "great zimbabwe" in topic_blob:
            specs = [
                (
                    "Colonial writers denied Great Zimbabwe's origins. Finds confirmed its African origins independently.",
                    "COLONIAL CLAIM DISPROVED",
                    ["Great Zimbabwe early archaeology colonial denial African builders"],
                ),
                (
                    "Shona ancestors built Great Zimbabwe. It became a key African capital. Construction spanned 1100 through 1450 CE.",
                    "SHONA BUILDERS, 1100-1450",
                    ["Great Zimbabwe Great Enclosure dry stone walls"],
                ),
                (
                    "Granite walls rose without any mortar. Cattle may have fueled elite wealth. Its state reached rich gold lands.",
                    "DRY STONE, CATTLE, GOLD",
                    ["Great Zimbabwe mortarless granite wall conical tower"],
                ),
                (
                    "Excavations found imported beads and ceramics. They reveal broad Indian Ocean trade.",
                    "INDIAN OCEAN TRADE EVIDENCE",
                    ["Great Zimbabwe imported beads ceramics Indian Ocean trade"],
                ),
                (
                    "Birds may have marked royal authority. Even their exact meaning remains unknown.",
                    "THE SOAPSTONE BIRDS",
                    ["Zimbabwe Bird soapstone sculpture Great Zimbabwe"],
                ),
                (
                    "The evidence confirms African origins. Great Zimbabwe linked distant trade.",
                    "AN AFRICAN CAPITAL",
                    ["Great Zimbabwe panoramic ruins Zimbabwe"],
                ),
            ]
        elif channel.id == "brain_lens" and "reply time anxiety" in topic_blob:
            specs = [
                (
                    "Their reply arrives late on-screen. But one timestamp proves nothing.",
                    "TIMESTAMP IS NOT REJECTION",
                    ["young adult checking delayed phone reply anxious realistic"],
                ),
                (
                    "One study surveyed 302 partnered undergraduates. Participants reported attachment and messaging patterns.",
                    "302 PARTNERED UNDERGRADUATES",
                    ["adult couple comparing texting preferences calm conversation"],
                ),
                (
                    "More anxious participants wanted more messages. They wanted more than came.",
                    "ONE STUDY, NOT A VERDICT",
                    ["young adult replying quickly to partner message phone"],
                ),
                (
                    "They also replied faster than partners. Those findings describe associations, not causes.",
                    "ASSOCIATION, NOT CAUSATION",
                    ["young adult waiting for phone reply continuing normal routine"],
                ),
                (
                    "This sample limits broad claims overall. Messaging preferences and circumstances still vary.",
                    "ONE SAMPLE HAS LIMITS",
                    ["adult reviewing practical urgent phone message"],
                ),
                (
                    "Check urgency and their usual pace.",
                    "CHECK URGENCY AND CONTEXT",
                    ["adult couple everyday messaging routine realistic"],
                ),
                (
                    "Compare plans, reasons, and later actions.",
                    "COMPARE FOLLOW-THROUGH",
                    ["adult couple following through on plans healthy relationship"],
                ),
                (
                    "Ask clearly when context is missing. Judge the pattern, not one reply.",
                    "JUDGE THE PATTERN",
                    ["adult couple clear respectful relationship conversation"],
                ),
            ]

        frame_hint = "vertical short frame"
        subject = self._display_subject(
            topic.subject or self._headline_subject(topic.title) or topic.title,
            channel.id,
        )
        return [
            ScenePlanItem(
                narration=narration,
                visual_text=visual_text,
                search_terms=search_terms,
                preferred_image_url="",
                visual_prompt=(
                    f"{subject}. {visual_text}. evidence-led documentary image, {frame_hint}"
                    if channel.id == "ancient_history"
                    else f"{subject}. {visual_text}. realistic adult relationship b-roll, {frame_hint}"
                ),
            )
            for narration, visual_text, search_terms in specs
        ]

    def _fallback_scene_plan(self, topic: TopicCandidate, content_kind: str = "short") -> list[ScenePlanItem]:
        subject = self._display_subject(topic.subject or self._headline_subject(topic.title) or topic.title, topic.niche_id)
        if topic.niche_id == "brain_lens":
            lines = [self._opener(topic)]
            body_limit = 6 if content_kind == "short" else 16
            for sentence in self._sentences(topic.narration):
                if self._word_count(sentence) >= 6:
                    lines.append(sentence)
                if len(lines) >= body_limit + 1:
                    break
            for extra in self._video_expansion_lines(topic, self._spoken_subject(topic)):
                if len(lines) >= body_limit + 1:
                    break
                if self._word_count(extra) >= 6:
                    lines.append(extra)
            lines.append(self._closer(topic))
        else:
            lines = [self._opener(topic)]
            lines.extend([self._sentence(c) for c in topic.visual_captions if self._sentence(c)])
            lines.append(self._closer(topic))
        frame_hint = "landscape documentary frame" if content_kind == "video" else "vertical short frame"
        out = []
        for line in lines:
            out.append(
                ScenePlanItem(
                    narration=line,
                    visual_text=self._captionize(line) or subject,
                    search_terms=[subject],
                    preferred_image_url="",
                    visual_prompt=f"{subject}. documentary still, {frame_hint}",
                )
            )
        return out

    def _sentences(self, text: str) -> list[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+", self._clean(text)) if part.strip()]

    def _reduce_subject_repetition(self, text: str, subject: str, channel_id: str) -> str:
        subject = re.sub(r"\s+", " ", subject or "").strip()
        if channel_id != "ancient_history" or not subject or len(subject.split()) < 2:
            return text
        if self._word_count(text) <= 140:
            return text
        replacements = ("this clue", "the evidence", "the site", "the episode", "the surviving record")
        counter = {"count": 0}

        def repl(match: re.Match) -> str:
            counter["count"] += 1
            if counter["count"] <= 2:
                return match.group(0)
            return replacements[(counter["count"] - 3) % len(replacements)]

        return re.sub(r"\b" + re.escape(subject) + r"\b", repl, text, flags=re.IGNORECASE)

    def _video_sections_from_narration(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[ScenePlanItem]:
        sentences = self._sentences(topic.narration)
        if not sentences:
            return []

        target_words = self._target_words(channel, content_kind="video")
        target_sections = max(12, min(22, int(target_words / 75)))
        section_target_words = max(55, int(target_words / target_sections))
        frame_hint = "landscape documentary frame"
        sections: list[str] = []
        current: list[str] = []
        current_words = 0
        for sentence in sentences:
            words = self._word_count(sentence)
            if current and current_words + words > section_target_words:
                sections.append(" ".join(current))
                current = []
                current_words = 0
            current.append(sentence)
            current_words += words
        if current:
            sections.append(" ".join(current))

        if len(sections) < 8:
            return []

        cold_open = self._long_cold_open_sections(channel, topic, subject)
        sections = cold_open + sections
        out: list[ScenePlanItem] = []
        for idx, section in enumerate(sections[:22]):
            caption = self._captionize(section) or subject
            if idx == 0:
                caption = "Start here"
            elif idx == 1:
                caption = "The real stakes"
            elif idx == 2:
                caption = "What changes"
            elif idx == len(sections[:22]) - 1:
                caption = "Why it still matters"
            out.append(
                ScenePlanItem(
                    narration=self._sentence(section),
                    visual_text=caption,
                    search_terms=(
                        self._brain_lens_visual_search_terms(
                            subject,
                            section,
                            content_kind="video",
                        )
                        if channel.id == "brain_lens"
                        else [subject, caption]
                    ),
                    preferred_image_url="",
                    visual_prompt=f"{subject}. {caption}. cinematic documentary b-roll, detailed visual evidence, {frame_hint}",
                )
            )
        return out

    def _long_cold_open_sections(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[str]:
        display_subject = self._display_subject(subject, channel.id)
        spoken_subject = self._spoken_subject(topic)
        if channel.id == "brain_lens":
            return [
                "Your body reacts before your explanation catches up.",
                f"With {spoken_subject}, the habit is visible first: a pause, a scan, a fake smile, or a reply you rewrite twice.",
                "Watch the cue, the cost, and the reset before the loop gets louder.",
            ]
        if channel.id == "ancient_history":
            return [
                f"Start with {display_subject} as a physical case file, not a legend.",
                "The first question is simple: what object, wall, inscription, landscape, or written source still exists?",
                "Once that evidence is visible, the famous version has to compete with the material record.",
            ]
        return [
            f"The first clue inside {display_subject} is easy to miss.",
            "The real story is in the pressure, the consequence, and the choice that follows.",
            "Stay for the turn that changes how the whole topic feels.",
        ]

    def _long_scene(
        self,
        channel_id: str,
        subject: str,
        narration: str,
        caption: str,
    ) -> ScenePlanItem:
        if channel_id == "brain_lens":
            search_terms = self._brain_lens_visual_search_terms(
                subject,
                narration,
                content_kind="video",
            )
            visual_prompt = (
                f"{subject}. {caption}. realistic young adult relationship moment, expressive body language, "
                "cinematic lifestyle b-roll, professional landscape composition"
            )
        else:
            search_terms = [
                f"{subject} {caption}",
                f"{subject} archaeology",
                f"{subject} historical site",
                subject,
            ]
            visual_prompt = (
                f"{subject}. {caption}. museum-grade historical documentary image, material evidence, "
                "period-accurate detail, professional landscape composition"
            )
        return ScenePlanItem(
            narration=self._sentence(narration),
            visual_text=caption,
            search_terms=list(dict.fromkeys(search_terms)),
            preferred_image_url="",
            visual_prompt=visual_prompt,
        )

    def _caption_safe_section_issues(
        self,
        sections: list[tuple[str, str]],
        *,
        min_words: int = 3,
        max_words: int = 8,
    ) -> list[str]:
        """Report sentences that cannot remain whole under the caption word cap."""
        issues: list[str] = []
        for scene_index, (caption, narration) in enumerate(sections, start=1):
            sentences = [
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", narration.strip())
                if sentence.strip()
            ]
            for sentence_index, sentence in enumerate(sentences, start=1):
                word_count = self._word_count(sentence)
                if not min_words <= word_count <= max_words:
                    issues.append(
                        f"scene {scene_index} ({caption!r}) sentence {sentence_index} "
                        f"has {word_count} words; expected {min_words}-{max_words}: {sentence}"
                    )
        return issues

    def _require_caption_safe_sections(
        self,
        sections: list[tuple[str, str]],
        *,
        label: str,
    ) -> list[tuple[str, str]]:
        """Fail before TTS when a curated long script would force a clause split."""
        issues = self._caption_safe_section_issues(sections)
        if issues:
            preview = "; ".join(issues[:4])
            raise ValueError(f"{label} caption-safe script check failed: {preview}")
        return sections

    def _history_fact_category(self, text: str) -> str:
        lowered = text.lower()
        categories = (
            ("war", ("battle", "siege", "army", "soldier", "invasion", "war", "destroyed", "conquer")),
            ("construction", ("built", "construct", "wall", "stone", "terrace", "architecture", "canal", "road")),
            ("trade", ("trade", "harbor", "harbour", "route", "merchant", "ship", "port")),
            ("writing", ("inscription", "tablet", "scroll", "manuscript", "written", "text", "relief")),
            ("burial", ("tomb", "burial", "grave", "mausoleum", "funer")),
            ("religion", ("temple", "ritual", "sacred", "priest", "god", "religion")),
            ("power", ("king", "queen", "emperor", "ruler", "dynasty", "empire", "republic")),
            ("archaeology", ("archaeolog", "excavat", "discovered", "artifact", "evidence", "remains")),
            ("chronology", ("century", " bc", " ad", "before", "after", "during", "founded")),
            ("landscape", ("river", "coast", "mountain", "desert", "valley", "island", "plain", "slope")),
        )
        for category, markers in categories:
            if any(
                re.search(
                    r"\b" + re.escape(marker.strip()).replace(r"\ ", r"\s+") + r"\w*\b",
                    lowered,
                )
                for marker in markers
            ):
                return category
        return "evidence"

    def _lachish_long_video_plan(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[ScenePlanItem]:
        """Build a source-led Lachish documentary instead of generic expansion.

        Lachish is unusually well documented by a surviving attack site, Assyrian
        palace reliefs, royal inscriptions, and biblical texts. A fixed evidence
        sequence lets the script explain those relationships without repeating
        all-purpose war, trade, and monument boilerplate.
        """
        sections = [
            (
                "Propaganda meets the ground",
                "Sennacherib commissioned reliefs after Lachish fell. His palace displayed their carved victory. Yet Lachish preserved another record underground. Assyria captured this fortified Judean center. The conquest happened during 701 BCE. Royal panels turn violence into glory. Archaeology preserves ramps, weapons, and burning. Written accounts preserve competing memories. These sources can confirm each other. They can also expose important omissions. The question concerns more than victory. It concerns whose costs became invisible.",
            ),
            (
                "Why Lachish mattered",
                "Lachish occupied a high Shephelah mound. This foothill zone shaped regional movement. Judah's hills rose farther east. The coastal plain opened westward. Roads connected farms and fortified towns. Other routes led toward Egypt. Still other roads approached Jerusalem. Lachish guarded this connected landscape. Its walls protected supplies and movement. They also supported political control. Breaking Lachish increased pressure across Judah. Regional resistance became much harder.",
            ),
            (
                "Assyria comes to Judah",
                "Sennacherib entered Judah during wider revolt. Rebels challenged Assyrian rule and tribute. His campaign struck multiple fortified towns. King Hezekiah confronted danger from Jerusalem. Assyrian power required battlefield skill. It also required imperial logistics. Troops and engineers needed coordinated movement. Animals carried food and weapons. Messages crossed the long imperial network. That machinery converged upon one city. Lachish concentrated Assyria's organized power. Archaeology still traces that concentration.",
            ),
            (
                "The southwest attack point",
                "Assyria targeted Lachish's southwest corner. The slope offered attackers an approach. High walls still blocked their advance. Defenders fired downward from above. Surviving topography fixes the tactical setting. It also anchors the palace imagery. The attack followed a constrained angle. Ancient roadways limited possible movement. Camp space shaped Assyrian choices. City walls shaped Judean choices. Both armies faced the same terrain. Their options remained profoundly unequal.",
            ),
            (
                "Building the siege ramp",
                "Assyrian engineers built a massive ramp. Stone and earth formed its body. The structure rose toward city walls. Soldiers could advance across that surface. Heavy siege engines followed behind them. Construction research reveals organized collection. Transport crews carried those materials. Other troops protected repeated construction work. Everyone labored beneath defensive fire. This route turned manpower into pressure. It focused that pressure precisely. The city resisted throughout its construction.",
            ),
            (
                "Judah builds back",
                "Judah's defenders built an internal counter-ramp. It stood opposite Assyria's rising approach. That response reveals an engineering contest. Attackers raised their pathway outside. Defenders reinforced the threatened wall behind. Added material could absorb heavy impacts. It could also delay breaching. The counter-ramp reveals tactical understanding. Defenders recognized Assyria's chosen target. They answered with their available resources. Their solution bought resistance time. It could not erase imperial imbalance.",
            ),
            (
                "Weapons under the rubble",
                "Excavators found 859 arrowheads together. One excavated attack area contained them. Sling stones appeared there too. So did scattered armor scales. An iron chain survived nearby. Perforated stones also appeared there. These objects cannot identify every fighter. Their concentration maps intense violence. The chain suggests possible countermeasures. Heavy stones may have struck machinery. Defenders may have deflected siege equipment. Attackers and defenders crowded one passage. Projectiles, fire, and machines filled it. This debris carries no royal slogan.",
            ),
            (
                "Fire in 701 BCE",
                "Recent archaeomagnetic research examined a mudbrick tower. That tower guarded Lachish's outer defenses. Its burning dates within the Iron Age. The 701 BCE siege remains likeliest. Temperatures inside became intensely high. Evidence cannot identify who started it. Defenders may have targeted siege engines. They may have targeted Assyria's ramp. Assyrian troops could likewise wield fire. Either explanation remains genuinely plausible. Scientific dating secures the destructive event. Human responsibility remains unresolved.",
            ),
            (
                "Victory recut in stone",
                "Conquest preceded the palace reliefs. Sculptors carved them across gypsum panels. They adorned Sennacherib's Southwest Palace. That palace stood within Nineveh. Lachish lay nearly a thousand kilometers away. The intended audience was Assyrian royalty. Artists worked around 700 to 692 BCE. Their work remained near-contemporary evidence. Yet these images shaped official memory. The victorious ruler commissioned that memory. The panels formed deliberate imperial memory. Evidence and propaganda coexist here.",
            ),
            (
                "How the relief moves",
                "The relief never freezes one instant. Instead, action crosses one continuous surface. Troops climb toward Lachish's defenses. Archers and slingers launch covering fire. Siege engines approach the walls. Defenders resist from elevated positions. Captives later leave the conquered city. Collected booty grows across the sequence. Finally, Sennacherib receives victory's results. This order illuminates Assyrian tactics. Yet viewers see constructed chronology. Space, scale, and time become compressed. Resistance then appears destined for collapse.",
            ),
            (
                "Battering rams go up",
                "Wheeled siege engines climbed Assyrian ramps. Covering fire protected their crews. Their rams threatened gates and mudbrick walls. Archers drove defenders from battlements. One carved detail may show water. That water apparently protected an engine. Burning material descended from above. The literal moment remains uncertain. Yet several elements reinforce each other. Ramps, engines, and projectiles appear together. Lachish preserves their actual breach zone. Palace art meets practical evidence.",
            ),
            (
                "Defenders fight the machines",
                "Lachish's defenders were never passive victims. Relief panels show active wall fighting. Defenders shoot arrows toward Assyrian machines. They throw stones from above. Burning torches also descend toward engines. Archaeology contributes weapons and a counter-ramp. Neither source fully explains everything. Together, they reveal adaptation and resistance. Yet their resources stayed unequal. Lachish could reinforce one threatened wall. Assyria could replace fallen men. Assyria could also maintain the ramp. Pressure continued at one chosen point. Eventually, the defenses failed there.",
            ),
            (
                "Families become imperial spoils",
                "After breaching, the imagery changes focus. Machines yield space to human suffering. Families depart with possessions and animals. Prisoners enter the imperial display. Executions and booty accompany them. Deportation was no accidental aftermath. Assyria used it against resistance. Assyria also redistributed displaced labor. Other communities received a warning. Conquest becomes visible through conquering eyes. Captives retain bodies, burdens, and direction. Their own words remain unpreserved.",
            ),
            (
                "Sennacherib stages judgment",
                "The sequence ends before Sennacherib's throne. Prisoners and spoils approach him. Bodyguards reinforce the royal setting. A royal tent and chariots appear. Cuneiform inscriptions frame his authority. Thousands of actions may have enabled conquest. Palace composition gathers every outcome together. All results flow toward one king. Engineers become instruments of command. Soldiers become instruments of command. Captives become proof of command. Even landscape confirms royal order. This composition performs Assyrian propaganda.",
            ),
            (
                "This is not a photograph",
                "Propaganda does not make evidence worthless. Instead, it changes our questions. Panels preserve equipment and clothing. They preserve tactics and names. They also stage events carefully. Control appears smoother than reality. Failures disappear from palace memory. Independent evidence strengthens specific details. These include Lachish's attack point. They include ramps and projectiles. Burned defenses support the destruction layer. Palace claims about emotions need caution. Palace claims about motives need caution. Claims of perfect order need caution. Detailed sources can still carry agendas.",
            ),
            (
                "Three records, three agendas",
                "Three records preserve the wider campaign. Assyrian royal texts form one record. The Hebrew Bible forms another. Archaeology supplies the third record. Palace inscriptions celebrate Assyrian dominance. Biblical accounts preserve Judah's political memory. They also preserve religious memory. Excavation recovers destruction and weapons. Buildings and chronology also survive. Archaeology cannot narrate complete motives. Agreement strongly secures Lachish's capture. Disagreement exposes deliberately shaped meanings. Each record addressed a different audience.",
            ),
            (
                "Jerusalem is a separate question",
                "Jerusalem stood about thirty miles away. Its fate demands separate treatment. Lachish cannot decide Jerusalem's ending. Assyrian texts describe Hezekiah blockaded. They also describe his tribute payments. Biblical tradition describes dramatic deliverance. Historians still debate that ending. Lachish offers considerably firmer ground. Excavation revealed its destruction layer. Its siege ramp still survives. Reliefs and texts confirm capture. Separating the cities protects honest uncertainty. One conclusion cannot settle another controversy.",
            ),
            (
                "Two destructions, two stories",
                "Stratigraphy prevents another serious mistake. Lachish's Level III city was destroyed. Sennacherib's 701 BCE campaign caused that destruction. The city was later rebuilt. Level II later fell to Babylonia. That fall occurred during 587 or 586 BCE. Famous Lachish Letters belong near that fall. They do not document Assyria's siege. Two endings occupy one mound. More than one century separates them. Mixing their evidence creates false history. Weapons and burned walls become misplaced. Inscriptions and crises become misplaced.",
            ),
            (
                "What Assyria changed",
                "Lachish was rebuilt afterward. Judah also survived Assyrian conquest. Survival never erased Lachish's losses. The city lost people. It lost defenses and wealth. Regional security also weakened. Hezekiah's kingdom faced heavier Assyrian pressure. It also carried heavier obligations. Assyria gained strategic control. Victory entered imperial palace memory. Judah remembered invasion and judgment. It also remembered endurance and Jerusalem. One conquest created different afterlives. Landscape preserved one afterlife. Imperial art preserved another. Political and religious history preserved others.",
            ),
            (
                "The ground cross-examines power",
                "Lachish remains compelling for one reason. Propaganda and archaeology intersect unusually clearly. Neither source cancels the other. Ground evidence confirms Assyria's siege ramp. It confirms the southwest attack point. It confirms concentrated projectile fire. It confirms a destruction layer. Assyria broke Lachish's defenses. The relief stages desired imperial memory. Its silences reveal excluded experiences. Combined sources sharpen the ending. Assyria unquestionably captured the city. Yet victory never controlled later evidence. Meaning escaped exclusive imperial ownership. The ground still cross-examines power.",
            ),
        ]
        # One repetitive sentence per section is removed deliberately, rather
        # than speeding speech or splitting captions. That keeps the 20-scene
        # documentary inside its ten-minute ceiling while preserving every core
        # claim, source relationship, and caption-safe sentence boundary.
        pacing_cuts = {
            "Propaganda meets the ground": "The question concerns more than victory.",
            "Why Lachish mattered": "They also supported political control.",
            "Assyria comes to Judah": "Assyrian power required battlefield skill.",
            "The southwest attack point": "The attack followed a constrained angle.",
            "Building the siege ramp": "It focused that pressure precisely.",
            "Judah builds back": "The counter-ramp reveals tactical understanding.",
            "Weapons under the rubble": "An iron chain survived nearby.",
            "Fire in 701 BCE": "Temperatures inside became intensely high.",
            "Victory recut in stone": "Their work remained near-contemporary evidence.",
            "How the relief moves": "This order illuminates Assyrian tactics.",
            "Battering rams go up": "Yet several elements reinforce each other.",
            "Defenders fight the machines": "Yet their resources stayed unequal.",
            "Families become imperial spoils": "Other communities received a warning.",
            "Sennacherib stages judgment": "Even landscape confirms royal order.",
            "This is not a photograph": "They preserve tactics and names.",
            "Three records, three agendas": "Buildings and chronology also survive.",
            "Jerusalem is a separate question": "Its fate demands separate treatment.",
            "Two destructions, two stories": "Two endings occupy one mound.",
            "What Assyria changed": "Landscape preserved one afterlife.",
            "The ground cross-examines power": "Combined sources sharpen the ending.",
        }
        compact_sections = []
        for caption, narration in sections:
            cut = pacing_cuts[caption]
            if narration.count(cut) != 1:
                raise ValueError(f"Lachish pacing cut was not uniquely found: {caption}")
            compact_sections.append((caption, narration.replace(cut, "").replace("  ", " ").strip()))
        sections = compact_sections
        sections = self._require_caption_safe_sections(sections, label="Lachish long")
        plan = [
            self._long_scene(channel.id, subject, narration, caption)
            for caption, narration in sections
        ]
        # A small curated archive spine prevents scene-search latency from
        # replacing the mound, surviving ramp, or a named relief detail with a
        # generic Assyrian object.  Search still remains as a bounded fallback
        # when Wikimedia is temporarily unavailable.
        preferred_archive_by_caption = {
            "Propaganda meets the ground": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Lachish_Relief%2C_British_Museum.jpg/1920px-Lachish_Relief%2C_British_Museum.jpg",
            "Why Lachish mattered": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/43/Tel-Lakhish-V2-562.jpg/1920px-Tel-Lakhish-V2-562.jpg",
            "Assyria comes to Judah": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/be/Battle_Siege_of_Lachish_Wall_Panels_in_the_British_Museum_%2843494312812%29.jpg/1920px-Battle_Siege_of_Lachish_Wall_Panels_in_the_British_Museum_%2843494312812%29.jpg",
            "The southwest attack point": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/88/Capture_of_Lachish_-_ramps_and_battle_engines.jpg/1920px-Capture_of_Lachish_-_ramps_and_battle_engines.jpg",
            "Building the siege ramp": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5a/LachishRamp053011.jpg/1920px-LachishRamp053011.jpg",
            "Weapons under the rubble": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/67/Assyrian_arrowheads_Lachish_BM.jpg/1920px-Assyrian_arrowheads_Lachish_BM.jpg",
            "Victory recut in stone": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a9/Lachish_Relief%2C_British_Museum_10.jpg/1920px-Lachish_Relief%2C_British_Museum_10.jpg",
            "How the relief moves": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Lachish_Relief%2C_British_Museum_8.jpg/1920px-Lachish_Relief%2C_British_Museum_8.jpg",
            "Battering rams go up": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/93/Assyrian_siege-engine_attacking_the_city_wall_of_Lachish%2C_part_of_the_ascending_assaulting_wave._Detail_of_a_wall_relief_dating_back_to_the_reign_of_Sennacherib%2C_700-692_BCE._From_Nineveh%2C_Iraq%2C_currently_housed_in_the_British_Museum.jpg/1920px-thumbnail.jpg",
            "Defenders fight the machines": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b9/Assyrian_Archers_and_Slingers_from_the_Siege_of_Lachish_Relief_British_Museum_124906.jpg/1920px-Assyrian_Archers_and_Slingers_from_the_Siege_of_Lachish_Relief_British_Museum_124906.jpg",
            "Families become imperial spoils": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d7/Jewish_Captives_and_Assyrian_Captors%2C_Siege_of_Lachish_%2843494315232%29.jpg/1920px-Jewish_Captives_and_Assyrian_Captors%2C_Siege_of_Lachish_%2843494315232%29.jpg",
            "Sennacherib stages judgment": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/28/King_Sennacherib_on_his_throne._Siege_of_Lachish_Palace_Wall_Panel_-_Nate_Loper_%2843494313962%29.jpg/1920px-King_Sennacherib_on_his_throne._Siege_of_Lachish_Palace_Wall_Panel_-_Nate_Loper_%2843494313962%29.jpg",
            "This is not a photograph": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7c/Lachish_Relief%2C_British_Museum_5.jpg/1920px-Lachish_Relief%2C_British_Museum_5.jpg",
            "What Assyria changed": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1b/The_fall_of_Lachish%2C_King_Sennacherib_reviews_Judaean_prisoners..JPG/1920px-The_fall_of_Lachish%2C_King_Sennacherib_reviews_Judaean_prisoners..JPG",
            "The ground cross-examines power": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4c/Lachish_Relief%2C_British_Museum_3.jpg/1920px-Lachish_Relief%2C_British_Museum_3.jpg",
        }
        for scene in plan:
            scene.preferred_image_url = preferred_archive_by_caption.get(scene.visual_text, "")
        return plan

    # Roughly one researched fact per this many spoken words, measured from
    # the nine-minute scripts that wanted fourteen.
    LONG_WORDS_PER_FACT = 90
    MIN_LONG_FACTS = 6

    def _long_facts_needed(self, channel: ChannelConfig) -> int:
        """Facts a long script needs, scaled to its configured length."""
        target = int(self._target_words(channel, "video"))
        return max(self.MIN_LONG_FACTS, round(target / self.LONG_WORDS_PER_FACT))

    def _history_long_video_plan(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[ScenePlanItem]:
        raw_points = []
        scenes = list(topic.scene_plan or [])
        middle_scenes = scenes[1:-1] if len(scenes) >= 3 else scenes
        for scene in middle_scenes:
            point = self._sentence(self._clean(scene.narration or scene.visual_text))
            if not point or self._is_generic_opener(point) or self._bad_ai_line_issue(point):
                continue
            raw_points.append(point)
        points = []
        seen = set()
        seen_token_sets: list[set[str]] = []
        duplicate_stopwords = {
            "about", "after", "again", "against", "also", "around", "because", "before",
            "between", "could", "during", "first", "from", "have", "into", "later", "likely",
            "more", "over", "same", "their", "there", "these", "they", "this", "through",
            "very", "were", "which", "while", "with", "would",
        }
        for point in raw_points:
            key = re.sub(r"[^a-z0-9]+", " ", point.lower()).strip()
            if not key or key in seen:
                continue
            token_set = {
                token
                for token in key.split()
                if (len(token) >= 4 or token.isdigit()) and token not in duplicate_stopwords
            }
            if token_set and any(
                len(token_set & previous) / max(1, min(len(token_set), len(previous))) >= 0.68
                for previous in seen_token_sets
            ):
                continue
            seen.add(key)
            seen_token_sets.append(token_set)
            points.append(point)
        # How many facts a script needs depends on how long it has to fill.
        # Fourteen was right for a nine-minute video and impossible for a
        # four-minute one, so a shortened channel rejected every topic for
        # being under-researched and never built anything.
        needed = self._long_facts_needed(channel)
        points = points[:needed]
        if len(points) < needed:
            raise ValueError(
                f"Long Ancient script has only {len(points)} distinct researched facts; needs {needed} before expansion."
            )
        if "lachish" in subject.lower():
            return self._lachish_long_video_plan(channel, topic, subject)

        category_context = {
            "war": "Military events become clearer when terrain, supply, timing, and command decisions are treated as constraints rather than dramatic decoration.",
            "construction": "Built remains turn authority into something measurable: materials had to be obtained, workers coordinated, and the structure maintained after the first display of power.",
            "trade": "Exchange networks connect this local clue to distant demand, transport costs, political protection, and the people who moved goods rather than writing royal histories.",
            "writing": "A written source is powerful but never neutral; its date, audience, author, purpose, and survival all shape what it can honestly tell us.",
            "burial": "Burial evidence records choices about rank, memory, labor, belief, and access, while also warning us that elite tombs do not represent every life.",
            "religion": "Sacred space can organize calendars, wealth, authority, and public identity at the same time, so ritual evidence belongs inside the political story rather than outside it.",
            "power": "Power is visible here through coordination: who could command labor, protect routes, collect resources, preserve a message, or punish resistance.",
            "archaeology": "Physical evidence can correct a later story, but context matters; an object removed from its layer, building, or find spot loses part of its historical meaning.",
            "chronology": "Chronology prevents separate generations from collapsing into one legend and lets us see which cause, response, and consequence actually followed another.",
            "landscape": "The landscape is an active constraint because movement, water, visibility, defense, and access all change what people can build or control.",
            "evidence": "This claim earns attention because it can be compared with material remains, dated sources, and competing explanations instead of being repeated as a legend.",
        }
        category_context_variants = {
            "war": [
                "The clash itself was only one part of the contest. Supply, terrain, allies, ships, money, and the ability to replace losses determined whether a victory could change the war.",
                "A campaign could destabilize the system behind it: unpaid troops, exhausted allies, debt, and rival commanders often carried conflict back into domestic politics.",
                "Military power reached far beyond the battlefield. Recruitment, pay, transport, and obligations to allied communities shaped what a commander could attempt and how long the effort could continue.",
                "After the fighting, the decisive question was whether military success could become durable political control without exhausting the system that produced it.",
                "Defeat changed bargaining power immediately. Peace terms, indemnities, surrendered territory, and limits on future action could reshape a state long after the armies left the field.",
                "Military memory became political material. Later writers and leaders could turn an old enemy, victory, or humiliation into an argument for new policy in their own time.",
                "A siege turned strategy into endurance. Food, water, disease, repairs, civilian survival, and the attacker's ability to maintain pressure decided what walls alone could not.",
            ],
            "construction": [
                "Architecture turns policy into a physical commitment: materials must arrive, skilled work must be coordinated, and the structure must keep functioning after the ceremony ends.",
                "What survives is the final layer of a longer process of quarrying, transport, measurement, labor, repair, and control over access.",
                "Rebuilding and reuse are part of the story because later rulers often changed an older monument while borrowing the authority already attached to its site.",
                "Transport belongs inside the design story. The chosen material matters together with the route, season, tools, and repeated effort required to move it into position.",
                "Separate construction phases before treating the monument as one plan. A later addition can answer a different need and preserve a different community's choices.",
                "Tool marks, joints, dressed surfaces, and unfinished work reveal sequences of skilled decisions that a distant view can hide.",
                "Placement changed how the structure worked. Approach routes, sightlines, nearby water, older monuments, and gathering space shaped the experience before anyone reached its center.",
                "Differences in material and technique can mark chronology, repair, or separate work groups, so variation is evidence rather than an imperfection to smooth away.",
                "Maintenance is evidence of continued use. Repairs, replaced pieces, worn paths, and altered entrances show whether people returned after the first construction phase.",
                "The order of foundations, walls, openings, and later cuts helps distinguish the original project from changes made by communities who inherited it.",
                "Labor should be inferred from repeated tasks and logistical constraints, not from an impressive invented workforce number that the surviving record cannot verify.",
                "A structure can remain politically useful after its builders are gone because later groups can repair it, reinterpret it, or control access to the place around it.",
                "Scale becomes historical evidence only when it is connected to supply, working time, specialized knowledge, and the institutions that kept those commitments together.",
                "The building process linked distant extraction sites with local preparation and final assembly, turning geography into a chain of coordinated choices.",
            ],
            "trade": [
                "This was power measured in cargoes, protected routes, credit, warehouses, and the political relationships that kept goods moving through dangerous water or contested land.",
                "Every trade route created winners and vulnerabilities at the same time: prosperity depended on distant suppliers, secure passage, and partners who could withdraw cooperation.",
                "Imports, harbor works, weights, coins, and shipwrecks can reveal the reach of a network more clearly than a ruler's claim to control it.",
            ],
            "writing": [
                "The words preserve a voice, but also an agenda. Date, audience, authorship, and the reason the text survived determine which parts can be treated as testimony.",
                "Official language often describes the world as authority wanted it to appear; archaeology tests whether administration and daily practice matched that public message.",
                "A later copy can preserve real memory, but the time gap matters whenever the account supplies exact speeches, motives, numbers, or blame.",
            ],
            "burial": [
                "Burial choices concentrate belief, rank, family memory, labor, and access to wealth, yet an elite tomb can never stand in for every person in the society.",
                "Grave goods and human remains answer different questions, so status, health, migration, ritual, and cause of death must not be collapsed into one dramatic conclusion.",
                "The missing graves matter too because preservation, later disturbance, and unequal treatment can distort which lives enter the archaeological record.",
            ],
            "religion": [
                "Sacred institutions could organize calendars, wealth, diplomacy, law, and public identity, making ritual inseparable from political and economic power.",
                "Offerings and sanctuaries record repeated choices, but they rarely explain private belief without help from inscriptions, domestic evidence, and regional comparison.",
                "A disputed ritual should be presented through the remains and the competing interpretations, not converted into certainty because the shocking version is more memorable.",
            ],
            "power": [
                "Authority becomes visible when orders turn into collected resources, standardized work, guarded space, repeated messages, or punishment that can be sustained over time.",
                "A title proves less than the system behind it. The stronger question is who controlled access, who carried out decisions, and who absorbed their cost.",
                "Political power was never perfectly uniform; local elites, allied communities, soldiers, workers, and families could cooperate, bargain, evade, or resist.",
            ],
            "archaeology": [
                "The object matters most in context: its layer, neighboring finds, signs of repair, and route into a collection can strengthen or sharply limit the conclusion.",
                "Excavation recovers a sample shaped by preservation and earlier rebuilding, so what survives should not be mistaken for a complete inventory of past life.",
                "New methods can revise an old interpretation without erasing the whole site; the key is naming exactly which claim changed and why.",
            ],
            "chronology": [
                "The date changes the story because it decides which event can be a cause, which can only be a consequence, and which famous connection is impossible.",
                "Later rebuilding can place several centuries in the same view, so a dramatic ruin must be separated into the different communities that created it.",
                "A sequence of pressure, decision, action, and response is more revealing than a list of dates because it shows where another outcome was still possible.",
            ],
            "landscape": [
                "Coast, slope, water, farmland, roads, and visibility shaped movement and defense before any ruler or army made a choice.",
                "The map exposes practical limits: distance consumes time and food, narrow routes create risk, and a protected harbor or pass can multiply local power.",
                "Environmental opportunity did not dictate the outcome, but it changed the cost of every route, settlement, campaign, and supply decision.",
            ],
            "evidence": [
                "This clue becomes meaningful when it changes the sequence, scale, or human consequence of the story rather than serving as decoration.",
                "Its strongest use is specific: identify what it confirms, what remains inferential, and which independent trace could support or contradict it.",
                "The memorable version may compress decades into one scene; this detail restores a step where people still had choices and consequences were not yet fixed.",
            ],
        }
        primary_lenses = [
            "What happened next reveals whether this was a temporary advantage or a change strong enough to reshape the choices available to everyone around it.",
            "For the people living through it, the shift appeared as work, food, tax, danger, movement, status, or protection long before it became a sentence in a chronicle.",
            "The scale is visible in repetition: one trace can show contact, while the same pattern across several secure contexts points to an organized system.",
            "This is where the public image and daily reality meet, because a decision only became history when other people built it, funded it, enforced it, or resisted it.",
            "The turning point was not automatic. Different choices remained open, and each depended on resources, information, alliances, and risks that were unevenly shared.",
            "Independent sources do not need to tell the same story in the same words; confidence rises when different kinds of evidence converge on the same sequence.",
            "The consequence matters more than the slogan because it shows who gained room to act, who lost it, and which pressure carried into the next stage.",
            "A dramatic interpretation is strongest when the missing motive or conversation is left missing instead of being filled with invented certainty.",
            "The next generation inherited more than the event itself: it inherited damaged or protected routes, institutions, memories, skills, and political limits.",
            "That longer afterlife explains why a decision that looked successful in the moment could create a vulnerability visible only years later.",
            "The strongest rival explanation must account for the same dates, material traces, and practical limits rather than surviving only because it sounds possible.",
            "At human scale, the result was not abstract; it altered where people could live, travel, worship, trade, serve, speak, or remain safe.",
            "The surviving record favors durable objects and powerful voices, so silence must be treated as missing evidence rather than automatic proof of absence.",
            "Follow this clue into the next dated change, because consequence is the bridge that turns an isolated fact into a coherent historical story.",
        ]
        repeat_category_contexts = [
            "Because this is another clue of the same type, it must add a new date, location, scale, actor, or consequence instead of borrowing importance from the earlier point.",
            "Treat this evidence as a separate test. Its context should narrow the interpretation, reveal a different constraint, or expose where the first explanation stops working.",
            "The repetition of an evidence category is useful only when the second example changes the pattern, extends its range, or identifies an exception that needs explanation.",
            "This detail belongs beside the earlier evidence, not on top of it. Compare what each one independently establishes before combining them into a wider claim.",
            "A second example can reveal duration or organization, but only if its dating and find context are secure enough to rule out accidental similarity.",
            "Use this point to test a different part of the story: origin, movement, implementation, resistance, survival, or consequence after the initial decision.",
            "The new information here should alter the sequence rather than merely decorate it, so identify exactly which earlier assumption becomes stronger, weaker, or more precise.",
            "Read this clue at human scale. Ask who had to notice it, carry it out, maintain it, resist it, or live with the result after the official decision was made.",
            "This evidence earns separate attention when it links a local action to a wider network, institution, conflict, or change visible beyond one object or site.",
            "Before treating the pattern as settled, compare this example with the strongest exception and explain why the difference does or does not matter.",
            "The safest use of this point is specific: state what it confirms, which uncertainty it reduces, and which larger conclusion it still cannot support alone.",
            "Now place the detail against the physical constraints of its period so that modern expectations do not silently supply missing technology, speed, or political control.",
        ]
        followup_lenses = [
            "Test the first clue against an independent source from the same period. If the date or context disagrees, the disagreement must be explained before the claim grows larger.",
            "Move from description to consequence. Ask what became easier, harder, safer, richer, or more dangerous after this condition entered the story, and identify who experienced that change.",
            "Check the physical scale before using dramatic language. Materials, distance, labor, transport, and maintenance can distinguish a one-time display from a system able to operate repeatedly.",
            "Separate what the evidence proves from what it merely permits. A plausible motive can guide a question, but it cannot replace a dated object, a documented action, or a consistent pattern.",
            "Now examine whose voice is absent. Royal texts, monumental art, and elite buildings preserve authority well, while households, captives, migrants, and workers often require different evidence.",
            "Put the clue back into its landscape. Water, visibility, slope, coast, road, farmland, or distance can make an apparently irrational decision practical in its original setting.",
            "Compare the immediate result with the longer afterlife. A decision may fail politically yet preserve a technique, route, memory, or institution that continues under new rulers.",
            "Ask how the evidence reached the present. Erosion, rebuilding, looting, excavation choices, and museum collecting can shape both what survives and which interpretation looks most obvious.",
            "Use the strongest rival explanation as a stress test rather than a straw man. The better account should explain more of the dated evidence with fewer unsupported assumptions.",
            "Track coordination instead of admiring scale alone. Repeated standards, supply, records, access, and maintenance reveal whether authority could turn an order into sustained action.",
            "Keep chronology strict. A later tradition may preserve memory, but it cannot be treated as an eyewitness without explaining the gap between the event and the surviving text.",
            "End the check with a falsifiable question: name the discovery, date, or contradiction that would force this interpretation to change rather than protecting it from every possible result.",
        ]
        followup_openers = [
            "The opening fact is only the starting point.",
            "Description alone cannot show historical impact.",
            "A large number needs a practical frame.",
            "A compelling interpretation can still exceed its evidence.",
            "Official records preserve only part of the experience.",
            "The map can overturn a comfortable explanation.",
            "The turning point is not the end of the sequence.",
            "Survival creates a filter before interpretation even begins.",
            "Confidence should increase only after a serious challenge.",
            "Spectacle can hide the organization that made it possible.",
            "Memory can remain valuable without becoming eyewitness testimony.",
            "A historical claim should remain open to revision.",
        ]
        followup_captions = [
            "Cross-check the opening claim",
            "Follow the consequence",
            "Measure the real scale",
            "Separate proof from possibility",
            "Find the missing voices",
            "Restore the landscape",
            "Trace what survived",
            "Explain preservation bias",
            "Test the rival account",
            "Look for sustained coordination",
            "Date the later memory",
            "Make the claim testable",
        ]
        followup_closers = [
            "That comparison gives the audience a reason to trust the conclusion rather than asking them to trust the narrator's confidence. It also exposes whether two sources are truly independent or simply recycling the same surviving tradition.",
            "The answer turns a static fact into a historical change with people on both sides of its cost. Without that step, a vivid event can remain disconnected from the lives it supposedly changed.",
            "Visible constraints are more persuasive than an unsupported number chosen only to sound impressive. If the evidence cannot sustain precision, describe the credible range honestly instead of manufacturing an exact total.",
            "Keeping those levels apart protects a compelling interpretation from becoming an invented certainty. Inference remains useful when its uncertainty is visible rather than hidden behind the narrator's confidence.",
            "Their traces can reveal whether the public message matched the experience of people with less power.",
            "A practical route or barrier may explain a choice that looks irrational when the map is removed.",
            "Longer consequences show whether the event transformed a system or only interrupted it for a moment.",
            "This filter explains why the surviving sample may exaggerate elite wealth, durable materials, and official memory.",
            "A conclusion becomes stronger after surviving the best alternative, not after ignoring it.",
            "Maintenance and repetition reveal a working institution more clearly than a single spectacular achievement.",
            "The time gap changes how precisely names, motives, speeches, and numbers can be repeated.",
            "A claim that could be revised by evidence is history; a claim protected from every result is only a story.",
        ]
        crosschecks = [
            (
                "Put the dates in order",
                f"Chronology is the first control for {subject}. Arrange the verified clues from earliest to latest, then separate the event itself from later memory, rebuilding, excavation, and modern interpretation. A cause must come before its claimed effect, and two similar remains should not be treated as contemporary until dating supports that link. This simple sequence prevents generations of change from collapsing into one cinematic moment.",
            ),
            (
                "Map the physical setting",
                f"Geography tests what was possible around {subject}. Place the site, route, coast, river, pass, farmland, desert, or urban center on a map and measure the distances connecting them. Terrain affects movement, communication, defense, food, and access to materials. When a written account ignores those limits, the landscape becomes an independent witness that can support the story or expose a practical contradiction.",
            ),
            (
                "Read excavated evidence",
                f"Excavated evidence for {subject} needs context, not just a dramatic close-up. Record where an object was found, the layer or structure around it, signs of repair or reuse, and the method used to date it. An isolated artifact may prove contact or technique; a repeated pattern across secure contexts can support a wider conclusion about organization, behavior, or change.",
            ),
            (
                "Interrogate written sources",
                f"Written evidence about {subject} must be read as an action by an author. Identify when it was produced, who was expected to hear or see it, what authority it served, and how the text survived. Praise, accusation, prophecy, law, and commemoration can preserve facts while shaping their meaning. Comparing those claims with archaeology keeps eloquent language from becoming automatic proof.",
            ),
            (
                "Measure labor and supply",
                f"Large claims about {subject} become testable through logistics. Ask how people, food, tools, animals, fuel, stone, metal, water, or messages reached the required place and how long the effort had to continue. The answer does not need an invented workforce total. Even broad constraints can reveal whether the evidence reflects a brief emergency, a ceremonial display, or administration capable of sustained coordination.",
            ),
            (
                "Recover ordinary experience",
                f"Elite decisions are only one layer of {subject}. Follow the consequences into homes, workshops, farms, roads, markets, camps, sanctuaries, and burial grounds where ordinary routines left different traces. This does not authorize imaginary personal stories. It asks which material changes would be expected if policy, conflict, ritual, trade, disease, or construction truly altered daily work and survival.",
            ),
            (
                "Identify organized power",
                f"Power around {subject} should be described through observable coordination rather than titles alone. Look for controlled access, standardized work, collected resources, protected routes, repeated messages, or the ability to maintain a project after its first display. Then ask who benefited and who absorbed the cost. That comparison makes authority visible without assuming every monument or text represents unanimous consent.",
            ),
            (
                "Trace wider connections",
                f"No ancient case existed in isolation, so test whether {subject} connects to neighboring regions through materials, styles, coins, texts, ships, roads, migration, or shared techniques. Similarity by itself is not proof of direct contact; chronology and route must agree. When they do, a local clue can reveal a larger network of exchange, competition, diplomacy, or movement.",
            ),
            (
                "Compare rival explanations",
                f"A strong documentary gives {subject} a genuine competing explanation. State what each account predicts, then compare those predictions with the dated remains and the source record. The goal is not to manufacture controversy. It is to show why one explanation currently covers more evidence, where another still fits, and which missing discovery could shift the balance.",
            ),
            (
                "Account for preservation",
                f"What survives from {subject} is not a neutral sample of the past. Stone, metal, fired clay, dry caves, sealed burials, and monumental inscriptions may endure while wood, cloth, food, speech, and ordinary movement disappear. Later rebuilding and excavation add another filter. Recognizing that imbalance prevents absence from being mistaken for proof that an activity, group, or belief never existed.",
            ),
            (
                "Separate finding from story",
                f"Modern interpretation of {subject} has its own chronology. Record when major finds were made, which methods were available, and which political or popular stories shaped early conclusions. New dating, remote sensing, residue analysis, or contextual excavation can revise an older narrative without making the entire record worthless. Good history shows exactly which claim changed and which evidence remained stable.",
            ),
            (
                "Define the lasting change",
                f"The final test for {subject} is consequence. Distinguish the immediate outcome from changes that lasted across later generations, and separate documented influence from resemblance created by hindsight. A responsible conclusion can be dramatic without becoming absolute: it names what changed, who experienced it, which evidence supports that judgment, and where the record still refuses a simple ending.",
            ),
            (
                "Check every historical label",
                f"Modern labels can make {subject} look more unified than it was. Check whether names for peoples, borders, religions, offices, and periods came from contemporary sources or later historians. A useful label can organize evidence without proving that everyone inside it shared one identity or political goal. This vocabulary check removes anachronism before it quietly reshapes motive, territory, and allegiance.",
            ),
            (
                "Build one secure evidence chain",
                f"Finish the investigation of {subject} by tracing one complete chain: a dated source or object, the action or condition it documents, and a consequence supported by separate evidence. Mark every step that remains inferential. This method is less flashy than stacking mysteries, but it gives the audience a conclusion they can inspect and shows exactly why the final claim deserves confidence.",
            ),
        ]

        opening_line = self._sentence(self._clean(topic.hook))
        opening_words = len(re.findall(r"[A-Za-z0-9']+", opening_line))
        normalized_opening = re.sub(r"[^a-z0-9]+", " ", opening_line.lower()).strip()
        normalized_points = {
            re.sub(r"[^a-z0-9]+", " ", point.lower()).strip()
            for point in points
        }
        if (
            not opening_line
            or opening_words < 7
            or opening_words > 34
            or self._is_generic_opener(opening_line)
            or self._bad_ai_line_issue(opening_line)
            or normalized_opening in normalized_points
        ):
            fallback_hooks = [
                f"The dramatic version of {subject} starts at the climax.",
                f"The most familiar image of {subject} hides the network that made it possible.",
                f"{subject} looks inevitable in hindsight, but every major turning point was once an open choice.",
                f"What survived from {subject} is only the part that stone, metal, climate, and power allowed us to see.",
                f"The famous moment in {subject} began generations before anyone knew how the story would end.",
                f"One object, ruin, or battle made {subject} memorable, but it cannot explain the whole transformation alone.",
            ]
            hook_index = sum(ord(character) for character in subject.lower()) % len(fallback_hooks)
            opening_line = fallback_hooks[hook_index]

        out = [
            self._long_scene(
                channel.id,
                subject,
                f"{opening_line} That is the familiar edge of the story, not the whole of it. The deeper question is how geography, resources, institutions, rival decisions, and ordinary people carried {subject} toward that moment. Follow those pressures in sequence and the famous image stops being an isolated legend; it becomes the visible result of choices that could still have ended differently.",
                "The turning point has a backstory",
            ),
            self._long_scene(
                channel.id,
                subject,
                f"Begin where the people inside {subject} had to begin: with the physical setting and the first dated traces. Then move through expansion, pressure, conflict, adaptation, collapse, or survival without skipping the links between them. Objects, buildings, inscriptions, landscapes, and written accounts will not always agree. Those disagreements are useful because they reveal which parts are secure, which depend on later memory, and where the strongest competing explanation still survives.",
                "Begin with place and chronology",
            ),
        ]
        body: list[ScenePlanItem] = []
        category_counts: dict[str, int] = {}
        for index, point in enumerate(points):
            category = self._history_fact_category(point)
            category_counts[category] = category_counts.get(category, 0) + 1
            context_options = category_context_variants.get(category, [category_context["evidence"]])
            context = context_options[(category_counts[category] - 1) % len(context_options)]
            lens = primary_lenses[index % len(primary_lenses)]
            caption = self._captionize(point) or f"Evidence clue {index + 1}"
            body.append(
                self._long_scene(
                    channel.id,
                    subject,
                    f"{point} {context} {lens}",
                    caption,
                )
            )
        synthesis_sections = [
            (
                "Test the strongest rival account",
                f"The cleanest explanation of {subject} is not automatically the strongest one. Put the leading account beside its best rival and ask what each predicts about dates, routes, damage, settlement, written testimony, and human behavior. The better explanation should cover more of those independent traces with fewer unsupported assumptions. Where both still fit, the disagreement belongs in the conclusion instead of being hidden for the sake of a smoother story.",
            ),
            (
                "Follow the consequences forward",
                f"Now follow {subject} beyond its famous turning point. Separate the immediate result from the institutions, routes, memories, technologies, communities, or political limits that survived into the next generation. That longer view often reverses the simple verdict of victory or defeat. It shows whether the event transformed a system, interrupted it briefly, or created a new problem that later rulers inherited without fully understanding its origin.",
            ),
        ]
        # Mild Phase-1 skeleton variation: rotate synthesis + crosscheck order by
        # subject so two longs do not share an identical chapter spine.
        skeleton_seed = sum(ord(ch) for ch in subject.lower()) + len(points)
        if skeleton_seed % 2:
            synthesis_sections = list(reversed(synthesis_sections))
        rotated_crosschecks = list(crosschecks)
        rotate_by = skeleton_seed % max(1, len(rotated_crosschecks))
        if rotate_by:
            rotated_crosschecks = rotated_crosschecks[rotate_by:] + rotated_crosschecks[:rotate_by]
        for caption, narration in synthesis_sections:
            if len(body) >= 16:
                break
            body.append(self._long_scene(channel.id, subject, narration, caption))
        for caption, narration in rotated_crosschecks:
            if len(body) >= 16:
                break
            body.append(self._long_scene(channel.id, subject, narration, caption))
        out.extend(body[:16])
        closing_variants = [
            (
                f"The remaining uncertainty around {subject} is not a failure of the story. It is a boundary around what the sources can support. Strong history marks that boundary clearly, compares rival explanations, and avoids filling silence with invented certainty. That honesty makes the confirmed evidence more powerful, not less dramatic. It also identifies the next inscription, layer, date, or scientific test that could genuinely change the conclusion.",
                "What remains uncertain",
                f"The lasting importance of {subject} is not one isolated fact. It is the relationship between physical evidence, organized power, human choices, and the consequences that followed. Once those layers are kept together, the past stops looking like a legend and starts looking like people solving problems under pressure with imperfect information. Return to the opening clue and notice how much more precisely its meaning can now be stated.",
                "Why it still matters",
            ),
            (
                f"What we still cannot prove about {subject} belongs in the conclusion. Mark the gaps, keep rival explanations visible, and refuse invented certainty where the sources are silent. That restraint makes the confirmed evidence sharper and shows which discovery would actually change the claim.",
                "Name the open questions",
                f"The reason {subject} still matters is the chain connecting evidence, power, ordinary lives, and later consequences. Keep those layers together and the opening clue stops looking like a legend. It becomes a decision made under pressure, with imperfect information, that still shaped later choices.",
                "Return to the opening clue",
            ),
        ]
        close_a, close_a_caption, close_b, close_b_caption = closing_variants[
            skeleton_seed % len(closing_variants)
        ]
        out.extend(
            [
                self._long_scene(channel.id, subject, close_a, close_a_caption),
                self._long_scene(channel.id, subject, close_b, close_b_caption),
            ]
        )
        return out

    def _brain_relationship_profile(self, subject: str) -> dict[str, str]:
        lowered = subject.lower()
        profiles = (
            (
                (
                    "text", "reply", "breadcrumb", "dry texting", "double texting", "slow fading",
                    "voice note", "late night texting", "canceled date",
                ),
                {
                    "hook": "The message gets shorter, the phone stays quiet, and suddenly one tiny signal feels like a verdict on the whole connection.",
                    "cue": "a change in reply length, timing, tone, or follow-through",
                    "impulse": "check again, rewrite the message, or send something designed to force reassurance",
                    "evidence": "whether communication, plans, and returned effort stay consistent across several days",
                    "experiment": "Mute the thread for one hour, keep the plan you already made, and record the next observable action without guessing its motive.",
                    "question": "I enjoy talking with you. Has the pace or your interest changed?",
                    "boundary": "I will ask once for clarity, then match the consistency I actually receive.",
                    "healthy": "communication that can vary without making basic interest a daily mystery",
                },
            ),
            (
                (
                    "situationship", "almost relationship", "talking stage", "exclusivity", "romantic uncertainty",
                    "future faking", "benching", "relationship pacing", "mutual effort",
                ),
                {
                    "hook": "You talk every day, share intimate moments, and still cannot answer the simplest question: what is this connection actually becoming?",
                    "cue": "closeness increasing faster than shared expectations, labels, or dependable plans",
                    "impulse": "treat potential as commitment and invest more in the hope than the current relationship",
                    "impulse_scene": (
                        "People can mistake potential for commitment. "
                        "Hope receives more investment than the present relationship."
                    ),
                    "evidence": "clear intention, kept plans, reciprocal effort, and respect when the pace slows",
                    "experiment": "Keep your normal week intact and compare every future promise with one concrete action that happens now.",
                    "question": "I like where this is going. What are you genuinely available to build with me?",
                    "boundary": "I will not give relationship-level access to a connection that repeatedly avoids relationship-level clarity.",
                    "healthy": "excitement that grows beside shared intention instead of replacing it",
                },
            ),
            (
                (
                    "kiss", "flirt", "eye contact", "body language", "chemistry", "first date",
                    "sexual tension", "consent", "friendship to romance", "voice change",
                ),
                {
                    "hook": "The space between you closes, one look lasts longer, and your body notices the attraction before either person says it aloud.",
                    "cue": "a cluster of returned eye contact, warmer timing, chosen closeness, and relaxed attention",
                    "impulse": "turn one electric moment into certainty about mutual desire or long-term compatibility",
                    "evidence": "enthusiastic consent, returned signals, respectful pacing, and interest that continues after the charged moment",
                    "experiment": "Offer one small, respectful signal and leave enough space for the other person to return it freely.",
                    "question": "I want to kiss you. Would you like that too?",
                    "boundary": "I will treat hesitation as a reason to pause, not a challenge to persuade.",
                    "healthy": "mutual attraction that becomes clearer because both people can say yes, no, or slower",
                },
            ),
            (
                (
                    "attachment", "fear of intimacy", "fear of abandonment", "unavailable", "push pull",
                    "emotional availability", "secure attraction", "emotional safety", "self respect",
                ),
                {
                    "hook": "The connection gets real, closeness rises, and then reassurance or distance suddenly starts feeling urgent.",
                    "cue": "what happens when vulnerability, uncertainty, or a request for closeness enters the room",
                    "impulse": "chase harder, disappear first, or label the other person before asking what changed",
                    "evidence": "clear requests, respected space, repaired misunderstandings, and dependable return after tension",
                    "experiment": "Name one need without apologizing for it, then watch whether the response brings clarity, respect, and follow-through.",
                    "question": "When closeness feels intense, what helps you stay connected without feeling crowded?",
                    "boundary": "I can respect space, but I will not stay in repeated unexplained withdrawal or pressure.",
                    "healthy": "closeness with enough honesty and stability for both people to remain themselves",
                },
            ),
            (
                ("conflict repair", "apology consistency", "vulnerability", "oversharing"),
                {
                    "hook": "The difficult moment ends, but what happens next reveals whether trust is being repaired or merely reset for another repeat.",
                    "cue": "whether discomfort is met with curiosity, accountability, defensiveness, or emotional punishment",
                    "impulse": "accept perfect words immediately or reveal more in order to earn a safer response",
                    "evidence": "specific accountability, changed behavior, mutual care, and a repair that lasts beyond the conversation",
                    "experiment": "Choose one small truth, state what repair would look like, and compare the response with behavior over the next week.",
                    "question": "What will we each do differently if this situation happens again?",
                    "boundary": "I will not call the issue repaired when the apology arrives but the same behavior keeps returning.",
                    "healthy": "openness that is paced, reciprocal, and followed by actions that make future honesty safer",
                },
            ),
            (
                ("dating app", "online dating burnout", "choice overload"),
                {
                    "hook": "You open the app looking for one real conversation and end up comparing dozens of people you have barely met.",
                    "cue": "swiping replacing curiosity, conversations feeling interchangeable, or small imperfections ending interest instantly",
                    "impulse": "keep searching for a perfect option instead of learning whether one promising connection has depth",
                    "evidence": "conversation quality, reciprocal curiosity, a safe in-person plan, and how you feel after using the app",
                    "experiment": "Limit active conversations to three and pause new swiping until each has either progressed or clearly ended.",
                    "question": "Would you like to meet for a short coffee instead of staying in app small talk?",
                    "boundary": "I will stop using the app when it makes people feel like inventory rather than possible partners.",
                    "healthy": "enough choice to meet people without endless comparison replacing genuine attention",
                },
            ),
            (
                ("ghosting", "closure", "orbiting", "checking an ex", "rebound"),
                {
                    "hook": "They disappear, reappear online, or leave one question unanswered—and your mind keeps the emotional door open.",
                    "cue": "a search for one final explanation, profile check, story view, or new connection that promises instant relief",
                    "impulse": "use another fragment of contact to reopen the full relationship story",
                    "evidence": "direct communication, accountable repair, emotional availability, and whether moving forward becomes easier",
                    "experiment": "Remove the easiest checking cue for seven days and write the answer you already have from the repeated behavior.",
                    "question": "Are you contacting me to repair and rebuild something specific, or only to reconnect for a moment?",
                    "boundary": "I will not exchange ongoing emotional access for occasional online attention or unclear contact.",
                    "healthy": "grief that can move forward without waiting for another person to deliver a perfect ending",
                },
            ),
            (
                ("limerence", "crush idealization"),
                {
                    "hook": "You know a few magnetic details, and your mind quietly writes the rest of the person into someone almost impossible to resist.",
                    "cue": "fantasy growing faster than shared experience and uncertainty making the imagined future more vivid",
                    "impulse": "collect tiny signs that confirm the fantasy while ignoring missing or conflicting information",
                    "evidence": "ordinary interactions, real compatibility, returned effort, and facts that survive outside imagination",
                    "experiment": "List what you directly know, what you infer, and what you still need to learn through real interaction.",
                    "question": "I enjoy our connection. Would you like to spend time together and see what is actually here?",
                    "boundary": "I will not organize my life around a relationship that mainly exists in prediction and fantasy.",
                    "healthy": "attraction that becomes more realistic, mutual, and grounded as information increases",
                },
            ),
            (
                ("social media jealousy",),
                {
                    "hook": "One story, like, or follow turns a fragment of online behavior into a full relationship threat before context arrives.",
                    "cue": "repeated profile checking and treating an algorithmic glimpse as complete evidence",
                    "impulse": "investigate harder, compare yourself, or confront the other person with a finished accusation",
                    "evidence": "the agreed relationship boundaries, direct behavior, honest answers, and patterns beyond the screen",
                    "experiment": "Pause checking for forty-eight hours and write the direct question the online clue cannot answer.",
                    "question": "That online interaction brought up insecurity for me. Can we clarify what our boundaries are?",
                    "boundary": "I will discuss real concerns directly instead of monitoring a partner for endless indirect proof.",
                    "healthy": "digital behavior that matches shared expectations without turning surveillance into reassurance",
                },
            ),
            (
                ("friends with benefits",),
                {
                    "hook": "The chemistry is clear, but the rules stay vague until one person's feelings, availability, or expectations begin to change.",
                    "cue": "affection, exclusivity, jealousy, or emotional support increasing beyond the original agreement",
                    "impulse": "avoid the conversation to protect the fun while silently hoping both people want the same change",
                    "evidence": "explicit consent, honest check-ins, respected limits, and freedom to renegotiate or stop",
                    "experiment": "Schedule a calm check-in outside an intimate moment and compare current feelings with the original agreement.",
                    "question": "Is this arrangement still working for you? Have your feelings changed? Have your boundaries changed?",
                    "boundary": "I will not continue a casual arrangement when honesty, consent, or emotional wellbeing is no longer mutual.",
                    "healthy": "an arrangement where both adults can speak clearly, revise consent, and leave without pressure",
                },
            ),
        )
        for terms, profile in profiles:
            if any(term in lowered for term in terms):
                return profile
        return {
            "hook": "One small dating moment changes your body before the meaning is clear, and the story can grow faster than the evidence.",
            "cue": "the first visible change in attention, communication, closeness, or effort",
            "impulse": "react to the strongest feeling before checking the wider pattern",
            "evidence": "reciprocity, clarity, consistency, respect, and repaired misunderstandings",
            "experiment": "Slow one reaction, keep your normal routine, and record what the next three actions actually show.",
            "question": "I enjoy this connection. What are you honestly looking for right now?",
            "boundary": "I will stay warm and interested without abandoning the standards that protect my wellbeing.",
            "healthy": "attraction that remains exciting while reality, consent, and consistency keep shaping the pace",
        }

    def _brain_long_video_plan_legacy(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[ScenePlanItem]:
        spoken_subject = self._spoken_subject(topic)
        profile = self._brain_relationship_profile(spoken_subject)
        profile_points = [
            f"With {spoken_subject}, the first useful cue is {profile['cue']}.",
            f"The common impulse is to {profile['impulse']}. That can lower uncertainty for a moment without answering the larger relationship question.",
            "The feeling becomes louder when suspense, relief, or attraction is treated as proof instead of a reaction that still needs context.",
            "Separate the observable event from the interpretation: write what happened in one sentence, then write the prediction your mind added in another.",
            f"Use {profile['evidence']} as the scoreboard instead of grading the connection by its most emotionally charged minute.",
            f"The cost becomes visible when {spoken_subject} starts stealing sleep, focus, friendships, routines, or the ability to ask a direct question.",
            f"A clear request for this pattern can sound like: {profile['question']}",
            f"A usable boundary stays under your control: {profile['boundary']}",
            f"The healthier contrast is {profile['healthy']}.",
        ]
        expansion_points = list(self._video_expansion_lines(topic, spoken_subject))
        raw_points = [*profile_points[:6], *expansion_points[:3], *profile_points[6:]]
        scenes = list(topic.scene_plan or [])
        middle_scenes = scenes[1:-1] if len(scenes) >= 3 else scenes
        raw_points.extend(scene.narration or scene.visual_text for scene in middle_scenes)
        points = []
        seen = set()
        for raw_point in raw_points:
            point = self._sentence(self._clean(raw_point))
            key = re.sub(r"[^a-z0-9]+", " ", point.lower()).strip()
            if not point or not key or key in seen or self._bad_ai_line_issue(point):
                continue
            seen.add(key)
            points.append(point)
        fallback_points = profile_points
        for fallback in fallback_points:
            key = re.sub(r"[^a-z0-9]+", " ", fallback.lower()).strip()
            if key not in seen:
                seen.add(key)
                points.append(self._sentence(fallback))
        points = points[:9]

        opener = self._tidy_scene_line(self._opener(topic), max_words=24)
        if opener.lower().startswith("you notice "):
            opener = self._tidy_scene_line(profile["hook"], max_words=28)
        out = [
            self._long_scene(
                channel.id,
                subject,
                f"{opener} That reaction is real, but it is not the whole answer. In this episode, we are testing {spoken_subject}: the first visible cue, why uncertainty can amplify it, and which behavior gives you a cleaner answer. The goal is not to become cold or play harder-to-get. It is to keep the chemistry while protecting consent, judgment, and self-respect.",
                "The moment it starts",
            ),
            self._long_scene(
                channel.id,
                subject,
                f"First, treat {spoken_subject} as a repeated pattern to examine, not a diagnosis of you or the other person. One charged date, delayed reply, awkward pause, or nervous conversation is not a personality verdict. We are looking for the cue, the interpretation, the reaction, and the result across more than one moment. Patterns deserve attention; isolated moments deserve context.",
                "Pattern, not diagnosis",
            ),
            self._long_scene(
                channel.id,
                subject,
                f"Map the sequence before trying to explain it. Start with {profile['cue']}. Then write what your body did, what meaning appeared, what action followed, and whether that action created clarity or more confusion. This five-part map stops a powerful feeling from editing the evidence and gives you something concrete to compare when the same trigger returns.",
                "Map the sequence",
            ),
        ]
        body_lenses = [
            "Watch for the first visible cue rather than the most dramatic emotion. The cue may be a phone check, a rehearsed reply, a shift in eye contact, a sudden urge to impress, or a need to repair something nobody has named. Catching that first movement gives you a choice before the entire story accelerates.",
            "Uncertainty can hold attention because the mind keeps searching for the missing answer. That does not make the connection fake, but it can make relief feel like proof of compatibility. Compare how you feel during the comeback with how you feel across the full week, including the quiet and confusing parts.",
            "A short burst of reassurance can lower tension immediately. When relief follows checking, chasing, or overexplaining, the behavior can become easier to repeat the next time uncertainty appears. The practical reset is to delay the familiar move long enough to learn whether the feeling changes without another test of the relationship.",
            "Interpretation is where chemistry can become a story. Separate observable facts from guesses about intention, worth, or the future. Then ask what evidence would genuinely change your conclusion. If every possible outcome confirms the same fear or fantasy, you are no longer testing the idea; you are protecting it.",
            "Use behavior over time as the scoreboard. Plans kept, limits respected, effort returned, misunderstandings repaired, and honest questions answered are stronger signals than one perfectly timed message. This does not remove romance. It protects romance from being graded only by intensity, suspense, or how nervous one moment made you feel.",
            "Notice the cost of the loop. Does it steal sleep, interrupt work, shrink friendships, or make your mood depend on a notification? The goal is not to shame the reaction. The cost tells you where a boundary or routine needs support so attraction remains part of your life instead of becoming the control panel for it.",
            "Clarity works best when it is simple and specific. Ask about the behavior you can name, the pace you want, or the plan that needs an answer. Avoid presenting a complete psychological theory about the other person. A clear request gives both people a fair chance to respond without turning the conversation into a trial.",
            "A boundary is not a threat designed to force the desired response. It is the action you will take to protect your time, dignity, and emotional stability if the pattern continues. The strongest boundary is realistic, calm, and under your control. It does not require the other person to agree with your interpretation first.",
            "Now compare the pattern with a healthier version. Attraction can still include nerves, longing, playful tension, and vulnerable moments. The difference is that respect and consistency keep returning after the spark. You spend less time decoding basic interest and more time discovering whether your values, timing, communication, and everyday lives can actually fit.",
        ]
        for index, (point, lens) in enumerate(zip(points, body_lenses), start=1):
            out.append(
                self._long_scene(
                    channel.id,
                    subject,
                    f"{point} {lens}",
                    (
                        "The first visible cue",
                        "Why uncertainty hooks attention",
                        "How relief trains the loop",
                        "Fact versus interpretation",
                        "Use behavior as evidence",
                        "Measure the real cost",
                        "Ask for clarity",
                        "Set a usable boundary",
                        "What healthy attraction adds",
                    )[index - 1],
                )
            )
        out.extend(
            [
                self._long_scene(
                    channel.id,
                    subject,
                    f"Avoid the two reactions that create the least useful evidence. The first is to {profile['impulse']}. The second is to perform indifference and never ask what you genuinely need to know. A stronger move is emotionally honest and behaviorally calm: keep your routine, make one clear request, and let the next actions show whether the connection can support mutual effort.",
                    "Two reactions to avoid",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"Move the camera away from the most dramatic moment and study ordinary evidence. For {spoken_subject}, look at {profile['evidence']}. A magnetic date, kiss, message, or apology can create a powerful memory, but compatibility is easier to see when nobody is performing. Ordinary behavior shows whether the spark can survive schedules, limits, tired evenings, and small misunderstandings.",
                    "Use ordinary evidence",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"Turn the boundary into something you can actually keep: {profile['boundary']} This is not punishment and it is not a strategy to manufacture desire. A boundary protects time, emotional energy, physical autonomy, and attention while trust is still being earned. Attraction can stay real without receiving unlimited access before the evidence supports it.",
                    "A boundary you can keep",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"Try one seven-day experiment designed for {spoken_subject}. {profile['experiment']} At the end of the week, review what repeated, what became clearer, and whether your own level of calm changed. The goal is honest observation. Do not act unavailable. Never try to control someone. It is a way to collect cleaner evidence before the strongest feeling writes the conclusion.",
                    "A seven-day reality test",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"Use a conversation that leaves room for both people instead of presenting a psychological verdict. Try this: {profile['question']} Then listen without rushing to soften the answer, and compare it with later behavior. A direct question cannot guarantee the response you want, but it can replace hours of decoding with information both people understand.",
                    "The exact conversation",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"If {spoken_subject} repeatedly creates fear, isolation, coercive pressure, loss of sleep, or difficulty functioning, a licensed mental-health professional can help you examine the full context. A video can offer language and an experiment; it cannot diagnose you, diagnose a partner, or decide whether a relationship is safe. Consent, safety, and emotional wellbeing always outrank the excitement of the storyline.",
                    "Know when to get support",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"Here is the reset for {spoken_subject}. Notice {profile['cue']}. Separate fact from prediction. Compare the moment with {profile['evidence']}. Ask clearly, then keep the boundary you chose. Chemistry deserves attention, but repeated behavior earns trust. The confident response is not pretending you feel nothing; it is staying warm and steady enough to let reality answer.",
                    "The complete reset",
                ),
                self._long_scene(
                    channel.id,
                    subject,
                    f"Take this contrast into the next real interaction. A healthier alternative to the loop around {spoken_subject} is {profile['healthy']}. If the connection becomes clearer when you slow down, ask directly, and keep your standards, you gain useful information. If confusion remains the main source of intensity, that is information too. Keep the spark, remove the games, and let mutual behavior decide what deserves more access.",
                    "The final contrast",
                ),
            ]
        )
        return out

    def _friends_with_benefits_caption_safe_sections(self) -> list[tuple[str, str]]:
        """Return the source-bounded FWB episode in indivisible caption sentences."""

        return [
            (
                "The moment that starts the story",
                "The chemistry feels unmistakably real. The agreement still feels dangerously vague. "
                "Feelings can change between encounters. Availability can shift without warning. "
                "Expectations can quietly grow beyond consent. This episode follows those changing signals. "
                "We will separate chemistry from evidence. We will protect desire without playing games. "
                "Consent, dignity, and judgment stay together. Reality gets time to answer clearly.",
            ),
            (
                "What the event actually proves",
                "Begin with one strict limit. Examine behavior without diagnosing anybody. "
                "Ask what happened more than once. Recall what both people actually discussed. "
                "Notice what followed any rising tension. One painful moment proves very little. "
                "It cannot measure your human worth. It cannot reveal someone's hidden motives. "
                "Repeated patterns deserve closer attention. Isolated moments still deserve context.",
            ),
            (
                "Replay the first hour",
                "Rewind to the first visible change. Affection may deepen beyond the agreement. "
                "Exclusivity might become quietly assumed. Jealousy may enter ordinary conversations. "
                "Emotional support might expand unexpectedly. Name each action before naming emotions. "
                "Notice any urge to cross limits. Notice any unfinished honest sentence. "
                "Early awareness creates another choice. The familiar reaction becomes less inevitable.",
            ),
            (
                "When uncertainty leaves questions open",
                "Uncertainty can leave important questions open. Charged moments often feel strangely clarifying. "
                "They rarely settle an unstated agreement. Compare that spike with the week. "
                "Study calm moments between intimate ones. Attraction can remain completely genuine. "
                "Compatibility may still remain uncertain. Both truths can exist together. "
                "Uncertainty deserves questions, not invented certainty. Patterns matter more than emotional peaks.",
            ),
            (
                "Facts on one side, predictions on the other",
                "Create two short columns. Place only observable facts first. "
                "Write what each person actually said. Record which plans or limits changed. "
                "Note which questions remained unanswered. Place every prediction beside those facts. "
                "Predictions may feel completely reasonable. They still are not evidence. "
                "Could any behavior change your conclusion? If nothing could, stop calling it testing.",
            ),
            (
                "Pause before the familiar reaction",
                "You may want to avoid talking. That impulse protects tonight's easy chemistry. "
                "It may also preserve silent assumptions. Notice the urge without obeying immediately. "
                "Never weaponize a thoughtful pause. Finish one ordinary task first. "
                "Return when your request feels clear. State what you need without guessing. "
                "The pause supports reflection, not control. Honesty protects more than temporary ease.",
            ),
            (
                "Pause before a high-stakes response",
                "Pause before high-stakes messages. Pause before accusations or promises. "
                "Unclench your jaw and exhale slowly. Drink water or step outside. "
                "Call a trusted, grounded friend. Choose someone who avoids gossip. "
                "Describe the problem in plain words. No pause guarantees better judgment. "
                "It simply creates deliberate choice. Warm responses can hold firm boundaries.",
            ),
            (
                "Use a behavior scoreboard",
                "Build a behavior-based scoreboard. Give explicit consent the greatest weight. "
                "Count honest check-ins and respected limits. Count freedom to renegotiate or stop. "
                "Notice whether effort stays reciprocal. Notice whether misunderstandings get repaired. "
                "Follow-through tests whether sincerity supports trust. One perfect moment proves less. "
                "Chemistry deserves enjoyment, not blind authority. Trust requires patterns beyond suspense.",
            ),
            (
                "Chemistry is not the same as repair",
                "A charged reunion can feel resolving. Brief relief can mimic real repair. "
                "Repair asks for more evidence. Can both people name what happened? "
                "Does a specific change follow? Can either person choose slower? "
                "Can either person freely stop? Does new behavior survive ordinary days? "
                "Physical closeness cannot replace consent. Sexual tension cannot replace accountability. "
                "Affection cannot create emotional availability.",
            ),
            (
                "Keep the event separate from your worth",
                "Someone's uncertainty may genuinely hurt. It does not define your worth. "
                "Their choices show current capacity. They do not rate your lovability. "
                "Stop auditioning for irresistible perfection. You cannot control another person's answer. "
                "Ask whether clarity stays mutual. Ask whether reciprocity feels consistent. "
                "Ask whether consent feels emotionally safe. Protect sleep, focus, friends, and dignity.",
            ),
            (
                "Study the boring evidence",
                "Study the boring evidence closely. Who initiates ordinary plans? "
                "Who remembers stated limits? What happens during tired evenings? "
                "What follows a small misunderstanding? Watch behavior beyond intimate dates. "
                "Compatibility often looks surprisingly ordinary. It appears through honesty and consideration. "
                "It survives schedules and minor repairs. Crisis-only intensity deserves careful attention. "
                "That contrast provides useful information.",
            ),
            (
                "Ask one clean question",
                "Ask three direct questions. Is this arrangement still working? "
                "Have your feelings changed? Have your boundaries changed? "
                "Choose a calm, non-intimate moment. Avoid speeches built for agreement. "
                "Directness cannot guarantee mutual interest. It can replace endless decoding. "
                "Both people gain shared information. Different intentions may still sting. "
                "They also prevent imaginary agreements.",
            ),
            (
                "Read the answer in three layers",
                "Read answers through three layers. First, hear the actual words. "
                "Next, notice useful specificity. Then, watch later follow-through. "
                "Clear answers name realistic intentions. Specificity can turn intention into plans. "
                "Later behavior tests those plans. People may feel nervous or uncertain. "
                "Do not demand flawless certainty. Repeated vagueness also communicates something. "
                "Grade patterns, not polished tone.",
            ),
            (
                "Set a boundary you control",
                "Choose a boundary you control. Here is one clear version. "
                "I will not continue this arrangement. Honesty and consent must remain mutual. "
                "Both people's emotional wellbeing matters. This boundary never forces agreement. "
                "It does not punish or manipulate. It names access you will offer. "
                "It names actions you control. Keep it workable during lonely nights. "
                "Protect time, attention, energy, and autonomy.",
            ),
            (
                "A personal observation window",
                "Use a personal observation window. Do not call it validated testing. "
                "Schedule one calm check-in. Meet outside an intimate moment. "
                "Compare current feelings with prior agreements. Keep work, sleep, movement, and friendships visible. "
                "Notice your urges without diagnosing them. Record direct behavior without diagnosing anyone. "
                "Review after one suitable timeframe. One week may fit some people. "
                "Another timeframe may fit better. Never provoke jealousy or strategic silence.",
            ),
            (
                "If the intensity suddenly returns",
                "Renewed chemistry offers new information. It does not prove compatibility. "
                "It does not prove repair. Ask what has actually changed. "
                "Ask what each person wants now. Check whether stated boundaries remain respected. "
                "Stay curious without reopening every door. Intensity never creates a new agreement. "
                "Clear words need sustained action. Test trust slowly while reality unfolds. "
                "Consent stays revisable during reconnection.",
            ),
            (
                "Avoid the two false power moves",
                "Two false power moves distort evidence. First, avoid the honest conversation. "
                "Second, perform cold indifference. Do not post for reactions. "
                "Do not disappear before they can. Both moves obscure useful answers. "
                "Respond with calm honesty instead. Keep your normal routine visible. "
                "Make one clear request. Let later actions answer. "
                "Confidence never means caring less. It means keeping consent and choice.",
            ),
            (
                "What the healthier contrast looks like",
                "Healthier arrangements keep communication open. Both adults can speak clearly. "
                "Both can revise consent freely. Either can leave without pressure. "
                "Longing and flirtation can remain. Nervous laughter can remain too. "
                "Respect keeps returning after chemistry. Consistency survives ordinary life. "
                "Less time goes toward decoding. More time reveals genuine compatibility. "
                "Healthy never means perfectly secure. Adults can pause without punishment. "
                "Adults can change without coercion.",
            ),
            (
                "Know when a video is not enough",
                "Sometimes a video is insufficient. Fear deserves serious attention. "
                "So do isolation and coercive pressure. Stalking and threats require support. "
                "Difficulty functioning also matters. Licensed professionals can examine context. "
                "Local safety resources may help. This episode cannot diagnose anybody. "
                "It cannot declare relationships safe. Consent and physical safety come first. "
                "Emotional wellbeing outranks every storyline.",
            ),
            (
                "Let reality deliver the ending",
                "Here is your complete reset. Catch the earliest visible cue. "
                "Separate facts from predictions. Pause before reacting automatically. "
                "Ask one clean question. Compare answers with ordinary behavior. "
                "Keep the boundary you chose. Never erase chemistry to feel safer. "
                "Never pretend hurt cannot reach you. Stay warm while reality answers. "
                "Growing clarity supports careful exploration. Ongoing confusion also provides information. "
                "Intensity alone never deserves more access.",
            ),
        ]
        return self._require_caption_safe_sections(
            sections,
            label="Friends with benefits long",
        )

    def _brain_long_video_plan(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[ScenePlanItem]:
        """Build a progressive, behavior-first relationship episode without idea loops."""
        spoken_subject = self._spoken_subject(topic)
        profile = self._brain_relationship_profile(spoken_subject)
        if "friends with benefits" in spoken_subject.lower():
            return [
                self._long_scene(channel.id, subject, narration, caption)
                for caption, narration in self._friends_with_benefits_caption_safe_sections()
            ]
        opener = self._tidy_scene_line(self._opener(topic), max_words=26)
        if self._is_generic_opener(opener) or opener.lower().startswith("you notice "):
            opener = self._tidy_scene_line(profile["hook"], max_words=30)

        topic_identity = f"{spoken_subject} {topic.title}".lower()
        phone_story = any(
            marker in topic_identity
            for marker in (
                "text", "reply", "ghost", "orbiting", "slow fading", "breadcrumb",
                "social media", "checking an ex", "dating app", "online dating",
                "voice note", "late night", "closure seeking",
            )
        )
        opening_caption = (
            "The silence that starts the story"
            if phone_story
            else "The moment that starts the story"
        )
        opening_bridge = (
            "The phone stays silent while the mind drafts its most emotionally charged explanation."
            if phone_story
            else "The body can register intensity before the agreement, intention, or meaning is clear."
        )
        cue_examples = (
            "Notice the phone unlock or profile search. Catch the drafted paragraph. "
            "Notice the urge to send another message."
            if phone_story
            else "Notice any changed distance. Catch the unfinished sentence or rush to explain. "
            "Notice the urge to cross a limit before asking."
        )
        impulse_scene = profile.get(
            "impulse_scene",
            f"The common impulse is to {profile['impulse']}.",
        )
        return_caption = (
            "If they suddenly reappear"
            if phone_story
            else "If the intensity suddenly returns"
        )
        return_scene = (
            "A return is new information, not automatic repair. Ask what changed, what the person is proposing now, and whether it respects the boundary already stated. You can remain curious without reopening every level of access in one night. Warmth without accountability or a plan is a flattering feeling, not evidence of repair. Clear ownership plus sustained action deserves a slower test. Let trust rebuild gradually enough for reality to stay visible."
            if phone_story
            else "Treat a renewed rush of chemistry as new information, not automatic compatibility or repair. Ask what is actually different, what each person wants now, and whether the moment respects the boundary already stated. You can remain curious without accelerating every level of access at once. Intensity without clarity is a feeling, not a new agreement. Clear words plus sustained action deserve a slower test so reality remains visible."
        )

        sections = [
            (
                opening_caption,
                f"{opener} {opening_bridge} We will reconstruct {spoken_subject} from the first visible cue to the evidence that deserves trust. The aim is not to become cold, manufacture jealousy, or pretend the feeling meant nothing. It is to keep desire, dignity, consent, and judgment in the same room while the evidence catches up.",
            ),
            (
                "What the event actually proves",
                f"Start with a strict limit: examine the specific behavior around {spoken_subject}; do not diagnose either person from a label. What happened more than once? What expectation had actually been discussed? What did each person do after tension appeared? One painful moment cannot rate your worth or reveal their hidden motives.",
            ),
            (
                "Replay the first hour",
                f"Rewind to that first hour. Notice when the loop became visible. The cue may be {profile['cue']}. Name the action before naming the emotion. {cue_examples} This movement happens before the full story hardens. Catch it early and you gain a choice before the familiar reaction feels inevitable.",
            ),
            (
                "When uncertainty leaves questions open",
                "Uncertainty can leave important questions unresolved. A charged moment may feel clarifying without settling the agreement. Compare that emotional spike with the whole week around it. Attraction can be genuine while the surrounding pattern remains incompatible with the relationship you want.",
            ),
            (
                "Facts on one side, predictions on the other",
                "Make two short columns. In the first, write only observable facts: what was said, which plan changed, what question went unanswered, or whether an apology named a specific action. In the second, write the prediction your mind added about intention, worth, or the future. Predictions may feel reasonable. They are not evidence. Ask what new behavior would genuinely change your conclusion. If no possible outcome could change it, you are defending a story rather than testing one.",
            ),
            (
                "Pause before the familiar reaction",
                f"You may feel tempted to {profile['impulse']}. Notice that impulse without treating it as proof. Never turn a pause into a dating tactic. Step away briefly, finish one ordinary task, and return when you can state what you want clearly. The pause is for reflection, not control.",
            ),
            (
                "Pause before a high-stakes response",
                "If emotions are running high, pause before an important message, accusation, or promise. Unclench your jaw, slow your exhale, drink water, step outside, or call a trusted friend who will not turn uncertainty into gossip. Then describe the problem in one plain sentence. This pause cannot guarantee better judgment; it simply creates time to choose deliberately. A warm response and a boundaried response can be the same response.",
            ),
            (
                "Use a behavior scoreboard",
                f"Now grade the connection with {profile['evidence']}. Give more weight to plans kept, limits respected, effort returned, misunderstandings repaired, and honest questions answered than to one perfectly timed moment. Follow-through shows whether sincerity can support trust. This scoreboard protects romance from being evaluated only by suspense, chemistry, and the moment that made you light up.",
            ),
            (
                "Chemistry is not the same as repair",
                "A charged reunion can feel like resolution because the tension disappears for a moment. Repair asks for more. Can both people name what happened without rewriting it? Is there a specific change, a realistic plan, and room for either person to say no or slower? Does the new behavior survive an ordinary week? Physical closeness, affectionate language, or sexual tension never substitutes for consent, accountability, and emotional availability.",
            ),
            (
                "Keep the event separate from your worth",
                "Someone else's hesitation, inconsistency, or uncertainty may hurt. It does not prove you were too much, unattractive, or impossible to love. Their behavior gives information about what they can offer in this connection; it does not issue a universal rating of you. Do not ask how to become irresistible enough to control the answer. Does this pattern offer clarity and reciprocity? Does it feel emotionally safe? Protect your sleep, focus, friendships, and self-respect.",
            ),
            (
                "Study the boring evidence",
                "Move the camera away from the hottest scene and inspect the ordinary moments. Who initiates a plan? Who remembers a stated limit? What happens on a tired evening? What follows a misunderstanding? Notice how they behave away from a date. Compatibility often looks less cinematic than attraction. It appears in scheduling, honesty, consideration, and the ability to repair small ruptures. Does intensity appear only during crisis or reunion? That contrast offers useful information.",
            ),
            (
                "Ask one clean question",
                f"Ask a question that leaves room for both people instead of presenting a psychological theory. Try: {profile['question']} Ask at a calm time. Do not use a speech designed to produce the desired answer. Direct communication cannot guarantee mutual interest. It can replace hours of decoding. Both people gain shared information. A no, a vague answer, or a different intention may sting while still protecting you from an imaginary agreement.",
            ),
            (
                "Read the answer in three layers",
                "Listen to the words, the specificity, and the follow-through. A clear answer names what the person wants and can realistically do. Specificity turns intention into a plan; later behavior shows whether it survives outside the conversation. People can be nervous, busy, or uncertain, so do not demand perfection. But repeated vagueness that keeps access open while responsibility stays absent is also an answer. Grade the pattern, not tone alone.",
            ),
            (
                "Set a boundary you control",
                f"A usable boundary sounds like this: {profile['boundary']} It is not a threat, punishment, or trick for making someone chase. It states what access you will offer and what action you will take if the pattern continues. Keep it realistic on a lonely night. If it depends on the other person agreeing with your interpretation, it is still a negotiation. A boundary under your control protects time, attention, emotional energy, and physical autonomy.",
            ),
            (
                "A personal observation window",
                "Use a personal observation window. This is not a validated test. Schedule one calm check-in. Meet outside an intimate moment. Compare current feelings with prior agreements. Keep work, sleep, movement, and friendships visible. Notice your urges without diagnosing them. Record direct behavior without diagnosing anyone. Review after one suitable timeframe. One week may fit some people. Another timeframe may fit better. Do not act unavailable, provoke jealousy, or use silence to control the outcome.",
            ),
            (
                return_caption,
                return_scene,
            ),
            (
                "Avoid the two false power moves",
                f"One false move means you {profile['impulse']}. The second performs indifference, posts for a reaction, or disappears first. Both reduce clean evidence. Respond honestly and calmly. Keep your routine, make one clear request, and allow the next actions to answer. Confidence is not winning a contest of who cares less. It is caring without surrendering consent, standards, or control of your choices.",
            ),
            (
                "What the healthier contrast looks like",
                f"The healthier contrast is {profile['healthy']}. It can still include longing, flirtation, nervous laughter, an intense kiss, and vulnerable conversation. The difference is that respect and consistency return after the spark. You spend less time decoding interest and more time learning whether values, pace, communication, and ordinary lives fit. Healthy is not emotionless or perfectly secure; both adults can ask, pause, repair, and change their minds without punishment or coercion.",
            ),
            (
                "Know when a video is not enough",
                "If this situation includes fear, isolation, coercive pressure, stalking, threats, or difficulty functioning, seek support beyond a video. A licensed mental-health professional can examine the full context, and local safety resources matter when control or violence is present. This episode offers language, a question, and an experiment; it cannot diagnose either person or decide whether a relationship is safe. Consent, physical safety, and wellbeing outrank the storyline.",
            ),
            (
                "Let reality deliver the ending",
                "Here is the reset. Catch the first cue. Separate facts from predictions. Regulate before reacting. Ask one clean question, compare the answer with ordinary behavior, and keep your boundary. The goal is not to erase chemistry or become impossible to hurt. Stay warm and steady enough for reality to answer. Growing clarity gives you something real to explore. If confusion remains the main source of intensity, you do not need another dramatic scene to believe that answer. Ordinary days provide the strongest contrast. Consistent answers reduce invented suspense. Repeated avoidance also supplies useful information. You never need perfect certainty. You do need enough clarity. Choices remain yours throughout the process. Evidence matters more than emotional theater. Your values still guide future access.",
            ),
        ]
        # Mild skeleton variation: rotate mid-episode chapter order by subject.
        skeleton_seed = sum(ord(ch) for ch in spoken_subject.lower()) + len(sections)
        head, mid, tail = sections[:2], sections[2:-3], sections[-3:]
        if mid and skeleton_seed % 3 == 1:
            mid = mid[1:] + mid[:1]
        elif mid and skeleton_seed % 3 == 2:
            mid = list(reversed(mid[:4])) + mid[4:]
        sections = head + mid + tail
        return [
            self._long_scene(channel.id, subject, narration, caption)
            for caption, narration in sections
        ]

    def _long_video_fallback_plan(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        subject: str,
    ) -> list[ScenePlanItem]:
        if channel.id == "brain_lens":
            return self._brain_long_video_plan(channel, topic, subject)
        return self._history_long_video_plan(channel, topic, subject)

    def _polish_scene_plan(self, channel: ChannelConfig, topic: TopicCandidate, content_kind: str = "short") -> TopicCandidate:
        subject = self._display_subject(topic.subject or self._headline_subject(topic.title) or topic.title, channel.id)
        curated_short_plan = self._curated_short_scene_plan(channel, topic, content_kind)
        if curated_short_plan:
            narration = " ".join(scene.narration for scene in curated_short_plan)
            narration_words = self._word_count(narration)
            if not 65 <= narration_words <= 100:
                raise ValueError(
                    f"Curated Short has {narration_words} words; expected 65 to 100"
                )
            beats = self._sentences(narration)
            return replace(
                topic,
                hook=beats[0],
                narration=narration,
                narration_beats=beats,
                visual_captions=[
                    scene.visual_text
                    for scene in curated_short_plan[1:-1]
                    if scene.visual_text
                ],
                scene_plan=curated_short_plan,
                content_kind=content_kind,
            )
        if content_kind == "video" and self._word_count(topic.narration) >= 700:
            section_plan = self._video_sections_from_narration(channel, topic, subject)
            if section_plan:
                section_plan = self._fit_long_plan(channel, section_plan, content_kind)
                beats = [scene.narration for scene in section_plan if scene.narration]
                visual_captions = [scene.visual_text for scene in section_plan if scene.visual_text]
                return replace(
                    topic,
                    hook=self._sentences(beats[0])[0] if beats and self._sentences(beats[0]) else topic.hook,
                    narration=" ".join(beats),
                    narration_beats=beats,
                    visual_captions=visual_captions,
                    scene_plan=section_plan,
                    content_kind=content_kind,
                )
        if content_kind == "video":
            fallback_plan = self._fit_long_plan(
                channel, self._long_video_fallback_plan(channel, topic, subject), content_kind
            )
            beats = [scene.narration for scene in fallback_plan if scene.narration]
            visual_captions = [scene.visual_text for scene in fallback_plan if scene.visual_text]
            return replace(
                topic,
                hook=self._sentences(beats[0])[0] if beats and self._sentences(beats[0]) else topic.hook,
                narration=" ".join(beats),
                narration_beats=beats,
                visual_captions=visual_captions,
                scene_plan=fallback_plan,
                content_kind=content_kind,
            )

        frame_hint = "landscape documentary frame" if content_kind == "video" else "vertical short frame"
        base_plan = topic.scene_plan or self._fallback_scene_plan(topic, content_kind=content_kind)
        if channel.id == "brain_lens" and content_kind == "short":
            proposed_opener = base_plan[0].narration if base_plan else ""
            opener_line = (
                proposed_opener
                if proposed_opener
                and not self._is_generic_opener(proposed_opener)
                and self._brain_lens_behavior_first(proposed_opener)
                and self._brain_lens_hook_matches(topic, proposed_opener)
                else self._opener(topic)
            )
            tailored_lines = self._video_expansion_lines(topic, self._spoken_subject(topic))[:6]
            rebuilt_lines = [opener_line, *tailored_lines, self._closer(topic)]
            base_plan = [
                ScenePlanItem(
                    narration=self._sentence(line),
                    visual_text=self._captionize(line) or subject,
                    search_terms=[subject, self._captionize(line) or subject],
                    preferred_image_url="",
                    visual_prompt=f"{subject}. {self._captionize(line) or subject}. cinematic relationship b-roll, realistic, {frame_hint}",
                )
                for line in rebuilt_lines
            ]

        polished: list[ScenePlanItem] = []
        seen_middle = set()
        middle_max_words = 30 if content_kind == "video" else (18 if channel.id == "brain_lens" else 22)
        for idx, scene in enumerate(base_plan):
            is_opener = idx == 0
            is_closer = idx == len(base_plan) - 1
            if is_opener:
                narration = self._retention_opener(topic, base_plan, content_kind=content_kind)
                key = re.sub(r"[^a-z0-9]+", " ", narration.lower()).strip()
                if key:
                    seen_middle.add(key)
            elif is_closer:
                narration = self._closer(topic)
            else:
                scene_text = scene.narration or scene.visual_text
                if channel.id == "ancient_history":
                    scene_text = self._simplify_history_line(scene_text)
                narration = self._tidy_scene_line(scene_text, max_words=middle_max_words)
                if not narration:
                    continue
                bad_issue = self._bad_ai_line_issue(narration)
                if bad_issue:
                    continue
                key = re.sub(r"[^a-z0-9]+", " ", narration.lower()).strip()
                if any(key.startswith(existing) or existing.startswith(key) for existing in seen_middle):
                    continue
                seen_middle.add(key)

            caption_source = narration if is_opener or is_closer else (scene.visual_text or narration)
            visual_text = self._captionize(caption_source) or subject
            search_terms = list(dict.fromkeys(scene.search_terms or [subject]))
            visual_prompt = scene.visual_prompt or f"{subject}. {visual_text}. documentary still, realistic, cinematic lighting, {frame_hint}"
            if channel.id == "brain_lens" and content_kind == "short":
                search_terms = self._brain_lens_visual_search_terms(
                    subject,
                    narration,
                    content_kind=content_kind,
                )
                visual_prompt = (
                    f"{subject}. {visual_text}. young adult age 22 to 35, no children, realistic emotional behavior, "
                    f"cinematic relationship b-roll, {frame_hint}"
                )
            polished.append(
                ScenePlanItem(
                    narration=narration,
                    visual_text=visual_text,
                    search_terms=search_terms,
                    preferred_image_url=scene.preferred_image_url,
                    visual_prompt=visual_prompt,
                )
            )

        minimum_scenes = 8 if content_kind == "video" else 5
        if len(polished) < minimum_scenes:
            fillers = [self._tidy_scene_line(c, max_words=16) for c in topic.visual_captions if self._tidy_scene_line(c, max_words=16)]
            for filler in fillers:
                key = re.sub(r"[^a-z0-9]+", " ", filler.lower()).strip()
                if any(key.startswith(existing) or existing.startswith(key) for existing in seen_middle):
                    continue
                seen_middle.add(key)
                polished.insert(-1, ScenePlanItem(
                    narration=filler,
                    visual_text=self._captionize(filler) or subject,
                    search_terms=[subject, self._captionize(filler) or subject],
                    preferred_image_url="",
                    visual_prompt=f"{subject}. {self._captionize(filler) or subject}. documentary still, {frame_hint}",
                ))
                if len(polished) >= minimum_scenes:
                    break

        max_scenes = 14 if content_kind == "video" else 10
        target_words = self._target_words(channel, content_kind=content_kind)
        expansion_lines = list(self._video_expansion_lines(topic, self._spoken_subject(topic)))
        random.shuffle(expansion_lines)
        expansion_index = 0
        expansion_word_limit = 32 if content_kind == "video" else (18 if channel.id == "brain_lens" else 24)
        while len(polished) < max_scenes and self._word_count(" ".join(scene.narration for scene in polished)) < target_words:
            line = self._tidy_scene_line(
                expansion_lines[expansion_index % len(expansion_lines)],
                max_words=expansion_word_limit,
            )
            expansion_index += 1
            key = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
            if any(key.startswith(existing) or existing.startswith(key) for existing in seen_middle):
                if expansion_index > len(expansion_lines) * 3:
                    break
                continue
            seen_middle.add(key)
            polished.insert(-1, ScenePlanItem(
                narration=line,
                visual_text=self._captionize(line) or subject,
                search_terms=[subject, self._captionize(line) or subject],
                preferred_image_url="",
                visual_prompt=f"{subject}. {self._captionize(line) or subject}. documentary still, {frame_hint}",
            ))

        if len(polished) > max_scenes:
            polished = polished[: max_scenes - 1] + [polished[-1]]

        polished = self._fit_long_plan(channel, polished, content_kind)

        if channel.id == "brain_lens" and content_kind == "short" and len(polished) >= 2:
            word_budget = self._target_words(channel, content_kind=content_kind)
            opener_scene = replace(
                polished[0],
                narration=self._tidy_scene_line(polished[0].narration, max_words=20),
            )
            closer_scene = replace(
                polished[-1],
                narration=self._tidy_scene_line(polished[-1].narration, max_words=18),
            )
            middle_scenes = [
                replace(scene, narration=self._tidy_scene_line(scene.narration, max_words=22))
                for scene in polished[1:-1]
            ]
            middle_scenes = [scene for scene in middle_scenes if scene.narration]
            selected_middle: list[ScenePlanItem] = []
            fixed_words = self._word_count(opener_scene.narration) + self._word_count(closer_scene.narration)
            used_words = fixed_words
            for scene in middle_scenes:
                scene_words = self._word_count(scene.narration)
                if len(selected_middle) < 4 or used_words + scene_words <= word_budget:
                    selected_middle.append(scene)
                    used_words += scene_words
                if len(selected_middle) >= 8:
                    break
            polished = [opener_scene, *selected_middle, closer_scene]
        elif channel.id == "ancient_history" and content_kind == "short" and len(polished) >= 2:
            word_budget = self._target_words(channel, content_kind=content_kind)
            opener_scene = replace(
                polished[0],
                narration=self._tidy_scene_line(polished[0].narration, max_words=22),
            )
            closer_scene = replace(
                polished[-1],
                narration=self._tidy_scene_line(polished[-1].narration, max_words=22),
            )
            middle_scenes = [
                replace(scene, narration=self._tidy_scene_line(scene.narration, max_words=22))
                for scene in polished[1:-1]
            ]
            middle_scenes = [scene for scene in middle_scenes if scene.narration]
            selected_middle: list[ScenePlanItem] = []
            used_words = self._word_count(opener_scene.narration) + self._word_count(closer_scene.narration)
            for scene in middle_scenes:
                scene_words = self._word_count(scene.narration)
                if len(selected_middle) < 4 or used_words + scene_words <= word_budget:
                    selected_middle.append(scene)
                    used_words += scene_words
                if len(selected_middle) >= 8:
                    break
            polished = [opener_scene, *selected_middle, closer_scene]

        beats = [scene.narration for scene in polished if scene.narration]
        narration = self._reduce_subject_repetition(" ".join(beats), subject, channel.id)
        beats = self._sentences(narration) if content_kind == "short" else beats
        if channel.id == "brain_lens" and content_kind == "short":
            narration_words = self._word_count(narration)
            if narration_words < self._BRAIN_SHORT_MIN_WORDS:
                fill_lines = list(self._video_expansion_lines(topic, subject))
                random.shuffle(fill_lines)
                for line in fill_lines:
                    clean = self._tidy_scene_line(line, max_words=22)
                    if not clean:
                        continue
                    beats.insert(-1 if len(beats) >= 2 else len(beats), clean)
                    narration = self._reduce_subject_repetition(" ".join(beats), subject, channel.id)
                    beats = self._sentences(narration)
                    if self._word_count(narration) >= self._BRAIN_SHORT_MIN_WORDS:
                        break
            narration_words = self._word_count(narration)
            if narration_words < self._BRAIN_SHORT_MIN_WORDS:
                raise ValueError(
                    "Brain Lens Short has only "
                    f"{narration_words} words; needs at least "
                    f"{self._BRAIN_SHORT_MIN_WORDS} for 50-59s pacing"
                )
            if narration_words > self._BRAIN_SHORT_MAX_WORDS:
                raise ValueError(
                    "Brain Lens Short has "
                    f"{narration_words} words; exceeds the "
                    f"{self._BRAIN_SHORT_MAX_WORDS}-word safety ceiling"
                )
        if channel.id == "ancient_history" and content_kind == "short":
            narration_words = self._word_count(narration)
            if narration_words < self._ANCIENT_SHORT_MIN_WORDS:
                fill_lines = list(self._video_expansion_lines(topic, subject))
                random.shuffle(fill_lines)
                for line in fill_lines:
                    clean = self._tidy_scene_line(line, max_words=24)
                    if not clean:
                        continue
                    beats.insert(-1 if len(beats) >= 2 else len(beats), clean)
                    narration = self._reduce_subject_repetition(" ".join(beats), subject, channel.id)
                    beats = self._sentences(narration)
                    if self._word_count(narration) >= self._ANCIENT_SHORT_MIN_WORDS:
                        break
            narration_words = self._word_count(narration)
            if narration_words < self._ANCIENT_SHORT_MIN_WORDS:
                raise ValueError(
                    "Ancient Short has only "
                    f"{narration_words} evidence-backed words; needs at least "
                    f"{self._ANCIENT_SHORT_MIN_WORDS} for natural pacing"
                )
            if narration_words > self._ANCIENT_SHORT_MAX_WORDS:
                raise ValueError(
                    "Ancient Short has "
                    f"{narration_words} words; exceeds the "
                    f"{self._ANCIENT_SHORT_MAX_WORDS}-word safety ceiling"
                )
        visual_captions = [scene.visual_text for scene in polished[1:-1] if scene.visual_text]
        return replace(topic, hook=beats[0] if beats else topic.hook, narration=narration, narration_beats=beats, visual_captions=visual_captions, scene_plan=polished, content_kind=content_kind)

    def _word_count_ok(self, text: str, content_kind: str = "short") -> bool:
        words = [w for w in text.split(" ") if w.strip()]
        if content_kind == "video":
            return 850 <= len(words) <= 1700
        return 110 <= len(words) <= 180

    # Extended style descriptions for richer script instructions
    _STYLE_INSTRUCTIONS = {
        "explainer":      "Clear, logical breakdown. Define the concept, then show why it matters, then give a real-world example.",
        "story":          "Narrative arc with a protagonist, conflict, and resolution. Use sensory details.",
        "listicle":       "Numbered reveals. Each point escalates in impact. Use short punchy sentences.",
        "documentary":    "Cinematic and authoritative. Build dramatic tension with precise facts. Each sentence should paint a vivid scene.",
        "talking-head":   "Direct, conversational, warm. Speak to one person as if they are in the room with you.",
        "infographic":    "Data-first. Lead with a statistic or comparison. Structure as: fact → context → implication.",
        "dark-mystery":   "Suspenseful and atmospheric. Withhold the key reveal until the end. Build dread with specific details.",
        "motivational":   "High energy, empowering. Use 'you' language. Build to an inspiring conclusion.",
    }

    def _style_instruction(self, style: str) -> str:
        return self._STYLE_INSTRUCTIONS.get(
            style.lower().strip(),
            "Engaging and informative. Use short punchy sentences with a strong hook."
        )

    def _dna_context(self, dna: dict | None) -> str:
        """Build a compact DNA context string to inject into the script prompt."""
        if not dna:
            return ""
        parts = []
        if dna.get("tone"):
            parts.append(f"Tone: {dna['tone']}")
        if dna.get("pacing"):
            parts.append(f"Pacing: {dna['pacing']}")
        if dna.get("hook_patterns"):
            hooks = "; ".join(dna["hook_patterns"][:3])
            parts.append(f"Hook patterns to emulate: {hooks}")
        if dna.get("script_instructions"):
            parts.append(f"Style rule: {dna['script_instructions']}")
        if dna.get("emotion_triggers"):
            triggers = ", ".join(dna["emotion_triggers"][:3])
            parts.append(f"Target emotions: {triggers}")
        if not parts:
            return ""
        return " | ".join(parts)

    def _transcript_context(self, transcripts: List[str]) -> str:
        """Build a compact research context from YouTube transcripts."""
        if not transcripts:
            return ""
        # Take snippets from each transcript to stay within context window
        snippets = []
        for t in transcripts:
            snippet = " ".join(t.split()[:150]) # First 150 words
            snippets.append(f"RESEARCH SNIPPET: {snippet}")
        return " | ".join(snippets)

    def _ollama_prompt(self, channel: ChannelConfig, topic: TopicCandidate,
                       content_kind: str = "short", avoid_titles: set[str] | None = None,
                       dna: dict | None = None) -> str:
        hashtags = " ".join(topic.hashtags[:8])
        trends = ", ".join(topic.trend_terms[:6])
        avoid_str = f" Avoid these recently used topics/angles: {', '.join(list(avoid_titles)[:10])}." if avoid_titles else ""
        style_instruction = self._style_instruction(topic.style)
        dna_context = self._dna_context(dna)
        dna_str = f" VIRAL STYLE GUIDE (from top-performing channels in this niche): {dna_context}." if dna_context else ""
        
        research_context = self._transcript_context(topic.transcripts)
        research_str = f" RESEARCH DATA (do NOT copy verbatim, rephrase in the viral style): {research_context}." if research_context else ""

        duration_hint = "8 to 10 minutes" if content_kind == "video" else "30 to 60 seconds"
        word_hint = "1,150 to 1,450 words in 12 to 18 clear sections" if content_kind == "video" else "around 95 to 135 words"
        format_hint = "You are writing a premium long-form YouTube documentary script." if content_kind == "video" else "You are writing a spoken YouTube Shorts script."
        long_form_rules = (
            " For long-form: structure the story as cold open, promised question, stakes, context, 5-8 escalating evidence points, consequences, and a satisfying answer."
            " Add a soft re-hook every 35-50 seconds with a specific unanswered question, reversal, consequence, or new clue."
            " Do not pad. Every section must add new information, visual potential, or emotional stakes."
            if content_kind == "video"
            else ""
        )
        supplied_facts = " ".join(
            scene.narration for scene in topic.scene_plan if scene.narration
        ) or topic.narration
        supplied_facts = self._clean(supplied_facts)[:6500]
        channel_rules = (
            " Brain Lens may feel magnetic, flirty, awkward, intimate, or controversial, but it must stay non-explicit, consensual, mature, and advertiser-safe. Treat body language and dating cues as context-dependent questions, never proof. Never diagnose a viewer or partner, invent gossip, teach manipulation, shame a body, or promise attraction. End with useful clarity, consent, reciprocity, confidence, or a boundary."
            if channel.id == "brain_lens"
            else " Ancient History must distinguish confirmed evidence from interpretation. Do not add a person, date, number, quote, motive, discovery, or controversy absent from the supplied facts. Drama must come from a documented constraint, conflict, consequence, object, or uncertainty rather than conspiracy language."
        )
        return (
            format_hint
            + " Keep it safe, non-vulgar, no explicit content, no hate, no harmful advice."
            " English only."
            f" Niche: {channel.niche_description}."
            f" Style: {topic.style}. {style_instruction}"
            f" Content Subject: {topic.title}."
            f" Context/Trends: {trends}."
            f" Target: {duration_hint} voiceover."
            f" Output one continuous narration paragraph {word_hint}."
            " Use short, natural spoken sentences. Open in 22 words or fewer with a visible moment, specific object, human consequence, or unresolved tension."
            " State or strongly imply the exact question the ending will answer. Make the final line echo and resolve the opening rather than changing subjects."
            " Make each factual moment visually specific so each sentence could match a separate video scene."
            f"{long_form_rules}"
            f"{channel_rules}"
            f" SUPPLIED FACTS (the factual ceiling; omit a claim if it is not supported here): {supplied_facts}."
            " Avoid ellipses, markdown, scene labels, fake quotations, unsupported superlatives, or clickbait without payoff."
            " **Strict Requirement**: Use unique storytelling, avoid cliches like 'The real story' or 'What they don't want you to know'."
            " Do NOT start with 'Have you ever' or 'Did you know'."
            f"{dna_str}"
            f"{research_str}"
            f"{avoid_str}"
            " Make it feel curiosity-forward and premium, with intellectual depth and a new reason to continue every 35-50 seconds."
            " Silently audit the draft for factual support, repeated ideas, hook/payoff closure, consent, and advertiser safety before returning it."
            " Do not add a generic subscribe CTA; end on the strongest resolved insight."
            f" Preserve relevance to these hashtags: {hashtags}."
        )

    def _long_video_expand_prompt(self, channel: ChannelConfig, topic: TopicCandidate, draft: str) -> str:
        subject = topic.subject or self._headline_subject(topic.title) or topic.title
        if channel.id == "brain_lens":
            return (
                "Expand this into a premium 8 to 10 minute Brain Lens relationship psychology voiceover. "
                "Return only the narration, no headings, no markdown, no labels. "
                "Target 1,350 to 1,600 words. Keep it continuous and spoken, with 12 to 18 natural sections. "
                "Tone: magnetic, flirty, emotionally intelligent, modern, respectful, and monetization-safe. No explicit sexual detail, fabricated gossip, diagnosis, manipulation, humiliation, or pickup-artist language. "
                "Structure: a visible dating micro-drama, one precise promised question, context-dependent psychology, a counterexample, pattern evidence, a practical reset, and a final payoff that answers the opening. "
                "Add a re-hook every 35-45 seconds using a relevant consequence, contradiction, question, or new observation; never inject an unrelated trendy topic. "
                "Treat attraction cues as ambiguous until reciprocity, consent, and behavior over time add context. "
                "Make every section useful and visually clear; avoid repeating the same idea. "
                f"Channel niche: {channel.niche_description}. Subject: {subject}. Title: {topic.title}. "
                f"Base draft to expand without copying awkward phrasing: {draft[:1800]}"
            )
        return (
            "Expand this into a premium 8 to 10 minute YouTube documentary voiceover. "
            "Return only the narration, no headings, no markdown, no labels. "
            "Target 1,450 to 1,750 words. Use 16 to 22 natural sections, but keep it as continuous narration. "
            "Structure: concrete surviving clue, promised historical question, stakes, chronology, escalating evidence, source limits, consequences, and a final answer that returns to the clue. "
            "Add a new visual moment or curiosity turn every 35-50 seconds. Do not repeat the same idea. "
            "Keep claims accurate and evidence-bounded. Do not invent a date, number, motive, quote, controversy, discovery, or consensus absent from the base draft. "
            f"Channel niche: {channel.niche_description}. Subject: {subject}. Title: {topic.title}. "
            f"Base draft to expand without copying awkward phrasing: {draft[:1800]}"
        )

    def _generate_ollama(
        self,
        prompt: str,
        max_output_tokens: int = 700,
        response_mime_type: str | None = None,
        purpose: str = "unspecified",
    ) -> str:
        model = str(self.cfg.ollama_model or "").strip().lower()
        call_purpose = str(purpose or "").strip().lower()
        # Full narration drafts need a stronger local model. Keep tiny 3B
        # models only for compact JSON repair / scoring when nothing else works.
        tiny_model = bool(re.search(r"(?:^|[:/\-_])3b(?:$|[:/\-_])", model)) or model.endswith(":3b")
        full_script_purposes = {
            "script_draft",
            "script_expansion",
            "scene_rewrite",
            "scene_script",
        }
        if tiny_model and call_purpose in full_script_purposes:
            raise RuntimeError(
                f"refusing full scripts on tiny Ollama model {self.cfg.ollama_model}; "
                "prefer qwen2.5:7b or a cloud provider"
            )
        payload = {
            "model": self.cfg.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.8,
                "top_p": 0.92,
                "num_predict": max(300, int(max_output_tokens)),
            },
        }
        if response_mime_type == "application/json":
            payload["format"] = "json"
        endpoint = validate_ollama_generate_url(self.cfg.ollama_url)
        r = requests.post(
            endpoint,
            json=payload,
            timeout=min(
                180,
                max(15, int(getattr(self.cfg, "ollama_timeout_seconds", 90) or 90)),
            ),
            allow_redirects=False,
        )
        r.raise_for_status()
        data = r.json()
        return str(data.get("response", "")).strip()

    def _named_openai_compatible_settings(self, provider: str) -> tuple[str, str, str]:
        """Resolve first-class Groq/Mistral profiles without clobbering HF gateway."""
        provider = (provider or "").strip().lower()
        if provider == "groq":
            return (
                str(getattr(self.cfg, "groq_url", "https://api.groq.com/openai/v1") or "").strip(),
                str(getattr(self.cfg, "groq_model", "llama-3.3-70b-versatile") or "").strip(),
                (
                    os.getenv(str(getattr(self.cfg, "groq_api_key_env", "GROQ_API_KEY") or "GROQ_API_KEY"))
                    or os.getenv("GROQ_API_KEY")
                    or ""
                ).strip(),
            )
        if provider == "mistral":
            return (
                str(getattr(self.cfg, "mistral_url", "https://api.mistral.ai/v1") or "").strip(),
                str(getattr(self.cfg, "mistral_model", "mistral-small-latest") or "").strip(),
                (
                    os.getenv(
                        str(getattr(self.cfg, "mistral_api_key_env", "MISTRAL_API_KEY") or "MISTRAL_API_KEY")
                    )
                    or os.getenv("MISTRAL_API_KEY")
                    or ""
                ).strip(),
            )
        raise RuntimeError(f"unsupported named openai-compatible provider: {provider}")

    def _generate_named_openai_compatible(
        self,
        provider: str,
        prompt: str,
        max_output_tokens: int = 700,
        response_mime_type: str | None = None,
    ) -> str:
        base_url, model, api_key = self._named_openai_compatible_settings(provider)
        if not base_url or not model:
            raise RuntimeError(f"{provider} url/model is not configured")
        if not api_key:
            raise RuntimeError(f"{provider} API key is missing")
        host = self._validate_openai_compatible_endpoint(base_url, model)
        endpoint = base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        messages = [{"role": "user", "content": prompt}]
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": max(256, int(max_output_tokens)),
        }
        if response_mime_type == "application/json":
            payload["response_format"] = {"type": "json_object"}
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=min(
                120,
                max(5, int(getattr(self.cfg, "openai_compatible_timeout_seconds", 90) or 90)),
            ),
            allow_redirects=False,
        )
        if response.status_code in {401, 403, 429, 500, 502, 503, 504}:
            detail = self._redact_keys(
                f"{response.status_code}: {response.text[:180]}",
                [api_key],
            )
            raise RuntimeError(detail)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"empty {provider} response")
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise RuntimeError(f"empty {provider} response")
        # Keep host in audit trail via last call details.
        _ = host
        return text

    def _openai_compatible_settings(self) -> tuple[str, str, str]:
        env_url = (os.getenv("AI_GATEWAY_URL") or "").strip()
        env_model = (os.getenv("AI_GATEWAY_MODEL") or "").strip()
        if env_url or env_model:
            # An environment override is a separate provider profile. Never pair
            # an overridden endpoint with the fixed profile's token (HF_API_KEY).
            return (
                env_url,
                env_model,
                (os.getenv("AI_GATEWAY_API_KEY") or "").strip(),
            )

        base_url = (getattr(self.cfg, "openai_compatible_url", "") or "").strip()
        model = (getattr(self.cfg, "openai_compatible_model", "") or "").strip()
        key_env = (
            getattr(self.cfg, "openai_compatible_api_key_env", "AI_GATEWAY_API_KEY")
            or "AI_GATEWAY_API_KEY"
        ).strip()
        api_key = (os.getenv(key_env) or "").strip()
        return base_url, model, api_key

    def _openai_compatible_models(self) -> list[str]:
        """Every model to try, in order.

        A free model answers 429 as often as it answers, so one of them is not
        a provider -- it is a coin flip. Tried back to back, four of seven were
        busy. The ladder makes that a non-event: the next one answers.
        """
        # An environment override is a separate provider profile: pairing its
        # endpoint with the configured profile's models would send this
        # account's model list to someone else's host. When either override is
        # present, only the override's own model is allowed -- and if it did
        # not name one, there is no model, which is the caller's error.
        env_url = (os.getenv("AI_GATEWAY_URL") or "").strip()
        env_model = (os.getenv("AI_GATEWAY_MODEL") or "").strip()
        if env_url or env_model:
            return [env_model] if env_model else []
        ladder = [
            str(name).strip()
            for name in (getattr(self.cfg, "openai_compatible_models", None) or [])
            if str(name).strip()
        ]
        first = (getattr(self.cfg, "openai_compatible_model", "") or "").strip()
        if first and first not in ladder:
            ladder.insert(0, first)
        return ladder

    @staticmethod
    def _safe_endpoint_host(url: str) -> str:
        """Return only a normalized hostname suitable for logs and metadata."""
        try:
            host = urlparse(str(url or "").strip()).hostname or ""
        except (TypeError, ValueError):
            return ""
        return host.lower().rstrip(".")[:253]

    @staticmethod
    def _safe_model_label(model: str) -> str:
        """Keep model provenance useful without persisting URL-style parameters."""
        label = re.split(r"[?#]", str(model or ""), maxsplit=1)[0].strip()
        if not label:
            return ""
        label = re.sub(r"[^A-Za-z0-9._:/+\-]", "_", label)
        return label[:160]

    def _openai_compatible_allowed_hosts(self) -> set[str]:
        configured = getattr(
            self.cfg,
            "openai_compatible_allowed_hosts",
            ["router.huggingface.co", "api.groq.com", "api.mistral.ai"],
        )
        if isinstance(configured, str):
            configured = [configured]
        allowed: set[str] = set()
        for item in configured or []:
            candidate = str(item or "").strip()
            if not candidate:
                continue
            parsed_candidate = candidate if "://" in candidate else f"//{candidate}"
            try:
                host = urlparse(parsed_candidate).hostname or ""
            except (TypeError, ValueError):
                continue
            if host:
                allowed.add(host.lower().rstrip("."))
        # Always permit first-class cloud hosts even if settings omit them.
        allowed.update({"api.groq.com", "api.mistral.ai", "router.huggingface.co"})
        return allowed

    def _validate_openai_compatible_endpoint(self, base_url: str, model: str) -> str:
        """Validate an authorized endpoint without echoing credentials or query data."""
        try:
            parsed = urlparse(base_url)
            # Accessing port also catches malformed/out-of-range values early.
            _ = parsed.port
        except (TypeError, ValueError):
            raise RuntimeError("AI_GATEWAY_URL is not a valid absolute URL") from None
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            raise RuntimeError("AI_GATEWAY_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(
                "AI_GATEWAY_URL must not contain credentials, query parameters, or fragments"
            )
        if parsed.scheme != "https":
            raise RuntimeError("AI_GATEWAY_URL endpoints require HTTPS")
        if host not in self._openai_compatible_allowed_hosts():
            raise RuntimeError("AI_GATEWAY_URL host is not allowed by policy")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}", model):
            raise RuntimeError("AI_GATEWAY_MODEL must be a plain model identifier")
        return host

    def _provider_safe_details(self, provider: str) -> dict[str, str]:
        if provider == "gemini":
            url = self.cfg.gemini_url
            model = self.cfg.gemini_model
        elif provider == "openai_compatible":
            url, model, _ = self._openai_compatible_settings()
        elif provider == "groq":
            url, model, _ = self._named_openai_compatible_settings("groq")
        elif provider == "mistral":
            url, model, _ = self._named_openai_compatible_settings("mistral")
        elif provider == "ollama":
            url = self.cfg.ollama_url
            model = self.cfg.ollama_model
        else:
            return {"endpoint_host": "", "model": ""}
        return {
            "endpoint_host": self._safe_endpoint_host(url),
            "model": self._safe_model_label(model),
        }

    def provider_output_metadata(
        self,
        topic: TopicCandidate | None = None,
    ) -> dict[str, str]:
        """Return truthful sanitized AI-call and selected-script provenance."""
        if topic is None:
            script_provider = self.script_provider
            script_host = self.script_provider_endpoint_host
            script_model = self.script_provider_model
        else:
            script_provider = str(getattr(topic, "script_provider", "") or "")
            script_host = str(
                getattr(topic, "script_provider_endpoint_host", "") or ""
            )
            script_model = str(getattr(topic, "script_provider_model", "") or "")
        return {
            "last_ai_provider": self.last_ai_provider,
            "last_ai_provider_endpoint_host": self.last_ai_provider_endpoint_host,
            "last_ai_provider_model": self.last_ai_provider_model,
            # Backward-compatible keys now mean exactly what they say: these
            # stay empty unless generated narration was accepted for this topic.
            "script_provider": script_provider,
            "script_provider_endpoint_host": script_host,
            "script_provider_model": script_model,
        }

    def _generate_openai_compatible(
        self,
        prompt: str,
        max_output_tokens: int = 700,
        response_mime_type: str | None = None,
    ) -> str:
        base_url, model, api_key = self._openai_compatible_settings()
        ladder = self._openai_compatible_models() or ([model] if model else [])
        if not base_url or not ladder:
            raise RuntimeError("AI_GATEWAY_URL and AI_GATEWAY_MODEL are not configured")
        if len(ladder) > 1:
            errors: list[str] = []
            for candidate in ladder:
                try:
                    return self._generate_openai_compatible_once(
                        prompt,
                        candidate,
                        base_url,
                        api_key,
                        max_output_tokens=max_output_tokens,
                        response_mime_type=response_mime_type,
                    )
                except Exception as exc:
                    errors.append(f"{self._safe_model_label(candidate)}: {exc}")
                    continue
            raise RuntimeError("every gateway model failed: " + "; ".join(errors[-4:]))
        model = ladder[0]
        return self._generate_openai_compatible_once(
            prompt,
            model,
            base_url,
            api_key,
            max_output_tokens=max_output_tokens,
            response_mime_type=response_mime_type,
        )

    def _generate_openai_compatible_once(
        self,
        prompt: str,
        model: str,
        base_url: str,
        api_key: str,
        max_output_tokens: int = 700,
        response_mime_type: str | None = None,
    ) -> str:
        self._validate_openai_compatible_endpoint(base_url, model)

        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            if not endpoint.endswith("/v1"):
                endpoint += "/v1"
            endpoint += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        disable_thinking = bool(
            getattr(self.cfg, "openai_compatible_disable_thinking", False)
        )
        provider_prompt = f"/no_think\n{prompt}" if disable_thinking else prompt
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": provider_prompt}],
            "temperature": 0.72,
            "top_p": 0.92,
            "max_tokens": max(96, int(max_output_tokens)),
        }
        if disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if response_mime_type == "application/json":
            payload["response_format"] = {"type": "json_object"}
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=min(
                120,
                max(
                    5,
                    int(getattr(self.cfg, "openai_compatible_timeout_seconds", 90) or 90),
                ),
            ),
            allow_redirects=False,
        )
        if response.status_code in {401, 403, 408, 409, 429, 500, 502, 503, 504}:
            safe_message = self._redact_keys(response.text[:240], [api_key])
            raise RuntimeError(f"gateway HTTP {response.status_code}: {safe_message}")
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                )
            if str(content or "").strip():
                return str(content).strip()
        output_text = data.get("output_text")
        if str(output_text or "").strip():
            return str(output_text).strip()
        raise RuntimeError("empty OpenAI-compatible response")

    @staticmethod
    def _gemini_key() -> str:
        """Return one explicitly authorized key; never cycle quota keys."""
        return (os.getenv("GEMINI_API_KEY") or "").strip()

    @staticmethod
    def _validate_gemini_endpoint(base_url: str, model: str) -> str:
        try:
            parsed = urlparse(base_url)
            _ = parsed.port
        except (TypeError, ValueError):
            raise RuntimeError("Gemini URL is not a valid absolute URL") from None
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host != "generativelanguage.googleapis.com":
            raise RuntimeError("Gemini URL must use the official HTTPS API host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(
                "Gemini URL must not contain credentials, query parameters, or fragments"
            )
        path = (parsed.path or "").rstrip("/")
        if path not in {"/v1/models", "/v1beta/models"}:
            raise RuntimeError("Gemini URL must use an official models API path")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,119}", model):
            raise RuntimeError("Gemini model must be a plain model identifier")
        return host

    def _extract_gemini_text(self, data: dict) -> str:
        parts: list[str] = []
        for candidate in data.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                text = part.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()

    def _redact_keys(self, message: str, keys: list[str]) -> str:
        redacted = str(message or "")
        for key in keys:
            if key:
                redacted = redacted.replace(key, "[redacted]")
        return redacted

    def _generate_gemini(
        self,
        prompt: str,
        max_output_tokens: int = 700,
        response_mime_type: str | None = None,
    ) -> str:
        key = self._gemini_key()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        base_url = (self.cfg.gemini_url or "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
        model = (self.cfg.gemini_model or "gemini-2.5-flash-lite").strip()
        self._validate_gemini_endpoint(base_url, model)
        endpoint = f"{base_url}/{model}:generateContent"
        generation_config = {
            "temperature": 0.72,
            "topP": 0.92,
            "maxOutputTokens": max_output_tokens,
        }
        if response_mime_type:
            generation_config["responseMimeType"] = response_mime_type
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": generation_config,
        }

        try:
            response = requests.post(
                endpoint,
                headers={"x-goog-api-key": key},
                json=payload,
                timeout=min(120, max(15, int(self.cfg.timeout_seconds or 120))),
                allow_redirects=False,
            )
            if response.status_code in {401, 403, 429, 500, 502, 503, 504}:
                detail = self._redact_keys(
                    f"{response.status_code}: {response.text[:180]}",
                    [key],
                )
                raise RuntimeError(detail)
            response.raise_for_status()
            text = self._extract_gemini_text(response.json())
            if text:
                return text
            raise RuntimeError("empty Gemini response")
        except Exception as exc:
            raise RuntimeError(self._redact_keys(str(exc), [key])) from None

    def _generate_ai(
        self,
        prompt: str,
        max_output_tokens: int = 700,
        response_mime_type: str | None = None,
        purpose: str = "unspecified",
        validator: Callable[[str], bool] | None = None,
    ) -> str:
        provider = (self.cfg.provider or "template").lower().strip()
        configured_order = [
            str(item).strip().lower()
            for item in getattr(self.cfg, "provider_order", [])
            if str(item).strip()
        ]
        if provider in {"routed", "auto"}:
            order = configured_order or ["gemini", "groq", "openai_compatible", "ollama"]
        elif provider == "local_first":
            order = configured_order or ["ollama", "gemini", "groq", "openai_compatible"]
        elif provider == "gemini_ollama":
            order = ["gemini", "ollama"]
        elif provider in {"gemini", "ollama", "openai_compatible", "groq", "mistral"}:
            order = [provider]
        else:
            raise RuntimeError(f"Unsupported script writer provider: {self.cfg.provider}")

        call_purpose = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(purpose or "").strip().lower(),
        ).strip("_")[:64] or "unspecified"

        generators = {
            "gemini": lambda: self._generate_gemini(
                prompt,
                max_output_tokens=max_output_tokens,
                response_mime_type=response_mime_type,
            ),
            "openai_compatible": lambda: self._generate_openai_compatible(
                prompt,
                max_output_tokens=max_output_tokens,
                response_mime_type=response_mime_type,
            ),
            "groq": lambda: self._generate_named_openai_compatible(
                "groq",
                prompt,
                max_output_tokens=max_output_tokens,
                response_mime_type=response_mime_type,
            ),
            "mistral": lambda: self._generate_named_openai_compatible(
                "mistral",
                prompt,
                max_output_tokens=max_output_tokens,
                response_mime_type=response_mime_type,
            ),
            "ollama": lambda: self._generate_ollama(
                prompt,
                max_output_tokens=max_output_tokens,
                response_mime_type=response_mime_type,
                purpose=call_purpose,
            ),
        }
        errors: list[str] = []
        attempted: list[dict] = []

        def audit_fields() -> dict[str, object]:
            return {
                "call_purpose": call_purpose,
                "accepted_content": False,
                "accepted_as": "",
            }

        now = time.monotonic()
        cooldown = max(0, int(getattr(self.cfg, "provider_cooldown_seconds", 180) or 0))
        for candidate in order:
            generate = generators.get(candidate)
            if generate is None:
                errors.append(f"unknown provider {candidate}")
                attempted.append({
                    "provider": candidate,
                    "status": "failed",
                    "error": "unknown provider",
                    "endpoint_host": "",
                    "model": "",
                    **audit_fields(),
                })
                continue
            safe_details = self._provider_safe_details(candidate)
            blocked_until = float(self._provider_backoff_until.get(candidate, 0.0) or 0.0)
            if blocked_until > now:
                attempted.append({
                    "provider": candidate,
                    "status": "cooldown",
                    **safe_details,
                    **audit_fields(),
                })
                continue
            try:
                generated = str(generate() or "").strip()
                if not generated:
                    raise RuntimeError("empty response")
                if validator is not None:
                    try:
                        semantically_valid = bool(validator(generated))
                    except Exception:
                        semantically_valid = False
                    if not semantically_valid:
                        if cooldown:
                            self._provider_backoff_until[candidate] = (
                                time.monotonic() + cooldown
                            )
                        message = "response failed semantic validation"
                        attempted.append({
                            "provider": candidate,
                            "status": "rejected",
                            "error": message,
                            **safe_details,
                            **audit_fields(),
                        })
                        errors.append(f"{candidate}: {message}")
                        continue
                self._provider_backoff_until.pop(candidate, None)
                self.last_provider = candidate
                self.last_provider_endpoint_host = safe_details["endpoint_host"]
                self.last_provider_model = safe_details["model"]
                attempted.append({
                    "provider": candidate,
                    "status": "ok",
                    **safe_details,
                    **audit_fields(),
                })
                self._record_provider_attempts(attempted)
                return generated
            except Exception as exc:
                if cooldown:
                    self._provider_backoff_until[candidate] = time.monotonic() + cooldown
                _, _, gateway_key = self._openai_compatible_settings()
                secret_keys = []
                if self._gemini_key():
                    secret_keys.append(self._gemini_key())
                if gateway_key:
                    secret_keys.append(gateway_key)
                for named in ("groq", "mistral"):
                    try:
                        _, _, named_key = self._named_openai_compatible_settings(named)
                    except Exception:
                        named_key = ""
                    if named_key:
                        secret_keys.append(named_key)
                message = self._redact_keys(str(exc), secret_keys)
                attempted.append({
                    "provider": candidate,
                    "status": "failed",
                    "error": message[:240],
                    **safe_details,
                    **audit_fields(),
                })
                errors.append(f"{candidate}: {message}")
        self._record_provider_attempts(attempted)
        raise RuntimeError("All configured AI providers failed: " + "; ".join(errors[-4:]))

    def _json_from_ai(self, raw: str) -> dict:
        """The answer in a reply, ignoring whatever the model said around it.

        Free models think out loud and often restate the example from the
        question before answering. Taking the first JSON-looking span read the
        question back as the answer, so the last complete object wins and
        anything that only echoes the prompt's placeholders is skipped.
        """
        raw = (raw or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        # Strip code fences so a fenced answer parses like a bare one.
        fenced = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()

        candidates: list[dict] = []
        starts = [i for i, ch in enumerate(fenced) if ch == "{"]
        for start in starts:
            depth = 0
            for index in range(start, len(fenced)):
                if fenced[index] == "{":
                    depth += 1
                elif fenced[index] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            found = json.loads(fenced[start:index + 1])
                        except Exception:
                            break
                        if isinstance(found, dict) and found:
                            candidates.append(found)
                        break

        for found in reversed(candidates):
            if not self._looks_like_prompt_echo(found):
                return found
        return candidates[-1] if candidates else {}

    # Placeholder wording a model copies from the question instead of answering.
    _ECHO_MARKERS = ("...", "…", "your text", "example", "placeholder", "first beat", "second beat")

    @classmethod
    def _looks_like_prompt_echo(cls, payload: dict) -> bool:
        blob = " ".join(
            str(item)
            for value in payload.values()
            for item in (value if isinstance(value, list) else [value])
        ).strip().lower()
        if not blob:
            return True
        return any(marker in blob for marker in cls._ECHO_MARKERS)

    @staticmethod
    def _valid_percent_score(value: object) -> bool:
        if isinstance(value, bool):
            return False
        try:
            score = float(value)
            return math.isfinite(score) and 0 <= score <= 100
        except (TypeError, ValueError):
            return False

    def _quality_review_payload_is_valid(self, raw: str) -> bool:
        data = self._json_from_ai(raw)
        return (
            isinstance(data, dict)
            and self._valid_percent_score(data.get("score"))
            and str(data.get("verdict") or "").strip().lower() in {"pass", "hold"}
            and isinstance(data.get("issues"), list)
            and isinstance(data.get("strengths"), list)
        )

    def _topic_score_payload_is_valid(self, raw: str) -> bool:
        data = self._json_from_ai(raw)
        return (
            isinstance(data, dict)
            and self._valid_percent_score(data.get("score"))
            and isinstance(data.get("reasons"), list)
            and isinstance(data.get("risks"), list)
        )

    def _scene_rewrite_prompt(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        content_kind: str = "short",
        avoid_titles: set[str] | None = None,
        dna: dict | None = None,
    ) -> str:
        scene_lines = "\n".join(
            f"{idx + 1}. {scene.narration}"
            for idx, scene in enumerate(topic.scene_plan)
            if scene.narration
        )
        avoid_str = ", ".join(list(avoid_titles or [])[:14])
        dna_context = self._dna_context(dna)
        creative_frame = self._creative_frame(channel, topic)
        target_words = (
            f"{self._BRAIN_SHORT_MIN_WORDS}-{self._BRAIN_SHORT_MAX_WORDS}"
            if content_kind == "short" and channel.id == "brain_lens"
            else (
                f"{self._ANCIENT_SHORT_MIN_WORDS}-{self._ANCIENT_SHORT_MAX_WORDS}"
                if content_kind == "short"
                else "150-240"
            )
        )
        beat_limit = (
            "20 words for beat 1, 24 words for every other beat"
            if content_kind == "short"
            else "24 words per beat"
        )
        channel_rule = (
            "Brain Lens: make the viewer feel seen with mature, flirty, emotionally intelligent relationship psychology. Use attraction, chemistry, texting, body language, mixed signals, crushes, kissing tension, attachment, confidence, and boundaries when relevant. Keep it spicy but non-explicit, non-manipulative, respectful, and not medical. Start with what the viewer does or feels, not the concept name."
            if channel.id == "brain_lens"
            else "Ancient History: anchor every line in concrete evidence, artifacts, rulers, dates, consequences, or places."
        )
        structure_rule = (
            "For Ancient History, structure the beats as: striking surviving clue, precise context, physical evidence, "
            "human or political consequence, final reveal, and a factual payoff that changes how the opening is understood."
            if channel.id == "ancient_history"
            else "For a short, structure the beats as: micro-drama, immediate emotional consequence, psychology reveal, "
            "pattern proof, reframe, practical power move, memorable loop payoff."
        )
        return (
            "You are a senior YouTube Shorts creative director. Rewrite the script beats for retention, not hype. "
            "Use only the facts already present; do not invent new facts. "
            f"Channel: {channel.display_name}. Niche: {channel.niche_description}. {channel_rule} "
            f"Current title: {topic.title}. Subject: {topic.subject or topic.title}. "
            f"Target total narration words: {target_words}. Beat limit: {beat_limit}. "
            f"Creative frame for this script: {creative_frame}. "
            f"{structure_rule} "
            "Beat 1 must be a direct scroll-stopper with a concrete image, consequence, or personal tension. "
            "For Brain Lens, beat 1 must start with 'You' or a visible dating/relationship behavior before the explanation: rereading a text, checking their story, almost replying, holding eye contact, pretending not to care, noticing a voice change, or feeling chemistry after mixed signals. "
            "For Brain Lens, do not name the psychology term in beat 1 unless the behavior is already clear. "
            "For Brain Lens, avoid abstract brain diagrams in the writing: describe a face, body cue, phone, room, relationship moment, almost-flirt, silence, or tiny behavior. "
            "For Brain Lens, the payoff should make the viewer feel more attractive through calm, boundaries, confidence, or understanding, never through manipulation or humiliation. "
            "Do not reuse the same sentence rhythm across beats; mix short punches with one vivid longer line. "
            "Display text should be clean caption phrases, never broken fragments or vague labels. "
            "Every 3-4 seconds add a new reveal, contrast, consequence, or question. Keep each beat necessary; remove repeated explanations. "
            "Avoid generic openings like 'to understand', 'there is a fascinating reason', 'modern psychology', "
            "'historians keep coming back', 'the archive is full', 'have you ever', and 'did you know'. "
            "For Ancient History, do not add a person, place, date, number, object, or claim that is absent from the existing beats. "
            "Do not write a promotional CTA; end on the strongest factual consequence. "
            "For Brain Lens titles, avoid repeated formulas like 'brain trick', 'quietly steals your attention', 'first clue', and '3 signs'. Prefer a concrete viewer problem with a real payoff. "
            "End with a memorable payoff that echoes the opening. A CTA is optional and may use no more than four words. Never end with generic 'follow for more' language. "
            f"Recent topics/angles to avoid: {avoid_str}. "
            f"Viral style guide: {dna_context or 'clear, visual, fast-paced, original'}.\n"
            f"Existing beats:\n{scene_lines}\n"
            "Return ONLY valid JSON in this exact shape: "
            "{\"title\":\"optional improved title under 90 chars\",\"beats\":[\"beat 1\", \"beat 2\"]}. "
            "The beats array must have the same number of items as the existing beats."
        )

    def _rewrite_scene_plan_with_ai(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        content_kind: str = "short",
        avoid_titles: set[str] | None = None,
        dna: dict | None = None,
    ) -> TopicCandidate:
        if not topic.scene_plan:
            return topic
        prompt = self._scene_rewrite_prompt(
            channel=channel,
            topic=topic,
            content_kind=content_kind,
            avoid_titles=avoid_titles,
            dna=dna,
        )
        expected_beat_count = len(topic.scene_plan)

        def valid_scene_payload(raw_text: str) -> bool:
            payload = self._json_from_ai(raw_text)
            raw_beats = payload.get("beats")
            return (
                isinstance(raw_beats, list)
                and len(raw_beats) == expected_beat_count
                and all(isinstance(beat, str) and beat.strip() for beat in raw_beats)
            )

        raw = self._generate_ai(
            prompt,
            max_output_tokens=400,
            response_mime_type="application/json",
            purpose="scene_rewrite",
            validator=valid_scene_payload,
        )
        data = self._json_from_ai(raw)
        beats_raw = data.get("beats") or []
        if not isinstance(beats_raw, list):
            return topic
        beats = [self._sentence(str(beat)) for beat in beats_raw if self._sentence(str(beat))]
        if len(beats) != len(topic.scene_plan):
            return topic
        total_words = self._word_count(" ".join(beats))
        if content_kind == "short":
            opener_limit = 20
            if channel.id == "brain_lens":
                word_ok = self._BRAIN_SHORT_MIN_WORDS <= total_words <= self._BRAIN_SHORT_MAX_WORDS
            elif channel.id == "ancient_history":
                word_ok = self._ANCIENT_SHORT_MIN_WORDS <= total_words <= self._ANCIENT_SHORT_MAX_WORDS
            else:
                word_ok = 120 <= total_words <= 148
            if self._word_count(beats[0]) > opener_limit or not word_ok:
                return topic
        elif not (100 <= total_words <= 320):
            return topic
        if self._is_generic_opener(beats[0]):
            return topic
        if self.editorial_quality_issues(topic, beats=beats, content_kind=content_kind):
            return topic
        if channel.id == "ancient_history":
            original_text = " ".join(scene.narration for scene in topic.scene_plan)
            original_numbers = set(re.findall(r"\b\d[\d,.]*(?:st|nd|rd|th)?\b", original_text.lower()))
            rewritten_numbers = set(re.findall(r"\b\d[\d,.]*(?:st|nd|rd|th)?\b", " ".join(beats).lower()))
            if not rewritten_numbers.issubset(original_numbers):
                return topic

            common_capitals = {
                "A", "An", "And", "At", "Before", "But", "For", "From", "How", "In", "Inside",
                "It", "Its", "Later", "The", "These", "This", "Today", "What", "When", "Where", "Why",
            }
            original_names = {
                token
                for token in re.findall(r"\b[A-Z][A-Za-z'-]{2,}\b", original_text)
                if token not in common_capitals
            }
            rewritten_names = {
                token
                for token in re.findall(r"\b[A-Z][A-Za-z'-]{2,}\b", " ".join(beats))
                if token not in common_capitals
            }
            if not rewritten_names.issubset(original_names):
                return topic

        scene_plan: list[ScenePlanItem] = []
        for beat, scene in zip(beats, topic.scene_plan):
            scene_plan.append(
                ScenePlanItem(
                    narration=beat,
                    visual_text=self._captionize(scene.visual_text or beat),
                    search_terms=list(scene.search_terms),
                    preferred_image_url=scene.preferred_image_url,
                    visual_prompt=scene.visual_prompt,
                )
            )

        title = str(data.get("title") or topic.title).strip(" \"'")
        if (
            not (24 <= len(title) <= 95)
            or self._title_formula_issue(channel.id, title)
        ):
            title = topic.title
        visual_captions = [scene.visual_text for scene in scene_plan[1:-1] if scene.visual_text]
        provenance = self._accept_last_ai_content("scene_script")
        return replace(
            topic,
            title=title,
            hook=beats[0],
            narration=" ".join(beats),
            narration_beats=beats,
            visual_captions=visual_captions,
            scene_plan=scene_plan,
            content_kind=content_kind,
            script_provider=provenance["provider"],
            script_provider_endpoint_host=provenance["endpoint_host"],
            script_provider_model=provenance["model"],
        )

    def _ollama_title_prompt(self, channel: ChannelConfig, topic: TopicCandidate) -> str:
        subject = self._headline_subject(topic.subject or topic.title)
        channel_rule = (
            "For Brain Lens, make it personal and psychology-driven without sounding medical or alarmist. "
            "Use behavior-first titles with a concrete payoff: reaction, choice, relationship cue, body signal, attention loop, or tiny reset. Avoid stale concept-label formulas."
            if channel.id == "brain_lens"
            else "For ancient history, anchor the title in evidence, consequences, empires, artifacts, rulers, or a precise historical mystery."
        )
        format_label = (
            "long-form YouTube video title"
            if topic.content_kind == "video"
            else "YouTube Shorts title"
        )
        return (
            "You are a YouTube title and SEO expert. "
            f"Context: {channel.niche_description}. "
            f"Subject: {subject}. "
            f"Generate one single accurate, high-retention {format_label}. "
            f"{channel_rule} "
            "Rule #1: Do NOT use generic templates like 'The real story of', 'The true story of', 'The psychology behind', 'Why your brain does this', 'Why you keep doing this', 'How X quietly steals your attention', 'What X does before you notice it', 'The first clue X is taking over', '3 signs X is running your reaction', or 'X: The Brain Trick That Skews Your Choices'. "
            "Rule #2: Avoid 'Stop doing this' or 'Watch this before'. "
            "Rule #3: For Brain Lens, lead with the viewer's behavior or pain first, then imply the searchable subject naturally. For history, put the clearest searchable subject near the beginning. "
            "Rule #4: Prefer 45 to 82 characters. Maximum 90 characters. "
            "Rule #5: No all-caps, no hashtags, no emojis, no quotes. "
            "Rule #6: Make the viewer feel there is a specific insight, not generic clickbait or a misleading promise. "
            "Rule #7: For Brain Lens, prefer concrete patterns like 'Why One Text Can Ruin Your Mood', 'The Tiny Loop Behind Overthinking', 'You Are Not Lazy, Your Brain Is Avoiding This', or 'Why Easy Choices Suddenly Feel Heavy'. "
            "Rule #8: Never claim one cue proves secret intent, that a brain already knows the outcome, or hide the subject behind 'this one text move'. "
            "Rule #9: Output ONLY the title text, nothing else."
        )

    def generate_viral_title(self, channel: ChannelConfig, topic: TopicCandidate) -> str:
        if not self._ai_enabled():
            return topic.title
        try:
            prompt = self._ollama_title_prompt(channel, topic)
            generated = self._generate_ai(
                prompt,
                max_output_tokens=90,
                purpose="title_generation",
            )
            forbidden_starts = (
                "the real story",
                "the true story",
                "the psychology behind",
                "why your brain does this",
                "why your mind gets stuck here",
                "why people act this way",
                "this mental bias changes your decisions",
                "the lesson inside",
                "the hidden brain loop behind",
                "the behavior pattern behind",
                "what really happened in",
                "why you keep doing this",
                "did you know",
                "have you ever",
                "here's",
                "here is",
                "sure",
            )

            candidate_lines = [line for line in str(generated or "").splitlines() if line.strip()]
            if generated and "\n" not in generated:
                candidate_lines = [str(generated)]
            for raw_line in candidate_lines[:6]:
                candidate = raw_line.strip(" \"'`*-•0123456789.").strip()
                candidate = re.sub(r"^\s*(title|option)\s*[:\-]\s*", "", candidate, flags=re.IGNORECASE).strip()
                candidate = re.sub(r"\s+", " ", candidate)
                lowered = candidate.lower()
                has_ai_note = bool(re.search(r"\(?\s*\d+\s*characters?\s*\)?", lowered))
                repeated_formula = bool(
                    re.match(r"^what .+ does before you notice it$", lowered)
                    or re.match(r"^why .+ feels personal even when it is not$", lowered)
                    or lowered.startswith("why you keep doing this")
                    or "quietly steals your attention" in lowered
                    or "brain trick shaping your next reaction" in lowered
                    or "brain trick that skews your choices" in lowered
                    or "running your reaction" in lowered
                    or re.match(r"^the first clue .+ is (taking over|bending your judgment)$", lowered)
                    or (lowered.startswith("how ") and ": how " in lowered)
                )
                ai_instruction_leak = (
                    "follows all the given rules" in lowered
                    or "youtube title for shorts" in lowered
                    or lowered.startswith(forbidden_starts)
                )
                if (
                    24 <= len(candidate) <= 95
                    and not has_ai_note
                    and not repeated_formula
                    and not self._title_formula_issue(channel.id, candidate)
                    and not ai_instruction_leak
                    and not candidate.isupper()
                ):
                    self._accept_last_ai_content("title")
                    return candidate
        except Exception:
            pass
        return topic.title

    @staticmethod
    def _quality_review_narration(text: str, content_kind: str) -> str:
        """Give the reviewer representative long-form context without an unbounded prompt."""
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        if content_kind != "video" or len(cleaned) <= 3600:
            return cleaned

        chunk_size = 1200

        def trim_window(start: int, end: int) -> str:
            window = cleaned[max(0, start):min(len(cleaned), end)]
            if start > 0 and " " in window:
                window = window.split(" ", 1)[1]
            if end < len(cleaned) and " " in window:
                window = window.rsplit(" ", 1)[0]
            return window.strip()

        midpoint = len(cleaned) // 2
        beginning = trim_window(0, chunk_size)
        middle = trim_window(midpoint - chunk_size // 2, midpoint + chunk_size // 2)
        ending = trim_window(len(cleaned) - chunk_size, len(cleaned))
        return (
            "[BEGINNING]\n"
            f"{beginning}\n"
            "[MIDDLE]\n"
            f"{middle}\n"
            "[ENDING]\n"
            f"{ending}"
        )

    def review_content_quality(self, channel: ChannelConfig, topic: TopicCandidate, metadata: dict) -> dict:
        if not self._ai_enabled():
            return {}
        format_label = "long-form YouTube video" if topic.content_kind == "video" else "YouTube Short"
        prompt = (
            f"You are a strict {format_label} quality reviewer. Return ONLY valid JSON with keys: "
            "score (0-100), verdict (pass or hold), issues (array), strengths (array). "
            "Review ONLY the supplied title, hook, and narration text. Do not judge visuals, footage, audio, voice, captions, or subtitles because you cannot inspect them. "
            "Judge if the writing feels original, effortful, clear, safe, and likely to hold attention. "
            "Penalize generic AI-sounding content, weak hooks, vague claims, clickbait without payoff, repeated templates, "
            "medical overclaims, and titles that feel spammy. "
            "Recent viewer complaints to prevent: words sounding spelled-out, awkward split captions, generic 'pathetic AI' phrasing, wrong-location stock visuals, and history claims without place/culture context. "
            "For Brain Lens, also penalize abstract scripts with no visible human behavior or body cue, and hold any script whose first beat does not show a viewer action before explaining the concept. "
            "For Ancient History, also penalize missing location anchors like Peru for Nazca Lines or Tunisia/Punic context for Carthage. "
            f"Channel niche: {channel.niche_description}. "
            f"Title: {metadata.get('title') or topic.title}. "
            f"Duration seconds: {metadata.get('duration_seconds')}. "
            f"Hook: {topic.hook}. "
            f"Narration: {self._quality_review_narration(topic.narration, topic.content_kind)}"
        )
        try:
            raw = self._generate_ai(
                prompt,
                max_output_tokens=350,
                response_mime_type="application/json",
                purpose="quality_review",
                validator=self._quality_review_payload_is_valid,
            )
            data = self._json_from_ai(raw)
            score = int(float(data.get("score", 0)))
            verdict = str(data.get("verdict") or "").lower().strip()
            review = {
                "score": max(0, min(100, score)),
                "verdict": "pass" if verdict == "pass" else "hold",
                "issues": [str(item) for item in data.get("issues", []) if str(item).strip()][:6],
                "strengths": [str(item) for item in data.get("strengths", []) if str(item).strip()][:6],
            }
            self._accept_last_ai_content("quality_review")
            return review
        except Exception:
            return {}

    def score_topic_candidate(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        avoid_titles: set[str] | None = None,
    ) -> dict:
        if not self._ai_enabled():
            return {}
        avoid_str = ", ".join(list(avoid_titles or [])[:12])
        format_label = "long-form YouTube video" if topic.content_kind == "video" else "YouTube Short"
        prompt = (
            f"You are ranking {format_label} ideas before production. Return ONLY valid JSON with keys: "
            "score (0-100), reasons (array), risks (array). "
            "Score for likely viewer retention and growth, not generic quality. "
            "Reward: a concrete first hook, clear viewer payoff, searchable subject, emotional curiosity, "
            "visual specificity, novelty versus recent topics, and strong channel fit. "
            "Penalize: vague AI phrasing, repeated angles, weak first 2 seconds, medical overclaims, "
            "boring titles, topics with poor visual potential, wrong-location visual risk, awkward pronunciation risk, and history ideas missing place/culture anchors. "
            f"Channel niche: {channel.niche_description}. "
            f"Title: {topic.title}. Subject: {topic.subject}. "
            f"Hook: {topic.hook or ((topic.narration_beats or [''])[0])}. "
            f"Trend terms: {', '.join(topic.trend_terms[:8])}. "
            f"Visual captions: {', '.join(topic.visual_captions[:5])}. "
            f"Recent topics to avoid: {avoid_str}. "
            f"Narration preview: {topic.narration[:900]}"
        )
        try:
            raw = self._generate_ai(
                prompt,
                max_output_tokens=260,
                response_mime_type="application/json",
                purpose="topic_scoring",
                validator=self._topic_score_payload_is_valid,
            )
            data = self._json_from_ai(raw)
            score = int(float(data.get("score", 0)))
            result = {
                "score": max(0, min(100, score)),
                "reasons": [str(item) for item in data.get("reasons", []) if str(item).strip()][:5],
                "risks": [str(item) for item in data.get("risks", []) if str(item).strip()][:5],
            }
            self._accept_last_ai_content("topic_score")
            return result
        except Exception:
            return {}

    def repair_brain_short_with_ai(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        *,
        avoid_titles: set[str] | None = None,
        dna: dict | None = None,
    ) -> TopicCandidate | None:
        """One provider-routed rewrite for a rejected Brain Lens Short.

        Used only after the deterministic polish path fails editorial/caption
        gates. Prefer Gemini → Groq → HF → Ollama over shipping templates.
        """
        if channel.id != "brain_lens" or not self._ai_enabled():
            return None
        return self._repair_short_with_ai(
            channel, topic, avoid_titles=avoid_titles, dna=dna
        )

    def repair_ancient_short_with_ai(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        *,
        avoid_titles: set[str] | None = None,
        dna: dict | None = None,
    ) -> TopicCandidate | None:
        """One provider-routed rewrite for a rejected Ancient History Short."""
        if channel.id != "ancient_history" or not self._ai_enabled():
            return None
        return self._repair_short_with_ai(
            channel, topic, avoid_titles=avoid_titles, dna=dna
        )

    def _repair_short_with_ai(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        *,
        avoid_titles: set[str] | None = None,
        dna: dict | None = None,
    ) -> TopicCandidate | None:
        base = topic
        try:
            base = self._polish_scene_plan(channel, topic, content_kind="short")
        except Exception:
            base = topic
        if not base.scene_plan:
            return None
        try:
            rewritten = self._rewrite_scene_plan_with_ai(
                channel=channel,
                topic=base,
                content_kind="short",
                avoid_titles=avoid_titles,
                dna=dna,
            )
            polished = self._polish_scene_plan(channel, rewritten, content_kind="short")
            issues = self.editorial_quality_issues(
                polished,
                beats=polished.narration_beats,
                content_kind="short",
            )
            if issues:
                return None
            return polished
        except Exception:
            return None

    def improve(self, channel: ChannelConfig, topic: TopicCandidate,
                content_kind: str = "short", avoid_titles: set[str] | None = None,
                dna: dict | None = None) -> TopicCandidate:
        provider = (self.cfg.provider or "template").lower().strip()
        improved = topic
        if content_kind == "video" and str(os.getenv("YT_LONG_SCRIPT_AI", "0")).lower() not in {
            "1", "true", "yes", "on"
        }:
            return self._polish_scene_plan(channel, topic, content_kind=content_kind)
        # Deterministic behavior-first planning is fast and has hard editorial
        # guarantees. Free models remain valuable as scorers/reviewers after a
        # candidate passes those gates, but must not stall every rejected idea.
        if content_kind == "short" and channel.id in {"brain_lens", "ancient_history"}:
            return self._polish_scene_plan(channel, topic, content_kind=content_kind)
        if topic.scene_plan and self._ai_enabled():
            if content_kind == "video":
                pass
            else:
                try:
                    improved = self._rewrite_scene_plan_with_ai(
                        channel=channel,
                        topic=topic,
                        content_kind=content_kind,
                        avoid_titles=avoid_titles,
                        dna=dna,
                    )
                except Exception:
                    improved = topic
                return self._polish_scene_plan(channel, improved, content_kind=content_kind)

        if self._ai_enabled():
            try:
                prompt = self._ollama_prompt(channel, topic, content_kind=content_kind,
                                             avoid_titles=avoid_titles, dna=dna)
                max_tokens = 2600 if content_kind == "video" else 420
                generated = self._generate_ai(
                    prompt,
                    max_output_tokens=max_tokens,
                    purpose="script_draft",
                )
                generated = self._clean(generated)
                if content_kind == "video" and self._word_count(generated) < (1100 if channel.id == "ancient_history" else 850):
                    expand_prompt = self._long_video_expand_prompt(channel, topic, generated or topic.narration)
                    generated = self._clean(
                        self._generate_ai(
                            expand_prompt,
                            max_output_tokens=3600,
                            purpose="script_expansion",
                        )
                    )
                if self._word_count_ok(generated, content_kind=content_kind):
                    candidate = replace(
                        topic,
                        narration=generated,
                        narration_beats=self._sentences(generated),
                    )
                    if not self.editorial_quality_issues(
                        candidate,
                        beats=candidate.narration_beats,
                        content_kind=content_kind,
                    ):
                        provenance = self._accept_last_ai_content("script")
                        improved = replace(
                            candidate,
                            script_provider=provenance["provider"],
                            script_provider_endpoint_host=provenance["endpoint_host"],
                            script_provider_model=provenance["model"],
                        )
            except Exception:
                improved = topic

        return self._polish_scene_plan(channel, improved, content_kind=content_kind)
