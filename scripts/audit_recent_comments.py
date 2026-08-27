from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yt_auto.config import load_config
from yt_auto.uploaders import YouTubeUploader
from yt_auto.utils import ensure_dir, write_json


CATEGORIES: dict[str, list[str]] = {
    "audio_voice": [
        "audio",
        "voice",
        "sound",
        "pronounce",
        "pronunciation",
        "accent",
        "narrator",
        "robot",
        "tts",
        "garbage ai voice",
        "c-a-r",
    ],
    "caption_text": [
        "caption",
        "subtitle",
        "text",
        "spelling",
        "typo",
        "grammar",
        "word",
        "read",
        "garbage text",
        "c-a-r",
    ],
    "factual_accuracy": [
        "wrong",
        "incorrect",
        "fake",
        "not true",
        "lie",
        "source",
        "thermopile",
        "thermopylae",
        "spartan",
        "actually",
        "built by",
        "amazigh",
        "berber",
        "bereber",
    ],
    "visual_mismatch": [
        "image",
        "picture",
        "visual",
        "stock",
        "ai image",
        "doesn't match",
        "not related",
    ],
    "low_quality_ai": [
        "ai",
        "bot",
        "garbage",
        "trash",
        "lazy",
        "low quality",
        "lol",
        "lmao",
        "wtf",
    ],
    "positive": [
        "good",
        "great",
        "nice",
        "love",
        "amazing",
        "interesting",
        "thanks",
        "wow",
    ],
}

NEGATIVE_CATEGORIES = {"audio_voice", "caption_text", "factual_accuracy", "visual_mismatch", "low_quality_ai"}


def _contains(text: str, needle: str) -> bool:
    lowered = text.lower()
    if " " in needle or "'" in needle:
        return needle in lowered
    return re.search(rf"\b{re.escape(needle)}\b", lowered) is not None


def classify_comment(text: str) -> list[str]:
    matches = []
    for category, needles in CATEGORIES.items():
        if any(_contains(text, needle) for needle in needles):
            matches.append(category)
    return matches


def _short(text: str, limit: int = 160) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _dedupe_comments(comments: Iterable[dict]) -> list[dict]:
    seen = set()
    out = []
    for comment in comments:
        key = comment.get("comment_id") or (comment.get("video_id"), comment.get("text"))
        if key in seen:
            continue
        seen.add(key)
        out.append(comment)
    return out


