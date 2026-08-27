from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yt_auto.models import ScenePlanItem, TopicCandidate
from yt_auto.pipeline import ShortsFactory
from yt_auto.thumbnailer import ThumbnailMaker
from yt_auto.title_lab import TitleVariant
from yt_auto.tts_engine import NarrationEngine
from yt_auto.utils import ensure_dir, now_in_tz, read_json, write_json


SOURCE_RUNS = {
    "ancient_short": ROOT / "output/ancient_history/short/2026-07-19/20260719_053536",
    "brain_short": ROOT / "output/brain_lens/short/2026-07-20/20260720_014630",
    "brain_long": ROOT / "output/brain_lens/video/2026-07-19/20260719_173019",
    "ancient_long": ROOT / "output/ancient_history/video/2026-07-20/20260720_001817",
}

# This asset set was fetched by the first fail-closed Brain rebuild. The source
# and relationship-relevance gates passed; only its narration CPS failed before
# any video was encoded. Reusing the already-audited 12 unique assets avoids a
# second network search while the corrected narration is synthesized anew.
AUDITED_BRAIN_SHORT_ASSET_RUN = (
    ROOT / "output/brain_lens/short/2026-07-20/20260720_023502"
)
ANCIENT_SHORT_FETCH_RUN = (
    ROOT / "output/ancient_history/short/2026-07-20/20260720_024732"
)
ANCIENT_SHORT_ASSET_PACK = (
    ROOT / "output/audit_asset_packs/great_zimbabwe_unique_12"
)

GREAT_ZIMBABWE_SOURCES = [
    "https://whc.unesco.org/en/list/364/",
    "https://www.metmuseum.org/essays/great-zimbabwe-11th-15th-century",
]

REPLY_TIME_SOURCES = [
    "https://pubmed.ncbi.nlm.nih.gov/35085449/",
    "https://www.apa.org/news/press/releases/2018/08/relationship-texting",
]

FWB_SOURCES = [
    "https://pubmed.ncbi.nlm.nih.gov/34779977/",
    "https://rainn.org/share-the-facts/consent-101-respect-boundaries-and-building-trust/",
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC12124867/",
]


def _fresh_run_dir(factory: ShortsFactory, channel_id: str, content_kind: str) -> Path:
    channel = factory._channel(channel_id)
    now = now_in_tz(factory.config.app.timezone)
    day_dir = ensure_dir(channel.output_dir / content_kind / now.strftime("%Y-%m-%d"))
    stem = now.strftime("%Y%m%d_%H%M%S")
    candidate = day_dir / stem
    serial = 1
    while candidate.exists():
        candidate = day_dir / f"{stem}_audit_{serial:02d}"
        serial += 1
    return ensure_dir(candidate)


def _short_scene(
    narration: str,
    visual_text: str,
    *search_terms: str,
) -> ScenePlanItem:
    return ScenePlanItem(
        narration=narration,
        visual_text=visual_text,
        search_terms=[term for term in search_terms if term],
        visual_prompt=(
            f"{visual_text}. {narration} Evidence-led, cinematic, specific, "
            "no generic title card, no text baked into the source image."
        ),
    )


def _great_zimbabwe_topic(base: TopicCandidate) -> TopicCandidate:
    scene_beats = [
        (
            "Colonial writers denied Great Zimbabwe's origins. "
            "Finds confirmed its African origins independently."
        ),
        (
            "Shona ancestors built Great Zimbabwe. "
            "It became a key African capital. "
            "Construction spanned 1100 through 1450 CE."
        ),
        (
            "Granite walls rose without any mortar. "
            "Cattle may have fueled elite wealth. "
            "Its state reached rich gold lands."
        ),
        (
            "Excavations found imported beads and ceramics. "
            "They reveal broad Indian Ocean trade."
        ),
        (
            "Birds may have marked royal authority. "
            "Even their exact meaning remains unknown."
        ),
        "The evidence confirms African origins. Great Zimbabwe linked distant trade.",
    ]
    scene_plan = [
        _short_scene(
            scene_beats[0],
            "COLONIAL CLAIM DISPROVED",
            "Great Zimbabwe early archaeology colonial denial African builders",
            "Great Zimbabwe historical excavation ruins",
        ),
        _short_scene(
            scene_beats[1],
            "SHONA BUILDERS: 1100-1450",
            "Great Zimbabwe Great Enclosure dry stone walls",
            "Great Zimbabwe Hill Complex Valley Ruins",
        ),
        _short_scene(
            scene_beats[2],
            "DRY STONE / CATTLE / GOLD",
            "Great Zimbabwe mortarless granite wall conical tower",
            "Great Zimbabwe archaeology cattle gold evidence",
        ),
        _short_scene(
            scene_beats[3],
            "INDIAN OCEAN TRADE EVIDENCE",
            "Great Zimbabwe imported glass beads ceramics archaeology",
            "Great Zimbabwe Indian Ocean trade artifacts",
        ),
        _short_scene(
            scene_beats[4],
            "THE SOAPSTONE BIRDS",
            "Zimbabwe Bird soapstone sculpture Great Zimbabwe",
            "Great Zimbabwe soapstone bird artifact",
        ),
        _short_scene(
            scene_beats[5],
            "AN AFRICAN CAPITAL",
            "Great Zimbabwe panoramic ruins Zimbabwe",
            "Great Zimbabwe Great Enclosure aerial ruins",
        ),
    ]
    beats = [
        sentence.strip()
        for scene in scene_beats
        for sentence in re.split(r"(?<=[.!?])\s+", scene)
        if sentence.strip()
    ]
    title = "Great Zimbabwe: How Archaeology Overturned a Colonial Myth"
    return replace(
        base,
        title=title,
        subject="Great Zimbabwe",
        hook=scene_beats[0],
        narration=" ".join(scene_beats),
        narration_beats=beats,
        visual_captions=[scene.visual_text for scene in scene_plan],
        source_urls=list(GREAT_ZIMBABWE_SOURCES),
        image_queries=[term for scene in scene_plan for term in scene.search_terms[:1]],
        scene_plan=scene_plan,
        title_variants=[title],
        selected_title_pattern="evidence_reversal",
        trend_terms=[
            "great zimbabwe",
            "shona history",
            "african archaeology",
            "african history",
            "colonial archaeology",
            "dry-stone architecture",
            "indian ocean trade",
            "zimbabwe birds",
        ],
        hashtags=["AncientHistory", "Archaeology", "History"],
        content_kind="short",
        script_provider="",
        script_provider_endpoint_host="",
        script_provider_model="",
    )


