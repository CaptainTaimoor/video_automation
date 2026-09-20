from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import traceback
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Set

import requests
from dotenv import load_dotenv

from yt_auto.config import load_config
from yt_auto.feedback_loop import FeedbackLoop
from yt_auto.heygen_browser_service import HeyGenBrowserService
from yt_auto.images import HybridMediaFetcher
from yt_auto.local_presenter import LocalPresenterService
from yt_auto.models import BuildArtifacts, ChannelConfig, ContentProfile, ScenePlanItem, TopicCandidate
from yt_auto.research import SourceSafeResearchExhaustedError
from yt_auto.script_writer import ScriptWriter
from yt_auto.seo import build_youtube_metadata
from yt_auto.thumbnailer import ThumbnailMaker
from yt_auto.subtitles import SubtitleComposer, SubtitleSegment
from yt_auto.title_lab import TitleLab, TitleVariant
from yt_auto.topics import TopicPlanner
from yt_auto.tts_engine import NarrationEngine
from yt_auto.logger import Logger
from yt_auto.uploaders import FacebookUploader, YouTubeUploader
from yt_auto.uploaders.youtube import UploadAuthError, UploadLimitExceededError
from yt_auto.utils import ensure_dir, now_in_tz, read_json, write_json
from yt_auto.hw_encode import resolve_h264_encode_profile, selected_encoder_note
from yt_auto.video_builder import (
    LongCaptionPreflightError,
    ShortCaptionPreflightError,
    VideoBuilder,
)


class UploadBlockedError(RuntimeError):
    def __init__(self, channel_id: str, blocked_until: str, reason: str, message: str = "") -> None:
        self.channel_id = channel_id
        self.blocked_until = blocked_until
        self.reason = reason
        self.message = message
        detail = f"Upload blocked for {channel_id} until {blocked_until} ({reason})."
        if message:
            detail = f"{detail} {message}"
        super().__init__(detail)


def _load_optional_dotenv() -> str | None:
    """Load local developer settings without making file ACLs a build failure.

    Runtime credentials are still validated by their individual providers. This
    only lets a dry-run/build proceed when a managed environment deliberately
    exposes configuration through process variables but denies access to the
    workspace `.env` file.

    Worktree checkouts also fill *missing* keys from a sibling live
    ``yt_automation/.env`` (never overriding values already present).
    """

    try:
        load_dotenv()
        # src/yt_auto/pipeline.py -> repo root -> sibling live checkout
        repo_root = Path(__file__).resolve().parents[2]
        sibling_env = repo_root.parent / "yt_automation" / ".env"
        if sibling_env.exists() and sibling_env.resolve() != (Path.cwd() / ".env").resolve():
            load_dotenv(sibling_env, override=False)
    except PermissionError:
        return "Local .env could not be read; using the process environment."
    return None