def fetch_comments_with_ytdlp(video_id: str, cache_dir: Path, max_comments: int) -> list[dict]:
    ensure_dir(cache_dir)
    yt_dlp = ROOT / ".venv" / "Scripts" / "yt-dlp.exe"
    executable = str(yt_dlp) if yt_dlp.exists() else "yt-dlp"
    url = f"https://www.youtube.com/watch?v={video_id}"
    output = str(cache_dir / "%(id)s.%(ext)s")
    subprocess.run(
        [
            executable,
            "--skip-download",
            "--write-info-json",
            "--write-comments",
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "-o",
            output,
            url,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    info_path = cache_dir / f"{video_id}.info.json"
    if not info_path.exists():
        return []
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    comments = []
    for raw in data.get("comments", [])[:max_comments]:
        text = raw.get("text") or ""
        if not text:
            continue
        comments.append(
            {
                "comment_id": raw.get("id", ""),
                "video_id": video_id,
                "author": raw.get("author", ""),
                "text": text,
                "like_count": int(raw.get("like_count") or 0),
                "published_at": raw.get("timestamp") or "",
                "updated_at": "",
                "reply_count": 0,
                "source": "yt-dlp",
            }
        )
    return comments


def fetch_video_comments(
    uploader: YouTubeUploader,
    channel,
    video: dict,
    state_dir: Path,
    max_comments: int,
) -> tuple[list[dict], str]:
    if int(video.get("comments", 0) or 0) <= 0:
        return [], "skipped_no_public_comments"
    try:
        recent_comments = uploader.list_video_comments(
            channel,
            video["video_id"],
            max_results=max_comments,
            order="time",
        )
        relevant_comments = uploader.list_video_comments(
            channel,
            video["video_id"],
            max_results=min(max_comments, 20),
            order="relevance",
        )
        return _dedupe_comments([*recent_comments, *relevant_comments]), "youtube_api"
    except Exception:
        comments = fetch_comments_with_ytdlp(
            video["video_id"],
            state_dir / "comment_audit_ytdlp_cache",
            max_comments,
        )
        return _dedupe_comments(comments), "yt-dlp_fallback"


def recommended_actions(category_counts: Counter) -> list[str]:
    actions = []
    if category_counts["audio_voice"]:
        actions.append("Slow voice slightly, add pronunciation overrides, and reject scripts with hard names unless pronunciation is known.")
    if category_counts["caption_text"]:
        actions.append("Keep captions to phrase chunks, never split named entities, and run text-lint before rendering.")
    if category_counts["factual_accuracy"]:
        actions.append("Gate history scripts with a fact-name check and avoid ambiguous ancient names without source terms.")
    if category_counts["visual_mismatch"]:
        actions.append("Tighten image queries around the exact subject, era, place, and object instead of generic stock scenes.")
    if category_counts["low_quality_ai"]:
        actions.append("Increase script variety and reduce repeated formula titles/openers.")
    if not actions:
        actions.append("No strong complaint cluster found; keep monitoring comments and optimize from retention/views.")
    return actions


def main() -> int:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Audit recent YouTube comments for quality complaints.")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--channel", action="append", help="Channel id. Repeat or omit for all YouTube-enabled channels.")
    parser.add_argument("--max-videos", type=int, default=30)
    parser.add_argument("--max-comments", type=int, default=30)
    args = parser.parse_args()

    config = load_config(Path(args.config))
    wanted = set(args.channel or [])
    channels = [
        channel
        for channel in config.channels
        if channel.youtube and channel.youtube.upload_enabled and (not wanted or channel.id in wanted)
    ]
    if not channels:
        print("No matching YouTube-enabled channels.")
        return 1

    uploader = YouTubeUploader()
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "max_videos_per_channel": args.max_videos,
        "max_comments_per_video": args.max_comments,
        "channels": {},
        "summary": {},
    }
    overall_categories: Counter = Counter()
    overall_comments = 0
    overall_videos = 0

    for channel in channels:
        videos = uploader.list_recent_videos(channel, max_results=args.max_videos)
        channel_categories: Counter = Counter()
        channel_examples: dict[str, list[dict]] = defaultdict(list)
        video_rows = []

        for video in videos:
            comments, comment_source = fetch_video_comments(
                uploader,
                channel,
                video,
                config.app.state_dir,
                args.max_comments,
            )
            category_counts: Counter = Counter()
            analyzed_comments = []

            for comment in comments:
                categories = classify_comment(comment.get("text", ""))
                for category in categories:
                    category_counts[category] += 1
                    channel_categories[category] += 1
                    if len(channel_examples[category]) < 8:
                        channel_examples[category].append(
                            {
                                "video_id": video["video_id"],
                                "title": video["title"],
                                "text": _short(comment.get("text", "")),
                                "like_count": comment.get("like_count", 0),
                                "published_at": comment.get("published_at", ""),
                            }
                        )
                comment_copy = dict(comment)
                comment_copy["categories"] = categories
                analyzed_comments.append(comment_copy)

            negative_count = sum(category_counts[category] for category in NEGATIVE_CATEGORIES)
            video_rows.append(
                {
                    **video,
                    "fetched_comments": len(comments),
                    "comment_source": comment_source,
                    "complaint_count": negative_count,
                    "category_counts": dict(category_counts),
                    "sample_comments": analyzed_comments[:12],
                }
            )

        video_rows.sort(key=lambda row: (row["complaint_count"], row.get("comments", 0), row.get("views", 0)), reverse=True)
        report["channels"][channel.id] = {
            "display_name": channel.display_name,
            "videos_checked": len(videos),
            "comments_checked": sum(row["fetched_comments"] for row in video_rows),
            "category_counts": dict(channel_categories),
            "problem_videos": video_rows[:10],
            "examples": dict(channel_examples),
            "recommended_actions": recommended_actions(channel_categories),
        }
        overall_videos += len(videos)
        overall_comments += sum(row["fetched_comments"] for row in video_rows)
        overall_categories.update(channel_categories)

    report["summary"] = {
        "videos_checked": overall_videos,
        "comments_checked": overall_comments,
        "category_counts": dict(overall_categories),
        "recommended_actions": recommended_actions(overall_categories),
    }

    state_dir = ensure_dir(config.app.state_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_path = state_dir / "comment_audit_latest.json"
    stamped_path = state_dir / f"comment_audit_{stamp}.json"
    write_json(latest_path, report)
    write_json(stamped_path, report)

    print(f"Saved: {latest_path}")
    print(f"Snapshot: {stamped_path}")
    print(f"Videos checked: {overall_videos}")
    print(f"Comments checked: {overall_comments}")
    print("Complaint signals:")
    for category, count in overall_categories.most_common():
        if count:
            print(f"- {category}: {count}")
    print("Recommended actions:")
    for action in report["summary"]["recommended_actions"]:
        print(f"- {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