def _reply_time_topic(base: TopicCandidate) -> TopicCandidate:
    scene_beats = [
        "Their reply arrives late on-screen. But one timestamp proves nothing.",
        "One study surveyed 302 partnered undergraduates. Participants reported attachment and messaging patterns.",
        "More anxious participants wanted more messages. They wanted more than came.",
        "They also replied faster than partners. Those findings describe associations, not causes.",
        "This sample limits broad claims overall. Messaging preferences and circumstances still vary.",
        "Check urgency and their usual pace.",
        "Compare plans, reasons, and later actions.",
        "Ask clearly when context is missing. Judge the pattern, not one reply.",
    ]
    captions = [
        "TIMESTAMP IS NOT REJECTION",
        "302 PARTNERED UNDERGRADUATES",
        "ONE STUDY, NOT A VERDICT",
        "ASSOCIATION, NOT CAUSATION",
        "ONE SAMPLE HAS LIMITS",
        "CHECK URGENCY AND CONTEXT",
        "COMPARE FOLLOW-THROUGH",
        "JUDGE THE PATTERN",
    ]
    scene_plan = [
        _short_scene(
            scene_beats[index],
            captions[index],
            "adult checking smartphone message timestamp realistic lifestyle",
            "adult relationship texting response time anxiety",
        )
        for index in range(len(scene_beats))
    ]
    beats = [
        sentence.strip()
        for scene in scene_beats
        for sentence in re.split(r"(?<=[.!?])\s+", scene)
        if sentence.strip()
    ]
    title = "Late Replies and Attachment Anxiety: What One Study Found"
    return replace(
        base,
        title=title,
        subject="Reply Time Anxiety",
        hook=scene_beats[0],
        narration=" ".join(scene_beats),
        narration_beats=beats,
        visual_captions=captions,
        source_urls=list(REPLY_TIME_SOURCES),
        image_queries=[
            "adult checking smartphone reply time relationship",
            "adult couple discussing messages calmly",
        ],
        scene_plan=scene_plan,
        title_variants=[title],
        selected_title_pattern="research_tension",
        trend_terms=[
            "attachment anxiety",
            "texting behavior",
            "relationship communication",
            "romantic attachment",
            "messaging preferences",
            "dating psychology",
        ],
        hashtags=["Psychology", "Relationships"],
        content_kind="short",
        script_provider="",
        script_provider_endpoint_host="",
        script_provider_model="",
    )


def _brain_long_topic(factory: ShortsFactory, base: TopicCandidate) -> TopicCandidate:
    channel = factory._channel("brain_lens")
    scene_plan = factory.script_writer._brain_long_video_plan(
        channel,
        base,
        base.subject or "Friends With Benefits Boundaries",
    )
    beats = [scene.narration for scene in scene_plan if scene.narration]
    if len(beats) != 20:
        raise RuntimeError(f"Brain long editorial plan has {len(beats)} scenes; expected 20.")
    narration = " ".join(beats)
    word_count = len(re.findall(r"[A-Za-z0-9']+", narration))
    if not 850 <= word_count <= 1600:
        raise RuntimeError(f"Brain long editorial plan has unsafe word count {word_count}.")
    topic = replace(
        base,
        hook=re.split(r"(?<=[.!?])\s+", beats[0], maxsplit=1)[0].strip(),
        narration=narration,
        narration_beats=beats,
        visual_captions=[scene.visual_text for scene in scene_plan],
        scene_plan=scene_plan,
        source_urls=list(FWB_SOURCES),
        trend_terms=[
            "friends with benefits",
            "FWB boundaries",
            "relationship communication",
            "sexual communication",
            "consent",
            "casual relationships",
            "boundary setting",
            "relationship check-in",
        ],
        hashtags=["Psychology", "Relationships", "Consent"],
        title_variants=[
            base.title,
            "When Friends With Benefits Stops Feeling Casual",
            "Friends With Benefits: The Boundary Talk Most People Avoid",
            "Friends With Benefits: When to Revisit the Agreement",
            "Friends With Benefits: A Clearer Boundary Check-In",
        ],
        script_provider="",
        script_provider_endpoint_host="",
        script_provider_model="",
    )
    issues = factory.script_writer.editorial_quality_issues(
        topic,
        beats=beats,
        content_kind="video",
    )
    if issues:
        raise RuntimeError(f"Brain long editorial plan failed before TTS: {issues}")
    return topic


