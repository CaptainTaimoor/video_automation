from __future__ import annotations

import argparse
import math
import re
import struct
import sys
import wave
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yt_auto.pipeline import ShortsFactory
from yt_auto.thumbnailer import ThumbnailMaker
from yt_auto.video_builder import VideoBuilder


BANNED_TITLE_BITS = (
    "quietly steals your attention",
    "brain trick shaping your next reaction",
    "brain trick that skews your choices",
    "how this brain trick skews your choices",
    "running your reaction",
)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def _failures_for_topic(factory: ShortsFactory, channel, topic, kind: str, enforce_recent: bool) -> list[str]:
    failures: list[str] = []
    title = topic.title or ""
    title_lower = title.lower()
    words = _word_count(topic.narration)
    beats = len(topic.narration_beats or [])
    scenes = len(topic.scene_plan or [])
    if any(bit in title_lower for bit in BANNED_TITLE_BITS):
        failures.append(f"banned title formula: {title}")
    if kind == "short" and channel.id == "brain_lens" and any(
        bit in title_lower
        for bit in (
            "reward loop your brain keeps choosing",
            "pattern people read before you speak",
            "loop your brain keeps rewarding",
            "feels urgent when nothing is wrong",
        )
    ):
        failures.append(f"banned Brain Lens repeated formula: {title}")
    if title.startswith(("The real story", "The true story", "The psychology behind")):
        failures.append(f"generic title start: {title}")
    if kind == "short":
        if not (65 <= words <= 220):
            failures.append(f"short word count outside 65-220: {words}")
        if scenes < 5:
            failures.append(f"short has too few scenes: {scenes}")
    else:
        if not (850 <= words <= 1700):
            failures.append(f"long word count outside 850-1700: {words}")
        if scenes < 12:
            failures.append(f"long has too few scenes: {scenes}")
        if beats < 12:
            failures.append(f"long has too few narration beats: {beats}")
        first_three = list(topic.narration_beats or [])[:3]
        if len(first_three) < 3:
            failures.append("long is missing 3-part cold open")
        if first_three and _word_count(first_three[0]) > 22:
            failures.append(f"long first beat too slow: {_word_count(first_three[0])} words")
        if sum(_word_count(beat) for beat in first_three) > 70:
            failures.append("long cold open is too wordy")
        first_captions = [getattr(scene, "visual_text", "") for scene in (topic.scene_plan or [])[:3]]
        required_captions = {"start here", "the real stakes", "what changes"}
        if {caption.lower().strip() for caption in first_captions} != required_captions:
            failures.append(f"long cold open captions wrong: {first_captions}")
    if channel.id == "brain_lens":
        opener = (topic.narration_beats or [topic.hook or ""])[0].lower().strip()
        concrete_starts = ("you ", "your ", "notice ", "watch ", "the moment ", "that second ")
        concrete_cues = ("phone", "text", "reply", "thumb", "scroll", "chest", "smile", "eye contact", "reread", "avoid", "pause", "room")
        if not (opener.startswith(concrete_starts) or any(cue in opener for cue in concrete_cues)):
            failures.append("Brain Lens opener is not behavior-first")
    title_issue = factory._backlog_title_quality_issue(topic.title)
    if title_issue:
        failures.append(f"title quality issue: {title_issue}")
    hook_issue = factory._opening_hook_issue(topic)
    if hook_issue:
        failures.append(f"hook quality issue: {hook_issue}")
    script_issues = factory._script_quality_issues(channel, topic)
    if script_issues:
        failures.append(f"script quality issue: {script_issues[0]}")
    if enforce_recent:
        try:
            factory._validate_topic_quality(channel, topic, factory._recent_titles(channel.id))
        except Exception as exc:
            failures.append(f"pipeline quality gate rejected topic: {exc}")
    return failures