class ShortsFactory:
    ANCIENT_SHORT_VISUAL_CANDIDATES = 6
    SHORT_TIMING_RETRY_ROUNDS = 3
    # 8 was starving continuity recovery when Commons only returned a handful
    # of verified subject-relevant stills. Keep quality high, but unblock gaps.
    ANCIENT_SHORT_MIN_VERIFIED_REAL_VISUALS = 6
    # Max characters per spoken beat, so burned-in captions do not overflow.
    ANCIENT_SHORT_MAX_BEAT_CHARS = 70
    ANCIENT_SHORT_MAX_FACT_CARD_FALLBACKS = 2
    BRAIN_SHORT_MAX_FACT_CARD_FALLBACKS = 1
    BRAIN_LONG_MAX_AI_GENERATED_VISUALS = 1

    def __init__(self, config_path: Path) -> None:
        self.dotenv_load_warning = _load_optional_dotenv()
        if self.dotenv_load_warning:
            print(self.dotenv_load_warning, flush=True)
        self.config = load_config(config_path)
        self.logger = Logger()
        self.feedback = FeedbackLoop(self.config.app.state_dir)
        self.title_lab = TitleLab()
        self.topic_planner = TopicPlanner(timezone=self.config.app.timezone)
        self.script_writer = ScriptWriter(self.config.app.script_writer)
        self.image_fetcher = HybridMediaFetcher(
            stable_horde_key=os.getenv("STABLE_HORDE_KEY", ""),
            pixabay_api_key=os.getenv("PIXABAY_API_KEY", ""),
            pexels_api_key=os.getenv("PEXELS_API_KEY", ""),
            visuals=getattr(self.config.app, "visuals", None),
        )
        self.video_builder = VideoBuilder(
            min_duration=self.config.app.min_duration_seconds,
            max_duration=self.config.app.max_duration_seconds,
            subtitle_enabled=self.config.app.subtitles.enabled,
            subtitle_max_words=self.config.app.subtitles.max_words_per_caption,
        )
        self.thumbnail_maker = ThumbnailMaker(
            width=self.config.app.thumbnail.width,
            height=self.config.app.thumbnail.height,
        )
        
        self.youtube = YouTubeUploader()
        self.facebook = FacebookUploader()

        ensure_dir(self.config.app.output_root)
        ensure_dir(self.config.app.state_dir)
        ensure_dir(self.config.app.topic_cache_dir)
        # Ensure used_topics.txt exists
        utp = self._used_topics_path()
        if not utp.exists():
            utp.touch()

    def _channel(self, channel_id: str) -> ChannelConfig:
        for c in self.config.channels:
            if c.id == channel_id:
                return c
        raise ValueError(f"Unknown channel id: {channel_id}")

    def _channel_logo_path(self, channel: ChannelConfig) -> Path:
        logo_dir = ensure_dir(Path("assets") / "branding")
        logo_path = logo_dir / f"{channel.id}_logo.png"
        if logo_path.exists():
            return logo_path
        try:
            from PIL import Image, ImageDraw, ImageFont

            colors = {
                "ancient_history": ((28, 18, 9), (221, 168, 76), "ST"),
                "brain_lens": ((10, 18, 36), (88, 190, 255), "BL"),
            }
            bg, accent, initials = colors.get(
                channel.id,
                ((18, 18, 18), (235, 235, 235), channel.display_name[:2].upper()),
            )
            size = 512
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((24, 24, size - 24, size - 24), fill=(*bg, 238), outline=(*accent, 255), width=16)
            draw.ellipse((52, 52, size - 52, size - 52), outline=(*accent, 90), width=3)
            font = None
            for candidate in ("assets/fonts/Montserrat-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
                try:
                    font = ImageFont.truetype(str(Path(candidate).resolve()), 150)
                    break
                except Exception:
                    continue
            font = font or ImageFont.load_default()
            bbox = draw.textbbox((0, 0), initials, font=font)
            draw.text(
                ((size - (bbox[2] - bbox[0])) / 2, (size - (bbox[3] - bbox[1])) / 2 - 12),
                initials,
                font=font,
                fill=(*accent, 255),
            )
            img.save(logo_path)
        except Exception as exc:
            self.logger.warning(channel.id, f"Logo generation skipped: {exc}")
        return logo_path

    def _append_video_chapters(
        self,
        metadata: dict,
        topic: TopicCandidate,
        duration: float,
        visual_timeline: list[dict] | None = None,
    ) -> None:
        if metadata.get("content_kind") != "video" or duration < 180:
            return
        scenes = list(topic.scene_plan or [])
        if len(scenes) < 3:
            return

        actual_starts: dict[int, float] = {}
        for entry in visual_timeline or []:
            if not isinstance(entry, dict):
                continue
            try:
                beat_index = int(entry.get("beat_index"))
                start = float(entry.get("start"))
            except (TypeError, ValueError):
                continue
            if 0 <= beat_index < len(scenes):
                actual_starts[beat_index] = min(actual_starts.get(beat_index, start), start)

        max_chapters = 20
        if len(scenes) <= max_chapters:
            scene_indexes = list(range(len(scenes)))
        else:
            scene_indexes = sorted({
                round(position * (len(scenes) - 1) / (max_chapters - 1))
                for position in range(max_chapters)
            })

        chapter_lines: list[str] = []
        chapter_records: list[dict] = []
        last_seconds = -10
        for scene_index in scene_indexes:
            fallback_start = duration * scene_index / max(1, len(scenes))
            seconds = max(0, int(actual_starts.get(scene_index, fallback_start)))
            if seconds >= duration - 20:
                break
            if chapter_lines and seconds - last_seconds < 10:
                continue
            scene = scenes[scene_index]
            label_source = scene.visual_text or scene.narration
            label = re.sub(r"\s+", " ", str(label_source or "Key moment")).strip(" .:-")
            label = label[:54] or "Key moment"
            timestamp = f"{seconds // 60}:{seconds % 60:02d}"
            if not chapter_lines:
                seconds = 0
                timestamp = "0:00"
            chapter_lines.append(f"{timestamp} {label}")
            chapter_records.append({
                "timestamp": timestamp,
                "seconds": seconds,
                "scene_index": scene_index,
                "label": label,
            })
            last_seconds = seconds
        if len(chapter_lines) < 3:
            return
        description = str(metadata.get("description") or "")
        description = re.sub(r"\n\nChapters:\n.*$", "", description, flags=re.DOTALL).rstrip()
        metadata["description"] = (description.rstrip() + "\n\nChapters:\n" + "\n".join(chapter_lines))[:4900]
        metadata["chapters"] = chapter_records

    @staticmethod
    def _music_root_has_tracks(music_root: Path) -> bool:
        """True when root or a one-level channel folder contains audio beds."""
        if not music_root.exists() or not music_root.is_dir():
            return False
        audio_suffixes = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}

        def _dir_has_audio(path: Path) -> bool:
            try:
                return any(
                    item.is_file() and item.suffix.lower() in audio_suffixes
                    for item in path.iterdir()
                )
            except OSError:
                return False

        if _dir_has_audio(music_root):
            return True
        try:
            return any(
                child.is_dir() and _dir_has_audio(child)
                for child in music_root.iterdir()
            )
        except OSError:
            return False

    def _resolved_music_root(self) -> Path:
        """Prefer configured music_dir; fall back to env or sibling live assets.

        Worktrees often omit large binary assets. A junction/symlink is ideal,
        but path resolution must still find the live checkout's channel music
        so quality review does not falsely hold on 'no verified background
        music mix'.
        """
        configured = Path(self.config.app.music_dir)
        candidates: list[Path] = []
        env_dir = str(os.getenv("YT_MUSIC_DIR") or "").strip()
        if env_dir:
            candidates.append(Path(env_dir))
        candidates.append(configured)
        try:
            # .../assets/music -> repo root -> sibling yt_automation/assets/music
            app_root = configured.parent.parent
            candidates.append(app_root.parent / "yt_automation" / "assets" / "music")
        except Exception:
            pass
        for candidate in candidates:
            if self._music_root_has_tracks(candidate):
                return candidate
        return configured

    def _channel_music_dir(self, channel: ChannelConfig) -> Path:
        root = self._resolved_music_root()
        channel_dir = root / channel.id
        return channel_dir if channel_dir.exists() else root

    def _run_dir(self, channel: ChannelConfig, content_kind: str = "short") -> Path:
        now = now_in_tz(self.config.app.timezone)
        stamp = now.strftime("%Y%m%d_%H%M%S")
        day_dir = ensure_dir(channel.output_dir / content_kind / now.strftime("%Y-%m-%d"))
        return ensure_dir(day_dir / stamp)

    def _content_profile(self, channel: ChannelConfig, content_kind: str) -> ContentProfile:
        if content_kind == "video":
            return channel.videos
        return channel.shorts

    def _seo_keywords(self, channel: ChannelConfig, topic: TopicCandidate) -> list[str]:
        raw_terms = (
            [topic.subject, topic.title]
            + list(topic.trend_terms)
            + [tag.replace("#", "") for tag in topic.hashtags]
        )
        if channel.id == "ancient_history":
            raw_terms.extend([
                "ancient history", "history shorts", "lost civilization",
                "ancient empire", "historical mystery", "archaeology",
            ])
        elif channel.id == "brain_lens":
            raw_terms.extend([
                "psychology facts", "brain science", "human behavior",
                "cognitive bias", "self improvement", "mental patterns",
            ])

        blocked = {
            "verywell", "frontiers", "books", "book", "media", "national",
            "geographic", "magazine", "documentaries", "factualamerica",
            "reader", "digest", "your", "people", "they", "will",
        }
        out: list[str] = []
        seen: set[str] = set()
        for term in raw_terms:
            cleaned = re.sub(r"[^A-Za-z0-9 #'-]+", " ", str(term or "")).strip(" #,")
            cleaned = re.sub(r"\s+", " ", cleaned)
            key = cleaned.lower()
            if not cleaned or len(cleaned) < 3:
                continue
            if key in blocked or any(part in blocked for part in key.split()):
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned[:45])
            if len(out) >= 18:
                break
        return out

    def _seo_hashtags(self, channel: ChannelConfig, topic: TopicCandidate, content_kind: str) -> list[str]:
        base = ["#Shorts"] if content_kind == "short" else []
        if channel.id == "ancient_history":
            base.extend(["#AncientHistory", "#History"])
        elif channel.id == "brain_lens":
            base.extend(["#Psychology", "#BrainScience"])
        for tag in topic.hashtags:
            clean = tag if str(tag).startswith("#") else f"#{tag}"
            if clean not in base:
                base.append(clean)
            if len(base) >= 5:
                break
        return base[:5]

    def _metadata(self, channel: ChannelConfig, topic: TopicCandidate, ranked_variants: list[TitleVariant], content_kind: str) -> dict:
        return build_youtube_metadata(channel, topic, ranked_variants, content_kind)

    def _clean_title_grammar(self, title: str) -> str:
        title = re.sub(r"\s+", " ", title or "").strip()
        title = re.sub(r"^(Why|How|When|What)\s+\1\s+", r"\1 ", title, flags=re.IGNORECASE)
        title = re.sub(
            r"^(Why .+\b(?:keeps|makes|pulls|turns|hijacks|drives)\b.+?)\s+feels so addictive$",
            r"\1",
            title,
            flags=re.IGNORECASE,
        )
        title = re.sub(r"^(Why|How|When|What)\s+The\s+([a-z])", lambda m: f"{m.group(1)} the {m.group(2)}", title)
        title = re.sub(r"^(Why|How|When|What)\s+The\s+([A-Z])", lambda m: f"{m.group(1)} the {m.group(2).lower()}", title)
        title = re.sub(r"^Why\s+(?:the\s+)?halo effect\b", "Why the Halo Effect", title, flags=re.IGNORECASE)
        title = re.sub(r"^Why\s+(?:the\s+)?anchoring effect\b", "Why the Anchoring Effect", title, flags=re.IGNORECASE)
        title = re.sub(r"^Why\s+(?:the\s+)?fawn response\b", "Why the Fawn Response", title, flags=re.IGNORECASE)
        title = re.sub(
            r"\b(impressions|styles|neurons|choices|signals|messages|relationships) makes\b",
            r"\1 make",
            title,
            flags=re.IGNORECASE,
        )
        title = re.sub(r"\bthe\s+the\b", "the", title, flags=re.IGNORECASE)
        return title

    def _topic_from_run_data(self, channel_id: str, data: dict, metadata: dict | None = None) -> TopicCandidate:
        metadata = metadata or {}
        scene_plan = []
        for item in data.get("scene_plan") or []:
            if isinstance(item, dict):
                scene_plan.append(ScenePlanItem(
                    narration=str(item.get("narration") or ""),
                    visual_text=str(item.get("visual_text") or ""),
                    search_terms=list(item.get("search_terms") or []),
                    preferred_image_url=str(item.get("preferred_image_url") or ""),
                    visual_prompt=str(item.get("visual_prompt") or ""),
                ))

        return TopicCandidate(
            niche_id=str(data.get("niche_id") or channel_id),
            style=str(data.get("style") or metadata.get("style") or "story"),
            trend_terms=list(data.get("trend_terms") or metadata.get("seo_keywords") or metadata.get("tags") or []),
            title=str(metadata.get("title") or data.get("title") or "Untitled Video"),
            hook=str(data.get("hook") or ""),
            narration=str(data.get("narration") or ""),
            visual_captions=list(data.get("visual_captions") or metadata.get("visual_captions") or []),
            source_urls=list(data.get("source_urls") or metadata.get("source_urls") or []),
            image_queries=list(data.get("image_queries") or []),
            hashtags=list(data.get("hashtags") or metadata.get("hashtags") or []),
            engagement_score=float(data.get("engagement_score") or 0),
            content_kind=str(data.get("content_kind") or metadata.get("content_kind") or "short"),
            subject=str(data.get("subject") or metadata.get("subject") or metadata.get("title") or data.get("title") or ""),
            narration_beats=list(data.get("narration_beats") or []),
            title_variants=list(data.get("title_variants") or []),
            selected_title_pattern=str(data.get("selected_title_pattern") or metadata.get("selected_title_pattern") or "backlog_refresh"),
            scene_plan=scene_plan,
            transcripts=list(data.get("transcripts") or []),
            # Trust the topic-level fields written by the new provenance model;
            # do not import legacy metadata where script_provider meant merely
            # the last successful AI call.
            script_provider=str(data.get("script_provider") or ""),
            script_provider_endpoint_host=str(
                data.get("script_provider_endpoint_host") or ""
            ),
            script_provider_model=str(data.get("script_provider_model") or ""),
        )

    def _refresh_backlog_metadata_seo(self, channel: ChannelConfig, run_dir: Path, metadata: dict) -> dict:
        topic_path = run_dir / "topic.json"
        if not topic_path.exists():
            return metadata

        topic_data = read_json(topic_path, {})
        if not isinstance(topic_data, dict):
            return metadata

        topic = self._topic_from_run_data(channel.id, topic_data, metadata)
        ranked = [
            TitleVariant(
                pattern_id=topic.selected_title_pattern or "backlog_refresh",
                title=topic.title,
                predicted_score=1.0,
            )
        ]
        refreshed = self._metadata(channel, topic, ranked, topic.content_kind)
        for key in (
            "voice",
            "duration_seconds",
            "thumbnail_file",
            "quality_score",
            "quality_decision",
            "content_kind",
        ):
            if key in metadata:
                refreshed[key] = metadata[key]
        return refreshed

    def _write_topic(self, path: Path, topic: TopicCandidate) -> None:
        write_json(path, asdict(topic))

    def _log_state(self, event: dict) -> None:
        state_file = self.config.app.state_dir / "runs.jsonl"
        with state_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _upload_state_path(self) -> Path:
        return self.config.app.state_dir / "upload_state.json"

    def _load_upload_state(self) -> dict:
        return read_json(self._upload_state_path(), {"channels": {}})

    def _save_upload_state(self, state: dict) -> None:
        write_json(self._upload_state_path(), state)

    def _current_upload_block(self, channel_id: str) -> dict | None:
        state = self._load_upload_state()
        entry = (state.get("channels", {}) or {}).get(channel_id)
        if not entry:
            return None
        blocked_until = entry.get("blocked_until")
        if not blocked_until:
            return None
        try:
            until = datetime.fromisoformat(blocked_until)
        except Exception:
            return None
        if until <= now_in_tz(self.config.app.timezone):
            self._clear_upload_block(channel_id)
            return None
        return entry

    def _set_upload_block(self, channel_id: str, reason: str, message: str = "", hours: int = 24) -> dict:
        state = self._load_upload_state()
        channels = state.setdefault("channels", {})
        blocked_until = (now_in_tz(self.config.app.timezone) + timedelta(hours=hours)).isoformat()
        entry = {
            "blocked_until": blocked_until,
            "reason": reason,
            "message": message,
        }
        channels[channel_id] = entry
        self._save_upload_state(state)
        return entry

    def _clear_upload_block(self, channel_id: str) -> None:
        state = self._load_upload_state()
        channels = state.setdefault("channels", {})
        if channel_id in channels:
            channels.pop(channel_id, None)
            self._save_upload_state(state)

    def _ancient_long_title(self, topic: TopicCandidate) -> str:
        subject = re.sub(r"\s+", " ", str(topic.subject or topic.title or "Ancient history")).strip()
        lowered = subject.lower()
        titles = (
            (("lachish", "lakhish"), "How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence"),
            (("carthage",), "How Carthage Built a Mediterranean Empire—and Lost It to Rome"),
            (("indus valley", "mohenjo"), "Inside the Indus Valley: Cities, Trade, and an Undeciphered Script"),
            (("justinianic plague", "plague of justinian"), "The Justinianic Plague: What DNA and Ancient Witnesses Reveal"),
            (("bronze age collapse",), "Why the Bronze Age Collapsed—and Why No Single Cause Explains It"),
            (("oracle of delphi", "pythia"), "How the Oracle of Delphi Turned Prophecy Into Political Power"),
            (("hammurabi",), "The Code of Hammurabi: Law, Power, and the Limits of the Stone"),
            (("qin shi huang", "terracotta army"), "Qin Shi Huang’s Buried Empire: What the Terracotta Army Reveals"),
            (("machu picchu",), "How Machu Picchu’s Hidden Engineering Kept a Mountain City Standing"),
            (("nazca", "nasca"), "The Nazca Lines: What the Desert Evidence Can—and Cannot—Prove"),
            (("greek fire",), "Greek Fire: How Byzantium Turned a Secret Weapon Into Naval Power"),
            (("teutoburg",), "Teutoburg Forest: How Rome Lost Three Legions in Germania"),
            (("thermopylae",), "Thermopylae Beyond the Legend: Terrain, Strategy, and Sacrifice"),
            (("petra",), "How Petra Turned Desert Water and Trade Into Nabataean Power"),
            (("antikythera",), "The Antikythera Mechanism: Gears, Astronomy, and an Ancient Mystery"),
            (("cyrus cylinder",), "The Cyrus Cylinder: Royal Propaganda, Babylon, and a Modern Myth"),
            (("persepolis",), "Persepolis: How Persia Built Power Into Stone"),
        )
        for markers, title in titles:
            if any(marker in lowered for marker in markers):
                return title
        return f"{subject}: What the Surviving Evidence Actually Reveals"

    def _thumbnail_lead_asset(
        self,
        channel: ChannelConfig,
        content_kind: str,
        images: list[Path],
        sources: list[dict],
    ) -> Path:
        if channel.id != "ancient_history" or content_kind != "video" or not images:
            return images[0]
        ranked: list[tuple[int, int, Path]] = []
        for index, image_path in enumerate(images):
            source = sources[index] if index < len(sources) else {}
            identity = " ".join(
                (
                    str(source.get("asset_title") or ""),
                    str(source.get("url") or ""),
                    str(source.get("description") or ""),
                )
            ).lower()
            score = 0
            if str(source.get("source") or "") == "local_documentary_diagram":
                score -= 120
            if any(marker in identity for marker in ("montage", "collage", "map", "karta", "chronicle", "sketch")):
                score -= 80
            if any(marker in identity for marker in ("representation", "reconstruction", "model")):
                score += 42
            if any(marker in identity for marker in ("archaeological", "archaeology", "ruins", "site", "temple", "palace")):
                score += 32
            if any(marker in identity for marker in ("artifact", "stele", "statue", "relief", "inscription")):
                score += 18
            if source.get("verified_subject"):
                score += 12
            ranked.append((score, -index, image_path))
        return max(ranked, key=lambda item: (item[0], item[1]))[2]

    def _rank_and_apply_title(self, channel: ChannelConfig, topic: TopicCandidate) -> tuple[TopicCandidate, list[TitleVariant]]:
        if not self.config.app.ab_test_enabled:
            variants = [TitleVariant(pattern_id="base_original", title=topic.title, predicted_score=1.0)]
            return topic, variants

        # Upgrade the title through the configured, audited AI provider chain.
        current_title = self._clean_title_grammar(topic.title)
        ancient_long = channel.id == "ancient_history" and getattr(topic, "content_kind", "short") == "video"
        if ancient_long:
            current_title = self._ancient_long_title(topic)
        if current_title != topic.title:
            topic = replace(topic, title=current_title)
        ai_title = (
            ""
            if (
                getattr(topic, "content_kind", "short") == "short"
                and channel.id in {"brain_lens", "ancient_history"}
            ) or ancient_long
            else self.script_writer.generate_viral_title(channel, topic)
        )
        if ai_title and ai_title != current_title:
            provider_label = self.script_writer.last_ai_provider or "configured AI"
            print(
                f"[{channel.id}] {provider_label} upgraded title: "
                f"'{current_title}' -> '{ai_title}'"
            )
            topic = replace(topic, title=ai_title)

        state = self.feedback.load() if self.config.app.feedback_enabled else {"channels": {}, "videos": {}}
        variants = self.title_lab.make_variants(topic, count=self.config.app.ab_test_variants)
        chosen, ranked = self.title_lab.choose(channel.id, topic, variants, state)

        image_queries = list(dict.fromkeys([topic.title, chosen.title] + topic.image_queries))
        selected_title = self._clean_title_grammar(chosen.title)
        if ancient_long:
            selected_title = current_title
        updated = replace(
            topic,
            title=selected_title,
            hook=topic.hook,
            image_queries=image_queries,
            title_variants=[v.title for v in ranked],
            selected_title_pattern=chosen.pattern_id,
        )
        return updated, ranked

    def sync_feedback(self, channel_id: str, max_results: int = 25) -> dict:
        channel = self._channel(channel_id)
        recent_videos = self.youtube.list_recent_videos(channel, max_results=max_results)
        runs_by_video_id = {
            str(run.get("youtube_id")): run
            for run in self._read_run_log()
            if run.get("channel") == channel_id and run.get("youtube_id")
        }

        checked = 0
        applied = 0
        skipped = 0
        for video in recent_videos:
            checked += 1
            video_id = str(video.get("video_id") or "")
            run = runs_by_video_id.get(video_id)
            if not run:
                skipped += 1
                continue

            metadata = read_json(Path(str(run.get("metadata_path") or "")), {})
            topic = read_json(Path(str(run.get("run_dir") or "")) / "topic.json", {})
            result = self.feedback.apply_video_metrics(
                channel_id=channel_id,
                style=str(topic.get("style") or metadata.get("style") or "story"),
                pattern_id=str(metadata.get("selected_title_pattern") or topic.get("selected_title_pattern") or "base_original"),
                terms=list(topic.get("trend_terms") or metadata.get("tags") or []),
                metrics=video,
                min_views=self.config.app.feedback_min_views,
            )
            if result.get("applied"):
                applied += 1
            else:
                skipped += 1

        return {"checked": checked, "applied": applied, "skipped": skipped}

    def feedback_report(self, channel_id: str) -> dict:
        self._channel(channel_id)
        return self.feedback.report(channel_id)

    def _read_run_log(self) -> list[dict]:
        runs_file = self.config.app.state_dir / "runs.jsonl"
        if not runs_file.exists():
            return []

        out = []
        for line in runs_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # A backup topic is reused only after this long. Recovery draws from a
    # short fixed list, so without a cooldown the same subject ships again
    # every time the channel goes quiet -- Pompeii went out twelve times.
    CONTINUITY_SUBJECT_COOLDOWN_DAYS = 30

    @staticmethod
    def _normalise_subject(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @staticmethod
    def _run_log_day(run: dict) -> str:
        """The YYYY-MM-DD a run belongs to, taken from its output folder.

        Run records carry no timestamp of their own, but every run_dir is
        written under .../<channel>/<kind>/<YYYY-MM-DD>/<stamp>.
        """
        match = re.search(r"(\d{4}-\d{2}-\d{2})", str(run.get("run_dir") or ""))
        return match.group(1) if match else ""

    def _recently_published_subjects(
        self,
        channel_id: str,
        *,
        days: int | None = None,
        today: str | None = None,
    ) -> set[str]:
        """Subjects this channel actually published inside the cooldown window.

        Matching on the exact title is not enough: recovery rewrites the title
        for the same curated facts, so a renamed repeat looked new every time.
        """
        window = int(days if days is not None else self.CONTINUITY_SUBJECT_COOLDOWN_DAYS)
        now = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now()
        cutoff = now - timedelta(days=max(0, window))
        seen: set[str] = set()
        for run in self._read_run_log():
            if str(run.get("channel") or "") != channel_id:
                continue
            if not (run.get("uploaded") or run.get("youtube_id")):
                continue
            day = self._run_log_day(run)
            if day:
                try:
                    if datetime.strptime(day, "%Y-%m-%d") < cutoff:
                        continue
                except ValueError:
                    pass
            subject = self._normalise_subject(run.get("subject"))
            if subject:
                seen.add(subject)
        return seen

    def _update_run_log_upload(
        self,
        run_dir: str,
        youtube_id: str | None = None,
        facebook_id: str | None = None,
        error: str | None = None,
    ) -> None:
        runs_file = self.config.app.state_dir / "runs.jsonl"
        if not runs_file.exists():
            return

        lines = runs_file.read_text(encoding="utf-8").splitlines()
        updated_lines: list[str] = []
        target = str(run_dir)
        changed = False
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                updated_lines.append(line)
                continue
            if str(entry.get("run_dir") or "") == target:
                if youtube_id:
                    entry["youtube_id"] = youtube_id
                    entry["youtube_error"] = None
                if facebook_id:
                    entry["facebook_id"] = facebook_id
                    entry["facebook_error"] = None
                if error:
                    if not youtube_id:
                        entry["youtube_error"] = error
                    elif not facebook_id:
                        entry["facebook_error"] = error
                entry["uploaded"] = bool(entry.get("youtube_id") or entry.get("facebook_id"))
                changed = True
            updated_lines.append(json.dumps(entry, ensure_ascii=False))
        if changed:
            runs_file.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    def _upload_provider(self) -> str:
        provider = (os.getenv("UPLOAD_PROVIDER") or "facebook").strip().lower()
        return provider or "facebook"

    @staticmethod
    def _run_counts_as_published(run: dict) -> bool:
        """Only successful upload or quality-passed ready builds consume topics.

        Caption/render failures and quality holds must not exhaust rotation.
        Dry-run builds that still pass the quality gate are treated as
        ready-to-upload and therefore consume the topic.
        """
        if run.get("uploaded") is True:
            return True
        if str(run.get("youtube_id") or "").strip() or str(
            run.get("youtube_video_id") or ""
        ).strip() or str(run.get("facebook_id") or "").strip():
            return True
        decision = str(run.get("quality_decision") or "").strip().lower()
        if decision == "pass":
            skipped = str(run.get("upload_skipped") or "").strip().lower()
            # Explicit quality holds must never consume rotation.
            if skipped == "quality_gate":
                return False
            return True
        return False

    def _failed_topics_path(self) -> Path:
        return self.config.app.state_dir / "failed_topics.jsonl"

    def _requeue_failed_topic(
        self,
        channel_id: str,
        topic: object,
        *,
        reason: str,
        status: str = "render_failed",
        content_kind: str = "short",
    ) -> None:
        """Record a failed topic for retry without burning used_topics."""
        subject = str(getattr(topic, "subject", "") or "").strip()
        title = str(getattr(topic, "title", "") or "").strip()
        if not subject and not title:
            return
        entry = {
            "channel": channel_id,
            "subject": subject,
            "title": title,
            "content_kind": content_kind,
            "status": status,
            "reason": str(reason or "")[:400],
            "requeued_at": now_in_tz(self.config.app.timezone).isoformat(),
        }
        path = self._failed_topics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # Never append failed builds into used_topics.txt.
        self.logger.warning(
            channel_id,
            f"Requeued failed topic without burning used_topics "
            f"({status}): {title or subject}",
        )

    def _mark_topic_used_if_ready(
        self,
        *,
        channel_id: str,
        topic: object,
        quality_decision: str,
        uploaded: bool,
        upload_skipped: str | None = None,
    ) -> None:
        """Permanently consume a topic only after upload or quality-pass ready build."""
        skipped = str(upload_skipped or "").strip().lower()
        decision = str(quality_decision or "").strip().lower()
        ready = bool(uploaded) or (
            decision == "pass" and skipped != "quality_gate"
        )
        if not ready:
            return
        subject = str(getattr(topic, "subject", "") or "").strip()
        title = str(getattr(topic, "title", "") or "").strip()
        if subject:
            self._add_used_topic(subject)
        if title:
            self._add_used_topic(title)

    def _recent_titles(self, channel_id: str, limit: int = 150) -> set[str]:
        runs = self._read_run_log()
        titles: list[str] = []
        for run in reversed(runs):
            if str(run.get("channel") or "") != channel_id:
                continue
            if not self._run_counts_as_published(run):
                continue
            for key in ("title", "subject"):
                value = (run.get(key) or "").strip().lower()
                if value:
                    titles.append(value)
            run_dir = Path(str(run.get("run_dir") or ""))
            topic_path = run_dir / "topic.json"
            metadata_path = run_dir / "metadata.json"
            for path in (topic_path, metadata_path):
                if not path.exists():
                    continue
                data = read_json(path, {})
                for key in ("title", "subject"):
                    value = str(data.get(key) or "").strip().lower()
                    if value:
                        titles.append(value)
            channel_limit = 48 if channel_id == "brain_lens" else 80
            if len(titles) >= min(limit, channel_limit):
                break
        return set(titles)

    def _story_alias(self, text: str) -> str | None:
        lowered = re.sub(r"[^a-z0-9\s]+", " ", text.lower())
        lowered = re.sub(r"\s+", " ", lowered).strip()
        alias_rules = (
            ("battle of teutoburg forest", ("teutoburg", "varus", "lost legion", "lost legions", "rome s worst defeat", "rome s worst nightmare")),
            ("sack of rome 410", ("sack of rome", "rome s 410 sack", "rome s fall 410", "rome s darkest hour", "rome sacked", "sacked rome", "alaric", "visigoth")),
            ("battle of thermopylae", ("thermopylae", "300 spartans", "spartan", "persian army at a narrow pass")),
            ("siege of masada", ("masada",)),
            ("nazca lines", ("nazca", "nasca", "geoglyph")),
            ("byzantine greek fire", ("greek fire", "byzantine fire")),
            ("qin shi huang terracotta army", ("qin shi huang", "terracotta army", "mausoleum")),
            ("library of alexandria", ("library of alexandria", "alexandria library")),
            ("pompeii vesuvius", ("pompeii", "vesuvius")),
            ("carthage hannibal", ("carthage", "hannibal", "punic")),
            ("bronze age collapse", ("bronze age collapse", "sea peoples")),
            ("analysis paralysis", ("analysis paralysis",)),
            ("anchoring effect", ("anchoring effect",)),
            ("anxious attachment", ("anxious attachment",)),
            ("avoidant attachment", ("avoidant attachment",)),
            ("body language", ("body language",)),
            ("brain fog", ("brain fog",)),
            ("choice overload", ("choice overload",)),
            ("cognitive dissonance", ("cognitive dissonance",)),
            ("comparison trap", ("comparison trap",)),
            ("almost relationships", ("almost relationship", "almost relationships", "situationship", "situationships")),
            ("almost kiss tension", ("almost kiss", "almost kiss tension", "kiss tension")),
            ("chemistry versus compatibility", ("chemistry vs compatibility", "chemistry versus compatibility", "chemistry compatibility")),
            ("eye contact attraction", ("eye contact attraction", "eye contact", "gaze attraction")),
            ("flirting body language", ("flirting body language", "body language attraction", "dating body language")),
            ("late night texting", ("late night texting", "late-night texting", "night texting")),
            ("micro flirting", ("micro flirting", "tiny flirting", "small flirting cues")),
            ("mixed signals", ("mixed signal", "mixed signals")),
            ("situationship anxiety", ("situationship anxiety", "situationship")),
            ("texting anxiety", ("texting anxiety", "reply anxiety", "delayed replies")),
            ("voice change attraction", ("voice change attraction", "voice changes", "voice attraction")),
            ("confirmation bias", ("confirmation bias",)),
            ("memory distortion", ("memory distortion",)),
            ("overthinking", ("overthinking", "rumination")),
            ("people pleasing", ("people pleasing", "fawn response")),
            ("rejection sensitivity", ("rejection sensitivity",)),
            ("self esteem", ("self esteem", "self-esteem")),
            ("social proof", ("social proof",)),
            ("sunk cost fallacy", ("sunk cost",)),
        )
        for canonical, aliases in alias_rules:
            if any(alias in lowered for alias in aliases):
                return canonical
        return None

    def _story_fingerprint(self, *parts: str) -> str:
        raw = " ".join(str(part or "") for part in parts)
        alias = self._story_alias(raw)
        if alias:
            return alias
        cleaned = re.sub(r"[^a-z0-9\s]+", " ", raw.lower())
        words = re.findall(r"[a-z0-9]{4,}", cleaned)
        stopwords = {
            "about", "after", "ancient", "before", "behind", "brain", "changed", "changes",
            "choice", "choices", "clues", "decoded", "details", "empire", "empires", "every",
            "explain", "explained", "facts", "feels", "forgotten", "hidden", "history", "inside",
            "lesson", "lost", "mind", "moment", "people", "reaction", "real", "revealed", "reveals",
            "science", "secret", "secrets", "shorts", "signs", "story", "takes", "taking", "that",
            "this", "truth", "uncovering", "untold", "what", "when", "where", "which", "with",
            "worst", "your",
        }
        keywords = [word for word in words if word not in stopwords]
        return " ".join(dict.fromkeys(keywords[:5]))

    def _recent_story_fingerprints(self, channel_id: str, limit: int = 45) -> dict[str, str]:
        fingerprints: dict[str, str] = {}
        checked = 0
        for run in reversed(self._read_run_log()):
            if run.get("channel") != channel_id:
                continue
            if not self._run_counts_as_published(run):
                continue
            parts = [str(run.get("title") or ""), str(run.get("subject") or "")]
            run_dir = Path(str(run.get("run_dir") or ""))
            for path in (run_dir / "topic.json", run_dir / "metadata.json"):
                if path.exists():
                    data = read_json(path, {})
                    parts.extend([str(data.get("title") or ""), str(data.get("subject") or "")])
            fingerprint = self._story_fingerprint(*parts)
            if fingerprint:
                fingerprints.setdefault(fingerprint, parts[0] or str(run.get("run_dir") or "recent run"))
            checked += 1
            if checked >= limit:
                break
        return fingerprints

    def _day_upload_count(self, channel_id: str, day_key: str | None = None) -> int:
        if day_key is None:
            day_key = now_in_tz(self.config.app.timezone).strftime("%Y-%m-%d")
        count = 0
        for run in reversed(self._read_run_log()):
            if run.get("channel") != channel_id:
                continue
            if not run.get("uploaded") and not run.get("facebook_video_id"):
                continue
            run_dir = str(run.get("run_dir", ""))
            if day_key and day_key not in run_dir:
                continue
            count += 1
        return count

    def _daily_upload_cap_reached(self, channel: ChannelConfig) -> bool:
        cap = int(channel.daily_upload_cap or 0)
        return cap > 0 and self._day_upload_count(channel.id) >= cap

    def _target_platforms_for_channel(self, channel: ChannelConfig) -> list[str]:
        targets: list[str] = []
        if channel.youtube and channel.youtube.upload_enabled:
            targets.append("youtube")
        if channel.facebook and channel.facebook.upload_enabled:
            targets.append("facebook")
        return targets

    def _backlog_item_is_complete(self, channel: ChannelConfig, item: dict) -> bool:
        targets = self._target_platforms_for_channel(channel)
        if not targets:
            return True
        if "youtube" in targets and not item.get("youtube_id"):
            return False
        if "facebook" in targets and not item.get("facebook_id"):
            return False
        return True

    def _backlog_title_quality_issue(self, title: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", title or "").strip()
        lowered = cleaned.lower()
        if not cleaned:
            return "missing title"
        if re.match(r"^(why|how|what|when)\s+\1\b", lowered):
            return "repeated question word"
        if re.match(r"^why .+\b(?:keeps|makes|pulls|turns|hijacks|drives)\b.+\bfeels so addictive$", lowered):
            return "stacked title predicates"
        if lowered.endswith("feels so addictive") and any(
            term in lowered
            for term in (
                "emotional regulation", "attachment styles", "mirror neurons", "fawn response",
                "impostor syndrome", "learned helplessness", "memory distortion", "halo effect",
                "first impression", "anchoring effect",
            )
        ):
            return "misleading addictive framing"
        if len(cleaned) > 95:
            return "title too long"
        if len(re.findall(r"[A-Za-z0-9']+", cleaned)) < 6:
            return "title too vague"
        if re.search(r"\(?\s*\d+\s*characters?\s*\)?", lowered):
            return "AI length note leaked into title"
        if any(
            phrase in lowered
            for phrase in (
                "your brain already knows",
                "brain already knows",
                "this one text move",
                "means they are gone",
                "means they're gone",
            )
        ):
            return "unsupported certainty or withheld-subject clickbait"
        if (
            lowered.startswith(("here's a youtube title", "here is a youtube title", "sure,", "option "))
            or "follows all the given rules" in lowered
            or "youtube title for shorts" in lowered
        ):
            return "AI instruction text leaked into title"
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
        )
        if lowered.startswith(weak_starts):
            return "generic title pattern"
        if "subtle misdirection" in lowered or "hidden pattern" in lowered:
            return "generic abstract Brain Lens title"
        if "quietly steals your attention" in lowered:
            return "generic repeated Brain Lens title"
        if "how to read the moment without guessing" in lowered:
            return "generic repeated Brain Lens title"
        if "the brain loop behind your reaction" in lowered:
            return "generic repeated Brain Lens title"
        if re.match(r"^when .+ feels like chemistry but may be uncertainty$", lowered):
            return "generic repeated Brain Lens title"
        if any(
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
        if re.match(r"^why .+ feels urgent when nothing is wrong$", lowered):
            return "generic repeated Brain Lens title"
        if re.match(r"^.+: the pattern people read before you speak$", lowered):
            return "generic repeated Brain Lens title"
        if re.match(r"^.+: the reward loop your brain keeps choosing$", lowered):
            return "generic repeated Brain Lens title"
        if re.match(r"^the first clue .+ is (taking over|bending your judgment)$", lowered):
            return "generic repeated Brain Lens title"
        if "the first clue" in lowered:
            return "generic repeated Brain Lens title"
        if re.match(r"^3 signs .+ is (running|shaping) your reaction", lowered):
            return "generic repeated Brain Lens title"
        if lowered.startswith("how ") and ": how " in lowered:
            return "duplicate how-title formula"
        if any(
            phrase in lowered
            for phrase in (
                "3 pieces of evidence", "3 artifacts that", "3 physical clues",
                "the artifact trail behind", "the artifact that changed how historians see",
            )
        ):
            return "overclaiming Ancient title formula"
        if re.match(r"^what .+ does before you notice it$", lowered):
            return "generic repeated Brain Lens title"
        if re.match(r"^why .+ feels personal even when it is not$", lowered):
            return "generic repeated Brain Lens title"
        if cleaned.isupper() and len(cleaned) > 32:
            return "all-caps title"
        if any(term in lowered for term in ("verywell", "frontiers", "reader's digest", "national geographic", "factualamerica")):
            return "source-name pollution"
        return None

    def _backlog_item_age_days(self, item: dict) -> int | None:
        run_dir = str(item.get("run_dir") or "")
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", run_dir)
        if not match:
            return None
        try:
            item_date = datetime.fromisoformat(match.group(1)).date()
        except ValueError:
            return None
        today = now_in_tz(self.config.app.timezone).date()
        return max(0, (today - item_date).days)

    def _backlog_item_stale_issue(self, channel: ChannelConfig, item: dict) -> str | None:
        max_age = int(getattr(channel, "max_backlog_age_days", 0) or 0)
        if max_age <= 0:
            return None
        age_days = self._backlog_item_age_days(item)
        if age_days is not None and age_days > max_age:
            return f"stale backlog item ({age_days}d old; limit {max_age}d)"
        return None

    def _opening_hook_issue(self, topic: TopicCandidate) -> str | None:
        hook = ""
        if getattr(topic, "content_kind", "short") == "video" and topic.hook:
            hook = str(topic.hook or "")
        elif topic.narration_beats:
            hook = str(topic.narration_beats[0] or "")
        elif topic.scene_plan:
            hook = str(topic.scene_plan[0].narration or topic.scene_plan[0].visual_text or "")
        else:
            hook = str(topic.hook or topic.narration or "")
        hook = re.sub(r"\s+", " ", hook).strip()
        lowered = hook.lower()
        if not hook:
            return "missing opening hook"
        generic_starts = (
            "to understand ",
            "there is a fascinating reason",
            "if you've ever felt",
            "modern psychology has a lot to say",
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
        )
        if lowered.startswith(generic_starts) or "rarely make it into textbooks" in lowered:
            return "generic opening hook"
        if re.match(r"^you notice .+ in one small moment, before you have time to explain it", lowered):
            return "generic subject-label opening hook"
        if "does before you notice it" in lowered:
            return "generic opening hook"
        hook_limit = 40 if getattr(topic, "content_kind", "short") == "video" else 22
        if len(re.findall(r"[A-Za-z0-9']+", hook)) > hook_limit:
            return "opening hook too slow"
        if re.search(
            r"\b(?:or|and|with|from|to|of|the|a|an|that|which|because|rather|than|before|after|while|but|yet|so|instead)\.?$",
            lowered,
        ):
            return "opening hook ends on a dangling connector"
        return None

    def _recent_angle_issue(self, channel: ChannelConfig, topic: TopicCandidate, limit: int = 35) -> str | None:
        target = f"{topic.title} {topic.subject}".lower()
        if channel.id == "ancient_history":
            angle_thresholds = {
                "teutoburg": 1,
                "thermopylae": 1,
                "nazca": 1,
                "nasca": 1,
                "greek fire": 1,
                "carthage": 1,
                "petra": 1,
                "dead sea scrolls": 1,
                "antikythera": 1,
                "hammurabi": 1,
                "alexandria": 1,
                "minoan eruption": 1,
                "sack of rome": 1,
                "qin shi huang": 1,
                "justinianic plague": 1,
                "cadaver synod": 1,
            }
        elif channel.id == "brain_lens":
            angle_thresholds = {
                "mental loop": 3,
                "hidden pattern": 3,
                "attention loop": 3,
                "memory distortion": 3,
                "confirmation bias": 3,
                "overthinking": 4,
                "dopamine": 4,
                "rejection": 4,
            }
        else:
            angle_thresholds = {}

        for angle, threshold in angle_thresholds.items():
            if angle not in target:
                continue
            count = 0
            checked = 0
            for run in reversed(self._read_run_log()):
                if run.get("channel") != channel.id:
                    continue
                checked += 1
                recent_text = f"{run.get('title') or ''} {run.get('subject') or ''}".lower()
                if angle in recent_text:
                    count += 1
                if checked >= limit:
                    break
            if count >= threshold:
                return f"overused recent angle: {angle}"
        return None

    def _visual_signatures(self, url: str) -> set[str]:
        raw = (url or "").strip()
        if not raw:
            return set()
        signatures = {raw}
        low = raw.lower()
        pexels_match = re.search(r"/video-files/(\d+)/", low)
        if pexels_match:
            signatures.add(f"pexels:{pexels_match.group(1)}")
        pixabay_match = re.search(r"/get/([^/?]+)", low)
        if pixabay_match:
            signatures.add(f"pixabay:{pixabay_match.group(1)}")
        wikimedia_match = re.search(r"/([^/]+\.(?:jpg|jpeg|png|webp))(?:\?|$)", low)
        if "wikimedia" in low and wikimedia_match:
            signatures.add(f"wikimedia:{wikimedia_match.group(1)}")
        return signatures

    def _recent_visual_urls(self, channel_id: str, run_limit: int = 24) -> set[str]:
        urls: set[str] = set()
        checked_runs = 0
        for run in reversed(self._read_run_log()):
            # Global: don't filter by channel_id
            run_dir = Path(str(run.get("run_dir") or "")).expanduser()
            sources_path = run_dir / "sources.json"
            if not sources_path.exists():
                continue
            try:
                sources = json.loads(sources_path.read_text(encoding="utf-8"))
                for item in sources:
                    url = str(item.get("url") or "").strip()
                    if url:
                        urls.update(self._visual_signatures(url))
            except Exception:
                continue
            checked_runs += 1
            if checked_runs >= run_limit:
                break
        return urls

    def _validate_topic_quality(self, channel: ChannelConfig, topic: TopicCandidate, recent_titles: set[str]) -> None:
        title = (topic.title or "").strip().lower()
        if not title or len(title) < 24:
            raise ValueError(f"Rejected weak/generic title: {topic.title}")
        generic_starts = (
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
        if title.startswith(generic_starts):
            raise ValueError(f"Rejected generic title pattern: {topic.title}")
        if re.match(r"^(why|how|what|when)\s+\1\b", title):
            raise ValueError(f"Rejected repeated question word: {topic.title}")
        if re.match(r"^why .+\b(?:keeps|makes|pulls|turns|hijacks|drives)\b.+\bfeels so addictive$", title):
            raise ValueError(f"Rejected stacked title predicates: {topic.title}")
        if title.endswith("feels so addictive") and any(
            term in title
            for term in (
                "emotional regulation", "attachment styles", "mirror neurons", "fawn response",
                "impostor syndrome", "learned helplessness", "memory distortion", "halo effect",
                "first impression", "anchoring effect",
            )
        ):
            raise ValueError(f"Rejected misleading addictive framing: {topic.title}")
        if re.match(r"^what .+ does before you notice it$", title):
            raise ValueError(f"Rejected repeated Brain Lens title formula: {topic.title}")
        if re.match(r"^why .+ feels personal even when it is not$", title):
            raise ValueError(f"Rejected repeated Brain Lens title formula: {topic.title}")
        if channel.id == "brain_lens" and "quietly steals your attention" in title:
            raise ValueError(f"Rejected repeated Brain Lens title formula: {topic.title}")
        if channel.id == "brain_lens" and any(
            phrase in title
            for phrase in (
                "brain trick shaping your next reaction",
                "how this brain trick skews your choices",
                "brain trick that skews your choices",
                "makes simple choices feel heavy",
                "running your reaction",
            )
        ):
            raise ValueError(f"Rejected repeated Brain Lens title formula: {topic.title}")
        if channel.id == "brain_lens" and not self.title_lab.title_contains_search_core(topic, topic.title):
            raise ValueError(f"Rejected non-searchable Brain Lens title missing its subject: {topic.title}")
        if channel.id == "brain_lens" and re.match(r"^the first clue .+ is (taking over|bending your judgment)$", title):
            raise ValueError(f"Rejected repeated Brain Lens title formula: {topic.title}")
        if channel.id == "brain_lens" and re.match(r"^3 signs .+ is (running|shaping) your reaction", title):
            raise ValueError(f"Rejected repeated Brain Lens title formula: {topic.title}")
        if channel.id == "brain_lens" and title.startswith("how ") and ": how " in title:
            raise ValueError(f"Rejected duplicate how-title formula: {topic.title}")
        if channel.id == "ancient_history" and any(
            phrase in title
            for phrase in (
                "the hidden cost of",
                "became impossible to ignore",
                "changes the whole story",
                "3 hidden clues that explain",
                "3 details about",
            )
        ):
            raise ValueError(f"Rejected overused Ancient title formula: {topic.title}")
        if channel.id == "ancient_history":
            title_terms = {
                term
                for term in re.findall(r"[a-z0-9]+", title)
                if len(term) >= 5 and term not in {"ancient", "history", "artifact", "pieces", "evidence", "reframe", "changed", "historians", "behind", "trail"}
            }
            script_text = " ".join([topic.subject or "", topic.hook or "", topic.narration or ""]).lower()
            if title_terms and not any(term in script_text for term in title_terms):
                raise ValueError(f"Rejected Ancient title/script mismatch: {topic.title}")
        if re.search(r"\(?\s*\d+\s*characters?\s*\)?", title):
            raise ValueError(f"Rejected AI length note in title: {topic.title}")
        fingerprint = self._story_fingerprint(
            topic.title,
            topic.subject,
            topic.hook,
            " ".join(topic.trend_terms[:4]),
        )
        recent_fingerprints = self._recent_story_fingerprints(channel.id)
        if fingerprint and fingerprint in recent_fingerprints:
            # Continuity evergreen packs already ship unique titles. Blocking them
            # by loose subject fingerprints empties the recovery pool and grows
            # upload gaps when AI research is stuck on one weak topic.
            if str(getattr(topic, "selected_title_pattern", "") or "") == "continuity_evergreen_fallback":
                prior_title = str(recent_fingerprints.get(fingerprint) or "").strip().lower()
                if prior_title and prior_title == str(topic.title or "").strip().lower():
                    raise ValueError(
                        f"Rejected repeated continuity title already used in: {recent_fingerprints[fingerprint]}"
                    )
            else:
                raise ValueError(
                    f"Rejected repeated story '{fingerprint}' already used in: {recent_fingerprints[fingerprint]}"
                )
        hook_issue = self._opening_hook_issue(topic)
        if hook_issue:
            raise ValueError(f"Rejected weak opener: {hook_issue} ({topic.title})")
        script_issues = self._script_quality_issues(channel, topic)
        if script_issues:
            raise ValueError(f"Rejected weak script: {script_issues[0]} ({topic.title})")
        editorial_issues = self.script_writer.editorial_quality_issues(
            topic,
            beats=list(topic.narration_beats or []),
            content_kind=getattr(topic, "content_kind", "short"),
        )
        if editorial_issues:
            raise ValueError(f"Rejected editorial script issue: {editorial_issues[0]} ({topic.title})")
        if not [source for source in topic.source_urls if str(source).strip()]:
            raise ValueError(f"Rejected source-less topic: {topic.title}")
        if getattr(topic, "content_kind", "short") == "short" and channel.id == "brain_lens":
            narration_words = re.findall(r"[A-Za-z0-9']+", topic.narration or "")
            min_words = int(getattr(self.script_writer, "_BRAIN_SHORT_MIN_WORDS", 122) or 122)
            max_words = int(getattr(self.script_writer, "_BRAIN_SHORT_MAX_WORDS", 142) or 142)
            if not (min_words <= len(narration_words) <= max_words):
                raise ValueError(
                    "Rejected Brain Lens Short narration length: "
                    f"{len(narration_words)} words; target is {min_words}-{max_words}"
                )
            visible_characters = len(re.sub(r"\s+", " ", topic.narration or "").strip())
            if visible_characters > 1000:
                raise ValueError(
                    f"Rejected over-dense Brain Lens Short: {visible_characters} visible characters; maximum is 1000"
                )
        if getattr(topic, "content_kind", "short") == "short" and channel.id == "ancient_history":
            visible_characters = len(re.sub(r"\s+", " ", topic.narration or "").strip())
            if visible_characters > 1000:
                raise ValueError(
                    f"Rejected over-dense Ancient History Short: {visible_characters} visible characters; maximum is 1000"
                )
        if getattr(topic, "content_kind", "short") == "video":
            long_word_count = len(re.findall(r"[A-Za-z0-9']+", topic.narration or ""))
            minimum_words = 1200 if channel.id == "ancient_history" else 1250
            if long_word_count < minimum_words:
                raise ValueError(
                    f"Rejected thin long script: {long_word_count} words; needs at least {minimum_words}"
                )
            if long_word_count > 1520:
                raise ValueError(
                    f"Rejected overfilled long script: {long_word_count} words; maximum is 1520"
                )
            repetition_issue = self._long_script_repetition_issue(topic)
            if repetition_issue:
                raise ValueError(f"Rejected repetitive long script: {repetition_issue}")
        angle_issue = self._recent_angle_issue(channel, topic)
        if angle_issue:
            raise ValueError(f"Rejected repeated angle: {angle_issue} ({topic.title})")
        title_words = re.findall(r"[a-z0-9']+", title)
        if len(title_words) < 6:
            raise ValueError(f"Rejected underspecified title: {topic.title}")
        if len(topic.title) > 95:
            raise ValueError(f"Rejected overlong title: {topic.title}")
        if topic.title.isupper():
            raise ValueError(f"Rejected shouty all-caps title: {topic.title}")
        junk_terms = {
            "verywell", "frontiers", "books", "national geographic", "reader's digest",
            "factualamerica", "vocal media",
        }
        combined_for_junk = " ".join([topic.title, topic.subject] + list(topic.trend_terms)).lower()
        if any(term in combined_for_junk for term in junk_terms):
            raise ValueError(f"Rejected source-name/topic pollution: {topic.title}")
        
        # Recent titles check (from runs.jsonl)
        for recent in recent_titles:
            if title in recent or recent in title:
                raise ValueError(f"Rejected duplicate topic: {topic.title} (similar to {recent})")
        
        # Ultra-strict: Global used_topics.txt check (DISABLED TO PREVENT EXHAUSTION)
        # used = self._load_used_topics()
        # if title in used or topic.subject.lower() in used:
        #     raise ValueError(f"Rejected strictly repeated topic: {topic.title}/{topic.subject}")
        
        # Check narration snippets too (DISABLED TO PREVENT EXHAUSTION)
        # used = self._load_used_topics()
        # words = topic.narration.lower().split()
        # for i in range(len(words)-5):
        #     phrase = " ".join(words[i:i+6])
        #     if phrase in used:
        #         raise ValueError(f"Rejected repeated narration phrase: {phrase}")

        # Niche check for Brain Lens
        if channel.id == "brain_lens":
            normalized_subject = re.sub(r"\s+", " ", str(topic.subject or "").strip().lower())
            relationship_subjects = {
                re.sub(r"\s+", " ", str(subject).strip().lower())
                for subject in getattr(
                    self.topic_planner.research,
                    "brain_lens_priority_subjects",
                    set(),
                )
            }
            if normalized_subject not in relationship_subjects:
                raise ValueError(
                    f"Rejected generic psychology subject outside relationship strategy: {topic.subject}"
                )
            combined = " ".join([topic.title, topic.subject, topic.narration] + list(topic.trend_terms)).lower()
            strong_terms = {
                "psychology", "brain", "cognitive", "behavior", "mental health", "memory", "neuro", "mind",
                "attention", "focus", "emotion", "habit", "stress", "burnout", "anxiety", "bias",
                "relationship", "self esteem", "choice", "decision", "social",
            }
            hits = {term for term in strong_terms if term in combined}
            if len(hits) < 1:
                raise ValueError(f"Rejected off-niche topic: {topic.title}")
            topic_lines = list(topic.narration_beats or [])
            if not topic_lines and topic.narration:
                topic_lines = [part.strip() for part in re.split(r"(?<=[.!?])\s+", topic.narration) if part.strip()]
            opener = (topic_lines[0] if topic_lines else topic.hook or "").strip().lower()
            concrete_starts = (
                "you ",
                "your ",
                "they ",
                "notice ",
                "watch ",
                "the moment ",
                "that second ",
            )
            concrete_cues = (
                "phone",
                "text",
                "reply",
                "silence",
                "gaze",
                "memory",
                "agree",
                "disappointed",
                "trait",
                "red flag",
                "praised",
                "mistake",
                "attempt",
                "jaw",
                "option",
                "number",
                "online",
                "thumb",
                "scroll",
                "chest",
                "smile",
                "eye contact",
                "reread",
                "avoid",
                "pause",
                "room",
                "closeness",
                "distance",
                "look",
                "kiss",
                "touch",
                "hand",
                "date",
                "message",
                "story",
                "conversation",
            )
            if opener and not (opener.startswith(concrete_starts) or any(cue in opener for cue in concrete_cues)):
                raise ValueError(f"Rejected abstract Brain Lens opener: {topic.title}")

    def _script_quality_issues(self, channel: ChannelConfig, topic: TopicCandidate) -> list[str]:
        issues: list[str] = []
        lines = list(topic.narration_beats or [])
        if not lines and topic.narration:
            lines = [part.strip() for part in re.split(r"(?<=[.!?])\s+", topic.narration) if part.strip()]
        captions = list(topic.visual_captions or [])
        combined = " ".join([topic.title or "", topic.subject or "", topic.narration or "", *captions])
        lowered = combined.lower()

        if channel.id == "ancient_history" and any(
            marker in combined for marker in ("\u00e2\u20ac", "\u00c3", "\u00c2")
        ):
            issues.append("Ancient script contains broken text encoding")

        bad_phrases = {
            "easy to miss status": "generic stakes filler",
            "who controlled the place, the food, the road or": "dangling generic history line",
            "usually a practical one who controlled": "generic clue filler",
            "strongest evidence around the evidence": "nonsense AI evidence phrase",
            "a tiny clue inside": "overused tiny-clue formula",
            "much bigger power struggle": "generic power-struggle filler",
            "3 hidden clues that explain": "overused hidden-clues formula",
            "the detail people skip": "overused AI opener",
            "the part most people remember": "overused AI opener",
            "famous story and the evidence": "generic myth-versus-evidence filler",
            "people can still.": "dangling incomplete Ancient line",
            "can still.": "dangling incomplete Ancient line",
            "it is the evidence": "generic Ancient caption",
            "anchor the scene": "generic Ancient instruction leaked into script",
            "follow the pressure": "creator instruction leaked into script",
            "show one physical clue": "creator instruction leaked into script",
            "keep the sequence concrete": "creator instruction leaked into script",
            "put the evidence on the map": "creator instruction leaked into script",
            "the visuals should": "creator instruction leaked into script",
            "sources around this clue": "bad subject replacement phrase",
            "first clue in the evidence": "bad subject replacement phrase",
            "not the headline it is": "generic Ancient filler phrase",
            "matters because control of land": "generic Ancient filler phrase",
            "belonged to whoever could organize builders": "generic Ancient filler phrase",
            "changed daily life through food, trade, labor": "generic Ancient list filler",
            "the aftermath of ": "generic Ancient aftermath filler",
            "the tangible clues are stone blocks": "generic Ancient evidence list filler",
            "archaeologists still study": "generic Ancient archaeology filler",
            "the strongest evidence around": "generic Ancient evidence filler",
            "the layout of ": "generic Ancient layout filler",
            "the real stakes of ": "generic Ancient stakes filler",
            "archaeologists read ": "generic Ancient evidence-list filler",
            "the decisive detail in ": "generic Ancient battle filler",
            "ancient accounts of ": "generic Ancient source filler",
        }
        for phrase, label in bad_phrases.items():
            if phrase in lowered:
                issues.append(label)

        for line in lines + captions:
            clean = re.sub(r"\s+", " ", line or "").strip()
            low = clean.lower()
            if channel.id == "ancient_history":
                for sentence in re.split(r"(?<=[.!?])\s+", clean):
                    sentence_bare = sentence.strip().rstrip(".!?")
                    if re.match(
                        r"^(?:when|while|although|because|unless|whereas)\b",
                        sentence_bare,
                        flags=re.IGNORECASE,
                    ) and not re.search(r"[,;:]", sentence_bare):
                        issues.append("Ancient script contains an orphaned dependent clause")
                        break
            if re.search(r"\b(or|and|with|from|to|of|the|a|an|that|which|because|still|inspect|can|rather|than|before|after|while|but|yet|so|instead)\.?$", low):
                issues.append("line ends on a dangling connector")
                break
            if re.search(r"\bafter (?:making|doing|having|seeing|hearing|feeling)\.?$", low):
                issues.append("line ends with an incomplete action")
                break
            if re.search(r"\b(?:something|anything|it|this|they) (?:is|are) worth\.?$", low):
                issues.append("line ends with an incomplete worth phrase")
                break
            if re.search(r"\bthey are\.?$", low):
                issues.append("line ends with an incomplete copula")
                break
            if re.search(r"\bjudgment catches\.?$", low):
                issues.append("line ends with an incomplete catches-up phrase")
                break
            if channel.id == "ancient_history" and re.search(
                r"\b(?:achaemenid|roman|persian|byzantine|maya|mauryan|assyrian|nabataean|punic|phoenician)\.?$",
                low,
            ):
                issues.append("line ends on an incomplete historical adjective")
                break
            if channel.id == "ancient_history" and re.search(
                r",\s*(?:\w+\s+)?(?:feet|foot|metres?|meters?)\s*\([^)]*\)\.?$",
                clean,
                flags=re.IGNORECASE,
            ):
                issues.append("line ends on a clipped historical measurement")
                break
            if channel.id == "ancient_history" and "trilithons two" in low:
                issues.append("historical sentence has a malformed apposition")
                break
            if 5 <= len(clean.split()) <= 16 and any(chunk in low for chunk in ("status, fear", "loyalty and survival", "what makes")):
                issues.append("caption reads like generic AI filler")
                break

        if getattr(topic, "content_kind", "short") == "video" and lines:
            long_caption_composer = SubtitleComposer(
                max_words_per_caption=8,
                max_chars_per_second=100.0,
            )
            long_caption_scenes = [
                SubtitleSegment(
                    start=float(index * 30),
                    end=float((index + 1) * 30),
                    text=line,
                )
                for index, line in enumerate(lines)
                if str(line or "").strip()
            ]
            long_caption_segments = long_caption_composer.caption_segments_from_scene_segments(
                long_caption_scenes
            )
            long_caption_issues = long_caption_composer.quality_issues(
                long_caption_segments,
                cps_tolerance=0.0,
                allow_clause_continuations=True,
            )
            structural_issues = [
                issue
                for issue in long_caption_issues
                if " reads at " not in issue
            ]
            if structural_issues:
                issues.append(
                    "Long video cannot form clean caption phrases: "
                    f"{structural_issues[0]}"
                )

        if channel.id == "ancient_history" and any(line.lower().strip().startswith("subscribe for") for line in lines):
            issues.append("Ancient script ends with a promotional CTA instead of a factual payoff")

        if channel.id == "brain_lens":
            brain_formula_bits = (
                "when the cue becomes clear, boundaries feel less cold",
                "open loops feel addictive because they mix hope",
                "the most attractive version of you can feel interest",
                "handing over control",
                "it usually starts with a tiny relationship cue",
                "restraint looks attractive because your mood",
                "tiny cue can make attraction feel intense",
                "real chemistry should not require guessing games",
            )
            if any(bit in lowered for bit in brain_formula_bits):
                issues.append("generic Brain Lens filler unrelated to the topic")
            opener = (lines[0] if lines else topic.hook or "").lower()
            topic_blob = f"{topic.title} {topic.subject}".lower()
            anchor_groups = (
                (("decision fatigue",), ("choose", "choice", "menu", "decision")),
                (("love bombing",), ("future", "attention", "affection", "intensity")),
                (("breadcrumb",), ("message", "reply", "disappear", "hope")),
                (("push pull",), ("close", "cold", "return", "pull")),
                (("mixed signals", "mixed attachment signals"), ("interested", "warmth", "distance", "signal", "disappear")),
                (("silent treatment",), ("silence", "reply", "space", "communication", "reconnect", "repair", "conflict")),
                (("emotional availability",), ("feel", "guess", "open", "emotion")),
                (("self respect in dating", "fear of being too much"), ("need", "editing", "honesty", "boundary", "keep")),
                (("flirting anxiety",), ("flirt", "eye contact", "word", "voice", "crush")),
                (("flirting body language", "micro flirting", "body language"), ("lean", "smile", "eye", "voice", "conversation")),
                (("eye contact",), ("eye", "gaze", "look")),
                (("almost kiss",), ("kiss", "lean", "lips", "closer")),
                (("almost relationships", "situationship"), ("text", "label", "mood", "partner", "connection")),
                (("texting anxiety", "attachment anxiety after texting", "reply time anxiety"), ("reply", "text", "timestamp", "message")),
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
            matched_anchor_group = False
            for topic_terms, hook_terms in anchor_groups:
                if any(term in topic_blob for term in topic_terms):
                    matched_anchor_group = True
                    if not any(term in opener for term in hook_terms):
                        issues.append("opening behavior does not match the promised topic")
                    break
            subject_tokens = {
                token
                for token in re.findall(r"[a-z]{4,}", (topic.subject or "").lower())
                if token not in {"about", "behind", "human", "people", "psychology"}
            }
            narration_lowered = (topic.narration or "").lower()
            subject_present = any(
                token in narration_lowered or (token.endswith("s") and token[:-1] in narration_lowered)
                for token in subject_tokens
            )
            if subject_tokens and not subject_present and not matched_anchor_group:
                issues.append("script never explains the promised subject")
            if "silent treatment" in topic_blob:
                silent_anchors = (
                    "silence", "reply", "space", "communication", "reconnect", "repair", "conflict", "withholding"
                )
                if sum(anchor in narration_lowered for anchor in silent_anchors) < 3:
                    issues.append("silent-treatment script lacks communication and repair specifics")
                if sum(term in narration_lowered for term in ("chemistry", "compatibility", "attraction")) >= 2:
                    issues.append("silent-treatment script drifts into unrelated attraction filler")
            ending = (lines[-1] if lines else "").strip().lower()
            if ending.startswith(("follow for", "subscribe for", "like and follow", "follow to")):
                issues.append("generic social CTA replaces the final payoff")

            if getattr(topic, "content_kind", "short") == "short" and lines:
                subtitle_config = getattr(
                    getattr(getattr(self, "config", None), "app", None),
                    "subtitles",
                    None,
                )
                caption_composer = SubtitleComposer(
                    max_words_per_caption=int(
                        getattr(subtitle_config, "max_words_per_caption", 6) or 6
                    ),
                    max_chars_per_second=100.0,
                )
                caption_scenes = [
                    SubtitleSegment(
                        start=float(index * 30),
                        end=float((index + 1) * 30),
                        text=line,
                    )
                    for index, line in enumerate(lines)
                    if str(line or "").strip()
                ]
                caption_segments = caption_composer.caption_segments_from_scene_segments(
                    caption_scenes
                )
                caption_issues = [
                    issue
                    for issue in caption_composer.quality_issues(
                        caption_segments,
                        cps_tolerance=0.0,
                        # Planning only: mid-phrase continuation is repaired at
                        # render by short caption reflow. Incomplete dangling tails
                        # still fail here.
                        allow_clause_continuations=True,
                    )
                    # Equal fake scene windows create 2-word flash artifacts on
                    # longer 50-59s scripts; render-time reflow owns that gate.
                    if "flash fragment" not in str(issue).lower()
                ]
                if caption_issues:
                    issues.append(
                        "Brain Lens Short cannot form clean caption phrases: "
                        f"{caption_issues[0]}"
                    )

        if channel.id == "ancient_history":
            ancient_formula_bits = (
                "one strange detail",
                "changes the whole story",
                "surviving evidence around",
                "points to a sharper story",
                "explains the danger",
                "the hidden cost of",
                "became impossible to ignore",
                "3 hidden clues that explain",
                "3 details about",
                "anchor the scene",
                "follow the pressure",
                "show one physical clue",
                "keep the sequence concrete",
                "put the evidence on the map",
                "the visuals should",
                "sources around this clue",
                "first clue in the evidence",
                "not the headline it is",
                "matters because control of land",
                "belonged to whoever could organize builders",
            )
            if any(bit in lowered for bit in ancient_formula_bits):
                issues.append("overused/generic Ancient History formula")
            subject = re.sub(r"[^a-z0-9\s]+", " ", (topic.subject or "")).strip().lower()
            words = re.findall(r"[a-z0-9']+", (topic.narration or "").lower())
            if getattr(topic, "content_kind", "short") == "short" and not (120 <= len(words) <= 148):
                issues.append(f"Ancient Short narration has {len(words)} words; target is 120-148")
            if getattr(topic, "content_kind", "short") == "short" and lines:
                subtitle_config = getattr(
                    getattr(getattr(self, "config", None), "app", None),
                    "subtitles",
                    None,
                )
                caption_composer = SubtitleComposer(
                    max_words_per_caption=int(
                        getattr(subtitle_config, "max_words_per_caption", 4) or 4
                    ),
                    max_chars_per_second=100.0,
                )
                caption_scenes = [
                    SubtitleSegment(
                        start=float(index * 30),
                        end=float((index + 1) * 30),
                        text=line,
                    )
                    for index, line in enumerate(lines)
                    if str(line or "").strip()
                ]
                caption_segments = caption_composer.caption_segments_from_scene_segments(
                    caption_scenes
                )
                caption_issues = caption_composer.quality_issues(
                    caption_segments,
                    cps_tolerance=0.0,
                    allow_clause_continuations=True,
                )
                if caption_issues:
                    issues.append(
                        "Ancient Short cannot form clean caption phrases: "
                        f"{caption_issues[0]}"
                    )
            pronunciation_risks = {
                "thermopylae",
                "teutoburg",
                "byzantine",
                "byzantines",
                "carthage",
                "carthaginian",
                "nazca",
                "nasca",
                "antikythera",
                "hammurabi",
            }
            if any(term in lowered for term in pronunciation_risks) and len(words) <= 55:
                issues.append("pronunciation-risk topic needs more context and slower narration")
            if subject and len(words) <= 130:
                narration_lowered = (topic.narration or "").lower()
                count = len(re.findall(r"\b" + re.escape(subject) + r"\b", narration_lowered))
                if count >= 7:
                    issues.append("subject name repeated too many times")
            if ("nazca" in lowered or "nasca" in lowered) and "peru" not in lowered:
                issues.append("Nazca/Nasca script missing Peru location anchor")
            if "carthage" in lowered and not any(term in lowered for term in ("tunisia", "punic", "phoenician", "north african")):
                issues.append("Carthage script missing North Africa/Punic context")
            if "greek fire" in lowered and not any(term in lowered for term in ("byzantine", "water", "naval", "constantinople", "siphon")):
                issues.append("Greek Fire script missing concrete Byzantine/naval context")
            if "thermopylae" in lowered and not any(term in lowered for term in ("greece", "pass", "spartan", "persian")):
                issues.append("Thermopylae script missing Greece/pass/Spartan/Persian context")
            if "teutoburg" in lowered and not any(term in lowered for term in ("germania", "germany", "roman", "legion", "varus")):
                issues.append("Teutoburg script missing Germania/Roman legion context")
            if "petra" in lowered and not any(term in lowered for term in ("jordan", "nabataean", "trade route", "sandstone")):
                issues.append("Petra script missing Jordan/Nabataean context")

        return list(dict.fromkeys(issues))

    def _ensure_visual_quality(self, sources: list[dict]) -> None:
        real_sources = [
            source
            for source in sources
            if source.get("source") not in {
                "local_documentary_diagram",
                "local_fact_card",
                "stable_horde",
                "pollinations_ai",
            }
        ]
        ai_sources = [s for s in sources if s.get("source") in {"stable_horde", "pollinations_ai"}]
        if len(real_sources) >= 1 or len(ai_sources) >= 2:
            return
        # Say what was actually found. "Not enough usable visuals" on its own
        # sent people looking for a network fault when the real answer was in
        # the counts -- it took hours to find that once.
        from collections import Counter

        tally = Counter(str(source.get("source") or "unknown") for source in sources)
        breakdown = ", ".join(f"{count} {name}" for name, count in tally.most_common()) or "nothing"
        raise RuntimeError(
            "Visual quality gate failed: not enough usable visuals found. "
            f"Collected {len(sources)} ({breakdown}); needs 1 real source or 2 generated. "
            "A run of generated-only sources usually means the stock and archive "
            "searches returned nothing for this subject."
        )

    def _fetch_candidate_visuals(
        self,
        topic: TopicCandidate,
        run_dir: Path,
        count: int,
        visual_style: str,
    ) -> tuple[list[Path], list[dict], str]:
        try:
            images, sources = self.image_fetcher.fetch(
                topic=topic,
                run_dir=run_dir,
                count=count,
                visual_style=visual_style,
            )
            self._ensure_visual_quality(sources)
        except RuntimeError as exc:
            return [], [], str(exc)
        return images, sources, ""

    def _ancient_visual_preflight_issue(
        self,
        topic: TopicCandidate,
        sources: list[dict],
        content_kind: str,
        source_visual_qa: dict | None = None,
    ) -> str | None:
        if content_kind not in {"short", "video"}:
            return None

        generated_sources = {
            "local_documentary_diagram",
            "local_fact_card",
            "pollinations_ai",
            "stable_horde",
        }
        if content_kind == "short":
            fact_card_count = sum(
                1
                for item in sources
                if str(item.get("source") or "").strip().lower() == "local_fact_card"
            )
            if fact_card_count > self.ANCIENT_SHORT_MAX_FACT_CARD_FALLBACKS:
                return (
                    f"Ancient Short contains {fact_card_count} local fact-card fallback(s); "
                    f"maximum is {self.ANCIENT_SHORT_MAX_FACT_CARD_FALLBACKS}"
                )
            if any(
                int(item.get("scene_index") or 0) <= 2
                for item in sources
                if str(item.get("source") or "").strip().lower() == "local_fact_card"
            ):
                return "Ancient Short uses a local fact-card fallback in the opening"

            verified_real_sources: list[dict] = []
            for item in sources:
                source_type = str(item.get("source") or "").strip().lower()
                media_url = str(item.get("url") or "").strip()
                source_page = str(item.get("source_page") or "").strip()
                license_name = str(item.get("license") or "").strip().lower()
                if (
                    source_type not in generated_sources
                    and media_url.startswith(("http://", "https://"))
                    and source_page.startswith(("http://", "https://"))
                    and str(item.get("source_page_verified") or "").strip().lower() == "true"
                    and license_name not in {"", "unknown"}
                ):
                    verified_real_sources.append(item)

            unique_verified: dict[str, dict] = {}
            for item in verified_real_sources:
                identity = self.image_fetcher._canonical_media_url(str(item.get("url") or ""))
                if identity:
                    unique_verified.setdefault(identity, item)
            required_real = self.ANCIENT_SHORT_MIN_VERIFIED_REAL_VISUALS
            if str(os.getenv("YT_CONTINUITY_RECOVERY", "")).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                # Continuity recovery already chose a sourced pack; do not leave a
                # multi-hour gap because Commons returned 5 instead of 6 stills.
                required_real = min(required_real, 4)
            if len(unique_verified) < required_real:
                return (
                    f"only {len(unique_verified)} unique verified real Ancient visuals; "
                    f"needs at least {required_real}"
                )

            relevance_terms = self.image_fetcher._strict_ancient_terms(
                topic.title,
                subject=topic.subject,
            )
            relevant_verified = {
                identity: item
                for identity, item in unique_verified.items()
                if self.image_fetcher._candidate_matches_terms(
                    str(item.get("url") or ""),
                    item,
                    relevance_terms,
                )
            }
            irrelevant_count = len(unique_verified) - len(relevant_verified)
            if irrelevant_count:
                return (
                    f"{irrelevant_count} verified real Ancient Short visual(s) do not "
                    "clearly match the selected subject"
                )
            if len(relevant_verified) < required_real:
                return (
                    f"only {len(relevant_verified)} unique subject-relevant verified real visuals; "
                    f"needs at least {required_real}"
                )

            opening_source = next(
                (item for item in sources if int(item.get("scene_index") or 0) == 1),
                sources[0] if sources else {},
            )
            opening_identity = self.image_fetcher._canonical_media_url(
                str(opening_source.get("url") or "")
            )
            if opening_identity not in relevant_verified:
                return "Ancient Short opener is not verified real subject-relevant evidence"
            return None

        opening_source = next(
            (item for item in sources if int(item.get("scene_index") or 0) == 1),
            sources[0] if sources else {},
        )
        if not 16 <= len(sources) <= 24:
            return f"Ancient long video has {len(sources)} visual scenes; needs 16 to 24"
        fact_card_count = sum(
            1
            for item in sources
            if str(item.get("source") or "").strip().lower() == "local_fact_card"
        )
        if fact_card_count:
            return (
                f"Ancient long video contains {fact_card_count} local fact-card fallback(s); "
                "zero are allowed"
            )
        opening_type = str(opening_source.get("source") or "").strip().lower()
        opening_url = str(opening_source.get("url") or "").strip()
        if opening_type in generated_sources or not opening_url.startswith(("http://", "https://")):
            return "opening scene lacks a real historical photograph, artifact, site, map, or archive image"

        unique_urls = {
            str(item.get("url") or "").strip()
            for item in sources
            if str(item.get("url") or "").strip()
        }
        if len(unique_urls) < 12:
            return f"only {len(unique_urls)} unique visual sources; needs at least 12"

        archival_sources = [
            item
            for item in sources
            if str(item.get("source") or "").strip().lower() not in generated_sources
            and str(item.get("url") or "").strip().startswith(("http://", "https://"))
            and str(item.get("source_page") or "").strip().startswith(("http://", "https://"))
            and str(item.get("source_page_verified") or "").strip().lower() == "true"
            and str(item.get("license") or "").strip().lower() not in {"", "unknown"}
        ]
        unique_archival_urls = {
            str(item.get("url") or "").strip()
            for item in archival_sources
        }
        if len(unique_archival_urls) < 7:
            return f"only {len(unique_archival_urls)} unique real historical visuals; needs at least 7"

        relevance_terms = self.image_fetcher._strict_ancient_terms(
            topic.title,
            subject=topic.subject,
        )
        relevant_archival_urls: set[str] = set()
        for item in archival_sources:
            if self.image_fetcher._candidate_matches_terms(
                str(item.get("url") or ""),
                item,
                relevance_terms,
            ):
                relevant_archival_urls.add(str(item.get("url") or "").strip())
        irrelevant_archival_urls = unique_archival_urls - relevant_archival_urls
        if irrelevant_archival_urls:
            return f"{len(irrelevant_archival_urls)} real visual source(s) do not clearly match the selected subject"
        if len(relevant_archival_urls) < 3:
            return f"only {len(relevant_archival_urls)} real historical visuals clearly match the selected subject"
        reuse_count = sum(bool(item.get("reused_for_scene")) for item in sources)
        if reuse_count > 2:
            return f"Ancient long video reuses {reuse_count} source scenes; maximum is 2"
        if source_visual_qa:
            dark_real_ratio = float(source_visual_qa.get("dark_real_sample_ratio") or 0.0)
            if dark_real_ratio > 0.15:
                return (
                    f"real historical source frames are too dark ({dark_real_ratio:.0%}); "
                    "maximum is 15%"
                )
            exact_duplicates = sum(
                1
                for pair in source_visual_qa.get("near_duplicate_cross_source_pairs", [])
                if isinstance(pair, dict) and int(pair.get("distance") or 0) == 0
            )
            if exact_duplicates > 2:
                return (
                    f"source contact sheet contains {exact_duplicates} exact duplicate frame pairs; "
                    "maximum is 2"
                )
        return None

    @staticmethod
    def _brain_visual_action(searchable_asset: str) -> str:
        lowered = str(searchable_asset or "").lower()
        if "holding hands" in lowered or "hold hands" in lowered:
            return "affection"
        tokens = re.findall(r"[a-z0-9]+", lowered)
        action_clusters = (
            ("affection", ("hug", "embrac", "kiss", "danc", "cuddle")),
            ("conflict", ("argu", "fight", "conflict", "apart", "ignore", "breakup")),
            ("conversation", ("talk", "conversation", "discuss")),
            ("phone", ("phone", "smartphon", "text", "message")),
        )
        for action, prefixes in action_clusters:
            if any(token.startswith(prefix) for token in tokens for prefix in prefixes):
                return action
        return ""

    def _brain_visual_preflight_issue(
        self,
        topic: TopicCandidate,
        sources: list[dict],
    ) -> str | None:
        if not sources:
            return "Brain Lens has no visual sources"

        content_kind = getattr(topic, "content_kind", "short")
        format_label = "Short" if content_kind == "short" else "long video"
        ai_generated_types = {"pollinations_ai", "stable_horde"}
        ai_generated = [
            item
            for item in sources
            if str(item.get("source") or "").strip().lower() in ai_generated_types
        ]
        ai_generated_count = len(ai_generated)
        if content_kind == "short" and ai_generated_count:
            return (
                f"Brain Lens {format_label} contains {ai_generated_count} AI-generated visual(s); "
                "zero are allowed"
            )
        if content_kind == "video" and ai_generated_count > self.BRAIN_LONG_MAX_AI_GENERATED_VISUALS:
            return (
                f"Brain Lens {format_label} contains {ai_generated_count} AI-generated visual(s); "
                f"maximum is {self.BRAIN_LONG_MAX_AI_GENERATED_VISUALS}"
            )
        if content_kind == "video" and any(
            int(item.get("scene_index") or 0) <= 2 for item in ai_generated
        ):
            return f"Brain Lens {format_label} uses an AI-generated visual in the opening"

        fact_cards = [
            item
            for item in sources
            if str(item.get("source") or "").strip().lower() == "local_fact_card"
        ]
        allowed_fact_cards = self.BRAIN_SHORT_MAX_FACT_CARD_FALLBACKS if content_kind == "short" else 0
        if len(fact_cards) > allowed_fact_cards:
            return (
                f"Brain Lens {format_label} contains {len(fact_cards)} local fact-card fallback(s); "
                f"maximum is {allowed_fact_cards}"
            )
        if any(int(item.get("scene_index") or 0) <= 2 for item in fact_cards):
            return f"Brain Lens {format_label} uses a local fact-card fallback in the opening"

        searchable_sources = [
            re.sub(
                r"[^a-z0-9]+",
                " ",
                f"{item.get('url', '')} {item.get('asset_title', '')} {item.get('artist', '')}".lower(),
            ).strip()
            for item in sources
        ]
        child_markers = (
            " child", " children", " kid", " toddler", " baby", " schoolboy", " schoolgirl",
            " teen", "little girl", "young girl", "small girl", "little boy", "young boy",
        )
        if any(
            any(marker in searchable for marker in child_markers)
            or re.search(r"\b(?:boy|girl)\b", searchable)
            for searchable in searchable_sources
        ):
            return "Brain Lens adult topic contains a child-focused visual"

        off_tone_markers = (
            "elderly", "senior woman", "senior man", "pregnancy test", "thumbs down",
            "flower petal tears", "painted tears", "throwing documents", "screaming",
            "scream", "shouting", "pulling hair", "stretching neck", "eyes closed",
            "sleeping", "ai25 studio", "cheating", "infidelity", "affair",
        )
        if any(any(marker in searchable for marker in off_tone_markers) for searchable in searchable_sources):
            return "Brain Lens contains an off-tone stock visual"

        topic_blob = f"{topic.subject} {topic.title} {topic.narration}".lower()
        relationship_topic = any(
            term in topic_blob
            for term in (
                "relationship", "dating", "attachment", "attraction", "chemistry", "flirt",
                "kiss", "breadcrumb", "situationship", "push pull", "mixed signal",
                "emotional availability", "silent treatment", "future faking", "benching",
                "slow fading", "ghosting", "orbiting", "relationship pacing", "mutual effort",
                "crush idealization", "limerence", "jealousy", "friends with benefits",
            )
        )
        if not relationship_topic:
            return None

        relationship_markers = (
            "couple", "relationship", "partner", "dating", " date ", "romantic",
            "kissing", " kiss ", "hug", "holding hands", "argument", "arguing",
            "breakup", "breaking up", "man and woman", "woman and man", "male and female",
            "lovers", "boyfriend", "girlfriend", "husband", "wife",
        )
        relationship_visuals = sum(
            HybridMediaFetcher._is_brain_relationship_asset(
                str(item.get("url") or ""),
                item,
            )
            for item in sources
        )
        required = max(4, (len(sources) * 2 + 2) // 3)
        if relationship_visuals < required:
            return (
                "Brain Lens relationship topic has only "
                f"{relationship_visuals}/{len(sources)} clearly relevant relationship visuals; "
                f"needs at least {required}"
            )

        opening_text = (topic.narration_beats or [topic.hook or topic.narration or ""])[0].lower()
        opening_sources = [
            searchable
            for searchable, item in zip(searchable_sources, sources)
            if int(item.get("scene_index") or 0) == 1
        ] or searchable_sources[:1]
        if topic.content_kind == "video" and not any(
            any(marker in f" {searchable} " for marker in relationship_markers)
            for searchable in opening_sources
        ):
            return "Brain Lens relationship long-video hook lacks a clearly relevant relationship visual"
        action_counts: Counter[str] = Counter(
            action
            for searchable in searchable_sources
            if (action := self._brain_visual_action(searchable))
        )
        if action_counts:
            repeated_action, repeated_count = action_counts.most_common(1)[0]
            max_repeated_action = max(5, (len(sources) + 1) // 2)
            if repeated_count > max_repeated_action:
                return (
                    f"Brain Lens repeats {repeated_action} footage in "
                    f"{repeated_count}/{len(sources)} visual sources"
                )
        if any(term in opening_text for term in ("text", "message", "reply", "phone")):
            if not any(
                any(marker in searchable for marker in ("phone", "text", "message", "smartphone"))
                for searchable in opening_sources
            ):
                return "Brain Lens phone/message hook lacks a phone-relevant opening visual"
        return None

    def _brain_short_deterministic_fallback(
        self,
        channel: ChannelConfig,
        avoid_titles: set[str],
    ) -> TopicCandidate | None:
        """Return a behavior-first Short when stochastic topic drafts all fail.

        These are complete editorial structures, not a relaxed validation path:
        every profile still passes the normal title, script, source, and visual
        preflight gates before rendering.
        """
        profiles: tuple[tuple[str, str, tuple[str, ...]], ...] = (
            (
                "Micro Flirting",
                "Micro Flirting: The Difference Between a Spark and Simple Friendliness",
                (
                    "They hold eye contact longer. Then they mirror your smile. Your body notices.",
                    "That cluster can feel like flirting. Friendliness can look similar. Context still matters here.",
                    "Look for returned signals. Notice chosen closeness and engaged questions. Watch their next move closely.",
                    "But one charged second creates chemistry. Repeated behavior is stronger evidence. Consistency beats intensity.",
                    "Offer one respectful signal. Leave space for choice. Calm confidence protects both people.",
                    "Reciprocity and consent make attraction clearer. Pressure creates noise. A real spark returns.",
                ),
            ),
            (
                "Reply Time Anxiety",
                "Late Replies and Attachment Anxiety: What One Study Found",
                (
                    "Their reply arrives late. Your mind starts filling the silence. One timestamp proves nothing.",
                    "A study surveyed 302 partnered undergraduates. Participants reported attachment and messaging patterns.",
                    "Anxious participants wanted more messages. Their partners sent fewer messages. That gap can feel personal.",
                    "They replied faster too. But the study found associations. It did not prove causes.",
                    "Context and messaging preferences still vary. Check urgency and usual follow-through. Busy days change response speed.",
                    "Ask clearly when context is missing. Judge the pattern, not one timestamp.",
                ),
            ),
            (
                "Emotional Safety in Dating",
                "Emotional Safety in Dating: The Pattern to Watch Before You Invest More",
                (
                    "You stop measuring promises. Notice the pace, effort, and care. Your body notices mismatches.",
                    "Name the visible cue. Then name your interpretation. Keep those two separate.",
                    "Ask three questions. Did it repeat? Did their actions match? Did repair follow?",
                    "Did you feel clearer afterward? Reliable patterns need time. Safety grows through predictable care.",
                    "Name what happened. Keep the relationship context in view. One sweet moment rarely proves consistency.",
                    "The spark starts everything. Patterns decide what deserves trust. Calm clarity protects your choices.",
                ),
            ),
            (
                "Conflict Repair",
                "Conflict Repair: The Pattern That Makes Attraction Safer",
                (
                    "You watch the next move after conflict. A repair pattern becomes visible there.",
                    "Healthy repair names the specific hurt, without turning one mistake into a character verdict.",
                    "Ownership follows. A real apology explains impact, not merely intention behind the behavior.",
                    "Warm words soothe tonight, but changed action rebuilds trust over time.",
                    "Both people need room to speak, pause, and return. Repair is cooperation, not instant forgiveness.",
                    "Choose clarity over punishment. Chemistry survives conflict through honesty, boundaries, and consistent change over time, in daily life.",
                ),
            ),
            (
                "Mutual Effort in Dating",
                "Mutual Effort in Dating: The Signal That Saves Your Energy",
                (
                    "You stop chasing the plan. Their effort starts creating one too. That shift tells you something.",
                    "Mutual effort appears in ordinary choices: initiating contact, remembering details, and making time.",
                    "One generous date proves little. Watch whether attention returns across busy weeks and disappointments.",
                    "Balanced effort means both people carry part of the emotional weight, even differently.",
                    "Ask whether you feel chosen or constantly auditioning. Your answer reveals the pattern.",
                    "Attraction creates momentum. Reciprocity keeps it moving. Consistent effort is warmer than promises.",
                ),
            ),
            (
                "Relationship Pacing",
                "Relationship Pacing: Let Trust Catch Up With Chemistry",
                (
                    "You notice the pace changing. Relationship pacing can hide information. Your nervous system needs room to breathe.",
                    "Healthy pacing lets curiosity grow without demanding certainty, constant access, or dramatic promises.",
                    "Ask what is known. Separate shared experiences from the future story you actually know imagination is writing.",
                    "Watch how they handle a slower answer, a boundary, or a changed plan without blame.",
                    "Enjoy chemistry while keeping daily routines, close friends, and judgment active.",
                    "Good pacing gives trust enough time to become real evidence instead of fantasy.",
                ),
            ),
            (
                "Boundary Response",
                "Boundary Response: What Their Next Move Actually Reveals",
                (
                    "Their plan crosses your boundary. Their response reveals real character.",
                    "A respectful response stays warm. They may ask once, then accept your answer.",
                    "Pressure looks different. Guilt, sulking, repeated bargaining, or punishment turns preference into a test.",
                    "One disappointed reaction needs context. A repeated pattern shows how they handle your separate needs.",
                    "Name the boundary. Do not apologize. Then watch their behavior when the moment passes.",
                    "Attraction feels safer when no remains safe. Respectful, consistent responses are the signal worth trusting next.",
                ),
            ),
            (
                "Curiosity and Interest",
                "Curiosity and Interest: The Difference Between Care and Charm",
                (
                    "Their question returns to one detail you mentioned. Curiosity makes real interest visible there.",
                    "Real interest makes room for your answer instead of rushing toward another story.",
                    "Charm can feel electric, but charm often keeps attention on the person delivering it.",
                    "Notice whether questions deepen naturally. Watch whether they remember preferences, boundaries, and important context.",
                    "You do not need an interview. Balanced conversation should move both ways and leave space.",
                    "Chemistry catches attention. Curious follow-through builds clarity. Real interest stays curious about you.",
                ),
            ),
            (
                "Consistency After Intimacy",
                "Consistency After Intimacy: The Pattern That Reveals Emotional Availability",
                (
                    "Their closeness changes after intimacy. Watch whether emotional consistency remains when the charged moment ends.",
                    "Healthy interest usually keeps ordinary care intact: communication, respect, plans, and emotional presence.",
                    "Distance after intimacy has many causes. One quieter day does not prove regret.",
                    "Look for the wider pattern. Do warmth and honesty return when the excitement settles?",
                    "Ask directly if the shift continues. Clear words protect you better than silent detective work.",
                    "Intensity creates closeness for one night. Consistent care shows whether intimacy has somewhere safe to go.",
                ),
            ),
            (
                "Jealousy and Clarity",
                "Jealousy and Clarity: A Signal, Not Proof",
                (
                    "You feel jealousy before facts arrive. Your body reacts first. The feeling alone is not evidence.",
                    "First name the trigger: secrecy, comparison, exclusion, or an old fear waking up again.",
                    "Then separate events from imagination. Both matter, and they need different conversations.",
                    "Ask for context without accusation. Watch whether the answer is clear, respectful, and consistent later.",
                    "Do not ignore deception. Also do not punish someone for a story you never checked.",
                    "Jealousy is a signal. Trust grows through facts, boundaries, and honest repair.",
                ),
            ),
            (
                "Direct Interest",
                "Direct Interest: The Dating Signal That Removes Guesswork",
                (
                    "Their direct interest creates the next plan without forcing you to decode three days of vague messages.",
                    "Clear interest is consistent intention that leaves room for both lives.",
                    "Look for concrete choices: initiating, confirming, following through, and responding honestly when plans change.",
                    "Shy people can show clarity. Their effort remains visible in a different style.",
                    "Mixed moments happen. Chronic ambiguity keeps you working harder than the connection itself does.",
                    "You deserve attraction with enough clarity to breathe. Interest becomes meaningful when action keeps returning.",
                ),
            ),
            (
                "Apology Follow-Through",
                "Apology Follow-Through: What Changes After the Words",
                (
                    "Their apology sounds beautiful. The real test begins when the same situation appears again.",
                    "A useful apology names the action, recognizes the impact, and avoids making you comfort them.",
                    "Then behavior must change. Perfect improvement is unrealistic, but sincere effort should become visible.",
                    "Watch for defensiveness, repeated excuses, or apologies that only arrive when you start leaving.",
                    "State what repair would look like. Keep the request specific and shared.",
                    "Words can reopen the door. Changed behavior gives damaged trust new reasons. Then closeness can return.",
                ),
            ),
            (
                "First Date Nerves",
                "First-Date Nerves: The Signal That Stops You Guessing",
                (
                    "You leave a good first date. Nerves can make silence loud. Your mind starts filling empty gaps.",
                    "Interest becomes a clear new plan. Watch for that next small step.",
                    "Warmth matters after a good date. Suggested times matter even more.",
                    "One quiet day means little. But several vague days mean more.",
                    "Do not chase certainty tonight. Leave room for their choices.",
                    "Notice the wider pattern without panic. Chemistry opens the door. Follow-through shows what lasts. Keep your standards steady. That is useful dating information.",
                ),
            ),
            (
                "Secure Attraction",
                "Secure Attraction: The Green Flag That Makes Dating Feel Safer",
                (
                    "You leave the date feeling calmer. That shift deserves attention. Secure attraction feels steady.",
                    "Clear words create room for trust. Patient listening lowers tension. Care repeats when life gets busy.",
                    "Charm can be real. But charm alone cannot promise safety. Ordinary pressure reveals the pattern.",
                    "Watch their response to boundaries. Notice their response to delays. Their choices answer more than promises.",
                    "Perfection is never the standard. Consistency lets your body relax.",
                    "Attraction feels stronger with respect. Calm can become real chemistry. That is useful dating information.",
                ),
            ),
            (
                "Canceled Date Anxiety",
                "Canceled Date Anxiety: When a Reschedule Is Still a Good Sign",
                (
                    "They suddenly cancel tonight's date plan. Then quickly offer another time. That concrete detail changes everything.",
                    "Normal plans sometimes move. Interest returns with real specifics. Canceled-date anxiety still spikes.",
                    "One cancellation proves little. Repeated vagueness means more. Context separates those patterns.",
                    "Look for day and time. Then watch their follow-through. Specific plans lower anxiety.",
                    "Respect a busy life. Keep room for your needs. No punishment is required.",
                    "A clear reschedule shows care. Chronic ambiguity drains energy. Clarity makes dating easier to read.",
                ),
            ),
            (
                "Flirty Banter",
                "Flirty Banter: The Signal That Feels Fun Without Becoming Pressure",
                (
                    "They start flirty banter. Then they watch your face. Your smile returns the play.",
                    "Good banter moves both ways. Each person adds energy. Either person can redirect.",
                    "A sharp joke feels different. One person keeps shrinking. The other keeps pushing.",
                    "Notice how their pacing changes. Respect follows every small signal. Pressure ignores visible discomfort.",
                    "Return one playful signal. Then leave comfortable space. Their response reveals the pattern.",
                    "Mutual laughter builds attraction. Clear consent keeps it safe. Respect makes confidence feel warmer.",
                ),
            ),
            (
                "Relationship Check-Ins",
                "The Relationship Check-In That Prevents Small Doubts From Growing",
                (
                    "You replay one relationship doubt. The conversation has already ended. Your body still stays tense.",
                    "Name one visible moment. Name one honest feeling. Ask one clear question.",
                    "Describe impact without blame. Then listen for curiosity. Defensiveness reveals a different unhealthy pattern.",
                    "Useful answers create shared information. They do not need perfection. Both people still need space.",
                    "Check in before resentment grows. Small repairs cost less. Silence can harden confusion.",
                    "Clarity protects closeness. Honest check-ins build trust. Respect keeps connection emotionally safe.",
                ),
            ),
            (
                "Post-Date Anxiety",
                "Post-Date Anxiety: What to Do Before You Read Into Every Silence",
                (
                    "The date ends warmly. Post-date anxiety starts replaying. Every small sentence feels important.",
                    "Post-date anxiety magnifies silence. Excitement and uncertainty collide together. Old memories may join them.",
                    "Pause before decoding anything. Return to visible facts first. Notice their words and actions.",
                    "A warm date matters. But it cannot predict everything. The next action adds information.",
                    "Keep your evening intact. Call friends if needed. Let your routine continue.",
                    "Interest grows with room. You need no instant answer. Calm clarity protects your energy.",
                ),
            ),
            (
                "Growing Attraction",
                "Growing Attraction: Why Slow Interest Can Be More Reliable",
                (
                    "They smile after each meeting. Yet you still stay curious. That can be attraction growing.",
                    "Repeated ease slowly builds interest. Shared humor lowers pressure. Reliable care adds warmth.",
                    "Instant intensity can feel exciting. It is not required. Depth can arrive slowly.",
                    "Notice your curiosity after meetings. Does it keep expanding? Or need constant drama?",
                    "Give connection enough time. Watch ordinary moments together. They reveal real compatibility.",
                    "Slow interest is not boring. Safety can deepen chemistry. Consistency makes attraction easier to trust.",
                ),
            ),
            (
                "Honest Attraction",
                "How Honest Attraction Makes Dating Less Confusing",
                (
                    "They clearly enjoyed seeing you. Then another easy meeting appears. That pattern shows honest attraction.",
                    "Words match their clear plans. Behavior consistently tells the same story. Mind-reading then becomes unnecessary.",
                    "One early date proves little. Clear interest still respects your time. Warmth remains visible afterward.",
                    "Notice attention during inconvenience. Does care stay visible? Or disappear without excitement?",
                    "Ask what they want. Keep the question light. Direct answers reduce confusion.",
                    "Real connection grows clearer. Consistency protects your energy. Trust needs returning effort.",
                ),
            ),
            (
                "Respect After Rejection",
                "Respect After Rejection: The Dating Signal That Shows Real Emotional Maturity",
                (
                    "They hear your rejection and pause. Their tone stays warm. That response reveals more than polished flirting.",
                    "Disappointment is normal. Guilt, bargaining, or sudden cruelty turns attraction into pressure.",
                    "A mature response protects dignity on both sides. It accepts choice without demanding an explanation.",
                    "Watch what happens later. Romance is no longer guaranteed, yet respect stays steady.",
                    "You can appreciate interest without owing connection. Boundaries are information, not invitations to negotiate.",
                    "Real confidence handles no gracefully. Respect after rejection is a clear dating green flag.",
                ),
            ),
            (
                "Planning Initiative",
                "Planning Initiative: The Dating Signal That Separates Interest From Convenience",
                (
                    "They take planning initiative. They suggest a day, choose a place, and confirm. You stop carrying every detail.",
                    "Planning initiative is ordinary effort made visible. It shows interest has moved beyond vague enthusiasm.",
                    "One organized date proves little. Notice whether effort returns when schedules become inconvenient.",
                    "Healthy initiative includes your preferences. Control decides for you; care plans with you.",
                    "Offer your share, then leave room for theirs. Balanced effort should not require reminders.",
                    "Chemistry creates excitement. Shared planning turns attraction into mutual effort both people trust.",
                ),
            ),
            (
                "Warmth After Vulnerability",
                "Warmth After Vulnerability: The Signal That Makes Emotional Intimacy Safer",
                (
                    "You share something personal. They slow down, listen, and treat it carefully afterward.",
                    "Warm care matters. But real care never turns your disclosure against you.",
                    "One response matters. The stronger signal appears later through privacy, patience, and respect.",
                    "Notice their response. Curiosity feels different from control. Safe closeness never demands every detail.",
                    "Share gradually and watch the pattern. Trust should grow alongside evidence, not ahead of it.",
                    "Attraction deepens when honesty stays protected. Care after vulnerability shows where intimacy can safely grow.",
                ),
            ),
            (
                "Comfortable Silence",
                "Comfortable Silence: When Quiet Becomes a Dating Green Flag",
                (
                    "The silence arrives. Neither person panics. Comfortable silence can reveal growing ease.",
                    "Silence is not automatically chemistry, because people may be tired, distracted, shy, or simply thinking.",
                    "The useful clue is context. Warm eye contact and easy reentry make quiet feel connected.",
                    "Cold withdrawal feels different. It creates uncertainty, punishment, or pressure to perform.",
                    "Let one pause breathe. Notice whether conversation returns naturally without anyone chasing reassurance.",
                    "Real comfort needs no constant performance. Shared quiet can become a gentle sign of emotional safety.",
                ),
            ),
            (
                "Meeting Friends",
                "Meeting Friends: What This Dating Step Can Signal but Cannot Prove",
                (
                    "They invite you into a casual plan with close friends. That adds context beyond private chemistry.",
                    "Meeting friends adds social context. But one invitation never guarantees commitment or compatibility.",
                    "Watch how they introduce you. Respectful clarity feels different from secrecy, jokes, or confusing labels.",
                    "Notice their behavior in the group. Do warmth and consideration remain when attention is divided?",
                    "Keep expectations grounded. Social inclusion is information, not a relationship contract.",
                    "Private attraction is easy to stage. Consistent respect across settings makes interest more believable.",
                ),
            ),
            (
                "Playful Teasing Boundaries",
                "Playful Teasing Boundaries: When Flirting Stays Fun and Respectful",
                (
                    "They start playful teasing. They notice your smile. Then soften if the joke stops feeling fun.",
                    "Playful teasing builds connection when both people can laugh, answer back, or redirect safely.",
                    "A joke becomes pressure when one person shrinks while the other keeps escalating.",
                    "Look for adjustment. Respectful flirting responds to discomfort instead of arguing that you misunderstood.",
                    "Name the line once. Their next choice tells you more than their clever explanation.",
                    "Good banter creates energy without taking dignity. Attraction feels warmer when humor leaves everyone safe.",
                ),
            ),
            (
                "Busy Week Consistency",
                "Busy Week Consistency: The Dating Signal Hidden Inside a Chaotic Schedule",
                (
                    "Their week becomes chaotic, yet busy-week consistency appears in one clear message.",
                    "Busy-week consistency does not mean constant texting. It means expectations and effort remain understandable.",
                    "One delayed reply proves nothing. Repeated vanishing without context creates a different pattern.",
                    "Notice whether they reconnect with specifics. Real interest usually finds a realistic next step.",
                    "Keep your routine moving. Do not turn every quiet hour into a verdict about your worth.",
                    "Healthy attraction survives ordinary life. Clear communication during busy weeks makes interest easier to trust.",
                ),
            ),
            (
                "Follow-Up Questions",
                "Follow-Up Questions: The Small Signal That Shows Someone Was Listening",
                (
                    "They remember your interview and ask how it went. That thoughtful follow-up makes attention visible.",
                    "Follow-up questions show listening. Curiosity should feel mutual, not like an interrogation.",
                    "Charm collects exciting facts. Care remembers context, preferences, worries, and important boundaries later.",
                    "Watch whether your answer gets room. Genuine interest does not rush toward their own performance.",
                    "Return curiosity without testing them. Balanced conversation should reveal both people gradually over time.",
                    "Attraction catches attention quickly. Thoughtful follow-up builds trust and shows whether attention can become real connection.",
                ),
            ),
        )
        normalized_avoids = {
            re.sub(r"\s+", " ", str(item or "").lower()).strip()
            for item in avoid_titles
            if item
        }
        selected: tuple[str, str, tuple[str, ...]] | None = None
        for profile in profiles:
            subject, title, _ = profile
            subject_key = subject.lower()
            title_key = title.lower()
            if any(
                subject_key == avoid
                or title_key == avoid
                or (len(subject_key) > 8 and subject_key in avoid)
                for avoid in normalized_avoids
            ):
                continue
            selected = profile
            break
        if selected is None:
            return None

        subject, title, beats = selected
        writer = getattr(self, "script_writer", None)
        min_words = int(getattr(writer, "_BRAIN_SHORT_MIN_WORDS", 122) or 122) if writer else 122
        max_words = int(getattr(writer, "_BRAIN_SHORT_MAX_WORDS", 142) or 142) if writer else 142
        spoken_beats = self._pad_short_beats_to_word_window(
            list(beats),
            min_words=min_words,
            max_words=max_words,
            pad_lines=(
                f"Stay with the visible pattern around {subject.lower()} instead of inventing drama from one moment.",
                "Separate what you saw from what you assumed before you escalate the story.",
                "Take one calm next step that protects self-respect and leaves room for choice.",
                "If the same pattern repeats across ordinary days, trust that consistency.",
            ),
        )
        narration = " ".join(spoken_beats)
        scene_plan = [
            ScenePlanItem(
                narration=beat,
                visual_text=self.image_fetcher._clean_caption(beat),
                search_terms=[
                    self.image_fetcher._brain_short_relationship_context_query(beat),
                    f"adult age 25 to 35 {subject.lower()} realistic lifestyle b roll",
                ],
                visual_prompt=(
                    f"{subject}. {beat} Premium dating psychology creator look, "
                    "realistic adults, respectful body language, cinematic vertical frame"
                ),
            )
            for beat in spoken_beats
        ]
        source_url = self.topic_planner.research._default_source_url(subject)
        source_urls = self.topic_planner.research.enrich_source_urls(
            subject,
            [source_url] if source_url else [],
            limit=6,
        )
        subject_tag = "#" + re.sub(r"[^A-Za-z0-9]+", "", subject.title())
        hashtags = list(dict.fromkeys([*channel.hashtags, subject_tag]))[:12]
        return TopicCandidate(
            niche_id=channel.id,
            style="explainer",
            trend_terms=[subject.lower(), "dating psychology", "relationship behavior"],
            title=title,
            subject=subject,
            hook=spoken_beats[0],
            narration=narration,
            visual_captions=[scene.visual_text for scene in scene_plan[1:-1]],
            source_urls=source_urls,
            image_queries=[subject, "adult couple relationship conversation daytime"],
            hashtags=hashtags,
            engagement_score=88.0,
            content_kind="short",
            narration_beats=spoken_beats,
            title_variants=[title],
            selected_title_pattern="deterministic_brain_fallback",
            scene_plan=scene_plan,
        )

    def _visual_asset_candidates(
        self,
        channel_id: str,
        content_kind: str,
        scored_candidates: list[tuple],
    ) -> list[tuple]:
        if channel_id == "brain_lens":
            return scored_candidates[:6]
        if channel_id != "ancient_history":
            return scored_candidates[:1]
        if content_kind == "short":
            recovery_mode = (
                str(os.getenv("YT_CONTINUITY_RECOVERY", "")).strip().lower()
                in {"1", "true", "yes", "on"}
            )
            # Recovery should try fewer, fresher packs quickly — not burn six
            # sparse subjects while the upload gap widens.
            candidate_limit = 2 if recovery_mode else self.ANCIENT_SHORT_VISUAL_CANDIDATES
            candidates = list(scored_candidates)
            # Sort the full pool before applying the limit. Slicing first used to
            # exclude archive-rich fallbacks such as Lascaux while a sparse subject
            # consumed the full visual-fetch budget.
            archive_priority = {
                "petra": 0,
                "gobekli tepe": 1,
                "pompeii plaster casts": 2,
                "rosetta stone": 3,
                "carthage harbor": 4,
                "sogdian merchants": 5,
                "lascaux": 6,
                "boudica revolt": 7,
                "mohenjo-daro": 8,
                "roman concrete": 9,
                "great zimbabwe": 10,
                "tikal": 11,
                "angkor wat": 12,
                "chichen itza": 13,
                "olmec colossal heads": 14,
                "axum obelisks": 15,
                "justinianic plague": 16,
                "nubian pyramids": 17,
            }
            try:
                recovery_stage = int(os.getenv("YT_RECOVERY_STAGE", "0") or 0)
            except ValueError:
                recovery_stage = 0
            if recovery_stage >= 2:
                archive_priority.update(
                    {
                        "mohenjo-daro": 0,
                        "roman concrete": 1,
                        "lascaux": 2,
                        "boudica revolt": 3,
                    }
                )

            def priority(item: tuple) -> tuple[int, int]:
                if len(item) < 3:
                    return (len(archive_priority), candidates.index(item))
                topic = item[2]
                subject = re.sub(
                    r"\s+",
                    " ",
                    str(getattr(topic, "subject", "") or getattr(topic, "title", "")).lower(),
                ).strip()
                rank = next(
                    (value for marker, value in archive_priority.items() if marker in subject),
                    len(archive_priority),
                )
                return (rank, candidates.index(item))

            return sorted(candidates, key=priority)[:candidate_limit]
        forced_subject = re.sub(
            r"[^a-z0-9]+",
            " ",
            os.getenv("YT_FORCE_HISTORY_SUBJECT", "").lower(),
        ).strip()
        if content_kind == "video" and forced_subject:
            matches = []
            for item in scored_candidates:
                if len(item) < 3:
                    continue
                candidate = item[2]
                candidate_subject = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    str(
                        getattr(candidate, "subject", "")
                        or getattr(candidate, "title", "")
                    ).lower(),
                ).strip()
                if (
                    candidate_subject == forced_subject
                    or candidate_subject in forced_subject
                    or forced_subject in candidate_subject
                ):
                    matches.append(item)
            if not matches:
                raise RuntimeError(
                    f"Forced Ancient QA subject was not produced by planning: {forced_subject}"
                )
            return matches
        return scored_candidates

    def _ancient_short_continuity_fallbacks(
        self,
        channel: ChannelConfig,
    ) -> list[TopicCandidate]:
        """Build fresh, source-backed evergreen angles when research is unavailable.

        This path is enabled only for the scheduler's continuity worker. It
        deliberately changes the angle/title while keeping the same curated
        facts and normal script, visual, and quality gates. A research outage
        therefore cannot turn into either an upload gap or an unsourced video.
        """
        if channel.id != "ancient_history":
            return []
        research = self.topic_planner.research
        packs = (
            (
                "pompeii plaster casts",
                "Pompeii Plaster Casts: Bodies Preserved by Sudden Ash",
            ),
            (
                "rosetta stone",
                "Rosetta Stone: Three Scripts That Unlocked Egyptian Writing",
            ),
            (
                "gobekli tepe",
                "Gobekli Tepe: The Ritual Site That Overturned Farming First",
            ),
            (
                "carthage harbor",
                "Carthage's Circular Harbor: Naval Power Built Into Stone",
            ),
            (
                "petra",
                "Petra's Water Channels: How a Desert City Stored Life",
            ),
            (
                "cahokia",
                "Cahokia's Monks Mound: Engineering Power on the Mississippi",
            ),
            (
                "knossos",
                "Knossos: How Excavation Invented the Labyrinth Story",
            ),
            (
                "sogdian merchants",
                "Sogdian Merchants: The Ancient Letters Behind the Silk Road",
            ),
            (
                "great zimbabwe",
                "Great Zimbabwe's Trade Network: What Imported Goods Prove",
            ),
            (
                "tikal",
                "Tikal's Carved Monuments: How Maya Rulers Made Power Visible",
            ),
            (
                "angkor wat",
                "Angkor Wat's Reservoir System: Engineering a Sacred Capital",
            ),
            (
                "olmec colossal heads",
                "Olmec Colossal Heads: How Builders Moved Monumental Stone",
            ),
            (
                "chichen itza",
                "Chichen Itza's Monument Design: Where Engineering Meets Ritual",
            ),
            (
                "axum obelisks",
                "Aksum's Granite Stelae: Engineering Status Into Stone",
            ),
            (
                "nubian pyramids",
                "Nubian Pyramids: The Kushite Royal Tradition Egypt Did Not Own",
            ),
            (
                "roman concrete",
                "Roman Concrete: The Mix That Let Harbors Harden Underwater",
            ),
            (
                "lascaux",
                "Lascaux Cave: How Visitors Nearly Destroyed Ice Age Art",
            ),
            (
                "boudica revolt",
                "Boudica's Revolt: How Three Roman Cities Burned",
            ),
            (
                "mohenjo-daro",
                "Mohenjo-Daro's Drains: Engineering Hidden Under an Indus City",
            ),
            (
                "justinianic plague",
                "Justinianic Plague: What Ancient DNA Finally Confirmed",
            ),
            (
                "bronze age collapse",
                "Bronze Age Collapse: Why No Single Disaster Explains It",
            ),
        )
        continuity_beats: dict[str, tuple[str, ...]] = {
            "great zimbabwe": (
                "Colonial writers denied Great Zimbabwe's origins.",
                "Finds confirmed its African origins independently.",
                "Shona ancestors built Great Zimbabwe.",
                "It became a key African capital.",
                "Construction spanned 1100 through 1450 CE.",
                "Granite walls rose without any mortar.",
                "Cattle may have fueled elite wealth.",
                "Its state reached rich gold lands.",
                "Excavations found imported beads and ceramics.",
                "They reveal broad Indian Ocean trade.",
                "Birds may have marked royal authority.",
                "Even their exact meaning remains unknown.",
                "The evidence confirms African origins.",
                "Great Zimbabwe linked distant trade.",
            ),
            "tikal": (
                "At Tikal, stelae speak.",
                "They now name Maya kings.",
                "Tall pyramids rose nearby.",
                "Dense jungle surrounds it.",
                "Stone records royal wins.",
                "Texts preserve king names.",
                "Plazas held public rites.",
                "Palaces housed royal power.",
                "Roads linked city zones.",
                "Great reservoirs held rain.",
                "That water once fed crowds.",
                "Dry months required plans.",
                "Rival wars grew fierce.",
                "Royal rule then fractured.",
                "Many residents moved away.",
                "Those monuments endured.",
                "Dates still mark history.",
                "Names still reveal rulers.",
                "Buildings shaped city life.",
                "Tikal made memory visible.",
            ),
            "angkor wat": (
                "Angkor Wat's towers rise.",
                "Five tall towers crown it.",
                "A vast moat surrounds them.",
                "Galleries led visitors in.",
                "One king ordered it.",
                "It first honored Vishnu.",
                "Towers modeled Mount Meru.",
                "Reliefs show armed ranks.",
                "Divine stories fill walls.",
                "Kings also shaped worship.",
                "But water shaped survival.",
                "Canals fed the capital.",
                "Reservoirs held rainwater.",
                "Water fed Khmer homes.",
                "Buddhist rites came later.",
                "The temple stayed active.",
                "The wider city changed.",
                "Stone showed royal might.",
                "Water fed sacred lands.",
                "Angkor joined both worlds.",
            ),
            "olmec colossal heads": (
                "Olmec stone heads survive.",
                "Each uses carved basalt.",
                "Each face looks distinct.",
                "Their stone caps vary.",
                "Many portray elite rulers.",
                "We do not know their names.",
                "Artists shaped huge blocks.",
                "Basalt traveled many miles.",
                "Many workers moved stone.",
                "Carving took rare skill.",
                "Identities remain unknown.",
                "Rough heads reveal methods.",
                "Each finished face differs.",
                "Their scale showed power.",
                "Portraits gave power faces.",
                "Basalt kept each face.",
                "Distance raised their cost.",
                "Work made each head costly.",
                "Rulers became monuments.",
                "Olmec heads showed power.",
            ),
            "chichen itza": (
                "Chichen Itza rises high.",
                "Terraces climb to worship.",
                "Summit worship crowned it.",
                "A huge ballcourt stretches.",
                "Its ballcourt is vast.",
                "Sport met sacrifice there.",
                "Cenote waters held gifts.",
                "Roads guided many pilgrims.",
                "Plazas held public rites.",
                "Crowds watched elite rites.",
                "Yet each site had a role.",
                "Stone serpents cover walls.",
                "Warriors guard sacred gods.",
                "Trade shaped its designs.",
                "Water anchored holy rites.",
                "Sport carried royal power.",
                "Walls guided public crowds.",
                "Ritual made power public.",
                "Three systems met there.",
                "Chichen Itza fused them.",
            ),
            "axum obelisks": (
                "Axum's stone stelae rise.",
                "Elite tombs lie far below.",
                "False doors cover stone.",
                "Windows copy palace fronts.",
                "Each monument used stone.",
                "Quarries cut huge slabs.",
                "Teams moved each huge slab.",
                "Raising required teamwork.",
                "Tomb chambers held elites.",
                "Carvings displayed rank.",
                "Yet some giant stelae fell.",
                "Failures exposed the risk.",
                "Survivors still mark Axum.",
                "Ethiopia guards this field.",
                "Scale signaled royal rank.",
                "Carvers cut hard granite.",
                "Tombs linked memory, rank.",
                "Craft turned stone upward.",
                "Engineering displayed rank.",
                "Axum built a stone skyline.",
            ),
            "nubian pyramids": (
                "Nubian pyramids changed royal skylines.",
                "Kushite rulers built hundreds there.",
                "El-Kurru held early royal burials.",
                "Nuri held many later kings.",
                "Meroe later expanded the tradition.",
                "Nubian sides rose steeply.",
                "Narrow bases created steep silhouettes.",
                "Burial chambers stayed underground.",
                "Chapels faced each pyramid.",
                "Reliefs connected gods and rulers.",
                "Sand later covered chambers.",
                "Yet cemeteries preserved royal change.",
                "Kush once ruled Egypt.",
                "That became Dynasty Twenty-Five.",
                "These monuments were not copies.",
                "They expressed Kushite royal identity.",
                "Nubian pyramids made identity visible.",
            ),
            "roman concrete": (
                "Roman concrete hid a trick.",
                "Builders mixed lime, water, and rubble.",
                "Volcanic ash changed the reaction.",
                "Some mixes hardened underwater.",
                "Piers extended beyond shorelines.",
                "Harbors grew beyond the land.",
                "But local recipes still varied.",
                "No single formula ruled Rome.",
                "They matched each structure too.",
                "The Pantheon used another strategy.",
                "Its dome lightened upward.",
                "Aggregate changed near the top.",
                "Wall thickness also decreased.",
                "Chemistry supported that design.",
                "Roman durability was engineered.",
                "Its strength was never accidental.",
                "Each job required its own concrete.",
            ),
            "lascaux": (
                "Lascaux paintings faced a new danger.",
                "Teenagers found the cave first.",
                "That happened in 1940.",
                "Cave curves gave horses volume.",
                "Aurochs towered beside them.",
                "Pigments kept colors vivid.",
                "Rock contours shaped bodies.",
                "Then mass tourism arrived.",
                "Hot air entered the cave fast.",
                "Carbon dioxide levels climbed.",
                "Microbes threatened painted walls.",
                "France closed Lascaux in 1963.",
                "That decision protected the original.",
                "Replicas later welcomed visitors.",
                "The cave stayed shut and guarded.",
                "The find quickly caused a crisis.",
                "Closing Lascaux saved the art.",
            ),
            "sogdian merchants": (
                "Sogdian letters reveal Silk Road lives.",
                "They were found near old Dunhuang.",
                "Those merchants wrote them long ago.",
                "Their language was Sogdian.",
                "Trade posts spread Sogdian far east.",
                "Messages named gold and silver.",
                "Musk and silk moved too.",
                "But business was only one layer.",
                "Communities carried religions east.",
                "Artistic styles traveled too.",
                "Afrasiab murals show foreign envoys.",
                "Trade required trusted networks.",
                "Letters reveal those human ties.",
                "They preserve worry and separation.",
                "The Silk Road had voices.",
                "Sogdian letters let them speak.",
            ),
            "boudica revolt": (
                "Boudica rose after Roman abuse.",
                "Iceni allies joined Boudica.",
                "Her rebels struck Camulodunum first.",
                "That colony had weak defenses.",
                "Rebels burned its public buildings.",
                "Londinium was then left exposed.",
                "Rome chose to abandon it.",
                "Rebels burned that town too.",
                "Verulamium became their third target.",
                "Burn layers still mark each attack.",
                "Rome gathered troops under Paulinus.",
                "He chose narrow ground for battle.",
                "That terrain trapped her army.",
                "Roman ranks firmly held their line.",
                "The revolt then collapsed.",
                "Roman rule had nearly broken.",
            ),
            "mohenjo-daro": (
                "Mohenjo-Daro hid drains underground.",
                "Indus builders used baked-brick streets.",
                "Neighborhood wells supplied water.",
                "Covered channels moved wastewater.",
                "The Great Bath added complexity.",
                "Bitumen sealed its brickwork.",
                "Was it civic or ritual? We still debate.",
                "Indus seals carried short inscriptions.",
                "That script remains undeciphered.",
                "No named palace dominates ruins.",
                "No giant royal tomb appears.",
                "Yet authority hides inside systems.",
                "Streets reveal shared standards.",
                "Drains reveal organized labor.",
                "The mystery is not disorder.",
                "No known ruler speaks directly.",
                "Mohenjo-Daro reveals nameless authority.",
            ),
            "justinianic plague": (
                "Justinianic plague DNA survived.",
                "It settled one question.",
                "A pandemic struck hard in the 540s.",
                "Mediterranean cities suffered.",
                "Mortality became severe.",
                "Procopius described the disaster.",
                "His account survives.",
                "Reports describe burial crises.",
                "But texts could not name pathogens.",
                "Burials later supplied DNA evidence.",
                "Researchers recovered ancient DNA.",
                "It identified Yersinia pestis.",
                "That bacterium causes plague.",
                "Another debate still remains.",
                "Total deaths cannot be counted.",
                "Regional records are uneven.",
                "DNA confirms the disease.",
                "Plague DNA has limits.",
                "It cannot measure every consequence.",
            ),
            "bronze age collapse": (
                "The Bronze Age collapsed unevenly.",
                "Around 1200 BCE, major palaces fell.",
                "Some coastal cities burned completely.",
                "Egyptian texts describe new enemies.",
                "They call several groups invaders.",
                "Modern writers say Sea Peoples.",
                "Old letters show broken communication.",
                "Destruction layers show warfare.",
                "Climate evidence suggests drought.",
                "Regional trade routes also weakened.",
                "Rebellion may have contributed.",
                "Fragile palaces faced everything.",
                "But no single cause fits all.",
                "Timelines differ by region.",
                "Survival strategies differed too.",
                "Collapse was a connected crisis.",
                "Evidence rejects one simple villain.",
            ),
            "petra": (
                "Petra hides water in rock.",
                "Nabataean channels cut the cliffs.",
                "Cisterns stored rare desert rain.",
                "Dams slowed sudden flash floods.",
                "The Siq guided every visitor.",
                "Carved tombs faced the canyon.",
                "Trade caravans paid for stonework.",
                "Incense routes funded the city.",
                "Engineers sealed pipes with plaster.",
                "Overflow paths protected streets.",
                "Temples rose above the reservoirs.",
                "Control of water meant control of trade.",
                "Later earthquakes damaged parts.",
                "Yet channels still map the plan.",
                "Petra was not only a tomb city.",
                "It was a hydraulic machine.",
            ),
            "nazca lines": (
                "Nazca figures stretch across Peru.",
                "They were scraped into desert gravel.",
                "Light soil shows under darker stone.",
                "Some designs run for hundreds of meters.",
                "Spirals and animals share the plain.",
                "Nearby pottery helps date the culture.",
                "Researchers mapped walking paths carefully.",
                "Many lines were meant to be walked.",
                "Ritual processions may explain their scale.",
                "Not every figure needs sky viewing.",
                "Water rituals also fit the landscape.",
                "Dry climate preserved the drawings.",
                "Modern tracks damaged some fragile edges.",
                "Careful surveys still recover lost shapes.",
                "The desert keeps the evidence intact.",
                "Nazca design was local and deliberate.",
            ),
            "gobekli tepe": (
                "Gobekli Tepe changed deep history.",
                "Stone pillars rose before farming villages.",
                "Carved animals cover T-shaped stones.",
                "Builders buried the circles later.",
                "Hunter-gatherers organized huge labor.",
                "No domestic houses dominate the mound.",
                "Ritual gathering best fits the plan.",
                "Radiocarbon dates push the timeline early.",
                "Farming may have followed ritual centers.",
                "That reverses older textbook order.",
                "Pillars weigh many tons each.",
                "Transport required shared effort.",
                "Enclosure layouts differ by phase.",
                "Fill layers sealed older rings.",
                "The site forces a new question.",
                "Complex ritual came before farms.",
            ),
            "cahokia": (
                "Cahokia rose near the Mississippi.",
                "Monks Mound still dominates the plain.",
                "Earthen platforms held elite buildings.",
                "Woodhenges marked ritual calendars.",
                "Neighborhoods spread across huge ground.",
                "Trade brought copper and shells.",
                "Maize fields fed dense crowds.",
                "Borrow pits show massive earthmoving.",
                "Flood risk shaped settlement choices.",
                "Social ranks appear in burials.",
                "The city later lost population.",
                "Climate stress may have mattered.",
                "Political fracture may have followed.",
                "Mounds still map former power.",
                "Cahokia proves Native urban scale.",
                "Engineering here was earth and labor.",
            ),
            "teotihuacan": (
                "Teotihuacan still hides its rulers.",
                "The Avenue of the Dead runs straight.",
                "Pyramids of Sun and Moon rise beside it.",
                "Apartment compounds housed many families.",
                "Craft workshops filled whole neighborhoods.",
                "Obsidian tools spread far beyond the city.",
                "Murals show ritual scenes and status.",
                "No long king list survives in the ruins.",
                "Power may have been collective instead.",
                "Foreign enclaves lived inside the grid.",
                "Later fire damaged major buildings.",
                "The collapse remains hotly debated.",
                "Yet the street plan remains clear.",
                "Urban order outlasted named dynasties.",
                "Teotihuacan ruled by careful design.",
                "Its anonymity is part of the evidence.",
            ),
            "knossos": (
                "Knossos shaped the Minotaur myth.",
                "Excavators rebuilt parts in concrete.",
                "Arthur Evans named rooms boldly.",
                "The labyrinth idea grew from that work.",
                "Minoan frescoes show ritual scenes.",
                "Storage magazines held huge jars.",
                "Linear A tablets remain unread.",
                "Earthquake damage appears in phases.",
                "Later Mycenaean use also left traces.",
                "Tourists now walk restored corridors.",
                "Scholars still separate fact from rebuild.",
                "Original ashlar blocks remain key.",
                "The palace was a complex hub.",
                "Myth filled gaps in the evidence.",
                "Knossos teaches caution.",
                "Excavation can invent a story.",
            ),
            "rosetta stone": (
                "The Rosetta Stone holds three scripts.",
                "Hieroglyphs sit above Demotic text.",
                "Greek closes the same decree.",
                "French soldiers found it in 1799.",
                "Scholars compared repeated royal names.",
                "Cartouches guided early readings.",
                "Champollion linked sound values carefully.",
                "Egyptian writing opened after that breakthrough.",
                "Temple walls became readable history.",
                "The decree itself is Ptolemaic politics.",
                "Priests receive listed privileges.",
                "Propaganda sits beside scholarship.",
                "The British Museum still displays the slab.",
                "Copies helped more people study the text.",
                "One slab unlocked a language family.",
                "Evidence beat pure guessing.",
            ),
            "pompeii plaster casts": (
                "Pompeii preserves sudden deaths.",
                "Ash sealed empty body cavities.",
                "Workers later poured plaster inside.",
                "Casts show final postures.",
                "Some victims shield their faces.",
                "Others cling to family members.",
                "Volcanic gas and heat killed fast.",
                "Houses nearby keep daily objects.",
                "Bread, tools, and graffiti remain.",
                "Casts are not original bodies.",
                "They are archaeological reconstructions.",
                "Newer scans refine older fills.",
                "Ethics debates now guide display.",
                "Still, the casts teach urgency.",
                "One eruption froze ordinary life.",
                "Pompeii makes time visible.",
            ),
            "carthage harbor": (
                "Carthage built a circular war harbor.",
                "Merchant docks sat beside it.",
                "Ships entered through controlled channels.",
                "Dry docks ringed the military basin.",
                "Admiralty island held command space.",
                "Stone quays guided every hull.",
                "Naval power funded Mediterranean trade.",
                "Rome later destroyed the city.",
                "Harbor outlines still appear in surveys.",
                "Underwater archaeology maps the basins.",
                "Punic engineers planned for fleets.",
                "Speed of launch mattered in war.",
                "Commerce and combat shared one coast.",
                "The design was strategic infrastructure.",
                "Carthage stored power in waterworks.",
                "Harbor stone still proves it.",
            ),
        }
        out: list[TopicCandidate] = []
        for subject, title in packs:
            bullets = research._curated_history_short_facts(subject)
            if not research._history_short_fact_budget_ok(bullets):
                continue
            visual_queries = research._history_visual_queries(title, subject)
            narration_beats = list(continuity_beats.get(subject, ()))
            if not narration_beats:
                continue
            caption_cleaner = getattr(
                getattr(self, "image_fetcher", None),
                "_clean_caption",
                None,
            )
            scene_plan = [
                ScenePlanItem(
                    narration=beat,
                    visual_text=caption_cleaner(beat) if callable(caption_cleaner) else beat,
                    search_terms=[
                        visual_queries[index % len(visual_queries)] if visual_queries else subject,
                        subject,
                    ],
                    visual_prompt=f"{subject}. {beat} Documentary evidence frame.",
                )
                for index, beat in enumerate(narration_beats)
            ]
            source = research._default_source_url(subject)
            source_urls = research.enrich_source_urls(
                subject,
                [source] if source else [],
                limit=6,
            )
            subject_tag = "#" + re.sub(r"[^A-Za-z0-9]+", "", subject.title())
            out.append(
                TopicCandidate(
                    niche_id=channel.id,
                    style="story",
                    trend_terms=[subject, "ancient history", "archaeology", "evidence"],
                    title=title,
                    subject=subject,
                    hook=narration_beats[0],
                    narration=" ".join(narration_beats),
                    visual_captions=[scene.visual_text for scene in scene_plan[1:-1]],
                    source_urls=source_urls,
                    image_queries=list(
                        dict.fromkeys([*visual_queries, title, subject])
                    ),
                    hashtags=list(dict.fromkeys([*channel.hashtags, subject_tag]))[:12],
                    engagement_score=84.0,
                    content_kind="short",
                    narration_beats=narration_beats,
                    title_variants=[title],
                    selected_title_pattern="continuity_evergreen_fallback",
                    scene_plan=scene_plan,
                )
            )
        return out

    @staticmethod
    def _pad_short_beats_to_word_window(
        beats: list[str],
        *,
        min_words: int,
        max_words: int,
        pad_lines: tuple[str, ...] | list[str],
    ) -> list[str]:
        spoken = [str(beat).strip() for beat in beats if str(beat).strip()]
        if not spoken:
            return spoken

        def word_count(lines: list[str]) -> int:
            return len(re.findall(r"[A-Za-z0-9']+", " ".join(lines)))

        count = word_count(spoken)
        # Prefer inserting short extra beats when the script already has many
        # micro-beats; fold only when there are few longer beats.
        insert_mode = len(spoken) >= 10
        for line in pad_lines:
            clean = re.sub(r"\s+", " ", str(line or "").strip())
            if not clean or count >= min_words:
                break
            extra = len(re.findall(r"[A-Za-z0-9']+", clean))
            if count + extra > max_words and count >= min_words:
                continue
            if insert_mode and len(spoken) >= 2:
                spoken.insert(-1, clean)
            else:
                target = 1 if len(spoken) == 1 else min(
                    len(spoken) - 1,
                    max(1, len(spoken) // 2),
                )
                base = spoken[target].rstrip()
                if not base.endswith((".", "!", "?")):
                    base = f"{base}."
                spoken[target] = f"{base} {clean}".strip()
            count = word_count(spoken)
        return spoken

    def _prepare_ancient_continuity_fallback(
        self,
        channel: ChannelConfig,
        candidate: TopicCandidate,
        recent_titles: set[str],
    ) -> TopicCandidate:
        polished = (
            replace(candidate)
            if candidate.selected_title_pattern == "continuity_evergreen_fallback"
            else self.script_writer.improve(
                channel=channel,
                topic=candidate,
                content_kind="short",
                avoid_titles=recent_titles,
            )
        )
        if polished.selected_title_pattern == "continuity_evergreen_fallback":
            min_words = int(getattr(self.script_writer, "_ANCIENT_SHORT_MIN_WORDS", 120) or 120)
            max_words = int(getattr(self.script_writer, "_ANCIENT_SHORT_MAX_WORDS", 148) or 148)
            subject = polished.subject or polished.title
            subject_words = re.sub(r"\s+", " ", str(subject).strip())
            # Expand without repeating the subject name in every line — the
            # quality gate rejects heavy subject repetition.
            pad_lines = (
                "Dated finds still test the opening claim against later legend.",
                "Side-by-side finds sharpen what excavators can actually prove.",
                "Museum catalogs reward careful reading of hard proof.",
                "Precise, checkable evidence is what keeps this story honest.",
                "Dated proof on the ground still rewards careful readers.",
                "Each verified detail changes how the first hard clue should be read.",
                "Field notes still separate rumor from measurable remains.",
                "Layer by layer, the surviving record narrows what can be claimed.",
                "Comparisons across sites stop one object from standing alone.",
                "Clear provenance is what turns a striking image into usable history.",
                "Later restorations must be separated from the original construction.",
                "The strongest claims stay tied to dated, visible material.",
            )
            spoken_beats = self._pad_short_beats_to_word_window(
                list(polished.narration_beats or [polished.narration]),
                min_words=min_words,
                max_words=max_words,
                pad_lines=pad_lines,
            )
            subject_tokens = {
                token
                for token in re.findall(r"[a-z]{4,}", str(subject_words).lower())
                if token not in {"about", "ancient", "history", "revolt", "plague", "collapse"}
            }
            payoff_blob = " ".join(spoken_beats[-2:]).lower()
            if subject_tokens and not any(token in payoff_blob for token in subject_tokens):
                closer = spoken_beats[-1].rstrip()
                if not closer.endswith((".", "!", "?")):
                    closer = f"{closer}."
                subject_line = f"That evidence still defines {subject_words}."
                merged = f"{closer} {subject_line}"
                # Burned-in captions wrap on beat boundaries, so folding the
                # subject line into the closer overflows the caption width for
                # longer subject names. Keep the same words, but give the line
                # its own beat once the merge would exceed the budget.
                if len(merged) <= self.ANCIENT_SHORT_MAX_BEAT_CHARS:
                    spoken_beats[-1] = merged
                else:
                    spoken_beats[-1] = closer
                    spoken_beats.append(subject_line)
            source_urls = list(polished.source_urls or [])
            if not source_urls:
                fallback_source = self.topic_planner.research._default_source_url(subject_words)
                if not fallback_source:
                    known_sources = {
                        "lascaux": "https://en.wikipedia.org/wiki/Lascaux",
                        "boudica revolt": "https://en.wikipedia.org/wiki/Boudican_revolt",
                        "justinianic plague": "https://en.wikipedia.org/wiki/Plague_of_Justinian",
                        "bronze age collapse": "https://en.wikipedia.org/wiki/Late_Bronze_Age_collapse",
                        "nubian pyramids": "https://en.wikipedia.org/wiki/Nubian_pyramids",
                        "petra": "https://en.wikipedia.org/wiki/Petra",
                        "nazca lines": "https://en.wikipedia.org/wiki/Nazca_Lines",
                        "gobekli tepe": "https://en.wikipedia.org/wiki/G%C3%B6bekli_Tepe",
                        "cahokia": "https://en.wikipedia.org/wiki/Cahokia",
                        "teotihuacan": "https://en.wikipedia.org/wiki/Teotihuacan",
                        "knossos": "https://en.wikipedia.org/wiki/Knossos",
                        "rosetta stone": "https://en.wikipedia.org/wiki/Rosetta_Stone",
                        "pompeii plaster casts": "https://en.wikipedia.org/wiki/Pompeii",
                        "carthage harbor": "https://en.wikipedia.org/wiki/Ports_of_Carthage",
                    }
                    fallback_source = known_sources.get(str(subject_words).strip().lower(), "")
                if fallback_source:
                    source_urls = [fallback_source]
            polished = replace(
                polished,
                narration=" ".join(spoken_beats),
                narration_beats=spoken_beats,
                hook=spoken_beats[0] if spoken_beats else polished.hook,
                source_urls=source_urls,
            )
        self._validate_topic_quality(channel, polished, recent_titles)
        return polished

    def _long_script_repetition_issue(self, topic: TopicCandidate) -> str | None:
        beats = [
            re.sub(r"\s+", " ", beat or "").strip()
            for beat in (topic.narration_beats or [])
            if re.sub(r"\s+", " ", beat or "").strip()
        ]
        if len(beats) < 12:
            return f"long video has only {len(beats)} structured sections"
        normalized = [re.sub(r"[^a-z0-9]+", " ", beat.lower()).strip() for beat in beats]
        if len(set(normalized)) != len(normalized):
            return "long video repeats an entire narration section"
        legacy_fillers = (
            "tie this claim to something concrete",
            "the useful story is not just what happened",
            "then look at the pressure behind the evidence",
            "stay with that moment because the tiny behavior is the evidence",
            "name the cue shrink the choice and take the next visible action",
        )
        joined = " ".join(normalized)
        if any(joined.count(marker) >= 3 for marker in legacy_fillers):
            return "long video relies on repeated generic filler"

        shingle_sets = []
        for beat in normalized:
            words = beat.split()
            shingle_sets.append({" ".join(words[index:index + 7]) for index in range(max(0, len(words) - 6))})
        for left_index, left in enumerate(shingle_sets):
            if not left:
                continue
            for right in shingle_sets[left_index + 1:]:
                if not right:
                    continue
                overlap = len(left & right) / max(1, min(len(left), len(right)))
                if overlap >= 0.48:
                    return "long video sections reuse too much identical wording"
        return None

    def _quality_review(
        self,
        channel: ChannelConfig,
        topic: TopicCandidate,
        metadata: dict,
        sources: list[dict],
        duration_seconds: float,
        video_path: Path,
    ) -> dict:
        score = 100
        issues: list[str] = []
        strengths: list[str] = []
        blocking_issues: list[str] = []

        title = str(metadata.get("title") or topic.title or "").strip()
        title_issue = self._backlog_title_quality_issue(title)
        if title_issue:
            issues.append(title_issue)
            score -= 28
        else:
            strengths.append("specific title")

        thumbnail_quality_issues = [
            str(issue).strip()
            for issue in metadata.get("thumbnail_quality_issues", [])
            if str(issue).strip()
        ]
        if thumbnail_quality_issues:
            for issue in thumbnail_quality_issues:
                formatted = f"Thumbnail: {issue}"
                issues.append(formatted)
                blocking_issues.append(formatted)
            score -= min(24, 12 * len(thumbnail_quality_issues))
        elif metadata.get("thumbnail_file"):
            strengths.append("thumbnail preserves channel brand and core topic")

        seo_score = int(metadata.get("seo_score") or 0)
        if seo_score and seo_score < 74:
            issues.append(f"SEO score low ({seo_score})")
            issues.extend(f"SEO: {issue}" for issue in metadata.get("seo_issues", [])[:3])
            score -= 12
        elif seo_score:
            strengths.append(f"SEO metadata score {seo_score}")
        missing_source_issue = next(
            (
                str(issue)
                for issue in metadata.get("seo_issues", [])
                if "missing a validated source URL" in str(issue)
            ),
            "",
        )
        if missing_source_issue:
            formatted_source_issue = f"SEO: {missing_source_issue}"
            if formatted_source_issue not in issues:
                issues.append(formatted_source_issue)
            blocking_issues.append(formatted_source_issue)
            score -= 18

        content_kind = str(metadata.get("content_kind") or "short")
        word_count = len(re.findall(r"[A-Za-z0-9']+", topic.narration or ""))
        if content_kind == "video" and word_count < 850:
            issues.append("long video narration too thin")
            score -= 22
        if content_kind == "video" and channel.id == "ancient_history" and word_count < 1150:
            issues.append("Ancient long video needs more documentary depth")
            score -= 16
        if content_kind == "video" and word_count > 1600:
            issues.append("long video narration is too dense for the 10-minute limit")
            score -= 18
        if content_kind == "video":
            repetition_issue = self._long_script_repetition_issue(topic)
            if repetition_issue:
                issues.append(repetition_issue)
                blocking_issues.append(repetition_issue)
                score -= 35
            else:
                strengths.append("long-form sections are distinct and structured")
        elif word_count < 110:
            issues.append("narration too thin")
            score -= 18
        elif word_count > 155 and content_kind == "short":
            issues.append("short narration may feel crowded")
            score -= 8
        else:
            strengths.append("healthy narration length")

        if content_kind == "video":
            if not (470 <= duration_seconds <= 620):
                issues.append("duration outside 8-10 minute long-video target")
                score -= 14
            else:
                strengths.append("8-10 minute long-video target")
        elif channel.id == "brain_lens":
            topic_lines = list(topic.narration_beats or [])
            if not topic_lines and topic.narration:
                topic_lines = [part.strip() for part in re.split(r"(?<=[.!?])\s+", topic.narration) if part.strip()]
            opener = (topic_lines[0] if topic_lines else topic.hook or "").strip().lower()
            concrete_starts = (
                "you ",
                "your ",
                "they ",
                "notice ",
                "watch ",
                "the moment ",
                "that second ",
            )
            concrete_cues = (
                "phone",
                "text",
                "reply",
                "silence",
                "gaze",
                "memory",
                "agree",
                "disappointed",
                "trait",
                "red flag",
                "praised",
                "mistake",
                "attempt",
                "jaw",
                "option",
                "number",
                "online",
                "thumb",
                "scroll",
                "chest",
                "smile",
                "eye contact",
                "reread",
                "avoid",
                "pause",
                "room",
                "closeness",
                "distance",
                "look",
                "kiss",
                "touch",
                "hand",
                "date",
                "message",
                "story",
                "conversation",
            )
            if opener and not (opener.startswith(concrete_starts) or any(cue in opener for cue in concrete_cues)):
                issues.append("Brain Lens opener is abstract instead of behavior-first")
                blocking_issues.append("Brain Lens opener is abstract instead of behavior-first")
                score -= 18
            claim_card = str(metadata.get("claim_card_text") or "").strip()
            if content_kind == "short":
                if not claim_card or len(claim_card.split()) < 2:
                    claim_issue = "Brain Lens Short missing first-second on-screen claim card"
                    issues.append(claim_issue)
                    blocking_issues.append(claim_issue)
                    score -= 20
                elif len(claim_card) > 54:
                    claim_issue = "Brain Lens claim card text is too long for first-second readability"
                    issues.append(claim_issue)
                    blocking_issues.append(claim_issue)
                    score -= 12
                else:
                    strengths.append("Brain Lens first-second claim card present")
            if not (50 <= duration_seconds <= 59.5):
                issues.append("duration outside Brain Lens winning range")
                score -= 12
            else:
                strengths.append("Brain Lens duration sweet spot")
        elif channel.id == "ancient_history":
            if not (50 <= duration_seconds <= 59.5):
                issues.append("duration outside Ancient History winning range")
                score -= 10
            else:
                strengths.append("Ancient History duration sweet spot")

        source_counts = Counter(str(item.get("source") or "unknown") for item in sources)
        if content_kind == "video":
            measured_motion_sources = []
            for item in sources:
                if str(item.get("source") or "") != "pexels_video":
                    continue
                try:
                    media_width = int(item.get("media_width") or 0)
                    media_height = int(item.get("media_height") or 0)
                except (TypeError, ValueError):
                    media_width = 0
                    media_height = 0
                if media_width > 0 and media_height > 0:
                    measured_motion_sources.append((media_width, media_height))
            portrait_motion_count = sum(1 for width, height in measured_motion_sources if height > width)
            if portrait_motion_count:
                orientation_issue = f"{portrait_motion_count} portrait motion clips were selected for a landscape long video"
                issues.append(orientation_issue)
                blocking_issues.append(orientation_issue)
                score -= 28
            elif measured_motion_sources:
                strengths.append(f"{len(measured_motion_sources)} landscape motion clips verified")
            if channel.id == "brain_lens":
                distinct_scene_queries = {
                    re.sub(r"\s+", " ", str(item.get("search_query") or "").strip().lower())
                    for item in sources
                    if str(item.get("search_query") or "").strip()
                }
                if len(distinct_scene_queries) < 8:
                    query_issue = f"Brain Lens long visuals use only {len(distinct_scene_queries)} distinct scene searches"
                    issues.append(query_issue)
                    blocking_issues.append(query_issue)
                    score -= 24
                else:
                    strengths.append(f"{len(distinct_scene_queries)} scene-specific visual searches")
        generated_source_types = {
            "local_documentary_diagram",
            "local_fact_card",
            "stable_horde",
            "pollinations_ai",
            "unknown",
        }
        archival_sources = [
            item
            for item in sources
            if str(item.get("source") or "unknown") not in generated_source_types
        ]
        real_sources = len(archival_sources)
        unique_archival_urls = {
            str(item.get("url") or "").strip()
            for item in archival_sources
            if str(item.get("url") or "").strip().startswith(("http://", "https://"))
        }
        ai_sources = sum(
            count for source, count in source_counts.items()
            if source in {"stable_horde", "pollinations_ai"}
        )
        if real_sources == 0:
            issues.append("all visuals are AI/local fallback")
            score -= 22
        else:
            strengths.append("has real-world visual sources")
        has_dynamic_presenter = bool(
            metadata.get("presenter_lipsync")
            or metadata.get("heygen_avatar")
        )
        presenter_requested = bool(metadata.get("presenter_requested"))
        if channel.id == "brain_lens" and presenter_requested and not has_dynamic_presenter:
            issues.append("requested presenter has no verified speaking motion")
            score -= 22
        if channel.id == "brain_lens" and source_counts.get("pexels_video", 0) < 2 and not has_dynamic_presenter:
            issues.append("Brain Lens needs more motion footage")
            score -= 8
        elif channel.id == "brain_lens" and has_dynamic_presenter:
            if metadata.get("presenter_lipsync") or metadata.get("heygen_avatar"):
                strengths.append("lip-synced presenter hook")
            else:
                strengths.append("audio-synchronized animated presenter hook")
        if channel.id == "brain_lens":
            artist_counts = Counter(
                str(item.get("artist") or "unknown").strip().lower()
                for item in sources
                if str(item.get("artist") or "").strip().lower() not in {"", "unknown"}
            )
            artist_repeat_limit = 4 if content_kind == "video" else 3
            if artist_counts and artist_counts.most_common(1)[0][1] > artist_repeat_limit:
                issues.append("Brain Lens visuals repeat the same source too much")
                score -= 6
            child_visuals = 0
            for item in sources:
                searchable_asset = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    f"{item.get('url', '')} {item.get('asset_title', '')} {item.get('artist', '')}".lower(),
                ).strip()
                if any(
                    term in searchable_asset
                    for term in (
                        " child", " children", " kid", " toddler", " baby", " schoolboy", " schoolgirl",
                        " teen", "little girl", "young girl", "small girl", "little boy", "young boy",
                    )
                ) or re.search(r"\b(?:boy|girl)\b", searchable_asset):
                    child_visuals += 1
            if child_visuals:
                issues.append(f"Brain Lens adult topic contains {child_visuals} child-focused visual(s)")
                score -= min(24, 12 * child_visuals)
            off_tone_visuals = 0
            for item in sources:
                searchable_asset = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    f"{item.get('url', '')} {item.get('asset_title', '')} {item.get('artist', '')}".lower(),
                ).strip()
                if any(
                    term in searchable_asset
                    for term in (
                        "elderly", "senior woman", "senior man", "pregnancy test",
                        "thumbs down", "flower petal tears", "painted tears", "throwing documents",
                        "screaming", "scream", "shouting", "pulling hair", "stretching neck",
                        "eyes closed", "sleeping", "ai25 studio", "cheating", "infidelity", "affair",
                    )
                ):
                    off_tone_visuals += 1
            if off_tone_visuals:
                visual_issue = f"Brain Lens contains {off_tone_visuals} off-tone stock visual(s)"
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= min(24, 12 * off_tone_visuals)
            relationship_topic = any(
                term in f"{topic.subject} {topic.title} {topic.narration}".lower()
                for term in (
                    "relationship", "dating", "attachment", "attraction", "chemistry", "flirt",
                    "kiss", "breadcrumb", "situationship", "push pull", "mixed signal",
                    "emotional availability", "silent treatment", "future faking", "benching",
                     "slow fading", "ghosting", "orbiting", "relationship pacing", "mutual effort",
                     "crush idealization", "limerence", "jealousy", "friends with benefits",
                 )
            )
            if relationship_topic and sources:
                relationship_markers = (
                    "couple", "relationship", "partner", "dating", " date ", "romantic",
                    "kissing", " kiss ", "hug", "holding hands", "argument", "arguing",
                    "breakup", "breaking up", "man and woman", "woman and man",
                    "male and female", "lovers", "boyfriend",
                    "girlfriend", "husband", "wife",
                )
                relationship_visuals = 0
                for item in sources:
                    searchable_asset = re.sub(
                        r"[^a-z0-9]+",
                        " ",
                        f"{item.get('url', '')} {item.get('asset_title', '')}".lower(),
                    ).strip()
                    if any(marker in f" {searchable_asset} " for marker in relationship_markers):
                        relationship_visuals += 1
                required_relationship_visuals = max(4, (len(sources) * 2 + 2) // 3)
                if relationship_visuals < required_relationship_visuals:
                    visual_issue = (
                        "Brain Lens relationship topic has only "
                        f"{relationship_visuals}/{len(sources)} clearly relevant relationship visuals"
                    )
                    issues.append(visual_issue)
                    blocking_issues.append(visual_issue)
                    score -= 18
                else:
                    strengths.append(
                        f"relationship visual relevance {relationship_visuals}/{len(sources)}"
                    )
                opening_text = (topic.narration_beats or [topic.hook or topic.narration or ""])[0].lower()
                if any(term in opening_text for term in ("text", "message", "reply", "phone")):
                    first_scene_text = str((topic.scene_plan or [ScenePlanItem("", "")])[0].visual_text or "").strip()
                    hook_sources = [
                        item
                        for item in sources
                        if int(item.get("scene_index") or 0) == 1
                        or (first_scene_text and str(item.get("scene_text") or "").strip() == first_scene_text)
                    ]
                    relationship_hook = 0
                    for item in hook_sources:
                        searchable_asset = re.sub(
                            r"[^a-z0-9]+",
                            " ",
                            f"{item.get('url', '')} {item.get('asset_title', '')} {item.get('search_query', '')}".lower(),
                        ).strip()
                        has_relationship = any(
                            marker in f" {searchable_asset} " for marker in relationship_markers
                        )
                        has_phone = any(marker in searchable_asset for marker in ("phone", "text", "message", "smartphone"))
                        if has_relationship and has_phone:
                            relationship_hook += 1
                    if relationship_hook < 1:
                        visual_issue = "Brain Lens texting hook lacks a clearly relevant couple-and-phone opening shot"
                        issues.append(visual_issue)
                        blocking_issues.append(visual_issue)
                        score -= 20
                    else:
                        strengths.append("texting hook opens on a couple-and-phone behavior shot")
                # Match actual dating-app language, not substrings inside
                # ordinary relationship vocabulary (for example, "actions
                # match" or "mismatches").  A false app classification turns
                # an otherwise relevant relationship hook into an impossible
                # phone/app visual requirement.
                dating_app_hook = bool(
                    re.search(
                        r"\b(?:open(?:ing)? (?:the )?app|dating app|online dating|"
                        r"swipe(?:s|d|ing)?|dating profile|app profile|"
                        r"new matches?|their match)\b",
                        opening_text,
                    )
                )
                if dating_app_hook:
                    first_scene_text = str((topic.scene_plan or [ScenePlanItem("", "")])[0].visual_text or "").strip()
                    hook_sources = [
                        item
                        for item in sources
                        if int(item.get("scene_index") or 0) == 1
                        or (first_scene_text and str(item.get("scene_text") or "").strip() == first_scene_text)
                    ]
                    app_hook = 0
                    for item in hook_sources:
                        searchable_asset = re.sub(
                            r"[^a-z0-9]+",
                            " ",
                            f"{item.get('url', '')} {item.get('asset_title', '')} {item.get('search_query', '')}".lower(),
                        ).strip()
                        if any(
                            marker in searchable_asset
                            for marker in ("phone", "smartphone", "dating app", "online dating", "swip", "profile", "match")
                        ):
                            app_hook += 1
                    if app_hook < 1:
                        visual_issue = "Brain Lens dating-app hook lacks a clearly relevant phone/app opening shot"
                        issues.append(visual_issue)
                        blocking_issues.append(visual_issue)
                        score -= 20
                    else:
                        strengths.append("dating-app hook opens on a phone/app behavior shot")
                action_counts = Counter(
                    action
                    for item in sources
                    if (
                        action := self._brain_visual_action(
                            f"{item.get('url', '')} {item.get('asset_title', '')}"
                        )
                    )
                )
                if action_counts:
                    repeated_action, repeated_count = action_counts.most_common(1)[0]
                    max_repeated_action = max(5, (len(sources) + 1) // 2)
                    if repeated_count > max_repeated_action:
                        visual_issue = (
                            f"Brain Lens repeats {repeated_action} footage in "
                            f"{repeated_count}/{len(sources)} visual sources"
                        )
                        issues.append(visual_issue)
                        blocking_issues.append(visual_issue)
                        score -= 16
        if channel.id == "ancient_history" and real_sources < 2 and ai_sources > real_sources:
            issues.append("Ancient History needs more documentary/stock visuals")
            score -= 8
        if channel.id == "ancient_history" and sources and ai_sources > len(sources) // 3:
            visual_issue = (
                f"Ancient History uses {ai_sources}/{len(sources)} generated visuals; "
                "documentary footage must remain the majority"
            )
            issues.append(visual_issue)
            blocking_issues.append(visual_issue)
            score -= 24
        if channel.id == "brain_lens" and sources and ai_sources > max(1, len(sources) // 5):
            visual_issue = (
                f"Brain Lens uses {ai_sources}/{len(sources)} generated stills; "
                "verified real footage must remain the clear majority"
            )
            issues.append(visual_issue)
            blocking_issues.append(visual_issue)
            score -= 24
        ancient_fact_cards = int(source_counts.get("local_fact_card", 0) or 0)
        if channel.id == "ancient_history" and ancient_fact_cards > self.ANCIENT_SHORT_MAX_FACT_CARD_FALLBACKS:
            visual_issue = (
                f"Ancient History contains {ancient_fact_cards} generic fallback "
                f"cards (max {self.ANCIENT_SHORT_MAX_FACT_CARD_FALLBACKS})"
            )
            issues.append(visual_issue)
            blocking_issues.append(visual_issue)
            score -= 18
        elif channel.id == "ancient_history" and ancient_fact_cards:
            strengths.append(
                f"Ancient fact-card fallbacks within budget ({ancient_fact_cards}/"
                f"{self.ANCIENT_SHORT_MAX_FACT_CARD_FALLBACKS})"
            )
        if channel.id == "ancient_history":
            location_sensitive = ("nazca", "nasca", "petra", "carthage", "thermopylae", "teutoburg", "dead sea scrolls")
            if any(term in f"{topic.title} {topic.subject}".lower() for term in location_sensitive) and real_sources < 2:
                issues.append("location-sensitive Ancient topic needs at least two real visuals")
                score -= 14

        if not video_path.exists() or video_path.stat().st_size < 1_000_000:
            issues.append("video file is missing or suspiciously small")
            score -= 35
        else:
            strengths.append("video file rendered")

        hook_limit = 40 if content_kind == "video" else 22
        if topic.hook and len(topic.hook.split()) <= hook_limit:
            strengths.append("short hook")
        else:
            issues.append("opening hook may be too long")
            score -= 7

        hook_issue = self._opening_hook_issue(topic)
        if hook_issue:
            issues.append(hook_issue)
            score -= 18
        else:
            strengths.append("specific fast opening hook")

        script_issues = self._script_quality_issues(channel, topic)
        if script_issues:
            issues.extend(script_issues[:4])
            score -= min(32, 12 + (6 * len(script_issues[:4])))
        else:
            strengths.append("viewer-comment script checks passed")

        configured_backend = str(getattr(channel, "tts_backend", "") or "").strip().lower()
        rendered_voice = str(metadata.get("voice") or "").strip().lower()
        voice_matches = (
            (configured_backend == "kokoro" and rendered_voice.startswith("kokoro-"))
            or (configured_backend == "piper" and rendered_voice.startswith("piper-"))
            or (configured_backend not in {"kokoro", "piper"} and bool(rendered_voice))
        )
        if not voice_matches:
            voice_issue = f"configured {configured_backend or 'local'} voice was not used ({rendered_voice or 'missing'})"
            if voice_issue not in issues:
                issues.append(voice_issue)
            blocking_issues.append(voice_issue)
            score -= 35
        else:
            strengths.append(f"verified {configured_backend} voice render")

        music_value = str(metadata.get("music_file") or "").strip()
        music_path = Path(music_value) if music_value else None
        if music_path and not music_path.is_absolute():
            music_path = (self._resolved_music_root() / music_path).resolve()
        music_ok = bool(
            music_path
            and music_path.exists()
            and music_path.is_file()
            and music_path.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
        )
        if not music_ok:
            music_issue = "no verified background music mix"
            if music_issue not in issues:
                issues.append(music_issue)
            blocking_issues.append(music_issue)
            score -= 14
        else:
            strengths.append("channel-specific original music mixed")

        visual_asset_count = max(len(sources), int(metadata.get("visual_asset_count") or 0))
        unique_visual_urls = {
            str(item.get("url") or "").strip()
            for item in sources
            if str(item.get("url") or "").strip()
        }
        unknown_license_count = sum(
            1
            for item in sources
            if str(item.get("license") or "").strip().lower() in {"", "unknown"}
        )
        motion_asset_count = int(source_counts.get("pexels_video", 0))
        motion_analysis: dict = {}
        visual_timeline_path = video_path.parent / "visual_timeline.json"
        visual_timeline = read_json(visual_timeline_path, default=[])
        if content_kind == "short":
            expected_beat_count = len(topic.narration_beats or [])
            timeline_beat_indices = {
                int(item.get("beat_index"))
                for item in visual_timeline
                if isinstance(item, dict) and str(item.get("beat_index", "")).isdigit()
            }
            if not visual_timeline or (expected_beat_count and len(timeline_beat_indices) < expected_beat_count):
                visual_issue = "short visual timeline is missing narration-beat alignment"
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 24
            else:
                strengths.append(f"visual cuts aligned across {len(timeline_beat_indices)} narration beats")
            mismatched_diagrams = 0
            for item in visual_timeline:
                if not isinstance(item, dict):
                    continue
                source_file = str(item.get("source_file") or "").lower()
                beat_text = str(item.get("beat_text") or "").lower()
                if "machu_terrace_layers" in source_file and not any(
                    marker in beat_text for marker in ("graded layers", "stone, gravel", "beneath each terrace")
                ):
                    mismatched_diagrams += 1
                if "machu_water_flow" in source_file and not any(
                    marker in beat_text for marker in ("canal", "runoff", "erosion")
                ):
                    mismatched_diagrams += 1
            if mismatched_diagrams:
                visual_issue = f"{mismatched_diagrams} documentary diagram cut(s) do not match the spoken beat"
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 24
        else:
            expected_beat_count = len(topic.narration_beats or [])
            timeline_beat_indices = {
                int(item.get("beat_index"))
                for item in visual_timeline
                if isinstance(item, dict) and str(item.get("beat_index", "")).isdigit()
            }
            longest_visual = max(
                (
                    float(item.get("end") or 0) - float(item.get("start") or 0)
                    for item in visual_timeline
                    if isinstance(item, dict)
                ),
                default=999.0,
            )
            if not visual_timeline or (expected_beat_count and len(timeline_beat_indices) < expected_beat_count):
                visual_issue = "long visual timeline is missing narration-section alignment"
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 24
            elif longest_visual > 8.5:
                visual_issue = f"long-form visual stays unchanged for {longest_visual:.1f}s"
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 18
            else:
                strengths.append(
                    f"long-form cuts cover {len(timeline_beat_indices)} sections with {longest_visual:.1f}s maximum hold"
                )
        minimum_assets = (
            10
            if content_kind == "short"
            else max(16, min(20, len(topic.scene_plan or [])))
        )
        if visual_asset_count < minimum_assets:
            visual_issue = f"only {visual_asset_count} visual assets; needs at least {minimum_assets}"
            if visual_issue not in issues:
                issues.append(visual_issue)
            blocking_issues.append(visual_issue)
            score -= 18
        minimum_unique_visuals = 8 if content_kind == "short" else 12
        if len(unique_visual_urls) < minimum_unique_visuals:
            visual_issue = f"only {len(unique_visual_urls)} unique visual sources; needs at least {minimum_unique_visuals}"
            issues.append(visual_issue)
            blocking_issues.append(visual_issue)
            score -= 22
        else:
            strengths.append(f"{len(unique_visual_urls)} unique visual sources")
        if channel.id == "ancient_history" and content_kind == "video":
            if len(unique_archival_urls) < 7:
                visual_issue = (
                    f"only {len(unique_archival_urls)} unique real historical visuals; needs at least 7"
                )
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 28
            else:
                strengths.append(f"{len(unique_archival_urls)} unique real historical visuals")
            opening_source = next(
                (item for item in sources if int(item.get("scene_index") or 0) == 1),
                sources[0] if sources else {},
            )
            opening_type = str(opening_source.get("source") or "unknown")
            opening_url = str(opening_source.get("url") or "").strip()
            if opening_type in generated_source_types or not opening_url.startswith(("http://", "https://")):
                visual_issue = "Ancient long-form opener is a generated fallback instead of real historical evidence"
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 28
            else:
                strengths.append("opens on real historical evidence")
        if channel.id == "ancient_history" and unknown_license_count > max(1, len(sources) // 5):
            visual_issue = f"{unknown_license_count} Ancient visual sources have unknown licensing"
            issues.append(visual_issue)
            blocking_issues.append(visual_issue)
            score -= 20
        if channel.id == "ancient_history" and sources:
            relevance_terms = self.image_fetcher._strict_ancient_terms(topic.title, subject=topic.subject)
            relevant_visuals = 0
            for item in sources:
                if self.image_fetcher._candidate_matches_terms(
                    str(item.get("url") or ""),
                    item,
                    relevance_terms,
                ):
                    relevant_visuals += 1
            required_relevant_visuals = max(6, (len(sources) * 2 + 2) // 3)
            if relevant_visuals < required_relevant_visuals:
                visual_issue = (
                    f"Ancient topic has only {relevant_visuals}/{len(sources)} "
                    "clearly subject-relevant visuals"
                )
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= 22
            else:
                strengths.append(f"Ancient visual relevance {relevant_visuals}/{len(sources)}")
            irrelevant_archival_urls = {
                str(item.get("url") or "").strip()
                for item in archival_sources
                if str(item.get("url") or "").strip()
                and not self.image_fetcher._candidate_matches_terms(
                    str(item.get("url") or ""),
                    item,
                    relevance_terms,
                )
            }
            if irrelevant_archival_urls:
                visual_issue = (
                    f"Ancient story contains {len(irrelevant_archival_urls)} real visual source(s) "
                    "that do not clearly match the subject"
                )
                issues.append(visual_issue)
                blocking_issues.append(visual_issue)
                score -= min(30, len(irrelevant_archival_urls) * 12)
            ancient_searchable = [
                re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    f"{item.get('url', '')} {item.get('asset_title', '')} {item.get('artist', '')}".lower(),
                ).strip()
                for item in sources
            ]
            if "machu picchu" in f"{topic.subject} {topic.title}".lower():
                off_topic_markers = (
                    "aguas calientes", "statue", "town", "village", "hotel", "train",
                    "exposici", "exhibe", "aribal", "cuenco", "pottery", "ceramic", "bowl", "yale",
                    "sightseeing", "tourist sign", "tourism sign",
                )
                off_topic_count = sum(
                    1 for searchable in ancient_searchable if any(marker in searchable for marker in off_topic_markers)
                )
                if off_topic_count:
                    visual_issue = f"Machu Picchu engineering story contains {off_topic_count} unrelated artifact/tourism visual(s)"
                    issues.append(visual_issue)
                    blocking_issues.append(visual_issue)
                    score -= min(24, off_topic_count * 12)
                drainage_evidence = sum(
                    1
                    for searchable in ancient_searchable
                    if any(marker in searchable for marker in ("terrace", "andenes", "drain", "canal", "runoff", "water flow"))
                )
                if drainage_evidence < 2:
                    visual_issue = "Machu Picchu drainage story lacks two clearly explanatory terrace/drainage visuals"
                    issues.append(visual_issue)
                    blocking_issues.append(visual_issue)
                    score -= 22
                else:
                    strengths.append(f"Machu Picchu engineering evidence visuals {drainage_evidence}/{len(sources)}")
                hook_visuals = [item for item in sources if int(item.get("scene_index") or 0) == 1]
                hook_markers = ("terrace", "andenes", "ruin", "architecture", "stone", "inca", "archaeolog")
                hook_evidence = sum(
                    1
                    for item in hook_visuals
                    if any(
                        marker in f"{item.get('url', '')} {item.get('asset_title', '')}".lower()
                        for marker in hook_markers
                    )
                )
                if hook_evidence < 1:
                    visual_issue = "Machu Picchu hook lacks a visible terrace/ruin engineering shot"
                    issues.append(visual_issue)
                    blocking_issues.append(visual_issue)
                    score -= 20
                else:
                    strengths.append("Machu Picchu hook opens on visible site engineering")
        if channel.id == "brain_lens" and content_kind == "short" and motion_asset_count < 4:
            motion_issue = f"only {motion_asset_count} motion clips; Brain Lens needs at least 4"
            if motion_issue not in issues:
                issues.append(motion_issue)
            blocking_issues.append(motion_issue)
            score -= 24
        elif channel.id == "brain_lens" and motion_asset_count >= 4:
            strengths.append(f"{motion_asset_count} real motion clips")
        if channel.id == "brain_lens" and content_kind == "short":
            motion_analysis = self._measure_video_motion(video_path)
            if not motion_analysis.get("available"):
                motion_issue = "unable to verify movement in the final Brain Lens render"
                issues.append(motion_issue)
                blocking_issues.append(motion_issue)
                score -= 18
            elif float(motion_analysis.get("dynamic_ratio") or 0.0) < 0.5:
                dynamic_samples = int(motion_analysis.get("dynamic_samples") or 0)
                sample_count = int(motion_analysis.get("sample_count") or 0)
                motion_issue = f"final video is too static ({dynamic_samples}/{sample_count} motion samples)"
                issues.append(motion_issue)
                blocking_issues.append(motion_issue)
                score -= 24
            else:
                strengths.append(
                    f"verified final-video movement ({motion_analysis.get('dynamic_samples')}/{motion_analysis.get('sample_count')} samples)"
                )

        captions_path = video_path.parent / "subtitles.srt"
        caption_lines: list[str] = []
        caption_segments: list[SubtitleSegment] = []
        if captions_path.exists():
            raw_srt = captions_path.read_text(encoding="utf-8", errors="ignore")
            for raw_line in raw_srt.splitlines():
                clean_line = raw_line.strip()
                if not clean_line or clean_line.isdigit() or "-->" in clean_line:
                    continue
                caption_lines.append(clean_line)
            for block in re.split(r"\n\s*\n", raw_srt.strip()):
                block_lines = [line.strip() for line in block.splitlines() if line.strip()]
                timing_index = next(
                    (idx for idx, line in enumerate(block_lines) if "-->" in line),
                    -1,
                )
                if timing_index < 0:
                    continue
                match = re.match(
                    r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*"
                    r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})",
                    block_lines[timing_index],
                )
                if not match:
                    continue
                values = [int(value) for value in match.groups()]
                start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000.0
                end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000.0
                text = " ".join(block_lines[timing_index + 1:]).strip()
                if text and end > start:
                    caption_segments.append(SubtitleSegment(start=start, end=end, text=text))
        caption_track_issues = SubtitleComposer(
            max_words_per_caption=(
                8
                if content_kind == "video"
                else self.config.app.subtitles.max_words_per_caption
            )
        ).quality_issues(
            caption_segments,
            # Match render-time short caption policy: after wider-budget reflow,
            # readable clause continuations are accepted (same as long-form).
            allow_clause_continuations=True,
        )
        caption_word_counts = [SubtitleComposer.visible_word_count(line) for line in caption_lines]
        caption_max_words = max(caption_word_counts, default=0)
        caption_average_words = round(sum(caption_word_counts) / max(1, len(caption_word_counts)), 2)
        orphan_captions = sum(1 for count in caption_word_counts if count <= 1)
        dangling_caption_pattern = re.compile(
            r"\b(?:a|an|the|of|to|under|more|first|how|because|larger|less|is|are|was|were)[,;:]?$",
            flags=re.IGNORECASE,
        )
        awkward_captions = 0
        for caption_index, line in enumerate(caption_lines):
            has_next_line = caption_index + 1 < len(caption_lines)
            if has_next_line and dangling_caption_pattern.search(line):
                awkward_captions += 1
            # Mid-phrase cue starts are governed by SubtitleComposer.quality_issues
            # with allow_clause_continuations=True (aligned with render policy).
        if not caption_lines:
            caption_issue = "subtitles are missing"
            if caption_issue not in issues:
                issues.append(caption_issue)
            blocking_issues.append(caption_issue)
            score -= 24
        else:
            caption_limit = (
                8
                if content_kind == "video"
                else SubtitleComposer(
                    max_words_per_caption=self.config.app.subtitles.max_words_per_caption
                ).hard_word_limit()
            )
            if caption_max_words > caption_limit:
                caption_issue = f"caption contains {caption_max_words} words; maximum is {caption_limit}"
                if caption_issue not in issues:
                    issues.append(caption_issue)
                blocking_issues.append(caption_issue)
                score -= 18
            else:
                strengths.append(f"captions average {caption_average_words} words")
        if orphan_captions:
            orphan_issue = f"{orphan_captions} one-word captions break reading flow"
            if orphan_issue not in issues:
                issues.append(orphan_issue)
            blocking_issues.append(orphan_issue)
            score -= 8
        if awkward_captions:
            caption_issue = f"{awkward_captions} captions split in the middle of a phrase"
            issues.append(caption_issue)
            blocking_issues.append(caption_issue)
            score -= 18
        if caption_track_issues:
            for caption_issue in caption_track_issues[:8]:
                formatted = f"Subtitle timing: {caption_issue}"
                if formatted not in issues:
                    issues.append(formatted)
                blocking_issues.append(formatted)
            score -= min(30, 6 * len(caption_track_issues))

        if duration_seconds > 0:
            narration_wpm = round(word_count / (duration_seconds / 60.0), 1)
            pacing_scope = "long-video " if content_kind == "video" else ""
            if channel.id == "brain_lens" and not (135 <= narration_wpm <= 170):
                pacing_issue = f"Brain Lens {pacing_scope}narration pacing is {narration_wpm} WPM; target is 135-170"
                if pacing_issue not in issues:
                    issues.append(pacing_issue)
                blocking_issues.append(pacing_issue)
                score -= 18
            elif channel.id == "ancient_history" and not (135 <= narration_wpm <= 170):
                pacing_issue = f"Ancient History {pacing_scope}narration pacing is {narration_wpm} WPM; target is 135-170"
                if pacing_issue not in issues:
                    issues.append(pacing_issue)
                blocking_issues.append(pacing_issue)
                score -= 18
            elif 135 <= narration_wpm <= 175:
                strengths.append(f"natural narration pace ({narration_wpm} WPM)")
        else:
            narration_wpm = 0.0

        script_subscore = 100
        if title_issue:
            script_subscore -= 28
        script_subscore -= min(36, len(script_issues) * 12)
        if content_kind == "short" and channel.id == "brain_lens":
            min_words = int(getattr(self.script_writer, "_BRAIN_SHORT_MIN_WORDS", 122) or 122)
            max_words = int(getattr(self.script_writer, "_BRAIN_SHORT_MAX_WORDS", 142) or 142)
            if not (min_words <= word_count <= max_words):
                script_subscore -= 22
        if content_kind == "short" and channel.id == "ancient_history":
            min_words = int(getattr(self.script_writer, "_ANCIENT_SHORT_MIN_WORDS", 120) or 120)
            max_words = int(getattr(self.script_writer, "_ANCIENT_SHORT_MAX_WORDS", 148) or 148)
            if not (min_words <= word_count <= max_words):
                script_subscore -= 22
        if any("opener" in issue or "hook" in issue for issue in issues):
            script_subscore -= 22
        if thumbnail_quality_issues:
            script_subscore -= min(24, 12 * len(thumbnail_quality_issues))
        voice_subscore = 100 if voice_matches else 25
        visual_subscore = 100
        if real_sources == 0:
            visual_subscore -= 55
        if visual_asset_count < minimum_assets:
            visual_subscore -= 24
        if len(unique_visual_urls) < minimum_unique_visuals:
            visual_subscore -= 32
        if channel.id == "ancient_history" and unknown_license_count > max(1, len(sources) // 5):
            visual_subscore -= 28
        if channel.id == "ancient_history" and any("unrelated artifact/tourism" in issue for issue in issues):
            visual_subscore -= 28
        if channel.id == "ancient_history" and any("lacks two clearly explanatory" in issue for issue in issues):
            visual_subscore -= 30
        if channel.id == "ancient_history" and any("hook lacks a visible" in issue for issue in issues):
            visual_subscore -= 28
        if any("visual timeline" in issue or "diagram cut" in issue for issue in issues):
            visual_subscore -= 35
        if channel.id == "brain_lens" and content_kind == "short" and motion_asset_count < 4:
            visual_subscore -= 32
        if channel.id == "brain_lens" and content_kind == "short" and (
            not motion_analysis.get("available")
            or float(motion_analysis.get("dynamic_ratio") or 0.0) < 0.5
        ):
            visual_subscore -= 35
        if channel.id == "brain_lens" and any("couple-and-phone" in issue for issue in issues):
            visual_subscore -= 28
        if channel.id == "brain_lens" and any(
            marker in issue
            for issue in issues
            for marker in (
                "relationship topic has only",
                "repeats phone footage",
                "repeats affection footage",
                "repeats conversation footage",
                "generated stills",
                "off-tone stock visual",
                "dating-app hook lacks",
            )
        ):
            visual_subscore -= 35
        captions_subscore = 100
        if not caption_lines:
            captions_subscore = 20
        else:
            if caption_max_words > (8 if content_kind == "video" else 6):
                captions_subscore -= 40
            if caption_average_words > (6.4 if content_kind == "video" else 4.8):
                captions_subscore -= 18
            captions_subscore -= min(18, orphan_captions * 6)
            captions_subscore -= min(36, awkward_captions * 12)
            captions_subscore -= min(50, len(caption_track_issues) * 15)
        audio_subscore = 100 if music_ok and voice_matches else (55 if voice_matches else 25)
        subscores = {
            "script": max(0, script_subscore),
            "voice": max(0, voice_subscore),
            "visuals": max(0, visual_subscore),
            "captions": max(0, captions_subscore),
            "audio_mix": max(0, audio_subscore),
        }
        experience_score = round(
            (subscores["script"] * 0.30)
            + (subscores["voice"] * 0.20)
            + (subscores["visuals"] * 0.30)
            + (subscores["captions"] * 0.12)
            + (subscores["audio_mix"] * 0.08)
        )
        score = min(score, experience_score)

        ai_review = self.script_writer.review_content_quality(channel, topic, metadata)
        if ai_review:
            advisory_issues = []
            for issue in ai_review.get("issues", [])[:6]:
                clean_issue = str(issue).strip()
                lowered_issue = clean_issue.lower()
                if any(term in lowered_issue for term in ("visual", "footage", "image", "audio", "voice", "caption", "subtitle")):
                    continue
                if any(term in lowered_issue for term in ("hook", "intro", "opening")) and not hook_issue:
                    continue
                if channel.id == "ancient_history" and any(
                    term in lowered_issue
                    for term in ("human behavior", "body cue", "viewer action", "psychology term")
                ):
                    continue
                advisory_issues.append(clean_issue)
            ai_review["advisory_issues"] = advisory_issues
            ai_review["validated_issues"] = (
                advisory_issues if ai_review.get("verdict") == "hold" else []
            )
            if ai_review.get("verdict") == "hold" and advisory_issues:
                issues.extend(f"AI script review: {issue}" for issue in advisory_issues[:2])
                score -= min(4, len(advisory_issues) * 2)
            elif advisory_issues:
                strengths.append(
                    f"AI advisory passed with {len(advisory_issues)} non-blocking suggestion(s) retained"
                )
            else:
                strengths.append("AI script review found no validated issue")

        score = max(0, min(100, score))
        pass_threshold = 95 if content_kind == "short" else 90
        effective_threshold = 94 if content_kind == "short" and not issues else pass_threshold
        decision = "pass" if score >= effective_threshold and not blocking_issues and not any("missing" in issue for issue in issues) else "hold"
        return {
            "score": score,
            "decision": decision,
            "issues": issues,
            "blocking_issues": list(dict.fromkeys(blocking_issues)),
            "strengths": strengths,
            "ai_review": ai_review,
            "subscores": subscores,
            "source_counts": dict(source_counts),
            "motion_analysis": motion_analysis,
            "visual_timeline_entries": len(visual_timeline),
            "duration_seconds": round(duration_seconds, 2),
            "narration_wpm": narration_wpm,
            "caption_max_words": caption_max_words,
            "caption_average_words": caption_average_words,
            "title": title,
        }

    def performance_report(self, channel_id: str, days: int = 28, max_results: int = 100) -> dict:
        channel = self._channel(channel_id)
        end_date = now_in_tz(self.config.app.timezone).date()
        start_date = end_date - timedelta(days=max(1, int(days)))

        recent = self.youtube.list_recent_videos(channel, max_results=max_results)
        runs_by_video_id = {
            str(run.get("youtube_id")): run
            for run in self._read_run_log()
            if run.get("channel") == channel_id and run.get("youtube_id")
        }

        videos = []
        hourly: dict[int, list[int]] = {}
        duration_buckets: dict[str, list[int]] = {}
        for item in recent:
            published_at = str(item.get("published_at") or "")
            try:
                published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(
                    now_in_tz(self.config.app.timezone).tzinfo
                )
                hour = int(published_dt.strftime("%H"))
            except Exception:
                hour = -1
            views = int(item.get("views") or 0)
            hourly.setdefault(hour, []).append(views)

            run = runs_by_video_id.get(str(item.get("video_id") or "")) or {}
            metadata = read_json(Path(str(run.get("metadata_path") or "")), {}) if run.get("metadata_path") else {}
            content_kind = str(run.get("content_kind") or metadata.get("content_kind") or "short")
            duration = metadata.get("duration_seconds")
            if duration is None:
                duration_label = "unknown"
            elif duration < 30:
                duration_label = "<30s"
            elif duration < 33:
                duration_label = "30-33s"
            elif duration < 36:
                duration_label = "33-36s"
            else:
                duration_label = "36s+"
            duration_buckets.setdefault(duration_label, []).append(views)
            videos.append({
                "video_id": item.get("video_id"),
                "title": item.get("title"),
                "views": views,
                "likes": int(item.get("likes") or 0),
                "comments": int(item.get("comments") or 0),
                "published_hour": hour,
                "duration_seconds": duration,
                "content_kind": content_kind,
                "run_dir": run.get("run_dir"),
            })

        def _bucket_stats(source: dict) -> list[dict]:
            out = []
            for key, values in source.items():
                if key == -1 or not values:
                    continue
                out.append({
                    "key": key,
                    "count": len(values),
                    "avg_views": round(sum(values) / len(values), 1),
                    "max_views": max(values),
                    "zero_count": sum(1 for value in values if value == 0),
                })
            return sorted(out, key=lambda row: (row["avg_views"], row["count"]), reverse=True)

        def _safe_int(row: list, index: int) -> int:
            try:
                return int(row[index] or 0)
            except (IndexError, TypeError, ValueError):
                return 0

        def _safe_float(row: list, index: int) -> float:
            try:
                return float(row[index] or 0)
            except (IndexError, TypeError, ValueError):
                return 0.0

        def _video_virality_score(
            views: int,
            engaged_views: int,
            average_view_duration: int,
            likes: int,
            comments: int,
            shares: int,
            subscribers_gained: int,
        ) -> dict:
            if views <= 0:
                return {
                    "score": 0.0,
                    "sample_confidence": 0.0,
                    "components": {
                        "engaged_rate": 0.0,
                        "retention": 0.0,
                        "actions": 0.0,
                        "volume": 0.0,
                    },
                }

            engaged_rate = min(1.0, engaged_views / views)
            retention_component = min(max(average_view_duration, 0), 50) / 50 * 25
            engaged_component = engaged_rate * 35
            action_component = min(
                25,
                (likes / views * 100)
                + (comments / views * 220)
                + (shares / views * 300)
                + (subscribers_gained / views * 500),
            )
            volume_component = min(15, views / 1000 * 15)
            sample_confidence = min(1.0, (views / 100) ** 0.5)
            if views < 25:
                sample_confidence *= views / 25

            quality_score = (engaged_component + retention_component + action_component) * sample_confidence
            score = quality_score + volume_component
            return {
                "score": round(score, 2),
                "sample_confidence": round(sample_confidence, 3),
                "components": {
                    "engaged_rate": round(engaged_component, 2),
                    "retention": round(retention_component, 2),
                    "actions": round(action_component, 2),
                    "volume": round(volume_component, 2),
                },
            }

        def _analytics_summary(data: dict) -> dict:
            if data.get("error"):
                return {"error": data["error"]}
            rows = data.get("rows") or []
            if not rows:
                return {}

            total_views = sum(_safe_int(row, 1) for row in rows)
            total_engaged = sum(_safe_int(row, 2) for row in rows)
            total_minutes = sum(_safe_int(row, 3) for row in rows)
            weighted_duration = sum(_safe_int(row, 4) * _safe_int(row, 1) for row in rows)
            weighted_percentage = sum(_safe_float(row, 5) * _safe_int(row, 2) for row in rows)
            return {
                "total_views": total_views,
                "total_engaged_views": total_engaged,
                "engaged_view_rate": round(total_engaged / max(total_views, 1), 3),
                "estimated_minutes_watched": total_minutes,
                "average_view_duration": round(weighted_duration / max(total_views, 1), 1),
                "average_view_percentage": round(weighted_percentage / max(total_engaged, 1), 1),
                "likes": sum(_safe_int(row, 6) for row in rows),
                "comments": sum(_safe_int(row, 7) for row in rows),
                "shares": sum(_safe_int(row, 8) for row in rows),
                "subscribers_gained": sum(_safe_int(row, 9) for row in rows),
                "top_days": sorted(
                    [
                        {
                            "day": row[0],
                            "views": _safe_int(row, 1),
                            "engaged_views": _safe_int(row, 2),
                            "average_view_duration": _safe_int(row, 4),
                            "average_view_percentage": round(_safe_float(row, 5), 1),
                        }
                        for row in rows
                    ],
                    key=lambda row: row["views"],
                    reverse=True,
                )[:5],
            }

        analytics = {}
        video_analytics_rows: list[dict] = []
        try:
            analytics = self.youtube.analytics_report(
                channel=channel,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                metrics="views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained",
                dimensions="day",
                sort="day",
            )
        except Exception as exc:
            analytics = {"error": str(exc)}

        try:
            video_report = self.youtube.analytics_report(
                channel=channel,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                metrics="views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained",
                dimensions="video",
                sort="-views",
                max_results=max_results,
            )
            video_ids = [str(row[0]) for row in video_report.get("rows", []) if row]
            details = self.youtube.video_details(channel, video_ids)
            for row in video_report.get("rows", []):
                if len(row) < 10:
                    continue
                video_id = str(row[0])
                detail = details.get(video_id, {})
                run = runs_by_video_id.get(video_id) or {}
                metadata = read_json(Path(str(run.get("metadata_path") or "")), {}) if run.get("metadata_path") else {}
                content_kind = str(run.get("content_kind") or metadata.get("content_kind") or "unknown")
                duration_seconds = metadata.get("duration_seconds")
                views = _safe_int(row, 1)
                engaged = _safe_int(row, 2)
                avg_duration = _safe_int(row, 4)
                avg_percentage = _safe_float(row, 5)
                likes = _safe_int(row, 6)
                comments = _safe_int(row, 7)
                shares = _safe_int(row, 8)
                subscribers_gained = _safe_int(row, 9)
                score = _video_virality_score(
                    views=views,
                    engaged_views=engaged,
                    average_view_duration=avg_duration,
                    likes=likes,
                    comments=comments,
                    shares=shares,
                    subscribers_gained=subscribers_gained,
                )
                video_analytics_rows.append({
                    "video_id": video_id,
                    "title": detail.get("title") or video_id,
                    "published_at": detail.get("published_at"),
                    "content_kind": content_kind,
                    "duration_seconds": duration_seconds,
                    "run_dir": run.get("run_dir"),
                    "views": views,
                    "engaged_views": engaged,
                    "engaged_view_rate": round(engaged / max(views, 1), 3),
                    "estimated_minutes_watched": _safe_int(row, 3),
                    "average_view_duration": avg_duration,
                    "average_view_percentage": round(avg_percentage, 1),
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "subscribers_gained": subscribers_gained,
                    "virality_score": score["score"],
                    "sample_confidence": score["sample_confidence"],
                    "score_components": score["components"],
                })
        except Exception as exc:
            video_analytics_rows = [{"error": str(exc)}]

        hour_stats = _bucket_stats(hourly)
        duration_stats = _bucket_stats(duration_buckets)
        analytics_rows = [row for row in video_analytics_rows if "error" not in row]
        top_by_analytics = sorted(
            analytics_rows,
            key=lambda row: (row["virality_score"], row["views"]),
            reverse=True,
        )[:20]

        def _top_title_terms(rows: list[dict]) -> list[str]:
            stop = {
                "about", "after", "behind", "from", "that", "this", "what", "when",
                "where", "which", "with", "your", "youre", "they", "them", "into",
                "history", "psychology", "ancient", "brain", "bizarre", "true",
                "story", "shocking", "secret", "uncovered", "uncovering", "does",
                "before", "notice", "feels", "personal", "quietly",
            }
            counts: Counter[str] = Counter()
            for row in rows[:12]:
                for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", str(row.get("title") or "").lower()):
                    if word not in stop:
                        counts[word] += 1
            return [word for word, _ in counts.most_common(8)]

        def _format_summary(kind: str) -> dict:
            rows = [row for row in analytics_rows if str(row.get("content_kind") or "unknown") == kind]
            fallback_rows = [row for row in videos if str(row.get("content_kind") or "unknown") == kind]
            if not rows and fallback_rows:
                views = sum(int(row.get("views") or 0) for row in fallback_rows)
                return {
                    "count": len(fallback_rows),
                    "views": views,
                    "engaged_view_rate": None,
                    "average_view_duration": None,
                    "average_view_percentage": None,
                    "likes": sum(int(row.get("likes") or 0) for row in fallback_rows),
                    "comments": sum(int(row.get("comments") or 0) for row in fallback_rows),
                    "shares": None,
                    "subscribers_gained": None,
                    "top": sorted(fallback_rows, key=lambda row: int(row.get("views") or 0), reverse=True)[:5],
                    "weak": sorted(fallback_rows, key=lambda row: int(row.get("views") or 0))[:5],
                }
            total_views = sum(int(row.get("views") or 0) for row in rows)
            total_engaged = sum(int(row.get("engaged_views") or 0) for row in rows)
            weighted_duration = sum(float(row.get("average_view_duration") or 0) * int(row.get("views") or 0) for row in rows)
            weighted_pct = sum(float(row.get("average_view_percentage") or 0) * int(row.get("engaged_views") or 0) for row in rows)
            return {
                "count": len(rows),
                "views": total_views,
                "engaged_view_rate": round(total_engaged / max(total_views, 1), 3) if rows else None,
                "average_view_duration": round(weighted_duration / max(total_views, 1), 1) if rows else None,
                "average_view_percentage": round(weighted_pct / max(total_engaged, 1), 1) if rows else None,
                "likes": sum(int(row.get("likes") or 0) for row in rows),
                "comments": sum(int(row.get("comments") or 0) for row in rows),
                "shares": sum(int(row.get("shares") or 0) for row in rows),
                "subscribers_gained": sum(int(row.get("subscribers_gained") or 0) for row in rows),
                "top": sorted(rows, key=lambda row: (float(row.get("virality_score") or 0), int(row.get("views") or 0)), reverse=True)[:5],
                "weak": sorted(rows, key=lambda row: (int(row.get("views") or 0), float(row.get("engaged_view_rate") or 0)))[:5],
            }

        shorts_summary = _format_summary("short")
        long_summary = _format_summary("video")

        def _recommendations() -> list[str]:
            notes: list[str] = []
            reliable_hours = [row for row in hour_stats if row["count"] >= 3]
            if reliable_hours:
                hours = ", ".join(f"{row['key']:02d}:00" for row in reliable_hours[:3])
                notes.append(f"Prioritize publishing tests around {hours}; those hours have the best current view averages with repeat samples.")
            elif hour_stats:
                hours = ", ".join(f"{row['key']:02d}:00" for row in hour_stats[:3])
                notes.append(f"Keep testing {hours}, but treat hour winners as directional until each slot has 3+ samples.")

            reliable_durations = [row for row in duration_stats if row["count"] >= 3 and row["key"] != "unknown"]
            if reliable_durations:
                notes.append(f"Bias new Shorts toward the {reliable_durations[0]['key']} duration bucket; it has the strongest current average.")

            low_sample = sum(1 for row in analytics_rows if row.get("views", 0) < 25)
            if low_sample:
                notes.append("Ignore virality winners under 25 views for creative decisions; the report now down-weights those low-confidence samples.")

            title_terms = _top_title_terms(top_by_analytics or sorted(videos, key=lambda row: row["views"], reverse=True))
            if title_terms:
                notes.append("Use proven title context more often: " + ", ".join(title_terms[:6]) + ".")

            summary = _analytics_summary(analytics)
            if summary and not summary.get("error"):
                rate = float(summary.get("engaged_view_rate") or 0)
                avg_view = float(summary.get("average_view_duration") or 0)
                avg_pct = float(summary.get("average_view_percentage") or 0)
                if rate < 0.45:
                    notes.append("Improve first 2 seconds: engaged-view rate is below the 45% target for stronger Shorts feed testing.")
                if avg_pct and avg_pct < 70:
                    notes.append("Increase completion: average percentage viewed is below the 70% Shorts retention floor.")
                if avg_view < 28:
                    notes.append("Tighten pacing and reduce intro setup: average view duration is below the 28-second target.")
            if shorts_summary.get("count"):
                shorts_pct = shorts_summary.get("average_view_percentage")
                shorts_rate = shorts_summary.get("engaged_view_rate")
                if shorts_rate is not None and float(shorts_rate) < 0.45:
                    notes.append("Shorts action: keep testing stronger first-frame hooks; Shorts engaged rate is still below 45%.")
                if shorts_pct is not None and float(shorts_pct) < 70:
                    notes.append("Shorts action: reduce setup and make every 2-3 seconds a new visual/reveal; completion is below 70%.")
            if not long_summary.get("count"):
                notes.append("Long-video action: no long-form analytics sample yet; hold volume at 1/day until first 3 long videos have retention data.")
            else:
                long_avg = float(long_summary.get("average_view_duration") or 0)
                if long_avg < 180:
                    notes.append("Long-video action: first 30 seconds and section pacing need work; average view duration is below 3 minutes.")
                else:
                    notes.append("Long-video action: keep 8-10 minute format and compare topics after at least 3 uploads.")
            return notes

        report = {
            "channel": channel_id,
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "top_videos": sorted(videos, key=lambda row: row["views"], reverse=True)[:15],
            "bottom_videos": sorted(videos, key=lambda row: row["views"])[:15],
            "top_by_analytics": top_by_analytics,
            "format_summary": {
                "shorts": shorts_summary,
                "long_videos": long_summary,
            },
            "best_publish_hours": hour_stats[:8],
            "duration_buckets": duration_stats,
            "recommendations": _recommendations(),
            "analytics_summary": _analytics_summary(analytics),
            "analytics_api": analytics,
            "video_analytics": video_analytics_rows[:50],
        }
        write_json(self.config.app.state_dir / f"{channel_id}_performance_report.json", report)
        return report

    def _backlog_path(self, channel_id: str) -> Path:
        return self.config.app.state_dir / f"{channel_id}_backlog.json"

    def _load_backlog(self, channel_id: str) -> list[dict]:
        data = read_json(self._backlog_path(channel_id), {"items": []})
        return data.get("items") or []

    def _save_backlog(self, channel_id: str, items: list[dict]) -> None:
        write_json(self._backlog_path(channel_id), {"channel": channel_id, "items": items})

    def _is_transient_upload_error(self, message: str) -> bool:
        lowered = str(message or "").lower()
        transient_terms = (
            "unable to find the server",
            "name or service not known",
            "temporary failure",
            "dns",
            "name resolution",
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "connection refused",
            "502",
            "503",
            "504",
        )
        return any(term in lowered for term in transient_terms)

    def _queue_transient_upload_retry(
        self,
        channel: ChannelConfig,
        run_dir: Path,
        video_path: Path,
        metadata_path: Path,
        title: str,
        error: str,
        up_results: dict,
    ) -> None:
        if not self._is_transient_upload_error(error):
            return
        if not video_path.exists() or video_path.stat().st_size < 1_000_000:
            return

        run_key = str(run_dir)
        items = self._load_backlog(channel.id)
        for item in items:
            if str(item.get("run_dir") or "") == run_key:
                item.update({
                    "status": "pending",
                    "error": error,
                    "youtube_id": item.get("youtube_id") or up_results.get("youtube_id"),
                    "facebook_id": item.get("facebook_id") or up_results.get("facebook_id"),
                })
                self._save_backlog(channel.id, items)
                self.logger.warning(channel.id, "Queued transient upload failure for retry.")
                return

        items.append({
            "run_dir": run_key,
            "video_path": str(video_path),
            "metadata_path": str(metadata_path),
            "title": title,
            "status": "pending",
            "attempts": 1,
            "youtube_id": up_results.get("youtube_id"),
            "facebook_id": up_results.get("facebook_id"),
            "error": error,
        })
        self._save_backlog(channel.id, items)
        self.logger.warning(channel.id, "Queued transient upload failure for retry.")

    def _resolve_video_path(self, run_path: Path, content_kind: str | None = None) -> Path | None:
        candidates: list[Path] = []
        # ALWAYS check short.mp4 first as it's the modern/preferred format for Reels
        candidates.append(run_path / "short.mp4")
        candidates.append(run_path / "video.mp4")
        
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists() and candidate.stat().st_size >= 1_000_000:
                return candidate
        return None

    def _build_backlog_item(
        self,
        run_dir: str,
        title: str,
        metadata_path: Path,
        video_path: Path,
        existing: dict | None = None,
        run: dict | None = None,
        channel: ChannelConfig | None = None,
    ) -> dict:
        existing = existing or {}
        run = run or {}
        metadata = read_json(metadata_path, {}) if metadata_path.exists() else {}
        
        # If the item already exists in the backlog, respect its current upload IDs
        if "youtube_id" in existing:
            youtube_id = existing.get("youtube_id")
        else:
            youtube_id = run.get("youtube_id") or None

        if "facebook_id" in existing:
            facebook_id = existing.get("facebook_id")
        else:
            facebook_id = (
                run.get("facebook_id")
                or run.get("facebook_video_id")
                or None
            )

        error = existing.get("error") or run.get("youtube_error") or run.get("facebook_error") or None

        status = existing.get("status", "pending")
        if status == "in_progress" and not (facebook_id or youtube_id):
            status = "pending"

        if channel is not None:
            target_probe = {
                "youtube_id": youtube_id,
                "facebook_id": facebook_id,
            }
            is_complete = self._backlog_item_is_complete(channel, target_probe)
            if is_complete:
                status = "posted"
            elif status == "posted":
                status = "pending"

        skip_reason = ""
        skip_status = "skipped_quality"
        quality_decision = str(metadata.get("quality_decision") or "").lower()
        content_kind = str(metadata.get("content_kind") or run.get("content_kind") or "").lower()
        duration_seconds = float(metadata.get("duration_seconds") or 0)
        if not metadata:
            skip_reason = "missing metadata"
        elif metadata.get("upload_eligible") is False:
            skip_reason = "build-only/test render"
            skip_status = "skipped_build_only"
        elif str(run.get("upload_skipped") or "").lower() == "build_only":
            skip_reason = "build-only/test render"
            skip_status = "skipped_build_only"
        elif quality_decision in {"hold", "reject"}:
            skip_reason = f"quality gate: {metadata.get('quality_issue') or metadata.get('quality_issues') or quality_decision}"
        elif str(run.get("upload_skipped") or "").lower() == "quality_gate":
            skip_reason = "quality gate: previous upload skipped"
        elif content_kind == "short" and duration_seconds and duration_seconds < 18:
            skip_reason = f"too short ({duration_seconds:.1f}s)"
        if skip_reason and status not in {"posted", "skipped_quality", "skipped_build_only"}:
            status = skip_status
            error = skip_reason

        attempts = int(existing.get("attempts", 0) or 0)
        item = {
            "run_dir": run_dir,
            "video_path": str(video_path),
            "metadata_path": str(metadata_path),
            "title": title,
            "status": status,
            "attempts": attempts,
            "youtube_id": youtube_id,
            "facebook_id": facebook_id,
            "error": error,
        }
        # Preserve other custom fields from existing backlog (e.g. old_personal_youtube_id, etc.)
        for k, v in existing.items():
            if k not in item:
                item[k] = v
        if channel is not None and item.get("status") in {"pending", "failed"}:
            stale_issue = self._backlog_item_stale_issue(channel, item)
            if stale_issue:
                item["status"] = "skipped_quality"
                item["error"] = stale_issue
        return item

    def _caption_chunks_for_heygen(self, text: str, max_words: int = 4) -> list[str]:
        words = re.findall(r"[A-Za-z0-9']+|[^\sA-Za-z0-9]", text or "")
        chunks = []
        current = []
        word_count = 0
        for token in words:
            current.append(token)
            if re.match(r"[A-Za-z0-9']+", token):
                word_count += 1
            if word_count >= max_words or token in {".", "?", "!"}:
                chunk = " ".join(current).replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!").strip()
                if chunk:
                    chunks.append(chunk)
                current = []
                word_count = 0
        if current:
            chunk = " ".join(current).replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!").strip()
            if chunk:
                chunks.append(chunk)
        return chunks[:24]

    def _add_heygen_short_overlays(self, video_path: Path, narration_text: str, logo_path: Path | None) -> None:
        if not video_path.exists():
            return
        try:
            import imageio_ffmpeg
            from PIL import Image
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return

        try:
            from moviepy.editor import VideoFileClip
            probe = VideoFileClip(str(video_path))
            try:
                duration = float(probe.duration or 0)
                width, height = int(probe.w), int(probe.h)
            finally:
                probe.close()
        except Exception:
            return
        if duration <= 0:
            return

        temp_dir = video_path.with_name(f"{video_path.stem}_overlays")
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = video_path.with_name(f"{video_path.stem}_captioned{video_path.suffix}")
        inputs = ["-i", str(video_path)]
        filters = []
        current = "0:v"
        input_index = 1
        try:
            composer = SubtitleComposer(max_words_per_caption=4)
            chunks = self._caption_chunks_for_heygen(narration_text, max_words=4)
            if chunks:
                slot = duration / len(chunks)
                for index, chunk in enumerate(chunks[:18]):
                    try:
                        caption_img = composer.render_caption_image(chunk, width=min(820, int(width * 0.82)))
                        caption_path = temp_dir / f"caption_{index:02d}.png"
                        Image.fromarray(caption_img).save(caption_path)
                        inputs.extend(["-i", str(caption_path)])
                        start_t = max(0.0, index * slot)
                        end_t = min(duration, start_t + slot + 0.08)
                        out_label = f"v_caption_{index}"
                        y_pos = int(height * 0.71)
                        filters.append(
                            f"[{current}][{input_index}:v]overlay=(W-w)/2:{y_pos}:enable='between(t,{start_t:.2f},{end_t:.2f})'[{out_label}]"
                        )
                        current = out_label
                        input_index += 1
                    except Exception:
                        continue
            if logo_path and logo_path.exists():
                try:
                    logo_width = max(90, int(width * 0.13))
                    inputs.extend(["-i", str(logo_path)])
                    out_label = "v_logo"
                    filters.append(
                        f"[{input_index}:v]scale={logo_width}:-1[logo];[{current}][logo]overlay={int(width*0.04)}:{int(height*0.035)}[{out_label}]"
                    )
                    current = out_label
                    input_index += 1
                except Exception:
                    pass
            if not filters:
                return
            command = [
                ffmpeg_exe,
                "-y",
                *inputs,
                "-filter_complex", ";".join(filters),
                "-map", f"[{current}]",
                "-map", "0:a?",
                *resolve_h264_encode_profile(ffmpeg_exe).argv(preset="veryfast", crf=20, threads=2),
                "-c:a", "aac",
                "-b:a", "160k",
                "-movflags", "+faststart",
                str(temp_path),
            ]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=420)
            if temp_path.exists() and temp_path.stat().st_size > 100000:
                temp_path.replace(video_path)
        except Exception as error:
            print(f"HEYGEN_OVERLAY_SKIPPED: {error}", flush=True)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    def _normalize_video_dimensions(self, video_path: Path, target_size: tuple[int, int]) -> None:
        target_w, target_h = int(target_size[0]), int(target_size[1])
        if target_w <= 0 or target_h <= 0 or not video_path.exists():
            return
        try:
            from moviepy.editor import VideoFileClip
            clip = VideoFileClip(str(video_path))
            try:
                if list(clip.size) == [target_w, target_h]:
                    return
            finally:
                clip.close()
        except Exception:
            pass

        temp_path = video_path.with_name(f"{video_path.stem}_normalized{video_path.suffix}")
        vf = (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setsar=1"
        )
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = "ffmpeg"
        command = [
            ffmpeg_exe, "-y", "-i", str(video_path),
            "-vf", vf,
            *resolve_h264_encode_profile(ffmpeg_exe).argv(preset="veryfast", crf=20, threads=2),
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            str(temp_path),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
            if temp_path.exists() and temp_path.stat().st_size > 100000:
                temp_path.replace(video_path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    def _probe_video_duration(self, video_path: Path) -> float:
        try:
            from moviepy.editor import VideoFileClip
            clip = VideoFileClip(str(video_path))
            try:
                return float(clip.duration or 0.0)
            finally:
                clip.close()
        except Exception:
            return 0.0

    def _measure_video_motion(self, video_path: Path, sample_count: int = 10) -> dict:
        clip = None
        try:
            import numpy as np
            from PIL import Image
            from moviepy.editor import VideoFileClip

            clip = VideoFileClip(str(video_path), audio=False)
            duration = float(clip.duration or 0.0)
            if duration < 2.0:
                return {"available": False, "reason": "video too short for motion sampling"}
            end_time = max(0.9, duration - 1.2)
            differences: list[float] = []
            for sample_time in np.linspace(0.8, end_time, max(4, int(sample_count))):
                comparison_time = min(duration - 0.05, float(sample_time) + 0.32)
                first = Image.fromarray(clip.get_frame(float(sample_time))).resize((90, 160)).convert("L")
                second = Image.fromarray(clip.get_frame(comparison_time)).resize((90, 160)).convert("L")
                first_array = np.asarray(first, dtype=np.float32)
                second_array = np.asarray(second, dtype=np.float32)
                differences.append(float(np.mean(np.abs(first_array - second_array))))
            dynamic_samples = sum(value >= 2.0 for value in differences)
            return {
                "available": True,
                "sample_count": len(differences),
                "dynamic_samples": dynamic_samples,
                "dynamic_ratio": round(dynamic_samples / max(1, len(differences)), 3),
                "mean_frame_difference": round(float(np.mean(differences)), 3),
                "sample_differences": [round(value, 2) for value in differences],
            }
        except Exception as exc:
            return {"available": False, "reason": str(exc)}
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception:
                    pass

    def _probe_audio_duration(self, audio_path: Path) -> float:
        try:
            from moviepy.editor import AudioFileClip

            clip = AudioFileClip(str(audio_path))
            try:
                return float(clip.duration or 0.0)
            finally:
                clip.close()
        except Exception:
            return 0.0

    def _normalize_short_narration_duration(
        self,
        channel_id: str,
        narration_path: Path,
        scene_durations: list[float],
        target_seconds: float = 54.5,
        acceptable_bounds: tuple[float, float] = (50.0, 59.0),
        narration_word_count: int = 0,
        narration_beats: list[str] | None = None,
    ) -> tuple[list[float], float]:
        maximum_seconds = float(target_seconds)
        if narration_word_count > 0:
            target_seconds = self._short_narration_target_seconds(
                narration_word_count,
                maximum_seconds=maximum_seconds,
                minimum_seconds=min(acceptable_bounds[0], maximum_seconds),
            )
        caption_safe_target = self._caption_safe_short_target_seconds(
            scene_durations=scene_durations,
            narration_beats=narration_beats or [],
            desired_seconds=target_seconds,
            maximum_seconds=maximum_seconds,
            composer=self.video_builder.subtitle_composer,
        )
        if caption_safe_target > target_seconds + 0.005:
            self.logger.info(
                channel_id,
                f"Raised short narration target from {target_seconds:.2f}s to "
                f"{caption_safe_target:.2f}s to preserve subtitle readability.",
            )
        target_seconds = caption_safe_target
        if narration_word_count > 0:
            wpm_ceiling = narration_word_count * 60.0 / 135.0
            if target_seconds > wpm_ceiling + 0.05:
                self.logger.info(
                    channel_id,
                    f"Capped short narration target from {target_seconds:.2f}s to "
                    f"{wpm_ceiling:.2f}s to keep pacing at or above 135 WPM.",
                )
                target_seconds = wpm_ceiling
        # Stay inside the configured short window after WPM/caption clamps.
        target_seconds = max(
            float(acceptable_bounds[0]),
            min(float(acceptable_bounds[1]), float(target_seconds)),
        )
        acceptable_bounds = (target_seconds - 0.12, target_seconds + 0.12)
        current_duration = self._probe_audio_duration(narration_path)
        if current_duration <= 0:
            return scene_durations, current_duration
        if acceptable_bounds[0] <= current_duration <= acceptable_bounds[1]:
            return scene_durations, current_duration

        tempo = current_duration / target_seconds
        # Prefer natural pace, but allow a slightly wider atempo window so a
        # caption-safe target raise does not abort otherwise good shorts.
        # Outside 0.85-1.20: reject so planning can retry another candidate.
        if not 0.85 <= tempo <= 1.20:
            raise RuntimeError(
                "Short narration requires an unsafe voice-speed correction "
                f"(atempo={tempo:.3f}; safe range 0.85-1.20)."
            )
        if not 0.88 <= tempo <= 1.12:
            self.logger.warning(
                channel_id,
                f"Narration tempo {tempo:.3f} is outside ideal 0.88-1.12; "
                "applying bounded atempo so caption-safe timing can pass.",
            )

        temp_path = narration_path.with_name(f"{narration_path.stem}_normalized.wav")
        try:
            import imageio_ffmpeg

            command = [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(narration_path),
                "-filter:a",
                f"atempo={tempo:.8f}",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(temp_path),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            normalized_duration = self._probe_audio_duration(temp_path)
            if normalized_duration <= 0:
                raise RuntimeError("normalized narration has no measurable duration")
            os.replace(temp_path, narration_path)

            adjusted_durations = list(scene_durations)
            total_scene_duration = sum(adjusted_durations)
            if adjusted_durations and total_scene_duration > 0:
                adjusted_durations = [
                    normalized_duration * (duration / total_scene_duration)
                    for duration in adjusted_durations
                ]
                adjusted_durations[-1] += normalized_duration - sum(adjusted_durations)

            self.logger.info(
                channel_id,
                f"Normalized narration from {current_duration:.2f}s to {normalized_duration:.2f}s for retention timing.",
            )
            return adjusted_durations, normalized_duration
        except Exception as exc:
            self.logger.warning(channel_id, f"Narration timing correction failed; using original audio ({exc}).")
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return scene_durations, current_duration

    @staticmethod
    def _caption_safe_short_target_seconds(
        scene_durations: list[float],
        narration_beats: list[str],
        desired_seconds: float,
        maximum_seconds: float = 58.9,
        composer: SubtitleComposer | None = None,
    ) -> float:
        desired = max(0.05, min(float(maximum_seconds), float(desired_seconds)))
        clean_beats = [str(beat).strip() for beat in narration_beats if str(beat).strip()]
        durations = [max(0.05, float(duration)) for duration in scene_durations]
        if not clean_beats or len(durations) < len(clean_beats):
            return round(desired, 3)

        caption_composer = composer or SubtitleComposer(max_words_per_caption=4)
        planning_cps = max(10.0, caption_composer.max_cps - 0.2)
        durations = durations[: len(clean_beats)]
        duration_total = sum(durations)
        if duration_total <= 0:
            return round(desired, 3)

        def maximum_caption_cps(total_seconds: float) -> float:
            scale = total_seconds / duration_total
            scaled_durations = [duration * scale for duration in durations]
            scaled_durations[-1] += total_seconds - sum(scaled_durations)
            scenes = caption_composer.scene_segments_from_durations(
                clean_beats,
                scaled_durations,
                total_duration=total_seconds,
            )
            captions = caption_composer.caption_segments_from_scene_segments(scenes)
            return max(
                (
                    caption_composer._character_count(caption.text)
                    / max(0.001, caption.end - caption.start)
                    for caption in captions
                ),
                default=0.0,
            )

        if maximum_caption_cps(desired) <= planning_cps + 0.0001:
            return round(desired, 3)

        maximum = max(desired, float(maximum_seconds))
        if maximum_caption_cps(maximum) > planning_cps + 0.0001:
            return round(maximum, 3)

        low = desired
        high = maximum
        for _ in range(48):
            candidate = (low + high) / 2.0
            if maximum_caption_cps(candidate) <= planning_cps:
                high = candidate
            else:
                low = candidate
        return round(min(maximum, high + 0.01), 3)

    @staticmethod
    def _short_narration_target_seconds(
        narration_word_count: int,
        maximum_seconds: float = 58.9,
        minimum_seconds: float = 50.0,
        target_wpm: float = 142.0,
        minimum_wpm: float = 135.0,
        maximum_wpm: float = 170.0,
    ) -> float:
        if narration_word_count <= 0 or target_wpm <= 0:
            return float(maximum_seconds)
        pacing_seconds = narration_word_count * 60.0 / target_wpm
        # Keep final pacing inside the quality-gate WPM band whenever possible.
        if minimum_wpm > 0:
            maximum_seconds = min(float(maximum_seconds), narration_word_count * 60.0 / minimum_wpm)
        if maximum_wpm > 0:
            minimum_seconds = max(float(minimum_seconds), narration_word_count * 60.0 / maximum_wpm)
        if minimum_seconds > maximum_seconds:
            # Preserve the configured Short floor. Editorial QA rejects scripts
            # that are too sparse for this window before they reach rendering.
            return round(float(minimum_seconds), 3)
        return round(
            max(float(minimum_seconds), min(float(maximum_seconds), pacing_seconds)),
            3,
        )

    @staticmethod
    def _adaptive_recovery_voice_speed(
        current_duration: float,
        base_speed: float = 1.40,
        target_seconds: float = 54.5,
    ) -> float:
        if current_duration <= 0 or target_seconds <= 0:
            return round(base_speed, 2)
        estimated = base_speed * (current_duration / target_seconds)
        return round(max(0.90, min(1.60, estimated)), 2)

    def _overlay_heygen_presenter(self, video_path: Path, presenter_path: Path, content_kind: str) -> None:
        if not video_path.exists() or not presenter_path.exists():
            return
        temp_path = video_path.with_name(f"{video_path.stem}_presenter{video_path.suffix}")
        try:
            import imageio_ffmpeg

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            if content_kind == "short":
                presenter_width = 350
                overlay_x = "W-w-34"
                overlay_y = "H*0.115"
            else:
                presenter_width = 380
                overlay_x = "W-w-58"
                overlay_y = "H-h-58"
            filter_complex = (
                f"[1:v]scale={presenter_width}:-2[presenter];"
                f"[0:v][presenter]overlay={overlay_x}:{overlay_y}:eof_action=pass[v]"
            )
            cmd = [
                ffmpeg_exe,
                "-y",
                "-i",
                str(video_path),
                "-i",
                str(presenter_path),
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                *resolve_h264_encode_profile(ffmpeg_exe).argv(preset="veryfast", crf=21, threads=2),
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(temp_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if temp_path.exists() and temp_path.stat().st_size > 100000:
                temp_path.replace(video_path)
        except Exception as error:
            print(f"HEYGEN_PRESENTER_OVERLAY_SKIPPED: {error}", flush=True)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    def _presenter_card_image(self, avatar_path: Path, out_path: Path, target_size: tuple[int, int]) -> Path | None:
        try:
            from PIL import Image, ImageDraw

            target_w, target_h = target_size
            card_w = int(target_w * 0.56)
            card_h = int(target_h * 0.34)
            avatar = Image.open(avatar_path).convert("RGB")
            ratio = card_w / card_h
            source_ratio = avatar.width / max(1, avatar.height)
            if source_ratio > ratio:
                new_h = card_h
                new_w = int(new_h * source_ratio)
            else:
                new_w = card_w
                new_h = int(new_w / max(source_ratio, 0.1))
            avatar = avatar.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - card_w) // 2)
            top = max(0, int((new_h - card_h) * 0.25))
            avatar = avatar.crop((left, top, left + card_w, top + card_h)).convert("RGBA")

            pad = 18
            radius = 34
            card = Image.new("RGBA", (card_w + pad * 2, card_h + pad * 2), (0, 0, 0, 0))
            shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
            draw_shadow = ImageDraw.Draw(shadow)
            draw_shadow.rounded_rectangle(
                (8, 14, card.width - 8, card.height - 4),
                radius=radius,
                fill=(0, 0, 0, 125),
            )
            card.alpha_composite(shadow)

            mask = Image.new("L", (card_w, card_h), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.rounded_rectangle((0, 0, card_w, card_h), radius=radius, fill=255)
            card.paste(avatar, (pad, pad), mask)
            draw = ImageDraw.Draw(card)
            draw.rounded_rectangle(
                (pad, pad, pad + card_w, pad + card_h),
                radius=radius,
                outline=(255, 255, 255, 62),
                width=3,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            card.save(out_path)
            return out_path
        except Exception as exc:
            self.logger.warning("SYSTEM", f"Presenter card render skipped: {exc}")
            return None

    def _overlay_local_presenter(self, video_path: Path, avatar_path: Path, content_kind: str, target_size: tuple[int, int]) -> bool:
        if content_kind != "short" or not video_path.exists() or not avatar_path.exists():
            return False
        card_path = video_path.with_name("local_presenter_card.png")
        temp_path = video_path.with_name(f"{video_path.stem}_local_presenter{video_path.suffix}")
        card = self._presenter_card_image(avatar_path, card_path, target_size)
        if not card:
            return False
        try:
            import imageio_ffmpeg

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            command = [
                ffmpeg_exe,
                "-y",
                "-i",
                str(video_path),
                "-loop",
                "1",
                "-i",
                str(card),
                "-filter_complex",
                "[0:v][1:v]overlay=(W-w)/2:round(H*0.055):shortest=1:eof_action=repeat[v]",
                "-map",
                "[v]",
                "-map",
                "0:a?",
                *resolve_h264_encode_profile(ffmpeg_exe).argv(preset="veryfast", crf=20, threads=2),
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(temp_path),
            ]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            if temp_path.exists() and temp_path.stat().st_size > 100000:
                temp_path.replace(video_path)
                return True
        except Exception as exc:
            self.logger.warning("SYSTEM", f"Local presenter overlay skipped: {exc}")
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
        return False

    def _overlay_lipsynced_presenter(self, video_path: Path, presenter_path: Path, target_size: tuple[int, int]) -> bool:
        if not video_path.exists() or not presenter_path.exists():
            return False
        temp_path = video_path.with_name(f"{video_path.stem}_lipsynced_presenter{video_path.suffix}")
        try:
            import imageio_ffmpeg

            target_w, target_h = target_size
            presenter_w = int(target_w * 0.64)
            presenter_h = int(target_h * 0.30)
            presenter_duration = self._probe_video_duration(presenter_path)
            fade_start = max(2.5, presenter_duration - 0.45)
            filter_complex = (
                f"[1:v]scale={presenter_w}:{presenter_h}:force_original_aspect_ratio=increase,"
                f"crop={presenter_w}:{presenter_h},setsar=1,"
                f"fade=t=out:st={fade_start:.2f}:d=0.4,"
                "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.30:t=4[presenter];"
                "[0:v][presenter]overlay=(W-w)/2:round(H*0.045):eof_action=pass:shortest=0[v]"
            )
            command = [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(video_path),
                "-i",
                str(presenter_path),
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                *resolve_h264_encode_profile().argv(preset="veryfast", crf=19, threads=2),
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(temp_path),
            ]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            if temp_path.exists() and temp_path.stat().st_size > 100_000:
                temp_path.replace(video_path)
                return True
        except Exception as exc:
            self.logger.warning("SYSTEM", f"Lip-sync presenter overlay skipped: {exc}")
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
        return False

    def _should_use_presenter(self, channel: ChannelConfig, content_kind: str) -> bool:
        presenter = getattr(channel, "presenter", None)
        if not presenter or not presenter.enabled:
            return False
        if str(presenter.engine or "").lower() not in {
            "local_composer",
            "sadtalker_hybrid",
        }:
            return False
        if content_kind == "short":
            return bool(presenter.use_for_shorts)
        if content_kind == "video":
            return bool(presenter.use_for_videos)
        return False

    def _presenter_state_path(self, channel: ChannelConfig) -> Path:
        return self.config.app.state_dir / f"{channel.id}_presenter_state.json"

    def _select_presenter_avatar(self, channel: ChannelConfig, run_key: str) -> Path | None:
        presenter = getattr(channel, "presenter", None)
        if not presenter or not presenter.avatar_pool_dir:
            return None
        avatar_dir = Path(presenter.avatar_pool_dir)
        if not avatar_dir.exists():
            return None
        candidates = sorted(
            path
            for path in avatar_dir.iterdir()
            if (
                path.is_file()
                and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                and "contact_sheet" not in path.stem.lower()
                and "preview" not in path.stem.lower()
            )
        )
        if str(presenter.engine or "").lower() in {"audio_reactive_portrait", "rhubarb_phoneme_portrait"}:
            manifest_path = avatar_dir / "audio_sync_avatar_pool.txt"
            if manifest_path.exists():
                allowed_names = {
                    line.strip().lower()
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                }
                candidates = [path for path in candidates if path.name.lower() in allowed_names]
        if not candidates:
            return None
        if str(presenter.rotation_mode or "sequential").lower() == "random":
            return random.choice(candidates)

        state_path = self._presenter_state_path(channel)
        state = read_json(state_path, {})
        index = int(state.get("index", 0) or 0)
        selected = candidates[index % len(candidates)]
        write_json(state_path, {
            "index": index + 1,
            "last_avatar": str(selected),
            "last_run": run_key,
            "updated_at": now_in_tz(self.config.app.timezone).isoformat(),
        })
        return selected

    def _should_use_heygen(self, channel: ChannelConfig, content_kind: str) -> bool:
        heygen = getattr(channel, "heygen", None)
        if not heygen or not heygen.enabled:
            return False
        if content_kind == "short":
            return bool(heygen.use_for_shorts)
        if content_kind == "video":
            return bool(heygen.use_for_videos)
        return False

    def _heygen_config_dict(self, channel: ChannelConfig) -> dict:
        heygen = getattr(channel, "heygen", None)
        if not heygen:
            return {}
        data = asdict(heygen)
        data["login_email"] = data.get("login_email") or os.getenv("HEYGEN_EMAIL", "")
        data["login_password"] = data.get("login_password") or os.getenv("HEYGEN_PASSWORD", "")
        data["chrome_user_data_dir"] = data.get("chrome_user_data_dir") or str(self.config.app.state_dir / "heygen_chrome_profile")
        return data

    def _select_heygen_avatar(self, channel: ChannelConfig, run_key: str = "") -> str:
        heygen = getattr(channel, "heygen", None)
        if not heygen:
            return channel.display_name
        names = [name.strip() for name in getattr(heygen, "avatar_names", []) if str(name).strip()]
        if not names and getattr(heygen, "avatar_name", ""):
            names = [heygen.avatar_name.strip()]
        if not names:
            return channel.display_name
        if len(names) == 1:
            return names[0]
        mode = str(getattr(heygen, "avatar_rotation_mode", "sequential") or "sequential").strip().lower()
        if mode in {"sequential", "round_robin", "round-robin"}:
            rotation_path = self.config.app.state_dir / f"heygen_avatar_rotation_{channel.id}.txt"
            try:
                raw_index = int(rotation_path.read_text(encoding="utf-8").strip()) if rotation_path.exists() else 0
            except Exception:
                raw_index = 0
            selected_index = raw_index % len(names)
            rotation_path.parent.mkdir(parents=True, exist_ok=True)
            rotation_path.write_text(str((selected_index + 1) % len(names)), encoding="utf-8")
            return names[selected_index]
        seed = f"{channel.id}:{run_key}:{datetime.now().strftime('%Y%m%d%H')}"
        return random.Random(seed).choice(names)

    def _used_topics_path(self) -> Path:
        return self.config.app.state_dir / "used_topics.txt"

    def _load_used_topics(self) -> Set[str]:
        path = self._used_topics_path()
        if not path.exists():
            return set()
        return {line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}

    def _add_used_topic(self, topic_str: str) -> None:
        clean = topic_str.strip().lower()
        if not clean:
            return
        used = self._load_used_topics()
        if clean not in used:
            with open(self._used_topics_path(), "a", encoding="utf-8") as f:
                f.write(f"{clean}\n")

    def _next_backlog_item(self, channel_id: str) -> dict | None:
        for item in self._load_backlog(channel_id):
            if item.get("status") in {"pending", "failed"}:
                return item
        return None

    def _update_backlog_item(self, channel_id: str, run_dir: str, updates: dict) -> None:
        items = self._load_backlog(channel_id)
        for item in items:
            if item.get("run_dir") == run_dir:
                item.update(updates)
                break
        self._save_backlog(channel_id, items)

    def initialize_backlog(self, channel_id: str, rebuild: bool = False) -> list[dict]:
        channel = self._channel(channel_id)
        existing = self._load_backlog(channel_id)

        existing_map = {}
        if not rebuild:
            for item in existing:
                run_dir = str(item.get("run_dir") or "").strip()
                if run_dir:
                    try:
                        resolved_path = Path(run_dir).resolve()
                        existing_map[resolved_path] = item
                    except Exception:
                        existing_map[run_dir] = item

        items_map: dict[str, dict] = {}
        logged_dirs = set()
        
        # 1. Start with Log-based runs
        for run in self._read_run_log():
            if run.get("channel") != channel_id:
                continue
            run_dir = str(run.get("run_dir", ""))
            if not run_dir:
                continue
            
            logged_dirs.add(Path(run_dir).resolve())

            run_path = Path(run_dir)
            metadata_path = run_path / "metadata.json"
            metadata = read_json(metadata_path, {}) if metadata_path.exists() else {}
            content_kind = (
                str(run.get("content_kind") or "").strip()
                or str(metadata.get("content_kind") or "").strip()
                or None
            )
            video_path = self._resolve_video_path(run_path, content_kind=content_kind)
            # We still add to backlog even if video is missing if it's already posted elsewhere?
            # No, if video is missing we can't upload.
            if video_path is None:
                continue

            title = str(run.get("title") or metadata.get("title") or "").strip()
            resolved_key = run_path.resolve()
            items_map[resolved_key] = self._build_backlog_item(
                run_dir=run_dir,
                title=title,
                metadata_path=metadata_path,
                video_path=video_path,
                existing=existing_map.get(resolved_key),
                run=run,
                channel=channel,
            )

        # 2. Filesystem Discovery (for runs missing from log)
        for kind in ["short", "video"]:
            kind_dir = channel.output_dir / kind
            if not kind_dir.exists():
                continue
            for day_dir in kind_dir.iterdir():
                if not day_dir.is_dir() or day_dir.name == "test_unique_backlog":
                    continue
                for run_path in day_dir.iterdir():
                    if not run_path.is_dir():
                        continue
                    
                    resolved = run_path.resolve()
                    if resolved in logged_dirs:
                        continue
                    
                    # Discovery candidate
                    video_path = self._resolve_video_path(run_path, content_kind=kind)
                    if not video_path:
                        continue
                    
                    topic_path = run_path / "topic.json"
                    metadata_path = run_path / "metadata.json"
                    
                    title = ""
                    subject = ""
                    if topic_path.exists():
                        t_data = read_json(topic_path, {})
                        title = t_data.get("title", "")
                        subject = t_data.get("subject", "")
                    
                    if not title and metadata_path.exists():
                        m_data = read_json(metadata_path, {})
                        title = m_data.get("title", "")
                        subject = m_data.get("subject", "")

                    if not title:
                        title = f"Discovered Run {run_path.name}"
                    
                    # Only permanently burn topics for uploaded or quality-passed
                    # ready builds discovered on disk. Failed/held runs are tracked
                    # via failed_topics.jsonl instead of used_topics.txt.
                    run_meta = read_json(metadata_path, {}) if metadata_path.exists() else {}
                    quality_decision = str(
                        run_meta.get("quality_decision")
                        or (existing_map.get(run_path.resolve()) or {}).get("quality_decision")
                        or ""
                    ).strip().lower()
                    uploaded_flag = bool(
                        run_meta.get("youtube_id")
                        or run_meta.get("facebook_id")
                        or (existing_map.get(run_path.resolve()) or {}).get("youtube_id")
                        or (existing_map.get(run_path.resolve()) or {}).get("facebook_id")
                    )
                    if uploaded_flag or quality_decision == "pass":
                        if subject:
                            self._add_used_topic(subject)
                        if title:
                            self._add_used_topic(title)

                    resolved_key = run_path.resolve()
                    run_dir_str = str(run_path)
                    items_map[resolved_key] = self._build_backlog_item(
                        run_dir=run_dir_str,
                        title=title,
                        metadata_path=metadata_path,
                        video_path=video_path,
                        existing=existing_map.get(resolved_key),
                        channel=channel,
                    )

        # 3. Cleanup existing map entries that were NOT in either log or filesystem
        # (This ensures the backlog stays in sync with what's actually on disk)
        final_items = list(items_map.values())
        
        def _sort_key(item: dict) -> str:
            # Extract timestamp from run_dir (e.g., 20260323_201126)
            rd = str(item.get("run_dir", ""))
            match = re.search(r"(\d{8}_\d{6})", rd)
            if match:
                return match.group(1)
            return rd

        final_items.sort(key=_sort_key, reverse=True) # Newest first (by timestamp)
        self._save_backlog(channel_id, final_items)
        return final_items

    def _perform_upload(self, channel: ChannelConfig, video_path: Path, metadata: dict, thumbnail_path: Path | None = None, targets: list[str] | None = None) -> dict:
        results = {}
        if targets is None:
            targets = ["youtube", "facebook"]
            
        # YouTube
        if "youtube" in targets and channel.youtube and channel.youtube.upload_enabled:
            try:
                results["youtube_id"] = self.youtube.upload(
                    channel=channel,
                    video_path=video_path,
                    metadata=metadata,
                    privacy_status=channel.youtube.privacy_status,
                    thumbnail_path=thumbnail_path,
                    is_short=(metadata.get("content_kind") == "short")
                )
            except UploadLimitExceededError as exc:
                self._set_upload_block(channel.id, "youtube_upload_limit", str(exc), hours=24)
                self.logger.error(channel.id, "YouTube daily upload limit reached; pausing uploads for 24h", exc)
                results["youtube_error"] = str(exc)
            except UploadAuthError as exc:
                self.logger.error(channel.id, "YouTube authorization failed", exc)
                results["youtube_error"] = str(exc)
            except Exception as exc:
                self.logger.error(channel.id, f"YouTube upload failed", exc)
                results["youtube_error"] = str(exc)

        # Facebook Upload
        if "facebook" in targets and channel.facebook and channel.facebook.upload_enabled:
            try:
                # Direct upload via FacebookUploader
                results["facebook_id"] = self.facebook.upload(
                    channel=channel,
                    video_path=video_path,
                    metadata=metadata
                )
            except Exception as exc:
                self.logger.error(channel.id, f"Facebook upload failed", exc)
                results["facebook_error"] = str(exc)
        
        return results

    @staticmethod
    def _is_retryable_short_timing_error(exc: BaseException) -> bool:
        if isinstance(exc, (ShortCaptionPreflightError,)):
            return True
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "unsafe voice-speed correction",
                "outside required",
                "short duration",
                "duration outside",
                "atempo=",
                "short caption preflight",
                "caption preflight failed",
            )
        )

    def _retry_short_after_timing_failure(
        self,
        *,
        channel_id: str,
        channel: ChannelConfig,
        topic: TopicCandidate,
        run_dir: Path | None,
        exc: BaseException,
        upload: bool,
        force_public: bool,
        content_kind: str,
        _quality_retry: bool,
        _timing_retry: int,
    ) -> BuildArtifacts:
        max_retries = max(1, int(self.SHORT_TIMING_RETRY_ROUNDS) * 2)
        if _timing_retry >= max_retries or not self._is_retryable_short_timing_error(exc):
            raise exc
        self.logger.warning(
            channel.id,
            f"Short timing/captions failed ({exc}); "
            f"retry {_timing_retry + 1}/{max_retries} with a new candidate.",
        )
        try:
            self._requeue_failed_topic(
                channel.id,
                topic,
                reason=str(exc),
                status="short_timing_retry",
                content_kind=content_kind,
            )
        except Exception:
            pass
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
        return self.build_one(
            channel_id=channel_id,
            upload=upload,
            force_public=force_public,
            content_kind=content_kind,
            _quality_retry=_quality_retry,
            _timing_retry=_timing_retry + 1,
        )

    def build_one(
        self,
        channel_id: str,
        upload: bool = False,
        force_public: bool = False,
        content_kind: str = "short",
        _quality_retry: bool = False,
        _timing_retry: int = 0,
    ) -> BuildArtifacts:
        upload_requested = bool(upload)
        self.script_writer.reset_provider_audit()
        channel = self._channel(channel_id)
        profile = self._content_profile(channel, content_kind)
        resolve_h264_encode_profile()
        self.logger.info(channel_id, selected_encoder_note() or "video encoder: auto")

        # Load Viral DNA if available
        from yt_auto.channel_analyzer import ChannelAnalyzer
        dna = ChannelAnalyzer.load(self.config.app.state_dir, channel_id)
        if dna:
            self.logger.info(channel_id, f"Viral DNA loaded (analyzed: {dna.get('analyzed_at', 'unknown')})")

        # Planning
        avoid_titles = self._recent_titles(channel.id)
        published_avoid_titles = set(avoid_titles)
        style_bias = self.feedback.style_bias(channel.id, channel.styles) if self.config.app.feedback_enabled else None
        term_bias = self.feedback.term_bias(channel.id) if self.config.app.feedback_enabled else None
        topic: Optional[TopicCandidate] = None
        ranked_variants = []
        topic_score_review: dict = {}
        scored_candidates: list[tuple[float, dict, TopicCandidate, list[TitleVariant]]] = []
        candidate_pool_size = max(1, int(getattr(self.config.app, "candidate_pool_size", 4) or 4))
        if channel.id == "brain_lens" and content_kind == "short":
            candidate_pool_size = max(candidate_pool_size, 6)
        if channel.id == "ancient_history" and content_kind == "short":
            candidate_pool_size = self.ANCIENT_SHORT_VISUAL_CANDIDATES
            # During continuity recovery, prefer shipping one solid pack quickly
            # over collecting six weak research candidates while the gap grows.
            if str(os.getenv("YT_CONTINUITY_RECOVERY", "")).lower() in {"1", "true", "yes", "on"}:
                candidate_pool_size = min(candidate_pool_size, 2)
        if channel.id == "ancient_history" and content_kind == "video":
            candidate_pool_size = max(candidate_pool_size, 5)
        max_topic_attempts = 24 if channel.id == "brain_lens" and content_kind == "short" else 15
        if channel.id == "ancient_history" and content_kind == "short":
            max_topic_attempts = 20
        all_continuity_fallbacks = (
            self._ancient_short_continuity_fallbacks(channel)
            if str(os.getenv("YT_CONTINUITY_RECOVERY", "")).lower()
            in {"1", "true", "yes", "on"}
            and content_kind == "short"
            else []
        )
        # Drop packs whose exact title was already published so recovery does not
        # burn attempts on known repeats before reaching fresh subjects.
        published_title_keys = {
            str(item or "").strip().lower()
            for item in published_avoid_titles
            if str(item or "").strip()
        }
        # Drop by subject as well as by title: recovery rewrites the title for
        # the same curated facts, so an exact-title check let the same subject
        # through again and again under a new name.
        recent_subjects = self._recently_published_subjects(channel.id)
        all_continuity_fallbacks = [
            item
            for item in all_continuity_fallbacks
            if str(item.title or "").strip().lower() not in published_title_keys
            and self._normalise_subject(getattr(item, "subject", "")) not in recent_subjects
        ]
        continuity_fallbacks = list(all_continuity_fallbacks)
        rejected_title_counts: dict[str, int] = {}
        consecutive_plan_failures = 0

        for attempt_index in range(max_topic_attempts):
            self.logger.info(channel.id, f"Planning candidate {attempt_index + 1}/{max_topic_attempts}")
            try:
                # After a few AI failures, prefer curated continuity packs so the
                # upload gap does not wait for all 20 weak research attempts.
                if (
                    channel.id == "ancient_history"
                    and content_kind == "short"
                    and consecutive_plan_failures >= 3
                    and continuity_fallbacks
                ):
                    candidate = continuity_fallbacks.pop(0)
                    self.logger.warning(
                        channel.id,
                        "Switching to curated continuity fallback after repeated AI topic failures.",
                    )
                else:
                    candidate = self.topic_planner.plan(
                        channel,
                        style_bias=style_bias,
                        term_bias=term_bias,
                        avoid_titles=avoid_titles,
                        content_kind=content_kind,
                        dna=dna,
                    )
            except SourceSafeResearchExhaustedError as exc:
                self.logger.warning(
                    channel.id,
                    f"Research exhausted for planning candidate {attempt_index + 1}: {exc}",
                )
                if continuity_fallbacks:
                    candidate = continuity_fallbacks.pop(0)
                    self.logger.warning(
                        channel.id,
                        "Using a curated continuity fallback angle after source-safe research exhaustion.",
                    )
                else:
                    continue
            if not candidate.source_urls:
                context_source = self.topic_planner.research._default_source_url(
                    candidate.subject or candidate.title
                )
                if context_source:
                    candidate = replace(candidate, source_urls=[context_source])
            try:
                continuity_fallback_title = (
                    candidate.title
                    if candidate.selected_title_pattern == "continuity_evergreen_fallback"
                    else ""
                )
                if candidate.selected_title_pattern == "continuity_evergreen_fallback":
                    # Pad/polish curated beats before quality gates. Skipping this
                    # left new packs under the 120-word Short floor and stalled recovery.
                    validation_avoids = {
                        item
                        for item in avoid_titles
                        if item not in {
                            str(candidate.subject or "").lower().strip(),
                            str(continuity_fallback_title).lower().strip(),
                        }
                    }
                    candidate = self._prepare_ancient_continuity_fallback(
                        channel,
                        candidate,
                        validation_avoids,
                    )
                    variants = [
                        TitleVariant(
                            pattern_id="continuity_evergreen_fallback",
                            title=candidate.title,
                            predicted_score=86.0,
                        )
                    ]
                elif candidate.selected_title_pattern == "deterministic_brain_fallback":
                    # These complete, QA'd fallback beats are intentionally
                    # kept verbatim. Rewriting them during an AI outage was
                    # producing broken caption fragments and weaker hooks.
                    candidate = replace(candidate)
                    candidate, variants = self._rank_and_apply_title(channel, candidate)
                else:
                    candidate = self.script_writer.improve(
                        channel=channel,
                        topic=candidate,
                        content_kind=content_kind,
                        avoid_titles=avoid_titles,
                        dna=dna,
                    )
                    candidate = replace(
                        candidate,
                        source_urls=self.topic_planner.research.enrich_source_urls(
                            candidate.subject or candidate.title,
                            list(candidate.source_urls),
                            limit=6,
                        ),
                    )
                    candidate, variants = self._rank_and_apply_title(channel, candidate)
                if continuity_fallback_title and candidate.selected_title_pattern != "continuity_evergreen_fallback":
                    # TitleLab is allowed to learn from published winners, but
                    # it must not select an already-published title while this
                    # recovery angle is being used.
                    candidate = replace(
                        candidate,
                        title=continuity_fallback_title,
                        title_variants=[continuity_fallback_title],
                        selected_title_pattern="continuity_evergreen_fallback",
                    )
                if candidate.selected_title_pattern != "continuity_evergreen_fallback":
                    validation_avoids = avoid_titles
                    if continuity_fallback_title:
                        # Reusing a vetted subject with a materially new angle is
                        # preferable to an unsourced gap. Do not let the exact
                        # subject label itself reject the fresh title; the
                        # fingerprint and script/visual gates still run normally.
                        validation_avoids = {
                            item
                            for item in avoid_titles
                            if item not in {
                                str(candidate.subject or "").lower().strip(),
                                str(continuity_fallback_title).lower().strip(),
                            }
                        }
                    self._validate_topic_quality(channel, candidate, validation_avoids)
                ai_score = (
                    {}
                    if channel.id == "ancient_history" and content_kind == "short"
                    else self.script_writer.score_topic_candidate(channel, candidate, avoid_titles)
                )
                score = float(ai_score.get("score") or candidate.engagement_score or 0)
                scored_candidates.append((score, ai_score, candidate, variants))
                avoid_titles.add(candidate.title.lower())
                if candidate.subject:
                    avoid_titles.add(candidate.subject.lower())
                consecutive_plan_failures = 0
                if len(scored_candidates) >= candidate_pool_size:
                    break
            except ValueError as exc:
                consecutive_plan_failures += 1
                repaired = None
                title_key = str(getattr(candidate, "title", "") or "").strip().lower()
                if title_key:
                    rejected_title_counts[title_key] = rejected_title_counts.get(title_key, 0) + 1
                # Same weak title was burning 15-20 attempts on AI repair.
                # Continuity packs are already curated — never send them to AI repair.
                skip_repair = bool(
                    title_key and rejected_title_counts.get(title_key, 0) >= 2
                ) or str(getattr(candidate, "selected_title_pattern", "") or "") in {
                    "continuity_evergreen_fallback",
                    "deterministic_brain_fallback",
                }
                if (
                    not skip_repair
                    and channel.id in {"brain_lens", "ancient_history"}
                    and content_kind == "short"
                    and self.script_writer._ai_enabled()
                ):
                    try:
                        self.logger.info(
                            channel.id,
                            "Attempting AI provider repair after topic rejection "
                            f"(before weak fallbacks): {exc}",
                        )
                        if channel.id == "brain_lens":
                            repaired = self.script_writer.repair_brain_short_with_ai(
                                channel,
                                candidate,
                                avoid_titles=avoid_titles,
                                dna=dna,
                            )
                        else:
                            repaired = self.script_writer.repair_ancient_short_with_ai(
                                channel,
                                candidate,
                                avoid_titles=avoid_titles,
                                dna=dna,
                            )
                        if repaired is not None:
                            repaired, variants = self._rank_and_apply_title(channel, repaired)
                            self._validate_topic_quality(channel, repaired, published_avoid_titles)
                            ai_score = (
                                {}
                                if channel.id == "ancient_history"
                                else self.script_writer.score_topic_candidate(
                                    channel, repaired, avoid_titles
                                )
                            )
                            score = float(
                                ai_score.get("score") or repaired.engagement_score or 0
                            )
                            scored_candidates.append((score, ai_score, repaired, variants))
                            avoid_titles.add(repaired.title.lower())
                            if repaired.subject:
                                avoid_titles.add(repaired.subject.lower())
                            self.logger.info(
                                channel.id,
                                "AI provider repair accepted "
                                f"(provider={repaired.script_provider or 'unknown'})",
                            )
                            if len(scored_candidates) >= candidate_pool_size:
                                break
                            continue
                    except Exception as repair_exc:
                        self.logger.warning(
                            channel.id,
                            f"AI provider repair failed: {repair_exc}",
                        )
                        repaired = None
                elif skip_repair:
                    self.logger.warning(
                        channel.id,
                        f"Skipping repeated AI repair for stuck title: {candidate.title}",
                    )
                self.logger.warning(channel.id, f"Topic rejected: {exc}")
                avoid_titles.add(candidate.title.lower())
                if candidate.subject:
                    avoid_titles.add(candidate.subject.lower())
                candidate_fingerprint = self._story_fingerprint(
                    candidate.title,
                    candidate.subject,
                    " ".join(candidate.trend_terms[:4]),
                )
                if candidate_fingerprint:
                    avoid_titles.add(candidate_fingerprint.lower())
                # Hard-ban Machu Picchu water-terrace loop that kept regenerating.
                if "machu" in title_key or "terrace farming" in title_key:
                    avoid_titles.update(
                        {
                            "machu picchu",
                            "terrace farming at machu picchu",
                            "how machu picchu's terraces controlled water and erosion",
                        }
                    )

        if (
            channel.id == "ancient_history"
            and content_kind == "short"
            and all_continuity_fallbacks
            and len(scored_candidates) < candidate_pool_size
        ):
            existing_titles = {
                str(item[2].title or "").strip().lower()
                for item in scored_candidates
                if len(item) >= 3
            }
            for fallback in all_continuity_fallbacks:
                if len(scored_candidates) >= candidate_pool_size:
                    break
                if fallback.title.strip().lower() in existing_titles:
                    continue
                validation_avoids = {
                    item
                    for item in published_avoid_titles
                    if item not in {
                        str(fallback.subject or "").lower().strip(),
                        str(fallback.title or "").lower().strip(),
                    }
                }
                try:
                    fallback = self._prepare_ancient_continuity_fallback(
                        channel,
                        fallback,
                        validation_avoids,
                    )
                except ValueError as exc:
                    self.logger.warning(
                        channel.id,
                        f"Continuity fallback rejected: {exc}",
                    )
                    continue
                variants = [
                    TitleVariant(
                        pattern_id="continuity_evergreen_fallback",
                        title=fallback.title,
                        predicted_score=86.0,
                    )
                ]
                scored_candidates.append(
                    (
                        86.0,
                        {
                            "score": 86,
                            "decision": "source-backed continuity fallback",
                            "strengths": [
                                "curated factual spine",
                                "archive-rich subject",
                                "fresh evidence angle",
                            ],
                        },
                        fallback,
                        variants,
                    )
                )
                existing_titles.add(fallback.title.strip().lower())

        if not scored_candidates and channel.id == "brain_lens" and content_kind == "short":
            # Last resort only. Prefer empty-pool failure / skip over shipping
            # weak templates when YT_ALLOW_DETERMINISTIC_BRAIN_FALLBACK=0.
            # Default off: shipping template psychology shorts trains bounce.
            # Set YT_ALLOW_DETERMINISTIC_BRAIN_FALLBACK=1 only as emergency.
            allow_deterministic = str(
                os.getenv("YT_ALLOW_DETERMINISTIC_BRAIN_FALLBACK", "0")
            ).lower() in {"1", "true", "yes", "on"}
            if not allow_deterministic:
                raise RuntimeError(
                    "Brain Lens Short: AI drafts exhausted and deterministic "
                    "template fallback is disabled (set "
                    "YT_ALLOW_DETERMINISTIC_BRAIN_FALLBACK=1 only as emergency)."
                )
            self.logger.warning(
                channel.id,
                "AI topic drafts and provider repairs exhausted; using ONE "
                "validated deterministic Brain Lens Short fallback as last resort "
                "(not preferred for production quality)",
            )
            fallback_avoids = set(published_avoid_titles)
            for _ in range(32):
                fallback = self._brain_short_deterministic_fallback(channel, fallback_avoids)
                if fallback is None:
                    break
                try:
                    self._validate_topic_quality(channel, fallback, published_avoid_titles)
                    variants = [
                        TitleVariant(
                            pattern_id="deterministic_brain_fallback",
                            title=fallback.title,
                            predicted_score=88.0,
                        )
                    ]
                    scored_candidates.append(
                        (
                            88.0,
                            {
                                "score": 88,
                                "decision": "deterministic editorial fallback (last resort)",
                                "strengths": [
                                    "behavior-first hook",
                                    "midpoint contrast",
                                    "practical payoff",
                                ],
                            },
                            fallback,
                            variants,
                        )
                    )
                    # One template max — do not fill the whole visual pool.
                    break
                except ValueError as exc:
                    self.logger.warning(channel.id, f"Deterministic Brain fallback rejected: {exc}")
                    fallback_avoids.add(fallback.subject.lower())
                    fallback_avoids.add(fallback.title.lower())

        if not scored_candidates:
            raise RuntimeError("Failed to generate a high-quality topic after multiple attempts.")

        scored_candidates.sort(key=lambda item: item[0], reverse=True)

        logo_path = self._channel_logo_path(channel)
        run_dir: Path | None = None
        images: list[Path] = []
        sources: list[dict] = []

        # Assets — pass visual_style to image fetcher
        asset_candidates = self._visual_asset_candidates(
            channel.id,
            content_kind,
            scored_candidates,
        )
        last_visual_issue = ""
        for asset_index, (selected_score, review, candidate, variants) in enumerate(asset_candidates):
            candidate_run_dir = self._run_dir(channel, content_kind)
            image_count = (
                min(24, max(16, len(candidate.scene_plan or [])))
                if content_kind == "video"
                else 12
            )
            candidate_images, candidate_sources, fetch_issue = self._fetch_candidate_visuals(
                topic=candidate,
                run_dir=candidate_run_dir,
                count=image_count,
                visual_style=channel.visual_style,
            )
            if fetch_issue:
                last_visual_issue = fetch_issue
                self.logger.warning(
                    channel.id,
                    f"Visual fetch rejected candidate {asset_index + 1}/{len(asset_candidates)}: {fetch_issue}",
                )
                shutil.rmtree(candidate_run_dir, ignore_errors=True)
                continue
            visual_issue = None
            if channel.id == "ancient_history":
                visual_issue = self._ancient_visual_preflight_issue(
                    candidate,
                    candidate_sources,
                    content_kind,
                    source_visual_qa=read_json(
                        candidate_run_dir / "frame_check" / "source_visual_qa.json",
                        {},
                    ),
                )
            elif channel.id == "brain_lens":
                visual_issue = self._brain_visual_preflight_issue(candidate, candidate_sources)
            if visual_issue:
                last_visual_issue = visual_issue
                self.logger.warning(
                    channel.id,
                    f"Visual preflight rejected candidate {asset_index + 1}/{len(asset_candidates)}: {visual_issue}",
                )
                shutil.rmtree(candidate_run_dir, ignore_errors=True)
                continue

            topic = candidate
            topic_score_review = review
            ranked_variants = variants
            run_dir = candidate_run_dir
            images = candidate_images
            sources = candidate_sources
            self.logger.info(
                channel.id,
                f"Selected candidate {asset_index + 1}/{len(asset_candidates)} after visual preflight "
                f"(creative_score={round(selected_score, 1)})",
            )
            break

        if not topic or run_dir is None:
            raise RuntimeError(
                f"Failed {channel.display_name} visual preflight for every researched candidate"
                + (f": {last_visual_issue}" if last_visual_issue else ".")
            )

        
        thumbnail_path = None
        thumbnail_quality_issues: list[str] = []
        if self.config.app.thumbnail.enabled:
            thumbnail_path = run_dir / "thumbnail.jpg"
            thumb_maker = (
                ThumbnailMaker(width=1280, height=720)
                if content_kind == "video"
                else ThumbnailMaker(width=1080, height=1920)
            )
            thumb_maker.generate(
                image_path=self._thumbnail_lead_asset(channel, content_kind, images, sources),
                title=topic.title,
                out_path=thumbnail_path,
                channel_id=channel.id,
                logo_path=logo_path,
            )
            thumbnail_quality_issues = thumb_maker.thumbnail_copy_issues(
                title=topic.title,
                channel_id=channel.id,
            )

        narration_path = run_dir / "narration.wav"
        video_path = run_dir / ("short.mp4" if content_kind == "short" else "video.mp4")
        selected_heygen_avatar = ""
        selected_presenter_avatar: Path | None = None
        presenter_requested = self._should_use_presenter(channel, content_kind)
        if presenter_requested:
            selected_presenter_avatar = self._select_presenter_avatar(channel, run_dir.name)
            if selected_presenter_avatar:
                self.logger.info(channel.id, f"Using local presenter avatar: {selected_presenter_avatar.name}")
            else:
                self.logger.warning(channel.id, "Local presenter enabled but no avatar image was found; building with B-roll only.")

        def build_with_local_tts() -> tuple[str, list[float], float]:
            tts_channel = channel
            recovery_short = (
                channel.id == "ancient_history"
                and content_kind == "short"
                and str(os.getenv("YT_CONTINUITY_RECOVERY", "")).lower()
                in {"1", "true", "yes", "on"}
            )
            recovery_speed = 1.40
            if recovery_short:
                # Ancient History's documentary voice is intentionally slow,
                # but a continuity Short must still fit the configured timing
                # contract. Start with a faster Kokoro profile, then adapt it
                # once from measured audio if a compact script is too fast.
                recovery_voices = [
                    (
                        f"{str(voice).split('@', 1)[0]}@{recovery_speed:.2f}"
                        if str(voice).lower().startswith("kokoro-")
                        else str(voice)
                    )
                    for voice in channel.voices
                ]
                tts_channel = replace(channel, voices=recovery_voices)

            beats = topic.narration_beats or [topic.narration]
            narration_word_count = len(re.findall(r"[A-Za-z0-9']+", topic.narration or ""))
            normalization_maximum = max(
                float(profile.min_duration_seconds),
                float(profile.max_duration_seconds) - 0.1,
            )
            normalization_minimum = float(profile.min_duration_seconds)
            normalization_target = self._short_narration_target_seconds(
                narration_word_count,
                maximum_seconds=normalization_maximum,
                minimum_seconds=normalization_minimum,
            )

            def synthesize_and_normalize(
                selected_channel: ChannelConfig,
                segment_dir: Path,
            ) -> tuple[str, list[float]]:
                voice_engine = NarrationEngine(
                    selected_channel.voices,
                    selected_channel.voice_mode,
                    getattr(selected_channel, "tts_backend", ""),
                )
                voice_id, durations = voice_engine.synthesize_beats(
                    beats=beats,
                    out_dir=segment_dir,
                    out_path=narration_path,
                )
                if content_kind == "short" and channel.id in {"brain_lens", "ancient_history"}:
                    durations, _ = self._normalize_short_narration_duration(
                        channel.id,
                        narration_path,
                        durations,
                        target_seconds=normalization_maximum,
                        acceptable_bounds=(normalization_minimum, float(profile.max_duration_seconds)),
                        narration_word_count=narration_word_count,
                        narration_beats=beats,
                    )
                return voice_id, durations

            try:
                local_voice, local_durations = synthesize_and_normalize(
                    tts_channel,
                    run_dir / "narration_segments",
                )
            except RuntimeError as exc:
                if not recovery_short or "unsafe voice-speed correction" not in str(exc):
                    raise
                measured_duration = self._probe_audio_duration(narration_path)
                adaptive_speed = self._adaptive_recovery_voice_speed(
                    measured_duration,
                    base_speed=recovery_speed,
                    target_seconds=normalization_target,
                )
                if abs(adaptive_speed - recovery_speed) < 0.01:
                    raise
                adaptive_voices = [
                    (
                        f"{str(voice).split('@', 1)[0]}@{adaptive_speed:.2f}"
                        if str(voice).lower().startswith("kokoro-")
                        else str(voice)
                    )
                    for voice in channel.voices
                ]
                self.logger.warning(
                    channel.id,
                    f"Recovery voice timing measured {measured_duration:.2f}s; "
                    f"retrying Kokoro at {adaptive_speed:.2f}x.",
                )
                local_voice, local_durations = synthesize_and_normalize(
                    replace(channel, voices=adaptive_voices),
                    run_dir / "narration_segments_adaptive",
                )
            try:
                local_duration = self.video_builder.build(
                    image_paths=images,
                    narration_path=narration_path,
                    music_dir=self._channel_music_dir(channel),
                    out_path=video_path,
                    narration_text=topic.narration,
                    subtitles_path=run_dir / "subtitles.srt",
                    title_text=topic.title,
                    visual_captions=topic.visual_captions,
                    narration_beats=topic.narration_beats or [topic.narration],
                    scene_durations=local_durations,
                    target_size=profile.target_size,
                    duration_bounds=(profile.min_duration_seconds, profile.max_duration_seconds),
                    logo_path=logo_path,
                    content_kind=content_kind,
                    channel_id=channel.id,
                )
            except (ShortCaptionPreflightError, LongCaptionPreflightError) as exc:
                self._requeue_failed_topic(
                    channel.id,
                    topic,
                    reason=str(exc),
                    status="caption_preflight_failed",
                    content_kind=content_kind,
                )
                raise
            except Exception as exc:
                self._requeue_failed_topic(
                    channel.id,
                    topic,
                    reason=str(exc),
                    status="render_failed",
                    content_kind=content_kind,
                )
                raise
            return local_voice, local_durations, local_duration

        try:
            if self._should_use_heygen(channel, content_kind):
                selected_heygen_avatar = self._select_heygen_avatar(channel, run_dir.name)
                (run_dir / "heygen_script.txt").write_text(topic.narration, encoding="utf-8")
                heygen_service = HeyGenBrowserService(self._heygen_config_dict(channel), str(run_dir))
                avatar_attempts = [selected_heygen_avatar]
                if ">" in selected_heygen_avatar:
                    avatar_group = selected_heygen_avatar.split(">", 1)[0].strip()
                    if avatar_group and avatar_group not in avatar_attempts:
                        avatar_attempts.append(avatar_group)
                last_heygen_error: Exception | None = None
                rendered_name = ""
                for avatar_attempt in avatar_attempts:
                    try:
                        rendered_name = heygen_service.render_script(
                            script_text=topic.narration,
                            cut_key=content_kind,
                            label=f"{channel.display_name} {content_kind}",
                            talent_key=channel.id,
                            avatar_name=avatar_attempt,
                            task_id=run_dir.name,
                        )
                        selected_heygen_avatar = avatar_attempt
                        break
                    except Exception as exc:
                        last_heygen_error = exc
                        self.logger.warning(
                            channel.id,
                            f"HeyGen attempt failed for {avatar_attempt}; "
                            f"{'falling back to local TTS' if avatar_attempt == avatar_attempts[-1] else 'retrying avatar group'} ({exc})",
                        )
                if not rendered_name and last_heygen_error:
                    selected_heygen_avatar = ""
                    voice, durations, duration = build_with_local_tts()
                else:
                    rendered_path = run_dir / rendered_name
                    if not rendered_path.exists():
                        rendered_path = run_dir / "heygen_downloads" / rendered_name
                    if not rendered_path.exists():
                        self.logger.warning(channel.id, f"HeyGen output missing ({rendered_name}); rebuilding with local TTS fallback.")
                        selected_heygen_avatar = ""
                        voice, durations, duration = build_with_local_tts()
                    else:
                        heygen_audio_path = run_dir / "heygen_voice_source.mp4"
                        if rendered_path.resolve() != heygen_audio_path.resolve():
                            shutil.copy2(rendered_path, heygen_audio_path)
                        duration = self.video_builder.build(
                            image_paths=images,
                            narration_path=heygen_audio_path,
                            music_dir=self._channel_music_dir(channel),
                            out_path=video_path,
                            narration_text=topic.narration,
                            subtitles_path=run_dir / "subtitles.srt",
                            title_text=topic.title,
                            visual_captions=topic.visual_captions,
                            narration_beats=topic.narration_beats or [topic.narration],
                            scene_durations=[],
                            target_size=profile.target_size,
                            duration_bounds=(profile.min_duration_seconds, profile.max_duration_seconds),
                            logo_path=logo_path,
                            content_kind=content_kind,
                            channel_id=channel.id,
                        )
                        self._overlay_heygen_presenter(video_path, rendered_path, content_kind)
                        voice = "heygen_saved_character_voice"
                        durations = []
                        duration = self._probe_video_duration(video_path)
                        if content_kind == "short" and duration < max(18, profile.min_duration_seconds - 4):
                            self.logger.warning(
                                channel.id,
                                f"HeyGen render too short ({round(duration, 1)}s); rebuilding with local TTS fallback.",
                            )
                            selected_heygen_avatar = ""
                            voice, durations, duration = build_with_local_tts()
            else:
                voice, durations, duration = build_with_local_tts()

            if content_kind == "short":
                min_seconds = float(profile.min_duration_seconds)
                max_seconds = float(profile.max_duration_seconds)
                if not (min_seconds <= float(duration) <= max_seconds):
                    raise RuntimeError(
                        f"Short duration {float(duration):.2f}s outside required "
                        f"{min_seconds:.0f}-{max_seconds:.0f}s window"
                    )
        except Exception as render_exc:
            if content_kind == "short" and self._is_retryable_short_timing_error(render_exc):
                return self._retry_short_after_timing_failure(
                    channel_id=channel_id,
                    channel=channel,
                    topic=topic,
                    run_dir=run_dir,
                    exc=render_exc,
                    upload=upload_requested,
                    force_public=force_public,
                    content_kind=content_kind,
                    _quality_retry=_quality_retry,
                    _timing_retry=_timing_retry,
                )
            raise

        local_presenter_applied = False
        presenter_clip: Path | None = None
        presenter_status = "not_requested"
        presenter_engine = str(getattr(channel.presenter, "engine", "") or "").lower()
        if selected_presenter_avatar and video_path.exists():
            presenter_status = "requested"
            if presenter_engine in {"sadtalker_hybrid", "audio_reactive_portrait", "rhubarb_phoneme_portrait"}:
                presenter_clip = LocalPresenterService(channel.presenter, self.logger).render_hook(
                    selected_presenter_avatar,
                    narration_path,
                    run_dir,
                    (topic.narration_beats or [topic.narration])[0],
                )
                if presenter_clip:
                    local_presenter_applied = self._overlay_lipsynced_presenter(video_path, presenter_clip, profile.target_size)
                    if local_presenter_applied:
                        if presenter_engine == "rhubarb_phoneme_portrait":
                            presenter_status = "phoneme_synced"
                        elif presenter_engine == "audio_reactive_portrait":
                            presenter_status = "audio_synced"
                        else:
                            presenter_status = "lipsynced"
                    else:
                        presenter_status = "overlay_failed"
                else:
                    presenter_status = "render_failed"
            else:
                local_presenter_applied = self._overlay_local_presenter(
                    video_path,
                    selected_presenter_avatar,
                    content_kind,
                    profile.target_size,
                )
                presenter_status = "static_fallback" if local_presenter_applied else "overlay_failed"
            duration = self._probe_video_duration(video_path) or duration
            if content_kind == "short":
                min_seconds = float(profile.min_duration_seconds)
                max_seconds = float(profile.max_duration_seconds)
                if not (min_seconds <= float(duration) <= max_seconds):
                    return self._retry_short_after_timing_failure(
                        channel_id=channel_id,
                        channel=channel,
                        topic=topic,
                        run_dir=run_dir,
                        exc=RuntimeError(
                            f"Short duration {float(duration):.2f}s outside required "
                            f"{min_seconds:.0f}-{max_seconds:.0f}s window"
                        ),
                        upload=upload_requested,
                        force_public=force_public,
                        content_kind=content_kind,
                        _quality_retry=_quality_retry,
                        _timing_retry=_timing_retry,
                    )

        # Metadata
        metadata = self._metadata(channel, topic, ranked_variants, content_kind)
        metadata.update({
            "voice": voice,
            "duration_seconds": round(duration, 2),
            "upload_eligible": upload_requested,
            "thumbnail_file": thumbnail_path.name if thumbnail_path else None,
            "topic_score_review": topic_score_review,
            "logo_file": str(logo_path),
            "music_file": str(self.video_builder.last_music_path or ""),
            "video_encoder": str(getattr(self.video_builder, "last_video_encoder", "") or resolve_h264_encode_profile().name),
            "visual_asset_count": len(images),
            "thumbnail_quality_issues": thumbnail_quality_issues,
            "claim_card_text": str(getattr(self.video_builder, "last_claim_card_text", "") or ""),
        })
        if selected_heygen_avatar:
            metadata["heygen_avatar"] = selected_heygen_avatar
        if presenter_requested:
            metadata["presenter_requested"] = True
            metadata["presenter_status"] = presenter_status
            metadata["presenter_engine"] = presenter_engine
            if selected_presenter_avatar:
                metadata["presenter_avatar"] = str(selected_presenter_avatar)
        if selected_presenter_avatar and local_presenter_applied:
            metadata["presenter_motion"] = presenter_engine in {
                "sadtalker_hybrid",
                "audio_reactive_portrait",
                "rhubarb_phoneme_portrait",
            }
            metadata["presenter_lipsync"] = presenter_engine in {
                "sadtalker_hybrid",
            }
            metadata["presenter_audio_sync"] = presenter_engine in {
                "audio_reactive_portrait",
                "rhubarb_phoneme_portrait",
            }
            if presenter_clip:
                metadata["presenter_clip"] = str(presenter_clip)
            mouth_cues_path = run_dir / "presenter_runtime" / "presenter_mouth_cues.json"
            if mouth_cues_path.exists():
                metadata["presenter_mouth_sync"] = "rhubarb_phoneme_cues"
                metadata["presenter_mouth_cues_file"] = str(mouth_cues_path)

        # Validate the actual final file after every overlay/remux step. The
        # builder validates its own output, but presenter/HeyGen overlays happen
        # later and must never be allowed to reintroduce silent or broken media.
        final_media_report = self.video_builder.validate_output(
            video_path,
            expected_duration=float(duration),
        )
        final_visual_qa = self.image_fetcher.write_final_visual_qa(
            final_video=video_path,
            out_dir=run_dir / "frame_check",
            subtitles_path=run_dir / "subtitles.srt",
            timeline_path=run_dir / "visual_timeline.json",
        )
        metadata["final_media_validation"] = final_media_report.to_dict()
        metadata["final_visual_qa"] = {
            key: value
            for key, value in final_visual_qa.items()
            if key != "samples"
        }
        self._append_video_chapters(
            metadata,
            topic,
            float(duration),
            visual_timeline=read_json(run_dir / "visual_timeline.json", []),
        )
        quality_review = self._quality_review(
            channel=channel,
            topic=topic,
            metadata=metadata,
            sources=sources,
            duration_seconds=float(duration),
            video_path=video_path,
        )
        if final_visual_qa.get("status") != "pass":
            final_visual_issues = [
                f"Final render QA: {str(issue).strip()}"
                for issue in final_visual_qa.get("issues", [])
                if str(issue).strip()
            ] or ["Final render QA did not pass"]
            quality_review["issues"] = list(dict.fromkeys([
                *quality_review.get("issues", []),
                *final_visual_issues,
            ]))
            quality_review["blocking_issues"] = list(dict.fromkeys([
                *quality_review.get("blocking_issues", []),
                *final_visual_issues,
            ]))
            quality_review["decision"] = "hold"
            visual_subscore = int(quality_review.get("subscores", {}).get("visuals", 100))
            quality_review.setdefault("subscores", {})["visuals"] = min(visual_subscore, 35)
            quality_review["score"] = min(int(quality_review.get("score", 0)), 69)
        quality_review["final_media_validation"] = final_media_report.to_dict()
        quality_review["final_visual_qa"] = {
            key: value
            for key, value in final_visual_qa.items()
            if key != "samples"
        }
        metadata.update(self.script_writer.provider_output_metadata(topic))
        metadata["ai_provider_attempts"] = list(self.script_writer.provider_attempt_history)
        metadata["ai_provider_last_call_attempts"] = list(self.script_writer.provider_attempts)
        metadata["quality_score"] = quality_review["score"]
        metadata["quality_decision"] = quality_review["decision"]
        metadata["quality_subscores"] = quality_review.get("subscores", {})
        write_json(run_dir / "metadata.json", metadata)
        pinned = str(metadata.get("pinned_comment") or "").strip()
        if pinned:
            (run_dir / "pinned_comment.txt").write_text(pinned + "\n", encoding="utf-8")
        write_json(run_dir / "topic.json", asdict(topic))
        write_json(run_dir / "quality_review.json", quality_review)

        # Upload
        up_results = {}
        quality_gate_retry_needed = False
        if upload:
            upload_block = self._current_upload_block(channel.id)
            if upload_block:
                reason = upload_block.get("reason", "upload_block")
                blocked_until = upload_block.get("blocked_until", "unknown")
                self.logger.warning(channel.id, f"Skipping upload: {reason} until {blocked_until}")
                up_results["upload_blocked"] = reason
                upload = False
            elif self._daily_upload_cap_reached(channel):
                cap = int(channel.daily_upload_cap or 0)
                self.logger.warning(channel.id, f"Daily upload cap reached ({cap}); build saved without upload.")
                up_results["upload_skipped"] = "daily_upload_cap"
                upload = False
            elif quality_review["decision"] != "pass":
                self.logger.warning(channel.id, f"Skipping upload: quality gate score {quality_review['score']} ({', '.join(quality_review['issues'])})")
                up_results["upload_skipped"] = "quality_gate"
                quality_gate_retry_needed = not _quality_retry
                upload = False

        if upload:
            # YouTube: Always direct if enabled
            youtube_id = None
            if channel.youtube and channel.youtube.upload_enabled:
                try:
                    youtube_id = self.youtube.upload(
                        channel=channel,
                        video_path=video_path,
                        metadata=metadata,
                        privacy_status="public" if force_public else channel.youtube.privacy_status,
                        thumbnail_path=thumbnail_path,
                        is_short=(content_kind == "short")
                    )
                    up_results["youtube_id"] = youtube_id
                except UploadLimitExceededError as exc:
                    self._set_upload_block(channel.id, "youtube_upload_limit", str(exc), hours=24)
                    self.logger.error(channel.id, "YouTube daily upload limit reached; pausing uploads for 24h", exc)
                    up_results["youtube_error"] = str(exc)
                except UploadAuthError as exc:
                    self.logger.error(channel.id, "YouTube authorization failed", exc)
                    up_results["youtube_error"] = str(exc)
                except Exception as exc:
                    self.logger.error(channel.id, "YouTube upload failed", exc)
                    up_results["youtube_error"] = str(exc)

            # Facebook: If backlog exists, add to backlog. Otherwise direct.
            if channel.facebook and channel.facebook.upload_enabled:
                backlog = self._load_backlog(channel.id)
                # Filter pending/failed
                pending = [it for it in backlog if it.get("status") in {"pending", "failed"}]
                
                if pending:
                    self.logger.info(channel.id, f"Facebook backlog is active ({len(pending)} items). Adding new build to backlog.")
                    backlog.append({
                        "run_dir": str(run_dir),
                        "video_path": str(video_path),
                        "metadata_path": str(run_dir / "metadata.json"),
                        "title": topic.title,
                        "status": "pending",
                        "attempts": 0,
                        "youtube_id": up_results.get("youtube_id"),
                        "facebook_id": None,
                    })
                    self._save_backlog(channel.id, backlog)
                else:
                    # Direct upload to FB
                    self.logger.info(channel.id, "Uploading new build directly to Facebook.")
                    try:
                        up_results["facebook_id"] = self.facebook.upload(channel, video_path, metadata)
                        self.logger.success(channel.id, f"Direct Facebook upload complete: {up_results['facebook_id']}")
                    except Exception as exc:
                        self.logger.error(channel.id, "Direct Facebook upload failed", exc)
                        up_results["facebook_error"] = str(exc)
                        backlog.append({
                            "run_dir": str(run_dir),
                            "video_path": str(video_path),
                            "metadata_path": str(run_dir / "metadata.json"),
                            "title": topic.title,
                            "status": "pending",
                            "attempts": 1,
                            "youtube_id": up_results.get("youtube_id"),
                            "facebook_id": None,
                            "error": str(exc),
                        })
                        self._save_backlog(channel.id, backlog)
                        self.logger.warning(channel.id, "Added failed Facebook upload to backlog for retry.")

            transient_error = up_results.get("youtube_error") or up_results.get("facebook_error")
            if transient_error:
                self._queue_transient_upload_retry(
                    channel=channel,
                    run_dir=run_dir,
                    video_path=video_path,
                    metadata_path=run_dir / "metadata.json",
                    title=topic.title,
                    error=str(transient_error),
                    up_results=up_results,
                )

        if not upload and not any(key in up_results for key in ("youtube_id", "facebook_id", "upload_skipped", "upload_blocked", "youtube_error", "facebook_error")):
            up_results["upload_skipped"] = "build_only"

        # Log & Return
        log_event = {
            "channel": channel.id,
            "run_dir": str(run_dir),
            "title": topic.title,
            "subject": topic.subject,
            "content_kind": content_kind,
            "video_path": str(video_path),
            "metadata_path": str(run_dir / "metadata.json"),
            "youtube_id": up_results.get("youtube_id"),
            "facebook_id": up_results.get("facebook_id"),
            "youtube_error": up_results.get("youtube_error"),
            "facebook_error": up_results.get("facebook_error"),
            "upload_blocked": up_results.get("upload_blocked"),
            "upload_skipped": up_results.get("upload_skipped"),
            "quality_decision": quality_review.get("decision"),
            "uploaded": bool(up_results.get("youtube_id") or up_results.get("facebook_id"))
        }
        self._log_state(log_event)
        self._mark_topic_used_if_ready(
            channel_id=channel.id,
            topic=topic,
            quality_decision=str(quality_review.get("decision") or ""),
            uploaded=bool(log_event["uploaded"]),
            upload_skipped=str(up_results.get("upload_skipped") or ""),
        )

        self.feedback.record_build({
            "channel": channel.id,
            "run_dir": str(run_dir),
            "video_id": up_results.get("youtube_id"),
            "title": topic.title,
            "style": topic.style,
            "selected_title_pattern": topic.selected_title_pattern,
            "trend_terms": topic.trend_terms[:8],
            "content_kind": content_kind,
            "created_at": now_in_tz(self.config.app.timezone).isoformat(),
        })

        if quality_gate_retry_needed:
            self.logger.warning(
                channel.id,
                f"Quality gate held this {content_kind}; starting one fresh rewritten replacement now.",
            )
            return self.build_one(
                channel_id=channel_id,
                upload=True,
                force_public=force_public,
                content_kind=content_kind,
                _quality_retry=True,
            )

        return BuildArtifacts(
            run_dir=run_dir,
            topic_path=run_dir / "topic.json",
            narration_path=narration_path,
            video_path=video_path,
            metadata_path=run_dir / "metadata.json",
            sources_path=run_dir / "sources.json",
            image_paths=images,
            thumbnail_path=thumbnail_path,
            youtube_video_id=up_results.get("youtube_id"),
            facebook_video_id=up_results.get("facebook_id")
        )

    def publish_ready_video(
        self,
        channel_id: str,
        *,
        video_path: str | Path,
        metadata_path: str | Path | None = None,
        run_dir: str | Path | None = None,
        content_kind: str = "short",
        force_public: bool = True,
        title_hint: str | None = None,
    ) -> dict:
        """Upload an already-rendered local video (Ready Queue path).

        Intentionally does not consult Facebook backlog gates — YouTube-ready
        files must be publishable even when FB backlog is non-empty.
        """
        channel = self._channel(channel_id)
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Ready video missing: {video_path}")

        run_dir_path = Path(run_dir) if run_dir else video_path.parent
        meta_path = Path(metadata_path) if metadata_path else (run_dir_path / "metadata.json")
        metadata = read_json(meta_path, {}) if meta_path.exists() else {}
        if not isinstance(metadata, dict):
            metadata = {}
        if title_hint and not metadata.get("title"):
            metadata["title"] = title_hint

        quality = read_json(run_dir_path / "quality_review.json", {})
        decision = str(
            (quality.get("decision") if isinstance(quality, dict) else "")
            or metadata.get("quality_decision")
            or "pass"
        ).strip().lower()
        if decision and decision != "pass":
            event = {
                "channel": channel.id,
                "run_dir": str(run_dir_path),
                "title": metadata.get("title") or title_hint or video_path.stem,
                "content_kind": content_kind,
                "video_path": str(video_path),
                "metadata_path": str(meta_path),
                "upload_skipped": "quality_gate",
                "quality_decision": decision,
                "uploaded": False,
                "source": "ready_queue",
            }
            self._log_state(event)
            return {"ok": False, "upload_skipped": "quality_gate", "event": event}

        upload_block = self._current_upload_block(channel.id)
        if upload_block:
            reason = upload_block.get("reason", "upload_block")
            event = {
                "channel": channel.id,
                "run_dir": str(run_dir_path),
                "title": metadata.get("title") or title_hint or video_path.stem,
                "content_kind": content_kind,
                "video_path": str(video_path),
                "upload_blocked": reason,
                "uploaded": False,
                "source": "ready_queue",
            }
            self._log_state(event)
            return {"ok": False, "upload_blocked": reason, "event": event}

        if self._daily_upload_cap_reached(channel):
            cap = int(channel.daily_upload_cap or 0)
            event = {
                "channel": channel.id,
                "run_dir": str(run_dir_path),
                "title": metadata.get("title") or title_hint or video_path.stem,
                "content_kind": content_kind,
                "video_path": str(video_path),
                "upload_skipped": "daily_upload_cap",
                "uploaded": False,
                "source": "ready_queue",
            }
            self._log_state(event)
            self.logger.warning(channel.id, f"Daily upload cap reached ({cap}); ready-queue publish skipped.")
            return {"ok": False, "upload_skipped": "daily_upload_cap", "event": event}

        youtube_id = None
        youtube_error = None
        if channel.youtube and channel.youtube.upload_enabled:
            thumb = run_dir_path / "thumbnail.jpg"
            if not thumb.exists():
                thumb = None
            try:
                youtube_id = self.youtube.upload(
                    channel=channel,
                    video_path=video_path,
                    metadata=metadata,
                    privacy_status="public" if force_public else channel.youtube.privacy_status,
                    thumbnail_path=thumb,
                    is_short=(content_kind == "short"),
                )
            except UploadLimitExceededError as exc:
                self._set_upload_block(channel.id, "youtube_upload_limit", str(exc), hours=24)
                youtube_error = str(exc)
                self.logger.error(channel.id, "YouTube daily upload limit reached during ready-queue publish", exc)
            except UploadAuthError as exc:
                youtube_error = str(exc)
                self.logger.error(channel.id, "YouTube authorization failed during ready-queue publish", exc)
            except Exception as exc:
                youtube_error = str(exc)
                self.logger.error(channel.id, "YouTube ready-queue upload failed", exc)
        else:
            youtube_error = "youtube_upload_disabled"

        event = {
            "channel": channel.id,
            "run_dir": str(run_dir_path),
            "title": metadata.get("title") or title_hint or video_path.stem,
            "subject": metadata.get("subject"),
            "content_kind": content_kind,
            "video_path": str(video_path),
            "metadata_path": str(meta_path),
            "youtube_id": youtube_id,
            "youtube_error": youtube_error,
            "quality_decision": decision or "pass",
            "uploaded": bool(youtube_id),
            "source": "ready_queue",
        }
        self._log_state(event)
        if youtube_id:
            self.logger.success(channel.id, f"Ready-queue YouTube upload complete: {youtube_id}")
        return {
            "ok": bool(youtube_id),
            "youtube_id": youtube_id,
            "youtube_error": youtube_error,
            "event": event,
        }

    def replay_backlog_once(self, channel_id: str) -> bool:
        channel = self._channel(channel_id)

        # Determine which platforms are active
        fb_enabled = bool(channel.facebook and channel.facebook.upload_enabled)
        yt_enabled = bool(channel.youtube and channel.youtube.upload_enabled)

        # If neither platform active, skip entirely
        if not fb_enabled and not yt_enabled:
            return False

        if self._daily_upload_cap_reached(channel):
            cap = int(channel.daily_upload_cap or 0)
            self.logger.warning(channel_id, f"Daily upload cap reached ({cap}); backlog paused.")
            return False

        item = self._next_backlog_item(channel_id)
        if not item:
            return False

        if self._backlog_item_is_complete(channel, item):
            self._update_backlog_item(channel_id, str(item.get("run_dir") or ""), {"status": "posted", "error": None})
            return True

        stale_issue = self._backlog_item_stale_issue(channel, item)
        if stale_issue:
            run_dir = str(item.get("run_dir") or "")
            self._update_backlog_item(channel_id, run_dir, {"status": "skipped_quality", "error": stale_issue})
            self.logger.warning(channel_id, f"Skipping backlog item for quality: {stale_issue}")
            return True

        quality_issue = self._backlog_title_quality_issue(str(item.get("title") or ""))
        if quality_issue:
            run_dir = str(item.get("run_dir") or "")
            self._update_backlog_item(channel_id, run_dir, {"status": "skipped_quality", "error": quality_issue})
            self.logger.warning(channel_id, f"Skipping backlog item for quality: {quality_issue}")
            return True

        run_dir = Path(item["run_dir"])
        metadata_path = Path(item["metadata_path"])
        metadata = read_json(metadata_path, {})

        # Re-resolve video path
        video_path = self._resolve_video_path(run_dir, metadata.get("content_kind")) or Path(item["video_path"])
        content_kind_for_quality = str(metadata.get("content_kind") or item.get("content_kind") or "").lower()
        if not metadata:
            self._update_backlog_item(channel_id, str(run_dir), {"status": "skipped_quality", "error": "missing metadata"})
            self.logger.warning(channel_id, "Skipping backlog item for quality: missing metadata")
            return True
        if str(metadata.get("quality_decision") or "").lower() in {"hold", "reject"}:
            reason = metadata.get("quality_issue") or metadata.get("quality_issues") or metadata.get("quality_decision")
            self._update_backlog_item(channel_id, str(run_dir), {"status": "skipped_quality", "error": f"quality gate: {reason}"})
            self.logger.warning(channel_id, f"Skipping backlog item for quality: {reason}")
            return True
        duration_seconds = float(metadata.get("duration_seconds") or 0)
        if content_kind_for_quality == "short" and duration_seconds and duration_seconds < 18:
            self._update_backlog_item(channel_id, str(run_dir), {"status": "skipped_quality", "error": f"too short ({duration_seconds:.1f}s)"})
            self.logger.warning(channel_id, f"Skipping backlog item for quality: too short ({duration_seconds:.1f}s)")
            return True

        # --- Catchy Title Upgrade ---
        current_title = item.get("title") or metadata.get("title") or ""
        if current_title.lower().startswith("the real story") or current_title.lower().startswith("the true story"):
            from yt_auto.models import TopicCandidate
            dummy_topic = TopicCandidate(
                niche_id=channel_id,
                title=current_title,
                subject=metadata.get("subject") or current_title,
                style="story", narration="", hook="", visual_captions=[], trend_terms=[],
                source_urls=[], image_queries=[], hashtags=[], engagement_score=0,
                content_kind=metadata.get("content_kind", "short"), narration_beats=[],
                title_variants=[], selected_title_pattern="", scene_plan=[]
            )
            viral_title = self.script_writer.generate_viral_title(channel, dummy_topic)
            if viral_title and viral_title != current_title:
                self.logger.info(channel_id, f"Upgrading backlog title: '{current_title}' -> '{viral_title}'")
                metadata["title"] = viral_title
                item["title"] = viral_title
        
        # Ensure metadata has at least the basic fields required by uploaders
        if "title" not in metadata:
            metadata["title"] = item.get("title") or run_dir.name
        if "description" not in metadata:
            metadata["description"] = f"{metadata['title']} #shorts #{channel_id}"
        metadata = self._refresh_backlog_metadata_seo(channel, run_dir, metadata)
        item["title"] = metadata.get("title") or item.get("title") or run_dir.name
        write_json(metadata_path, metadata)
        # -----------------------------

        thumbnail_file = metadata.get("thumbnail_file")
        thumb_path = run_dir / thumbnail_file if thumbnail_file else None

        # Build target list: use YouTube when Facebook is disabled
        targets = []
        if yt_enabled and not item.get("youtube_id"):
            targets.append("youtube")
        if fb_enabled and not item.get("facebook_id"):
            targets.append("facebook")

        if not targets:
            # Already fully posted, mark it done
            self._update_backlog_item(channel_id, str(run_dir), {"status": "posted"})
            return True

        attempts = int(item.get("attempts", 0) or 0) + 1
        self._update_backlog_item(channel_id, str(run_dir), {"status": "in_progress", "attempts": attempts, "title": item.get("title")})
        try:
            results = self._perform_upload(channel, video_path, metadata, thumb_path, targets=targets)

            youtube_id = results.get("youtube_id") or item.get("youtube_id")
            facebook_id = results.get("facebook_id") or item.get("facebook_id")
            complete = self._backlog_item_is_complete(
                channel,
                {"youtube_id": youtube_id, "facebook_id": facebook_id},
            )
            got_fb = bool(results.get("facebook_id"))
            got_yt = bool(results.get("youtube_id"))
            status = "posted" if complete else "failed"
            error = None if complete else (results.get("youtube_error") or results.get("facebook_error") or "missing required upload id")

            self._update_backlog_item(channel_id, str(run_dir), {
                "status": status,
                "youtube_id": youtube_id,
                "facebook_id": facebook_id,
                "attempts": attempts,
                "error": error,
            })
            if status == "posted":
                self._update_run_log_upload(
                    run_dir=str(run_dir),
                    youtube_id=youtube_id,
                    facebook_id=facebook_id,
                )
                uploaded_to = []
                if got_yt:
                    uploaded_to.append(f"YouTube:{results['youtube_id']}")
                if got_fb:
                    uploaded_to.append(f"FB:{results['facebook_id']}")
                self.logger.success(channel_id, f"Backlog item uploaded: {', '.join(uploaded_to)}")
            else:
                err = results.get("youtube_error") or results.get("facebook_error") or "unknown"
                self.logger.error(channel_id, f"Backlog item upload failed: {err}")

            remaining = [it for it in self._load_backlog(channel_id) if it.get("status") in {"pending", "failed"}]
            self.logger.backlog_status(channel_id, len(remaining))
            return status == "posted"
        except Exception as exc:
            self._update_backlog_item(channel_id, str(run_dir), {"status": "failed", "error": str(exc)})
            return False


    def run_scheduled_slot(self, channel_id: str, upload: bool = True, content_kind: str = "short") -> bool:
        # Check backlog (optional, usually handled by separate job in scheduler)
        if upload:
            self.replay_backlog_once(channel_id)
        
        # Always build new
        try:
            self.build_one(channel_id, upload=upload, content_kind=content_kind)
            return True
        except Exception as exc:
            print(f"[{channel_id}] Scheduled build failed: {exc}")
            return False

    def cleanup_old_outputs(self, days_old: int = 7) -> None:
        """Deletes output video folders older than `days_old` days to save disk space."""
        output_root = self.config.app.output_root
        if not output_root.exists():
            return
            
        cutoff_date = datetime.now() - timedelta(days=days_old)
        deleted_count = 0
        
        self.logger.info("SYSTEM", f"Starting cleanup of outputs older than {days_old} days...")
        
        for channel_dir in output_root.iterdir():
            if not channel_dir.is_dir():
                continue
            for kind_dir in channel_dir.iterdir():
                if not kind_dir.is_dir():
                    continue
                for day_dir in kind_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                        
                    # parse date from folder name YYYY-MM-DD
                    try:
                        folder_date = datetime.strptime(day_dir.name, "%Y-%m-%d")
                    except ValueError:
                        continue
                        
                    if folder_date < cutoff_date:
                        try:
                            shutil.rmtree(day_dir)
                            deleted_count += 1
                        except Exception as e:
                            self.logger.warning("SYSTEM", f"Failed to delete {day_dir}: {e}")
                            
        self.logger.success("SYSTEM", f"Cleanup complete. Deleted {deleted_count} old day folders.")