def _ancient_long_topic(factory: ShortsFactory, base: TopicCandidate) -> TopicCandidate:
    channel = factory._channel("ancient_history")
    scenes = factory.script_writer._history_long_video_plan(
        channel,
        base,
        base.subject or "Assyrian Siege of Lachish",
    )
    beats = [scene.narration for scene in scenes if scene.narration]
    if len(beats) != 20:
        raise RuntimeError(f"Lachish editorial plan has {len(beats)} scenes; expected 20.")
    narration = " ".join(beats)
    word_count = len(re.findall(r"[A-Za-z0-9']+", narration))
    if not 1150 <= word_count <= 1600:
        raise RuntimeError(f"Lachish editorial plan has unsafe word count {word_count}.")
    topic = replace(
        base,
        hook=re.split(r"(?<=[.!?])\s+", beats[0], maxsplit=1)[0].strip(),
        narration=narration,
        narration_beats=beats,
        visual_captions=[scene.visual_text for scene in scenes],
        scene_plan=scenes,
        source_urls=factory.topic_planner.research.enrich_source_urls(
            base.subject or "Assyrian Siege of Lachish",
            list(base.source_urls),
            limit=6,
        ),
        script_provider="",
        script_provider_endpoint_host="",
        script_provider_model="",
    )
    if "thousands of kilometers" in topic.narration.lower():
        raise RuntimeError("The known Lachish distance error survived correction.")
    if "nearly a thousand kilometers" not in topic.narration.lower():
        raise RuntimeError("The corrected Lachish distance wording is missing.")
    issues = factory.script_writer.editorial_quality_issues(
        topic,
        beats=beats,
        content_kind="video",
    )
    if issues:
        raise RuntimeError(f"Lachish editorial plan failed before TTS: {issues}")
    return topic


def _copy_reused_assets(source_run: Path, run_dir: Path) -> tuple[list[Path], list[dict]]:
    shutil.copytree(source_run / "images", run_dir / "images")
    sources = read_json(source_run / "sources.json", [])
    write_json(run_dir / "sources.json", sources)
    return _ordered_assets(run_dir, sources), sources


def _prepare_ancient_short_asset_pack(
    factory: ShortsFactory,
    topic: TopicCandidate,
) -> Path:
    """Build a 12-source union after one live fetch exhausted its providers.

    The original approved run contains three archive views not returned by the
    new fetch plus the trade-evidence diagram. Combined with the new fetch, the
    union contains ten distinct verified Wikimedia assets and two distinct
    evidence diagrams, with no fact-card fallbacks or within-video reuse.
    """
    completed = ANCIENT_SHORT_ASSET_PACK / "AUDITED_ASSET_PACK_COMPLETE.json"
    if completed.exists():
        return ANCIENT_SHORT_ASSET_PACK
    if not ANCIENT_SHORT_FETCH_RUN.exists():
        raise FileNotFoundError(ANCIENT_SHORT_FETCH_RUN)

    old_run = SOURCE_RUNS["ancient_short"]
    new_sources = read_json(ANCIENT_SHORT_FETCH_RUN / "sources.json", [])
    old_sources = read_json(old_run / "sources.json", [])
    if len(new_sources) < 8 or len(old_sources) < 6:
        raise RuntimeError("Great Zimbabwe source union is incomplete.")

    # New scenes 1-8 provide seven archive views plus the bird-evidence plate.
    # Old scenes 2, 3, 6 add distinct archive views; scene 4 adds the trade plate.
    selection = [
        *((ANCIENT_SHORT_FETCH_RUN, index) for index in range(1, 9)),
        (old_run, 2),
        (old_run, 3),
        (old_run, 6),
        (old_run, 4),
    ]
    image_dir = ensure_dir(ANCIENT_SHORT_ASSET_PACK / "images")
    raw_dir = ensure_dir(image_dir / "raw")
    frame_dir = ensure_dir(ANCIENT_SHORT_ASSET_PACK / "frame_check")
    scene_texts = factory.image_fetcher._scene_texts(topic, 12)
    manifest: list[dict] = []
    images: list[Path] = []
    for target_index, (source_run, source_index) in enumerate(selection, start=1):
        source_list = new_sources if source_run == ANCIENT_SHORT_FETCH_RUN else old_sources
        source = dict(source_list[source_index - 1])
        original_raw = source_run / "images" / "raw" / str(source.get("file") or "")
        if not original_raw.exists():
            raise FileNotFoundError(original_raw)
        raw_name = f"asset_{target_index:02d}_{original_raw.name}"
        target_raw = raw_dir / raw_name
        shutil.copy2(original_raw, target_raw)
        source["scene_index"] = target_index
        source["file"] = raw_name
        source["curated_union_origin"] = str(source_run.resolve())
        source["curated_union_original_scene"] = source_index
        out_path = image_dir / f"scene_{target_index:02d}.jpg"
        rendered = factory.image_fetcher._render_scene_frame(
            title=topic.title,
            text=scene_texts[target_index - 1],
            out_path=out_path,
            index=target_index - 1,
            total=12,
            backgrounds=[target_raw],
            topic=topic,
        )
        images.append(rendered)
        manifest.append(source)

    write_json(ANCIENT_SHORT_ASSET_PACK / "sources.json", manifest)
    source_qa = factory.image_fetcher._source_visual_qa(
        images,
        frame_dir / "contact_sheet.jpg",
        topic=topic,
        source_manifest=manifest,
    )
    factory._ensure_visual_quality(manifest)
    issue = factory._ancient_visual_preflight_issue(
        topic,
        manifest,
        "short",
        source_visual_qa=source_qa,
    )
    if issue:
        raise RuntimeError(f"Curated Great Zimbabwe asset preflight failed: {issue}")
    unique_urls = {str(item.get("url") or "").strip() for item in manifest}
    if len(unique_urls) != 12:
        raise RuntimeError(f"Great Zimbabwe asset union has only {len(unique_urls)} unique URLs.")
    if source_qa.get("near_duplicate_cross_source_pairs"):
        raise RuntimeError(
            "Great Zimbabwe asset union contains near-duplicate source frames: "
            f"{source_qa['near_duplicate_cross_source_pairs']}"
        )
    write_json(
        completed,
        {
            "source_gate": "pass",
            "unique_sources": 12,
            "verified_archive_sources": 10,
            "evidence_diagrams": 2,
            "fact_cards": 0,
            "near_duplicate_cross_source_pairs": [],
        },
    )
    return ANCIENT_SHORT_ASSET_PACK