def _generate_passing_topic(
    factory: ShortsFactory,
    channel,
    kind: str,
    attempts: int,
    enforce_recent: bool,
    extra_avoid_titles: set[str] | None = None,
):
    avoid_titles = factory._recent_titles(channel.id) if enforce_recent else set()
    avoid_titles.update(extra_avoid_titles or set())
    style_bias = factory.feedback.style_bias(channel.id, channel.styles) if factory.config.app.feedback_enabled else None
    term_bias = factory.feedback.term_bias(channel.id) if factory.config.app.feedback_enabled else None
    errors: list[str] = []
    for _ in range(attempts):
        candidate = factory.topic_planner.plan(
            channel,
            style_bias=style_bias,
            term_bias=term_bias,
            avoid_titles=avoid_titles,
            content_kind=kind,
        )
        candidate = factory.script_writer.improve(
            channel=channel,
            topic=candidate,
            content_kind=kind,
            avoid_titles=avoid_titles,
        )
        candidate, _variants = factory._rank_and_apply_title(channel, candidate)
        failures = _failures_for_topic(factory, channel, candidate, kind, enforce_recent=enforce_recent)
        if not failures:
            return candidate
        errors.extend(failures[:2])
        avoid_titles.add((candidate.title or "").lower())
        avoid_titles.add((candidate.subject or "").lower())
        fingerprint = factory._story_fingerprint(candidate.title, candidate.subject, " ".join(candidate.trend_terms[:4]))
        if fingerprint:
            avoid_titles.add(fingerprint.lower())
    raise AssertionError("; ".join(errors[-6:]) or f"no passing {kind} topic for {channel.id}")


def _metadata_failures(factory: ShortsFactory, channel, topic, kind: str) -> list[str]:
    failures: list[str] = []
    topic, variants = factory._rank_and_apply_title(channel, topic)
    metadata = factory._metadata(channel, topic, variants, kind)
    if int(metadata.get("seo_score") or 0) < 70:
        failures.append(f"SEO score below 70: {metadata.get('seo_score')}")
    if kind == "video":
        metadata["duration_seconds"] = 540
        factory._append_video_chapters(metadata, topic, 540)
        description = str(metadata.get("description") or "")
        chapter_lines = [line for line in description.splitlines() if re.match(r"^\d+:\d{2}\s+\S", line)]
        if "0:00 " not in description:
            failures.append("long video description missing 0:00 chapter")
        if len(chapter_lines) < 3:
            failures.append(f"long video has too few chapters: {len(chapter_lines)}")
    return failures


def _logo_and_thumbnail_failures(factory: ShortsFactory, channel) -> list[str]:
    failures: list[str] = []
    logo_path = factory._channel_logo_path(channel)
    if not logo_path.exists() or logo_path.stat().st_size < 2_000:
        failures.append(f"logo missing/small: {logo_path}")
    tmp_dir = ROOT / "tmp_force_quality"
    tmp_dir.mkdir(exist_ok=True)
    image_path = tmp_dir / f"{channel.id}_thumb_source.jpg"
    Image.new("RGB", (1280, 720), (24, 32, 48)).save(image_path)
    for kind, size in (("short", (1080, 1920)), ("video", (1280, 720))):
        out = tmp_dir / f"{channel.id}_{kind}_thumbnail.jpg"
        ThumbnailMaker(width=size[0], height=size[1]).generate(
            image_path=image_path,
            title="Quality Test Thumbnail",
            out_path=out,
            channel_id=channel.id,
            logo_path=logo_path,
        )
        with Image.open(out) as img:
            if img.size != size:
                failures.append(f"{kind} thumbnail size wrong: {img.size}")
    return failures


