from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from yt_auto.models import ChannelConfig, TopicCandidate


@dataclass
class TitleVariant:
    pattern_id: str
    title: str
    predicted_score: float = 0.0


class TitleLab:
    def __init__(self) -> None:
        self._pattern_templates = {
            "listicle": [
                ("listicle_artifacts", "3 artifacts that make {core} harder to dismiss"),
                ("listicle_evidence", "3 pieces of evidence that reframe {core}"),
                ("listicle_mistakes", "3 things everyone gets wrong about {core}"),
                ("listicle_consequences", "3 consequences of {core} nobody saw coming"),
                ("listicle_place", "3 physical clues that reveal the truth about {core}"),
            ],
            "story": [
                ("story_evidence", "The evidence behind {core} is stranger than the myth"),
                ("story_artifact", "The artifact that changed how historians see {core}"),
                ("story_aftermath", "What happened after {core} changed everything"),
                ("story_searchable", "{core}: the evidence, the conflict, and the aftermath"),
                ("story_pressure", "The real pressure behind {core} was not what people think"),
            ],
            "motivational": [
                ("motivation_lesson", "The lesson inside {core} that still hits today"),
                ("motivation_survival", "What {core} reveals about survival"),
                ("motivation_endure", "How people endured {core}"),
                ("motivation_pressure", "What {core} teaches about pressure and power"),
            ],
            "explainer": [
                ("explainer_body_cue", "The body cue that gives away {core}"),
                ("explainer_text_moment", "Why {core} can hijack an ordinary moment"),
                ("explainer_pause", "The tiny pause inside {core} has a reason"),
                ("explainer_avoidance", "You are not lazy, your brain is dodging discomfort"),
                ("explainer_heavy", "Why easy choices suddenly feel heavy"),
                ("explainer_reset", "A 10-second reset when {core} takes over"),
                ("explainer_relationship", "The relationship cue people notice before you do"),
            ],
        }
        self._generic_starts = (
            "the real story",
            "the true story",
            "the psychology behind",
            "why your brain does this",
            "why your mind gets stuck here",
            "why people act this way",
            "this mental bias changes your decisions",
            "the lesson inside",
            "what really happened in",
            "the hidden brain loop behind",
            "the behavior pattern behind",
            "why you keep doing this",
            "did you know",
            "have you ever",
        )

    def _core_phrase(self, topic: TopicCandidate) -> str:
        cleaned = (topic.title or "").strip()
        if topic.niche_id == "ancient_history" and (topic.subject or "").strip():
            cleaned = (topic.subject or "").strip().title()
        if topic.niche_id == "brain_lens" and (topic.subject or "").strip():
            cleaned = (topic.subject or "").strip()
        regex_prefixes = (
            r"^3 pieces of evidence that reframe (.+)$",
            r"^3 artifacts that make (.+?) harder to dismiss$",
            r"^3 physical clues that reveal the truth about (.+)$",
            r"^the artifact trail behind (.+)$",
            r"^the artifact that changed how historians see (.+)$",
            r"^how (.+?): how .+$",
            r"^how (.+?) quietly steals your attention$",
            r"^(.+?): the brain trick shaping your next reaction$",
            r"^the first clue (.+?) is (?:taking over|bending your judgment)$",
            r"^3 signs (.+?) is running your reaction$",
            r"^why (.+?) makes small moments feel huge$",
            r"^the tiny trigger that keeps (.+?) alive$",
            r"^the tiny trigger that turns (.+?) into a loop$",
            r"^why (.+?) feels so real in your brain$",
            r"^the brain pattern that makes (.+?) hard to stop$",
            r"^how to interrupt (.+?) before it takes over$",
            r"^3 signs (.+?) is shaping your reactions$",
            r"^3 body signals that reveal (.+?)$",
            r"^(.+?): the hidden loop behind your reaction$",
            r"^(.+?): the loop your brain keeps rewarding$",
        )
        for pattern in regex_prefixes:
            match = re.match(pattern, cleaned, flags=re.IGNORECASE)
            if match:
                cleaned = match.group(1).strip()
                break
        for prefix in ("The bizarre true story of ", "The real story behind ", "The real story of ", "The true story of ", "The psychology behind ", "The dating cue behind ", "Why your brain does this: ", "What ", "Why this feeling takes over: ", "Why people act this way: ", "Why your mind gets stuck here: ", "This mental bias changes your decisions: ", "The hidden reason behind ", "Why people keep doing this: ", "Why you keep doing this: ", "The behavior pattern behind "):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix):]
        cleaned = re.sub(
            r"^why\s+(.+?)\s+(?:can\s+)?(?:feel|feels)\s+(?:so\s+)?(?:addictive|hot|confusing|hot and confusing|heavy|urgent|personal)\s*$",
            r"\1",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        cleaned = re.sub(
            r"^what\s+(.+?)\s+says\s+about\s+(?:attraction|you|your brain|relationships)\s*$",
            r"\1",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        cleaned = re.sub(
            r"\s+says\s+about\s+(?:attraction|you|your brain|relationships)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        if ":" in cleaned:
            left, right = [part.strip() for part in cleaned.split(":", 1)]
            if 8 <= len(left) <= 58 and len(right) >= 8:
                cleaned = left
        cleaned = re.sub(r"\bAlexander Siege\b", "Alexander's Siege", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bOf\b", "of", cleaned)
        brain_core_rewrites = {
            "voice change attraction": "Voice Changes and Attraction",
            "eye contact attraction": "Eye Contact and Attraction",
            "chemistry versus compatibility": "Chemistry vs Compatibility",
            "mixed attachment signals": "Mixed Signals",
        }
        if topic.niche_id == "brain_lens" and cleaned.lower() in brain_core_rewrites:
            cleaned = brain_core_rewrites[cleaned.lower()]
        return cleaned or (topic.trend_terms[0].title() if topic.trend_terms else "Untold Truth")

    def title_contains_search_core(self, topic: TopicCandidate, title: str) -> bool:
        """Keep Brain Lens packaging tied to the actual searchable subject."""
        if topic.niche_id != "brain_lens":
            return True
        core_tokens = [
            token
            for token in re.findall(r"[a-z0-9']+", self._core_phrase(topic).lower())
            if token not in {"and", "or", "the", "to", "of", "in", "after", "versus", "vs"}
        ]
        if not core_tokens:
            return True
        title_tokens = re.findall(r"[a-z0-9']+", (title or "").lower())

        def stem(token: str) -> str:
            if len(token) > 4 and token.endswith("ies"):
                return token[:-3] + "y"
            if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
                return token[:-1]
            return token

        searchable = {stem(token) for token in title_tokens}
        if self._core_phrase(topic).strip().lower() == "reply time anxiety":
            return "reply" in searchable and bool({"late", "time", "timestamp"} & searchable)
        required = [stem(token) for token in core_tokens[:2]]
        return all(token in searchable for token in required)

    def _feels_phrase(self, core: str) -> str:
        lowered = (core or "").strip().lower()
        if lowered.endswith("s") and not lowered.endswith(("ss", "us")):
            return "feel"
        return "feels"

    def _changes_phrase(self, core: str) -> str:
        last_word = re.sub(r"[^a-z]", "", (core or "").strip().lower().split()[-1]) if core.strip() else ""
        plural_words = {
            "biases",
            "choices",
            "impressions",
            "memories",
            "neurons",
            "patterns",
            "relationships",
            "signals",
            "styles",
        }
        return "change" if last_word in plural_words else "changes"

    def make_variants(self, topic: TopicCandidate, count: int = 3) -> List[TitleVariant]:
        core = self._core_phrase(topic)
        templates = self._pattern_templates.get(topic.style, self._pattern_templates["story"])
        variants = [TitleVariant(pattern_id=pid, title=tpl.format(core=core)) for pid, tpl in templates]
        if topic.niche_id == "brain_lens" and getattr(topic, "content_kind", "short") == "video":
            if core.lower() == "friends with benefits boundaries":
                long_variants = [
                    TitleVariant(
                        "brain_long_fwb_feelings",
                        "Friends With Benefits Boundaries: When Feelings Change",
                    ),
                    TitleVariant(
                        "brain_long_fwb_casual",
                        "When Friends With Benefits Stops Feeling Casual",
                    ),
                    TitleVariant(
                        "brain_long_fwb_talk",
                        "Friends With Benefits: The Boundary Talk Most People Avoid",
                    ),
                    TitleVariant(
                        "brain_long_fwb_revisit",
                        "Friends With Benefits: When to Revisit the Agreement",
                    ),
                    TitleVariant(
                        "brain_long_fwb_checkin",
                        "Friends With Benefits: A Clearer Boundary Check-In",
                    ),
                ]
            else:
                long_variants = [
                    TitleVariant("brain_long_attraction", f"Why {core} feels like chemistry but may be your nervous system"),
                    TitleVariant("brain_long_attachment", f"The attachment loop behind {core} and how to break it"),
                    TitleVariant("brain_long_body", f"Why your body reacts before your mind explains {core}"),
                    TitleVariant("brain_long_cost", f"The hidden cost of {core} and how to interrupt it"),
                    TitleVariant("brain_long_reset", f"How {core} traps your attention and the reset that helps"),
                ]
            variants = long_variants + variants
        elif topic.niche_id == "brain_lens":
            if core.lower() == "mixed signals":
                variants = [
                    TitleVariant("brain_mixed_intense", "Why Mixed Signals Feel So Intense After Hot-and-Cold Attention"),
                    TitleVariant("brain_mixed_pull", "Mixed Signals: Why Hot-and-Cold Attention Pulls You Back"),
                    TitleVariant("brain_mixed_chemistry", "The Mixed-Signal Loop That Can Feel Like Chemistry"),
                ]
            elif core.lower() == "voice changes and attraction":
                variants = [
                    TitleVariant("brain_voice_signal", "What Voice Changes Can Signal but Cannot Prove About Attraction"),
                    TitleVariant("brain_voice_limits", "A Flirty Voice Shift Can Be a Clue, Not Proof"),
                    TitleVariant("brain_voice_question", "Their Voice Changes Around You—Does It Mean Attraction?"),
                    TitleVariant("brain_voice_context", "Voice Changes and Attraction: Why Context Matters"),
                ]
            elif core.lower() == "reply time anxiety":
                variants = [
                    TitleVariant(
                        "brain_reply_timestamp",
                        "Late Replies and Attachment Anxiety: What One Study Found",
                    ),
                    TitleVariant(
                        "brain_reply_pattern",
                        "Late Reply Anxiety: Why One Timestamp Proves Nothing",
                    ),
                    TitleVariant(
                        "brain_reply_context",
                        "Attachment Anxiety and Texting: Associations, Not Causes",
                    ),
                ]
            elif core.lower() == "eye contact and attraction":
                variants = [
                    TitleVariant("brain_gaze_signal", "What Eye Contact Can Signal About Attraction"),
                    TitleVariant("brain_gaze_limits", "Longer Eye Contact Can Be a Clue, Not Proof"),
                    TitleVariant("brain_gaze_context", "Eye Contact and Attraction: Why Context Matters"),
                ]
            elif core.lower() in {"crush idealization", "limerence"}:
                variants = [
                    TitleVariant("brain_crush_perfect", f"{core}: Why They Feel Perfect Before You Know Them"),
                    TitleVariant("brain_crush_facts", f"{core}: When Fantasy Runs Ahead of the Facts"),
                    TitleVariant("brain_crush_details", f"How {core} Turns a Few Details Into a Whole Relationship"),
                ]
            elif core.lower() in {"micro flirting", "flirting body language", "body language"}:
                variants = [
                    TitleVariant("brain_flirt_limits", f"{core}: How to Read the Spark Without Calling It Proof"),
                    TitleVariant("brain_flirt_friendliness", f"{core}: The Difference Between a Spark and Simple Friendliness"),
                    TitleVariant("brain_flirt_reciprocity", f"{core}: Why Reciprocity Matters More Than One Charged Cue"),
                ]
            else:
                relationship_core = any(
                    term in core.lower()
                    for term in (
                        "relationship", "dating", "attachment", "attraction", "chemistry", "flirt",
                        "kiss", "text", "breadcrumb", "situationship", "love bombing", "signal",
                        "future faking", "benching", "ghosting", "orbiting", "crush", "limerence",
                        "jealousy", "rebound", "exclusivity", "vulnerability", "conflict", "apology",
                        "pacing", "mutual effort", "emotional availability", "fear of intimacy",
                    )
                )
                if relationship_core:
                    variants = [
                        TitleVariant("brain_relationship_intensity", f"Why {core} Can Feel So Intense—and What Actually Matters"),
                        TitleVariant("brain_relationship_pattern", f"{core}: The Pattern to Watch Before You Invest More"),
                        TitleVariant("brain_relationship_clarity", f"{core}: How to Read the Moment Without Guessing"),
                    ]
                else:
                    variants = [
                        TitleVariant("brain_behavior_help", f"Why {core} Can Feel So Powerful—and What Actually Helps"),
                        TitleVariant("brain_behavior_pattern", f"{core}: The Pattern to Notice Before You React"),
                        TitleVariant("brain_behavior_choice", f"How {core} Shapes Your Next Choice"),
                    ]
        elif topic.niche_id == "ancient_history" and getattr(topic, "content_kind", "short") == "video":
            core_lower = core.lower()
            long_variants = []
            if "stonehenge" in core_lower:
                long_variants.extend([
                    TitleVariant(
                        "history_long_stonehenge_phases",
                        "Stonehenge Was Built Over 1,500 Years—Here’s What Survived",
                    ),
                    TitleVariant(
                        "history_long_stonehenge_stones",
                        "Stonehenge: How the Stones Moved and the Monument Changed",
                    ),
                ])
            if "lachish" in core_lower:
                long_variants.extend([
                    TitleVariant(
                        "history_long_lachish_ramp",
                        "How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence",
                    ),
                    TitleVariant(
                        "history_long_lachish_sources",
                        "Siege of Lachish: When Assyrian Propaganda Met Archaeology",
                    ),
                    TitleVariant(
                        "history_long_lachish_701",
                        "Lachish 701 BCE: The Siege Ramp That Broke a City",
                    ),
                ])
            if any(term in core_lower for term in ("battle", "siege", "sack", "revolt", "war")):
                long_variants.extend([
                    TitleVariant("history_long_battle", f"How {core} was decided by terrain, supply, and one turning point"),
                    TitleVariant("history_long_battle_record", f"{core}: what the battlefield evidence actually shows"),
                ])
            if any(term in core_lower for term in ("machu picchu", "stonehenge", "wall", "ziggurat", "temple", "petra", "city", "persepolis")):
                long_variants.extend([
                    TitleVariant("history_long_engineering", f"How {core} worked and the hidden system that sustained it"),
                    TitleVariant("history_long_ruins", f"What the surviving design of {core} reveals about power"),
                ])
            if any(term in core_lower for term in ("carthage", "silk road", "sogdian", "han dynasty")):
                long_variants.append(
                    TitleVariant("history_long_trade", f"How {core} became powerful through trade, geography, and control")
                )
            if any(term in core_lower for term in ("inscription", "tablet", "scroll", "code", "edicts", "cylinder", "library")):
                long_variants.append(
                    TitleVariant("history_long_text", f"What {core} actually records and what historians still debate")
                )
            long_variants.extend([
                TitleVariant("history_long_evidence", f"{core}: what the surviving evidence really shows"),
                TitleVariant("history_long_limits", f"How historians rebuilt {core} from incomplete evidence"),
            ])
            variants = long_variants + variants
        elif topic.niche_id == "ancient_history":
            narration = f"{topic.hook} {topic.narration}".lower()
            core_lower = core.lower()
            contextual_variants = []
            if "angkor wat" in core_lower:
                contextual_variants.append(
                    TitleVariant(
                        "history_short_subject_angkor",
                        "Inside Angkor Wat: The Towers, Moat, and Sacred Mountain",
                    )
                )
            elif core_lower == "tikal":
                contextual_variants.append(
                    TitleVariant(
                        "history_short_subject_tikal",
                        "Inside Tikal: Maya Kings, Reservoirs, and Jungle Temples",
                    )
                )
            elif "chichen itza" in core_lower:
                contextual_variants.append(
                    TitleVariant(
                        "history_short_subject_chichen",
                        "Inside Chichen Itza: El Castillo, the Great Ball Court, and Sacred Cenote",
                    )
                )
            elif "great zimbabwe" in core_lower:
                contextual_variants.append(
                    TitleVariant(
                        "history_short_subject_zimbabwe",
                        "Great Zimbabwe: How Archaeology Overturned a Colonial Myth",
                    )
                )
            elif "axum" in core_lower or "aksum" in core_lower:
                contextual_variants.append(
                    TitleVariant(
                        "history_short_subject_axum",
                        "Axum's Giant Stelae: Royal Tombs Built Like Stone Palaces",
                    )
                )
            elif "olmec" in core_lower and ("head" in core_lower or "colossal" in core_lower):
                contextual_variants.append(
                    TitleVariant(
                        "history_short_subject_olmec",
                        "Olmec Colossal Heads: Seventeen Rulers Carved in Basalt",
                    )
                )
            elif "rosetta stone" in core_lower:
                contextual_variants.append(
                    TitleVariant("history_short_rosetta", "How the Rosetta Stone Unlocked Egyptian Hieroglyphs")
                )
            elif "göbekli tepe" in core_lower or "gobekli tepe" in core_lower:
                contextual_variants.append(
                    TitleVariant("history_short_gobekli", "Why Göbekli Tepe's Stone Circles Are Older Than Stonehenge")
                )
            elif "cadaver synod" in core_lower:
                contextual_variants.append(
                    TitleVariant("history_short_cadaver", "Why the Cadaver Synod Put a Dead Pope on Trial")
                )
            elif "kingdom of kush" in core_lower:
                contextual_variants.append(
                    TitleVariant("history_short_kush", "How the Kingdom of Kush Ruled Egypt as the Twenty-Fifth Dynasty")
                )
            elif "nubian pyramid" in core_lower:
                contextual_variants.append(
                    TitleVariant("history_short_nubian", "How Nubian Pyramids Made Kushite Royal Power Visible in Stone")
                )
            elif "sogdian merchant" in core_lower:
                contextual_variants.append(
                    TitleVariant("history_short_sogdian", "How Sogdian Merchants Connected Cities Across the Silk Road")
                )
            elif core_lower == "petra":
                contextual_variants.append(
                    TitleVariant("history_short_petra", "How Petra's Rock-Cut City Rose on Major Trade Routes")
                )
            elif "machu picchu" in narration and "terrace" in narration:
                contextual_variants.append(TitleVariant("history_short_machu_water", "How Machu Picchu's terraces controlled water and erosion"))
            elif any(term in narration for term in ("obelisk", "stela", "stele")) and "tomb" in narration:
                contextual_variants.append(
                    TitleVariant("history_short_stelae", f"How {core} turned elite tombs into monuments")
                )
            elif any(term in narration for term in ("battle", "defeated", "victory", "army", "siege")):
                contextual_variants.append(TitleVariant("history_short_power", f"Why {core} changed the balance of power"))
            elif any(term in narration for term in ("inscription", "tablet", "scroll", "manuscript", "written record", "legal code")):
                contextual_variants.append(TitleVariant("history_short_text", f"What historians can read in {core}"))
            elif any(term in narration for term in ("merchant", "trade", "silk road", "trade route")):
                contextual_variants.append(TitleVariant("history_short_trade", f"How {core} connected the ancient world"))
            elif any(term in narration for term in ("plague", "collapse", "eruption", "destroyed", "fell apart")):
                contextual_variants.append(TitleVariant("history_short_crisis", f"How {core} reshaped the ancient world"))
            elif any(term in narration for term in ("built", "construction", "palace", "temple", "city")):
                contextual_variants.append(TitleVariant("history_short_built", f"What the design of {core} reveals"))
            elif any(term in narration for term in ("discovered", "excavated", "archaeologists found")):
                contextual_variants.append(TitleVariant("history_short_discovery", f"The discovery that changed how historians see {core}"))
            safe_variants = [
                TitleVariant("history_short_record", f"What the surviving record reveals about {core}"),
                TitleVariant("history_short_facts", f"{core}: the facts behind the famous version"),
                TitleVariant("history_short_matters", f"Why {core} still matters to historians"),
                TitleVariant("history_short_context", f"{core}: what happened and why it mattered"),
            ]
            variants = contextual_variants + safe_variants

        unsafe_ancient_original = topic.niche_id == "ancient_history" and any(
            phrase in (topic.title or "").lower()
            for phrase in (
                "3 pieces of evidence", "3 artifacts that", "3 physical clues",
                "the artifact trail behind", "the artifact that changed how historians see",
            )
        )
        if topic.title and not unsafe_ancient_original and all(v.title.lower() != topic.title.lower() for v in variants):
            original = TitleVariant(pattern_id="base_original", title=topic.title)
            if topic.niche_id in {"ancient_history", "brain_lens"}:
                variants.append(original)
            else:
                variants.insert(0, original)

        deduped = []
        seen = set()
        for variant in variants:
            key = variant.title.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(variant)

        return deduped[: max(2, count)]

    def _virality_score(self, channel_id: str, title: str) -> float:
        text = re.sub(r"\s+", " ", title or "").strip()
        lowered = text.lower()
        score = 1.0

        if 42 <= len(text) <= 82:
            score += 0.18
        elif len(text) > 95:
            score -= 0.22

        if any(char.isdigit() for char in text):
            score += 0.08
        if any(word in lowered for word in ("hidden", "strange", "cost", "loop", "evidence", "changed", "pattern", "aftermath")):
            score += 0.2
        if any(word in lowered for word in ("why", "what", "how")):
            score += 0.12
        if channel_id == "brain_lens" and any(word in lowered for word in ("you", "your", "brain", "mental", "attention", "loop", "signs", "body", "reactions")):
            score += 0.18
        if channel_id == "brain_lens" and any(term in lowered for term in ("one text", "tiny loop", "not lazy", "easy choices", "small reset", "your mood", "freeze")):
            score += 0.2
        if channel_id == "brain_lens" and any(term in lowered for term in ("body cue", "10-second", "one message", "tiny pause", "relationship cue", "dodging discomfort")):
            score += 0.18
        if channel_id == "brain_lens" and any(word in lowered for word in ("relationship", "stress", "anxiety", "burnout", "attachment", "confidence", "self", "people")):
            score += 0.14
        if channel_id == "brain_lens" and any(term in lowered for term in ("cannot prove", "may be", "can signal", "attention back", "attraction question")):
            score += 0.2
        if channel_id == "ancient_history" and any(word in lowered for word in ("evidence", "empire", "ancient", "rome", "forgotten", "artifact", "myth")):
            score += 0.18
        if channel_id == "ancient_history" and any(term in lowered for term in (
            "the hidden cost of",
            "became impossible to ignore",
            "changes the whole story",
            "3 hidden clues that explain",
            "3 details about",
            "3 pieces of evidence",
            "3 artifacts that",
            "3 physical clues",
            "the artifact trail behind",
            "the artifact that changed how historians see",
        )):
            score -= 0.55
        if lowered.startswith(self._generic_starts):
            score -= 0.38
        if re.match(r"^what .+ does before you notice it$", lowered):
            score -= 0.55
        if re.match(r"^why .+ feels personal even when it is not$", lowered):
            score -= 0.5
        if lowered.startswith("why you keep doing this"):
            score -= 0.45
        if "quietly steals your attention" in lowered:
            score -= 0.3
        if channel_id == "brain_lens" and any(term in lowered for term in ("subtle misdirection", "uncovering the hidden pattern", "hidden pattern of")):
            score -= 0.28
        if text.isupper():
            score -= 0.18
        hype_terms = ("shocking", "secret", "unbelievable", "insane", "forbidden")
        if sum(1 for term in hype_terms if term in lowered) > 1:
            score -= 0.22
        if channel_id == "brain_lens" and any(term in lowered for term in ("cure", "diagnose", "therapy hack")):
            score -= 0.35
        if channel_id == "brain_lens" and any(
            term in lowered
            for term in ("make anyone obsessed", "guaranteed attraction", "seduce anyone", "secretly cheating")
        ):
            score -= 1.0
        if self._hard_reject(channel_id, text):
            score -= 1.0
        if text.count(":") > 1:
            score -= 0.08
        return max(0.2, round(score, 4))

    def _hard_reject(self, channel_id: str, title: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", title or "").strip()
        lowered = cleaned.lower()
        if not cleaned:
            return "missing title"
        if re.match(r"^(why|how|what|when)\s+\1\b", lowered):
            return "repeated question word"
        if re.match(r"^why .+\b(?:keeps|makes|pulls|turns|hijacks|drives)\b.+\bfeels so addictive$", lowered):
            return "stacked title predicates"
        if re.match(
            r"^why .+\b(?:biases|choices|impressions|memories|neurons|patterns|relationships|signals|styles)\s+(?:changes|feels)\b",
            lowered,
        ):
            return "plural subject title grammar"
        if channel_id == "brain_lens" and lowered.endswith("feels so addictive") and any(
            term in lowered
            for term in (
                "emotional regulation", "attachment styles", "mirror neurons", "fawn response",
                "impostor syndrome", "learned helplessness", "memory distortion", "halo effect",
                "first impression", "anchoring effect", "rejection sensitivity",
            )
        ):
            return "misleading addictive framing"
        if channel_id == "brain_lens" and any(
            term in lowered
            for term in ("make anyone obsessed", "guaranteed attraction", "seduce anyone", "secretly cheating")
        ):
            return "deceptive or advertiser-unsafe framing"
        if channel_id == "brain_lens" and re.search(
            r"\b(?:voice change|eye contact|body language|one signal|tiny signal)\b.*\b(?:reveals?|proves?)\b.*\battraction\b",
            lowered,
        ):
            return "ambiguous attraction cue framed as proof"
        if (
            channel_id == "brain_lens"
            and re.match(r"^what .+ can signal and what it cannot prove$", lowered)
            and not any(term in lowered for term in ("voice change", "voice changes", "eye contact"))
        ):
            return "generic signal-limits title formula"
        if len(re.findall(r"[A-Za-z0-9']+", cleaned)) < 6:
            return "title too vague"
        if lowered.startswith(self._generic_starts):
            return "generic title pattern"
        if channel_id == "ancient_history" and any(term in lowered for term in (
            "the hidden cost of",
            "became impossible to ignore",
            "changes the whole story",
            "3 hidden clues that explain",
            "3 details about",
            "3 pieces of evidence",
            "3 artifacts that",
            "3 physical clues",
            "the artifact trail behind",
            "the artifact that changed how historians see",
        )):
            return "overused Ancient title formula"
        if re.match(r"^what .+ does before you notice it$", lowered):
            return "generic repeated Brain Lens title"
        if re.match(r"^why .+ feels personal even when it is not$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and "quietly steals your attention" in lowered:
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^why .+ (?:changes|change) your reaction so fast$", lowered):
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^the tiny signal that reveals .+$", lowered):
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
        if channel_id == "brain_lens" and "the brain loop behind your reaction" in lowered:
            return "generic repeated Brain Lens title"
        if channel_id == "brain_lens" and re.match(r"^when .+ feels like chemistry but may be uncertainty$", lowered):
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
        if re.search(r"\(?\s*\d+\s*characters?\s*\)?", lowered):
            return "AI note leaked into title"
        return None

    def _predict(
        self,
        channel_id: str,
        style: str,
        terms: List[str],
        variant: TitleVariant,
        feedback_state: Dict,
    ) -> float:
        channels = feedback_state.get("channels", {})
        channel_state = channels.get(channel_id, {})
        style_score = float(channel_state.get("styles", {}).get(style, {}).get("score", 1.0))
        pattern_score = float(channel_state.get("patterns", {}).get(variant.pattern_id, {}).get("score", 1.0))

        term_scores = channel_state.get("terms", {})
        if terms:
            avg_term = sum(float(term_scores.get(term, {}).get("score", 1.0)) for term in terms[:4]) / min(len(terms), 4)
        else:
            avg_term = 1.0

        original_bonus = 0.16 if variant.pattern_id == "base_original" else 0.0
        brevity_bonus = 0.08 if len(variant.title) <= 64 else 0.0
        viral_bonus = self._virality_score(channel_id, variant.title) * 0.22
        return round((style_score * 0.45) + (pattern_score * 0.22) + (avg_term * 0.15) + viral_bonus + original_bonus + brevity_bonus, 4)

    def choose(
        self,
        channel_id: str,
        topic: TopicCandidate,
        variants: List[TitleVariant],
        feedback_state: Dict,
        epsilon: float = 0.05,
    ) -> Tuple[TitleVariant, List[TitleVariant]]:
        scored = []
        for variant in variants:
            variant.predicted_score = self._predict(
                channel_id=channel_id,
                style=topic.style,
                terms=topic.trend_terms,
                variant=variant,
                feedback_state=feedback_state,
            )
            scored.append(variant)

        ranked = sorted(scored, key=lambda item: item.predicted_score, reverse=True)
        if not ranked:
            fallback = TitleVariant(pattern_id="base_fallback", title=topic.title, predicted_score=1.0)
            return fallback, [fallback]

        subject_specific = [
            variant
            for variant in ranked
            if variant.pattern_id.startswith("history_short_subject_")
            and not self._hard_reject(channel_id, variant.title)
        ]
        if channel_id == "ancient_history" and subject_specific:
            ranked = subject_specific

        reply_time_specific = [
            variant
            for variant in ranked
            if variant.pattern_id == "brain_reply_timestamp"
            and not self._hard_reject(channel_id, variant.title)
        ]
        if channel_id == "brain_lens" and reply_time_specific:
            ranked = reply_time_specific

        filtered = [
            variant
            for variant in ranked
            if not self._hard_reject(channel_id, variant.title)
            and (channel_id != "brain_lens" or self.title_contains_search_core(topic, variant.title))
        ]
        if filtered:
            ranked = filtered
        elif channel_id == "brain_lens":
            core = self._core_phrase(topic)
            fallback_title = f"Why {core} deserves a closer look in relationships"
            fallback = TitleVariant(pattern_id="brain_safe_fallback", title=fallback_title[:95], predicted_score=1.0)
            return fallback, [fallback] + ranked
        else:
            core = self._core_phrase(topic)
            fallback_title = f"Why {core} still matters to historians"
            fallback = TitleVariant(pattern_id="safe_searchable_fallback", title=fallback_title[:95], predicted_score=1.0)
            return fallback, [fallback] + ranked

        if len(ranked) > 1 and random.random() < epsilon:
            chosen = random.choice(ranked[1:])
        else:
            chosen = ranked[0]
        if self._hard_reject(channel_id, chosen.title):
            core = self._core_phrase(topic)
            fallback_title = (
                f"Why {core} deserves a closer look in relationships"
                if channel_id == "brain_lens"
                else f"Why {core} still matters to historians"
            )
            chosen = TitleVariant(pattern_id="safe_final_fallback", title=fallback_title[:95], predicted_score=1.0)

        return chosen, ranked