def _ordered_assets(run_dir: Path, sources: list[dict]) -> list[Path]:
    images: list[Path] = []
    for index, source in enumerate(sources, start=1):
        source_file = Path(str(source.get("file") or ""))
        if source_file.suffix.lower() == ".mp4":
            candidate = run_dir / "images" / "raw" / source_file.name
        else:
            candidate = run_dir / "images" / f"scene_{index:02d}.jpg"
        if not candidate.exists():
            raw_candidate = run_dir / "images" / "raw" / source_file.name
            if raw_candidate.exists():
                candidate = raw_candidate
        if not candidate.exists():
            raise FileNotFoundError(f"Missing rebuilt visual asset: {candidate}")
        images.append(candidate)
    return images


def _copy_narration(source_run: Path, run_dir: Path) -> tuple[str, list[float]]:
    shutil.copy2(source_run / "narration.wav", run_dir / "narration.wav")
    segment_source = source_run / "narration_segments"
    if segment_source.exists():
        shutil.copytree(segment_source, run_dir / "narration_segments")
        manifest_path = run_dir / "narration_segments" / "kokoro_manifest.json"
        manifest = read_json(manifest_path, {})
        if isinstance(manifest, dict):
            for item in manifest.get("items", []):
                if isinstance(item, dict) and item.get("path"):
                    item["path"] = str(
                        (
                            run_dir
                            / "narration_segments"
                            / Path(str(item["path"])).name
                        ).resolve()
                    )
            write_json(manifest_path, manifest)

    metadata = read_json(source_run / "metadata.json", {})
    timeline = read_json(source_run / "visual_timeline.json", [])
    beat_bounds: dict[int, list[float]] = {}
    for item in timeline:
        try:
            index = int(item.get("beat_index"))
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        bounds = beat_bounds.setdefault(index, [start, end])
        bounds[0] = min(bounds[0], start)
        bounds[1] = max(bounds[1], end)
    durations = [
        max(0.1, beat_bounds[index][1] - beat_bounds[index][0])
        for index in sorted(beat_bounds)
    ]
    return str(metadata.get("voice") or "reused-audited-narration"), durations


