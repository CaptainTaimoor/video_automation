from __future__ import annotations

import base64
import hashlib
import os
import io
import json
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import quote, unquote, urlparse, urlunsplit

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from yt_auto.models import TopicCandidate
from yt_auto.utils import ensure_dir, slugify_text, write_json


class HybridMediaFetcher:
    ANCIENT_SHORT_MIN_UNIQUE_REAL_FOR_REUSE = 8
    BRAIN_SHORT_MIN_UNIQUE_REAL_FOR_REUSE = 4
    BRAIN_LONG_MIN_UNIQUE_REAL_FOR_REUSE = 6
    SHORT_MAX_LOCAL_FACT_CARD_FALLBACKS = 1
    # API metadata for these assets is too vague to expose the off-topic context
    # visible on the human source page (for example, mortgage/home-buying stock).
    BRAIN_LENS_BLOCKED_ASSET_IDS = {
        "pexels_video:7577973",
    }
    # Credits verified on the linked Commons file pages.  Keeping this small
    # local manifest makes the documentary's fixed archive spine resilient to
    # MediaWiki API throttling without weakening license or source-page QA.
    CURATED_WIKIMEDIA_CREDITS: dict[str, tuple[str, str]] = {
        "lachish_relief,_british_museum.jpg": ("CC BY-SA 4.0", "Mike Peel"),
        "tel-lakhish-v2-562.jpg": ("CC BY 4.0", "Bukvoed"),
        "battle_siege_of_lachish_wall_panels_in_the_british_museum_(43494312812).jpg": ("CC BY 2.0", "Nate Loper"),
        "capture_of_lachish_-_ramps_and_battle_engines.jpg": ("CC BY-SA 4.0", "Zunkir"),
        "lachishramp053011.jpg": ("CC BY-SA 3.0", "Wilson44691"),
        "assyrian_arrowheads_lachish_bm.jpg": ("CC BY-SA 4.0", "Zunkir"),
        "lachish_relief,_british_museum_10.jpg": ("CC BY-SA 4.0", "Mike Peel"),
        "lachish_relief,_british_museum_8.jpg": ("CC BY-SA 4.0", "Mike Peel"),
        "assyrian_siege-engine_attacking_the_city_wall_of_lachish,_part_of_the_ascending_assaulting_wave._detail_of_a_wall_relief_dating_back_to_the_reign_of_sennacherib,_700-692_bce._from_nineveh,_iraq,_currently_housed_in_the_british_museum.jpg": ("CC BY-SA 4.0", "Osama Shukir Muhammed Amin"),
        "assyrian_archers_and_slingers_from_the_siege_of_lachish_relief_british_museum_124906.jpg": ("CC0", "Ywpark2003"),
        "jewish_captives_and_assyrian_captors,_siege_of_lachish_(43494315232).jpg": ("CC BY 2.0", "Nate Loper"),
        "king_sennacherib_on_his_throne._siege_of_lachish_palace_wall_panel_-_nate_loper_(43494313962).jpg": ("CC BY 2.0", "Nate Loper"),
        "lachish_relief,_british_museum_5.jpg": ("CC BY-SA 4.0", "Mike Peel"),
        "the_fall_of_lachish,_king_sennacherib_reviews_judaean_prisoners..jpg": ("CC BY-SA 4.0", "Osama Shukir Muhammed Amin"),
        "lachish_relief,_british_museum_3.jpg": ("CC BY-SA 4.0", "Mike Peel"),
    }

    def __init__(self, stable_horde_key: str | None = None, pixabay_api_key: str | None = None, pexels_api_key: str | None = None) -> None:
        self.stable_horde_key = (stable_horde_key or "").strip()
        self.pixabay_api_key = (pixabay_api_key or "").strip()
        self.pexels_api_key = (pexels_api_key or "").strip()
        self.enable_pollinations = str(os.getenv("YT_ENABLE_POLLINATIONS", "0")).lower() in {"1", "true", "yes", "on"}
        self.enable_stable_horde = str(os.getenv("YT_ENABLE_STABLE_HORDE", "0")).lower() in {"1", "true", "yes", "on"}

        self.short_query_limit = self._env_int("YT_VISUAL_SHORT_QUERY_LIMIT", 4, 1, 8)
        self.long_query_limit = self._env_int("YT_VISUAL_LONG_QUERY_LIMIT", 4, 1, 10)
        self.short_scene_budget_seconds = self._env_float("YT_VISUAL_SHORT_SCENE_DEADLINE_SECONDS", 75.0, 5.0, 180.0)
        self.long_scene_budget_seconds = self._env_float("YT_VISUAL_LONG_SCENE_DEADLINE_SECONDS", 120.0, 10.0, 300.0)
        self.provider_budget_seconds = self._env_float("YT_VISUAL_PROVIDER_DEADLINE_SECONDS", 18.0, 2.0, 60.0)
        self.short_fetch_budget_seconds = self._env_float("YT_VISUAL_SHORT_FETCH_DEADLINE_SECONDS", 360.0, 30.0, 900.0)
        self.long_fetch_budget_seconds = self._env_float("YT_VISUAL_LONG_FETCH_DEADLINE_SECONDS", 900.0, 60.0, 2400.0)
        self.min_source_short_edge = self._env_int("YT_VISUAL_MIN_SOURCE_SHORT_EDGE", 720, 256, 2160)
        self.allow_unprovenanced_media = str(os.getenv("YT_ALLOW_UNPROVENANCED_MEDIA", "0")).lower() in {"1", "true", "yes", "on"}
        self.enable_local_visual_relevance = str(os.getenv("YT_ENABLE_LOCAL_VISUAL_RELEVANCE", "0")).lower() in {"1", "true", "yes", "on"}
        self.local_visual_relevance_url = str(os.getenv("YT_LOCAL_VISUAL_RELEVANCE_URL", "http://127.0.0.1:11434/api/chat")).strip()
        self.local_visual_relevance_model = str(os.getenv("YT_LOCAL_VISUAL_RELEVANCE_MODEL", "qwen3.6:latest")).strip()
        self.local_visual_relevance_timeout = self._env_float("YT_LOCAL_VISUAL_RELEVANCE_TIMEOUT_SECONDS", 18.0, 2.0, 45.0)
        self.local_visual_relevance_min_score = self._env_int("YT_LOCAL_VISUAL_RELEVANCE_MIN_SCORE", 55, 0, 100)
        self._provider_backoff_until: Dict[str, float] = {}
        self._provider_failures: Dict[str, int] = {}
        self._visual_relevance_cache: Dict[str, Dict[str, object]] = {}
        self._wikimedia_search_cache: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}
        self._ancient_archive_image_cache: Dict[str, Image.Image] = {}
        self._ancient_diagram_archive_used: set[str] = set()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
                "Referer": "https://en.wikipedia.org/",
            }
        )

    @staticmethod
    def _allow_ai_image_fallback(topic: TopicCandidate) -> bool:
        return topic.niche_id != "ancient_history" and not (
            topic.niche_id == "brain_lens" and topic.content_kind == "short"
        )

    @classmethod
    def _short_fallback_limit_exceeded(cls, topic: TopicCandidate, fallback_count: int) -> bool:
        return (
            topic.content_kind == "short"
            and topic.niche_id in {"ancient_history", "brain_lens"}
            and fallback_count > cls.SHORT_MAX_LOCAL_FACT_CARD_FALLBACKS
        )

    @staticmethod
    def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(str(os.getenv(name, default)).strip())
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(str(os.getenv(name, default)).strip())
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _deadline_timeout(deadline: float | None, default: float) -> float | None:
        if deadline is None:
            return max(0.1, default)
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            return None
        return max(0.1, min(default, remaining))

    def _provider_available(self, provider: str) -> bool:
        return time.time() >= self._provider_backoff_until.get(provider, 0.0)

    def _mark_provider_backoff(self, provider: str, seconds: int, reason: str) -> None:
        backoff_seconds = max(30, int(seconds))
        until = time.time() + backoff_seconds
        previous = self._provider_backoff_until.get(provider, 0.0)
        self._provider_backoff_until[provider] = max(previous, until)
        if until > previous + 5:
            print(f"{provider.upper()} BACKOFF: pausing requests for {backoff_seconds}s after {reason}")

    def _reset_provider_failures(self, provider: str) -> None:
        self._provider_failures[provider] = 0

    def _record_provider_failure(self, provider: str) -> int:
        failures = self._provider_failures.get(provider, 0) + 1
        self._provider_failures[provider] = failures
        return failures

    def _scene_size(self, topic: TopicCandidate) -> tuple[int, int]:
        return (1920, 1080) if topic.content_kind == "video" else (1080, 1920)

    # ── Visual style presets ──────────────────────────────────────────────────
    # Each value is appended to the Pollinations prompt to enforce the visual style.
    VISUAL_STYLE_PROMPTS: dict[str, str] = {
        # Realism styles (no/minimal text in image)
        "documentary":   "premium documentary film still, photorealistic, cinematic color grading, shallow depth of field, dramatic natural lighting, 4K quality",
        "stock":         "clean professional stock photography, bright natural lighting, sharp focus, neutral background, editorial quality",
        "3d-render":     "stylized 3D render, volumetric lighting, subsurface scattering, physically-based rendering, cinematic composition, Unreal Engine quality",
        # Illustrated styles
        "anime":         "anime art style, studio-quality illustration, detailed linework, vibrant colors, dynamic composition, Makoto Shinkai influence",
        "comic":         "comic book art style, bold ink outlines, dynamic panel composition, cel shading, Marvel/DC quality illustration",
        "sketch":        "pencil sketch illustration, detailed crosshatching, expressive linework, black and white with subtle grey wash",
        "whiteboard":    "whiteboard explainer illustration, black marker on white background, hand-drawn style, simple clean diagrams",
        # Graphic/design styles
        "dark-tech":     "dark background, electric neon accent colors, glowing particles, digital circuit patterns, cyber aesthetic, high contrast",
        "infographic":   "clean data visualization design, bold typography layout, minimal color palette, statistical chart elements, professional editorial",
        "presentation":  "structured slide design, clean headline layout, gradient background, professional corporate aesthetic, minimal and bold",
    }

    def _visual_style_suffix(self, niche_id: str, visual_style: str | None = None) -> str:
        """Return style prompt suffix, falling back to niche default."""
        style = (visual_style or "").lower().strip()
        if style in self.VISUAL_STYLE_PROMPTS:
            return self.VISUAL_STYLE_PROMPTS[style]
        # Niche defaults
        if niche_id == "ancient_history":
            return self.VISUAL_STYLE_PROMPTS["documentary"]
        if niche_id == "brain_lens":
            return self.VISUAL_STYLE_PROMPTS["dark-tech"]
        return self.VISUAL_STYLE_PROMPTS["documentary"]

    def _fallback_visual_prompt(self, topic: TopicCandidate, text: str,
                                 scene_prompt: str = "", visual_style: str | None = None) -> str:
        if scene_prompt:
            # Append style suffix to user-provided prompt too
            style_sfx = self._visual_style_suffix(topic.niche_id, visual_style)
            return f"{scene_prompt}, {style_sfx}"
        frame_phrase = "landscape frame" if topic.content_kind == "video" else "vertical frame"
        style_sfx = self._visual_style_suffix(topic.niche_id, visual_style)
        return f"{topic.title}. {text}. {style_sfx}, {frame_phrase}"


    def _wikimedia_search(self, query: str, limit: int = 8, deadline: float | None = None) -> List[Tuple[str, Dict[str, str]]]:
        cache_key = f"{re.sub(r'\s+', ' ', query or '').strip().lower()}|{int(limit)}"
        cached = self._wikimedia_search_cache.get(cache_key)
        if cached is not None:
            # Validation annotates metadata, so callers receive independent copies.
            return [(url, dict(meta)) for url, meta in cached]
        endpoint = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": "6",
            "gsrlimit": str(limit),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": "1600",
        }
        r = None
        for attempt in range(3):
            timeout = self._deadline_timeout(deadline, 18.0)
            if timeout is None:
                return []
            r = self.session.get(endpoint, params=params, timeout=timeout)
            if int(getattr(r, "status_code", 200) or 200) != 429:
                r.raise_for_status()
                break
            if attempt >= 2:
                return []
            try:
                retry_after = float(str(getattr(r, "headers", {}).get("Retry-After", "1")).strip())
            except (TypeError, ValueError):
                retry_after = 1.0
            retry_after = max(0.2, min(5.0, retry_after))
            pause = self._deadline_timeout(deadline, retry_after)
            if pause is None or pause + 0.05 < retry_after:
                return []
            time.sleep(retry_after)
        if r is None:
            return []
        data = r.json()

        results: List[Tuple[str, Dict[str, str]]] = []
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            media_path = urlparse(str(url or "")).path.lower()
            if not url or not media_path.endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            meta = info.get("extmetadata", {}) or {}
            results.append(
                (
                    url,
                    {
                        "source": "wikimedia",
                        "license": str((meta.get("LicenseShortName", {}) or {}).get("value", "Unknown")),
                        "artist": str((meta.get("Artist", {}) or {}).get("value", "Unknown")),
                        "asset_title": str(page.get("title", "")),
                        "asset_id": f"wikimedia:{page.get('pageid') or page.get('title', '')}",
                        "source_page": str(info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title', '')), safe=':')}"),
                        "media_width": str(info.get("thumbwidth") or info.get("width") or ""),
                        "media_height": str(info.get("thumbheight") or info.get("height") or ""),
                        "original_media_width": str(info.get("width") or ""),
                        "original_media_height": str(info.get("height") or ""),
                    },
                )
            )
        self._wikimedia_search_cache[cache_key] = [(url, dict(meta)) for url, meta in results]
        return [(url, dict(meta)) for url, meta in results]

    def _pixabay_search(self, query: str, limit: int = 5, deadline: float | None = None) -> List[Tuple[str, Dict[str, str]]]:
        if not self.pixabay_api_key:
            return []
        endpoint = "https://pixabay.com/api/"
        params = {
            "key": self.pixabay_api_key,
            "q": query,
            "image_type": "photo",
            "per_page": str(limit),
            "safesearch": "true",
        }
        timeout = self._deadline_timeout(deadline, 18.0)
        if timeout is None:
            return []
        r = self.session.get(endpoint, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()

        results: List[Tuple[str, Dict[str, str]]] = []
        for hit in data.get("hits", []):
            img = hit.get("largeImageURL") or hit.get("webformatURL")
            if not img:
                continue
            results.append(
                (
                    img,
                    {
                        "source": "pixabay",
                        "license": "Pixabay License",
                        "artist": str(hit.get("user", "Unknown")),
                        "asset_title": str(hit.get("tags", "")),
                        "asset_id": f"pixabay:{hit.get('id')}",
                        "source_page": str(hit.get("pageURL") or ""),
                        "media_width": str(hit.get("imageWidth") or hit.get("webformatWidth") or ""),
                        "media_height": str(hit.get("imageHeight") or hit.get("webformatHeight") or ""),
                    },
                )
            )
        return results

    def _wikimedia_metadata_from_url(self, url: str, deadline: float | None = None) -> Dict[str, str] | None:
        parsed = urlparse(url)
        if parsed.netloc.lower() != "upload.wikimedia.org":
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        filename = unquote(path_parts[-2] if "thumb" in path_parts and len(path_parts) >= 2 else path_parts[-1])
        if not filename:
            return None
        curated_credit = self.CURATED_WIKIMEDIA_CREDITS.get(filename.lower())
        if curated_credit is not None:
            license_name, artist = curated_credit
            page_title = f"File:{filename}"
            return {
                "source": "wikimedia",
                "license": license_name,
                "artist": artist,
                "asset_title": page_title,
                "asset_id": f"wikimedia:file:{filename.lower()}",
                "source_page": f"https://commons.wikimedia.org/wiki/{quote(page_title, safe=':')}",
                "media_width": "",
                "media_height": "",
            }
        params = {
            "action": "query",
            "format": "json",
            "titles": f"File:{filename}",
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            # Prefer a bounded Commons derivative. Huge originals frequently trip
            # CDN throttling and add no useful detail to a 1080p video crop.
            "iiurlwidth": "1600",
        }
        endpoints = (
            "https://en.wikipedia.org/w/api.php",
            "https://commons.wikimedia.org/w/api.php",
        )
        for endpoint in endpoints:
            for attempt in range(3):
                timeout = self._deadline_timeout(deadline, 12.0)
                if timeout is None:
                    return None
                try:
                    response = self.session.get(endpoint, params=params, timeout=timeout)
                    response.raise_for_status()
                    pages = response.json().get("query", {}).get("pages", {})
                    page = next(iter(pages.values()), {})
                    image_info = page.get("imageinfo") or []
                    if not image_info:
                        break
                    info = image_info[0]
                    extmetadata = info.get("extmetadata", {}) or {}
                    license_name = str((extmetadata.get("LicenseShortName", {}) or {}).get("value", "Unknown"))
                    artist = str((extmetadata.get("Artist", {}) or {}).get("value", "Unknown"))
                    artist = re.sub(r"<[^>]+>", " ", artist)
                    artist = re.sub(r"\s+", " ", artist).strip() or "Unknown"
                    page_title = str(page.get("title", f"File:{filename}"))
                    return {
                        "source": "wikimedia",
                        "license": license_name,
                        "artist": artist,
                        "asset_title": page_title,
                        "asset_id": f"wikimedia:{page.get('pageid') or filename}",
                        "source_page": str(info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{quote(page_title, safe=':')}"),
                        "media_width": str(info.get("width") or ""),
                        "media_height": str(info.get("height") or ""),
                    }
                except Exception:
                    pause = self._deadline_timeout(deadline, 0.6 + attempt)
                    if attempt < 2 and pause is not None:
                        time.sleep(min(0.6 + attempt, pause))
        return None

    def _pexels_photo_search(self, query: str, limit: int = 4, orientation: str = "portrait", deadline: float | None = None) -> List[Tuple[str, Dict[str, str]]]:
        if not self.pexels_api_key:
            return []

        endpoint = "https://api.pexels.com/v1/search"
        headers = {"Authorization": self.pexels_api_key}
        params = {"query": query, "per_page": limit, "orientation": orientation}

        try:
            timeout = self._deadline_timeout(deadline, 18.0)
            if timeout is None:
                return []
            r = self.session.get(endpoint, headers=headers, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        results: List[Tuple[str, Dict[str, str]]] = []
        for photo in data.get("photos", []):
            src = photo.get("src") or {}
            img = src.get("large2x") or src.get("large") or src.get("portrait") or src.get("original")
            if not img:
                continue
            results.append(
                (
                    img,
                    {
                        "source": "pexels_photo",
                        "license": "Pexels License",
                        "artist": str((photo.get("photographer") or "Unknown")),
                        "asset_title": str(photo.get("alt") or ""),
                        "asset_id": f"pexels_photo:{photo.get('id')}",
                        "source_page": str(photo.get("url") or ""),
                        "media_width": str(photo.get("width") or ""),
                        "media_height": str(photo.get("height") or ""),
                    },
                )
            )
        return results

    def _save_media_bytes(self, data: bytes, out_path: Path) -> bool:
        if out_path.suffix.lower() == ".mp4":
            if len(data) < 100_000:
                return False
            try:
                out_path.write_bytes(data)
                return True
            except Exception:
                return False
        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.save(out_path, format="JPEG", quality=92)
            return True
        except Exception:
            return False

    def _download(self, url: str, out_path: Path, deadline: float | None = None) -> bool:
        for attempt in range(3):
            timeout = self._deadline_timeout(deadline, 16.0)
            if timeout is None:
                return False
            try:
                r = self.session.get(url, timeout=timeout)
                r.raise_for_status()
                if self._save_media_bytes(r.content, out_path):
                    return True
            except Exception:
                pass
            pause = self._deadline_timeout(deadline, 0.5 + attempt)
            if pause is None:
                return False
            time.sleep(min(0.5 + attempt, pause))
        return False

    def _pexels_video_search(self, query: str, limit: int = 3, orientation: str = "portrait", deadline: float | None = None) -> List[Tuple[str, Dict[str, str]]]:
        if not self.pexels_api_key:
            return []
        
        endpoint = "https://api.pexels.com/videos/search"
        headers = {"Authorization": self.pexels_api_key}
        params = {"query": query, "per_page": limit, "orientation": orientation}
        
        try:
            timeout = self._deadline_timeout(deadline, 18.0)
            if timeout is None:
                return []
            r = self.session.get(endpoint, headers=headers, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        results: List[Tuple[str, Dict[str, str]]] = []
        for video in data.get("videos", []):
            files = [item for item in video.get("video_files", []) if item.get("link")]
            target_w, target_h = (1920, 1080) if orientation == "landscape" else (1080, 1920)
            target_ratio = target_w / target_h

            def candidate_score(item: dict) -> tuple[int, int, float, int, int]:
                width = int(item.get("width") or 0)
                height = int(item.get("height") or 0)
                if width <= 0 or height <= 0:
                    return (0, 0, -99.0, 0, 0)
                orientation_match = int((width >= height) == (orientation == "landscape"))
                short_edge = min(width, height)
                target_short_edge = min(target_w, target_h)
                aspect_score = -abs((width / height) - target_ratio)
                quality_score = int(str(item.get("quality") or "").lower() == "hd")
                size_score = -abs((width * height) - (target_w * target_h))
                return (
                    orientation_match,
                    min(short_edge, target_short_edge),
                    aspect_score,
                    quality_score,
                    size_score,
                )

            best_file = max(files, key=candidate_score, default=None)
            if best_file:
                best_width = int(best_file.get("width") or 0)
                best_height = int(best_file.get("height") or 0)
                orientation_matches = (best_width >= best_height) == (orientation == "landscape")
                if not orientation_matches:
                    best_file = None
                
            if not best_file or not best_file.get("link"):
                continue

            results.append(
                (
                    best_file.get("link"),
                    {
                        "source": "pexels_video",
                        "license": "Pexels License",
                        "artist": str(video.get("user", {}).get("name", "Unknown")),
                        "asset_title": str(video.get("url") or ""),
                        "asset_id": f"pexels_video:{video.get('id')}",
                        "source_page": str(video.get("url") or ""),
                        "is_video": "true",
                        "media_width": str(best_file.get("width") or ""),
                        "media_height": str(best_file.get("height") or ""),
                        "orientation": "landscape" if int(best_file.get("width") or 0) >= int(best_file.get("height") or 0) else "portrait",
                    },
                )
            )
        return results

    def _pollinations_image_generate(self, prompt: str, out_path: Path, deadline: float | None = None) -> bool:
        provider = "pollinations"
        if not self._provider_available(provider):
            return False

        # Generate a perfectly crisp image (api currently defaults to a 1024x1024 square free generation which we'll crop natively)
        import urllib.parse

        encoded_prompt = urllib.parse.quote(prompt)
        # Using nologo to keep the image clean, enhance=true for cinematic look. Omitting width/height because it currently causes HTTP 500.
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?nologo=true&enhance=true"

        max_retries = 2
        for attempt in range(max_retries):
            timeout = self._deadline_timeout(deadline, 45.0)
            if timeout is None:
                return False
            try:
                r = self.session.get(url, timeout=timeout)
                r.raise_for_status()
                if self._save_media_bytes(r.content, out_path):
                    self._reset_provider_failures(provider)
                    return True
                print("POLLINATIONS ERROR: _save_media_bytes returned False")
                failures = self._record_provider_failure(provider)
                if failures >= 2:
                    self._mark_provider_backoff(provider, 300, "invalid image payload")
            except requests.HTTPError as e:
                print(f"POLLINATIONS ERROR: {e}")
                status = getattr(getattr(e, "response", None), "status_code", None)
                failures = self._record_provider_failure(provider)
                if status == 429:
                    self._mark_provider_backoff(provider, 1800, "HTTP 429 rate limit")
                    return False
                if status and status >= 500:
                    if attempt + 1 < max_retries:
                        pause = self._deadline_timeout(deadline, 1.0 + attempt)
                        if pause is None:
                            return False
                        time.sleep(min(1.0 + attempt, pause))
                        continue
                    self._mark_provider_backoff(provider, min(900, 180 * failures), f"HTTP {status}")
                    return False
                return False
            except (requests.Timeout, requests.exceptions.ReadTimeout) as e:
                print(f"POLLINATIONS TIMEOUT: {e} (attempt {attempt+1}/{max_retries})")
                failures = self._record_provider_failure(provider)
                if attempt + 1 < max_retries:
                    pause = self._deadline_timeout(deadline, 2.0)
                    if pause is None:
                        return False
                    time.sleep(min(2.0, pause))
                    continue
                # For timeouts, don't back off as long as HTTP 500s
                self._mark_provider_backoff(provider, 60, "read timeout")
                return False
            except requests.RequestException as e:
                print(f"POLLINATIONS ERROR: {e}")
                failures = self._record_provider_failure(provider)
                if attempt + 1 < max_retries:
                    pause = self._deadline_timeout(deadline, 1.0 + attempt)
                    if pause is None:
                        return False
                    time.sleep(min(1.0 + attempt, pause))
                    continue
                self._mark_provider_backoff(provider, min(600, 120 * failures), "network instability")
        return False

    def _generate_editorial_fallback(self, topic: TopicCandidate, text: str, out_path: Path, index: int) -> bool:
        w, h = self._scene_size(topic)
        palette_by_niche = {
            "ancient_history": ((28, 18, 10), (106, 74, 39), (215, 177, 112)),
            "brain_lens": ((10, 27, 44), (23, 88, 121), (157, 233, 255)),
        }
        start, end, accent = palette_by_niche.get(topic.niche_id, ((22, 28, 45), (74, 45, 66), (241, 196, 15)))
        seed_text = (text or topic.title or topic.subject or 'scene').strip()
        seed = sum((i + 1) * ord(ch) for i, ch in enumerate(seed_text))

        base = self._gradient_bg((w, h), index + (seed % 5)).convert('RGBA')
        overlay = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        pad_x = 70 if w < h else 110
        pad_y = 120 if w < h else 80
        inner = (pad_x, pad_y, w - pad_x, h - pad_y)
        accent_soft = tuple(min(255, c + 34) for c in accent)
        accent_glow = (*accent, 42)

        for band in range(9):
            y = int((h / 10) * (band + 0.5))
            alpha = 8 + ((band + seed) % 4) * 4
            draw.line((0, y, w, y), fill=(255, 255, 255, alpha), width=2)

        draw.ellipse((w - 420, 160, w - 40, 540), fill=(*accent_soft, 30))
        draw.ellipse((40, h - 520, 420, h - 120), fill=(255, 255, 255, 14))
        draw.rounded_rectangle(inner, radius=54, outline=(*accent_soft, 138), width=5)
        draw.rounded_rectangle((inner[0] + 24, inner[1] + 24, inner[2] - 24, inner[3] - 24), radius=46, outline=(255, 255, 255, 24), width=2)

        label = self._clean_caption(text) or self._short_title(topic.title)
        label_words = label.split()
        label = ' '.join(label_words[:4]).upper() if label_words else self._short_title(topic.title).upper()
        label = label[:34]
        chip_font = self._font(38 if w < h else 30)
        chip_box = draw.textbbox((0, 0), label, font=chip_font)
        chip_w = chip_box[2] - chip_box[0] + 48
        chip_h = chip_box[3] - chip_box[1] + 24
        chip_x = inner[0] + 30
        chip_y = inner[1] + 28
        draw.rounded_rectangle((chip_x, chip_y, chip_x + chip_w, chip_y + chip_h), radius=24, fill=(8, 12, 20, 126), outline=(*accent_soft, 120), width=2)
        draw.text((chip_x + 24, chip_y + 12), label, fill=(248, 250, 252, 232), font=chip_font)

        if topic.niche_id == 'brain_lens':
            variant = seed % 4
            if variant == 0:
                center_x = int(w * 0.5)
                center_y = int(h * 0.47)
                for radius in (250, 182, 120):
                    draw.ellipse((center_x - radius, center_y - radius, center_x + radius, center_y + radius), outline=(255, 255, 255, 28), width=3)
                nodes = [
                    (center_x - 185, center_y - 70),
                    (center_x - 92, center_y - 182),
                    (center_x + 40, center_y - 210),
                    (center_x + 178, center_y - 82),
                    (center_x + 192, center_y + 72),
                    (center_x + 28, center_y + 188),
                    (center_x - 128, center_y + 164),
                    (center_x - 206, center_y + 28),
                ]
                for idx_a in range(len(nodes)):
                    x1, y1 = nodes[idx_a]
                    x2, y2 = nodes[(idx_a + 1) % len(nodes)]
                    draw.line((x1, y1, x2, y2), fill=(*accent_soft, 88), width=4)
                for x, y in nodes:
                    draw.ellipse((x - 18, y - 18, x + 18, y + 18), fill=(*accent_soft, 196), outline=(255, 255, 255, 120), width=3)
            elif variant == 1:
                panel_w = int((inner[2] - inner[0]) * 0.72)
                panel_h = 150 if w < h else 120
                left = inner[0] + 70
                top = int(h * 0.35)
                for offset in range(3):
                    y0 = top + (offset * (panel_h + 34))
                    draw.rounded_rectangle((left, y0, left + panel_w, y0 + panel_h), radius=32, fill=(9, 18, 31, 82), outline=(*accent_soft, 110), width=3)
                    draw.rectangle((left + 32, y0 + 38, left + 58, y0 + panel_h - 38), fill=(*accent_soft, 168))
                    draw.line((left + 94, y0 + 58, left + panel_w - 34, y0 + 58), fill=(255, 255, 255, 96), width=6)
                    draw.line((left + 94, y0 + 92, left + panel_w - 98, y0 + 92), fill=(255, 255, 255, 54), width=4)
                draw.line((left + panel_w - 58, top + 40, left + panel_w + 34, top - 10), fill=(*accent_soft, 120), width=8)
                draw.line((left + panel_w + 34, top - 10, left + panel_w + 94, top + 44), fill=(*accent_soft, 120), width=8)
            elif variant == 2:
                chart_left = inner[0] + 80
                chart_bottom = int(h * 0.76)
                widths = [74, 112, 86, 136, 98]
                heights = [180, 260, 210, 320, 238]
                cursor = chart_left
                for width, bar_h in zip(widths, heights):
                    draw.rounded_rectangle((cursor, chart_bottom - bar_h, cursor + width, chart_bottom), radius=24, fill=(*accent_soft, 118), outline=(255, 255, 255, 44), width=2)
                    cursor += width + 28
                draw.arc((inner[0] + 110, int(h * 0.28), inner[2] - 110, int(h * 0.7)), start=205, end=332, fill=(255, 255, 255, 80), width=6)
                draw.ellipse((int(w * 0.64) - 48, int(h * 0.39) - 48, int(w * 0.64) + 48, int(h * 0.39) + 48), fill=(*accent_soft, 182), outline=(255, 255, 255, 138), width=3)
            else:
                focus_x = int(w * 0.5)
                focus_y = int(h * 0.5)
                draw.rounded_rectangle((focus_x - 220, focus_y - 170, focus_x + 220, focus_y + 170), radius=40, fill=(12, 18, 32, 70), outline=(*accent_soft, 120), width=3)
                for step in range(4):
                    inset = step * 38
                    draw.rounded_rectangle((focus_x - 170 + inset, focus_y - 122 + inset, focus_x + 170 - inset, focus_y + 122 - inset), radius=30, outline=(255, 255, 255, max(16, 72 - step * 14)), width=2)
                branches = [
                    ((focus_x - 220, focus_y - 70), (focus_x - 310, focus_y - 165)),
                    ((focus_x + 220, focus_y - 48), (focus_x + 314, focus_y - 162)),
                    ((focus_x - 220, focus_y + 76), (focus_x - 318, focus_y + 178)),
                    ((focus_x + 220, focus_y + 62), (focus_x + 306, focus_y + 188)),
                ]
                for (x1, y1), (x2, y2) in branches:
                    draw.line((x1, y1, x2, y2), fill=(*accent_soft, 110), width=5)
                    draw.rounded_rectangle((x2 - 92, y2 - 42, x2 + 92, y2 + 42), radius=22, fill=(8, 12, 20, 110), outline=(255, 255, 255, 54), width=2)
        elif topic.niche_id == 'ancient_history':
            variant = seed % 4
            if variant == 0:
                for step in range(5):
                    inset = 116 + (step * 26)
                    draw.rounded_rectangle((inset, 220 + (step * 14), w - inset, h - 250 - (step * 22)), radius=42, outline=(255, 232, 188, max(12, 46 - step * 7)), width=2)
                base_y = int(h * 0.79)
                for col in range(5):
                    x = int(w * 0.18) + (col * int(w * 0.12))
                    draw.rectangle((x, base_y - 230, x + 34, base_y), fill=(104, 72, 34, 120))
                    draw.polygon([(x - 18, base_y - 230), (x + 17, base_y - 304), (x + 52, base_y - 230)], fill=(163, 118, 62, 96))
            elif variant == 1:
                ship_mid = int(h * 0.58)
                hull = [(int(w * 0.22), ship_mid), (int(w * 0.76), ship_mid), (int(w * 0.68), ship_mid + 88), (int(w * 0.3), ship_mid + 88)]
                draw.polygon(hull, fill=(102, 63, 24, 140), outline=(226, 192, 130, 110))
                mast_x = int(w * 0.49)
                draw.rectangle((mast_x - 10, ship_mid - 240, mast_x + 10, ship_mid + 24), fill=(163, 118, 62, 120))
                draw.polygon([(mast_x + 10, ship_mid - 232), (mast_x + 10, ship_mid - 40), (mast_x + 184, ship_mid - 132)], fill=(184, 144, 88, 84), outline=(255, 233, 196, 60))
                for wave in range(4):
                    y = ship_mid + 108 + wave * 28
                    draw.arc((int(w * 0.18), y - 38, int(w * 0.82), y + 18), start=0, end=180, fill=(255, 219, 168, 56), width=4)
            elif variant == 2:
                coin_x = int(w * 0.5)
                coin_y = int(h * 0.5)
                for radius, alpha in ((240, 36), (210, 70), (180, 120)):
                    draw.ellipse((coin_x - radius, coin_y - radius, coin_x + radius, coin_y + radius), outline=(255, 220, 164, alpha), width=4)
                draw.ellipse((coin_x - 132, coin_y - 132, coin_x + 132, coin_y + 132), fill=(122, 82, 38, 92), outline=(255, 236, 198, 94), width=3)
                for offset in (-210, -150, 150, 210):
                    draw.arc((coin_x - 286, coin_y + offset - 80, coin_x - 80, coin_y + offset + 80), start=290, end=70, fill=(255, 224, 170, 64), width=4)
                    draw.arc((coin_x + 80, coin_y + offset - 80, coin_x + 286, coin_y + offset + 80), start=110, end=250, fill=(255, 224, 170, 64), width=4)
            else:
                draw.rounded_rectangle((int(w * 0.2), int(h * 0.3), int(w * 0.8), int(h * 0.78)), radius=38, fill=(22, 14, 8, 66), outline=(255, 230, 180, 86), width=3)
                route_points = [
                    (int(w * 0.28), int(h * 0.68)),
                    (int(w * 0.36), int(h * 0.56)),
                    (int(w * 0.46), int(h * 0.6)),
                    (int(w * 0.56), int(h * 0.46)),
                    (int(w * 0.68), int(h * 0.54)),
                ]
                for idx_a in range(len(route_points) - 1):
                    x1, y1 = route_points[idx_a]
                    x2, y2 = route_points[idx_a + 1]
                    draw.line((x1, y1, x2, y2), fill=(255, 222, 168, 110), width=5)
                for x, y in route_points:
                    draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=(222, 180, 116, 186), outline=(255, 243, 220, 120), width=2)
                roof = [(int(w * 0.3), int(h * 0.42)), (int(w * 0.5), int(h * 0.28)), (int(w * 0.7), int(h * 0.42))]
                draw.polygon(roof, fill=(126, 88, 46, 72), outline=(255, 224, 182, 70))
                for col in range(4):
                    x = int(w * 0.34) + (col * int(w * 0.09))
                    draw.rectangle((x, int(h * 0.42), x + 24, int(h * 0.62)), fill=(105, 72, 36, 90))
        else:
            for step in range(5):
                inset = 110 + (step * 36)
                draw.rounded_rectangle((inset, 220 + (step * 18), w - inset, h - 260 - (step * 28)), radius=46, outline=(255, 255, 255, max(10, 45 - step * 7)), width=2)

        img = Image.alpha_composite(base, overlay)
        img = Image.blend(img, Image.new('RGBA', (w, h), (*start, 255)), alpha=0.14)
        img = Image.blend(img, Image.new('RGBA', (w, h), (*end, 255)), alpha=0.08)
        img = Image.blend(img, Image.new('RGBA', (w, h), (8, 10, 14, 255)), alpha=0.06)
        img.convert('RGB').save(out_path, format='JPEG', quality=93)
        return True

    def _ancient_documentary_diagram_kind(self, topic: TopicCandidate, text: str) -> str:
        identity = f"{topic.subject} {topic.title}".lower()
        scene_text = (text or "").lower()
        if "machu picchu" in identity:
            if any(marker in scene_text for marker in ("graded layers", "stone, gravel", "beneath each terrace")):
                return "machu_terrace_layers"
            if any(marker in scene_text for marker in ("canal", "runoff", "erosion")):
                return "machu_water_flow"
        if topic.content_kind == "short" and "great zimbabwe" in identity:
            if any(marker in scene_text for marker in ("soapstone bird", "soapstone birds")):
                return "great_zimbabwe_bird_evidence"
            if any(
                marker in scene_text
                for marker in ("glass bead", "glass beads", "ceramic", "trade goods", "imported goods")
            ):
                return "great_zimbabwe_trade_evidence"
        if topic.content_kind != "video":
            return ""
        if "lachish" in identity:
            # These beats need explanatory evidence graphics, not another
            # interchangeable museum-relief close-up.  Each diagram states
            # exactly what is known and, where relevant, what remains unknown.
            if any(marker in scene_text for marker in ("judah builds back", "counter-ramp", "counter ramp")):
                return "lachish_siege_section"
            if any(marker in scene_text for marker in ("fire in 701", "burned tower", "archaeomagnetic")):
                return "lachish_fire_evidence"
            if any(marker in scene_text for marker in ("three records", "three agendas")):
                return "lachish_source_triangle"
            if any(marker in scene_text for marker in ("jerusalem is a separate", "jerusalem separate")):
                return "lachish_geography"
            if any(marker in scene_text for marker in ("two destructions", "two stories", "level iii", "level ii")):
                return "lachish_two_layers"
        diagram_markers = (
            ("evidence_board", ("how the case is built", "build the case", "evidence chain")),
            ("chronology_timeline", ("dates in order", "chronology", "date the later memory")),
            ("terrain_map", ("physical setting", "restore the landscape", "map the", "landscape")),
            ("artifact_context", ("excavated evidence", "artifact context", "read the evidence")),
            ("source_comparison", ("written sources", "source comparison", "cross-check the opening")),
            ("logistics_flow", ("labor and supply", "real scale", "sustained coordination", "organized power")),
            ("ordinary_life_grid", ("ordinary experience", "ordinary life", "daily life")),
            ("network_routes", ("wider connections", "trace the network", "exchange network")),
            ("authority_stack", ("organized power", "authority becomes action", "controlled access")),
            ("rival_matrix", ("rival explanation", "rival account", "proof from possibility", "remains uncertain")),
            ("preservation_filter", ("preservation", "why this evidence survived", "finding from story")),
            ("consequence_chain", ("lasting change", "follow the consequence", "trace what survived", "still matters")),
        )
        for diagram_kind, markers in diagram_markers:
            if any(marker in scene_text for marker in markers):
                return diagram_kind
        return ""

    def _generate_generic_ancient_documentary_diagram(
        self,
        topic: TopicCandidate,
        out_path: Path,
        diagram_kind: str,
    ) -> bool:
        w, h = self._scene_size(topic)
        canvas = Image.new("RGB", (w, h), (12, 17, 22))
        draw = ImageDraw.Draw(canvas)
        for y in range(h):
            ratio = y / max(1, h - 1)
            draw.line(
                (0, y, w, y),
                fill=(
                    int(13 + 24 * ratio),
                    int(19 + 20 * ratio),
                    int(25 + 15 * ratio),
                ),
            )

        accent = (220, 166, 82)
        pale = (246, 238, 218)
        muted = (174, 183, 184)
        panel = (28, 34, 39)
        line = (113, 126, 128)
        title_font = self._font(64 if w >= h else 54)
        subject_font = self._font(30 if w >= h else 34)
        label_font = self._font(29 if w >= h else 32)
        small_font = self._font(23 if w >= h else 26)
        titles = {
            "evidence_board": "HOW THE CASE IS BUILT",
            "chronology_timeline": "PUT THE EVIDENCE IN ORDER",
            "terrain_map": "THE LANDSCAPE IS EVIDENCE",
            "artifact_context": "AN OBJECT NEEDS CONTEXT",
            "source_comparison": "CLAIM VERSUS PHYSICAL TRACE",
            "logistics_flow": "WHAT SUSTAINED ACTION REQUIRES",
            "ordinary_life_grid": "LOOK BEYOND ELITE SOURCES",
            "network_routes": "TRACE THE WIDER NETWORK",
            "authority_stack": "HOW POWER BECOMES ACTION",
            "rival_matrix": "TEST THE RIVAL EXPLANATION",
            "preservation_filter": "WHY SOME EVIDENCE SURVIVES",
            "consequence_chain": "FROM PRESSURE TO CONSEQUENCE",
            "great_zimbabwe_trade_evidence": "TRADE REACHED GREAT ZIMBABWE",
            "great_zimbabwe_bird_evidence": "THE SOAPSTONE BIRDS",
            "lachish_siege_section": "RAMP AGAINST COUNTER-RAMP",
            "lachish_fire_evidence": "WHAT THE BURNED TOWER PROVES",
            "lachish_source_triangle": "THREE RECORDS, THREE AGENDAS",
            "lachish_geography": "LACHISH IS NOT JERUSALEM",
            "lachish_two_layers": "TWO DESTRUCTIONS, OVER A CENTURY APART",
        }
        subject = self._short_title(topic.subject or topic.title).upper()[:54]
        draw.text((int(w * 0.07), int(h * 0.075)), subject, font=subject_font, fill=accent)
        title = titles.get(diagram_kind, "DOCUMENTARY EVIDENCE FRAMEWORK")
        title_lines = self._wrap_by_width(draw, title, title_font, int(w * 0.86), 2)
        title_y = int(h * 0.13)
        for title_line in title_lines:
            draw.text((int(w * 0.07), title_y), title_line, font=title_font, fill=pale)
            title_y += self._line_height(title_font)
        draw.line((int(w * 0.07), title_y + 18, int(w * 0.93), title_y + 18), fill=accent, width=4)

        left = int(w * 0.09)
        right = int(w * 0.91)
        top = max(int(h * 0.34), title_y + 60)
        bottom = int(h * 0.82)

        def card(
            box: tuple[int, int, int, int],
            heading: str,
            detail: str = "",
            body_lines: tuple[str, ...] = (),
        ) -> None:
            draw.rounded_rectangle(box, radius=24, fill=panel, outline=(95, 103, 105), width=3)
            heading_box = draw.textbbox((0, 0), heading, font=label_font)
            heading_x = box[0] + ((box[2] - box[0]) - (heading_box[2] - heading_box[0])) / 2
            draw.text((heading_x, box[1] + 34), heading, font=label_font, fill=pale)
            if body_lines:
                body_y = box[1] + 112
                body_width = max(120, box[2] - box[0] - 112)
                for body_line in body_lines:
                    wrapped = self._wrap_by_width(draw, body_line, small_font, body_width, 2)
                    draw.rounded_rectangle(
                        (box[0] + 38, body_y + 8, box[0] + 50, body_y + 20),
                        radius=4,
                        fill=accent,
                    )
                    for wrapped_line in wrapped:
                        draw.text((box[0] + 70, body_y), wrapped_line, font=small_font, fill=pale)
                        body_y += self._line_height(small_font) + 4
                    body_y += 18
            if detail:
                detail_box = draw.textbbox((0, 0), detail, font=small_font)
                detail_x = box[0] + ((box[2] - box[0]) - (detail_box[2] - detail_box[0])) / 2
                draw.text((detail_x, box[3] - 56), detail, font=small_font, fill=muted)

        if diagram_kind == "lachish_siege_section":
            ground_y = int(h * 0.76)
            wall_x = int(w * 0.58)
            wall_top = int(h * 0.37)
            draw.polygon(
                (
                    (left, ground_y),
                    (int(w * 0.31), int(h * 0.61)),
                    (wall_x - 18, int(h * 0.48)),
                    (wall_x - 18, ground_y),
                ),
                fill=(112, 84, 52),
                outline=accent,
            )
            draw.text((left + 30, ground_y - 62), "ASSYRIAN SIEGE RAMP", font=label_font, fill=pale)
            for block_y in range(wall_top, ground_y, 52):
                draw.rectangle((wall_x, block_y, wall_x + 92, block_y + 42), fill=(118, 103, 78), outline=(204, 190, 155), width=2)
            draw.text((wall_x - 18, wall_top - 54), "CITY WALL", font=label_font, fill=pale)
            draw.polygon(
                (
                    (wall_x + 92, ground_y),
                    (wall_x + 92, int(h * 0.5)),
                    (right - 60, ground_y),
                ),
                fill=(82, 72, 58),
                outline=(214, 194, 153),
            )
            draw.text((wall_x + 122, ground_y - 62), "JUDAHITE COUNTER-RAMP", font=label_font, fill=pale)
            for arrow_y in (int(h * 0.48), int(h * 0.55), int(h * 0.62)):
                draw.line((int(w * 0.24), arrow_y + 70, wall_x - 24, arrow_y), fill=(205, 84, 62), width=6)
                draw.polygon(((wall_x - 24, arrow_y), (wall_x - 55, arrow_y - 13), (wall_x - 51, arrow_y + 18)), fill=(205, 84, 62))
            note = "Attackers raised the approach outside while defenders reinforced the threatened wall from within."
            note_y = int(h * 0.83)
            for line_text in self._wrap_by_width(draw, note, small_font, int(w * 0.78), 2):
                note_box = draw.textbbox((0, 0), line_text, font=small_font)
                draw.text(((w - (note_box[2] - note_box[0])) / 2, note_y), line_text, font=small_font, fill=muted)
                note_y += self._line_height(small_font) + 3
        elif diagram_kind == "lachish_fire_evidence":
            gap = int(w * 0.025)
            card_width = int((right - left - 2 * gap) / 3)
            evidence = (
                ("MAGNETIC SIGNAL", "intense heating", ("Mudbrick minerals", "recorded the fire")),
                ("BEST-FIT DATE", "most likely 701 BCE", ("Iron Age burning", "matches Level III")),
                ("HARD LIMIT", "igniter unknown", ("Defenders or attackers", "remain possible")),
            )
            for index, (heading, detail, lines) in enumerate(evidence):
                x0 = left + index * (card_width + gap)
                card((x0, top + 25, x0 + card_width, bottom - 25), heading, detail, lines)
            draw.text((int(w * 0.31), bottom + 4), "DATE THE EVENT; DO NOT INVENT THE CULPRIT", font=label_font, fill=accent)
        elif diagram_kind == "lachish_source_triangle":
            gap = int(w * 0.022)
            card_width = int((right - left - 2 * gap) / 3)
            records = (
                ("ASSYRIAN TEXTS", "royal victory", ("Conquest", "Tribute", "Imperial audience")),
                ("HEBREW BIBLE", "Judah's memory", ("Invasion", "Jerusalem", "Religious meaning")),
                ("ARCHAEOLOGY", "physical trace", ("Siege ramp", "Weapons + fire", "Level III fall")),
            )
            for index, (heading, detail, lines) in enumerate(records):
                x0 = left + index * (card_width + gap)
                card((x0, top + 8, x0 + card_width, bottom - 50), heading, detail, lines)
            verdict = "CONVERGENCE: LACHISH WAS CAPTURED IN 701 BCE"
            verdict_box = draw.textbbox((0, 0), verdict, font=label_font)
            draw.text(((w - (verdict_box[2] - verdict_box[0])) / 2, bottom), verdict, font=label_font, fill=accent)
        elif diagram_kind == "lachish_geography":
            draw.rounded_rectangle((left, top, right, bottom), radius=34, fill=(31, 38, 38), outline=(95, 111, 105), width=3)
            draw.rectangle((left, top, int(w * 0.31), bottom), fill=(37, 77, 93))
            draw.text((left + 42, top + 32), "COASTAL PLAIN", font=label_font, fill=pale)
            draw.text((int(w * 0.38), top + 32), "SHEPHELAH", font=label_font, fill=accent)
            draw.text((int(w * 0.7), top + 32), "HILL COUNTRY", font=label_font, fill=pale)
            lachish = (int(w * 0.45), int(h * 0.63))
            jerusalem = (int(w * 0.76), int(h * 0.48))
            draw.line((lachish[0], lachish[1], jerusalem[0], jerusalem[1]), fill=accent, width=8)
            for point, label in ((lachish, "LACHISH"), (jerusalem, "JERUSALEM")):
                draw.ellipse((point[0] - 23, point[1] - 23, point[0] + 23, point[1] + 23), fill=pale, outline=accent, width=5)
                draw.text((point[0] - 62, point[1] + 40), label, font=label_font, fill=pale)
            draw.text((int(w * 0.55), int(h * 0.49)), "ABOUT 50 KM", font=label_font, fill=accent)
            draw.text((int(w * 0.33), int(h * 0.72)), "FIRM EVIDENCE OF CAPTURE", font=small_font, fill=pale)
            draw.text((int(w * 0.67), int(h * 0.72)), "SEPARATE, DISPUTED ENDING", font=small_font, fill=pale)
        elif diagram_kind == "lachish_two_layers":
            axis_y = int(h * 0.59)
            start_x = int(w * 0.16)
            mid_x = int(w * 0.5)
            end_x = int(w * 0.84)
            draw.line((start_x, axis_y, end_x, axis_y), fill=accent, width=9)
            draw.polygon(((end_x, axis_y), (end_x - 38, axis_y - 24), (end_x - 38, axis_y + 24)), fill=accent)
            milestones = (
                (start_x, "701 BCE", "LEVEL III", "Assyrian destruction"),
                (mid_x, "REBUILT", "MORE THAN A CENTURY", "later city above"),
                (end_x, "587/586 BCE", "LEVEL II", "Babylonian destruction"),
            )
            for x, date_label, layer, detail in milestones:
                draw.ellipse((x - 22, axis_y - 22, x + 22, axis_y + 22), fill=pale, outline=accent, width=4)
                for offset, label, font, color in ((-142, date_label, label_font, pale), (-94, layer, small_font, accent), (54, detail, small_font, muted)):
                    label_box = draw.textbbox((0, 0), label, font=font)
                    draw.text((x - (label_box[2] - label_box[0]) / 2, axis_y + offset), label, font=font, fill=color)
            letters = "LACHISH LETTERS → THE LATER BABYLONIAN CRISIS"
            letters_box = draw.textbbox((0, 0), letters, font=label_font)
            draw.text(((w - (letters_box[2] - letters_box[0])) / 2, int(h * 0.79)), letters, font=label_font, fill=accent)
        elif diagram_kind == "chronology_timeline":
            labels = ("SOURCE", "EVENT", "RESPONSE", "RESULT", "MEMORY")
            axis_y = int((top + bottom) * 0.55)
            draw.line((left, axis_y, right, axis_y), fill=accent, width=8)
            step = (right - left) / (len(labels) - 1)
            for index, label in enumerate(labels):
                x = int(left + index * step)
                draw.ellipse((x - 18, axis_y - 18, x + 18, axis_y + 18), fill=accent, outline=pale, width=3)
                label_box = draw.textbbox((0, 0), label, font=label_font)
                draw.text((x - (label_box[2] - label_box[0]) / 2, axis_y + 44), label, font=label_font, fill=pale)
        elif diagram_kind in {"terrain_map", "network_routes"}:
            for offset in range(6):
                inset = 24 * offset
                draw.arc((left + inset, top + inset, right - inset, bottom - inset), 205, 515, fill=(90, 108, 102), width=3)
            route = ((left + 70, bottom - 70), (int(w * 0.35), int(h * 0.62)), (int(w * 0.55), int(h * 0.67)), (right - 90, top + 70))
            draw.line(route, fill=accent, width=9, joint="curve")
            for x, y in route:
                draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=pale, outline=accent, width=4)
            route_labels = (
                (("ORIGIN", left + 50, top + 25), ("TRANSFER", int(w * 0.44), bottom - 55), ("DESTINATION", right - 310, top + 20))
                if diagram_kind == "network_routes"
                else (("ACCESS", left + 50, top + 25), ("DISTANCE", int(w * 0.46), bottom - 55), ("TERRAIN", right - 250, top + 20))
            )
            for label, x, y in route_labels:
                draw.text((x, y), label, font=label_font, fill=pale)
        elif diagram_kind in {"evidence_board", "artifact_context"}:
            labels = (("OBJECT", "what survives"), ("CONTEXT", "where it belonged"), ("DATE", "when it fits"))
            gap = int(w * 0.025)
            card_width = int((right - left - 2 * gap) / 3)
            for index, (heading, detail) in enumerate(labels):
                x0 = left + index * (card_width + gap)
                card((x0, top + 40, x0 + card_width, bottom - 50), heading, detail)
            draw.text((int(w * 0.42), bottom - 18), "TOGETHER -> A TESTABLE CLAIM", font=label_font, fill=accent)
        elif diagram_kind == "great_zimbabwe_trade_evidence":
            route_y = int(h * 0.47)
            route_left = int(w * 0.15)
            route_right = int(w * 0.85)
            draw.line((route_left, route_y, route_right, route_y), fill=accent, width=10)
            draw.polygon(
                (
                    (route_right, route_y),
                    (route_right - 38, route_y - 24),
                    (route_right - 38, route_y + 24),
                ),
                fill=accent,
            )
            for x, heading, detail in (
                (route_left, "INDIAN OCEAN", "wider exchange"),
                (int(w * 0.5), "TRADE ROUTES", "goods moved inland"),
                (route_right, "STONE CITY", "local power"),
            ):
                draw.ellipse((x - 18, route_y - 18, x + 18, route_y + 18), fill=pale, outline=accent, width=4)
                heading_box = draw.textbbox((0, 0), heading, font=small_font)
                draw.text((x - (heading_box[2] - heading_box[0]) / 2, route_y - 86), heading, font=small_font, fill=pale)
                detail_box = draw.textbbox((0, 0), detail, font=small_font)
                draw.text((x - (detail_box[2] - detail_box[0]) / 2, route_y + 42), detail, font=small_font, fill=muted)
            card_top = int(h * 0.62)
            card_bottom = int(h * 0.76)
            gap = int(w * 0.025)
            card_width = int((right - left - 2 * gap) / 3)
            for index, (heading, detail) in enumerate(
                (("GLASS BEADS", "imported"), ("CERAMICS", "excavated"), ("GOLD + CATTLE", "local wealth"))
            ):
                x0 = left + index * (card_width + gap)
                card((x0, card_top, x0 + card_width, card_bottom), heading, detail)
        elif diagram_kind == "great_zimbabwe_bird_evidence":
            center_x = int(w * 0.5)
            archive_url = "https://upload.wikimedia.org/wikipedia/commons/c/cb/Soapstone_birds_on_pedestals.jpg"
            cached_plate = self._ancient_archive_image_cache.get(archive_url)
            archive_plate: Image.Image | None = cached_plate.copy() if cached_plate is not None else None
            if archive_plate is None:
                try:
                    response = self.session.get(archive_url, timeout=12)
                    response.raise_for_status()
                    archive_plate = Image.open(io.BytesIO(response.content)).convert("RGB")
                    self._ancient_archive_image_cache[archive_url] = archive_plate.copy()
                except Exception:
                    archive_plate = None
            plate_box = (
                int(w * 0.08),
                int(h * 0.31),
                int(w * 0.92),
                int(h * 0.64),
            )
            if archive_plate is not None:
                self._ancient_diagram_archive_used.add(diagram_kind)
                plate = ImageOps.fit(
                    archive_plate,
                    (plate_box[2] - plate_box[0], plate_box[3] - plate_box[1]),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                canvas.paste(plate, (plate_box[0], plate_box[1]))
                draw = ImageDraw.Draw(canvas)
                draw.rounded_rectangle(plate_box, radius=20, outline=accent, width=5)
                plate_label = "PUBLIC-DOMAIN ARCHIVE PLATE"
                plate_label_box = draw.textbbox((0, 0), plate_label, font=small_font)
                draw.rounded_rectangle(
                    (
                        center_x - (plate_label_box[2] - plate_label_box[0]) / 2 - 20,
                        plate_box[3] - 58,
                        center_x + (plate_label_box[2] - plate_label_box[0]) / 2 + 20,
                        plate_box[3] - 12,
                    ),
                    radius=12,
                    fill=(10, 14, 17),
                )
                draw.text(
                    (center_x - (plate_label_box[2] - plate_label_box[0]) / 2, plate_box[3] - 52),
                    plate_label,
                    font=small_font,
                    fill=pale,
                )
            else:
                bird_y = int(h * 0.5)
                stone = (150, 143, 126)
                draw.rounded_rectangle(
                    (center_x - 100, bird_y - 205, center_x + 85, bird_y + 120),
                    radius=54,
                    fill=stone,
                    outline=pale,
                    width=5,
                )
                draw.ellipse(
                    (center_x - 15, bird_y - 300, center_x + 150, bird_y - 140),
                    fill=stone,
                    outline=pale,
                    width=5,
                )
                draw.polygon(
                    (
                        (center_x + 145, bird_y - 235),
                        (center_x + 245, bird_y - 195),
                        (center_x + 145, bird_y - 160),
                    ),
                    fill=accent,
                )
                draw.rectangle(
                    (center_x - 62, bird_y + 120, center_x + 48, bird_y + 250),
                    fill=(116, 109, 96),
                    outline=pale,
                    width=5,
                )
            label_y = int(h * 0.69)
            labels = (
                ("SOAPSTONE", "carved birds"),
                ("SITE FIND", "Great Zimbabwe"),
                ("EMBLEM", "modern symbol"),
            )
            gap = int(w * 0.02)
            card_width = int((right - left - 2 * gap) / 3)
            for index, (heading, detail) in enumerate(labels):
                x0 = left + index * (card_width + gap)
                card((x0, label_y, x0 + card_width, label_y + int(h * 0.12)), heading, detail)
        elif diagram_kind == "source_comparison":
            mid = int(w * 0.5)
            card((left, top + 35, mid - 35, bottom - 35), "WRITTEN CLAIM", "author | audience | purpose")
            card((mid + 35, top + 35, right, bottom - 35), "PHYSICAL TRACE", "object | layer | date")
            draw.rounded_rectangle((mid - 92, int(h * 0.56), mid + 92, int(h * 0.65)), radius=25, fill=accent)
            compare = "COMPARE"
            compare_box = draw.textbbox((0, 0), compare, font=label_font)
            draw.text((mid - (compare_box[2] - compare_box[0]) / 2, int(h * 0.578)), compare, font=label_font, fill=(18, 20, 21))
        elif diagram_kind in {"logistics_flow", "authority_stack"}:
            input_labels = ("ORDER", "RESOURCES", "ACCESS") if diagram_kind == "authority_stack" else ("LABOR", "MATERIAL", "SUPPLY")
            inputs = tuple((label, top + 25 + index * 120) for index, label in enumerate(input_labels))
            center_x = int(w * 0.62)
            for heading, y in inputs:
                card((left, y, int(w * 0.36), y + 92), heading)
                draw.line((int(w * 0.36), y + 46, center_x - 170, int((top + bottom) / 2)), fill=line, width=6)
            output_heading = "REPEATED CONTROL" if diagram_kind == "authority_stack" else "SUSTAINED ACTION"
            output_detail = "authority made observable" if diagram_kind == "authority_stack" else "coordination over time"
            card((center_x - 150, int(h * 0.48), right, int(h * 0.68)), output_heading, output_detail)
        elif diagram_kind == "ordinary_life_grid":
            labels = (("HOME", "shelter + family"), ("WORK", "labor + tools"), ("FOOD", "supply + survival"), ("MOVEMENT", "roads + distance"))
            gap = 28
            card_width = int((right - left - gap) / 2)
            card_height = int((bottom - top - gap) / 2)
            for index, (heading, detail) in enumerate(labels):
                row = index // 2
                col = index % 2
                x0 = left + col * (card_width + gap)
                y0 = top + row * (card_height + gap)
                card((x0, y0, x0 + card_width, y0 + card_height), heading, detail)
        elif diagram_kind == "rival_matrix":
            identity = f"{topic.subject} {topic.title}".lower()
            rival_profiles = (
                (
                    ("carthage",),
                    (
                        "ROMAN MEMORY",
                        ("Renewed prosperity meant threat", "Destruction framed as necessary", "Victory shaped the written record"),
                        "MATERIAL RECORD",
                        ("Trade and settlement continued", "Archaeology tests exaggeration", "Uncertainty remains visible"),
                    ),
                ),
                (
                    ("indus", "mohenjo", "harappa"),
                    (
                        "SUDDEN INVASION",
                        ("One dramatic external cause", "Expected destruction layers", "Older popular explanation"),
                        "REGIONAL CHANGE",
                        ("River and climate pressure", "Trade networks shifted", "Cities changed at different rates"),
                    ),
                ),
                (
                    ("justinian", "plague"),
                    (
                        "PLAGUE FIRST",
                        ("Disease drove the disruption", "Mortality changed labor and tax", "Written witnesses stress shock"),
                        "MULTIPLE PRESSURES",
                        ("War and climate also mattered", "Effects varied by region", "DNA anchors only part of the story"),
                    ),
                ),
                (
                    ("bronze age",),
                    (
                        "SINGLE INVASION",
                        ("One attacker caused collapse", "Destruction should align", "A simple dramatic sequence"),
                        "SYSTEMS COLLAPSE",
                        ("Trade and supply failed together", "Regions fell at different times", "Several pressures reinforced each other"),
                    ),
                ),
                (
                    ("delphi", "pythia"),
                    (
                        "DIVINE PROPHECY",
                        ("Answers came from Apollo", "Later stories preserve certainty", "Authority rested on revelation"),
                        "RITUAL AND POLITICS",
                        ("Priests shaped consultation", "Ambiguity protected authority", "Institutions managed influence"),
                    ),
                ),
            )
            profile = (
                "LITERARY ACCOUNT",
                ("What later writers claimed", "Who benefited from the story", "Which details appeared later"),
                "MATERIAL RECORD",
                ("What objects and layers predict", "Where dates converge or conflict", "Which gaps remain unresolved"),
            )
            for markers, candidate_profile in rival_profiles:
                if any(marker in identity for marker in markers):
                    profile = candidate_profile
                    break
            left_heading, left_lines, right_heading, right_lines = profile
            card(
                (left, top + 30, int(w * 0.46), bottom - 30),
                left_heading,
                "TEST: dates | motive | source",
                left_lines,
            )
            card(
                (int(w * 0.54), top + 30, right, bottom - 30),
                right_heading,
                "TEST: objects | layers | pattern",
                right_lines,
            )
            question = "WHICH EXPLAINS MORE WITH FEWER ASSUMPTIONS?"
            question_box = draw.textbbox((0, 0), question, font=small_font)
            draw.text(((w - (question_box[2] - question_box[0])) / 2, bottom + 5), question, font=small_font, fill=accent)
        elif diagram_kind == "preservation_filter":
            draw.polygon(((left, top + 20), (right, top + 20), (int(w * 0.63), bottom - 20), (int(w * 0.37), bottom - 20)), fill=panel, outline=accent)
            for index, label in enumerate(("PAST ACTIVITY", "DECAY + REUSE", "EXCAVATION", "WHAT SURVIVES")):
                y = top + 55 + index * int((bottom - top - 120) / 3)
                label_box = draw.textbbox((0, 0), label, font=label_font)
                draw.text(((w - (label_box[2] - label_box[0])) / 2, y), label, font=label_font, fill=pale if index < 3 else accent)
        else:
            labels = ("PRESSURE", "CHOICE", "ACTION", "RESULT")
            gap = 28
            card_width = int((right - left - gap * 3) / 4)
            y0 = int(h * 0.49)
            y1 = int(h * 0.67)
            for index, label in enumerate(labels):
                x0 = left + index * (card_width + gap)
                card((x0, y0, x0 + card_width, y1), label)
                if index < len(labels) - 1:
                    arrow_x = x0 + card_width + 5
                    draw.line((arrow_x, int((y0 + y1) / 2), arrow_x + gap - 10, int((y0 + y1) / 2)), fill=accent, width=6)

        footer = "DOCUMENTARY EVIDENCE FRAMEWORK | NOT A SCALE RECONSTRUCTION"
        footer_box = draw.textbbox((0, 0), footer, font=small_font)
        draw.text(((w - (footer_box[2] - footer_box[0])) / 2, int(h * 0.91)), footer, font=small_font, fill=muted)
        canvas.save(out_path, format="JPEG", quality=94)
        return True

    def _generate_ancient_documentary_diagram(
        self,
        topic: TopicCandidate,
        out_path: Path,
        diagram_kind: str,
    ) -> bool:
        if diagram_kind not in {"machu_terrace_layers", "machu_water_flow"}:
            return self._generate_generic_ancient_documentary_diagram(topic, out_path, diagram_kind)
        w, h = self._scene_size(topic)
        canvas = Image.new("RGB", (w, h), (15, 22, 25))
        draw = ImageDraw.Draw(canvas)
        for y in range(h):
            ratio = y / max(1, h - 1)
            color = (
                int(18 + (48 * ratio)),
                int(27 + (30 * ratio)),
                int(30 + (18 * ratio)),
            )
            draw.line((0, y, w, y), fill=color)

        title_font = self._font(66 if w < h else 54)
        label_font = self._font(39 if w < h else 32)
        small_font = self._font(27 if w < h else 22)
        accent = (236, 188, 92)
        water = (70, 183, 224)
        muted = (220, 224, 218)

        title = "HOW THE TERRACES DRAINED" if diagram_kind == "machu_terrace_layers" else "HOW WATER LEFT THE CITY"
        title_lines = self._wrap_by_width(draw, title, title_font, int(w * 0.82), 2)
        title_y = int(h * 0.09)
        for line in title_lines:
            box = draw.textbbox((0, 0), line, font=title_font)
            draw.text(((w - (box[2] - box[0])) / 2, title_y), line, font=title_font, fill=(248, 244, 230))
            title_y += self._line_height(title_font)
        subtitle = "MACHU PICCHU • DOCUMENTARY SCHEMATIC"
        subtitle_box = draw.textbbox((0, 0), subtitle, font=small_font)
        draw.text(((w - (subtitle_box[2] - subtitle_box[0])) / 2, title_y + 16), subtitle, font=small_font, fill=accent)

        left = int(w * 0.11)
        right = int(w * 0.9)
        base_y = int(h * 0.79)
        step_h = int(h * 0.115)
        step_w = int(w * 0.16)
        terraces = []
        for step in range(4):
            x0 = left + (step * step_w)
            y0 = base_y - (step * step_h)
            x1 = right
            y1 = y0 + step_h
            terraces.append((x0, y0, x1, y1))
            draw.rectangle((x0, y0, x1, y1), fill=(86, 72, 49), outline=(212, 184, 126), width=4)
            draw.rectangle((x0, y0, x1, y0 + int(step_h * 0.2)), fill=(86, 124, 70))
            draw.rectangle((x0, y0 + int(step_h * 0.2), x1, y0 + int(step_h * 0.52)), fill=(121, 96, 58))
            draw.rectangle((x0, y0 + int(step_h * 0.52), x1, y1), fill=(95, 103, 101))
            for rock_x in range(x0 + 18, x1 - 12, 48):
                rock_y = y0 + int(step_h * 0.68) + ((rock_x // 48) % 2) * 18
                draw.ellipse((rock_x, rock_y, rock_x + 26, rock_y + 18), fill=(143, 145, 134))

        if diagram_kind == "machu_terrace_layers":
            labels = (
                ("SOIL", (86, 124, 70), int(h * 0.49)),
                ("GRAVEL", (121, 96, 58), int(h * 0.56)),
                ("STONE FILL", (143, 145, 134), int(h * 0.64)),
            )
            for label, color, y in labels:
                draw.rounded_rectangle((int(w * 0.08), y - 12, int(w * 0.36), y + 62), radius=18, fill=(8, 13, 16), outline=color, width=3)
                draw.text((int(w * 0.11), y), label, font=label_font, fill=muted)
            for arrow_x in (int(w * 0.55), int(w * 0.7), int(w * 0.83)):
                draw.line((arrow_x, int(h * 0.43), arrow_x, int(h * 0.72)), fill=water, width=9)
                draw.polygon(
                    ((arrow_x - 18, int(h * 0.7)), (arrow_x + 18, int(h * 0.7)), (arrow_x, int(h * 0.735))),
                    fill=water,
                )
            note = "Water filtered downward instead of building pressure behind the retaining walls."
        else:
            canal_y = terraces[-1][1] + int(step_h * 0.12)
            draw.line((terraces[-1][0] + 16, canal_y, right - 20, canal_y), fill=water, width=16)
            draw.polygon(((right - 22, canal_y - 26), (right + 20, canal_y), (right - 22, canal_y + 26)), fill=water)
            for step, terrace in enumerate(reversed(terraces)):
                x0, y0, _, y1 = terrace
                arrow_x = x0 + int(step_w * 0.52)
                draw.line((arrow_x, y0 + 18, arrow_x, y1 - 18), fill=water, width=8)
                draw.polygon(((arrow_x - 16, y1 - 42), (arrow_x + 16, y1 - 42), (arrow_x, y1 - 10)), fill=water)
            draw.rounded_rectangle((int(w * 0.08), int(h * 0.45), int(w * 0.38), int(h * 0.515)), radius=18, fill=(8, 13, 16), outline=water, width=3)
            draw.text((int(w * 0.11), int(h * 0.462)), "STONE CANAL", font=label_font, fill=muted)
            note = "Canals moved surface runoff away before it could erode the engineered slope."

        note_lines = self._wrap_by_width(draw, note, label_font, int(w * 0.82), 3)
        note_y = int(h * 0.84)
        for line in note_lines:
            box = draw.textbbox((0, 0), line, font=label_font)
            draw.text(((w - (box[2] - box[0])) / 2, note_y), line, font=label_font, fill=(245, 242, 228))
            note_y += self._line_height(label_font)
        footer = "Simplified cross-section • not to scale"
        footer_box = draw.textbbox((0, 0), footer, font=small_font)
        draw.text(((w - (footer_box[2] - footer_box[0])) / 2, int(h * 0.955)), footer, font=small_font, fill=(177, 183, 180))
        canvas.save(out_path, format="JPEG", quality=94)
        return True

    def _ancient_diagram_source_metadata(self, diagram_kind: str) -> Dict[str, str]:
        lachish_sources = {
            "lachish_siege_section": (
                "https://onlinelibrary.wiley.com/doi/10.1111/ojoa.12231",
                "Oxford Journal of Archaeology: construction of the Assyrian siege ramp at Lachish",
            ),
            "lachish_fire_evidence": (
                "https://pmc.ncbi.nlm.nih.gov/articles/PMC11090153/",
                "PLOS ONE/PMC: archaeomagnetic investigation of the burnt mudbrick tower at Lachish",
            ),
            "lachish_source_triangle": (
                "https://www.metmuseum.org/exhibitions/listings/2014/assyria-to-iberia/blog/posts/sennacherib-and-jerusalem",
                "Metropolitan Museum of Art: Sennacherib, Lachish, and Jerusalem",
            ),
            "lachish_geography": (
                "https://www.britishmuseum.org/collection/galleries/assyria-lion-hunts",
                "British Museum: Lachish and Sennacherib's palace reliefs",
            ),
            "lachish_two_layers": (
                "https://www.southern.edu/administration/archaeology/lachish/project-overview/history-of-research.html",
                "Southern Adventist University Lachish excavation: Level III (701 BCE), Level II and the Lachish Letters (586 BCE)",
            ),
        }
        if diagram_kind in lachish_sources:
            source_page, source_title = lachish_sources[diagram_kind]
            return {
                "supporting_source_page": source_page,
                "supporting_source_title": source_title,
                "diagram_scope_note": "Simplified editorial diagram; not a scale reconstruction",
            }
        if (
            diagram_kind == "great_zimbabwe_bird_evidence"
            and diagram_kind in self._ancient_diagram_archive_used
        ):
            return {
                "supporting_archive_url": "https://upload.wikimedia.org/wikipedia/commons/c/cb/Soapstone_birds_on_pedestals.jpg",
                "supporting_source_page": "https://commons.wikimedia.org/wiki/File:Soapstone_birds_on_pedestals.jpg",
                "supporting_archive_title": "Soapstone birds on pedestals",
                "supporting_archive_license": "Public domain",
                "supporting_archive_artist": "James Theodore Bent",
            }
        return {}

    def _stable_horde_generate(self, prompt: str, out_path: Path, deadline: float | None = None) -> bool:
        if not self.stable_horde_key:
            return False
            
        import time
        url = "https://stablehorde.net/api/v2/generate/async"
        headers = {
            "apikey": self.stable_horde_key,
            "Content-Type": "application/json",
            "Client-Agent": "yt-automation:1.0:unknown"
        }
        # Width 512, Height 768 is a standard vertical aspect ratio for SD models
        payload = {
            "prompt": prompt,
            "params": {"n": 1, "width": 512, "height": 768},
            "nsfw": False,
            "censor_nsfw": True,
            "models": ["stable_diffusion"]
        }
        try:
            timeout = self._deadline_timeout(deadline, 15.0)
            if timeout is None:
                return False
            res = self.session.post(url, json=payload, headers=headers, timeout=timeout)
            if res.status_code != 202:
                return False
            job_id = res.json().get("id")
            if not job_id:
                return False
                
            for _ in range(40): # Also bounded by the active scene deadline.
                pause = self._deadline_timeout(deadline, 3.0)
                if pause is None:
                    return False
                time.sleep(min(3.0, pause))
                timeout = self._deadline_timeout(deadline, 10.0)
                if timeout is None:
                    return False
                check = self.session.get(f"https://stablehorde.net/api/v2/generate/status/{job_id}", headers=headers, timeout=timeout)
                if check.status_code == 200:
                    data = check.json()
                    if data.get("done") and data.get("generations"):
                        img_url = data["generations"][0].get("img")
                        if img_url:
                            return self._download(img_url, out_path, deadline=deadline)
                    elif data.get("faulted"):
                        return False
        except Exception as e:
            print(f"STABLE HORDE ERROR: {e}")
            return False
        return False

    def _font(self, size: int):
        for candidate in (
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/georgiab.ttf",
        ):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _fit_cover(self, source: Image.Image, size: tuple[int, int], focus: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
        return ImageOps.fit(source, size, method=Image.Resampling.LANCZOS, centering=focus)

    def _open_rgb(self, path: Path, size: tuple[int, int], focus=(0.5, 0.5)) -> Image.Image:
        img = Image.open(path).convert("RGB")
        return self._fit_cover(img, size, focus=focus)

    def _gradient_bg(self, size: tuple[int, int], index: int) -> Image.Image:
        palettes = [
            ((15, 23, 42), (95, 23, 62)),
            ((22, 28, 45), (92, 52, 18)),
            ((10, 42, 52), (38, 70, 83)),
            ((23, 24, 32), (74, 45, 66)),
        ]
        start, end = palettes[index % len(palettes)]
        w, h = size
        img = Image.new("RGB", size, start)
        draw = ImageDraw.Draw(img)
        for y in range(h):
            ratio = y / max(1, h - 1)
            color = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
            draw.line((0, y, w, y), fill=color)
        return img

    def _short_title(self, title: str) -> str:
        out = (title or "").strip()
        for prefix in ("The bizarre true story of ", "The real story behind "):
            if out.lower().startswith(prefix.lower()):
                out = out[len(prefix):]
        return out.strip() or title

    def _clean_caption(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip(" ,;:-")
        text = text.replace("...", "")
        text = re.sub(r"\s*[\u2013\u2014]\s*", ", ", text)
        text = re.sub(r"\s+-\s+", ", ", text)
        text = re.sub(r"\s+,", ",", text)
        text = re.sub(r"^(Number \d+:|First,|Then,|Finally,|And the wildest part is,)\s*", "", text, flags=re.IGNORECASE)
        if not text:
            return ""
        first_clause = re.split(r"[;:]", text, maxsplit=1)[0].strip()
        if "," in first_clause and len(first_clause) > 110:
            first_clause = first_clause.split(",", 1)[0].strip()
        words = first_clause.split()
        if len(words) > 14:
            first_clause = " ".join(words[:14]).strip(" ,;:-")
        first_clause = re.sub(r"(?:\b(?:to|and|of|the|a|an|with|for|in|on|at|from)\b[ ,;:-]*)+$", "", first_clause, flags=re.IGNORECASE).strip(" ,;:-")
        return first_clause

    def _scene_texts(self, topic: TopicCandidate, count: int) -> List[str]:
        title = self._short_title(topic.title)
        if topic.scene_plan:
            texts = [
                self._clean_caption(scene.visual_text or scene.narration) or title
                for scene in topic.scene_plan
            ]
            if len(texts) >= count:
                return texts[:count]
            base_texts = list(texts)
            while len(texts) < count:
                texts.append(base_texts[len(texts) % len(base_texts)] if base_texts else title)
            return texts

        cleaned: List[str] = []
        seen = set()
        beat_candidates = topic.narration_beats[1:-1] if len(topic.narration_beats) >= 3 else topic.narration_beats
        for text in beat_candidates + topic.visual_captions:
            caption = self._clean_caption(text)
            if not caption:
                continue
            key = caption.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(caption)

        if not cleaned:
            cleaned = [title]

        texts = [title]
        idx = 0
        while len(texts) < count:
            texts.append(cleaned[idx % len(cleaned)])
            idx += 1
        return texts[:count]

    def _keywords_from_text(self, text: str) -> List[str]:
        proper_phrases = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", text or "")
        if proper_phrases:
            return list(dict.fromkeys([phrase.strip() for phrase in proper_phrases if phrase.strip()]))[:3]

        stopwords = {
            "this", "that", "with", "from", "into", "after", "before", "while", "because", "their", "they",
            "there", "were", "was", "have", "had", "been", "being", "about", "which", "would", "could",
            "story", "real", "true", "more", "follow", "surprising", "operation", "history", "event",
        }
        tokens = re.findall(r"[A-Za-z][A-Za-z'-]+", text or "")
        out = []
        for token in tokens:
            low = token.lower()
            if low in stopwords or len(low) <= 4:
                continue
            out.append(token)
        return list(dict.fromkeys(out))[:4]

    def _scene_query_candidates(self, title: str, text: str) -> List[str]:
        subject = self._short_title(title)
        cleaned_text = self._clean_caption(text)
        keywords = self._keywords_from_text(text)

        queries = [subject]
        if keywords:
            queries.append(f"{subject} {' '.join(keywords[:3])}")
            queries.append(' '.join(keywords[:3]))
        if cleaned_text and cleaned_text.lower() != subject.lower():
            queries.append(f"{subject} {cleaned_text}")
            queries.append(cleaned_text)

        ordered = []
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

    def _ancient_scene_priority_queries(self, subject: str, text: str) -> list[str]:
        identity = f"{subject} {text}".lower()
        priorities: list[str] = []
        if "lachish" in identity:
            if any(term in identity for term in ("propaganda meets", "victory recut", "relief moves", "not a photograph", "ground cross-examines")):
                return ["Lachish relief British Museum full panels Sennacherib"]
            if any(term in identity for term in ("why lachish mattered", "regional strongpoint", "shephelah")):
                return ["Tel Lachish archaeological site aerial mound"]
            if any(term in identity for term in ("southwest attack", "attack point", "battering ram", "battering rams", "siege engine")):
                return ["Lachish relief siege engines ramp wall"]
            if any(term in identity for term in ("building the siege ramp", "siege ramp archaeology")):
                return ["Lachish siege ramp archaeology southwest corner"]
            if any(term in identity for term in ("weapons under", "arrowhead", "arrowheads", "projectile")):
                return ["Lachish Assyrian arrowheads British Museum"]
            if any(term in identity for term in ("defenders fight", "archers", "slingers")):
                return ["Lachish relief Assyrian archers slingers defenders"]
            if any(term in identity for term in ("families become", "deportation", "captives", "prisoners")):
                return ["Lachish relief Judaean captives families deportation"]
            if any(term in identity for term in ("sennacherib stages", "king on throne", "royal throne")):
                return ["Sennacherib throne Lachish relief British Museum"]
            if any(term in identity for term in ("assyria comes", "campaign", "judah")):
                return ["Lachish relief Assyrian army Sennacherib"]
            if any(term in identity for term in ("what assyria changed", "aftermath")):
                return ["Lachish relief captives Sennacherib victory"]
            return ["Lachish relief British Museum Sennacherib"]
        if "tyre" in identity and ("alexander" in identity or "siege" in identity):
            if any(term in identity for term in ("causeway", "mole", "water", "island", "coast")):
                return ["Siege of Tyre causeway Alexander illustration"]
            if any(term in identity for term in ("tower", "artillery", "catapult", "wall", "siege engine")):
                return ["Siege of Tyre Alexander siege engines illustration"]
            if any(term in identity for term in ("fleet", "ship", "naval", "cyprus", "galley")):
                return ["Siege of Tyre naval action Alexander"]
            if any(term in identity for term in ("port", "trade", "phoenicia", "phoenician", "city")):
                return ["Ancient Tyre Lebanon archaeological site"]
            return ["Siege of Tyre 332 BC historical illustration"]
        if "angkor wat" in identity:
            if any(term in identity for term in ("bas-relief", "bas relief", "procession", "warfare", "epic")):
                return ["Angkor Wat sandstone bas relief Khmer"]
            if any(term in identity for term in ("moat", "tower", "mount meru", "gallery")):
                return ["Angkor Wat five towers moat causeway Cambodia"]
            if any(term in identity for term in ("suryavarman", "vishnu", "hindu")):
                return ["Angkor Wat Suryavarman II Vishnu bas relief"]
            if any(term in identity for term in ("buddhist", "worship", "monk")):
                return ["Angkor Wat Buddhist monks temple Cambodia"]
            return ["Angkor Wat temple architecture Cambodia"]
        if re.search(r"\btikal\b", identity):
            if any(term in identity for term in ("stela", "inscription", "dynast", "ruler", "war", "yax mutal")):
                return ["Tikal Maya stela inscription Guatemala"]
            if any(term in identity for term in ("reservoir", "causeway", "urban", "palace")):
                return ["Tikal reservoirs causeway archaeology Guatemala"]
            if any(term in identity for term in ("temple i", "jasaw", "funerary", "great plaza")):
                return ["Tikal Temple I Great Plaza Guatemala"]
            return ["Tikal Temple IV rainforest Guatemala"]
        if "chichen itza" in identity:
            if "ball court" in identity or "game" in identity:
                return ["Chichen Itza Great Ball Court Yucatan"]
            if any(term in identity for term in ("cenote", "sinkhole", "offering")):
                return ["Chichen Itza Sacred Cenote archaeology"]
            if any(term in identity for term in ("el castillo", "pyramid", "stairway")):
                return ["Chichen Itza El Castillo pyramid Yucatan"]
            return ["Chichen Itza Temple of Warriors Maya Yucatan"]
        if "great zimbabwe" in identity:
            if any(term in identity for term in ("soapstone", "bird", "symbol")):
                return ["Zimbabwe Bird soapstone sculpture Great Zimbabwe"]
            if any(term in identity for term in ("hill complex", "valley ruins", "settlement", "expanded")):
                return ["Great Zimbabwe Hill Complex Valley Ruins"]
            if any(term in identity for term in ("bead", "ceramic", "trade", "gold", "cattle", "indian ocean")):
                return ["Great Zimbabwe trade beads ceramics archaeology"]
            if any(term in identity for term in ("wall", "mortar", "enclosure", "dry-stone", "dry stone")):
                return ["Great Zimbabwe Great Enclosure dry stone walls"]
            return ["Great Zimbabwe archaeological ruins Zimbabwe"]
        if any(term in identity for term in ("axum", "aksum")):
            if any(term in identity for term in ("false door", "window", "palace", "carved")):
                return ["King Ezana Stele", "Aksum stela"]
            if any(term in identity for term in ("great stele", "fell", "shattered", "fragment")):
                return ["Obelisk of Aksum Remains", "King Remhai Stela Aksum"]
            if any(term in identity for term in ("italy", "rome", "1937", "return", "heritage")):
                return ["Rome Stele Aksum", "Obelisk Axum"]
            if any(term in identity for term in ("tomb", "underground", "northern field")):
                return ["Aksum Tomb of the False Door", "Aksum stela tomb"]
            if any(term in identity for term in ("quarr", "slab", "moved", "raising", "teamwork")):
                return ["Aksum Quarry Obelisks", "Aksum obelisk"]
            return ["Aksum stela", "Aksum obelisk"]
        if "olmec" in identity and any(term in identity for term in ("head", "colossal")):
            if any(term in identity for term in ("face", "headgear", "helmet", "portrait", "ruler", "individual")):
                return ["Olmec colossal head headdress ruler detail"]
            if any(term in identity for term in ("basalt", "tuxtla", "transport", "boulder", "labor")):
                return ["Olmec basalt colossal head Tuxtla Mountains"]
            if "tres zapotes" in identity:
                return ["Olmec colossal head Tres Zapotes Monument A"]
            if "la venta" in identity:
                return ["Olmec colossal head La Venta Monument 1"]
            if "san lorenzo" in identity:
                return ["Olmec colossal head San Lorenzo Monument 4"]
            if any(term in identity for term in ("seventeen", "known", "survive")):
                return ["Olmec colossal heads Xalapa museum"]
            return ["Olmec colossal head San Lorenzo La Venta"]
        if "carthage" in identity:
            if "hannibal crossed" in identity or "hannibal's campaign" in identity:
                priorities.append("Hannibal Second Punic War route map")
            elif "zama" in identity or "scipio africanus" in identity:
                priorities.append("Battle of Zama historical map")
            elif "first punic war" in identity:
                priorities.append("First Punic War Mediterranean map")
            elif "cothon" in identity or "military basin" in identity:
                priorities.append("Carthage cothon harbor reconstruction")
            elif "third punic war" in identity or "siege" in identity:
                priorities.append("Third Punic War siege of Carthage illustration")
        return priorities

    @staticmethod
    def _ancient_archive_seed_queries(subject: str) -> list[str]:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(subject or "").lower()).strip()
        if "axum" in normalized or "aksum" in normalized:
            return ["Aksum stela", "Aksum obelisk"]
        return [re.sub(r"\s+", " ", str(subject or "")).strip()] if normalized else []

    def _ancient_context_queries(self, subject: str, text: str) -> list[str]:
        """Return narrow, source-verifiable archive searches for sparse subjects."""
        identity = f"{subject} {text}".lower()
        if "lachish" in identity:
            if any(marker in identity for marker in ("why lachish mattered", "shephelah", "landscape", "routes")):
                return [
                    "Tel Lachish aerial archaeology",
                    "Lachish archaeological site Israel mound",
                ]
            if any(marker in identity for marker in ("ramp", "southwest", "wall", "siege engine", "battering")):
                return [
                    "Lachish siege ramp archaeology",
                    "Lachish relief siege engines wall",
                ]
            if any(marker in identity for marker in ("arrowhead", "weapon", "projectile", "sling")):
                return [
                    "Lachish arrowheads British Museum",
                    "Lachish relief archers slingers",
                ]
            if any(marker in identity for marker in ("captives", "families", "deportation", "prisoners")):
                return [
                    "Lachish relief Judaean captives",
                    "Lachish relief prisoners Sennacherib",
                ]
            if any(marker in identity for marker in ("throne", "king", "judgment")):
                return [
                    "Sennacherib throne Lachish relief",
                    "Lachish relief king prisoners British Museum",
                ]
            return [
                "Lachish relief British Museum",
                "Tel Lachish archaeology Israel",
            ]
        if "tyre" in identity and ("alexander" in identity or "siege" in identity):
            if any(marker in identity for marker in ("causeway", "mole", "water", "island", "coast")):
                return [
                    "Siege of Tyre causeway map",
                    "Ancient Tyre island Lebanon archaeology",
                ]
            if any(marker in identity for marker in ("port", "trade", "city", "phoenicia", "phoenician")):
                return [
                    "Tyre Lebanon Al Mina archaeological site",
                    "Tyre Lebanon ancient ruins archaeology",
                ]
            if any(marker in identity for marker in ("wall", "tower", "artillery", "catapult", "siege")):
                return [
                    "Siege of Tyre historical engraving",
                    "Alexander siege of Tyre illustration",
                ]
            if any(marker in identity for marker in ("fleet", "ship", "naval", "cyprus", "galley")):
                return [
                    "Siege of Tyre naval battle illustration",
                    "Phoenician ship relief museum",
                ]
            return [
                "Tyre Lebanon Al Bass archaeological site",
                "Siege of Tyre Alexander illustration",
            ]
        if any(marker in identity for marker in ("kingdom of kush", "kushite", "nubia", "meroe")):
            if any(marker in identity for marker in ("piye", "taharqa", "egypt", "pharaoh", "twenty-fifth", "25th")):
                return [
                    "Taharqa statue Kushite pharaoh museum",
                    "Piye victory stela Kush",
                ]
            if any(marker in identity for marker in ("pyramid", "burial", "tomb", "cemetery")):
                return [
                    "Nubian pyramids Meroe Sudan archaeology",
                    "Meroe royal cemetery pyramids",
                ]
            if any(marker in identity for marker in ("amun", "temple", "jebel barkal", "napata", "religion")):
                return [
                    "Jebel Barkal Temple of Amun Sudan",
                    "Jebel Barkal ruins archaeology",
                ]
            if any(marker in identity for marker in ("iron", "workshop", "craft", "production", "trade")):
                return [
                    "Meroe iron archaeology Sudan",
                    "Musawwarat es-Sufra Sudan archaeology",
                ]
            if any(marker in identity for marker in ("amanirenas", "kandake", "rome", "roman", "war")):
                return [
                    "Amanirenas Kandake Kush relief",
                    "Naqa Temple Sudan Kush archaeology",
                ]
            if any(marker in identity for marker in ("script", "inscription", "language", "writing")):
                return [
                    "Meroitic inscription museum",
                    "Meroitic script stela",
                ]
            if any(marker in identity for marker in ("kerma", "early", "origin", "city")):
                return [
                    "Western Deffufa Kerma Sudan",
                    "Kerma archaeological site Sudan",
                ]
            return [
                "Meroe pyramids Sudan archaeology",
                "Jebel Barkal ruins Sudan",
            ]
        if "stonehenge" in identity:
            if any(marker in identity for marker in ("sarsen", "trilithon", "lintel", "joint", "stonework")):
                return [
                    "Stonehenge sarsen trilithon lintel detail",
                    "Stonehenge mortise tenon archaeology",
                ]
            if any(marker in identity for marker in ("bluestone", "wales", "preseli", "transport")):
                return [
                    "Stonehenge bluestones Preseli archaeology",
                    "Waun Mawn stone circle Wales archaeology",
                ]
            if any(marker in identity for marker in ("burial", "cremat", "bone", "aubrey")):
                return [
                    "Stonehenge Aubrey Holes excavation",
                    "Stonehenge archaeology excavation remains",
                ]
            if any(marker in identity for marker in ("durrington", "settlement", "feast", "house")):
                return [
                    "Durrington Walls excavation houses",
                    "Durrington Walls Stonehenge archaeology",
                ]
            if any(marker in identity for marker in ("solstice", "sun", "alignment", "avenue")):
                return [
                    "Stonehenge Avenue archaeology",
                    "Stonehenge solstice alignment aerial",
                ]
            return [
                "Stonehenge aerial archaeological site",
                "Stonehenge monument close view",
            ]
        if "sogdian" not in identity and "sogdia" not in identity:
            return []
        if any(marker in identity for marker in ("letter", "inscription", "language", "debt", "family")):
            return [
                "Sogdian Ancient Letters manuscript",
                "Sogdian inscriptions artifact",
            ]
        if any(marker in identity for marker in ("samarkand", "city", "religion", "luxury", "idea")):
            return [
                "Afrasiab Sogdian murals Samarkand",
                "Panjikent Sogdian murals",
            ]
        return [
            "Sogdian merchant figurine",
            "Sogdian Silk Road mural",
        ]

    @staticmethod
    def _ancient_fallback_diagram_kind(
        index: int,
        diagram_counts: dict[str, int],
    ) -> str:
        """Choose a distinct documentary framework when a real archive search fails.

        The opener deliberately has no generated fallback because long Ancient
        videos must begin on real historical evidence. Later scenes may use one
        of the evidence frameworks below, but each framework is used at most once
        so a network outage cannot silently turn the edit into repeated cards.
        """
        if index <= 1:
            return ""
        preferred_by_scene = {
            2: "chronology_timeline",
            3: "terrain_map",
            4: "ordinary_life_grid",
            5: "logistics_flow",
            6: "network_routes",
            7: "authority_stack",
            8: "evidence_board",
            9: "preservation_filter",
            10: "artifact_context",
            11: "source_comparison",
            12: "rival_matrix",
            13: "consequence_chain",
            14: "terrain_map",
            15: "preservation_filter",
            16: "logistics_flow",
            17: "rival_matrix",
            18: "consequence_chain",
            19: "network_routes",
            20: "evidence_board",
        }
        all_kinds = (
            "chronology_timeline",
            "terrain_map",
            "ordinary_life_grid",
            "logistics_flow",
            "network_routes",
            "authority_stack",
            "evidence_board",
            "preservation_filter",
            "artifact_context",
            "source_comparison",
            "rival_matrix",
            "consequence_chain",
        )
        preferred = preferred_by_scene.get(index, "")
        ordered = ([preferred] if preferred else []) + [
            kind for kind in all_kinds if kind != preferred
        ]
        return next(
            (kind for kind in ordered if int(diagram_counts.get(kind, 0)) < 1),
            "",
        )

    def _ancient_asset_matches_scene_period(self, asset: str, text: str) -> bool:
        lowered_asset = unquote(asset).lower()
        lowered_text = text.lower()
        punic_era_scene = any(
            marker in lowered_text
            for marker in (
                "hannibal", "zama", "punic war", "barcid", "scipio africanus",
                "mercenary war", "cothon", "carthaginian power",
            )
        )
        if punic_era_scene and any(
            marker in lowered_asset
            for marker in ("500ad", "500_ad", "vandal", "byzantine", "eastern_roman_empire")
        ):
            return False
        return True

    def _strict_ancient_terms(self, query: str, subject: str = "") -> list[str]:
        lowered = self._normalize_ancient_evidence_text(query, subject)
        visual_ready_aliases: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
            (
                ("lachish",),
                (
                    "lachish",
                    "lakhish",
                    "tel lachish",
                    "tel lakhish",
                    "tell ed duweir",
                ),
            ),
            (
                ("angkor wat",),
                (
                    "angkor wat",
                    "prasat angkor wat",
                ),
            ),
            (
                ("tikal",),
                (
                    "tikal",
                    "yax mutal",
                ),
            ),
            (
                ("chichen itza",),
                (
                    "chichen itza",
                    "temple of kukulcan",
                    "sacred cenote chichen",
                    "great ball court chichen",
                ),
            ),
            (
                ("great zimbabwe",),
                (
                    "great zimbabwe",
                    "conical tower great enclosure",
                    "great enclosure iii",
                    "g zimbabwe",
                    "masvingo zimbabwe",
                    "ruined cities of mashonaland",
                    "zimbabwe bird",
                ),
            ),
            (
                (
                    "axum obelisk",
                    "aksum obelisk",
                    "obelisk of axum",
                    "obelisk of aksum",
                    "axum stela",
                    "aksum stela",
                    "axum stele",
                    "aksum stele",
                ),
                (
                    "obelisk of axum",
                    "obelisk of aksum",
                    "axum obelisk",
                    "aksum obelisk",
                    "axum stela",
                    "aksum stela",
                    "axum stele",
                    "aksum stele",
                    "stelae park axum",
                    "stelae park aksum",
                    "stele park axum",
                    "stele park aksum",
                    "great stele axum",
                    "great stele aksum",
                    "king ezana s stele",
                    "aksum quarry for obelisks",
                    "axum quarry for obelisks",
                    "unfinished obelisk axum",
                    "unfinished obelisk aksum",
                ),
            ),
            (
                ("olmec colossal head",),
                (
                    "olmec colossal head",
                    "olmec basalt head",
                    "colossal head san lorenzo",
                    "colossal head la venta",
                    "colossal head tres zapotes",
                    "tres zapotes monument a",
                ),
            ),
        )
        for subject_markers, evidence_aliases in visual_ready_aliases:
            if any(marker in lowered for marker in subject_markers):
                return list(evidence_aliases)
        if "sogdian" in lowered or "sogdia" in lowered:
            return [
                "sogdian", "sogdia", "samarkand", "afrasiyab", "afrosiab",
                "panjikent", "penjikent",
            ]
        if "tyre" in lowered and ("alexander" in lowered or "siege" in lowered):
            return [
                "siege of tyre",
                "alexander siege tyre",
                "ancient tyre lebanon",
                "tyre lebanon archaeology",
                "tyre al bass",
                "tyre al mina",
                "tyrian phoenician",
            ]
        if "nubian" in lowered and "pyramid" in lowered:
            return [
                "nubian", "meroe", "meroë", "sudan", "kush", "napata",
                "nuri", "jebel barkal", "al burkel",
            ]
        if "nazca" in lowered or "nasca" in lowered:
            return ["nazca", "nasca", "peru", "geoglyph"]
        if "carthage" in lowered or "punic" in lowered:
            return ["carthage", "punic", "tunisia", "hannibal", "phoenician"]
        if "teutoburg" in lowered or "varus" in lowered:
            return ["teutoburg", "varus", "kalkriese", "germania", "roman"]
        if "thermopylae" in lowered:
            return ["thermopylae", "spartan", "persian", "greece", "hoplite"]
        if "greek fire" in lowered or ("byzantine" in lowered and "fire" in lowered):
            return ["greek fire", "byzantine", "constantinople", "siphon"]
        if "justinianic plague" in lowered or "plague of justinian" in lowered:
            return ["justinian", "plague", "yersinia", "procopius", "byzantine", "ravenna"]
        if "petra" in lowered or "nabataean" in lowered:
            return ["petra", "nabataean", "jordan"]
        if "dead sea scrolls" in lowered or "qumran" in lowered:
            return ["dead sea scrolls", "qumran", "judean", "manuscript"]
        if "antikythera" in lowered:
            return ["antikythera", "mechanism", "gear", "bronze"]
        if "hammurabi" in lowered:
            return ["hammurabi", "babylon", "stele", "law"]
        if "alexandria" in lowered:
            return ["alexandria", "library", "egypt", "hellenistic"]
        if any(term in lowered for term in ("indus valley", "mohenjo daro", "mohenjo-daro", "harappa")):
            return ["indus", "harappan", "mohenjo", "harappa", "dholavira", "lothal", "rakhigarhi"]
        if "cyrus cylinder" in lowered:
            return ["cyrus cylinder", "cyrus", "achaemenid", "babylon", "persian"]
        if "axum" in lowered or "aksum" in lowered:
            return ["axum", "aksum", "stela", "stele", "obelisk"]
        if "roman concrete" in lowered or "opus caementicium" in lowered:
            return ["roman concrete", "opus caementicium", "pozzolan", "pantheon", "roman harbor"]
        if "oracle of delphi" in lowered or "pythia" in lowered:
            return ["delphi", "pythia", "apollo", "oracle"]
        if "bronze age collapse" in lowered:
            return ["bronze age", "mycenaean", "hittite", "ugarit", "sea peoples"]
        normalized_subject = self._normalize_ancient_evidence_text(subject)
        if not normalized_subject:
            return []
        generic_tokens = {
            "ancient", "battle", "city", "code", "dynasty", "empire", "fall",
            "kingdom", "mausoleum", "mechanism", "siege", "temple", "tomb", "wall", "war",
            "archaeology", "archaeological", "civilization", "history", "site", "valley",
        }
        subject_tokens = [
            token
            for token in normalized_subject.split()
            if len(token) >= 4 and token not in generic_tokens
        ]
        terms = [normalized_subject]
        terms.extend(subject_tokens[-2:])
        return list(dict.fromkeys(term for term in terms if term))

    @staticmethod
    def _normalize_ancient_evidence_text(*values: object) -> str:
        decoded = unquote(" ".join(str(value or "") for value in values))
        ascii_text = "".join(
            character
            for character in unicodedata.normalize("NFKD", decoded)
            if not unicodedata.combining(character)
        )
        return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()

    @classmethod
    def _ancient_asset_evidence_text(cls, url: str, meta: Dict[str, str]) -> str:
        """Return independent subject evidence supplied by the asset provider.

        Search queries and ``verified_subject`` are intentionally excluded: both
        are labels created by this pipeline and cannot prove what an image shows.
        """
        return cls._normalize_ancient_evidence_text(
            url,
            meta.get("asset_title"),
            meta.get("description"),
            meta.get("source_page"),
        )

    def _candidate_matches_terms(self, url: str, meta: Dict[str, str], terms: list[str]) -> bool:
        if not terms:
            return True
        searchable = self._ancient_asset_evidence_text(url, meta)
        normalized_terms = [
            self._normalize_ancient_evidence_text(term)
            for term in terms
            if self._normalize_ancient_evidence_text(term)
        ]
        if any("siege of tyre" in term for term in normalized_terms):
            # "Tyre" is also the British spelling of tire. Require independent
            # archaeological/location evidence instead of accepting any stock
            # result containing that single ambiguous token.
            tyre_evidence = (
                "siege of tyre",
                "alexander siege tyre",
                "ancient tyre lebanon",
                "tyre lebanon",
                "tyre archaeological",
                "tyre archaeology",
                "tyre al bass",
                "tyre al mina",
                "tyrian phoenician",
                "tyre phoenician",
            )
            return any(marker in searchable for marker in tyre_evidence)
        if any("great zimbabwe" in term for term in normalized_terms) and any(
            marker in searchable
            for marker in (
                "great zimbabwe university",
                "peer educators",
                "university logo",
            )
        ):
            return False
        for normalized_term in normalized_terms:
            suffix = r"\w*" if " " not in normalized_term else ""
            pattern = r"\b" + re.escape(normalized_term).replace(r"\ ", r"\s+") + suffix + r"\b"
            if re.search(pattern, searchable):
                return True
        return False

    @classmethod
    def _ancient_asset_conflicts_with_subject(cls, subject: str, url: str, meta: Dict[str, str]) -> bool:
        normalized_subject = cls._normalize_ancient_evidence_text(subject)
        searchable = cls._ancient_asset_evidence_text(url, meta)
        conflict_markers: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
            (
                ("angkor wat",),
                (
                    "angkor thom", "bayon", "ta prohm", "banteay srei",
                    "preah khan", "phnom bakheng", "baphuon", "nipple gong",
                    "kse diev", "khmer house",
                ),
            ),
            (
                ("tikal",),
                (
                    "chichen itza", "palenque", "uxmal", "coba", "teotihuacan",
                    "calakmul", "copan", "yaxha", "el mirador",
                ),
            ),
            (
                ("chichen itza",),
                (
                    "palenque", "tikal", "uxmal", "coba", "teotihuacan",
                    "calakmul", "copan", "yaxha", "el mirador",
                ),
            ),
            (
                ("great zimbabwe",),
                (
                    "victoria falls", "great pyramid", "giza", "great wall of china",
                    "beijing", "masada", "great dyke", "jungle", "forest plants",
                    "possibly shona peoples", "shona figure", "great zimbabwe university",
                    "peer educators", "university logo",
                ),
            ),
            (
                ("axum", "aksum"),
                (
                    "luxor obelisk", "karnak", "hatshepsut", "washington monument",
                    "cleopatra s needle", "obelisk of theodosius", "theodosius obelisk",
                ),
            ),
            (
                ("olmec colossal head",),
                (
                    "moai", "easter island", "ramses", "pharaoh", "buddha",
                    "aztec", "maya", "toltec",
                ),
            ),
        )
        for subject_markers, excluded_assets in conflict_markers:
            if any(marker in normalized_subject for marker in subject_markers):
                return any(marker in searchable for marker in excluded_assets)
        return False

    def _ancient_short_verified_reuse_pool(
        self,
        topic: TopicCandidate,
        backgrounds: List[Path | None],
        manifest: List[Dict[str, str]],
        reuse_counts: dict[str, int],
    ) -> List[Path]:
        """Allow bounded reuse only after the Short already has eight strong assets.

        The Ancient Short preflight requires eight unique, licensed, subject-relevant
        archive visuals. Once that floor is met, reusing different assets once each is
        preferable to inventing local fact cards merely to fill a twelve-scene edit.
        """
        if topic.niche_id != "ancient_history" or topic.content_kind != "short":
            return []

        generated_sources = {
            "local_documentary_diagram",
            "local_fact_card",
            "pollinations_ai",
            "stable_horde",
        }
        relevance_terms = self._strict_ancient_terms(
            topic.title,
            subject=topic.subject,
        )
        verified_files: set[str] = set()
        unique_identities: set[str] = set()
        for item in manifest:
            source_type = str(item.get("source") or "").strip().lower()
            media_url = str(item.get("url") or "").strip()
            source_page = str(item.get("source_page") or "").strip()
            license_name = str(item.get("license") or "").strip().lower()
            file_name = str(item.get("file") or "").strip()
            if (
                not file_name
                or source_type in generated_sources
                or not media_url.startswith(("http://", "https://"))
                or not source_page.startswith(("http://", "https://"))
                or str(item.get("source_page_verified") or "").strip().lower() != "true"
                or license_name in {"", "unknown"}
                or not self._candidate_matches_terms(media_url, item, relevance_terms)
            ):
                continue
            identity = self._canonical_media_url(media_url)
            if not identity:
                continue
            unique_identities.add(identity)
            verified_files.add(file_name)

        if len(unique_identities) < self.ANCIENT_SHORT_MIN_UNIQUE_REAL_FOR_REUSE:
            return []

        reusable: List[Path] = []
        seen_files: set[str] = set()
        for background in backgrounds:
            if background is None or background.name in seen_files:
                continue
            if background.name not in verified_files or reuse_counts.get(background.name, 0) >= 1:
                continue
            seen_files.add(background.name)
            reusable.append(background)
        return reusable

    def _brain_short_verified_reuse_pool(
        self,
        scene_text: str,
        backgrounds: List[Path | None],
        manifest: List[Dict[str, str]],
        reuse_counts: dict[str, int],
    ) -> List[tuple[Path, Dict[str, str]]]:
        """Return scene-matching real footage before a generated fallback is considered.

        A Brain Lens Short must never accept a generated fact card.  Once two
        independently licensed real assets have been secured, one bounded
        repeat is more truthful and more watchable than failing the entire
        scheduled slot because a single later stock query timed out.
        """
        generated_sources = {
            "local_documentary_diagram",
            "local_fact_card",
            "pollinations_ai",
            "stable_horde",
        }
        verified_by_file: dict[str, Dict[str, str]] = {}
        unique_identities: set[str] = set()
        for item in manifest:
            source_type = str(item.get("source") or "").strip().lower()
            media_url = str(item.get("url") or "").strip()
            file_name = str(item.get("file") or "").strip()
            license_name = str(item.get("license") or "").strip().lower()
            if (
                not file_name
                or source_type in generated_sources
                or not media_url.startswith(("http://", "https://"))
                or str(item.get("source_page_verified") or "").strip().lower() != "true"
                or license_name in {"", "unknown"}
            ):
                continue
            identity = self._canonical_media_url(media_url)
            if not identity:
                continue
            unique_identities.add(identity)
            verified_by_file.setdefault(file_name, item)

        # Keep an early diversity floor, but do not wait for all four sources:
        # this method is invoked only after the current scene exhausted its
        # real-source queries, and a one-time reuse is preferable to a local
        # generated card that the Brain Lens upload gate must reject.
        early_reuse_floor = 2
        if len(unique_identities) < early_reuse_floor:
            return []

        reusable: List[tuple[Path, Dict[str, str]]] = []
        topic_relevant_fallbacks: List[tuple[Path, Dict[str, str]]] = []
        seen_files: set[str] = set()
        for background in backgrounds:
            if background is None or background.name in seen_files or reuse_counts.get(background.name, 0) >= 1:
                continue
            source_meta = verified_by_file.get(background.name)
            if source_meta is None:
                continue
            seen_files.add(background.name)
            candidate = (background, source_meta)
            topic_relevant_fallbacks.append(candidate)
            if self._asset_matches_scene_intent(
                "brain_lens",
                scene_text,
                str(source_meta.get("url") or ""),
                source_meta,
            ):
                reusable.append(candidate)
        # Exact scene semantics win. Once four unique real sources exist, a
        # different verified clip from the same researched topic is still more
        # trustworthy and visually coherent than an uncanny generated portrait.
        return reusable or topic_relevant_fallbacks

    @staticmethod
    def _is_brain_relationship_asset(url: str, meta: Dict[str, str]) -> bool:
        searchable = re.sub(
            r"[^a-z0-9]+",
            " ",
            f"{url} {meta.get('asset_title', '')} {meta.get('artist', '')}".lower(),
        ).strip()
        padded = f" {searchable} "
        return any(
            marker in padded
            for marker in (
                "couple", "relationship", "partner", "dating", " date ", "romantic",
                "kissing", " kiss ", "hug", "holding hands", "argument", "arguing",
                "breakup", "breaking up", "man and woman", "woman and man",
                "male and female", "lovers", "boyfriend", "girlfriend", "husband", "wife",
            )
        )

    def _repair_brain_short_relationship_backgrounds(
        self,
        topic: TopicCandidate,
        backgrounds: List[Path | None],
        manifest: List[Dict[str, str]],
        reuse_counts: dict[str, int],
    ) -> None:
        if topic.niche_id != "brain_lens" or topic.content_kind != "short" or not manifest:
            return
        topic_blob = f"{topic.subject} {topic.title} {topic.narration}".lower()
        if not any(
            term in topic_blob
            for term in (
                "relationship", "dating", "attachment", "attraction", "chemistry", "flirt",
                "kiss", "breadcrumb", "situationship", "push pull", "mixed signal",
                "emotional availability", "silent treatment", "future faking", "benching",
                "slow fading", "ghosting", "orbiting", "relationship pacing", "mutual effort",
                "crush idealization", "limerence", "jealousy", "friends with benefits",
            )
        ):
            return

        source_by_scene = {
            int(item.get("scene_index") or 0): item
            for item in manifest
            if int(item.get("scene_index") or 0) > 0
        }
        required = max(4, (len(source_by_scene) * 2 + 2) // 3)
        relationship_scenes = {
            scene_index
            for scene_index, item in source_by_scene.items()
            if self._is_brain_relationship_asset(str(item.get("url") or ""), item)
        }
        needed = required - len(relationship_scenes)
        if needed <= 0:
            return

        generated_sources = {
            "local_documentary_diagram",
            "local_fact_card",
            "pollinations_ai",
            "stable_horde",
        }
        candidates: list[tuple[Path, Dict[str, str]]] = []
        for scene_index in sorted(relationship_scenes):
            if scene_index > len(backgrounds):
                continue
            source_path = backgrounds[scene_index - 1]
            source_meta = source_by_scene.get(scene_index)
            if source_path is None or source_meta is None:
                continue
            if (
                str(source_meta.get("source") or "").strip().lower() in generated_sources
                or not str(source_meta.get("url") or "").startswith(("http://", "https://"))
                or str(source_meta.get("source_page_verified") or "").strip().lower() != "true"
                or str(source_meta.get("license") or "").strip().lower() in {"", "unknown"}
            ):
                continue
            candidates.append((source_path, source_meta))

        target_scenes = [
            scene_index
            for scene_index in sorted(source_by_scene)
            if (
                scene_index != 1
                and scene_index not in relationship_scenes
                and scene_index <= len(backgrounds)
            )
        ]
        for target_scene in target_scenes:
            if needed <= 0:
                break
            target_meta = source_by_scene[target_scene]
            scene_text = str(target_meta.get("scene_text") or "")
            previous_path = backgrounds[target_scene - 2] if target_scene > 1 else None
            next_path = backgrounds[target_scene] if target_scene < len(backgrounds) else None
            eligible = [
                (source_path, source_meta)
                for source_path, source_meta in candidates
                if reuse_counts.get(source_path.name, 0) < 1
                and source_path != previous_path
                and source_path != next_path
            ]
            if not eligible:
                continue
            eligible.sort(
                key=lambda item: (
                    0
                    if self._asset_matches_scene_intent(
                        "brain_lens",
                        scene_text,
                        str(item[1].get("url") or ""),
                        item[1],
                    )
                    else 1,
                    reuse_counts.get(item[0].name, 0),
                    0 if item[0].suffix.lower() == ".mp4" else 1,
                )
            )
            source_path, source_meta = eligible[0]
            backgrounds[target_scene - 1] = source_path
            reuse_counts[source_path.name] = reuse_counts.get(source_path.name, 0) + 1
            replacement = dict(source_meta)
            replacement.update(
                {
                    "scene_index": target_scene,
                    "scene_text": scene_text,
                    "search_query": f"verified relationship-footage repair for {scene_text}",
                    "reused_for_scene": True,
                }
            )
            manifest_index = manifest.index(target_meta)
            manifest[manifest_index] = replacement
            source_by_scene[target_scene] = replacement
            needed -= 1

    def _brain_long_verified_reuse_pool(
        self,
        scene_text: str,
        backgrounds: List[Path | None],
        manifest: List[Dict[str, str]],
        reuse_counts: dict[str, int],
    ) -> List[tuple[Path, Dict[str, str]]]:
        """Return one-time, scene-matching real assets after a diversity floor."""
        generated_sources = {
            "local_documentary_diagram",
            "local_fact_card",
            "pollinations_ai",
            "stable_horde",
        }
        verified_by_file: dict[str, Dict[str, str]] = {}
        unique_identities: set[str] = set()
        for item in manifest:
            source_type = str(item.get("source") or "").strip().lower()
            media_url = str(item.get("url") or "").strip()
            file_name = str(item.get("file") or "").strip()
            license_name = str(item.get("license") or "").strip().lower()
            if (
                not file_name
                or source_type in generated_sources
                or not media_url.startswith(("http://", "https://"))
                or str(item.get("source_page_verified") or "").strip().lower() != "true"
                or license_name in {"", "unknown"}
            ):
                continue
            identity = self._canonical_media_url(media_url)
            if not identity:
                continue
            unique_identities.add(identity)
            verified_by_file.setdefault(file_name, item)

        if len(unique_identities) < self.BRAIN_LONG_MIN_UNIQUE_REAL_FOR_REUSE:
            return []

        reusable: List[tuple[Path, Dict[str, str]]] = []
        seen_files: set[str] = set()
        for background in backgrounds:
            if (
                background is None
                or background.name in seen_files
                or reuse_counts.get(background.name, 0) >= 1
            ):
                continue
            source_meta = verified_by_file.get(background.name)
            if source_meta is None:
                continue
            seen_files.add(background.name)
            if self._asset_matches_scene_intent(
                "brain_lens",
                scene_text,
                str(source_meta.get("url") or ""),
                source_meta,
            ):
                reusable.append((background, source_meta))
        return reusable

    def _repair_brain_long_generated_backgrounds(
        self,
        topic: TopicCandidate,
        backgrounds: List[Path | None],
        manifest: List[Dict[str, str]],
        reuse_counts: dict[str, int],
    ) -> None:
        """Replace transient early fallbacks after the verified real pool exists.

        Scene one is fetched before there is anything eligible for bounded reuse.
        A temporary stock-provider miss used to leave a generated card in an
        otherwise fully real 20-scene edit, forcing an expensive complete retry.
        Once all scenes have been attempted, repair those generated slots with a
        scene-matched, licensed real asset. Each source remains reusable at most
        once through ``reuse_counts``.
        """
        if topic.niche_id != "brain_lens" or topic.content_kind != "video" or not topic.scene_plan:
            return
        generated_sources = {
            "local_documentary_diagram",
            "local_fact_card",
            "pollinations_ai",
            "stable_horde",
        }
        for index, background in enumerate(list(backgrounds), start=1):
            if background is None:
                continue
            generated_meta = next(
                (
                    item
                    for item in manifest
                    if int(item.get("scene_index") or 0) == index
                    and str(item.get("source") or "").strip().lower() in generated_sources
                ),
                None,
            )
            if generated_meta is None:
                continue
            scene = topic.scene_plan[(index - 1) % len(topic.scene_plan)]
            scene_intent = str(scene.narration or scene.visual_text or "").strip()
            reusable_assets = self._brain_long_verified_reuse_pool(
                scene_intent,
                backgrounds,
                manifest,
                reuse_counts,
            )
            if not reusable_assets:
                continue
            reusable_assets.sort(
                key=lambda item: (
                    reuse_counts.get(item[0].name, 0),
                    -self._brain_reuse_semantic_score(scene_intent, item[1]),
                    0 if item[0].suffix.lower() == ".mp4" else 1,
                )
            )
            replacement, source_meta = reusable_assets[0]
            reuse_counts[replacement.name] = reuse_counts.get(replacement.name, 0) + 1
            backgrounds[index - 1] = replacement
            manifest.remove(generated_meta)
            reused_meta = dict(source_meta)
            reused_meta.update(
                {
                    "scene_index": index,
                    "scene_text": str(scene.visual_text or "").strip(),
                    "search_query": f"verified post-fetch real-footage reuse for {scene.visual_text}",
                    "reused_for_scene": True,
                }
            )
            manifest.append(reused_meta)
            if background != replacement:
                background.unlink(missing_ok=True)
        manifest.sort(key=lambda item: int(item.get("scene_index") or 0))

    @staticmethod
    def _asset_content_identity(meta: Dict[str, str]) -> str:
        source = re.sub(r"[^a-z0-9]+", " ", str(meta.get("source") or "").lower()).strip()
        title = unquote(str(meta.get("asset_title") or "")).lower().strip()
        title = re.sub(r"^file\s*:\s*", "", title)
        title = re.sub(r"\.(?:tiff?|png|jpe?g|webp)$", "", title)
        title = re.sub(r"[^a-z0-9]+", " ", title).strip()
        if not source or not title:
            return ""
        return f"asset:{source}:{title}"

    @staticmethod
    def _canonical_media_url(url: str) -> str:
        """Strip volatile query strings so signed CDN URLs dedupe consistently."""
        try:
            parsed = urlparse(url)
            return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))
        except Exception:
            return str(url or "").strip()

    def _stable_asset_id(self, url: str, meta: Dict[str, str] | None = None) -> str:
        meta = meta or {}
        explicit = str(meta.get("asset_id") or "").strip().lower()
        if explicit and not explicit.endswith(":none"):
            return explicit

        searchable = " ".join((str(url or ""), str(meta.get("source_page") or ""), str(meta.get("asset_title") or "")))
        patterns = (
            (r"video-files/(\d+)", "pexels_video"),
            (r"/video/[^/]*-?(\d{4,})/?", "pexels_video"),
            (r"pexels-photo-(\d+)", "pexels_photo"),
            (r"/photo/[^/]*-?(\d{4,})/?", "pexels_photo"),
            (r"pixabay[^\s]*/(?:photos|videos)/[^\s/]*-?(\d{3,})/?", "pixabay"),
        )
        for pattern, prefix in patterns:
            match = re.search(pattern, searchable, flags=re.IGNORECASE)
            if match:
                return f"{prefix}:{match.group(1)}"

        if str(meta.get("source") or "").lower() == "wikimedia":
            title = str(meta.get("asset_title") or Path(urlparse(url).path).name).strip().lower()
            if title:
                return f"wikimedia:{title}"

        canonical = self._canonical_media_url(url)
        if not canonical:
            return ""
        digest = hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()[:24]
        return f"url:{digest}"

    def _direct_url_metadata(self, url: str, deadline: float | None = None) -> Dict[str, str]:
        wikimedia = self._wikimedia_metadata_from_url(url, deadline=deadline)
        if wikimedia is not None:
            return wikimedia

        lowered = url.lower()
        video_match = re.search(r"video-files/(\d+)", lowered)
        if video_match:
            asset_id = video_match.group(1)
            return {
                "source": "pexels_video",
                "license": "Pexels License",
                "artist": "unknown",
                "asset_title": f"Pexels video {asset_id}",
                "asset_id": f"pexels_video:{asset_id}",
                "source_page": f"https://www.pexels.com/video/{asset_id}/",
                "is_video": "true",
            }
        photo_match = re.search(r"pexels-photo-(\d+)", lowered)
        if photo_match:
            asset_id = photo_match.group(1)
            return {
                "source": "pexels_photo",
                "license": "Pexels License",
                "artist": "unknown",
                "asset_title": f"Pexels photo {asset_id}",
                "asset_id": f"pexels_photo:{asset_id}",
                "source_page": f"https://www.pexels.com/photo/{asset_id}/",
            }
        return {
            "source": "direct_url",
            "license": "unknown",
            "artist": "unknown",
            "asset_title": unquote(Path(urlparse(url).path).name),
            "asset_id": self._stable_asset_id(url),
            "source_page": "",
        }

    @staticmethod
    def _has_source_page_provenance(meta: Dict[str, str]) -> bool:
        source = str(meta.get("source") or "").lower()
        if source.startswith("local_") or source in {"pollinations_ai", "stable_horde"}:
            return True
        source_page = str(meta.get("source_page") or "").strip()
        if not source_page.startswith(("http://", "https://")):
            return False
        return bool(urlparse(source_page).netloc)

    @staticmethod
    def _dhash(image: Image.Image) -> str:
        sample = ImageOps.grayscale(image).resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(sample.get_flattened_data())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | int(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
        return f"{bits:016x}"

    def _background_dhash(self, image: Image.Image) -> str:
        """Hash around the caption band so changing burned text cannot hide a freeze."""
        rgb = image.convert("RGB")
        width, height = rgb.size
        top = rgb.crop((0, 0, width, max(1, int(height * 0.34))))
        bottom = rgb.crop((0, min(height - 1, int(height * 0.76)), width, height))
        combined = Image.new("RGB", (width, top.height + bottom.height))
        combined.paste(top, (0, 0))
        combined.paste(bottom, (0, top.height))
        return self._dhash(combined)

    @staticmethod
    def _hash_distance(left: str, right: str) -> int:
        try:
            return (int(left, 16) ^ int(right, 16)).bit_count()
        except (TypeError, ValueError):
            return 64

    def _near_duplicate_hash(self, value: str, known: set[str], distance: int = 4) -> bool:
        return bool(value) and any(self._hash_distance(value, other) <= distance for other in known if other)

    @staticmethod
    def _ffmpeg_executable() -> str:
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"

    def _probe_video(self, path: Path, timeout: float = 8.0) -> tuple[int, int, float]:
        try:
            result = subprocess.run(
                [self._ffmpeg_executable(), "-hide_banner", "-i", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(1.0, timeout),
                check=False,
            )
            stderr = result.stderr.decode("utf-8", errors="ignore")
            video_line = next((line for line in stderr.splitlines() if "Video:" in line), "")
            dimension_match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video_line)
            duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
            width = int(dimension_match.group(1)) if dimension_match else 0
            height = int(dimension_match.group(2)) if dimension_match else 0
            duration = 0.0
            if duration_match:
                duration = int(duration_match.group(1)) * 3600 + int(duration_match.group(2)) * 60 + float(duration_match.group(3))
            return width, height, duration
        except Exception:
            return 0, 0, 0.0

    def _background_freeze_scan(self, path: Path, duration: float) -> Dict[str, object]:
        """Decode the full upper-content band and report static intervals.

        Captions occupy the lower frame, so this scan starts eight percent below
        the top and covers the next 55 percent. That still excludes captions but
        includes the actual subject in landscape archive photos; a top-only scan
        falsely classified moving ruins as frozen whenever the sky was uniform.
        The downscale keeps a full long-video pass inexpensive.
        """

        if not path.exists() or path.stat().st_size < 100_000:
            return {
                "status": "skipped_nonproduction_fixture",
                "filter": "upper_content_55_percent_freezedetect",
                "events": [],
            }
        timeout = max(120.0, min(1800.0, float(duration) * 3.0))
        command = [
            self._ffmpeg_executable(),
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            "crop=iw:ih*0.55:0:ih*0.08,scale=320:-2,freezedetect=n=-50dB:d=2.0",
            "-an",
            "-f",
            "null",
            os.devnull,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "error",
                "filter": "upper_content_55_percent_freezedetect",
                "events": [],
                "error": str(exc),
            }
        stderr = result.stderr.decode("utf-8", errors="ignore")
        events: List[Dict[str, float]] = []
        current_start: float | None = None
        current_duration: float | None = None
        malformed_log = False
        for match in re.finditer(
            r"freeze_(start|duration|end):\s*(-?\d+(?:\.\d+)?)",
            stderr,
        ):
            event_type = match.group(1)
            value = float(match.group(2))
            if event_type == "start":
                if current_start is not None:
                    malformed_log = True
                    break
                current_start = value
                current_duration = None
            elif event_type == "duration":
                if current_start is None or value < 0:
                    malformed_log = True
                    break
                current_duration = value
            else:
                if current_start is None or value < current_start:
                    malformed_log = True
                    break
                measured_duration = value - current_start
                if (
                    current_duration is not None
                    and abs(current_duration - measured_duration) > 0.1
                ):
                    malformed_log = True
                    break
                freeze_duration = (
                    current_duration
                    if current_duration is not None
                    else measured_duration
                )
                events.append(
                    {
                        "start": round(current_start, 3),
                        "end": round(value, 3),
                        "duration": round(freeze_duration, 3),
                    }
                )
                current_start = None
                current_duration = None
        if current_start is not None and not malformed_log:
            # Freezedetect may omit the closing event when a freeze reaches EOF.
            freeze_duration = (
                current_duration
                if current_duration is not None
                else max(0.0, float(duration) - current_start)
            )
            events.append(
                {
                    "start": round(current_start, 3),
                    "end": round(current_start + freeze_duration, 3),
                    "duration": round(freeze_duration, 3),
                }
            )
        scan_ok = result.returncode == 0 and not malformed_log
        return {
            "status": "pass" if scan_ok else "error",
            "filter": "upper_content_55_percent_freezedetect",
            "noise_threshold_db": -50,
            "minimum_detected_seconds": 2.0,
            "blocking_seconds": 4.0,
            "events": events,
            **(
                {
                    "error": (
                        "malformed freezedetect event sequence"
                        if malformed_log
                        else f"ffmpeg exited with code {result.returncode}"
                    )
                }
                if not scan_ok
                else {}
            ),
        }

    def _extract_video_frame(self, path: Path, timestamp: float = 0.5, timeout: float = 10.0) -> Image.Image | None:
        try:
            result = subprocess.run(
                [
                    self._ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
                    "-ss", f"{max(0.0, timestamp):.3f}", "-i", str(path),
                    "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(1.0, timeout),
                check=False,
            )
            if not result.stdout:
                return None
            return Image.open(io.BytesIO(result.stdout)).convert("RGB")
        except Exception:
            return None

    def _preview_image(self, path: Path, timestamp: float = 0.5, timeout: float = 10.0) -> Image.Image | None:
        if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            return self._extract_video_frame(path, timestamp=timestamp, timeout=timeout)
        try:
            return ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        except Exception:
            return None

    def _media_perceptual_hash(self, path: Path, deadline: float | None = None) -> str:
        timeout = self._deadline_timeout(deadline, 8.0)
        if timeout is None:
            return ""
        preview = self._preview_image(path, timestamp=0.5, timeout=timeout)
        return self._dhash(preview) if preview is not None else ""

    def _hook_video_has_motion(self, path: Path, deadline: float | None = None) -> bool:
        """Match the final-render hook freeze gate before accepting scene one."""
        hashes: List[str] = []
        # The long/short fast renderer starts the first source at 0.25 seconds;
        # these samples therefore correspond to output hook times 0.5/1.5/2.5.
        for timestamp in (0.75, 1.75, 2.75):
            timeout = self._deadline_timeout(deadline, 4.0)
            if timeout is None:
                return False
            frame = self._extract_video_frame(path, timestamp=timestamp, timeout=timeout)
            if frame is None:
                return False
            hashes.append(self._background_dhash(frame))
        return max(
            self._hash_distance(hashes[left], hashes[right])
            for left in range(len(hashes))
            for right in range(left + 1, len(hashes))
        ) > 2

    @staticmethod
    def _brain_action_cluster(url: str, meta: Dict[str, str]) -> str:
        searchable = re.sub(
            r"[^a-z0-9]+",
            " ",
            f"{url} {meta.get('asset_title', '')}".lower(),
        )
        clusters = (
            ("affection", ("hug", "embrac", "kiss", "danc", "cuddle", "hold hands", "holding hands")),
            ("conflict", ("argu", "fight", "conflict", "apart", "ignore", "breakup", "breaking up")),
            ("conversation", ("talk", "conversation", "discuss")),
            ("phone", ("phone", "text", "message")),
        )
        for action, markers in clusters:
            if any(marker in searchable for marker in markers):
                return action
        return ""

    def _validate_downloaded_media(
        self,
        path: Path,
        meta: Dict[str, str],
        content_kind: str,
        deadline: float | None = None,
        niche_id: str = "",
    ) -> tuple[bool, str]:
        try:
            width = int(str(meta.get("media_width") or 0))
            height = int(str(meta.get("media_height") or 0))
        except (TypeError, ValueError):
            width = height = 0

        if path.suffix.lower() == ".mp4":
            if width <= 0 or height <= 0:
                timeout = self._deadline_timeout(deadline, 6.0)
                if timeout is not None:
                    width, height, _ = self._probe_video(path, timeout=timeout)
        else:
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except Exception:
                return False, "unreadable media"

        meta["media_width"] = str(width or "")
        meta["media_height"] = str(height or "")
        if width <= 0 or height <= 0:
            return False, "missing source dimensions"
        short_edge = min(width, height)
        long_edge = max(width, height)
        verified_ancient_archive = (
            niche_id == "ancient_history"
            and path.suffix.lower() != ".mp4"
            and str(meta.get("source") or "").strip().lower() == "wikimedia"
            and self._has_source_page_provenance(meta)
            and short_edge >= 480
            and long_edge >= 640
        )
        if short_edge < self.min_source_short_edge and not verified_ancient_archive:
            return False, f"source short edge {short_edge}px below {self.min_source_short_edge}px"
        if short_edge < self.min_source_short_edge:
            # A real, correctly attributed archive image is preferable to a generic
            # generated card. Preserve the exception in the manifest for auditing.
            meta["archival_resolution_exception"] = "true"
        if not self.allow_unprovenanced_media and not self._has_source_page_provenance(meta):
            return False, "missing human-viewable source page provenance"
        meta["source_page_verified"] = "true"
        return True, ""

    @staticmethod
    def _asset_search_text(url: str, meta: Dict[str, str]) -> str:
        return re.sub(
            r"[^a-z0-9]+",
            " ",
            " ".join(
                (
                    unquote(url),
                    str(meta.get("asset_title") or ""),
                    str(meta.get("description") or ""),
                    str(meta.get("source_page") or ""),
                )
            ).lower(),
        ).strip()

    @staticmethod
    def _brain_short_relationship_context_query(scene_text: str) -> str:
        """Return a concrete, adult relationship search for one Short beat.

        Broad searches such as ``emotional relationship body language`` can
        satisfy a lexical relationship gate while returning nightclub, gaming,
        kissing, or bedroom footage that does not explain the spoken behavior.
        Keep every query couple-marked for the relationship-ratio gate, but make
        the action and setting specific enough for the current scene.
        """
        scene = re.sub(r"[^a-z0-9]+", " ", (scene_text or "").lower()).strip()
        if any(term in scene for term in ("text", "message", "reply", "phone")):
            if any(term in scene for term in ("disappear", "silence", "uncertainty", "wait", "ignored")):
                return "adult couple discussing a phone message emotional distance daytime"
            if any(term in scene for term in ("plan", "follow through", "consistent")):
                return "adult couple planning a date with a phone conversation daytime"
            return "adult couple reading a phone message together daytime conversation"
        if any(
            term in scene
            for term in (
                "friendliness", "friendly", "look similar", "one cue", "question rather than proof",
                "not proof", "cannot prove",
            )
        ):
            return "adult couple casual cafe conversation friendly body language daytime"
        if any(term in scene for term in ("voice", "speak", "tone", "pitch", "expressiveness")):
            return "adult couple face to face conversation speaking and listening daytime"
        if any(term in scene for term in ("eye contact", "gaze", "lean", "mirror", "posture", "smile")):
            return "adult couple subtle flirting cafe eye contact smiling daytime"
        if any(
            term in scene
            for term in ("reciprocity", "returned", "mutual", "comfort", "consent", "effort", "follow through")
        ):
            return "adult couple respectful mutual conversation eye contact daytime"
        if any(term in scene for term in ("tension", "spark", "chemistry", "attraction")):
            return "adult couple subtle romantic tension cafe eye contact daytime"
        if any(
            term in scene
            for term in ("repeat", "actions match", "clearer", "pattern", "reliable", "consistency")
        ):
            return "adult couple reviewing plans together at a table calendar conversation daytime"
        if any(term in scene for term in ("clarity", "ask", "behavior", "promise", "honest")):
            return "adult couple serious honest relationship conversation daytime"
        if any(term in scene for term in ("plan", "date", "consistent")):
            return "adult couple planning a date face to face conversation daytime"
        return "adult couple face to face relationship conversation daytime realistic"

    def _asset_matches_scene_intent(self, niche_id: str, scene_text: str, url: str, meta: Dict[str, str]) -> bool:
        scene = re.sub(r"[^a-z0-9]+", " ", (scene_text or "").lower()).strip()
        asset = f" {self._asset_search_text(url, meta)} "
        if not scene:
            return True

        if niche_id == "brain_lens":
            unrelated_activity_groups = (
                (("game", "gaming", "gamer", "video game", "virtual reality", "vr headset"),
                 ("game", "gaming", "gamer", "video game", "virtual reality", "vr headset")),
                (("music", "song", "singing", "singer", "concert", "microphone", "nightclub", "dance club"),
                 ("music", "song", "singing", "singer", "concert", "microphone", "nightclub", "dance club", " dj ", "rapper")),
                ((
                    "mortgage", "finance", "financial", "real estate", "broker",
                    "business", "business meeting", "corporate", "deadline", "expense",
                    "budget", "bill", "accounting", "banking", "loan", "invoice", "tax",
                ), (
                    "mortgage", "finance", "financial", "real estate", "broker",
                    " business ", "business meeting", "corporate", "deadline", "expense",
                    "budget", "bill", "accounting", "banking", "loan", "invoice", "tax",
                )),
            )
            for scene_markers, asset_markers in unrelated_activity_groups:
                if not any(marker in scene for marker in scene_markers) and any(marker in asset for marker in asset_markers):
                    return False

            subtle_or_ambiguous_scene = any(
                marker in scene
                for marker in (
                    "friendliness", "friendly", "look similar", "one cue", "question", "proof",
                    "eye contact", "gaze", "lean", "mirror", "smile", "voice", "speak",
                    "conversation", "returned signal", "reciprocity", "comfort", "consent",
                )
            )
            overt_intimacy_markers = (
                "shirtless", "topless", "bare chest", "lingerie", "underwear", "bedroom",
                "couple in bed", "kissing", " kiss ", "passionate", "sensual", "seductive",
                "making out", "cuddle", "embracing", "intimate moment", "love and intimacy",
            )
            scene_explicitly_calls_for_intimacy = any(
                marker in scene
                for marker in (
                    "kiss", "kissing", "physical affection", "making out", "embrace", "cuddle",
                    "bedroom", "sexual intimacy",
                )
            )
            if (
                (subtle_or_ambiguous_scene or not scene_explicitly_calls_for_intimacy)
                and any(marker in asset for marker in overt_intimacy_markers)
            ):
                return False

            calm_or_regulation_scene = any(
                marker in scene
                for marker in (
                    "calm", "unclench", "lengthen the exhale", "calmer body",
                    "regulate before", "respectful distance",
                )
            )
            if calm_or_regulation_scene and any(
                marker in asset
                for marker in ("heated", "angry", "yelling", "shouting", "screaming")
            ):
                return False
            regulation_scene = any(
                marker in scene
                for marker in ("unclench", "lengthen the exhale", "calmer body", "regulate before")
            )
            if regulation_scene:
                if any(marker in asset for marker in ("worried", "anxious", "upset", "panic")):
                    return False
                return any(
                    marker in asset
                    for marker in (
                        "breath", "meditat", "relax", "calm", "mindful", "walk", "walking",
                        "outdoor", "putting phone away", "phone face down",
                    )
                )

            self_worth_scene = any(
                marker in scene
                for marker in ("your worth", "not attractive enough", "impossible to love")
            )
            if self_worth_scene:
                if any(marker in asset for marker in ("romantic couple", "tender moment", "kissing", "cuddle")):
                    return False
                return any(
                    marker in asset
                    for marker in (
                        "friend", "support network", "person walking", "woman walking", "man walking",
                        "alone", "solo", "self assured", "self confident",
                    )
                )

            phone_scene = any(
                marker in scene
                for marker in ("text", "message", "reply", "phone", "smartphone")
            )
            if phone_scene:
                return any(
                    marker in asset
                    for marker in ("phone", "message", "text", "smartphone")
                )

            intent_groups = (
                (("unclench", "lengthen the exhale", "calmer body", "regulate before"), ("calm", "relax", "breath", "meditat", "walk", "outdoor")),
                (("two short columns", "write only observable", "observable facts", "notebook"), ("write", "note", "notebook", "paper", "pen", "journal")),
                (("charged reunion", "repair asks for more", "name what happened", "sexual tension never", "automatic repair"), ("talk", "conversation", "discussion", "repair", "reconnect", "reconcile", "conflict", "argument", "apology")),
                (("listen to the words", "specificity", "read the answer", "grade the pattern"), ("talk", "conversation", "discussion", "speaking", "listening", "eye contact")),
                (("mental health professional", "therapist", "support beyond a video"), ("therapist", "therapy", "counsel", "counseling", "psychologist")),
                (("perform indifference", "post for a reaction", "false power"), ("phone", "social media", "argument", "upset", "distance", "conversation", "talk")),
                (("bounded experiment", "seven days", "record urges"), ("calendar", "routine", "plan", "phone", "conversation", "walk", "work")),
                (("return is new information", "warmth without accountability"), ("phone", "message", "conversation", "talk", "discussion")),
                (("usable boundary", "boundary under your control", "access you will offer"), ("talk", "conversation", "discussion", "distance", "apart", "walking")),
                (("plan", "effort", "mutual", "follow through", "follow-through", "consistent", "behavior scoreboard"), ("plan", "date", "calendar", "schedule", "paper", "note", "book", "conversation", "discussion", "support", "team", "cook", "kitchen", "grocery", "walk", "routine")),
                (("repeat", "actions match", "clearer", "pattern", "reliable", "consistency"), ("plan", "calendar", "conversation", "phone", "discussion", "talk", "support", "work", "focus", "routine", "walk", "outdoor")),
                (("text", "message", "reply", "phone"), ("phone", "message", "text", "smartphone")),
                (("voice", "speak", "conversation", "talk", "tone", "timing"), ("talk", "speak", "conversation", "listen", "discussion", "chat")),
                (("eye contact", "gaze", "look"), ("eye", "look", "gaze", "conversation", "face to face")),
                (("lean", "body language", "posture"), ("lean", "body language", "conversation", "talk", "listen", "eye contact")),
                (("remember", "details", "stay engaged", "friendliness"), ("conversation", "talk", "listen", "smil", "planning", "note")),
                (("tension", "spark", "chemistry", "attraction"), ("flirt", "eye", "smil", "romantic", "conversation", "date", "laugh")),
            )
            for scene_markers, asset_markers in intent_groups:
                if any(marker in scene for marker in scene_markers):
                    return any(marker in asset for marker in asset_markers)
            return True

        if niche_id == "ancient_history":
            lachish_asset = "lachish" in asset or "lakhish" in asset
            if lachish_asset:
                # The named archive object is already independently tied to
                # Lachish.  Match the concrete evidence beat without letting
                # broad prose words such as "route", "maps", or "inscription"
                # redirect a mound, ramp, arrowhead, or throne image into the
                # generic Ancient intent groups below.
                lachish_intents = (
                    (("king carved", "palace wall", "royal propaganda"), ("relief", "palace", "british museum")),
                    (("high mound", "shephelah", "coastal plain", "regional strongpoint"), ("tel lachish", "tel lakhish", "aerial", "archaeological site", "national park")),
                    (("massive ramp", "siege ramp", "stone and earth"), ("ramp",)),
                    (("arrowhead", "arrowheads", "armor scales", "iron chain"), ("arrowhead", "arrowheads")),
                    (("families move", "deportation", "captives", "prisoners"), ("captive", "captor", "prisoner", "exile", "fall of lachish")),
                    (("sits on a throne", "royal tent", "bodyguards"), ("throne", "king sennacherib")),
                    (("wheeled siege engines", "battering ram", "battering rams"), ("siege engine", "siege-engine", "battle engine", "ramp")),
                    (("archers", "slingers", "people on the walls"), ("archer", "slinger", "assault", "battle")),
                )
                for scene_markers, asset_markers in lachish_intents:
                    if any(marker in scene for marker in scene_markers):
                        return any(marker in asset for marker in asset_markers)
                return True
            if any(marker in scene for marker in ("soapstone bird", "soapstone birds")):
                return any(marker in asset for marker in ("soapstone bird", "soapstone birds", "zimbabwe bird"))
            if any(
                marker in scene
                for marker in ("glass bead", "glass beads", "ceramic", "trade goods", "imported goods")
            ):
                return any(
                    marker in asset
                    for marker in ("glass bead", "glass beads", "bead", "ceramic", "pottery", "trade goods")
                )
            if "quarr" in scene:
                return any(marker in asset for marker in ("quarr", "unfinished obelisk", "unfinished stele"))
            if any(
                marker in scene
                for marker in ("rome returned", "returned the obelisk", "return of the obelisk", "1937")
            ):
                return any(
                    marker in asset
                    for marker in ("rome", "roma", "return", "returned", "celebrate", "1937", "1960")
                )
            if any(
                marker in scene
                for marker in ("great stele fell", "stelae fell", "stele fell", "fell and shattered")
            ):
                return any(
                    marker in asset
                    for marker in ("remain", "fragment", "fallen", "great obelisk", "remhai")
                )
            intent_groups = (
                (("obelisk", "stele", "stela"), ("obelisk", "stele", "stela", "monument")),
                (("tomb", "burial", "grave"), ("tomb", "tomba", "tombe", "burial", "grave", "mausoleum", "catacomb", "katakomb", "stele", "stela", "obelisk")),
                (("door", "window", "ornament", "decoration"), ("door", "window", "ornament", "decoration", "stele", "stela", "obelisk", "architecture")),
                (("map", "route", "campaign"), ("map", "route", "campaign", "battle")),
                (("artifact", "inscription", "tablet", "manuscript"), ("artifact", "inscription", "tablet", "manuscript", "museum")),
            )
            active_groups = [asset_markers for scene_markers, asset_markers in intent_groups if any(marker in scene for marker in scene_markers)]
            if active_groups:
                return all(any(marker in asset for marker in asset_markers) for asset_markers in active_groups)
        return True

    def _brain_reuse_semantic_score(self, scene_text: str, meta: Dict[str, str]) -> int:
        """Prefer the closest eligible real asset when a long scene must reuse one."""
        scene = re.sub(r"[^a-z0-9]+", " ", (scene_text or "").lower()).strip()
        asset = f" {self._asset_search_text(str(meta.get('url') or ''), meta)} "
        score = 0
        groups = (
            (("behavior scoreboard", "plan", "follow through", "ordinary week"), ("calendar", "schedule", "plan", "paper", "note", "book")),
            (("unclench", "lengthen the exhale", "regulate before", "calmer body"), ("calm", "space", "apart", "comfort", "relax")),
            (("text", "message", "reply", "phone"), ("phone", "message", "text", "smartphone")),
            (("observable facts", "two short columns", "write only"), ("write", "note", "notebook", "paper", "pen")),
            (("boundary", "return is new information", "warmth without accountability"), ("distance", "apart", "phone", "message")),
            (("perform indifference", "post for a reaction", "false power"), ("phone", "argument", "upset", "distance")),
        )
        for scene_markers, asset_markers in groups:
            if any(marker in scene for marker in scene_markers):
                score += 10 * sum(marker in asset for marker in asset_markers)
        if any(marker in asset for marker in ("conversation", "talk", "discussion")):
            score += 1
        return score

    def _local_visual_relevance(
        self,
        path: Path,
        subject: str,
        scene_text: str,
        deadline: float | None,
    ) -> Dict[str, object] | None:
        """Optional fail-open local Ollama vision check; never requires a cloud/free-trial service."""
        if not self.enable_local_visual_relevance or not self.local_visual_relevance_model:
            return None
        timeout = self._deadline_timeout(deadline, self.local_visual_relevance_timeout)
        if timeout is None:
            return None
        preview = self._preview_image(path, timestamp=0.75, timeout=timeout)
        if preview is None:
            return None
        perceptual_hash = self._dhash(preview)
        cache_key = hashlib.sha256(f"{perceptual_hash}|{subject}|{scene_text}".lower().encode("utf-8", errors="ignore")).hexdigest()
        if cache_key in self._visual_relevance_cache:
            return dict(self._visual_relevance_cache[cache_key])
        buffer = io.BytesIO()
        preview.thumbnail((768, 768), Image.Resampling.LANCZOS)
        preview.save(buffer, format="JPEG", quality=86)
        prompt = (
            "Judge whether this image directly illustrates the requested video scene. "
            "Do not reward generic mood, generic couples, or merely matching location/culture. "
            "Return JSON with integer score 0-100 and a short reason. "
            f"Video subject: {subject}. Scene: {scene_text}."
        )
        payload = {
            "model": self.local_visual_relevance_model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 120},
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(buffer.getvalue()).decode("ascii")],
                }
            ],
        }
        request_timeout = self._deadline_timeout(deadline, self.local_visual_relevance_timeout)
        if request_timeout is None:
            return None
        try:
            response = requests.post(self.local_visual_relevance_url, json=payload, timeout=request_timeout)
            response.raise_for_status()
            content = str((response.json().get("message") or {}).get("content") or "")
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            parsed = json.loads(match.group(0) if match else content)
            result: Dict[str, object] = {
                "score": max(0, min(100, int(parsed.get("score", 0)))),
                "reason": str(parsed.get("reason") or "")[:240],
                "model": self.local_visual_relevance_model,
            }
            self._visual_relevance_cache[cache_key] = result
            return dict(result)
        except Exception as exc:
            print(f"LOCAL VISUAL RELEVANCE skipped (fail-open): {type(exc).__name__}")
            return None

    @staticmethod
    def _manifest_run_is_complete(manifest_path: Path) -> bool:
        run_dir = manifest_path.parent
        for name in ("short.mp4", "video.mp4"):
            media_path = run_dir / name
            try:
                if media_path.is_file() and media_path.stat().st_size >= 100_000:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def _published_run_directories(
        state_path: Path = Path("data/state/runs.jsonl"),
    ) -> set[str] | None:
        """Return resolved output directories that reached a real platform.

        A completed diagnostic MP4 is useful for QA but must not consume visual
        diversity memory. ``None`` means the state log could not be read, in
        which case callers retain the older complete-render fallback behavior.
        """
        if not state_path.is_file():
            return None
        published: set[str] = set()
        try:
            for raw_line in state_path.read_text(encoding="utf-8").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    item = json.loads(raw_line)
                except Exception:
                    continue
                is_published = item.get("uploaded") is True or any(
                    str(item.get(key) or "").strip()
                    for key in (
                        "youtube_id",
                        "youtube_video_id",
                        "facebook_id",
                        "facebook_video_id",
                    )
                )
                recorded_run_dir = str(item.get("run_dir") or "").strip()
                if is_published and recorded_run_dir:
                    published.add(str(Path(recorded_run_dir).resolve()).lower())
        except Exception:
            return None
        return published

    def _channel_root_from_run_dir(self, run_dir: Path, channel_id: str = "") -> Path | None:
        if channel_id:
            for candidate in (run_dir, *run_dir.parents):
                if candidate.name.lower() == channel_id.lower():
                    return candidate
        try:
            return run_dir.parents[2]
        except Exception:
            return None

    def _recent_media_memory(self, run_dir: Path, channel_id: str, manifest_limit: int = 6) -> dict:
        memory = {
            "recent_urls": set(),
            "recent_asset_ids": set(),
            "recent_hashes": set(),
            "recent_lead_urls": set(),
            "artist_counts": {},
            "recent_lead_artists": set(),
        }
        channel_root = self._channel_root_from_run_dir(run_dir, channel_id=channel_id) or run_dir.parents[3]
        manifests = sorted(channel_root.rglob('sources.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        published_run_dirs = self._published_run_directories()
        loaded = 0
        url_manifest_limit = min(3, manifest_limit)
        for manifest_path in manifests:
            if manifest_path.parent == run_dir:
                continue
            if not self._manifest_run_is_complete(manifest_path):
                continue
            if (
                published_run_dirs is not None
                and str(manifest_path.parent.resolve()).lower() not in published_run_dirs
            ):
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(data, list) or not data:
                continue
            loaded += 1
            first = data[0] if isinstance(data[0], dict) else {}
            first_url = str(first.get('url', '')).strip()
            first_artist = str(first.get('artist', '')).strip().lower()
            if first_url:
                memory['recent_lead_urls'].add(first_url)
            if first_artist and first_artist != 'unknown':
                memory['recent_lead_artists'].add(first_artist)
            for item in data:
                if not isinstance(item, dict):
                    continue
                url = str(item.get('url', '')).strip()
                asset_id = str(item.get('asset_id') or self._stable_asset_id(url, item)).strip().lower()
                perceptual_hash = str(item.get('perceptual_hash') or '').strip().lower()
                artist = str(item.get('artist', '')).strip().lower()
                if url and loaded <= url_manifest_limit:
                    memory['recent_urls'].add(url)
                if asset_id and loaded <= url_manifest_limit:
                    memory['recent_asset_ids'].add(asset_id)
                if perceptual_hash and loaded <= url_manifest_limit:
                    memory['recent_hashes'].add(perceptual_hash)
                if artist and artist != 'unknown':
                    memory['artist_counts'][artist] = int(memory['artist_counts'].get(artist, 0)) + 1
            if loaded >= manifest_limit:
                break
        return memory

    def _ancient_continuity_cache(
        self,
        topic: TopicCandidate,
        raw_dir: Path,
        limit: int = 12,
    ) -> list[tuple[Path, dict]]:
        """Return verified, same-subject archive files from prior local runs.

        Wikimedia/API outages should not strand a continuity recovery when the
        workspace already contains licensed archive files for this exact
        historical subject. Each cached file is revalidated against the current
        provenance, resolution, and subject gates, so a caption- or audio-held
        build can safely preserve its good source work. This is opt-in to the
        recovery worker and keeps the normal freshness/reuse policy unchanged.
        """
        if (
            os.getenv("YT_CONTINUITY_RECOVERY", "").strip().lower()
            not in {"1", "true", "yes", "on"}
            or topic.niche_id != "ancient_history"
            or topic.content_kind != "short"
        ):
            return []
        current_run_dir = raw_dir.parent.parent
        channel_root = self._channel_root_from_run_dir(
            current_run_dir,
            channel_id=topic.niche_id,
        )
        if channel_root is None or not channel_root.exists():
            return []
        subject = re.sub(r"[^a-z0-9]+", " ", str(topic.subject or topic.title).lower()).strip()
        if not subject:
            return []
        subject_terms = {term for term in subject.split() if len(term) >= 4}
        relevance_terms = self._strict_ancient_terms(
            topic.title,
            subject=topic.subject,
        )
        candidates: list[tuple[Path, dict]] = []
        seen_urls: set[str] = set()
        manifests = sorted(
            channel_root.rglob("sources.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for manifest_path in manifests:
            if manifest_path.parent == current_run_dir:
                continue
            try:
                items = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source") or "").strip().lower()
                license_name = str(item.get("license") or "").strip().lower()
                if source in {"", "local_fact_card", "local_documentary_diagram"}:
                    continue
                if str(item.get("source_page_verified") or "").strip().lower() != "true":
                    continue
                if license_name in {"", "unknown"}:
                    continue
                searchable = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    " ".join(
                        [
                            str(item.get("verified_subject") or ""),
                            str(item.get("asset_title") or ""),
                            str(item.get("url") or ""),
                        ]
                    ).lower(),
                )
                verified_subject = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    str(item.get("verified_subject") or "").lower(),
                ).strip()
                if verified_subject != subject and not subject_terms.issubset(set(searchable.split())):
                    continue
                url = str(item.get("url") or "").strip()
                identity = self._canonical_media_url(url)
                if not url or not identity or identity in seen_urls:
                    continue
                file_name = str(item.get("file") or "").strip()
                source_path = manifest_path.parent / "images" / "raw" / file_name
                if not source_path.exists():
                    source_path = manifest_path.parent / "images" / file_name
                if not source_path.exists() or source_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    continue
                cached_meta = dict(item)
                valid, _ = self._validate_downloaded_media(
                    source_path,
                    cached_meta,
                    content_kind="short",
                    deadline=time.monotonic() + 3.0,
                    niche_id="ancient_history",
                )
                if not valid or not self._candidate_matches_terms(
                    url,
                    cached_meta,
                    relevance_terms,
                ):
                    continue
                seen_urls.add(identity)
                candidates.append((source_path, cached_meta))
                if len(candidates) >= limit:
                    return candidates
        return candidates

    def _download_candidate(
        self,
        query: str,
        raw_dir: Path,
        sequence: int,
        seen_urls: set[str],
        niche_id: str = "",
        subject: str = "",
        content_kind: str = "short",
        media_memory: dict | None = None,
        seen_artists: dict[str, int] | None = None,
        seen_hashes: set[str] | None = None,
        scene_text: str = "",
        deadline: float | None = None,
        brain_action_counts: dict[str, int] | None = None,
    ) -> tuple[Path, Dict[str, str]] | None:
        if self._deadline_timeout(deadline, 0.2) is None:
            return None
        media_memory = media_memory or {}
        seen_artists = seen_artists if seen_artists is not None else {}
        seen_hashes = seen_hashes if seen_hashes is not None else set()
        brain_action_counts = brain_action_counts if brain_action_counts is not None else {}
        recent_urls = media_memory.get("recent_urls", set())
        recent_asset_ids = media_memory.get("recent_asset_ids", set())
        recent_hashes = media_memory.get("recent_hashes", set())

        if query.startswith("http://") or query.startswith("https://"):
            normalized_subject = re.sub(r"[^a-z0-9]+", " ", subject.lower()).strip()
            lowered_query = unquote(query).lower()
            if "machu picchu" in normalized_subject and any(
                marker in lowered_query
                for marker in (
                    "aguas_calientes", "aguas calientes", "statue", "town", "village", "hotel", "train",
                    "exposici", "exhibe", "aribal", "cuenco", "pottery", "ceramic", "bowl", "yale",
                    "sightseeing", "tourist sign", "tourism sign",
                )
            ):
                return None
            allow_recent_documentary_source = niche_id == "ancient_history" and content_kind == "video"
            if query in seen_urls or (query in recent_urls and not allow_recent_documentary_source):
                return None
            metadata = self._direct_url_metadata(query, deadline=deadline)
            if niche_id == "ancient_history":
                strict_terms = self._strict_ancient_terms(query, subject=subject)
                if (
                    strict_terms
                    and not self._candidate_matches_terms(query, metadata, strict_terms)
                ) or self._ancient_asset_conflicts_with_subject(subject, query, metadata):
                    return None
                if strict_terms:
                    metadata["verified_subject"] = subject or query
            asset_id = self._stable_asset_id(query, metadata)
            if asset_id in seen_urls or (asset_id in recent_asset_ids and not allow_recent_documentary_source):
                return None
            if not self.allow_unprovenanced_media and not self._has_source_page_provenance(metadata):
                return None
            if not self._asset_matches_scene_intent(niche_id, scene_text, query, metadata):
                return None
            ext = ".jpg"
            lowered = query.lower()
            for candidate_ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
                if candidate_ext in lowered:
                    ext = ".mp4" if candidate_ext == ".mp4" else ".jpg"
                    break
            out_path = raw_dir / f"raw_{sequence:02d}_direct{ext}"
            if self._download(query, out_path, deadline=deadline):
                valid, reason = self._validate_downloaded_media(
                    out_path, metadata, content_kind, deadline=deadline, niche_id=niche_id
                )
                perceptual_hash = self._media_perceptual_hash(out_path, deadline=deadline) if valid else ""
                if valid and (
                    self._near_duplicate_hash(perceptual_hash, seen_hashes)
                    or self._near_duplicate_hash(perceptual_hash, recent_hashes)
                ):
                    valid, reason = False, "near-duplicate visual"
                relevance = self._local_visual_relevance(out_path, subject, scene_text, deadline) if valid else None
                if relevance is not None and int(relevance.get("score", 0)) < self.local_visual_relevance_min_score:
                    valid, reason = False, f"local relevance score {relevance.get('score')} below {self.local_visual_relevance_min_score}"
                if not valid:
                    try:
                        out_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    print(f"VISUAL REJECTED: {reason} ({query[:90]})")
                    return None
                seen_urls.add(query)
                if asset_id:
                    seen_urls.add(asset_id)
                if perceptual_hash:
                    seen_hashes.add(perceptual_hash)
                metadata["asset_id"] = asset_id
                metadata["perceptual_hash"] = perceptual_hash
                if relevance is not None:
                    metadata.update(
                        {
                            "relevance_score": str(relevance.get("score", "")),
                            "relevance_reason": str(relevance.get("reason", "")),
                            "relevance_model": str(relevance.get("model", "")),
                        }
                    )
                return out_path, {
                    "file": out_path.name,
                    "url": query,
                    **metadata,
                }
            return None

        candidates: List[Tuple[str, Dict[str, str]]] = []
        orientation = "landscape" if content_kind == "video" else "portrait"
        providers = [
            (self._pexels_video_search, {"query": query, "limit": 2, "orientation": orientation}),
            (self._pexels_photo_search, {"query": query, "limit": 3, "orientation": orientation}),
            (self._wikimedia_search, {"query": query, "limit": 5}),
            (self._pixabay_search, {"query": query, "limit": 3}),
        ]
        if niche_id == "brain_lens":
            lower_query = query.lower()
            relationship_context = any(
                term in f"{query} {subject}".lower()
                for term in (
                    "couple", "relationship", "dating", "date", "flirt", "romantic", "attraction",
                    "chemistry", "kiss", "partner", "love", "attachment", "breadcrumb", "situationship",
                    "mixed signal", "silent treatment", "almost relationship", "push pull", "jealousy",
                )
            )
            human_query = query
            if relationship_context and not any(term in lower_query for term in ("couple", "relationship", "partner", "dating", "romantic")):
                human_query = f"young adult couple relationship {query}"
            elif not any(term in lower_query for term in ("person", "people", "portrait", "face", "relationship", "body language", "stress", "office")):
                human_query = f"{query} real person emotional close up"
            providers = [
                (self._pexels_video_search, {"query": human_query, "limit": 10, "orientation": orientation}),
                (self._pexels_photo_search, {"query": human_query, "limit": 4, "orientation": orientation}),
                (self._pexels_video_search, {"query": query, "limit": 6, "orientation": orientation}),
                (self._pexels_photo_search, {"query": query, "limit": 3, "orientation": orientation}),
                (self._pixabay_search, {"query": human_query, "limit": 3}),
            ]
        if niche_id == "ancient_history":
            historical_queries = [query]
            lower_query = query.lower()
            suffixes = (
                ("archaeology",)
                if content_kind == "video"
                else ("ancient history", "archaeology", "ancient ruins", "artifact")
            )
            for suffix in suffixes:
                if suffix not in lower_query:
                    historical_queries.append(f"{query} {suffix}")
            providers = []
            archive_limit = 50 if content_kind == "video" else 40
            for historical_query in historical_queries[:2]:
                providers.append(
                    (
                        self._wikimedia_search,
                        {"query": historical_query, "limit": archive_limit},
                    )
                )
            stock_query = f"{query} ancient ruins archaeology museum"
            providers.append((self._pexels_video_search, {"query": stock_query, "limit": 2, "orientation": orientation}))
            providers.append((self._pixabay_search, {"query": stock_query, "limit": 4}))

            normalized_subject = re.sub(r"[^a-z0-9]+", " ", str(subject or "").lower()).strip()
            if "axum" in normalized_subject or "aksum" in normalized_subject:
                seed_candidates: List[Tuple[str, Dict[str, str]]] = []
                for seed_query in self._ancient_archive_seed_queries(subject):
                    try:
                        seed_candidates.extend(
                            self._wikimedia_search(
                                seed_query,
                                limit=archive_limit,
                                deadline=time.monotonic() + 8.0,
                            )
                        )
                    except Exception:
                        continue
                strict_seed_terms = self._strict_ancient_terms(query, subject=subject)
                seed_candidates = [
                    (url, meta)
                    for url, meta in seed_candidates
                    if (
                        (not strict_seed_terms or self._candidate_matches_terms(url, meta, strict_seed_terms))
                        and not self._ancient_asset_conflicts_with_subject(subject, url, meta)
                        and self._has_source_page_provenance(meta)
                        and self._asset_matches_scene_intent(niche_id, scene_text, url, meta)
                    )
                ]
                if seed_candidates:
                    candidates.extend(seed_candidates)
                    providers = []

        for provider, kwargs in providers:
            remaining = self._deadline_timeout(deadline, self.provider_budget_seconds)
            if remaining is None:
                break
            provider_deadline = time.monotonic() + remaining
            try:
                candidates.extend(provider(**kwargs, deadline=provider_deadline))
            except Exception:
                continue

        if niche_id == "ancient_history":
            strict_terms = self._strict_ancient_terms(query, subject=subject)
            if strict_terms:
                candidates = [
                    (url, meta)
                    for url, meta in candidates
                    if self._candidate_matches_terms(url, meta, strict_terms)
                ]
            normalized_subject = re.sub(r"[^a-z0-9]+", " ", subject.lower()).strip()
            candidates = [
                (url, meta)
                for url, meta in candidates
                if not self._ancient_asset_conflicts_with_subject(subject, url, meta)
            ]
            if strict_terms:
                for _, candidate_meta in candidates:
                    candidate_meta["verified_subject"] = subject or query
            if "machu picchu" in normalized_subject:
                off_topic_markers = (
                    "aguas calientes", "statue", "town", "village", "hotel", "train",
                    "exposici", "exhibe", "aribal", "cuenco", "pottery", "ceramic", "bowl", "yale",
                    "sightseeing", "tourist sign", "tourism sign",
                )
                candidates = [
                    (url, meta)
                    for url, meta in candidates
                    if not any(
                        marker in f"{unquote(url)} {meta.get('asset_title', '')}".lower()
                        for marker in off_topic_markers
                    )
                ]
                if "terrace" in normalized_subject:
                    engineering_markers = (
                        "terrace", "andenes", "ruin", "architecture", "stone", "inca", "archaeolog", "site",
                    )
                    candidates = [
                        (url, meta)
                        for url, meta in candidates
                        if any(
                            marker in f"{unquote(url)} {meta.get('asset_title', '')}".lower()
                            for marker in engineering_markers
                        )
                    ]

        recent_lead_urls = media_memory.get("recent_lead_urls", set())
        artist_counts = media_memory.get("artist_counts", {})
        recent_lead_artists = media_memory.get("recent_lead_artists", set())

        for url, meta in candidates:
            if self._deadline_timeout(deadline, 0.2) is None:
                break
            artist = str(meta.get("artist", "")).strip().lower()
            is_vid = meta.get("is_video") == "true"
            # The first 2.5 seconds determine whether a Short earns the next
            # swipe. Brain Lens must open on actual motion; a still opener was
            # correctly caught as a freeze by final-render QA.
            if niche_id == "brain_lens" and content_kind == "short" and sequence == 1 and not is_vid:
                continue
            asset_id = self._stable_asset_id(url, meta)
            asset_identity = self._asset_content_identity(meta)
            if niche_id == "brain_lens" and asset_id in self.BRAIN_LENS_BLOCKED_ASSET_IDS:
                continue
            allow_recent_documentary_source = niche_id == "ancient_history" and content_kind == "video"
            if url in seen_urls or (url in recent_urls and not allow_recent_documentary_source):
                continue
            if asset_id in seen_urls or (asset_id in recent_asset_ids and not allow_recent_documentary_source):
                continue
            if asset_identity and asset_identity in seen_urls:
                continue
            if not self.allow_unprovenanced_media and not self._has_source_page_provenance(meta):
                continue
            if not self._asset_matches_scene_intent(niche_id, scene_text, url, meta):
                continue
            if niche_id == "brain_lens":
                searchable_asset = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    f"{url} {meta.get('asset_title', '')} {meta.get('artist', '')}".lower(),
                ).strip()
                relationship_query = any(
                    term in f"{query} {subject}".lower()
                    for term in (
                        "couple", "relationship", "dating", "date", "flirt", "romantic",
                        "attraction", "chemistry", "kiss", "partner", "love", "attachment",
                        "breadcrumb", "situationship", "mixed signal", "silent treatment",
                        "almost relationship", "push pull", "jealousy", "friends with benefits",
                    )
                )
                relationship_markers = (
                    "couple", "relationship", "partner", "dating", " date ", "romantic",
                    "kissing", " kiss ", "hug", "holding hands", "argument", "arguing",
                    "breakup", "breaking up", "man and woman", "woman and man",
                    "male and female", "lovers", "boyfriend",
                    "girlfriend", "husband", "wife",
                )
                padded_asset = f" {searchable_asset} "
                solo_action_scene = content_kind == "video" and any(
                    marker in f"{scene_text} {query}".lower()
                    for marker in (
                        "unclench", "lengthen the exhale", "calmer body", "regulate before",
                        "putting phone face down", "returning to daily routine", "focusing on work",
                        "your worth", "not attractive enough", "impossible to love",
                        "reconnecting with friends", "self respect walk",
                    )
                )
                if (
                    relationship_query
                    and not solo_action_scene
                    and not any(marker in padded_asset for marker in relationship_markers)
                ):
                    continue
                if sequence == 1 and any(
                    marker in f"{scene_text} {query}".lower()
                    for marker in ("phone", "text", "message", "reply", "smartphone")
                ):
                    if not any(marker in searchable_asset for marker in ("phone", "text", "message", "smartphone")):
                        continue
                if any(
                    term in searchable_asset
                    for term in (
                        " child", " children", " kid", " toddler", " baby", " schoolboy", " schoolgirl",
                        " teen", "little girl", "young girl", "small girl", "little boy", "young boy",
                    )
                ) or re.search(r"\b(?:boy|girl)\b", searchable_asset):
                    continue
                if any(
                    term in searchable_asset
                    for term in (
                        "ai generative", "ai generated", "generated portrait", "ai25 studio",
                    )
                ):
                    continue
                if any(
                    term in searchable_asset
                    for term in (
                        "elderly", "senior woman", "senior man", "old woman", "old man",
                        "pregnancy", "pregnant", "pregnancy test", "throwing documents", "paperwork",
                        "thumbs down", "flower petal tears", "painted tears", "screaming", "scream",
                        "shouting", "pulling hair", "stretching neck", "eyes closed", "sleeping",
                        "cheating", "infidelity", "affair",
                    )
                ):
                    continue
                if sequence == 1 and (
                    url in recent_lead_urls
                    or (not is_vid and artist and artist in recent_lead_artists)
                ):
                    continue
                if artist and artist != "unknown":
                    if not is_vid and int(artist_counts.get(artist, 0)) >= (2 if sequence == 1 else 4):
                        continue
                    if int(seen_artists.get(artist, 0)) >= (2 if is_vid else 1):
                        continue
                action = self._brain_action_cluster(url, meta)
                action_limit = 6 if content_kind == "short" else 8
                if action and int(brain_action_counts.get(action, 0)) >= action_limit:
                    continue
            ext = ".mp4" if is_vid else ".jpg"
            out_path = raw_dir / f"raw_{sequence:02d}_{slugify_text(query, 20)}{ext}"
            if self._download(url, out_path, deadline=deadline):
                valid, reason = self._validate_downloaded_media(
                    out_path, meta, content_kind, deadline=deadline, niche_id=niche_id
                )
                if (
                    valid
                    and niche_id == "brain_lens"
                    and sequence == 1
                    and is_vid
                    and not self._hook_video_has_motion(out_path, deadline=deadline)
                ):
                    valid, reason = False, "opening video lacks visible motion in its first 2.5 seconds"
                perceptual_hash = self._media_perceptual_hash(out_path, deadline=deadline) if valid else ""
                if valid and (
                    self._near_duplicate_hash(perceptual_hash, seen_hashes)
                    or self._near_duplicate_hash(perceptual_hash, recent_hashes)
                ):
                    valid, reason = False, "near-duplicate visual"
                relevance = self._local_visual_relevance(out_path, subject, scene_text, deadline) if valid else None
                if relevance is not None and int(relevance.get("score", 0)) < self.local_visual_relevance_min_score:
                    valid, reason = False, f"local relevance score {relevance.get('score')} below {self.local_visual_relevance_min_score}"
                if not valid:
                    try:
                        out_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    print(f"VISUAL REJECTED: {reason} ({url[:90]})")
                    continue
                seen_urls.add(url)
                if asset_id:
                    seen_urls.add(asset_id)
                if asset_identity:
                    seen_urls.add(asset_identity)
                if perceptual_hash:
                    seen_hashes.add(perceptual_hash)
                if artist and artist != "unknown":
                    seen_artists[artist] = int(seen_artists.get(artist, 0)) + 1
                if niche_id == "brain_lens":
                    action = self._brain_action_cluster(url, meta)
                    if action:
                        brain_action_counts[action] = int(brain_action_counts.get(action, 0)) + 1
                return out_path, {
                    "file": out_path.name,
                    "url": url,
                    "source": meta.get("source", "unknown"),
                    "license": meta.get("license", "unknown"),
                    "artist": meta.get("artist", "unknown"),
                    "asset_title": meta.get("asset_title", ""),
                    "asset_id": asset_id,
                    "source_page": meta.get("source_page", ""),
                    "source_page_verified": meta.get("source_page_verified", "true"),
                    "perceptual_hash": perceptual_hash,
                    "description": meta.get("description", ""),
                    "media_width": meta.get("media_width", ""),
                    "media_height": meta.get("media_height", ""),
                    "original_media_width": meta.get("original_media_width", ""),
                    "original_media_height": meta.get("original_media_height", ""),
                    "archival_resolution_exception": meta.get("archival_resolution_exception", ""),
                    "verified_subject": meta.get("verified_subject", ""),
                    "orientation": meta.get("orientation", ""),
                    "relevance_score": str(relevance.get("score", "")) if relevance is not None else "",
                    "relevance_reason": str(relevance.get("reason", "")) if relevance is not None else "",
                    "relevance_model": str(relevance.get("model", "")) if relevance is not None else "",
                }
        return None

    def _fetch_scene_backgrounds(
        self,
        topic: TopicCandidate,
        raw_dir: Path,
        scene_texts: List[str],
        manifest: List[Dict[str, str]],
        media_memory: dict | None = None,
        visual_style: str | None = None,
    ) -> List[Path | None]:
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        seen_artists: dict[str, int] = {}
        direct_queries = [q for q in topic.image_queries if q.startswith("http://") or q.startswith("https://")]
        if topic.niche_id == "ancient_history" and topic.content_kind == "video":
            def direct_visual_score(url: str) -> int:
                lowered = unquote(url).lower()
                score = 0
                if any(marker in lowered for marker in ("archaeological", "archaeology", "ruins", "site_of", "temple", "palace")):
                    score += 40
                if any(marker in lowered for marker in ("representation", "reconstruction", "model", "artifact", "stele", "statue")):
                    score += 26
                if any(marker in lowered for marker in ("montage", "collage", "map", "karta", "empire.png", "empire.jpg", "chronicle", "sketch")):
                    score -= 70
                return score

            direct_queries = sorted(direct_queries, key=direct_visual_score, reverse=True)
        fallback_queries = [q for q in topic.image_queries if q not in direct_queries]

        backgrounds: List[Path | None] = []
        local_fact_card_count = sum(
            1 for item in manifest if str(item.get("source") or "").strip().lower() == "local_fact_card"
        )
        ai_index = 1
        reuse_counts: dict[str, int] = {}
        brain_action_counts: dict[str, int] = {}
        diagram_counts: dict[str, int] = {}
        fetch_budget = self.long_fetch_budget_seconds if topic.content_kind == "video" else self.short_fetch_budget_seconds
        fetch_deadline = time.monotonic() + fetch_budget
        scene_budget = self.long_scene_budget_seconds if topic.content_kind == "video" else self.short_scene_budget_seconds

        total_scenes = len(scene_texts)
        continuity_cache = self._ancient_continuity_cache(topic, raw_dir)
        for idx, text in enumerate(scene_texts, start=1):
            scene_deadline = min(fetch_deadline, time.monotonic() + scene_budget)
            query_candidates: List[str] = []
            scene = None
            if topic.scene_plan:
                if topic.content_kind == "short" and topic.niche_id in {"brain_lens", "ancient_history"}:
                    scene = topic.scene_plan[(idx - 1) % len(topic.scene_plan)]
                else:
                    scene = topic.scene_plan[(idx - 1) % len(topic.scene_plan)]
            scene_intent_text = text
            if (
                topic.niche_id == "brain_lens"
                and scene is not None
                and str(scene.narration or "").strip()
            ):
                # The on-screen visual caption is deliberately short, but source
                # selection needs the complete spoken claim. This is required in
                # both formats to distinguish a phone hook, mutual effort, or a
                # boundary from merely romantic-looking footage.
                scene_intent_text = str(scene.narration).strip()
            if scene is not None and scene.preferred_image_url:
                query_candidates.append(scene.preferred_image_url)
            if direct_queries:
                start = min(idx - 1, len(direct_queries) - 1)
                query_candidates.extend(direct_queries[start:start + 2])
                if start > 0:
                    query_candidates.append(direct_queries[0])
            if scene is not None and scene.search_terms:
                query_candidates.extend(scene.search_terms)
            elif idx == 1:
                query_candidates.extend(fallback_queries[:2] or [topic.title])
            else:
                query_candidates.extend(self._scene_query_candidates(topic.title, text))
                query_candidates.extend(fallback_queries[:2])
            if topic.niche_id == "ancient_history":
                if "machu picchu" in f"{topic.subject} {topic.title}".lower() and any(
                    marker in text.lower() for marker in ("terrace", "drain", "slope", "stonework")
                ):
                    query_candidates = [
                        "Machu Picchu terraces ruins archaeology",
                        "Machu Picchu stone terraces close up",
                        *query_candidates,
                    ]
                subject_queries = self._scene_query_candidates(topic.subject or topic.title, text)
                context_queries = self._ancient_context_queries(topic.subject or topic.title, text)
                query_candidates = [
                    *self._ancient_scene_priority_queries(topic.subject or topic.title, text),
                    *context_queries,
                    *subject_queries,
                    *query_candidates,
                ]
                if fallback_queries:
                    query_candidates = [
                        *query_candidates[:2],
                        *fallback_queries[:2],
                        *query_candidates[2:],
                    ]
            elif topic.niche_id == "brain_lens":
                relationship_topic = any(
                    term in f"{topic.subject} {topic.title} {topic.narration}".lower()
                    for term in (
                        "relationship", "dating", "attachment", "attraction", "chemistry", "flirt",
                        "kiss", "breadcrumb", "situationship", "mixed signal", "silent treatment",
                        "almost relationship", "push pull", "jealousy", "emotional availability",
                        "slow fading", "ghosting", "orbiting", "future faking", "benching",
                        "relationship pacing", "mutual effort", "crush idealization", "limerence",
                        "friends with benefits",
                    )
                )
                if relationship_topic:
                    lowered_text = scene_intent_text.lower()
                    if topic.content_kind == "short":
                        contextual_query = self._brain_short_relationship_context_query(scene_intent_text)
                    elif scene is not None and scene.search_terms:
                        # Long-form plans already carry a ranked, scene-specific
                        # query (journal, phone, boundary, routine, therapist,
                        # and so on). Keep that intent ahead of the generic
                        # relationship guard so any-couple footage cannot win.
                        contextual_query = scene.search_terms[0]
                    elif any(term in lowered_text for term in ("text", "message", "reply", "phone")):
                        if any(term in lowered_text for term in ("disappear", "silence", "uncertainty", "wait")):
                            contextual_query = "young adult couple emotional distance ignored phone message relationship"
                        elif any(term in lowered_text for term in ("plan", "follow-through", "consistent")):
                            contextual_query = "young adult couple planning a date relationship conversation"
                        else:
                            contextual_query = "young adult couple reading a phone message together relationship"
                    elif any(term in lowered_text for term in ("clarity", "ask", "behavior", "promise")):
                        contextual_query = "young adult couple serious honest relationship conversation"
                    else:
                        contextual_query = "young adult couple emotional relationship body language"
                    if int(brain_action_counts.get("conversation", 0)) >= 4:
                        diversity_queries = (
                            "adult couple cooking together at home realistic daytime",
                            "adult couple walking outdoors holding hands realistic daytime",
                            "adult couple planning a date with calendar at home realistic",
                            "adult couple sharing coffee quietly at a cafe realistic daytime",
                            "adult couple doing grocery shopping together realistic lifestyle",
                            "adult couple giving each other respectful space outdoors realistic",
                        )
                        contextual_query = diversity_queries[(idx - 1) % len(diversity_queries)]
                    # This query is the explicit relationship-relevance guard.
                    # Keep it ahead of the per-scene query cap instead of
                    # appending it where a four-query limit can discard it.
                    query_candidates = [contextual_query, *query_candidates]
            query_candidates = list(dict.fromkeys(query_candidates))
            if scene is not None and scene.preferred_image_url:
                # The preferred URL gets its own bounded attempt below; do not spend a
                # second query slot or a second metadata round trip on the same asset.
                query_candidates = [
                    query for query in query_candidates if query != scene.preferred_image_url
                ]
            if topic.niche_id == "ancient_history" and topic.content_kind == "video":
                query_candidates = [
                    query
                    for query in query_candidates
                    if not query.startswith(("http://", "https://"))
                    or self._ancient_asset_matches_scene_period(query, text)
                ]
            query_limit = self.long_query_limit if topic.content_kind == "video" else self.short_query_limit
            query_candidates = query_candidates[:query_limit]

            chosen: Path | None = None
            prompt = self._fallback_visual_prompt(
                topic=topic,
                text=text,
                scene_prompt=scene.visual_prompt if scene is not None else "",
                visual_style=visual_style,
            )

            diagram_kind = ""
            fallback_diagram_kind = ""
            if topic.niche_id == "ancient_history":
                diagram_kind = self._ancient_documentary_diagram_kind(topic, text)
                if not diagram_kind and topic.content_kind == "video":
                    fallback_diagram_kind = self._ancient_fallback_diagram_kind(
                        idx,
                        diagram_counts,
                    )
                # A diagram is a source-backed explainer, not extra B-roll. Reusing
                # the same rendered plate under a second filename overstates visual
                # diversity and creates a noticeable repeat in a 30-second Short.
                # Every diagram kind is therefore single-use; later beats must earn
                # a distinct licensed archival asset instead.
                diagram_limit = 1
                if diagram_kind and diagram_counts.get(diagram_kind, 0) >= diagram_limit:
                    diagram_kind = ""
                if (
                    not diagram_kind
                    and not fallback_diagram_kind
                    and topic.content_kind == "video"
                ):
                    fallback_diagram_kind = self._ancient_fallback_diagram_kind(
                        idx,
                        diagram_counts,
                    )
            if diagram_kind:
                out_path = raw_dir / f"raw_{idx:02d}_{diagram_kind}.jpg"
                if self._generate_ancient_documentary_diagram(topic, out_path, diagram_kind):
                    chosen = out_path
                    diagram_counts[diagram_kind] = diagram_counts.get(diagram_kind, 0) + 1
                    manifest.append(
                        {
                            "file": out_path.name,
                            "url": f"local_documentary_diagram:{slugify_text(topic.subject or topic.title, 36)}:{diagram_kind}:{idx}",
                            "source": "local_documentary_diagram",
                            "license": "Generated in-app from cited research",
                            "artist": "yt_automation",
                            "asset_title": f"{topic.subject or topic.title} {diagram_kind.replace('_', ' ')}",
                            "diagram_kind": diagram_kind,
                            "scene_index": idx,
                            "scene_text": text,
                            **self._ancient_diagram_source_metadata(diagram_kind),
                        }
                    )

            # A continuity worker should use the already verified same-subject
            # archive spine before spending its scene budget on a provider that
            # is currently returning low-resolution or duplicate files.
            if (
                chosen is None
                and topic.niche_id == "ancient_history"
                and topic.content_kind == "short"
                and continuity_cache
            ):
                cached_path, cached_meta = continuity_cache.pop(0)
                cached_out = raw_dir / f"raw_{idx:02d}_continuity_{slugify_text(topic.subject or topic.title, 24)}.jpg"
                try:
                    shutil.copy2(cached_path, cached_out)
                    chosen = cached_out
                    reused_meta = dict(cached_meta)
                    reused_meta.update(
                        {
                            "file": cached_out.name,
                            "scene_index": idx,
                            "scene_text": text,
                            "search_query": f"verified continuity archive reuse for {topic.subject or topic.title}",
                            "reused_for_scene": True,
                            "reused_from_run": str(cached_path.parent.parent.parent),
                        }
                    )
                    manifest.append(reused_meta)
                except OSError:
                    chosen = None

            # Phase 1: real-world assets first for a more professional Shorts look.
            if chosen is None and scene is not None and scene.preferred_image_url:
                result = self._download_candidate(
                    query=scene.preferred_image_url, raw_dir=raw_dir, sequence=idx,
                    seen_urls=seen_urls, niche_id=topic.niche_id, subject=topic.subject,
                    content_kind=topic.content_kind, media_memory=media_memory,
                    seen_artists=seen_artists, seen_hashes=seen_hashes,
                    scene_text=scene_intent_text, deadline=scene_deadline,
                    brain_action_counts=brain_action_counts,
                )
                if result is not None:
                    chosen, meta = result
                    if scene.preferred_image_url in direct_queries:
                        meta["verified_subject"] = topic.subject or topic.title
                    meta.update({"scene_index": idx, "scene_text": text, "search_query": scene.preferred_image_url})
                    manifest.append(meta)

            if chosen is None:
                for query in query_candidates:
                    if self._deadline_timeout(scene_deadline, 0.2) is None:
                        break
                    result = self._download_candidate(
                        query=query, raw_dir=raw_dir, sequence=idx, seen_urls=seen_urls,
                        niche_id=topic.niche_id, subject=topic.subject,
                        content_kind=topic.content_kind, media_memory=media_memory,
                        seen_artists=seen_artists, seen_hashes=seen_hashes,
                        scene_text=scene_intent_text, deadline=scene_deadline,
                        brain_action_counts=brain_action_counts,
                    )
                    if result is None:
                        continue
                    chosen, meta = result
                    if query in direct_queries:
                        meta["verified_subject"] = topic.subject or topic.title
                    meta.update({"scene_index": idx, "scene_text": text, "search_query": query})
                    manifest.append(meta)
                    break

            if (
                chosen is None
                and fallback_diagram_kind
                and diagram_counts.get(fallback_diagram_kind, 0) < 1
            ):
                out_path = raw_dir / f"raw_{idx:02d}_{fallback_diagram_kind}.jpg"
                if self._generate_ancient_documentary_diagram(topic, out_path, fallback_diagram_kind):
                    chosen = out_path
                    diagram_counts[fallback_diagram_kind] = diagram_counts.get(fallback_diagram_kind, 0) + 1
                    manifest.append(
                        {
                            "file": out_path.name,
                            "url": f"local_documentary_diagram:{slugify_text(topic.subject or topic.title, 36)}:{fallback_diagram_kind}:{idx}",
                            "source": "local_documentary_diagram",
                            "license": "Generated in-app from cited research",
                            "artist": "yt_automation",
                            "asset_title": f"{topic.subject or topic.title} {fallback_diagram_kind.replace('_', ' ')}",
                            "diagram_kind": fallback_diagram_kind,
                            "scene_index": idx,
                            "scene_text": text,
                            **self._ancient_diagram_source_metadata(fallback_diagram_kind),
                        }
                    )

            # Long history scenes must try a fresh archive asset and a distinct
            # explanatory diagram before bounded reuse. The old placement ran
            # this branch before either attempt for scenes 15+, guaranteeing four
            # exact duplicate frames in every twenty-scene documentary.
            if (
                chosen is None
                and topic.niche_id == "ancient_history"
                and topic.content_kind == "video"
                and idx > 14
                and sum(reuse_counts.values()) < 2
            ):
                reusable_assets = []
                for background in backgrounds:
                    if background is None:
                        continue
                    source_meta = next(
                        (
                            item
                            for item in manifest
                            if str(item.get("file") or "") == background.name
                            and str(item.get("source") or "") != "local_fact_card"
                        ),
                        None,
                    )
                    if source_meta is not None:
                        reusable_assets.append((background, source_meta))
                if reusable_assets:
                    reusable_assets.sort(
                        key=lambda item: (
                            reuse_counts.get(item[0].name, 0),
                            0 if item[0].suffix.lower() == ".mp4" else 1,
                        )
                    )
                    chosen, source_meta = reusable_assets[0]
                    reuse_counts[chosen.name] = reuse_counts.get(chosen.name, 0) + 1
                    reused_meta = dict(source_meta)
                    reused_meta.update(
                        {
                            "scene_index": idx,
                            "scene_text": text,
                            "search_query": f"verified documentary-source reuse for {text}",
                            "reused_for_scene": True,
                        }
                    )
                    manifest.append(reused_meta)

            if (
                chosen is None
                and topic.niche_id == "ancient_history"
                and topic.content_kind == "short"
                and continuity_cache
            ):
                cached_path, cached_meta = continuity_cache.pop(0)
                cached_out = raw_dir / f"raw_{idx:02d}_continuity_{slugify_text(topic.subject or topic.title, 24)}.jpg"
                try:
                    shutil.copy2(cached_path, cached_out)
                    chosen = cached_out
                    reused_meta = dict(cached_meta)
                    reused_meta.update(
                        {
                            "file": cached_out.name,
                            "scene_index": idx,
                            "scene_text": text,
                            "search_query": f"verified continuity archive reuse for {topic.subject or topic.title}",
                            "reused_for_scene": True,
                            "reused_from_run": str(cached_path.parent.parent.parent),
                        }
                    )
                    manifest.append(reused_meta)
                except OSError:
                    chosen = None

            if chosen is None and topic.content_kind == "video" and topic.niche_id == "brain_lens":
                reusable_assets = self._brain_long_verified_reuse_pool(
                    scene_intent_text,
                    backgrounds,
                    manifest,
                    reuse_counts,
                )
                if reusable_assets:
                    reusable_assets.sort(
                        key=lambda item: (
                            reuse_counts.get(item[0].name, 0),
                            -self._brain_reuse_semantic_score(
                                scene_intent_text,
                                item[1],
                            ),
                            0 if item[0].suffix.lower() == ".mp4" else 1,
                        )
                    )
                    chosen, source_meta = reusable_assets[0]
                    reuse_counts[chosen.name] = reuse_counts.get(chosen.name, 0) + 1
                    reused_meta = dict(source_meta)
                    reused_meta.update(
                        {
                            "scene_index": idx,
                            "scene_text": text,
                            "search_query": f"verified real-footage reuse for {text}",
                            "reused_for_scene": True,
                        }
                    )
                    manifest.append(reused_meta)

            if chosen is None and topic.content_kind == "short" and topic.niche_id == "brain_lens":
                reusable_assets = self._brain_short_verified_reuse_pool(
                    scene_intent_text,
                    backgrounds,
                    manifest,
                    reuse_counts,
                )
                if reusable_assets:
                    reusable_assets.sort(
                        key=lambda item: (
                            reuse_counts.get(item[0].name, 0),
                            0 if item[0].suffix.lower() == ".mp4" else 1,
                        )
                    )
                    chosen, source_meta = reusable_assets[0]
                    reuse_counts[chosen.name] = reuse_counts.get(chosen.name, 0) + 1
                    reused_meta = dict(source_meta)
                    reused_meta.update(
                        {
                            "scene_index": idx,
                            "scene_text": text,
                            "search_query": f"verified scene-matching footage reuse for {text}",
                            "reused_for_scene": True,
                        }
                    )
                    manifest.append(reused_meta)

            # Phase 2: polished AI fallback only when real assets do not land.
            if (
                chosen is None
                and self.enable_pollinations
                and self._allow_ai_image_fallback(topic)
                and self._deadline_timeout(scene_deadline, 0.2) is not None
            ):
                out_path = raw_dir / f"raw_{idx:02d}_pollinations_{ai_index}.jpg"
                if self._pollinations_image_generate(prompt, out_path, deadline=scene_deadline):
                    generated_meta = {
                        "source": "pollinations_ai",
                        "source_page": "https://pollinations.ai/",
                    }
                    valid, reason = self._validate_downloaded_media(out_path, generated_meta, topic.content_kind, deadline=scene_deadline)
                    perceptual_hash = self._media_perceptual_hash(out_path, deadline=scene_deadline) if valid else ""
                    if valid and self._near_duplicate_hash(perceptual_hash, seen_hashes):
                        valid, reason = False, "near-duplicate generated visual"
                    if valid:
                        chosen = out_path
                        if perceptual_hash:
                            seen_hashes.add(perceptual_hash)
                        manifest.append(
                            {
                                "file": out_path.name,
                                "url": f"pollinations.ai [{prompt[:30]}...]",
                                "source": "pollinations_ai",
                                "source_page": "https://pollinations.ai/",
                                "license": "Generated image",
                                "artist": "Pollinations AI",
                                "media_width": generated_meta.get("media_width", ""),
                                "media_height": generated_meta.get("media_height", ""),
                                "perceptual_hash": perceptual_hash,
                                "scene_index": idx,
                                "scene_text": text,
                                "search_query": prompt,
                            }
                        )
                        ai_index += 1
                    else:
                        out_path.unlink(missing_ok=True)
                        print(f"VISUAL REJECTED: {reason} (Pollinations)")

            if (
                chosen is None
                and self.enable_stable_horde
                and self.stable_horde_key
                and self._allow_ai_image_fallback(topic)
                and self._deadline_timeout(scene_deadline, 0.2) is not None
            ):
                out_path = raw_dir / f"raw_{idx:02d}_stablehorde_{ai_index}.jpg"
                if self._stable_horde_generate(prompt, out_path, deadline=scene_deadline):
                    generated_meta = {
                        "source": "stable_horde",
                        "source_page": "https://stablehorde.net/",
                    }
                    valid, reason = self._validate_downloaded_media(out_path, generated_meta, topic.content_kind, deadline=scene_deadline)
                    perceptual_hash = self._media_perceptual_hash(out_path, deadline=scene_deadline) if valid else ""
                    if valid and self._near_duplicate_hash(perceptual_hash, seen_hashes):
                        valid, reason = False, "near-duplicate generated visual"
                    if valid:
                        chosen = out_path
                        if perceptual_hash:
                            seen_hashes.add(perceptual_hash)
                        manifest.append(
                            {
                                "file": out_path.name,
                                "url": f"stablehorde.net [{prompt[:30]}...]",
                                "source": "stable_horde",
                                "source_page": "https://stablehorde.net/",
                                "license": "Generated image",
                                "artist": "Stable Horde Cluster",
                                "media_width": generated_meta.get("media_width", ""),
                                "media_height": generated_meta.get("media_height", ""),
                                "perceptual_hash": perceptual_hash,
                                "scene_index": idx,
                                "scene_text": text,
                                "search_query": prompt,
                            }
                        )
                        ai_index += 1
                    else:
                        out_path.unlink(missing_ok=True)
                        print(f"VISUAL REJECTED: {reason} (Stable Horde)")

            if (
                chosen is None
                and topic.niche_id == "ancient_history"
                and topic.content_kind == "video"
                and idx == 1
            ):
                # A generated opener can never pass the long-documentary gate.
                # Stop source acquisition now so the pipeline can reject this
                # candidate quickly and try the next researched topic.
                backgrounds.append(None)
                break

            if chosen is None and topic.niche_id == "ancient_history":
                # A Short must never collapse into the one-photo slideshow seen in the
                # Axum failure. Before the eight-source quality floor, prefer a
                # scene-specific local card so preflight fails closed. After that floor
                # is met, a different verified archive asset may be reused once to fill
                # the twelve-scene edit without introducing generic cards.
                if topic.content_kind == "short":
                    reusable_backgrounds = self._ancient_short_verified_reuse_pool(
                        topic,
                        backgrounds,
                        manifest,
                        reuse_counts,
                    )
                else:
                    reusable_backgrounds = [
                        background
                        for background in backgrounds
                        if (
                            background is not None
                            and reuse_counts.get(background.name, 0) < 1
                            and sum(reuse_counts.values()) < 2
                        )
                    ]
                if reusable_backgrounds:
                    chosen = min(reusable_backgrounds, key=lambda path: reuse_counts.get(path.name, 0))
                    reuse_counts[chosen.name] = reuse_counts.get(chosen.name, 0) + 1
                    source_meta = next(
                        (item for item in manifest if str(item.get("file") or "") == chosen.name),
                        None,
                    )
                    if source_meta is not None:
                        reused_meta = dict(source_meta)
                        reused_meta.update(
                            {
                                "scene_index": idx,
                                "scene_text": text,
                                "search_query": f"bounded emergency reuse for {text}",
                                "reused_for_scene": True,
                            }
                        )
                        manifest.append(reused_meta)

            if chosen is None:
                out_path = raw_dir / f"raw_{idx:02d}_fallback_card.jpg"
                if self._generate_editorial_fallback(topic, text, out_path, idx):
                    chosen = out_path
                    local_fact_card_count += 1
                    manifest.append(
                        {
                            "file": out_path.name,
                            "url": f"local_fallback [{text[:30]}...]",
                            "source": "local_fact_card",
                            "license": "Generated in-app fallback",
                            "artist": "yt_automation",
                            "scene_index": idx,
                            "scene_text": text,
                        }
                    )

            backgrounds.append(chosen)
            if self._short_fallback_limit_exceeded(topic, local_fact_card_count):
                break

        self._repair_brain_short_relationship_backgrounds(
            topic,
            backgrounds,
            manifest,
            reuse_counts,
        )
        self._repair_brain_long_generated_backgrounds(
            topic,
            backgrounds,
            manifest,
            reuse_counts,
        )
        return backgrounds

    def _wrap_by_width(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
        max_lines: int,
    ) -> List[str]:
        words = text.split()
        if not words:
            return []

        lines: List[str] = []
        current = ""
        remaining = words[:]
        while remaining:
            word = remaining.pop(0)
            proposal = word if not current else f"{current} {word}"
            width = draw.textbbox((0, 0), proposal, font=font)[2]
            if width <= max_width:
                current = proposal
                continue

            if current:
                lines.append(current)
                current = word
            else:
                lines.append(word)
                current = ""

            if len(lines) >= max_lines:
                break

        if current and len(lines) < max_lines:
            lines.append(current)

        return lines[:max_lines]

    def _line_height(self, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
        box = font.getbbox("Ag")
        return int((box[3] - box[1]) * 1.18)

    def _lift_underexposed_ancient_background(self, image: Image.Image) -> Image.Image:
        """Lift a dark archive scan/card before the caption-safe gradient is added."""
        prepared = image.convert("RGB")
        mean_luma, _ = self._frame_luma_stats(prepared)
        if mean_luma >= 72.0:
            return prepared
        # Mild autocontrast restores faded scans; bounded brightness avoids making
        # parchment and stone look artificially white.
        prepared = ImageOps.autocontrast(prepared, cutoff=1)
        corrected_luma, _ = self._frame_luma_stats(prepared)
        gain = min(3.2, max(1.0, 72.0 / max(1.0, corrected_luma)))
        return ImageEnhance.Brightness(prepared).enhance(gain)

    def _draw_text_lines(
        self,
        draw: ImageDraw.ImageDraw,
        lines: List[str],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        x: int,
        y: int,
        color: tuple[int, int, int],
        shadow_alpha: int = 135,
    ) -> int:
        line_height = self._line_height(font)
        for line in lines:
            draw.text((x + 3, y + 3), line, fill=(0, 0, 0, shadow_alpha), font=font)
            draw.text((x, y), line, fill=color, font=font)
            y += line_height
        return y

    def _render_scene_frame(
        self,
        title: str,
        text: str,
        out_path: Path,
        index: int,
        total: int,
        backgrounds: List[Path],
        topic: TopicCandidate,
    ) -> Path:
        primary = backgrounds[index % len(backgrounds)] if backgrounds else None
        
        # If it's a video file, we just return the raw video file and let VideoBuilder handle it later.
        if primary and str(primary).endswith(".mp4"):
            return primary
            
        w, h = self._scene_size(topic)
        focus_points = [(0.45, 0.42), (0.35, 0.5), (0.68, 0.42), (0.5, 0.5)]

        if primary is not None:
            base = self._open_rgb(primary, (w, h), focus=focus_points[index % len(focus_points)])
        else:
            base = self._gradient_bg((w, h), index)
        if topic.niche_id == "ancient_history":
            base = self._lift_underexposed_ancient_background(base)

        img = base.filter(ImageFilter.GaussianBlur(radius=0.5)).convert("RGBA")
        
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        top_height = max(1, int(h * 0.2))
        for y in range(h):
            bottom_ratio = y / max(1, h - 1)
            top_ratio = max(0.0, 1.0 - y / top_height)
            alpha = int((bottom_ratio ** 2.0) * 180 + (top_ratio ** 2.0) * 120)
            overlay_draw.line((0, y, w, y), fill=(0, 0, 0, min(220, alpha)))

        img = Image.alpha_composite(img, overlay)

        # No burned text or boxes needed - Dynamic Captions handle it

        img.convert("RGB").save(out_path, format="JPEG", quality=93)
        return out_path

    @staticmethod
    def _frame_luma_stats(image: Image.Image) -> tuple[float, float]:
        gray = ImageOps.grayscale(image).resize((96, 96), Image.Resampling.BILINEAR)
        values = list(gray.get_flattened_data())
        if not values:
            return 0.0, 1.0
        mean = sum(values) / len(values)
        near_black = sum(1 for value in values if value <= 8) / len(values)
        return round(mean, 2), round(near_black, 4)

    def _write_contact_sheet_entries(
        self,
        entries: List[Tuple[Image.Image, str]],
        out_path: Path,
        portrait: bool,
    ) -> None:
        if not entries:
            return
        thumb_size = (180, 320) if portrait else (320, 180)
        cols = 4
        label_height = 34
        gap = 12
        pad = 16
        rows = (len(entries) + cols - 1) // cols
        canvas = Image.new(
            "RGB",
            (
                cols * thumb_size[0] + (cols - 1) * gap + pad * 2,
                rows * (thumb_size[1] + label_height) + (rows - 1) * gap + pad * 2,
            ),
            (12, 16, 22),
        )
        draw = ImageDraw.Draw(canvas)
        label_font = self._font(15)
        for idx, (image, label) in enumerate(entries):
            col = idx % cols
            row = idx // cols
            x = pad + col * (thumb_size[0] + gap)
            y = pad + row * (thumb_size[1] + label_height + gap)
            thumb = ImageOps.fit(image.convert("RGB"), thumb_size, method=Image.Resampling.LANCZOS)
            canvas.paste(thumb, (x, y))
            draw.rectangle((x, y + thumb_size[1], x + thumb_size[0], y + thumb_size[1] + label_height), fill=(4, 7, 12))
            draw.text((x + 5, y + thumb_size[1] + 7), label[:28], fill=(235, 239, 245), font=label_font)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="JPEG", quality=92)

    def _source_visual_qa(
        self,
        frames: List[Path],
        out_path: Path,
        topic: TopicCandidate,
        source_manifest: List[Dict[str, str]] | None = None,
    ) -> Dict[str, object]:
        entries: List[Tuple[Image.Image, str]] = []
        hashes_by_source: Dict[str, List[str]] = {}
        dark_samples = 0
        sample_count = 0
        dark_real_samples = 0
        real_sample_count = 0
        video_sources = 0
        source_by_scene: Dict[int, Dict[str, str]] = {}
        for item in source_manifest or []:
            try:
                scene_index = int(item.get("scene_index") or 0)
            except (TypeError, ValueError):
                continue
            if scene_index > 0:
                source_by_scene[scene_index] = item
        max_media = 24 if topic.content_kind == "video" else 18
        for index, media_path in enumerate(frames[:max_media], start=1):
            source_type = str(source_by_scene.get(index, {}).get("source") or "").strip().lower()
            is_generated_diagram = source_type in {
                "local_documentary_diagram",
                "local_fact_card",
            }
            is_video = media_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
            timestamps = [0.0]
            if is_video:
                video_sources += 1
                _, _, duration = self._probe_video(media_path)
                if duration > 0.4:
                    timestamps = sorted({max(0.05, duration * 0.12), duration * 0.5, max(0.05, duration * 0.88)})
                else:
                    timestamps = [0.25, 0.75, 1.5]
            for sample_index, timestamp in enumerate(timestamps, start=1):
                preview = self._preview_image(media_path, timestamp=timestamp)
                if preview is None:
                    continue
                perceptual_hash = self._dhash(preview)
                hashes_by_source.setdefault(media_path.name, []).append(perceptual_hash)
                mean_luma, _ = self._frame_luma_stats(preview)
                dark_samples += int(mean_luma < 32.0)
                sample_count += 1
                if not is_generated_diagram:
                    dark_real_samples += int(mean_luma < 32.0)
                    real_sample_count += 1
                marker = f"{index:02d}{'V' if is_video else 'I'}"
                if is_video:
                    marker += f" {timestamp:.1f}s"
                sheet_preview = preview.copy()
                sheet_preview.thumbnail((360, 640), Image.Resampling.LANCZOS)
                entries.append((sheet_preview, marker))

        all_hashes = [(name, value) for name, values in hashes_by_source.items() for value in values]
        duplicate_pairs: List[Dict[str, object]] = []
        for left_index, (left_name, left_hash) in enumerate(all_hashes):
            for right_name, right_hash in all_hashes[left_index + 1:]:
                if left_name == right_name:
                    continue
                distance = self._hash_distance(left_hash, right_hash)
                if distance <= 4:
                    duplicate_pairs.append({"left": left_name, "right": right_name, "distance": distance})
                    if len(duplicate_pairs) >= 20:
                        break
            if len(duplicate_pairs) >= 20:
                break

        self._write_contact_sheet_entries(entries, out_path, portrait=topic.content_kind != "video")
        report: Dict[str, object] = {
            "media_sources": len(frames),
            "video_sources": video_sources,
            "sampled_frames": sample_count,
            "dark_sample_ratio": round(dark_samples / max(1, sample_count), 4),
            "real_sampled_frames": real_sample_count,
            "dark_real_sample_ratio": round(dark_real_samples / max(1, real_sample_count), 4),
            "reused_source_count": sum(
                bool(item.get("reused_for_scene"))
                for item in source_manifest or []
            ),
            "near_duplicate_cross_source_pairs": duplicate_pairs,
            "contact_sheet": str(out_path),
        }
        write_json(out_path.parent / "source_visual_qa.json", report)
        return report

    @staticmethod
    def _parse_timestamp(value: str) -> float:
        match = re.match(r"\s*(\d+):(\d+):(\d+)[,.](\d+)\s*", value or "")
        if not match:
            return 0.0
        fraction = float(f"0.{match.group(4)}")
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + fraction

    def _caption_midpoints(self, subtitles_path: Path | None) -> List[float]:
        if subtitles_path is None or not subtitles_path.is_file():
            return []
        try:
            text = subtitles_path.read_text(encoding="utf-8-sig")
        except Exception:
            return []
        midpoints: List[float] = []
        for start, end in re.findall(
            r"(?m)^(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)",
            text,
        ):
            start_seconds = self._parse_timestamp(start)
            end_seconds = self._parse_timestamp(end)
            if end_seconds > start_seconds:
                midpoints.append((start_seconds + end_seconds) / 2)
        return midpoints

    @staticmethod
    def _timeline_midpoints(timeline_path: Path | None) -> List[float]:
        if timeline_path is None or not timeline_path.is_file():
            return []
        try:
            data = json.loads(timeline_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        midpoints: List[float] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start", 0.0))
                end = float(item.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            if end > start:
                midpoints.append((start + end) / 2)
        return midpoints

    @staticmethod
    def _uniform_time_selection(values: List[float], count: int, duration: float) -> List[float]:
        """Choose temporal representatives without favoring the start of a video."""
        candidates = sorted(dict.fromkeys(values))
        if count <= 0 or not candidates:
            return []
        if len(candidates) <= count:
            return candidates

        # Aim at the center of equally sized duration buckets. Selecting by
        # list index would still over-sample an unusually caption-dense intro.
        span = max(float(duration), candidates[-1], 0.001)
        targets = [span * (index + 0.5) / count for index in range(count)]
        available = set(candidates)
        selected: List[float] = []
        for target in targets:
            nearest = min(available, key=lambda value: (abs(value - target), value))
            selected.append(nearest)
            available.remove(nearest)
        return sorted(selected)

    @classmethod
    def _capped_visual_qa_times(
        cls,
        tagged_times: Dict[float, set[str]],
        duration: float,
        max_samples: int,
    ) -> List[float]:
        """Cap QA timestamps while retaining full-video and source-type coverage."""
        ordered_times = sorted(tagged_times)
        cap = max(0, int(max_samples))
        if len(ordered_times) <= cap:
            return ordered_times
        if cap == 0:
            return []

        # Opening and ending sentinels must survive the cap. Otherwise a long
        # render can begin its first regular sample many seconds in, silently
        # disabling hook-motion QA and missing a broken final frame.
        tag_order = ("T", "V", "C")
        active_tags = [tag for tag in tag_order if any(tag in tags for tags in tagged_times.values())]
        selected: set[float] = {
            value
            for value, tags in tagged_times.items()
            if "H" in tags or "E" in tags
        }
        if len(selected) >= cap:
            return cls._uniform_time_selection(sorted(selected), cap, duration)

        # Caption, visual-scene, and regular-timeline samples each catch a
        # different failure mode. Give every available class a fair share of
        # the cap, accounting for any mandatory samples already selected.
        if active_tags:
            base_quota, remainder = divmod(cap, len(active_tags))
            for index, tag in enumerate(active_tags):
                target_quota = base_quota + int(index < remainder)
                already_selected = sum(tag in tagged_times[value] for value in selected)
                quota = min(cap - len(selected), max(0, target_quota - already_selected))
                candidates = [
                    value
                    for value in ordered_times
                    if tag in tagged_times[value] and value not in selected
                ]
                selected.update(cls._uniform_time_selection(candidates, quota, duration))

        # Nearby caption/scene/regular points can share one timestamp, and a
        # class may contain fewer candidates than its quota. Fill any freed
        # slots with duration-wide representatives from all remaining points.
        remaining_slots = cap - len(selected)
        if remaining_slots > 0:
            candidates = [value for value in ordered_times if value not in selected]
            selected.update(cls._uniform_time_selection(candidates, remaining_slots, duration))
        return sorted(selected)[:cap]

    def write_final_visual_qa(
        self,
        final_video: Path,
        out_dir: Path | None = None,
        subtitles_path: Path | None = None,
        timeline_path: Path | None = None,
        sample_interval_seconds: float = 1.0,
        max_samples: int = 72,
    ) -> Dict[str, object]:
        """Sample the captioned final render, not merely its source stills.

        This is an integration hook for the pipeline after the final MP4 is written. It
        remains completely offline and records enough evidence for a hard upload gate.
        """
        final_video = Path(final_video)
        qa_dir = ensure_dir(out_dir or (final_video.parent / "frame_check"))
        report_path = qa_dir / "final_visual_qa.json"
        if not final_video.is_file():
            report = {"status": "hold", "issues": ["final video is missing"], "video": str(final_video)}
            write_json(report_path, report)
            return report

        width, height, duration = self._probe_video(final_video, timeout=10.0)
        subtitles_path = subtitles_path or (final_video.parent / "subtitles.srt")
        timeline_path = timeline_path or (final_video.parent / "visual_timeline.json")
        interval = max(0.5, float(sample_interval_seconds))
        timeline_samples = [min(duration - 0.05, value) for value in self._timeline_midpoints(timeline_path)] if duration > 0 else []
        caption_samples = [min(duration - 0.05, value) for value in self._caption_midpoints(subtitles_path)] if duration > 0 else []
        regular_samples = []
        cursor = interval / 2
        # Container duration can extend a few milliseconds past the last
        # decodable video frame because the audio stream is slightly longer.
        # Keep routine samples off that boundary; the explicit ending sentinel
        # below still checks the final half-second of the actual render.
        regular_sample_limit = max(0.0, duration - 0.08)
        while duration > 0 and cursor < regular_sample_limit:
            regular_samples.append(cursor)
            cursor += interval

        tagged_times: Dict[float, set[str]] = {}
        for tag, values in (("T", regular_samples), ("V", timeline_samples), ("C", caption_samples)):
            for value in values:
                if value < 0:
                    continue
                rounded = round(value, 3)
                nearby = next((existing for existing in tagged_times if abs(existing - rounded) <= 0.18), None)
                key = nearby if nearby is not None else rounded
                tagged_times.setdefault(key, set()).add(tag)

        if duration > 0:
            for value in (0.5, 1.5, 2.5):
                if value < duration - 0.05:
                    tagged_times.setdefault(round(value, 3), set()).update(("T", "H"))
            for value in (duration - 2.5, duration - 0.5):
                if value > 0:
                    tagged_times.setdefault(round(value, 3), set()).update(("T", "E"))
        ordered_times = self._capped_visual_qa_times(tagged_times, duration, max_samples)

        entries: List[Tuple[Image.Image, str]] = []
        samples: List[Dict[str, object]] = []
        hashes: List[str] = []
        background_hashes: List[str] = []
        failures = 0
        decode_retry_attempts = 0
        failed_sample_times: List[float] = []
        for timestamp in ordered_times:
            image = self._extract_video_frame(final_video, timestamp=timestamp, timeout=10.0)
            if image is None:
                retry_ceiling = max(0.0, duration - 0.12)
                retry_times = (
                    max(0.0, min(retry_ceiling, timestamp - 0.12)),
                    max(0.0, min(retry_ceiling, timestamp + 0.12)),
                )
                for retry_timestamp in retry_times:
                    if abs(retry_timestamp - timestamp) < 0.001:
                        continue
                    decode_retry_attempts += 1
                    image = self._extract_video_frame(
                        final_video,
                        timestamp=retry_timestamp,
                        timeout=12.0,
                    )
                    if image is not None:
                        break
            if image is None:
                failures += 1
                failed_sample_times.append(round(timestamp, 3))
                continue
            perceptual_hash = self._dhash(image)
            background_hash = self._background_dhash(image)
            mean_luma, near_black = self._frame_luma_stats(image)
            tags = "".join(sorted(tagged_times[timestamp]))
            sheet_preview = image.copy()
            sheet_preview.thumbnail((360, 640), Image.Resampling.LANCZOS)
            entries.append((sheet_preview, f"{timestamp:05.1f}s {tags}"))
            hashes.append(perceptual_hash)
            background_hashes.append(background_hash)
            samples.append(
                {
                    "timestamp": round(timestamp, 3),
                    "tags": tags,
                    "perceptual_hash": perceptual_hash,
                    "background_hash": background_hash,
                    "mean_luma": mean_luma,
                    "near_black_ratio": near_black,
                }
            )

        contact_sheet = qa_dir / "final_contact_sheet.jpg"
        self._write_contact_sheet_entries(entries, contact_sheet, portrait=height >= width)
        # Use the uniformly spaced timeline samples for freeze duration. Caption
        # and scene midpoints are intentionally dense and previously turned a
        # two-second animated still into a false "five frames" HOLD.
        regular_background_hashes = [
            str(sample["background_hash"])
            for sample in samples
            if "T" in str(sample.get("tags") or "")
            and "H" not in str(sample.get("tags") or "")
            and "E" not in str(sample.get("tags") or "")
        ]
        freeze_hashes = regular_background_hashes or background_hashes
        static_transitions = [
            index
            for index in range(1, len(freeze_hashes))
            if self._hash_distance(freeze_hashes[index - 1], freeze_hashes[index]) <= 2
        ]
        longest_static_run = 0
        current_run = 0
        for index in range(1, len(freeze_hashes)):
            if self._hash_distance(freeze_hashes[index - 1], freeze_hashes[index]) <= 2:
                current_run += 1
                longest_static_run = max(longest_static_run, current_run)
            else:
                current_run = 0
        hook_hashes = [
            str(sample["background_hash"])
            for sample in samples
            if "H" in str(sample.get("tags") or "")
        ]
        if not hook_hashes:
            hook_hashes = [
                str(sample["background_hash"])
                for sample in samples
                if "T" in str(sample.get("tags") or "")
                and float(sample.get("timestamp") or 0.0) <= 2.75
            ]
        hook_motion_max_distance = max(
            (
                self._hash_distance(hook_hashes[index - 1], hook_hashes[index])
                for index in range(1, len(hook_hashes))
            ),
            default=0,
        )
        dark_samples = sum(1 for sample in samples if float(sample["mean_luma"]) < 32.0)
        near_black_samples = sum(1 for sample in samples if float(sample["near_black_ratio"]) >= 0.9)
        unique_hashes: List[str] = []
        for value in background_hashes:
            if not self._near_duplicate_hash(value, set(unique_hashes), distance=2):
                unique_hashes.append(value)

        issues: List[str] = []
        freeze_scan = self._background_freeze_scan(final_video, duration)
        freeze_events = list(freeze_scan.get("events") or [])
        blocking_freezes = [
            event
            for event in freeze_events
            if float(event.get("duration") or 0.0) >= 4.0
        ]
        review_freezes = [
            event
            for event in freeze_events
            if 2.0 <= float(event.get("duration") or 0.0) < 4.0
        ]
        if duration <= 0 or width <= 0 or height <= 0:
            issues.append("could not probe final video dimensions/duration")
        if failures:
            issues.append(f"failed to decode {failures} requested QA frames")
        if samples and near_black_samples / len(samples) > 0.05:
            issues.append("more than 5% of sampled frames are effectively black")
        if samples and dark_samples / len(samples) > 0.35:
            issues.append("more than 35% of sampled frames are very dark")
        if longest_static_run >= 4:
            issues.append("at least four consecutive uniformly sampled transitions are visually static")
        if len(hook_hashes) >= 2 and hook_motion_max_distance <= 2:
            issues.append("the opening 2.5 seconds lack visible background motion")
        if samples and len(unique_hashes) / len(samples) < 0.35:
            issues.append("fewer than 35% of sampled final frames are visually distinct")
        if freeze_scan.get("status") == "error":
            issues.append("full-duration background freeze scan could not complete")
        if blocking_freezes:
            worst = max(blocking_freezes, key=lambda event: float(event["duration"]))
            issues.append(
                "full-duration scan found a "
                f"{float(worst['duration']):.1f}s frozen background beginning at "
                f"{float(worst['start']):.1f}s"
            )
        if review_freezes:
            issues.append(
                "full-duration scan found 2.0-3.9s static background intervals "
                "that require source-level review"
            )

        report: Dict[str, object] = {
            "status": "hold" if issues else "pass",
            "issues": issues,
            "video": str(final_video),
            "width": width,
            "height": height,
            "duration_seconds": round(duration, 3),
            "requested_samples": len(ordered_times),
            "decoded_samples": len(samples),
            "decode_retry_attempts": decode_retry_attempts,
            "failed_sample_times": failed_sample_times,
            "caption_midpoints_requested": len(caption_samples),
            "visual_midpoints_requested": len(timeline_samples),
            "dark_sample_ratio": round(dark_samples / max(1, len(samples)), 4),
            "near_black_sample_ratio": round(near_black_samples / max(1, len(samples)), 4),
            "static_transition_ratio": round(len(static_transitions) / max(1, len(freeze_hashes) - 1), 4),
            "longest_static_transition_run": longest_static_run,
            "static_analysis_sample_count": len(freeze_hashes),
            "hook_motion_max_hash_distance": hook_motion_max_distance,
            "unique_frame_ratio": round(len(unique_hashes) / max(1, len(samples)), 4),
            "background_freeze_scan": freeze_scan,
            "background_freeze_blocking_intervals": blocking_freezes,
            "background_freeze_review_intervals": review_freezes,
            "contact_sheet": str(contact_sheet),
            "samples": samples,
        }
        write_json(report_path, report)
        return report

    def fetch(self, topic: TopicCandidate, run_dir: Path, count: int,
              visual_style: str | None = None) -> Tuple[List[Path], List[Dict[str, str]]]:
        image_dir = ensure_dir(run_dir / "images")
        raw_dir = ensure_dir(image_dir / "raw")
        manifest: List[Dict[str, str]] = []

        # Prime one broad, distinctive archive result set before fact-specific
        # scene searches can trigger Wikimedia rate limiting. Every later subject
        # query uses the same cache key and still passes the normal provenance,
        # resolution, relevance, and perceptual-hash checks during download.
        if topic.niche_id == "ancient_history":
            archive_limit = 50 if topic.content_kind == "video" else 40
            archive_subject = re.sub(
                r"\s+", " ", str(topic.subject or topic.title or "").strip()
            )
            if archive_subject:
                for seed_query in self._ancient_archive_seed_queries(archive_subject):
                    try:
                        self._wikimedia_search(
                            seed_query,
                            limit=archive_limit,
                            deadline=time.monotonic() + min(35.0, self.provider_budget_seconds + 12.0),
                        )
                    except Exception:
                        # Scene-level providers and fail-closed preflight remain the
                        # authority when the optional cache prime is unavailable.
                        continue

        # Stamp the visual style onto the topic so downstream _fallback_visual_prompt can use it
        if visual_style:
            from dataclasses import replace as dc_replace
            topic = dc_replace(topic)
            # We pass visual_style via a temporary attribute attached to topic if supported,
            # but since TopicCandidate is a dataclass without it we carry it as a local var
            # and pass directly to _fallback_visual_prompt below.

        scene_texts = self._scene_texts(topic, count)
        media_memory = self._recent_media_memory(run_dir=run_dir, channel_id=topic.niche_id)
        scene_backgrounds = self._fetch_scene_backgrounds(
            topic=topic, raw_dir=raw_dir, scene_texts=scene_texts,
            manifest=manifest, media_memory=media_memory,
            visual_style=visual_style,
        )

        final_frames: List[Path] = []
        for idx in range(count):
            out_path = image_dir / f"scene_{idx+1:02d}.jpg"
            background = scene_backgrounds[idx] if idx < len(scene_backgrounds) else None
            final_media_path = self._render_scene_frame(
                title=topic.title,
                text=scene_texts[idx],
                out_path=out_path,
                index=idx,
                total=count,
                backgrounds=[background] if background is not None else [],
                topic=topic,
            )
            final_frames.append(final_media_path)
            pass

        self._source_visual_qa(
            final_frames,
            ensure_dir(run_dir / "frame_check") / "contact_sheet.jpg",
            topic=topic,
            source_manifest=manifest,
        )
        write_json(run_dir / "sources.json", manifest)
        return final_frames, manifest




