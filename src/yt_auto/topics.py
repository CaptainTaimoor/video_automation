from __future__ import annotations

import random
import re
import os
from dataclasses import replace
from typing import Dict, List, Optional

from yt_auto.models import ChannelConfig, ScenePlanItem, TopicCandidate
from yt_auto.research import ContentResearcher
from yt_auto.trends import TrendScout



SAFE_BLOCKLIST = {
    "adult",
    "porn",
    "nude",
    "nudity",
    "sex",
    "explicit",
    "gore",
    "blood",
    "violence",
    "drugs",
    "weapon",
    "suicide",
    "murder",
}

GENERIC_TERMS = {
    "true",
    "best",
    "behind",
    "movies",
    "reader's",
    "real-life",
    "viral",
    "news",
}


class TopicPlanner:
    def __init__(self, timezone: str) -> None:
        self.trends = TrendScout(timezone=timezone)
        self.research = ContentResearcher()

    def _channel_brand(self, channel_id: str, content_kind: str = "short") -> dict:
        frame_phrase = "professional landscape composition" if content_kind == "video" else "professional vertical composition"
        history_frame_phrase = "cinematic realism, period-accurate detail, textured lighting, museum-grade composition, landscape frame" if content_kind == "video" else "cinematic realism, period-accurate detail, textured lighting, museum-grade composition, vertical frame"
        if channel_id == "ancient_history":
            return {
                "opener_templates": [
                    "The first hard clue in {subject} is not the legend; it is the evidence people can still inspect.",
                    "Start with the artifact from {subject}, because stone, text, and place reveal what the myth hides.",
                    "The physical clue in {subject} shows who built it, where it stood, and why it mattered.",
                    "The strongest evidence for {subject} begins with a place, a date, and a consequence.",
                    "Before the famous version of {subject}, there was a practical problem people had to survive.",
                ],
                "closer_templates": [
                    "The surviving evidence from {subject} matters because it lets historians test power against legend.",
                    "What survives from {subject} is evidence of how people organized power, labor, and belief.",
                    "The record of {subject} is strongest where archaeology and written sources can be compared.",
                ],
                "visual_style": f"archival documentary still, {history_frame_phrase}",
                "visual_terms": ["archaeology", "artifact", "historical reenactment"],
                "search_prefix": "historical documentary",
            }
        if channel_id == "brain_lens":
            return {
                "opener_templates": [
                    "They replied with one tiny word, and your whole nervous system changed.",
                    "If someone pulls away right after getting close, your brain reads it like danger.",
                    "A kiss can feel unforgettable because your brain is tagging safety, novelty, and reward at once.",
                    "The person who feels most addictive is not always the person who is best for you.",
                    "You think you miss them, but sometimes your brain is chasing the uncertainty loop.",
                    "When someone gives mixed signals, your brain can mistake anxiety for chemistry.",
                    "The fastest way to become attractive is not chasing harder; it is becoming emotionally harder to shake.",
                    "You are not obsessed with them; your brain may be obsessed with the almost.",
                    "Eye contact feels intense because the brain treats attention like a tiny social reward.",
                    "The hottest thing in attraction is often not mystery; it is calm confidence under pressure.",
                ],
                "closer_templates": [
                    "Follow for more spicy psychology without the toxic advice.",
                    "Follow for dating psychology that explains what your brain is really doing.",
                    "Follow for attraction, attachment, and human behavior explained simply.",
                ],
                "visual_style": f"premium dating psychology creator look, warm cinematic studio, attractive expressive presenter energy, stylish modern relationship visuals, close eye contact, phone texting tension, elegant nightlife colors, subtle brain science cues, {frame_phrase}",
                "visual_terms": ["dating psychology", "relationship psychology", "romantic attraction", "texting anxiety", "body language attraction", "eye contact flirting", "attachment style", "mixed signals", "emotional connection", "confidence dating"],
                "search_prefix": "relationship psychology",
            }
        return {
            "opener_templates": [
                "This real story sounds fake, but every part of {subject} actually happened.",
                "You would think {subject} was made up for a movie, but it is real.",
                "One of the wildest true stories ever is {subject}, and it only gets stranger.",
            ],
            "closer_templates": [
                "Subscribe for more unbelievable real stories and bizarre true events.",
                "Subscribe for more real stories that sound too insane to be true.",
                "Subscribe for more shocking true stories and strange real-world mysteries.",
            ],
            "visual_style": (
                "premium documentary still, realistic editorial lighting, cinematic depth, polished documentary composition, landscape frame"
                if content_kind == "video"
                else "premium documentary still, realistic editorial lighting, cinematic depth, polished documentary composition, vertical frame"
            ),
            "visual_terms": ["documentary still", "real world event", "editorial storytelling"],
            "search_prefix": "documentary",
        }

    def _safe_terms(self, terms: List[str]) -> List[str]:
        out = []
        for t in terms:
            low = t.lower().strip()
            if not low or low in GENERIC_TERMS or len(low) <= 3:
                continue
            if any(b in low for b in SAFE_BLOCKLIST):
                continue
            out.append(t)
        return out

    def _weighted_pick_style(self, styles: List[str], style_bias: Optional[Dict[str, float]]) -> str:
        if not styles:
            return "story"
        if not style_bias:
            return random.choice(styles)
        weights = [max(0.05, float(style_bias.get(s, 1.0))) for s in styles]
        return random.choices(styles, weights=weights, k=1)[0]

    def _rerank_terms(
        self,
        trend_pairs: List[tuple[str, int]],
        term_bias: Optional[Dict[str, float]],
    ) -> List[str]:
        if not trend_pairs:
            return []
        term_bias = term_bias or {}
        scored = []
        for term, trend_score in trend_pairs:
            learned = float(term_bias.get(term, 1.0))
            scored.append((term, float(trend_score) + (learned * 4.5)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [t for t, _ in scored]

    def _sentence(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip(" ,;:-")
        text = text.replace("...", ".")
        text = re.sub(r"\s+[-\u2013\u2014]\s+", ", ", text)
        text = re.sub(r"\s+,", ",", text)
        if not text:
            return ""
        if text[-1] not in ".!?":
            text += "."
        return text

    def _headline_subject(self, headline: str) -> str:
        subject = (headline or "").strip()
        for pattern in (
            r"^how (.+?): how .+$",
            r"^how (.+?) quietly steals your attention$",
            r"^(.+?): the brain trick shaping your next reaction$",
            r"^the first clue (.+?) is taking over$",
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
        for prefix in ("The bizarre true story of ", "The real story behind ", "The real story of ", "The true story of ", "The psychology behind ", "Why your brain does this: ", "What your brain is doing with ", "Why this feeling takes over: ", "Why people act this way: ", "Why your mind gets stuck here: ", "Why you keep doing this: ", "Why people keep doing this: ", "This mental bias changes your decisions: ", "The hidden reason behind ", "The behavior pattern behind ", "What "):
            if subject.lower().startswith(prefix.lower()):
                subject = subject[len(prefix):]
        if ":" in subject:
            left, right = [part.strip() for part in subject.split(":", 1)]
            if 8 <= len(left) <= 58 and len(right) >= 8:
                subject = left
        return subject.strip()

    def _display_subject_title(self, subject: str) -> str:
        normalized = re.sub(r"\s+", " ", subject or "").strip()
        if not normalized:
            return ""
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

    def _display_caption(self, text: str) -> str:
        text = self._sentence(text).rstrip(".!? ")
        if not text:
            return ""
        text = self._repair_history_fragment(text)
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
            r"(?:\b(?:to|and|of|the|a|an|with|for|in|on|at|from|because|why|how|what|that|which|still|inspect)\b[ ,;:-]*)+$",
            "",
            first_clause,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        if self._bad_history_fragment(first_clause):
            return ""
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

    def _repair_history_fragment(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        text = re.sub(
            r"\b(one of (?:the )?world'?s earliest major cities) became\b",
            r"\1 and became",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\bpeople can still\.?$", "", text, flags=re.IGNORECASE).strip(" ,;:-")
        text = re.sub(r"\bcan still\.?$", "", text, flags=re.IGNORECASE).strip(" ,;:-")
        return text

    def _bad_history_fragment(self, text: str) -> bool:
        lowered = re.sub(r"\s+", " ", text or "").strip().lower()
        if not lowered:
            return True
        bad_bits = (
            "one overlooked detail",
            "popular version",
            "material record",
            "anchor the scene",
            "messier chain",
            "clean myth",
            "it is the evidence",
            "people can still",
            "can still",
        )
        if any(bit in lowered for bit in bad_bits):
            return True
        return bool(re.search(r"\b(or|and|with|from|to|of|the|a|an|that|which|because|still|inspect|can)\.?$", lowered))

    def _hashtag(self, text: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]+", " ", text or "").strip()
        if not cleaned:
            return ""
        skip = {"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with"}
        parts = [part for part in cleaned.split() if part and part.lower() not in skip][:3]
        if not parts:
            return ""
        return "#" + "".join(part.capitalize() for part in parts)

    def _tidy_fragment(self, text: str) -> str:
        text = self._repair_history_fragment(text)
        text = re.sub(r"\s+", " ", text or "").strip(" ,;:-")
        text = re.sub(r"^(?:First|Then|Finally|Next|And the wildest part is),?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"(?:\b(?:in|on|at|to|of|with|for|from|and|or|but|that|which|why|how|what|still|inspect|can)\b[ ,;:-]*)+$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        if self._bad_history_fragment(text):
            return ""
        return self._sentence(text) if text else ""

    def _split_into_scene_lines(self, text: str, max_words: int = 15) -> List[str]:
        sentence = self._sentence(text).rstrip(".!? ")
        if not sentence:
            return []

        parts = re.split(r"(?<=[;:])\s+|\s+(?=(?:which|because|after|before|when|while|but|with)\b)", sentence)
        parts = [self._tidy_fragment(part) for part in parts if self._tidy_fragment(part)]
        if not parts:
            parts = [self._sentence(sentence)]

        out: List[str] = []
        for part in parts:
            words = part.rstrip(".!? ").split()
            if len(words) <= max_words:
                out.append(part)
                continue

            midpoint = max(6, len(words) // 2)
            split_at = midpoint
            for idx in range(midpoint, min(len(words) - 3, midpoint + 4)):
                if words[idx].lower() in {"and", "with", "after", "before", "when", "because", "while", "that"}:
                    split_at = idx
                    break
            first = self._tidy_fragment(" ".join(words[:split_at]))
            second = self._tidy_fragment(" ".join(words[split_at:]))
            if first:
                out.append(first)
            if second:
                out.append(second)

        merged: List[str] = []
        for item in out:
            cleaned = self._tidy_fragment(item)
            if not cleaned:
                continue
            lower = cleaned.lower()
            previous = merged[-1].lower().rstrip('.!? ') if merged else ''
            previous_ends_open = previous.endswith(("where", "which", "that", "because", "with", "when", "while"))
            should_merge = (
                len(cleaned.split()) < 5
                or lower.startswith(("which ", "and ", "or ", "because ", "with ", "after ", "before ", "when ", "while ", "that "))
                or cleaned[:1].islower()
                or previous_ends_open
            )
            if merged and should_merge:
                merged[-1] = self._tidy_fragment(merged[-1].rstrip(".!? ") + " " + cleaned) or merged[-1]
            else:
                merged.append(cleaned)
        return merged

    def _scene_search_terms(self, subject: str, text: str, extra_terms: List[str]) -> List[str]:
        subject = self._display_subject_title(subject) or subject
        proper_phrases = []
        for phrase in re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", text or ""):
            low = phrase.lower()
            if low in {"the", "this", "that", "one", "once", "if", "in"} or len(phrase) <= 2:
                continue
            if low in subject.lower() and len(phrase.split()) == 1:
                continue
            proper_phrases.append(phrase)
        caption = self._display_caption(text)
        lowered_subject = subject.lower()
        queries = []
        if "nazca" in lowered_subject or "nasca" in lowered_subject:
            queries.extend([
                "Nazca Lines Peru geoglyphs aerial view",
                "Nazca desert Peru archaeology",
                "South America Peru Nazca Lines",
            ])
        elif "carthage" in lowered_subject:
            queries.extend([
                "Carthage Tunisia archaeological site",
                "Carthage ruins Tunisia Punic harbor",
                "Carthage Phoenician Punic archaeology",
            ])
        elif "greek fire" in lowered_subject:
            queries.extend([
                "Byzantine Greek fire manuscript",
                "Greek fire Byzantine navy illustration",
                "Byzantine ship medieval fire siphon",
            ])
        queries.append(subject)
        if proper_phrases:
            queries.extend(proper_phrases[:3])
            queries.append(f"{subject} {proper_phrases[0]}")
        if caption and caption.lower() != subject.lower():
            queries.append(f"{subject} {caption}")
        queries.extend(extra_terms[:2])

        ordered: List[str] = []
        seen = set()
        for query in queries:
            q = re.sub(r"\s+", " ", query or "").strip()
            if not q:
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(q)
        return ordered[:4]

    def _build_scene_plan(
        self,
        channel: ChannelConfig,
        headline: str,
        bullets: List[str],
        image_urls: List[str],
        visual_queries: List[str],
        content_kind: str = "short",
    ) -> List[ScenePlanItem]:
        subject = self._display_subject_title(self._headline_subject(headline) or headline)
        brand = self._channel_brand(channel.id, content_kind=content_kind)
        factual_lines: List[str] = []
        line_word_limit = 22 if content_kind == "video" or channel.id == "ancient_history" else 16
        for bullet in bullets:
            if content_kind == "video":
                complete_fact = self._tidy_fragment(bullet)
                if complete_fact:
                    factual_lines.append(complete_fact)
            else:
                factual_lines.extend(self._split_into_scene_lines(bullet, max_words=line_word_limit))

        if not factual_lines:
            factual_lines = [
                f"{subject} left a physical trace: a site, object, inscription, or route historians can compare.",
                f"The strongest clue in {subject} is the practical problem people faced on the ground.",
                f"The consequence matters because {subject} changed trade, power, religion, or daily survival.",
            ]

        deduped_facts: List[str] = []
        seen = set()
        for line in factual_lines:
            normalized = self._tidy_fragment(line)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped_facts.append(normalized)
            max_facts = 16 if content_kind == "video" else 6
            if len(deduped_facts) >= max_facts:
                break

        opener = random.choice(brand["opener_templates"]).format(subject=subject)
        closer = random.choice(brand["closer_templates"]).format(subject=subject)
        image_pool = list(dict.fromkeys([url for url in image_urls if url]))
        extra_terms = [term for term in visual_queries if term and not term.startswith("http") and not term.startswith("https")]
        extra_terms.extend(brand["visual_terms"])

        scene_lines = [opener] + deduped_facts + [closer]
        scene_plan: List[ScenePlanItem] = []
        for idx, line in enumerate(scene_lines):
            is_opener = idx == 0
            is_closer = idx == len(scene_lines) - 1
            if is_opener:
                visual_text = self._display_caption(subject) or subject
            elif is_closer:
                visual_text = self._display_caption(closer) or "Final takeaway"
            else:
                visual_text = self._display_caption(line) or self._display_caption(subject) or subject

            image_index = min(idx, len(image_pool) - 1) if image_pool else -1
            preferred_image_url = image_pool[image_index] if image_index >= 0 else ""
            if is_opener:
                if channel.id == "brain_lens":
                    cinematic_terms = [
                        f"{subject} {brand['search_prefix']}",
                        f"{subject} human behavior",
                        f"{subject} realistic portrait",
                        subject,
                    ] + extra_terms[:2]
                else:
                    cinematic_terms = [f"{subject} {brand['search_prefix']}", subject] + extra_terms[:2]
            elif is_closer:
                cinematic_terms = [f"{subject} {brand['search_prefix']}", subject]
            else:
                cinematic_terms = [f"{subject} {brand['search_prefix']}"]
                if visual_text and len(visual_text.split()) <= 10:
                    cinematic_terms.append(f"{subject} {visual_text}")
                cinematic_terms.extend(extra_terms[:2])
            cinematic_terms = list(dict.fromkeys([term for term in cinematic_terms if term]))[:4]
            prompt_focus = visual_text
            if is_closer or prompt_focus.lower() == subject.lower():
                prompt_focus = ""
            visual_prompt = f"{subject}. {brand['visual_style']}"
            if prompt_focus:
                visual_prompt = f"{subject}. {prompt_focus}. {brand['visual_style']}"
            scene_plan.append(
                ScenePlanItem(
                    narration=self._sentence(line),
                    visual_text=visual_text,
                    search_terms=cinematic_terms,
                    preferred_image_url=preferred_image_url,
                    visual_prompt=visual_prompt,
                )
            )
        return scene_plan

    def plan(
        self,
        channel: ChannelConfig,
        style_bias: Optional[Dict[str, float]] = None,
        term_bias: Optional[Dict[str, float]] = None,
        avoid_titles: Optional[set[str]] = None,
        content_kind: str = "short",
        dna: Optional[dict] = None,
    ) -> TopicCandidate:
        avoid_titles = {t.lower().strip() for t in (avoid_titles or set()) if t}

        trend_pairs = self.trends.collect(channel.seed_keywords, limit=20)
        ranked_terms = self._rerank_terms(trend_pairs, term_bias)
        trend_terms = self._safe_terms(ranked_terms)
        if not trend_terms:
            trend_terms = [k.lower() for k in channel.seed_keywords]
        if not trend_terms:
            trend_terms = ["true story"]

        # Inject DNA video ideas as extra topic seeds (30% chance to use one)
        dna_ideas: list[dict] = []
        if dna and dna.get("video_ideas"):
            dna_ideas = [idea for idea in dna["video_ideas"]
                         if isinstance(idea, dict) and idea.get("title")]

        def _build_topic(chosen_terms: List[str]) -> TopicCandidate:
            style = self._weighted_pick_style(channel.styles, style_bias)
            if channel.id == "ancient_history":
                research_pack = self.research.history_pack(
                    chosen_terms,
                    avoid_subjects=avoid_titles,
                    content_kind=content_kind,
                )
            elif channel.id == "brain_lens":
                research_pack = self.research.brain_lens_pack(chosen_terms, avoid_subjects=avoid_titles)
                style = "explainer"
            else:
                story_queries = channel.seed_keywords[:4] if channel.seed_keywords else chosen_terms
                # Shuffle the subject list inside research_pack if it picks from a list
                research_pack = self.research.story_pack(story_queries)
                style = "story"

            headline = research_pack["headline"]
            bullets = [self._sentence(b) for b in research_pack["bullets"] if self._sentence(b)]
            visual_queries = research_pack.get("visual_queries", [])
            image_urls = [u for u in research_pack.get("image_urls", []) if u]
            scene_plan = self._build_scene_plan(
                channel=channel,
                headline=headline,
                bullets=bullets,
                image_urls=image_urls,
                visual_queries=visual_queries,
                content_kind=content_kind,
            )
            beats = [scene.narration for scene in scene_plan]
            narration = " ".join(beats)
            hook = scene_plan[0].narration

            image_queries = list(dict.fromkeys(image_urls + visual_queries + [headline, channel.niche_description]))

            subject_tag = self._hashtag(research_pack.get("subject", self._headline_subject(headline)))
            tag_candidates = channel.hashtags + ([subject_tag] if subject_tag else [])
            hashtags = [tag for tag in dict.fromkeys(tag_candidates) if tag]

            term_scores: Dict[str, int] = {t: s for t, s in trend_pairs}
            base_score = sum(term_scores.get(t, 1) for t in chosen_terms[:5])
            engagement_score = round(min(100.0, 30 + base_score / 2), 2)

            visual_captions = [scene.visual_text for scene in scene_plan[1:-1] if scene.visual_text]

            # Optional only: transcript scraping is too slow for scheduled Shorts.
            transcripts = []
            if str(os.getenv("YT_ENABLE_TRANSCRIPT_RESEARCH", "0")).lower() in {"1", "true", "yes", "on"}:
                try:
                    transcripts = self.research.search_youtube_transcripts(headline, max_videos=1)
                except Exception:
                    pass

            return TopicCandidate(
                niche_id=channel.id,
                style=style,
                trend_terms=chosen_terms,
                title=headline,
                subject=research_pack.get("subject", headline),
                hook=hook,
                narration=narration,
                visual_captions=visual_captions,
                source_urls=research_pack.get("sources", []),
                image_queries=image_queries,
                hashtags=hashtags[:12],
                engagement_score=engagement_score,
                content_kind=content_kind,
                narration_beats=beats,
                title_variants=[headline],
                selected_title_pattern="base_original",
                scene_plan=scene_plan,
                transcripts=transcripts,
            )

        attempts = 30
        for attempt in range(attempts):
            # Direct DNA title overrides keep the previously built subject and
            # scene plan, so they can silently create a title/script mismatch.
            # Brain Lens still uses DNA style/pacing elsewhere, but its topics
            # remain in the curated relationship catalog.
            use_dna = (
                channel.id not in {"ancient_history", "brain_lens"}
                and dna_ideas
                and (attempt % 3 == 1)
            )
            if use_dna:
                idea = random.choice(dna_ideas)
                idea_title = idea.get("title", "")
                idea_angle = idea.get("angle", "")
                idea_clean = idea_title.lower().strip()
                # Skip if this DNA idea overlaps with avoid_titles
                if any(idea_clean in a or a in idea_clean for a in avoid_titles if len(a) > 5):
                    use_dna = False

            if use_dna:
                # Build a special topic using the DNA idea as the headline
                idea = random.choice(dna_ideas)
                style = self._weighted_pick_style(channel.styles, style_bias)
                chosen = list(dict.fromkeys([idea["title"]] + trend_terms[:3]))
                topic = _build_topic(chosen)
                # Override the headline with the DNA idea title
                topic = replace(
                    topic,
                    title=idea["title"],
                    subject=idea.get("angle") or idea["title"],
                )
            else:
                shuffled = trend_terms[:]
                random.shuffle(shuffled)
                k_count = random.randint(2, 5)
                chosen = shuffled[:k_count] if shuffled else ["true story"]
                topic = _build_topic(chosen)

            # Duplicate check
            title_clean = topic.title.lower().strip()
            subject_clean = self._headline_subject(topic.subject or topic.title).lower().strip() or topic.subject.lower().strip()
            topic_identity = self._headline_subject(topic.title).lower().strip() or subject_clean
            forced_history = re.sub(
                r"[^a-z0-9]+",
                " ",
                os.getenv("YT_FORCE_HISTORY_SUBJECT", "").lower(),
            ).strip()
            forced_pin = bool(
                forced_history
                and any(
                    forced_history == item
                    or forced_history in item
                    or item in forced_history
                    for item in {subject_clean, topic_identity, title_clean} - {""}
                )
            )

            is_duplicate = False
            if not forced_pin:
                for avoid in avoid_titles:
                    a = avoid.lower().strip()
                    avoid_identity = self._headline_subject(a).lower().strip() or a
                    if (
                        avoid_identity == topic_identity
                        or avoid_identity == subject_clean
                        or topic_identity == avoid_identity
                        or subject_clean == avoid_identity
                        or (len(avoid_identity) > 5 and avoid_identity in topic_identity)
                        or (len(topic_identity) > 5 and topic_identity in avoid_identity)
                    ):
                        is_duplicate = True
                        break

                    ignore = {
                        "bizarre", "story", "true", "real", "behind", "history", "ancient", "mystery", "shocking",
                        "territory", "angle", "unseen", "facts", "psychology", "brain", "behavior", "human",
                        "mind", "people", "pattern", "mental", "science", "effect", "styles", "your", "this",
                        "untold", "forgotten", "secrets", "everything", "evidence", "discovery", "unbelievable",
                    }
                    words_in_avoid = {w for w in re.findall(r"\w{4,}", avoid_identity) if w not in ignore}
                    words_in_topic = {w for w in re.findall(r"\w{4,}", topic_identity + " " + subject_clean) if w not in ignore}

                    if len(words_in_avoid & words_in_topic) >= 1:
                        is_duplicate = True
                        break

            if not is_duplicate:
                return topic

        return topic