def _reuse_exact_long_narration(
    factory: ShortsFactory,
    channel_id: str,
    topic: TopicCandidate,
    run_dir: Path,
) -> tuple[str, list[float]] | None:
    """Reuse a prior long narration only when every identity check passes.

    TTS text normalization can change punctuation inside the Kokoro manifest, so
    the authoritative comparison is the complete TopicCandidate saved beside the
    rendered narration.  The audio manifest and visual timeline are still checked
    for completeness before any files are copied.
    """

    if topic.content_kind != "video":
        return None

    desired_beats = list(topic.narration_beats or [topic.narration])
    if not desired_beats:
        return None
    channel = factory._channel(channel_id)
    engine = NarrationEngine(
        channel.voices,
        channel.voice_mode,
        getattr(channel, "tts_backend", ""),
    )
    if engine.backend_preference != "kokoro" or engine.http_tts_url:
        return None
    configured_voice = f"kokoro-{engine.kokoro_voice}"
    output_root = Path(channel.output_dir) / "video"
    if not output_root.exists():
        return None

    candidates = [path for path in output_root.glob("*/*") if path.is_dir()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for source_run in candidates:
        if source_run.resolve() == run_dir.resolve():
            continue
        source_topic = read_json(source_run / "topic.json", {})
        source_metadata = read_json(source_run / "metadata.json", {})
        if not isinstance(source_topic, dict) or not isinstance(source_metadata, dict):
            continue
        if str(source_topic.get("title") or "") != topic.title:
            continue
        if str(source_topic.get("subject") or "") != str(
            getattr(topic, "subject", "") or ""
        ):
            continue
        if str(source_topic.get("narration") or "") != topic.narration:
            continue
        if list(source_topic.get("narration_beats") or []) != desired_beats:
            continue

        voice = str(source_metadata.get("voice") or "").strip()
        if voice != configured_voice:
            continue
        narration_path = source_run / "narration.wav"
        segment_dir = source_run / "narration_segments"
        manifest = read_json(segment_dir / "kokoro_manifest.json", {})
        timeline = read_json(source_run / "visual_timeline.json", [])
        if not narration_path.exists() or narration_path.stat().st_size < 100_000:
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("engine") != "kokoro"
            or not isinstance(timeline, list)
        ):
            continue
        manifest_items = manifest.get("items")
        if not isinstance(manifest_items, list) or len(manifest_items) != len(desired_beats):
            continue
        manifest_complete = True
        for index, (item, desired_beat) in enumerate(zip(manifest_items, desired_beats)):
            if not isinstance(item, dict):
                manifest_complete = False
                break
            segment_path = segment_dir / Path(str(item.get("path") or "")).name
            declared_path = Path(str(item.get("path") or ""))
            try:
                item_speed = float(item.get("speed"))
            except (TypeError, ValueError):
                manifest_complete = False
                break
            if (
                not segment_path.exists()
                or segment_path.stat().st_size < 1_024
                or declared_path.resolve() != segment_path.resolve()
                or item.get("index") != index
                or str(item.get("text") or "") != engine._normalize_text(desired_beat)
                or str(item.get("voice") or "") != engine.kokoro_voice
                or abs(item_speed - engine.kokoro_speed) > 0.0001
            ):
                manifest_complete = False
                break
        if not manifest_complete:
            continue

        beat_bounds: dict[int, list[float]] = {}
        timeline_valid = True
        for item in timeline:
            try:
                index = int(item.get("beat_index"))
                start = float(item.get("start"))
                end = float(item.get("end"))
            except (AttributeError, TypeError, ValueError):
                timeline_valid = False
                break
            if index < 0 or end <= start:
                timeline_valid = False
                break
            bounds = beat_bounds.setdefault(index, [start, end])
            bounds[0] = min(bounds[0], start)
            bounds[1] = max(bounds[1], end)
        if not timeline_valid or sorted(beat_bounds) != list(range(len(desired_beats))):
            continue
        durations = [
            beat_bounds[index][1] - beat_bounds[index][0]
            for index in range(len(desired_beats))
        ]
        if any(duration <= 0 for duration in durations):
            continue
        try:
            narration_duration = float(factory._probe_audio_duration(narration_path))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if narration_duration <= 0 or abs(narration_duration - sum(durations)) > 0.25:
            continue

        copied_voice, copied_durations = _copy_narration(source_run, run_dir)
        if len(copied_durations) != len(desired_beats):
            raise RuntimeError("Exact narration cache copied an incomplete beat timeline.")
        write_json(
            run_dir / "REUSED_EXACT_NARRATION.json",
            {
                "source_run": str(source_run.resolve()),
                "identity_gate": "exact_topic_title_subject_narration_beats_voice_speed",
                "narration_sha256": hashlib.sha256(
                    topic.narration.encode("utf-8")
                ).hexdigest(),
                "beat_count": len(desired_beats),
                "voice": copied_voice,
            },
        )
        return copied_voice, copied_durations
    return None


def _synthesize(
    factory: ShortsFactory,
    channel_id: str,
    topic: TopicCandidate,
    run_dir: Path,
) -> tuple[str, list[float]]:
    channel = factory._channel(channel_id)
    narration_path = run_dir / "narration.wav"
    reused = _reuse_exact_long_narration(factory, channel_id, topic, run_dir)
    if reused is not None:
        voice, durations = reused
        print(
            f"Reused exact-content long narration ({len(durations)} beats, voice={voice}).",
            flush=True,
        )
        return voice, durations
    voice, durations = NarrationEngine(
        channel.voices,
        channel.voice_mode,
        getattr(channel, "tts_backend", ""),
    ).synthesize_beats(
        beats=topic.narration_beats or [topic.narration],
        out_dir=run_dir / "narration_segments",
        out_path=narration_path,
    )
    if topic.content_kind == "short":
        durations, _ = factory._normalize_short_narration_duration(
            channel_id,
            narration_path,
            durations,
        )
    return voice, durations


def _music_dir(source_run: Path, run_dir: Path) -> Path:
    selected_dir = ensure_dir(run_dir / "selected_music")
    old_metadata = read_json(source_run / "metadata.json", {})
    music_path = Path(str(old_metadata.get("music_file") or ""))
    if not music_path.exists():
        raise FileNotFoundError(f"Missing previously approved music track: {music_path}")
    shutil.copy2(music_path, selected_dir / music_path.name)
    return selected_dir


