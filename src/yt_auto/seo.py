from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Iterable, List
from urllib.parse import urlparse

import requests

from yt_auto.models import ChannelConfig, TopicCandidate
from yt_auto.source_registry import VERIFIED_CONTEXT_URL_SET
from yt_auto.title_lab import TitleVariant


STOP_WORDS = {
    "about", "after", "again", "against", "also", "because", "before", "behind",
    "being", "between", "could", "every", "everyone", "everything", "from",
    "have", "into", "just", "more", "most", "that", "their", "there", "these",
    "they", "this", "those", "through", "what", "when", "where", "which", "while",
    "with", "would", "your", "youre",
}

BLOCKED_SOURCE_TERMS = {
    "verywell", "frontiers", "books", "book", "media", "national", "geographic",
    "magazine", "documentaries", "factualamerica", "reader", "digest",
}

LOW_VALUE_SINGLE_TERMS = {
    "today", "relationship", "relationships", "love", "dynamic", "thing", "things",
    "cities", "year-old",
}

CHANNEL_SEO = {
    "ancient_history": {
        "primary_terms": [
            "ancient history",
            "history shorts",
            "ancient civilization",
            "archaeology",
            "historical mystery",
        ],
        "hashtags": ["#Shorts", "#AncientHistory", "#History", "#Archaeology"],
        "description_phrase": "ancient history, archaeology, empires, and historical mysteries",
        "subscribe_line": "Subscribe for evidence-led ancient history in under a minute.",
    },
    "brain_lens": {
        "primary_terms": [
            "psychology facts",
            "human behavior",
            "mental patterns",
            "self improvement",
        ],
        "hashtags": ["#Shorts", "#Psychology", "#BrainScience", "#HumanBehavior"],
        "description_phrase": "psychology, brain science, behavior, and mental patterns",
        "subscribe_line": "Subscribe for clear psychology and brain science shorts.",
    },
}


