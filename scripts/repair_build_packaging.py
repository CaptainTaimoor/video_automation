from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yt_auto.pipeline import ShortsFactory
from yt_auto.seo import build_youtube_metadata
from yt_auto.thumbnailer import ThumbnailMaker
from yt_auto.utils import read_json, write_json


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh title, SEO metadata, chapters, thumbnail, and deterministic QA for an existing build."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--pattern", default="manual_packaging_repair")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    output_root = (ROOT / "output").resolve()
    if not _inside(run_dir, output_root):
        raise SystemExit(f"Refusing to modify a path outside {output_root}: {run_dir}")
    if not (run_dir / "video.mp4").exists() and not (run_dir / "short.mp4").exists():
        raise SystemExit(f"No rendered video found in {run_dir}")

    factory = ShortsFactory(ROOT / "config" / "settings.yaml")
    channel = next((item for item in factory.config.channels if item.id == args.channel), None)
    if channel is None:
        raise SystemExit(f"Unknown channel: {args.channel}")

    topic_path = run_dir / "topic.json"
    metadata_path = run_dir / "metadata.json"
    quality_path = run_dir / "quality_review.json"
    topic_data = read_json(topic_path, {})
    old_metadata = read_json(metadata_path, {})
    old_quality = read_json(quality_path, {})
    sources = read_json(run_dir / "sources.json", [])
    if not isinstance(topic_data, dict) or not isinstance(old_metadata, dict):
        raise SystemExit("The build is missing valid topic or metadata JSON")

    topic = factory._topic_from_run_data(args.channel, topic_data, old_metadata)
    variants = factory.title_lab.make_variants(topic, count=5)
    topic = replace(
        topic,
        title=args.title.strip(),
        selected_title_pattern=args.pattern,
        title_variants=[item.title for item in variants],
    )
    variants = factory.title_lab.make_variants(topic, count=5)

    refreshed = build_youtube_metadata(channel, topic, variants, topic.content_kind)
    # Preserve render/provenance fields while replacing every SEO-derived field.
    metadata = {**old_metadata, **refreshed}
    metadata["title"] = topic.title
    metadata["selected_title_pattern"] = args.pattern
    metadata["thumbnail_file"] = "thumbnail.jpg"

    first_source_file = ""
    if isinstance(sources, list):
        first_source_file = next(
            (
                str(item.get("file") or "")
                for item in sources
                if isinstance(item, dict) and str(item.get("file") or "")
            ),
            "",
        )
    lead_asset = run_dir / "images" / "raw" / first_source_file
    if not lead_asset.exists():
        candidates = sorted((run_dir / "images" / "raw").glob("raw_01_*"))
        if not candidates:
            raise SystemExit("Cannot find the lead visual needed to regenerate the thumbnail")
        lead_asset = candidates[0]

    logo_value = str(old_metadata.get("logo_file") or "")
    logo_path = Path(logo_value)
    if not logo_path.is_absolute():
        logo_path = (ROOT / logo_path).resolve()
    if not logo_path.exists():
        logo_path = (ROOT / "assets" / "branding" / f"{args.channel}_logo.png").resolve()

    landscape = topic.content_kind == "video"
    thumbnail = ThumbnailMaker(width=1280 if landscape else 1080, height=720 if landscape else 1920)
    thumbnail.generate(
        image_path=lead_asset,
        title=topic.title,
        out_path=run_dir / "thumbnail.jpg",
        channel_id=args.channel,
        logo_path=logo_path,
    )
    metadata["thumbnail_quality_issues"] = thumbnail.thumbnail_copy_issues(
        topic.title,
        args.channel,
    )

    duration = float(metadata.get("duration_seconds") or 0.0)
    factory._append_video_chapters(
        metadata,
        topic,
        duration,
        visual_timeline=read_json(run_dir / "visual_timeline.json", []),
    )
    metadata["packaging_repairs"] = [
        *list(old_metadata.get("packaging_repairs") or []),
        {
            "reason": "manual acceptance review found generic title, incomplete thumbnail phrase, and mismatched chapter labels",
            "old_title": str(old_metadata.get("title") or topic_data.get("title") or ""),
            "new_title": topic.title,
            "chapter_count": len(metadata.get("chapters") or []),
        },
    ]

    # Keep the prior AI advisory as an audit trail, but make the acceptance score
    # deterministic so this repair never consumes another provider quota.
    prior_ai_review = old_quality.get("ai_review") if isinstance(old_quality, dict) else None
    factory.script_writer.review_content_quality = lambda *_args, **_kwargs: None
    review = factory._quality_review(
        channel=channel,
        topic=topic,
        metadata=metadata,
        sources=sources if isinstance(sources, list) else [],
        duration_seconds=duration,
        video_path=run_dir / ("video.mp4" if landscape else "short.mp4"),
    )
    if prior_ai_review:
        review["prior_ai_advisory"] = prior_ai_review
    review["packaging_review"] = {
        "title": topic.title,
        "thumbnail_copy": thumbnail._display_title(topic.title, args.channel),
        "chapter_count": len(metadata.get("chapters") or []),
        "chapter_source": "actual visual timeline beat starts",
    }

    write_json(topic_path, asdict(topic))
    write_json(metadata_path, metadata)
    write_json(quality_path, review)
    print(f"Packaging refreshed: {run_dir}")
    print(f"Title: {topic.title}")
    print(f"Chapters: {len(metadata.get('chapters') or [])}")
    print(f"Quality decision: {review.get('decision')} ({review.get('score')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