def _write_final_review(
    factory: ShortsFactory,
    channel_id: str,
    topic: TopicCandidate,
    run_dir: Path,
    images: list[Path],
    sources: list[dict],
    voice: str,
    duration: float,
    source_run: Path,
) -> dict:
    channel = factory._channel(channel_id)
    content_kind = topic.content_kind
    video_name = "short.mp4" if content_kind == "short" else "video.mp4"
    video_path = run_dir / video_name
    logo_path = factory._channel_logo_path(channel)

    thumb_maker = (
        ThumbnailMaker(width=1280, height=720)
        if content_kind == "video"
        else ThumbnailMaker(width=1080, height=1920)
    )
    thumbnail_path = run_dir / "thumbnail.jpg"
    thumbnail_lead = factory._thumbnail_lead_asset(channel, content_kind, images, sources)
    curated_thumbnail = run_dir / "images" / "scene_04.jpg"
    if content_kind == "short" and curated_thumbnail.exists() and (
        "late replies" in topic.title.lower()
        or "great zimbabwe" in topic.title.lower()
    ):
        thumbnail_lead = curated_thumbnail
    thumb_maker.generate(
        image_path=thumbnail_lead,
        title=topic.title,
        out_path=thumbnail_path,
        channel_id=channel.id,
        logo_path=logo_path,
    )
    thumbnail_issues = thumb_maker.thumbnail_copy_issues(topic.title, channel.id)

    ranked = [
        TitleVariant(
            pattern_id=topic.selected_title_pattern or "audited_rebuild",
            title=topic.title,
            predicted_score=1.0,
        )
    ]
    metadata = factory._metadata(channel, topic, ranked, content_kind)
    existing_metadata = read_json(run_dir / "metadata.json", {})
    audited_music_file = str(
        factory.video_builder.last_music_path
        or existing_metadata.get("music_file")
        or ""
    )
    metadata.update(
        {
            "voice": voice,
            "duration_seconds": round(duration, 3),
            "upload_eligible": False,
            "dry_run": True,
            "editorial_audit_rebuild": True,
            "rebuild_of": str(source_run.resolve()),
            "thumbnail_file": thumbnail_path.name,
            "thumbnail_lead_asset": str(thumbnail_lead.resolve()),
            "thumbnail_quality_issues": thumbnail_issues,
            "logo_file": str(logo_path.resolve()),
            "music_file": audited_music_file,
            "visual_asset_count": len({str(Path(path).resolve()) for path in images}),
            "visual_beat_assignment_count": len(images),
        }
    )

    media_report = factory.video_builder.validate_output(video_path, expected_duration=duration)
    visual_report = factory.image_fetcher.write_final_visual_qa(
        final_video=video_path,
        out_dir=run_dir / "frame_check",
        subtitles_path=run_dir / "subtitles.srt",
        timeline_path=run_dir / "visual_timeline.json",
    )
    visual_timeline = read_json(run_dir / "visual_timeline.json", [])
    physical_segments = 0
    previous_timeline_item: dict | None = None
    longest_contiguous_source_hold = 0.0
    current_source = ""
    current_source_start = 0.0
    current_source_end = 0.0
    for item in visual_timeline:
        if not isinstance(item, dict):
            continue
        item_source = str(item.get("source_file") or "")
        item_start = float(item.get("start") or 0.0)
        item_end = float(item.get("end") or 0.0)
        if (
            item_source
            and item_source == current_source
            and abs(item_start - current_source_end) <= 0.05
        ):
            current_source_end = max(current_source_end, item_end)
        else:
            longest_contiguous_source_hold = max(
                longest_contiguous_source_hold,
                current_source_end - current_source_start,
            )
            current_source = item_source
            current_source_start = item_start
            current_source_end = item_end
        continues_previous_hold = bool(
            previous_timeline_item
            and item.get("continuous_visual_hold")
            and previous_timeline_item.get("continuous_visual_hold")
            and str(item.get("source_file") or "")
            == str(previous_timeline_item.get("source_file") or "")
            and abs(
                float(item.get("start") or 0.0)
                - float(previous_timeline_item.get("end") or 0.0)
            ) <= 0.05
        )
        if not continues_previous_hold:
            physical_segments += 1
        previous_timeline_item = item
    longest_contiguous_source_hold = max(
        longest_contiguous_source_hold,
        current_source_end - current_source_start,
    )
    metadata["visual_timeline_segment_count"] = physical_segments
    metadata["maximum_contiguous_same_source_seconds"] = round(
        longest_contiguous_source_hold, 3
    )
    metadata["final_media_validation"] = media_report.to_dict()
    metadata["final_visual_qa"] = {
        key: value for key, value in visual_report.items() if key != "samples"
    }
    factory._append_video_chapters(
        metadata,
        topic,
        duration,
        visual_timeline=read_json(run_dir / "visual_timeline.json", []),
    )
    review = factory._quality_review(
        channel=channel,
        topic=topic,
        metadata=metadata,
        sources=sources,
        duration_seconds=duration,
        video_path=video_path,
    )
    if content_kind == "video" and longest_contiguous_source_hold > 8.5:
        hold_issue = (
            "Long-form visual plan keeps one source on screen for "
            f"{longest_contiguous_source_hold:.1f}s; maximum allowed is 8.5s"
        )
        review["issues"] = list(dict.fromkeys([*review.get("issues", []), hold_issue]))
        review["blocking_issues"] = list(
            dict.fromkeys([*review.get("blocking_issues", []), hold_issue])
        )
        review["decision"] = "hold"
        review.setdefault("subscores", {})["visuals"] = min(
            int(review.get("subscores", {}).get("visuals", 100)), 45
        )
        review["score"] = min(int(review.get("score", 0)), 69)
    if visual_report.get("status") != "pass":
        visual_issues = [
            f"Final render QA: {str(issue).strip()}"
            for issue in visual_report.get("issues", [])
            if str(issue).strip()
        ] or ["Final render QA did not pass"]
        review["issues"] = list(dict.fromkeys([*review.get("issues", []), *visual_issues]))
        review["blocking_issues"] = list(
            dict.fromkeys([*review.get("blocking_issues", []), *visual_issues])
        )
        review["decision"] = "hold"
        review.setdefault("subscores", {})["visuals"] = min(
            int(review.get("subscores", {}).get("visuals", 100)), 35
        )
        review["score"] = min(int(review.get("score", 0)), 69)

    review["final_media_validation"] = media_report.to_dict()
    review["final_visual_qa"] = {
        key: value for key, value in visual_report.items() if key != "samples"
    }
    metadata.update(factory.script_writer.provider_output_metadata(topic))
    metadata["ai_provider_attempts"] = list(factory.script_writer.provider_attempt_history)
    metadata["ai_provider_last_call_attempts"] = list(factory.script_writer.provider_attempts)
    metadata["quality_score"] = review["score"]
    metadata["quality_decision"] = review["decision"]
    metadata["quality_subscores"] = review.get("subscores", {})
    metadata["audit_completed_at"] = datetime.now().astimezone().isoformat()

    write_json(run_dir / "metadata.json", metadata)
    write_json(run_dir / "topic.json", asdict(topic))
    write_json(run_dir / "quality_review.json", review)

    if not media_report.ok:
        raise RuntimeError(f"Final media validation failed: {media_report.to_dict()}")
    if visual_report.get("status") != "pass":
        raise RuntimeError(f"Final visual validation failed: {visual_report.get('issues')}")
    if review.get("decision") != "pass":
        raise RuntimeError(f"Editorial quality gate held output: {review.get('issues')}")
    return {
        "channel_id": channel_id,
        "content_kind": content_kind,
        "title": topic.title,
        "run_dir": str(run_dir.resolve()),
        "video_path": str(video_path.resolve()),
        "duration_seconds": round(duration, 3),
        "voice": voice,
        "visual_asset_count": len({str(Path(path).resolve()) for path in images}),
        "visual_beat_assignment_count": len(images),
        "visual_timeline_segment_count": physical_segments,
        "maximum_contiguous_same_source_seconds": round(
            longest_contiguous_source_hold, 3
        ),
        "quality_score": review["score"],
        "quality_decision": review["decision"],
        "media_validation": media_report.to_dict(),
        "visual_validation": {
            key: value for key, value in visual_report.items() if key != "samples"
        },
    }