def _clean_text(value: object) -> str:
    text = re.sub(r"[<>]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _trim_words(text: str, max_chars: int) -> str:
    text = _clean_text(text)
    if len(text) <= max_chars:
        return text
    trimmed = text[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" -:;,")
    if len(trimmed) >= 35:
        return trimmed
    return text[:max_chars].rstrip(" -:;,")


def _trim_sentence(text: str, max_chars: int) -> str:
    text = _clean_text(text)
    if len(text) <= max_chars:
        return text
    window = text[: max_chars + 1]
    sentence_ends = [match.end() for match in re.finditer(r"[.!?](?=\s|$)", window)]
    if sentence_ends and sentence_ends[-1] >= int(max_chars * 0.55):
        return window[:sentence_ends[-1]].strip()
    return _trim_words(text, max_chars)


_DANGLING_ENDINGS = {
    "a", "an", "and", "as", "at", "because", "by", "for", "from", "how",
    "in", "into", "of", "on", "or", "the", "that", "to", "what", "when",
    "where", "which", "while", "with", "without",
}


def _is_complete_phrase(text: str) -> bool:
    cleaned = _clean_text(text).strip(" -:;,")
    if not cleaned or len(cleaned.split()) < 3:
        return False
    words = re.findall(r"[A-Za-z0-9']+", cleaned.lower())
    if not words or words[-1] in _DANGLING_ENDINGS:
        return False
    if cleaned.count("(") != cleaned.count(")"):
        return False
    if re.search(r"\b(?:reveals?|shows?|means?|because|including|such as)\s*$", cleaned, re.IGNORECASE):
        return False
    return True


def _complete_metadata_sentence(text: str, max_chars: int = 150) -> str:
    """Create a complete metadata bullet without slicing through a clause."""
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
    candidate = _trim_sentence(first_sentence, max_chars).strip(" -:;,")
    if not _is_complete_phrase(candidate):
        return ""
    if candidate[-1] not in ".!?":
        candidate += "."
    return candidate


def _natural_subject_keyword(subject: str, channel_id: str) -> str:
    cleaned = _clean_text(subject)
    lowered = cleaned.lower()
    brain_rewrites = {
        "voice change attraction": "voice changes and attraction",
        "eye contact attraction": "eye contact and attraction",
        "chemistry versus compatibility": "chemistry vs compatibility",
        "attachment anxiety after texting": "texting and attachment anxiety",
    }
    if channel_id == "brain_lens" and lowered in brain_rewrites:
        return brain_rewrites[lowered]
    return cleaned


def _source_url_shape_ok(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and "." in parsed.netloc


@lru_cache(maxsize=512)
def _source_url_is_live(value: str) -> bool:
    if not _source_url_shape_ok(value):
        return False
    if value in VERIFIED_CONTEXT_URL_SET:
        return True
    try:
        response = requests.get(
            value,
            timeout=(4, 8),
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": "yt-auto/1.0 editorial-source-check"},
        )
        live = 200 <= response.status_code < 400
        response.close()
        return live
    except Exception:
        return False


def _validated_source_urls(values: Iterable[str], limit: int = 4) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = str(value or "").strip()
        key = url.lower()
        if not url or key in seen or not _source_url_is_live(url):
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def _keyword_candidates(topic: TopicCandidate) -> list[str]:
    # Trend terms and preconfigured hashtag lists can be unrelated to the final
    # selected story. Only derive backend keywords from the evidence-backed
    # subject; channel/context terms are added separately below.
    raw = [topic.subject]
    candidates: list[str] = []
    for value in raw:
        cleaned = re.sub(r"[^A-Za-z0-9 #'-]+", " ", str(value or "")).strip(" #,")
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned and value != topic.subject:
            candidates.append(cleaned)

        if value == topic.subject:
            words = [
                word
                for word in re.findall(r"[A-Za-z0-9']+", cleaned.lower())
                if len(word) >= 4 and word not in STOP_WORDS and word not in BLOCKED_SOURCE_TERMS
            ]
            for size in (2, 3):
                for index in range(0, max(0, len(words) - size + 1)):
                    candidates.append(" ".join(words[index:index + size]))
    return candidates


def seo_keywords(channel: ChannelConfig, topic: TopicCandidate, limit: int = 18) -> list[str]:
    channel_terms = list(CHANNEL_SEO.get(channel.id, {}).get("primary_terms", []))
    if topic.content_kind != "short":
        channel_terms = [term for term in channel_terms if term.lower() != "history shorts"]
    contextual_terms: list[str] = []
    topic_blob = f"{topic.subject} {topic.title}".lower()
    if channel.id == "brain_lens" and any(
        term in topic_blob
        for term in (
            "relationship", "dating", "attachment", "attraction", "chemistry", "flirt",
            "kiss", "breadcrumb", "situationship", "push pull", "mixed signal",
        )
    ):
        channel_terms = [
            term for term in channel_terms
            if term.lower() not in {"mental patterns", "self improvement"}
        ]
        contextual_terms = [
            "relationship psychology", "dating psychology", "relationship communication",
            "healthy relationships", "emotional intelligence",
        ]
    elif channel.id == "brain_lens":
        if any(term in topic_blob for term in ("bias", "decision", "choice", "anchor", "halo")):
            contextual_terms.extend(["cognitive bias", "decision psychology"])
        if any(term in topic_blob for term in ("dopamine", "memory", "attention", "nervous system")):
            contextual_terms.extend(["brain science", "neuroscience"])
    elif channel.id == "ancient_history":
        if not any(term in topic_blob for term in ("mystery", "mysterious", "lost", "unknown")):
            channel_terms = [
                term for term in channel_terms
                if term.lower() != "historical mystery"
            ]
        if any(term in topic_blob for term in ("rome", "roman")):
            contextual_terms.append("roman empire")
        if any(term in topic_blob for term in ("egypt", "egyptian")):
            contextual_terms.append("ancient egypt")
        if any(term in topic_blob for term in ("lost", "collapse", "abandoned")):
            contextual_terms.append("lost civilization")
    raw_terms = [
        _natural_subject_keyword(topic.subject, channel.id),
        *_keyword_candidates(topic),
        *contextual_terms,
        *channel_terms,
    ]
    out: list[str] = []
    seen: set[str] = set()
    unnatural_subject_phrases: set[str] = set()
    if channel.id == "brain_lens" and topic.subject.lower().strip() == "voice change attraction":
        unnatural_subject_phrases = {"voice change attraction", "change attraction"}
    for term in raw_terms:
        cleaned = re.sub(r"[^A-Za-z0-9 #'-]+", " ", str(term or "")).strip(" #,")
        cleaned = re.sub(r"\s+", " ", cleaned)
        key = cleaned.lower()
        if not cleaned or len(cleaned) < 3:
            continue
        keyword_words = re.findall(r"[A-Za-z0-9']+", cleaned.lower())
        if (
            len(keyword_words) > 6
            or (keyword_words and keyword_words[-1] in _DANGLING_ENDINGS)
            or cleaned.count("(") != cleaned.count(")")
        ):
            continue
        if key in seen or key in STOP_WORDS or key in BLOCKED_SOURCE_TERMS or key in unnatural_subject_phrases:
            continue
        if " " not in key and key in LOW_VALUE_SINGLE_TERMS:
            continue
        if any(part in BLOCKED_SOURCE_TERMS for part in key.split()):
            continue
        seen.add(key)
        out.append(_trim_words(cleaned, 45))
        if len(out) >= limit:
            break
    return out


def _hashtag(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "", str(value or "").replace("#", ""))
    if not cleaned:
        return ""
    replacements = {
        "archeology": "Archaeology",
        "ancienthistory": "AncientHistory",
        "historicalfacts": "HistoryFacts",
        "historicalmysteries": "HistoricalMysteries",
        "humanbehavior": "HumanBehavior",
        "psychologyfacts": "PsychologyFacts",
        "brainscience": "BrainScience",
        "mentalhealth": "MentalHealth",
        "selfimprovement": "SelfImprovement",
    }
    return "#" + replacements.get(cleaned.lower(), cleaned[:32])


def seo_hashtags(channel: ChannelConfig, topic: TopicCandidate, content_kind: str, limit: int = 4) -> list[str]:
    defaults = CHANNEL_SEO.get(channel.id, {}).get("hashtags", [])
    base = list(defaults)
    topic_blob = f"{topic.subject} {topic.title}".lower()
    if channel.id == "brain_lens" and any(
        term in topic_blob
        for term in (
            "relationship", "dating", "attachment", "attraction", "chemistry",
            "reply", "texting", "friends with benefits", "consent",
        )
    ):
        base = ["#Psychology", "#Relationships", "#Communication", "#Consent"]
    if content_kind != "short":
        base = [tag for tag in base if tag.lower() != "#shorts"]
    topic_tags = [_hashtag(tag) for tag in list(topic.hashtags or [])]
    if topic.subject:
        topic_tags.append(_hashtag(topic.subject))
    out: list[str] = []
    seen: set[str] = set()
    for tag in [*base, *topic_tags]:
        clean = _hashtag(tag)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _tag_cost(tag: str) -> int:
    cost = len(tag)
    if " " in tag:
        cost += 2
    return cost


def fit_youtube_tags(tags: Iterable[str], max_chars: int = 500, max_count: int = 18) -> list[str]:
    fitted: list[str] = []
    used = 0
    for tag in _dedupe(str(tag).replace("#", "") for tag in tags):
        tag = tag[:45].strip()
        if not tag or tag.lower() in STOP_WORDS:
            continue
        next_cost = _tag_cost(tag) + (1 if fitted else 0)
        if used + next_cost > max_chars:
            continue
        fitted.append(tag)
        used += next_cost
        if len(fitted) >= max_count:
            break
    return fitted


def _title_score(channel: ChannelConfig, title: str, keywords: list[str]) -> tuple[int, list[str], list[str]]:
    score = 100
    issues: list[str] = []
    strengths: list[str] = []
    lowered = title.lower()

    if 42 <= len(title) <= 82:
        strengths.append("title is concise")
    elif len(title) < 35:
        issues.append("title may be too short for search context")
        score -= 10
    elif len(title) > 92:
        issues.append("title may truncate in feeds")
        score -= 12

    if title.isupper() and len(title) > 28:
        issues.append("title uses too much ALL CAPS")
        score -= 16
    if "#" in title:
        issues.append("title contains hashtags")
        score -= 12
    if re.search(r"\(?\s*\d+\s*characters?\s*\)?", lowered):
        issues.append("title contains AI length note")
        score -= 24
    if (
        lowered.startswith(("here's a youtube title", "here is a youtube title", "sure,", "option "))
        or "follows all the given rules" in lowered
        or "youtube title for shorts" in lowered
    ):
        issues.append("title contains AI instruction text")
        score -= 40
    if re.search(r"!!+|\?\?+", title):
        issues.append("title uses spammy punctuation")
        score -= 10

    keyword_hits = [keyword for keyword in keywords[:8] if keyword.lower() in lowered]
    if keyword_hits:
        strengths.append("title contains primary search context")
    else:
        issues.append("title lacks primary keyword context")
        score -= 12

    hype_terms = ("shocking", "secret", "unbelievable", "insane", "forbidden")
    if sum(1 for term in hype_terms if term in lowered) > 1:
        issues.append("title may feel overhyped")
        score -= 10
    if channel.id == "brain_lens" and any(term in lowered for term in ("cure", "diagnose", "therapy hack")):
        issues.append("title risks medical overclaim")
        score -= 18
    if channel.id == "brain_lens" and re.search(
        r"\b(?:voice change|eye contact|body language|one signal|tiny signal)\b.*\b(?:reveals?|proves?)\b.*\battraction\b",
        lowered,
    ):
        issues.append("title frames an ambiguous attraction cue as proof")
        score -= 28
    unsafe_or_deceptive = (
        "guaranteed attraction", "make anyone obsessed", "seduce anyone", "manipulate",
        "exposed without proof", "everyone is lying", "secretly cheating", "naked",
        "explicit sex", "revenge porn",
    )
    if any(term in lowered for term in unsafe_or_deceptive):
        issues.append("title uses deceptive or advertiser-unsafe framing")
        score -= 40

    return max(0, min(100, score)), issues, strengths


def _resolved_affiliates(channel: ChannelConfig) -> list[dict]:
    money = getattr(channel, "monetization", None)
    if not money or not getattr(money, "enabled", True):
        return []
    resolved: list[dict] = []
    for item in list(getattr(money, "affiliates", []) or []):
        if not isinstance(item, dict):
            continue
        env_key = str(item.get("url_env") or "").strip()
        url = (os.getenv(env_key) or "").strip() if env_key else ""
        label = str(item.get("label") or "Recommended").strip()
        if url.startswith("http"):
            resolved.append({"label": label, "url": url, "url_env": env_key})
    return resolved


def _description_lines(
    channel: ChannelConfig,
    topic: TopicCandidate,
    title: str,
    keywords: list[str],
    hashtags: list[str],
    content_kind: str,
) -> list[str]:
    profile = dict(CHANNEL_SEO.get(channel.id, {}))
    topic_blob = f"{topic.subject} {topic.title}".lower()
    relationship_topic = channel.id == "brain_lens" and any(
        term in topic_blob
        for term in (
            "relationship", "dating", "attachment", "attraction", "chemistry",
            "reply", "texting", "friends with benefits", "consent",
        )
    )
    if relationship_topic:
        profile["description_phrase"] = (
            "relationship communication, consent, and evidence-informed psychology"
        )
        profile["subscribe_line"] = "Subscribe for clear, consent-first relationship psychology."
    subject = _trim_words(_natural_subject_keyword(topic.subject, channel.id) or title, 80)
    bullet_candidates: list[str] = []
    if topic.scene_plan:
        bullet_candidates.extend(scene.narration for scene in topic.scene_plan[1:] if scene.narration)
    else:
        bullet_candidates.extend(topic.narration_beats[1:] if len(topic.narration_beats) > 1 else topic.narration_beats)
    if not bullet_candidates:
        bullet_candidates.extend(topic.visual_captions)
    captions = _dedupe(
        bullet
        for bullet in (_complete_metadata_sentence(candidate, 150) for candidate in bullet_candidates)
        if bullet
    )[:4]

    if content_kind == "video" and channel.id == "brain_lens":
        format_label = "full evidence-informed educational video"
    elif content_kind == "video":
        format_label = "full evidence-led documentary"
    else:
        format_label = "evidence-led Short"
    lines = [
        f"{title} - a {format_label} about {subject} for viewers interested in {profile.get('description_phrase', channel.niche_description)}.",
        "",
        _trim_words(topic.hook or subject, 220),
        "",
        "In this video:",
    ]
    if captions:
        lines.extend(f"- {caption}" for caption in captions)
    else:
        fallback_bullet = _complete_metadata_sentence(topic.hook, 150)
        lines.append(f"- {fallback_bullet or subject + '.'}")

    if topic.narration:
        if content_kind == "video":
            takeaway = _trim_sentence(topic.narration, 850)
            takeaway_label = "Why it matters:"
        else:
            final_beat = (topic.narration_beats or [topic.narration])[-1]
            takeaway = _complete_metadata_sentence(final_beat, 180)
            takeaway_label = "Takeaway:"
        if takeaway:
            lines.extend(["", takeaway_label, takeaway])

    source_lines = _validated_source_urls(
        topic.source_urls,
        limit=6 if content_kind == "video" else 4,
    )
    if source_lines:
        lines.extend(["", "Sources and context:", *source_lines])

    if keywords:
        lines.extend(["", "Search context: " + ", ".join(keywords[:8])])
    subscribe_line = profile.get("subscribe_line", f"Subscribe for more {channel.display_name} videos.")
    if content_kind == "video":
        subscribe_line = (
            subscribe_line
            .replace("shorts", "videos")
            .replace("in under a minute", "with deeper context")
            .replace("under a minute", "with deeper context")
        )
    money = getattr(channel, "monetization", None)
    if content_kind == "short":
        cta = ""
        playlist = ""
        if money and getattr(money, "enabled", True):
            cta = str(getattr(money, "long_form_cta", "") or "").strip()
            playlist = str(getattr(money, "long_form_playlist_url", "") or "").strip()
        lines.extend(
            [
                "",
                "Want the full story?",
                cta
                or f"Watch the longer {channel.display_name} videos on this channel for deeper context and more evidence.",
            ]
        )
        if playlist.startswith("http"):
            lines.append(playlist)
    affiliates = _resolved_affiliates(channel)
    if affiliates:
        lines.extend(["", "Helpful resources (affiliate links may earn a commission):"])
        for item in affiliates[:3]:
            lines.append(f"- {item['label']}: {item['url']}")
    lines.extend(["", subscribe_line])
    lines.extend(["", " ".join(hashtags)])
    return lines


def build_youtube_metadata(
    channel: ChannelConfig,
    topic: TopicCandidate,
    ranked_variants: List[TitleVariant],
    content_kind: str,
) -> dict:
    title = _trim_words(re.sub(r"#\w+", "", topic.title or topic.subject or "Untitled Video"), 96)
    keywords = seo_keywords(channel, topic)
    hashtags = seo_hashtags(channel, topic, content_kind)
    tags = fit_youtube_tags([*keywords, *(tag.replace("#", "") for tag in hashtags)])
    description = "\n".join(_description_lines(channel, topic, title, keywords, hashtags, content_kind))
    description = description.encode("utf-8")[:4900].decode("utf-8", "ignore").rstrip()
    affiliates = _resolved_affiliates(channel)
    money = getattr(channel, "monetization", None)
    pinned = ""
    if money and getattr(money, "enabled", True):
        pinned = str(getattr(money, "pinned_comment_template", "") or "").strip()
        if pinned:
            pinned = pinned.replace("{title}", title).replace("{channel}", channel.display_name)
        if not pinned and content_kind == "short":
            playlist = str(getattr(money, "long_form_playlist_url", "") or "").strip()
            pinned = (
                f"Full story in our longer videos"
                + (f": {playlist}" if playlist.startswith("http") else " on this channel.")
            )
            if affiliates:
                pinned += f" Resource: {affiliates[0]['label']}."

    title_score, title_issues, title_strengths = _title_score(channel, title, keywords)
    description_score = 100
    description_issues: list[str] = []
    description_strengths: list[str] = []
    if len(description) < 220:
        description_issues.append("description is thin")
        description_score -= 16
    else:
        description_strengths.append("description gives search context")
    if len(hashtags) > 5:
        description_issues.append("too many hashtags")
        description_score -= 14
    else:
        description_strengths.append("hashtags are focused")
    if not tags:
        description_issues.append("missing backend tags")
        description_score -= 12
    else:
        description_strengths.append("backend tags fit YouTube limit")

    bullet_lines = [line[2:] for line in description.splitlines() if line.startswith("- ")]
    incomplete_bullets = [line for line in bullet_lines if not _is_complete_phrase(line)]
    if incomplete_bullets:
        description_issues.append("description contains incomplete bullet phrases")
        description_score -= min(24, 8 * len(incomplete_bullets))
    valid_sources = _validated_source_urls(
        topic.source_urls,
        limit=6 if content_kind == "video" else 4,
    )
    if not valid_sources:
        description_issues.append("missing a validated source URL")
        description_score -= 22
    elif len(valid_sources) < len([url for url in topic.source_urls if url]):
        description_issues.append("one or more malformed, unreachable, or missing source URLs were removed")
        description_score -= 12

    seo_score = round((title_score * 0.48) + (description_score * 0.34) + (min(100, len(tags) * 8) * 0.18))
    category_id = "27" if channel.id == "ancient_history" else "24"

    return {
        "title": title,
        "subject": topic.subject,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "seo_keywords": keywords,
        "seo_score": seo_score,
        "seo_issues": title_issues + description_issues,
        "seo_strengths": title_strengths + description_strengths,
        "category_id": category_id,
        "selected_title_pattern": topic.selected_title_pattern,
        "title_variants": [
            {"title": variant.title, "pattern": variant.pattern_id, "predicted_score": variant.predicted_score}
            for variant in ranked_variants
        ],
        "source_urls": valid_sources,
        "visual_captions": topic.visual_captions,
        "scene_count": len(topic.scene_plan) if topic.scene_plan else len(topic.narration_beats),
        "content_kind": content_kind,
        "affiliate_links": affiliates,
        "pinned_comment": pinned,
        "long_form_playlist_url": (
            str(getattr(money, "long_form_playlist_url", "") or "").strip()
            if money and getattr(money, "enabled", True)
            else ""
        ),
    }