def _render_smoke(factory: ShortsFactory, channel) -> list[str]:
    failures: list[str] = []
    tmp_dir = ROOT / "tmp_force_quality"
    tmp_dir.mkdir(exist_ok=True)
    image_path = tmp_dir / "render_scene.jpg"
    Image.new("RGB", (1280, 720), (18, 28, 44)).save(image_path)
    wav_path = tmp_dir / "render_narration.wav"
    framerate = 44100
    seconds = 2
    with wave.open(str(wav_path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        for index in range(framerate * seconds):
            value = int(10000 * math.sin(2 * math.pi * 220 * index / framerate))
            handle.writeframes(struct.pack("<h", value))
    out_path = tmp_dir / "render_smoke.mp4"
    VideoBuilder(1, 5, subtitle_enabled=True, target_size=(1280, 720)).build(
        image_paths=[image_path],
        narration_path=wav_path,
        music_dir=ROOT / "assets" / "music",
        out_path=out_path,
        narration_text="This is a force quality render smoke test.",
        subtitles_path=tmp_dir / "render_smoke.srt",
        title_text="Render Smoke",
        narration_beats=["This is a force quality render smoke test."],
        scene_durations=[2.0],
        target_size=(1280, 720),
        duration_bounds=(1, 5),
        logo_path=factory._channel_logo_path(channel),
        content_kind="video",
    )
    if not out_path.exists() or out_path.stat().st_size < 30_000:
        failures.append("render smoke output missing or too small")
    return failures


def _duplicate_gate_failures(factory: ShortsFactory) -> list[str]:
    failures: list[str] = []
    pairs = [
        ("Rome's Worst Defeat: Teutoburg Forest's Lost Legions", "The moment Battle Of Teutoburg Forest became impossible to ignore"),
        ("Rome Sacked 410 AD: Barbarians' Shocking Loot Reveals Empire's Fall", "3 details about Sack Of Rome (410) that change the whole story"),
        ("Analysis Paralysis: How This Brain Trick Skews Your Choices", "How Analysis Paralysis quietly steals your attention"),
    ]
    for left, right in pairs:
        left_fp = factory._story_fingerprint(left)
        right_fp = factory._story_fingerprint(right)
        if left_fp != right_fp:
            failures.append(f"duplicate alias mismatch: {left_fp!r} != {right_fp!r}")
    return failures


def run(
    samples: int,
    attempts: int,
    render_smoke: bool,
    provider: str,
    enforce_recent: bool,
    channels: list[str],
    kinds: list[str],
    provider_timeout: int,
) -> int:
    load_dotenv(ROOT / ".env")
    factory = ShortsFactory(ROOT / "config" / "settings.yaml")
    factory.config.app.script_writer.timeout_seconds = provider_timeout
    factory.script_writer.cfg.timeout_seconds = provider_timeout
    if provider != "current":
        factory.config.app.script_writer.provider = provider
        factory.script_writer.cfg.provider = provider
    failures: list[str] = []
    generated_fingerprints: set[str] = set()
    generated_titles: set[str] = set()
    generated_avoid_titles: set[str] = set()
    failures.extend(_duplicate_gate_failures(factory))

    selected_channels = [channel for channel in factory.config.channels if not channels or channel.id in channels]
    for channel in selected_channels:
        print(f"\nCHANNEL {channel.id}", flush=True)
        failures.extend(f"{channel.id}: {item}" for item in _logo_and_thumbnail_failures(factory, channel))
        for kind in kinds:
            for sample in range(1, samples + 1):
                topic = _generate_passing_topic(
                    factory,
                    channel,
                    kind,
                    attempts=attempts,
                    enforce_recent=enforce_recent,
                    extra_avoid_titles=generated_avoid_titles,
                )
                fingerprint = factory._story_fingerprint(topic.title, topic.subject, " ".join(topic.trend_terms[:4]))
                test_key = f"{channel.id}:{kind}:{fingerprint}"
                if test_key in generated_fingerprints:
                    failures.append(f"{channel.id} {kind}: duplicate generated story fingerprint {fingerprint}")
                generated_fingerprints.add(test_key)
                title_key = f"{channel.id}:{kind}:{(topic.title or '').lower().strip()}"
                if title_key in generated_titles:
                    failures.append(f"{channel.id} {kind}: duplicate generated title {topic.title}")
                generated_titles.add(title_key)
                generated_avoid_titles.add((topic.title or "").lower().strip())
                generated_avoid_titles.add((topic.subject or "").lower().strip())
                if fingerprint:
                    generated_avoid_titles.add(fingerprint.lower())
                meta_failures = _metadata_failures(factory, channel, topic, kind)
                failures.extend(f"{channel.id} {kind}: {item}" for item in meta_failures)
                print(
                    f"  PASS {kind} sample {sample}: words={_word_count(topic.narration)} "
                    f"scenes={len(topic.scene_plan)} title={topic.title[:72]}"
                    , flush=True
                )
        if render_smoke:
            failures.extend(f"{channel.id}: {item}" for item in _render_smoke(factory, channel))

    if failures:
        print("\nFAILURES")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nALL FORCE QUALITY TESTS PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Force-test topic, script, SEO, logo, chapter, and render smoke quality.")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--render-smoke", action="store_true")
    parser.add_argument("--provider", choices=["current", "template", "gemini", "ollama"], default="template")
    parser.add_argument("--provider-timeout", type=int, default=45)
    parser.add_argument("--enforce-recent", action="store_true", help="Also fail generated topics that collide with recent uploads.")
    parser.add_argument("--channel", action="append", default=[])
    parser.add_argument("--kind", action="append", choices=["short", "video"], default=[])
    args = parser.parse_args()
    return run(
        samples=max(1, args.samples),
        attempts=max(1, args.attempts),
        render_smoke=args.render_smoke,
        provider=args.provider,
        enforce_recent=args.enforce_recent,
        channels=args.channel,
        kinds=args.kind or ["short", "video"],
        provider_timeout=max(10, args.provider_timeout),
    )


if __name__ == "__main__":
    raise SystemExit(main())