def rebuild(factory: ShortsFactory, key: str) -> dict:
    factory.script_writer.reset_provider_audit()
    source_run = SOURCE_RUNS[key]
    if not source_run.exists():
        raise FileNotFoundError(source_run)
    source_channel_id = "brain_lens" if key.startswith("brain") else "ancient_history"
    old_topic = factory._topic_from_run_data(
        source_channel_id,
        read_json(source_run / "topic.json", {}),
        read_json(source_run / "metadata.json", {}),
    )
    if key == "ancient_short":
        channel_id, topic = "ancient_history", _great_zimbabwe_topic(old_topic)
    elif key == "brain_short":
        channel_id, topic = "brain_lens", _reply_time_topic(old_topic)
    elif key == "brain_long":
        channel_id, topic = "brain_lens", _brain_long_topic(factory, old_topic)
    elif key == "ancient_long":
        channel_id, topic = "ancient_history", _ancient_long_topic(factory, old_topic)
    else:
        raise ValueError(key)

    channel = factory._channel(channel_id)
    profile = factory._content_profile(channel, topic.content_kind)
    run_dir = _fresh_run_dir(factory, channel_id, topic.content_kind)
    write_json(
        run_dir / "AUDIT_REBUILD_IN_PROGRESS.json",
        {
            "source_run": str(source_run.resolve()),
            "key": key,
            "upload": False,
            "dry_run": True,
        },
    )

    if key == "brain_short" and AUDITED_BRAIN_SHORT_ASSET_RUN.exists():
        images, sources = _copy_reused_assets(AUDITED_BRAIN_SHORT_ASSET_RUN, run_dir)
        factory.image_fetcher._source_visual_qa(
            images,
            ensure_dir(run_dir / "frame_check") / "contact_sheet.jpg",
            topic=topic,
            source_manifest=sources,
        )
        write_json(
            run_dir / "AUDITED_ASSET_SOURCE.json",
            {
                "source_run": str(AUDITED_BRAIN_SHORT_ASSET_RUN.resolve()),
                "source_gate": "pass",
                "prior_video_encoded": False,
            },
        )
        semantic_order = [1, 12, 9, 6, 3, 4, 11, 5, 7, 8, 12, 6, 2, 10]
        images = [images[index - 1] for index in semantic_order]
        write_json(
            run_dir / "VISUAL_SEMANTIC_ORDER.json",
            {
                "beat_count": len(semantic_order),
                "unique_asset_count": len(set(semantic_order)),
                "source_asset_order": semantic_order,
                "spaced_motion_reuse": [12, 6],
            },
        )
    elif key == "ancient_short":
        asset_pack = _prepare_ancient_short_asset_pack(factory, topic)
        images, sources = _copy_reused_assets(asset_pack, run_dir)
        source_qa = factory.image_fetcher._source_visual_qa(
            images,
            ensure_dir(run_dir / "frame_check") / "contact_sheet.jpg",
            topic=topic,
            source_manifest=sources,
        )
        write_json(
            run_dir / "AUDITED_ASSET_SOURCE.json",
            {
                "source_run": str(asset_pack.resolve()),
                "source_gate": "pass",
                "unique_sources": 12,
                "near_duplicate_cross_source_pairs": source_qa.get(
                    "near_duplicate_cross_source_pairs", []
                ),
            },
        )
        # Fourteen sentence-level beats use twelve verified sources. The trade
        # and bird diagrams each support two consecutive claims, so duplicate
        # paths are intentional; VideoBuilder merges each adjacent pair into a
        # single continuous hold instead of restarting the still animation.
        semantic_order = [3, 9, 4, 11, 2, 10, 8, 1, 12, 12, 5, 5, 6, 7]
        images = [images[index - 1] for index in semantic_order]
        write_json(
            run_dir / "VISUAL_SEMANTIC_ORDER.json",
            {
                "beat_count": len(semantic_order),
                "unique_asset_count": len(set(semantic_order)),
                "source_asset_order": semantic_order,
                "continuous_hold_pairs": [[9, 10], [11, 12]],
            },
        )
    else:
        images, sources = _copy_reused_assets(source_run, run_dir)
        factory.image_fetcher._source_visual_qa(
            images,
            ensure_dir(run_dir / "frame_check") / "contact_sheet.jpg",
            topic=topic,
            source_manifest=sources,
        )

    factory._ensure_visual_quality(sources)
    if channel_id == "ancient_history":
        issue = factory._ancient_visual_preflight_issue(
            topic,
            sources,
            topic.content_kind,
            source_visual_qa=read_json(run_dir / "frame_check" / "source_visual_qa.json", {}),
        )
        if issue:
            raise RuntimeError(f"Ancient visual preflight failed: {issue}")
    else:
        issue = factory._brain_visual_preflight_issue(topic, sources)
        if issue:
            raise RuntimeError(f"Brain visual preflight failed: {issue}")

    voice, durations = _synthesize(factory, channel_id, topic, run_dir)

    video_path = run_dir / ("short.mp4" if topic.content_kind == "short" else "video.mp4")
    duration = factory.video_builder.build(
        image_paths=images,
        narration_path=run_dir / "narration.wav",
        music_dir=_music_dir(source_run, run_dir),
        out_path=video_path,
        narration_text=topic.narration,
        subtitles_path=run_dir / "subtitles.srt",
        title_text=topic.title,
        visual_captions=topic.visual_captions,
        narration_beats=topic.narration_beats or [topic.narration],
        scene_durations=durations,
        target_size=profile.target_size,
        duration_bounds=(profile.min_duration_seconds, profile.max_duration_seconds),
        logo_path=factory._channel_logo_path(channel),
        content_kind=topic.content_kind,
    )
    result = _write_final_review(
        factory,
        channel_id,
        topic,
        run_dir,
        images,
        sources,
        voice,
        duration,
        source_run,
    )
    progress_path = run_dir / "AUDIT_REBUILD_IN_PROGRESS.json"
    if progress_path.exists():
        progress_path.rename(run_dir / "AUDIT_REBUILD_COMPLETE.json")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the four human-audited dry-run outputs with production components."
    )
    parser.add_argument(
        "--only",
        choices=[*SOURCE_RUNS, "all"],
        default="all",
        help="Limit the rebuild to one audited output.",
    )
    args = parser.parse_args()
    keys = list(SOURCE_RUNS) if args.only == "all" else [args.only]
    factory = ShortsFactory(ROOT / "config/settings.yaml")
    results = []
    for key in keys:
        print(f"AUDIT_REBUILD_START {key}", flush=True)
        result = rebuild(factory, key)
        results.append(result)
        print(f"AUDIT_REBUILD_PASS {json.dumps(result, ensure_ascii=False)}", flush=True)
    manifest = ROOT / "output" / "editorial_audit_rebuild_manifest.json"
    existing = read_json(manifest, [])
    if not isinstance(existing, list):
        existing = []
    existing.extend(results)
    write_json(manifest, existing)
    print(f"AUDIT_REBUILD_MANIFEST {manifest.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
