from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from yt_auto.images import HybridMediaFetcher
from yt_auto.models import ScenePlanItem, TopicCandidate
from yt_auto.pipeline import ShortsFactory


def make_topic(*, search_terms: list[str] | None = None) -> TopicCandidate:
    scene = ScenePlanItem(
        narration="A voice changes during a conversation.",
        visual_text="Their voice softens while they speak in a conversation",
        search_terms=search_terms or [],
    )
    return TopicCandidate(
        niche_id="brain_lens",
        style="explainer",
        trend_terms=[],
        title="A subtle attraction signal",
        hook="Watch this signal",
        narration=scene.narration,
        visual_captions=[scene.visual_text],
        source_urls=[],
        image_queries=[],
        hashtags=[],
        engagement_score=1.0,
        content_kind="short",
        subject="attraction signals",
        narration_beats=[scene.narration],
        scene_plan=[scene],
    )


class RecordingFetcher(HybridMediaFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def _download_candidate(self, query: str, **kwargs):  # type: ignore[override]
        self.queries.append(query)
        return None

    def _generate_editorial_fallback(self, topic, text, out_path, index):  # type: ignore[override]
        Image.new("RGB", (720, 1280), (20, 30, 40)).save(out_path, format="JPEG")
        return True


class BoundedAncientSourceFetcher(RecordingFetcher):
    def __init__(self, successful_scenes: int) -> None:
        super().__init__()
        self.successful_scenes = successful_scenes
        self._landed_sequences: set[int] = set()

    def _download_candidate(self, query: str, raw_dir: Path, sequence: int, **kwargs):  # type: ignore[override]
        self.queries.append(query)
        if sequence > self.successful_scenes or sequence in self._landed_sequences:
            return None
        self._landed_sequences.add(sequence)
        out_path = raw_dir / f"raw_{sequence:02d}_tikal.jpg"
        Image.new("RGB", (720, 1280), (20 + sequence, 40, 60)).save(out_path, format="JPEG")
        return out_path, {
            "file": out_path.name,
            "url": f"https://upload.wikimedia.org/tikal_{sequence}.jpg",
            "source": "wikimedia",
            "source_page": f"https://commons.wikimedia.org/wiki/File:Tikal_{sequence}.jpg",
            "source_page_verified": "true",
            "license": "CC BY-SA 4.0",
            "artist": f"archive photographer {sequence}",
            "asset_title": f"Tikal Maya temple {sequence}",
            "asset_id": f"wikimedia:tikal-{sequence}",
        }


class PreviewFetcher(HybridMediaFetcher):
    def _probe_video(self, path: Path, timeout: float = 8.0):  # type: ignore[override]
        return 1080, 1920, 6.0

    def _preview_image(self, path: Path, timestamp: float = 0.5):  # type: ignore[override]
        color = int(timestamp * 31) % 255
        return Image.new("RGB", (108, 192), (color, 80, 150))

    def _extract_video_frame(self, path: Path, timestamp: float = 0.5, timeout: float = 10.0):  # type: ignore[override]
        color = int(timestamp * 37) % 255
        return Image.new("RGB", (108, 192), (color, 90, 160))


class ClusteredMidpointFetcher(HybridMediaFetcher):
    def _probe_video(self, path: Path, timeout: float = 8.0):  # type: ignore[override]
        return 1080, 1920, 6.0

    def _extract_video_frame(self, path: Path, timestamp: float = 0.5, timeout: float = 10.0):  # type: ignore[override]
        image = Image.new("RGB", (108, 192), "black")
        # A later animated-still scene can be perceptually similar for ~2.5s,
        # while the hook and surrounding timeline remain visibly dynamic.
        x = 34 if 2.5 <= timestamp <= 5.0 else max(2, min(82, int(timestamp * 24)))
        for px in range(x, min(108, x + 24)):
            for py in range(20, 172):
                image.putpixel((px, py), (240, 240, 240))
        return image


class LongPreviewFetcher(PreviewFetcher):
    def _probe_video(self, path: Path, timeout: float = 8.0):  # type: ignore[override]
        return 1920, 1080, 120.0


class EndingSentinelFetcher(HybridMediaFetcher):
    def _probe_video(self, path: Path, timeout: float = 8.0):  # type: ignore[override]
        return 1080, 1920, 8.93

    def _extract_video_frame(self, path: Path, timestamp: float = 0.5, timeout: float = 10.0):  # type: ignore[override]
        image = Image.new("RGB", (108, 192), "black")
        # The last 2.5 seconds intentionally hold one composition. The ending
        # sentinels sit very close to ordinary half-second samples, so counting
        # both sets as uniform transitions would fabricate a four-step freeze.
        x = 78 if timestamp >= 6.4 else max(2, min(78, int(timestamp * 10)))
        for px in range(x, min(108, x + 22)):
            for py in range(18, 174):
                image.putpixel((px, py), (240, 240, 240))
        return image


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": '{"score": 83, "reason": "Directly shows a conversation"}'}}


class FakeWikimediaResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "query": {
                "pages": {
                    "42": {
                        "pageid": 42,
                        "title": "File:Meroe pyramids.jpg",
                        "imageinfo": [
                            {
                                "url": "https://upload.wikimedia.org/original.jpg",
                                "thumburl": "https://upload.wikimedia.org/thumb/1920px-Meroe.jpg?utm_source=en.wikipedia.org",
                                "width": 6000,
                                "height": 4000,
                                "thumbwidth": 1600,
                                "thumbheight": 1067,
                                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Meroe_pyramids.jpg",
                                "extmetadata": {
                                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                    "Artist": {"value": "Archive photographer"},
                                },
                            }
                        ],
                    }
                }
            }
        }


class FakeWikimediaRateLimitResponse:
    status_code = 429
    headers = {"Retry-After": "3"}

    def raise_for_status(self) -> None:
        raise AssertionError("429 response should be retried before raise_for_status")


class VisualPipelineTests(unittest.TestCase):
    def test_brain_short_disables_ai_image_fallback(self) -> None:
        topic = make_topic()
        topic.niche_id = "brain_lens"
        topic.content_kind = "short"

        self.assertFalse(HybridMediaFetcher._allow_ai_image_fallback(topic))

        topic.content_kind = "video"
        self.assertTrue(HybridMediaFetcher._allow_ai_image_fallback(topic))

        topic.niche_id = "ancient_history"
        self.assertFalse(HybridMediaFetcher._allow_ai_image_fallback(topic))

    def test_short_fallback_limit_stops_doomed_candidate_early(self) -> None:
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.content_kind = "short"

        self.assertFalse(HybridMediaFetcher._short_fallback_limit_exceeded(topic, 1))
        self.assertTrue(HybridMediaFetcher._short_fallback_limit_exceeded(topic, 2))

        topic.niche_id = "brain_lens"
        self.assertTrue(HybridMediaFetcher._short_fallback_limit_exceeded(topic, 2))

        topic.content_kind = "video"
        self.assertFalse(HybridMediaFetcher._short_fallback_limit_exceeded(topic, 2))

    @staticmethod
    def _verified_ancient_source(index: int, *, relevant: bool = True) -> dict:
        subject = "Sogdian merchant artifact" if relevant else "unrelated Roman coin"
        return {
            "file": f"raw_{index:02d}.jpg",
            "url": f"https://upload.wikimedia.org/archive_{index}.jpg",
            "source": "wikimedia",
            "source_page": f"https://commons.wikimedia.org/wiki/File:Archive_{index}.jpg",
            "source_page_verified": "true",
            "license": "CC BY-SA 4.0",
            "asset_title": subject,
            "scene_index": index,
        }

    def test_recent_memory_ignores_incomplete_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "output" / "brain_lens"
            complete = channel / "short" / "2026-01-01" / "complete"
            incomplete = channel / "short" / "2026-01-02" / "incomplete"
            current = channel / "short" / "2026-01-03" / "current"
            for directory in (complete, incomplete, current):
                directory.mkdir(parents=True)
            (complete / "short.mp4").write_bytes(b"x" * 100_000)
            (complete / "sources.json").write_text(
                json.dumps([{"url": "https://cdn.example/kept.jpg", "asset_id": "photo:kept"}]),
                encoding="utf-8",
            )
            (incomplete / "sources.json").write_text(
                json.dumps([{"url": "https://cdn.example/ignored.jpg", "asset_id": "photo:ignored"}]),
                encoding="utf-8",
            )

            fetcher = HybridMediaFetcher()
            with patch.object(fetcher, "_published_run_directories", return_value=None):
                memory = fetcher._recent_media_memory(current, "brain_lens")

            self.assertIn("photo:kept", memory["recent_asset_ids"])
            self.assertNotIn("photo:ignored", memory["recent_asset_ids"])
            self.assertNotIn("https://cdn.example/ignored.jpg", memory["recent_urls"])

    def test_ancient_continuity_reuses_safe_sources_from_caption_held_run(self) -> None:
        # Ancient archive rule: a provenance-verified Wikimedia still is kept
        # down to 360x480, because Commons often only holds small catalog
        # images for niche subjects and an attributed real archive beats a
        # generated fact card. Anything below that floor is still dropped.
        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "output" / "ancient_history"
            held = channel / "short" / "2026-08-12" / "caption_held"
            current = channel / "short" / "2026-08-13" / "current"
            held_raw = held / "images" / "raw"
            current_raw = current / "images" / "raw"
            held_raw.mkdir(parents=True)
            current_raw.mkdir(parents=True)
            Image.new("RGB", (720, 1280), "white").save(held_raw / "good.jpg")
            # Above the 360x480 archive floor -> kept.
            Image.new("RGB", (432, 576), "white").save(held_raw / "archive.jpg")
            # Below the archive floor -> dropped.
            Image.new("RGB", (200, 300), "white").save(held_raw / "small.jpg")
            (held / "sources.json").write_text(
                json.dumps(
                    [
                        {
                            "file": "good.jpg",
                            "url": "https://upload.wikimedia.org/axum_stela_good.jpg",
                            "source": "wikimedia",
                            "source_page": "https://commons.wikimedia.org/wiki/File:Axum_stela_good.jpg",
                            "source_page_verified": "true",
                            "license": "CC BY-SA 4.0",
                            "asset_title": "Axum Obelisks stela field",
                            "verified_subject": "axum obelisks",
                        },
                        {
                            "file": "archive.jpg",
                            "url": "https://upload.wikimedia.org/axum_stela_archive.jpg",
                            "source": "wikimedia",
                            "source_page": "https://commons.wikimedia.org/wiki/File:Axum_stela_archive.jpg",
                            "source_page_verified": "true",
                            "license": "CC BY-SA 4.0",
                            "asset_title": "Axum Obelisks catalog still",
                            "verified_subject": "axum obelisks",
                        },
                        {
                            "file": "small.jpg",
                            "url": "https://upload.wikimedia.org/axum_stela_small.jpg",
                            "source": "wikimedia",
                            "source_page": "https://commons.wikimedia.org/wiki/File:Axum_stela_small.jpg",
                            "source_page_verified": "true",
                            "license": "CC BY-SA 4.0",
                            "asset_title": "Axum Obelisks low resolution",
                            "verified_subject": "axum obelisks",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            topic = TopicCandidate(
                niche_id="ancient_history",
                style="documentary",
                trend_terms=[],
                title="Axum's Giant Stelae",
                hook="",
                narration="",
                visual_captions=[],
                source_urls=[],
                image_queries=[],
                hashtags=[],
                engagement_score=1.0,
                content_kind="short",
                subject="Axum Obelisks",
            )

            with patch.dict("os.environ", {"YT_CONTINUITY_RECOVERY": "1"}):
                cached = HybridMediaFetcher()._ancient_continuity_cache(topic, current_raw)

            names = [path.name for path, _ in cached]
            self.assertEqual(2, len(cached))
            self.assertIn("good.jpg", names)
            self.assertIn("archive.jpg", names)
            self.assertNotIn("small.jpg", names)

    def test_mohenjo_continuity_reuse_is_matched_to_the_spoken_scene(self) -> None:
        fetcher = HybridMediaFetcher()
        dancing = {
            "url": "https://upload.wikimedia.org/Dancing_girl_of_Mohenjo-daro.jpg",
            "asset_title": "File:Dancing girl of Mohenjo-daro.jpg",
            "verified_subject": "mohenjo-daro",
        }
        site = {
            "url": "https://upload.wikimedia.org/Mohenjo-daro.jpg",
            "asset_title": "File:Mohenjo-daro.jpg",
            "verified_subject": "mohenjo-daro",
        }
        seal = {
            "url": "https://upload.wikimedia.org/Mohenjo-daro_three-faced_seal.jpg",
            "asset_title": "File:Mohenjo-daro three-faced seal.jpg",
            "verified_subject": "mohenjo-daro",
        }
        cache = [
            (Path("dancing.jpg"), dancing),
            (Path("site.jpg"), site),
            (Path("seal.jpg"), seal),
        ]

        selected = fetcher._pop_ancient_continuity_asset(
            cache,
            "Mohenjo-Daro hid drains underground.",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(Path("site.jpg"), selected[0])
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Mohenjo-Daro hid drains underground.",
                dancing["url"],
                dancing,
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Indus seals carried short inscriptions.",
                seal["url"],
                seal,
            )
        )

    def test_brain_action_cluster_recognizes_holding_hands_as_affection(self) -> None:
        action = HybridMediaFetcher._brain_action_cluster(
            "",
            {"asset_title": "adult couple walking outdoors holding hands"},
        )

        self.assertEqual("affection", action)

    def test_visual_memory_state_counts_only_published_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            published_run = root / "output" / "ancient_history" / "short" / "published"
            held_run = root / "output" / "ancient_history" / "short" / "held"
            published_run.mkdir(parents=True)
            held_run.mkdir(parents=True)
            state = root / "runs.jsonl"
            state.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "run_dir": str(held_run),
                                "uploaded": False,
                                "upload_skipped": "quality_gate",
                            }
                        ),
                        json.dumps(
                            {
                                "run_dir": str(published_run),
                                "youtube_video_id": "legacy-123",
                            }
                        ),
                    )
                ),
                encoding="utf-8",
            )

            published = HybridMediaFetcher._published_run_directories(state)

            self.assertIsNotNone(published)
            self.assertIn(str(published_run.resolve()).lower(), published)
            self.assertNotIn(str(held_run.resolve()).lower(), published)

    def test_low_resolution_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.jpg"
            Image.new("RGB", (432, 576), "white").save(path)
            meta = {"source": "wikimedia", "source_page": "https://commons.wikimedia.org/wiki/File:Small.jpg"}

            valid, reason = HybridMediaFetcher()._validate_downloaded_media(path, meta, "short")

            self.assertFalse(valid)
            self.assertIn("below 720px", reason)

    def test_resolution_and_provenance_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "good.jpg"
            Image.new("RGB", (720, 1280), "white").save(path)
            meta = {"source": "pexels_photo", "source_page": "https://www.pexels.com/photo/1234/"}

            valid, reason = HybridMediaFetcher()._validate_downloaded_media(path, meta, "short")

            self.assertTrue(valid, reason)
            self.assertEqual(meta["media_width"], "720")
            self.assertEqual(meta["source_page_verified"], "true")

    def test_verified_ancient_archive_can_use_moderate_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.jpg"
            Image.new("RGB", (640, 480), "white").save(path)
            meta = {
                "source": "wikimedia",
                "source_page": "https://commons.wikimedia.org/wiki/File:Meroe.jpg",
            }

            valid, reason = HybridMediaFetcher()._validate_downloaded_media(
                path, meta, "short", niche_id="ancient_history"
            )

            self.assertTrue(valid, reason)
            self.assertEqual(meta["archival_resolution_exception"], "true")

    def test_wikimedia_search_prefers_bounded_derivative_and_caches_results(self) -> None:
        fetcher = HybridMediaFetcher()
        with patch.object(fetcher.session, "get", return_value=FakeWikimediaResponse()) as request:
            first = fetcher._wikimedia_search("Nubian pyramids", limit=8)
            second = fetcher._wikimedia_search("Nubian pyramids", limit=8)

        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["params"]["iiurlwidth"], "1600")
        self.assertEqual(first, second)
        self.assertEqual(
            first[0][0],
            "https://upload.wikimedia.org/thumb/1920px-Meroe.jpg?utm_source=en.wikipedia.org",
        )
        self.assertEqual(first[0][1]["media_width"], "1600")
        self.assertEqual(first[0][1]["original_media_width"], "6000")

    def test_wikimedia_search_retries_bounded_429_with_retry_after(self) -> None:
        fetcher = HybridMediaFetcher()
        with (
            patch.object(
                fetcher.session,
                "get",
                side_effect=[FakeWikimediaRateLimitResponse(), FakeWikimediaResponse()],
            ) as request,
            patch("yt_auto.images.time.sleep") as sleep,
        ):
            results = fetcher._wikimedia_search("Tikal Maya stela", limit=8)

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(3.0)
        self.assertEqual(results[0][1]["asset_title"], "File:Meroe pyramids.jpg")

    def test_ancient_short_search_requests_enough_archive_candidates_for_quality_floor(self) -> None:
        fetcher = HybridMediaFetcher()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(fetcher, "_wikimedia_search", return_value=[]) as wikimedia,
            patch.object(fetcher, "_pexels_video_search", return_value=[]),
            patch.object(fetcher, "_pixabay_search", return_value=[]),
        ):
            fetcher._download_candidate(
                query="Great Zimbabwe",
                raw_dir=Path(tmp),
                sequence=1,
                seen_urls=set(),
                niche_id="ancient_history",
                subject="Great Zimbabwe",
                content_kind="short",
                seen_artists={},
                seen_hashes=set(),
                media_memory={},
                scene_text="Great Zimbabwe dry-stone walls",
                deadline=time.monotonic() + 5,
            )

        self.assertTrue(wikimedia.called)
        self.assertTrue(all(call.kwargs["limit"] == 40 for call in wikimedia.call_args_list))

    def test_ancient_fetch_primes_broad_subject_archive_before_scene_queries(self) -> None:
        fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.subject = "Great Zimbabwe"
        ancient.title = "Great Zimbabwe: Mortarless Walls, Gold, and Indian Ocean Trade"
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(fetcher, "_wikimedia_search", return_value=[]) as wikimedia,
            patch.object(fetcher, "_fetch_scene_backgrounds", return_value=[None]),
            patch.object(fetcher, "_render_scene_frame", side_effect=lambda **kwargs: kwargs["out_path"]),
            patch.object(fetcher, "_source_visual_qa", return_value={}),
        ):
            fetcher.fetch(ancient, Path(tmp), count=1)

        first_call = wikimedia.call_args_list[0]
        self.assertEqual("Great Zimbabwe", first_call.args[0])
        self.assertEqual(40, first_call.kwargs["limit"])

    def test_nubian_subject_terms_accept_meroe_but_reject_generic_giza(self) -> None:
        fetcher = HybridMediaFetcher()
        terms = fetcher._strict_ancient_terms("Nubian Pyramids", subject="nubian pyramids")
        meroe = {"asset_title": "Royal pyramids at Meroe, Sudan"}
        giza = {"asset_title": "Great Pyramid of Giza, Egypt"}

        self.assertTrue(fetcher._candidate_matches_terms("https://example.test/meroe.jpg", meroe, terms))
        self.assertFalse(fetcher._candidate_matches_terms("https://example.test/giza.jpg", giza, terms))

    def test_sogdian_context_queries_are_scene_specific_and_aliases_remain_narrow(self) -> None:
        fetcher = HybridMediaFetcher()
        letter_queries = fetcher._ancient_context_queries(
            "Sogdian merchants", "Their letters recorded debt and family ties"
        )
        city_queries = fetcher._ancient_context_queries(
            "Sogdian merchants", "Samarkand connected distant cities"
        )
        terms = fetcher._strict_ancient_terms("Sogdian Merchants", subject="sogdian merchants")

        self.assertEqual(letter_queries[0], "Sogdian Ancient Letters manuscript")
        self.assertEqual(city_queries[0], "Afrasiab Sogdian murals Samarkand")
        self.assertTrue(
            fetcher._candidate_matches_terms(
                "https://example.test/mural.jpg", {"asset_title": "Afrasiab murals, Samarkand"}, terms
            )
        )
        self.assertFalse(
            fetcher._candidate_matches_terms(
                "https://example.test/map.jpg", {"asset_title": "Generic Silk Road map"}, terms
            )
        )

    def test_kush_and_stonehenge_context_queries_diversify_sparse_archive_searches(self) -> None:
        fetcher = HybridMediaFetcher()

        self.assertEqual(
            fetcher._ancient_context_queries(
                "Kingdom of Kush",
                "Taharqa ruled Egypt as a Kushite pharaoh",
            )[0],
            "Taharqa statue Kushite pharaoh museum",
        )
        self.assertEqual(
            fetcher._ancient_context_queries(
                "Kingdom of Kush",
                "Royal burials moved to the pyramids at Meroe",
            )[0],
            "Nubian pyramids Meroe Sudan archaeology",
        )
        self.assertEqual(
            fetcher._ancient_context_queries(
                "Stonehenge",
                "The sarsen lintels used fitted joints",
            )[0],
            "Stonehenge sarsen trilithon lintel detail",
        )

    def test_siege_of_tyre_relevance_rejects_ambiguous_stock_word_matches(self) -> None:
        fetcher = HybridMediaFetcher()
        terms = fetcher._strict_ancient_terms(
            "Alexander Siege of Tyre",
            subject="alexander siege of tyre",
        )

        self.assertTrue(
            fetcher._candidate_matches_terms(
                "https://example.test/naval.jpg",
                {"asset_title": "A naval action during the Siege of Tyre"},
                terms,
            )
        )
        for wrong_title in (
            "tyre push workout training",
            "ring-necked parakeet on a tyre",
            "Alexander the Great monument in Skopje",
            "St Petersburg palace statue",
        ):
            with self.subTest(title=wrong_title):
                self.assertFalse(
                    fetcher._candidate_matches_terms(
                        "https://example.test/stock.jpg",
                        {"asset_title": wrong_title},
                        terms,
                    )
                )

        self.assertEqual(
            fetcher._ancient_scene_priority_queries(
                "Alexander Siege of Tyre",
                "The causeway crossed the water toward the island",
            )[0],
            "Siege of Tyre causeway Alexander illustration",
        )

    def test_ancient_long_fallback_diagrams_are_distinct_and_never_replace_opener(self) -> None:
        counts: dict[str, int] = {}

        self.assertEqual(HybridMediaFetcher._ancient_fallback_diagram_kind(1, counts), "")
        first = HybridMediaFetcher._ancient_fallback_diagram_kind(15, counts)
        counts[first] = 1
        second = HybridMediaFetcher._ancient_fallback_diagram_kind(16, counts)

        self.assertEqual(first, "preservation_filter")
        self.assertEqual(second, "logistics_flow")
        self.assertNotEqual(first, second)

        for kind in (
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
        ):
            counts[kind] = 1
        self.assertEqual(HybridMediaFetcher._ancient_fallback_diagram_kind(20, counts), "")

    def test_chichen_priority_queries_follow_each_scene(self) -> None:
        fetcher = HybridMediaFetcher()
        self.assertEqual(
            ["Chichen Itza El Castillo pyramid Yucatan"],
            fetcher._ancient_scene_priority_queries(
                "Chichen Itza", "El Castillo dominated the central plaza"
            ),
        )
        self.assertEqual(
            ["Chichen Itza Great Ball Court Yucatan"],
            fetcher._ancient_scene_priority_queries(
                "Chichen Itza", "The Great Ball Court linked ceremony and political display"
            ),
        )
        self.assertEqual(
            ["Chichen Itza Sacred Cenote archaeology"],
            fetcher._ancient_scene_priority_queries(
                "Chichen Itza", "Offerings were recovered from the Sacred Cenote"
            ),
        )

    def test_axum_intent_matching_keeps_tombs_quarries_and_return_scenes_specific(self) -> None:
        fetcher = HybridMediaFetcher()
        self.assertEqual(
            ["Aksum stela", "Aksum obelisk"],
            fetcher._ancient_archive_seed_queries("Axum Obelisks"),
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Elite tombs lie below.",
                "https://commons.wikimedia.org/wiki/File:Aksum_tomba_della_falsa_porta.jpg",
                {"asset_title": "File:Aksum, tomba della falsa porta, camera di sepoltura.jpg"},
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Quarries supplied slabs.",
                "https://commons.wikimedia.org/wiki/File:Aksum_Quarry_for_Obelisks.jpg",
                {"asset_title": "File:Aksum Quarry for Obelisks.jpg"},
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Quarries supplied slabs.",
                "https://commons.wikimedia.org/wiki/File:Axum_obelisk_in_Rome.jpg",
                {"asset_title": "File:Axum obelisk in Rome.jpg"},
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Rome returned the obelisk.",
                "https://commons.wikimedia.org/wiki/File:Celebrate_Return_Axum_Obelisk.jpg",
                {"asset_title": "File:Celebrate The Return of Axum Obelisk.jpg"},
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Rome returned the obelisk.",
                "https://commons.wikimedia.org/wiki/File:Aksum_stele_di_roma.jpg",
                {"asset_title": "File:Aksum, stele 2 (stele di roma) 04.jpg"},
            )
        )
        axum_terms = fetcher._strict_ancient_terms("Aksum Quarry Obelisks", subject="Axum Obelisks")
        self.assertTrue(
            fetcher._candidate_matches_terms(
                "https://commons.wikimedia.org/wiki/File:Aksum_Quarry_for_Obelisks.jpg",
                {"asset_title": "File:Aksum Quarry for Obelisks.jpg"},
                axum_terms,
            )
        )

    def test_all_visual_ready_ancient_subjects_have_scene_specific_archive_queries(self) -> None:
        fetcher = HybridMediaFetcher()
        cases = (
            (
                "Angkor Wat",
                "Long sandstone bas-reliefs show court processions",
                "Angkor Wat sandstone bas relief Khmer",
            ),
            (
                "Tikal",
                "Carved stelae name rulers and date wars",
                "Tikal Maya stela inscription Guatemala",
            ),
            (
                "Chichen Itza",
                "Offerings were recovered from the Sacred Cenote",
                "Chichen Itza Sacred Cenote archaeology",
            ),
            (
                "Great Zimbabwe",
                "Imported glass beads connect the city to Indian Ocean trade",
                "Great Zimbabwe trade beads ceramics archaeology",
            ),
            (
                "Axum Obelisks",
                "The Great Stele fell and shattered beside underground chambers",
                ["Obelisk of Aksum Remains", "King Remhai Stela Aksum"],
            ),
            (
                "Olmec Colossal Heads",
                "Faces and fitted headgear differ between portraits of rulers",
                "Olmec colossal head headdress ruler detail",
            ),
        )
        for subject, scene, expected in cases:
            with self.subTest(subject=subject):
                actual = fetcher._ancient_scene_priority_queries(subject, scene)
                self.assertEqual(actual, expected if isinstance(expected, list) else [expected])

    def test_great_zimbabwe_short_uses_evidence_diagrams_for_artifact_beats(self) -> None:
        fetcher = HybridMediaFetcher()
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.subject = "Great Zimbabwe"
        topic.title = "Great Zimbabwe: Mortarless Walls, Gold, and Indian Ocean Trade"
        topic.content_kind = "short"

        self.assertEqual(
            "great_zimbabwe_trade_evidence",
            fetcher._ancient_documentary_diagram_kind(
                topic,
                "Imported glass beads and ceramics connect the city to Indian Ocean trade",
            ),
        )
        self.assertEqual(
            "great_zimbabwe_bird_evidence",
            fetcher._ancient_documentary_diagram_kind(
                topic,
                "Trade goods and soapstone birds expose local wealth and global exchange",
            ),
        )

    def test_ancient_artifact_beats_reject_generic_site_photos(self) -> None:
        fetcher = HybridMediaFetcher()
        wall = {"asset_title": "Great Zimbabwe Great Enclosure dry stone walls"}
        beads = {"asset_title": "Imported glass beads excavated at Great Zimbabwe"}
        bird = {"asset_title": "Zimbabwe Bird soapstone sculpture from Great Zimbabwe"}

        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Imported glass beads and ceramics connect the city to Indian Ocean trade",
                "https://example.test/wall.jpg",
                wall,
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Imported glass beads and ceramics connect the city to Indian Ocean trade",
                "https://example.test/beads.jpg",
                beads,
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Soapstone birds became powerful symbols",
                "https://example.test/wall.jpg",
                wall,
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "ancient_history",
                "Soapstone birds became powerful symbols",
                "https://example.test/bird.jpg",
                bird,
            )
        )

    def test_great_zimbabwe_short_uses_each_evidence_diagram_once_without_cards(self) -> None:
        fetcher = BoundedAncientSourceFetcher(successful_scenes=10)
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.subject = "Great Zimbabwe"
        topic.title = "Great Zimbabwe: Mortarless Walls, Gold, and Indian Ocean Trade"
        topic.content_kind = "short"
        topic.scene_plan = []
        scene_texts = [
            "Great Zimbabwe was a stone-built city",
            "Dry-stone walls were built without mortar",
            "The Great Enclosure surrounded elite spaces",
            "Imported glass beads and ceramics connect Indian Ocean trade",
            "Soapstone birds became powerful symbols",
            "Great Zimbabwe was a stone-built city",
            "Dry-stone walls were built without mortar",
            "The Great Enclosure surrounded elite spaces",
            "Imported glass beads and ceramics connect Indian Ocean trade",
            "Soapstone birds became powerful symbols",
        ]
        manifest: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            fetcher._fetch_scene_backgrounds(
                topic,
                Path(tmp),
                scene_texts,
                manifest,
                media_memory={},
            )

        diagram_kinds = [
            item.get("diagram_kind")
            for item in manifest
            if item.get("source") == "local_documentary_diagram"
        ]
        self.assertEqual(diagram_kinds.count("great_zimbabwe_trade_evidence"), 1)
        self.assertEqual(diagram_kinds.count("great_zimbabwe_bird_evidence"), 1)
        self.assertFalse(any(item.get("source") == "local_fact_card" for item in manifest))

    def test_great_zimbabwe_bird_plate_is_cached_and_metadata_requires_actual_use(self) -> None:
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.subject = "Great Zimbabwe"
        topic.title = "Great Zimbabwe: Mortarless Walls, Gold, and Indian Ocean Trade"
        topic.content_kind = "short"
        payload = io.BytesIO()
        Image.new("RGB", (958, 538), (170, 160, 145)).save(payload, format="JPEG")

        class Response:
            content = payload.getvalue()

            @staticmethod
            def raise_for_status() -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            fetcher = HybridMediaFetcher()
            with patch.object(fetcher.session, "get", return_value=Response()) as request:
                for index in (1, 2):
                    self.assertTrue(
                        fetcher._generate_ancient_documentary_diagram(
                            topic,
                            Path(tmp) / f"bird-{index}.jpg",
                            "great_zimbabwe_bird_evidence",
                        )
                    )
            self.assertEqual(request.call_count, 1)
            self.assertEqual(
                fetcher._ancient_diagram_source_metadata("great_zimbabwe_bird_evidence")[
                    "supporting_archive_license"
                ],
                "Public domain",
            )

            offline_fetcher = HybridMediaFetcher()
            with patch.object(offline_fetcher.session, "get", side_effect=TimeoutError):
                self.assertTrue(
                    offline_fetcher._generate_ancient_documentary_diagram(
                        topic,
                        Path(tmp) / "bird-fallback.jpg",
                        "great_zimbabwe_bird_evidence",
                    )
                )
            self.assertEqual(
                offline_fetcher._ancient_diagram_source_metadata("great_zimbabwe_bird_evidence"),
                {},
            )

    def test_lachish_requires_asset_supplied_subject_evidence(self) -> None:
        fetcher = HybridMediaFetcher()
        subject = "Assyrian siege of Lachish"
        terms = fetcher._strict_ancient_terms(subject, subject=subject)
        exact = {
            "asset_title": "Lachish Relief, British Museum 9",
            "source_page": "https://commons.wikimedia.org/wiki/File:Lachish_Relief,_British_Museum_9.jpg",
        }
        generic_assyrian = {
            "asset_title": "Khorsabad Palace Reliefs and Assyrian Art - Lamassu",
            "source_page": "https://commons.wikimedia.org/wiki/File:Khorsabad_Lamassu.jpg",
            "verified_subject": subject,
            "search_query": subject,
        }

        self.assertEqual(
            terms,
            ["lachish", "lakhish", "tel lachish", "tel lakhish", "tell ed duweir"],
        )
        self.assertTrue(
            fetcher._candidate_matches_terms(
                "https://upload.wikimedia.org/Lachish_Relief.jpg", exact, terms
            )
        )
        self.assertFalse(
            fetcher._candidate_matches_terms(
                "https://upload.wikimedia.org/Khorsabad_Lamassu.jpg", generic_assyrian, terms
            )
        )

    def test_lachish_queries_follow_each_evidence_beat(self) -> None:
        fetcher = HybridMediaFetcher()
        subject = "Assyrian siege of Lachish"
        cases = (
            ("Propaganda meets the ground", "Lachish relief British Museum full panels Sennacherib"),
            ("Why Lachish mattered", "Tel Lachish archaeological site aerial mound"),
            ("Building the siege ramp", "Lachish siege ramp archaeology southwest corner"),
            ("Weapons under the rubble", "Lachish Assyrian arrowheads British Museum"),
            ("Families become imperial spoils", "Lachish relief Judaean captives families deportation"),
            ("Sennacherib stages judgment", "Sennacherib throne Lachish relief British Museum"),
        )
        for scene_text, expected in cases:
            with self.subTest(scene=scene_text):
                self.assertEqual(
                    fetcher._ancient_scene_priority_queries(subject, scene_text)[0],
                    expected,
                )

    def test_lachish_named_assets_match_literal_evidence_not_generic_prose_tokens(self) -> None:
        fetcher = HybridMediaFetcher()
        cases = (
            (
                "Roads through the Shephelah connected this high mound to Jerusalem.",
                "https://upload.wikimedia.org/Tel-Lakhish-V2-562.jpg",
                {"asset_title": "File:Tel-Lakhish-V2-562.jpg"},
            ),
            (
                "The massive siege ramp became an engineered attack route.",
                "https://upload.wikimedia.org/LachishRamp053011.jpg",
                {"asset_title": "File:LachishRamp053011.jpg"},
            ),
            (
                "The debris field maps 859 arrowheads, armor scales, and an iron chain.",
                "https://upload.wikimedia.org/Assyrian_arrowheads_Lachish_BM.jpg",
                {"asset_title": "File:Assyrian arrowheads Lachish BM.jpg"},
            ),
            (
                "Sennacherib sits on a throne beside cuneiform inscriptions.",
                "https://upload.wikimedia.org/King_Sennacherib_throne_Lachish.jpg",
                {"asset_title": "King Sennacherib on his throne, Siege of Lachish"},
            ),
        )
        for scene, url, metadata in cases:
            with self.subTest(scene=scene):
                self.assertTrue(
                    fetcher._asset_matches_scene_intent(
                        "ancient_history", scene, url, metadata
                    )
                )

    def test_lachish_curated_commons_metadata_survives_api_throttling(self) -> None:
        fetcher = HybridMediaFetcher()
        url = (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5a/"
            "LachishRamp053011.jpg/1920px-LachishRamp053011.jpg"
        )
        with patch.object(fetcher.session, "get", side_effect=AssertionError("API should not run")):
            metadata = fetcher._wikimedia_metadata_from_url(url)

        assert metadata is not None
        self.assertEqual(metadata["license"], "CC BY-SA 3.0")
        self.assertEqual(metadata["artist"], "Wilson44691")
        self.assertEqual(metadata["asset_title"], "File:LachishRamp053011.jpg")
        self.assertEqual(
            metadata["source_page"],
            "https://commons.wikimedia.org/wiki/File:LachishRamp053011.jpg",
        )

    def test_lachish_explanatory_beats_use_cited_specific_diagrams(self) -> None:
        fetcher = HybridMediaFetcher()
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.subject = "Assyrian siege of Lachish"
        topic.title = "How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence"
        topic.content_kind = "video"
        expected = {
            "Judah builds back": "lachish_siege_section",
            "Fire in 701 BCE": "lachish_fire_evidence",
            "Three records, three agendas": "lachish_source_triangle",
            "Jerusalem is a separate question": "lachish_geography",
            "Two destructions, two stories": "lachish_two_layers",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for scene_text, diagram_kind in expected.items():
                with self.subTest(scene=scene_text):
                    self.assertEqual(
                        fetcher._ancient_documentary_diagram_kind(topic, scene_text),
                        diagram_kind,
                    )
                    output = root / f"{diagram_kind}.jpg"
                    self.assertTrue(
                        fetcher._generate_ancient_documentary_diagram(topic, output, diagram_kind)
                    )
                    self.assertEqual(Image.open(output).size, (1920, 1080))
                    metadata = fetcher._ancient_diagram_source_metadata(diagram_kind)
                    self.assertTrue(metadata["supporting_source_page"].startswith("https://"))
                    self.assertIn("not a scale reconstruction", metadata["diagram_scope_note"])

    def test_chichen_rejects_named_wrong_site_assets(self) -> None:
        fetcher = HybridMediaFetcher()
        wrong = {"asset_title": "Maya pyramid at Palenque, Chiapas"}
        right = {"asset_title": "El Castillo at Chichen Itza, Yucatan"}
        self.assertTrue(
            fetcher._ancient_asset_conflicts_with_subject(
                "Chichen Itza", "https://example.test/palenque.jpg", wrong
            )
        )
        self.assertFalse(
            fetcher._ancient_asset_conflicts_with_subject(
                "Chichen Itza", "https://example.test/el-castillo.jpg", right
            )
        )

    def test_great_zimbabwe_rejects_the_exact_false_positive_asset_titles(self) -> None:
        fetcher = HybridMediaFetcher()
        subject = "Great Zimbabwe"
        terms = fetcher._strict_ancient_terms(subject, subject=subject)
        bad_assets = (
            "victoria falls, africa, zimbabwe, river, nature, landscape",
            "https://www.pexels.com/video/the-great-pyramid-of-giza-19502488/",
            "great wall of china, beijing, china, asia, historical landmark",
            "masada national park, the great revolt, roman siege ramp, israel",
            "jungle, nature, forest, plants, zimbabwe, africa",
            "File:Great Dyke of Zimbabwe ISS.JPG",
            "File:GREAT ZIMBABWE UNIVERSITY PEER EDUCATORS, ZIMBABWE 02.jpg",
            "File:Great Zimbabwe University Logo.png",
        )
        for index, title in enumerate(bad_assets):
            meta = {
                "asset_title": title,
                "source_page": f"https://example.test/assets/{index}/{title.replace(' ', '_')}",
                "verified_subject": subject,
                "search_query": "Great Zimbabwe Great Enclosure dry stone walls",
            }
            with self.subTest(title=title):
                self.assertFalse(
                    fetcher._candidate_matches_terms(
                        f"https://cdn.example.test/opaque-{index}.jpg", meta, terms
                    )
                )
                self.assertTrue(
                    fetcher._ancient_asset_conflicts_with_subject(
                        subject, f"https://cdn.example.test/opaque-{index}.jpg", meta
                    )
                )

    def test_great_zimbabwe_accepts_distinctive_site_and_artifact_metadata(self) -> None:
        fetcher = HybridMediaFetcher()
        subject = "Great Zimbabwe"
        terms = fetcher._strict_ancient_terms(subject, subject=subject)
        correct_assets = (
            "File:Conical Tower - Great Enclosure III (33736918448).jpg",
            "File:140 of The Ruined Cities of Mashonaland, excavation and exploration.jpg",
            "File:Zimbabwe Bird soapstone sculpture.jpg",
        )
        for index, title in enumerate(correct_assets):
            meta = {
                "asset_title": title,
                "source_page": f"https://commons.wikimedia.org/wiki/File:correct-{index}.jpg",
            }
            with self.subTest(title=title):
                self.assertTrue(
                    fetcher._candidate_matches_terms(
                        f"https://upload.wikimedia.org/correct-{index}.jpg", meta, terms
                    )
                )
                self.assertFalse(
                    fetcher._ancient_asset_conflicts_with_subject(
                        subject, f"https://upload.wikimedia.org/correct-{index}.jpg", meta
                    )
                )

    def test_pipeline_labels_are_not_independent_ancient_subject_evidence(self) -> None:
        fetcher = HybridMediaFetcher()
        terms = fetcher._strict_ancient_terms("Great Zimbabwe", subject="Great Zimbabwe")
        stamped_only = {
            "asset_title": "Generic ancient stone landscape",
            "source_page": "https://example.test/assets/opaque-42",
            "verified_subject": "Great Zimbabwe",
            "search_query": "Great Zimbabwe Great Enclosure dry stone walls",
        }
        self.assertFalse(
            fetcher._candidate_matches_terms(
                "https://cdn.example.test/opaque-42.jpg", stamped_only, terms
            )
        )

    def test_visual_ready_subjects_require_distinctive_metadata_and_reject_cross_sites(self) -> None:
        fetcher = HybridMediaFetcher()
        cases = (
            (
                "Angkor Wat",
                "File:Angkor Wat five towers and causeway, Cambodia.jpg",
                "File:Bayon temple at Angkor Thom.jpg",
            ),
            (
                "Tikal",
                "File:Tikal Temple I and Great Plaza, Guatemala.jpg",
                "File:Temple of the Inscriptions at Palenque.jpg",
            ),
            (
                "Chichen Itza",
                "File:Sacred Cenote at Chichen Itza, Yucatan.jpg",
                "File:Tikal Temple IV in Guatemala.jpg",
            ),
            (
                "Great Zimbabwe",
                "File:Great Zimbabwe wall, Masvingo, Zimbabwe.jpg",
                "File:Great Dyke of Zimbabwe ISS.JPG",
            ),
            (
                "Axum Obelisks",
                "File:Obelisk of Axum funerary stele, Ethiopia.jpg",
                "File:Luxor Obelisk at Place de la Concorde.jpg",
            ),
            (
                "Olmec Colossal Heads",
                "File:Olmec colossal head, San Lorenzo Monument 4.jpg",
                "File:Moai colossal head on Easter Island.jpg",
            ),
        )
        for index, (subject, correct_title, wrong_title) in enumerate(cases):
            terms = fetcher._strict_ancient_terms(subject, subject=subject)
            correct = {"asset_title": correct_title}
            wrong = {"asset_title": wrong_title}
            with self.subTest(subject=subject):
                self.assertTrue(
                    fetcher._candidate_matches_terms(
                        f"https://example.test/correct-{index}.jpg", correct, terms
                    )
                )
                self.assertFalse(
                    fetcher._ancient_asset_conflicts_with_subject(
                        subject, f"https://example.test/correct-{index}.jpg", correct
                    )
                )
                self.assertFalse(
                    fetcher._candidate_matches_terms(
                        f"https://example.test/wrong-{index}.jpg", wrong, terms
                    )
                )
                self.assertTrue(
                    fetcher._ancient_asset_conflicts_with_subject(
                        subject, f"https://example.test/wrong-{index}.jpg", wrong
                    )
                )

    def test_wikimedia_transcodes_share_a_content_identity(self) -> None:
        fetcher = HybridMediaFetcher()
        tiff = {
            "source": "wikimedia",
            "asset_title": "File:Map of Ruins at Chichen Itza 1935.tiff",
        }
        png = {
            "source": "wikimedia",
            "asset_title": "Map of Ruins at Chichen Itza 1935.PNG",
        }
        self.assertEqual(
            fetcher._asset_content_identity(tiff),
            fetcher._asset_content_identity(png),
        )

    def test_ancient_short_preflight_rejects_excess_fact_cards_before_render(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 9)]
        sources.extend(
            {
                "source": "local_fact_card",
                "url": f"local_fallback_{index}",
                "scene_index": index,
            }
            for index in range(9, 13)
        )

        issue = factory._ancient_visual_preflight_issue(ancient, sources, "short")

        self.assertEqual(
            issue,
            "Ancient Short contains 4 local fact-card fallback(s); maximum is 2",
        )

    def test_ancient_short_preflight_allows_one_late_fact_card_with_real_evidence(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 9)]
        sources.append(
            {
                "source": "local_fact_card",
                "url": "local_fallback_9",
                "scene_index": 9,
            }
        )

        self.assertIsNone(factory._ancient_visual_preflight_issue(ancient, sources, "short"))

    def test_visual_preflight_uses_the_expanded_six_candidate_pool(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        ranked = [(score,) for score in (95, 90, 85, 80)]

        ancient_pool = factory._visual_asset_candidates("ancient_history", "short", ranked)
        brain_pool = factory._visual_asset_candidates("brain_lens", "short", ranked)

        self.assertEqual(ancient_pool, ranked)
        self.assertEqual(brain_pool, ranked)

    def test_empty_visual_pool_is_recoverable_for_next_candidate(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = Mock()
        factory.image_fetcher.fetch.side_effect = [
            ([], [{"source": "local_fact_card"}]),
            ([Path("usable.jpg")], [{"source": "wikimedia_commons"}]),
        ]
        candidate = make_topic()

        first_images, first_sources, first_issue = factory._fetch_candidate_visuals(
            candidate,
            Path("first"),
            12,
            "documentary",
        )
        second_images, second_sources, second_issue = factory._fetch_candidate_visuals(
            candidate,
            Path("second"),
            12,
            "documentary",
        )

        self.assertEqual(first_images, [])
        self.assertEqual(first_sources, [])
        self.assertIn("not enough usable visuals", first_issue)
        self.assertEqual(second_images, [Path("usable.jpg")])
        self.assertEqual(second_sources, [{"source": "wikimedia_commons"}])
        self.assertEqual(second_issue, "")

    def test_ancient_short_tries_archive_rich_subject_before_sparse_axum(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        axum = make_topic()
        axum.subject = "Axum Obelisks"
        tikal = make_topic()
        tikal.subject = "Tikal"
        zimbabwe = make_topic()
        zimbabwe.subject = "Great Zimbabwe"
        ranked = [
            (90.0, {}, axum, []),
            (89.0, {}, tikal, []),
            (88.0, {}, zimbabwe, []),
        ]

        ordered = factory._visual_asset_candidates("ancient_history", "short", ranked)

        self.assertEqual(
            ["Great Zimbabwe", "Tikal", "Axum Obelisks"],
            [item[2].subject for item in ordered],
        )

    def test_ancient_recovery_sorts_full_pool_before_candidate_limit(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        ranked = []
        for index, subject in enumerate(
            [
                "Sogdian Merchants",
                "Nubian Pyramids",
                "Justinianic Plague",
                "Axum Obelisks",
                "Olmec Colossal Heads",
                "Boudica Revolt",
                "Lascaux",
            ]
        ):
            candidate = make_topic()
            candidate.subject = subject
            ranked.append((100.0 - index, {}, candidate, []))

        with patch.dict("os.environ", {"YT_CONTINUITY_RECOVERY": "1"}):
            ordered = factory._visual_asset_candidates("ancient_history", "short", ranked)

        subjects = [item[2].subject for item in ordered]
        # Recovery deliberately tries a short list of fresher packs instead of
        # burning the whole budget on sparse subjects while the gap widens.
        self.assertEqual(2, len(ordered))
        # Lascaux scores LAST of the seven inputs but is archive-rich. Its
        # survival is the point of the test: if the limit were applied before
        # the sort, the top-2 by score (Sogdian, Nubian) would win and Lascaux
        # could never appear.
        self.assertIn("Lascaux", subjects)
        # Nubian Pyramids is second by raw score yet archive-poor, so score
        # order alone must not decide the pack.
        self.assertNotIn("Nubian Pyramids", subjects)

    def test_stage_two_ancient_recovery_changes_archive_subject_priority(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        ranked = []
        for index, subject in enumerate(
            ["Boudica Revolt", "Lascaux", "Roman Concrete", "Mohenjo-Daro"]
        ):
            candidate = make_topic()
            candidate.subject = subject
            ranked.append((100.0 - index, {}, candidate, []))

        with patch.dict(
            "os.environ",
            {"YT_CONTINUITY_RECOVERY": "1", "YT_RECOVERY_STAGE": "2"},
        ):
            ordered = factory._visual_asset_candidates(
                "ancient_history",
                "short",
                ranked,
            )

        self.assertEqual("Mohenjo-Daro", ordered[0][2].subject)
        self.assertEqual("Roman Concrete", ordered[1][2].subject)

    def test_brain_short_repairs_relationship_ratio_with_verified_footage(self) -> None:
        fetcher = HybridMediaFetcher()
        candidate = make_topic()
        candidate.title = "Direct Interest: The Dating Signal That Removes Guesswork"
        candidate.subject = "Direct Interest"
        candidate.narration = "Dating interest becomes clearer through repeated behavior."
        candidate.narration_beats = ["Their message arrives on the phone."]
        backgrounds = [Path(f"raw_{index:02d}.mp4") for index in range(1, 13)]
        original_hook = backgrounds[0]
        sources = []
        for index in range(1, 13):
            relationship_asset = 2 <= index <= 8
            sources.append(
                {
                    "file": backgrounds[index - 1].name,
                    "source": "pexels_video",
                    "url": (
                        f"https://videos.example/couple-{index}.mp4"
                        if relationship_asset
                        else (
                            "https://videos.example/solo-smartphone-message.mp4"
                            if index == 1
                            else f"https://videos.example/solo-{index}.mp4"
                        )
                    ),
                    "source_page": f"https://www.pexels.com/video/{index}/",
                    "source_page_verified": "true",
                    "license": "Pexels license",
                    "asset_title": (
                        "adult romantic couple outdoors"
                        if relationship_asset
                        else (
                            "single adult reading a smartphone message"
                            if index == 1
                            else "single adult walking through an office"
                        )
                    ),
                    "scene_index": index,
                    "scene_text": (
                        "Their message arrives on the phone."
                        if index == 1
                        else "A visible dating behavior."
                    ),
                }
            )

        fetcher._repair_brain_short_relationship_backgrounds(
            candidate,
            backgrounds,
            sources,
            {},
        )

        relationship_count = sum(
            fetcher._is_brain_relationship_asset(str(item.get("url") or ""), item)
            for item in sources
        )
        self.assertGreaterEqual(relationship_count, 8)
        self.assertTrue(any(item.get("reused_for_scene") for item in sources))
        self.assertEqual(original_hook, backgrounds[0])
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = fetcher
        self.assertIsNone(factory._brain_visual_preflight_issue(candidate, sources))

    def test_brain_phone_hook_outranks_generic_plan_footage(self) -> None:
        fetcher = HybridMediaFetcher()
        scene = (
            "Their direct interest creates the next plan without forcing you "
            "to decode three days of vague messages."
        )

        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                scene,
                "https://www.pexels.com/video/emotional-conversation-in-domestic-setting/",
                {"asset_title": "Emotional conversation in a domestic setting"},
            )
        )
        self.assertTrue(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                scene,
                "https://www.pexels.com/video/couple-planning-with-smartphone/",
                {"asset_title": "Couple planning a date while reading a smartphone message"},
            )
        )

    def test_brain_long_preflight_rejects_weak_relationship_visual_ratio(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        candidate = make_topic()
        candidate.content_kind = "video"
        candidate.title = "Social Media Jealousy"
        candidate.subject = "Social Media Jealousy"
        candidate.narration = "A relationship can feel threatened by incomplete online signals."
        sources = [
            {
                "url": f"https://videos.example/couple-{index}.mp4",
                "asset_title": "young adult couple relationship conversation",
                "artist": f"artist-{index}",
                "scene_index": index,
            }
            for index in range(1, 12)
        ]
        sources.extend(
            {
                "url": f"https://videos.example/solo-{index}.mp4",
                "asset_title": "single adult using a phone alone",
                "artist": f"solo-{index}",
                "scene_index": index,
            }
            for index in range(12, 21)
        )

        issue = factory._brain_visual_preflight_issue(candidate, sources)

        self.assertEqual(
            issue,
            "Brain Lens relationship topic has only 11/20 clearly relevant relationship visuals; needs at least 14",
        )
        for item in sources[11:14]:
            item["asset_title"] = "young adult couple discussing a phone message relationship"
        varied_relationship_visuals = (
            "adult romantic couple walking outdoors holding hands",
            "adult romantic couple hugging after a date",
            "adult romantic couple dancing together",
            "adult romantic couple sharing a warm embrace",
            "adult couple having a relationship conversation",
            "adult couple discussing boundaries at home",
            "adult couple talking calmly over coffee",
            "adult couple having a serious conversation",
            "adult couple arguing about a relationship",
            "adult couple sitting apart after conflict",
            "adult couple resolving a disagreement",
            "adult couple reading a phone message together",
            "adult couple discussing a text message",
            "adult couple planning a date on a smartphone",
        )
        for item, title in zip(sources[:14], varied_relationship_visuals):
            item["asset_title"] = title
        self.assertIsNone(factory._brain_visual_preflight_issue(candidate, sources))

    def test_brain_long_preflight_rejects_friend_only_relationship_opener(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        candidate = make_topic()
        candidate.content_kind = "video"
        candidate.title = "Friends With Benefits: When Chemistry Outruns Clarity"
        candidate.subject = "Friends With Benefits Boundaries"
        candidate.narration = "Chemistry can feel clear even when the relationship agreement is not."
        sources = [
            {
                "url": "https://videos.example/friends-at-a-bar.mp4",
                "asset_title": "a man having a conversation with his friend at a bar",
                "artist": "friend-opener",
                "scene_index": 1,
            }
        ]
        sources.extend(
            {
                "url": f"https://videos.example/couple-{index}.mp4",
                "asset_title": "adult romantic couple having a relationship conversation",
                "artist": f"couple-{index}",
                "scene_index": index,
            }
            for index in range(2, 21)
        )

        self.assertEqual(
            factory._brain_visual_preflight_issue(candidate, sources),
            "Brain Lens relationship long-video hook lacks a clearly relevant relationship visual",
        )

    def test_brain_long_preflight_rejects_repeated_conversation_footage(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        candidate = make_topic()
        candidate.content_kind = "video"
        candidate.title = "Almost Relationships: When Intensity Replaces Clarity"
        candidate.subject = "Almost Relationships"
        candidate.narration = "A relationship can feel intense before the evidence is clear."
        sources = [
            {
                "url": f"https://videos.example/conversation-{index}.mp4",
                "asset_title": "adult couple having a relationship conversation",
                "artist": f"conversation-{index}",
                "scene_index": index,
            }
            for index in range(1, 12)
        ]
        sources.extend(
            {
                "url": f"https://videos.example/activity-{index}.mp4",
                "asset_title": "adult romantic couple cooking walking or planning a date",
                "artist": f"activity-{index}",
                "scene_index": index,
            }
            for index in range(12, 21)
        )

        self.assertEqual(
            factory._brain_visual_preflight_issue(candidate, sources),
            "Brain Lens repeats conversation footage in 11/20 visual sources",
        )

    def test_brain_hook_motion_guard_matches_final_render_threshold(self) -> None:
        fetcher = HybridMediaFetcher.__new__(HybridMediaFetcher)

        def frame(position: int) -> Image.Image:
            image = Image.new("RGB", (160, 90), "black")
            image.paste("white", (position, 10, min(159, position + 32), 80))
            return image

        static = [frame(20), frame(20), frame(20)]
        with patch.object(fetcher, "_extract_video_frame", side_effect=static):
            self.assertFalse(fetcher._hook_video_has_motion(Path("static-hook.mp4")))

        moving = [frame(8), frame(48), frame(96)]
        with patch.object(fetcher, "_extract_video_frame", side_effect=moving):
            self.assertTrue(fetcher._hook_video_has_motion(Path("moving-hook.mp4")))

    def test_brain_short_preflight_rejects_ai_generated_visual(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        topic = make_topic()
        topic.content_kind = "short"
        topic.subject = "Reply Time Anxiety"
        topic.title = "Reply Time Anxiety: What the Pattern Actually Means"
        sources = [
            {
                "source": "pexels_video",
                "url": f"https://videos.pexels.com/couple-phone-{index}.mp4",
                "asset_title": "adult couple discussing a phone reply",
                "scene_index": index,
            }
            for index in range(1, 12)
        ]
        sources.append(
            {
                "source": "pollinations_ai",
                "url": "pollinations.ai/generated",
                "asset_title": "generated anxious portrait",
                "scene_index": 12,
            }
        )

        self.assertEqual(
            factory._brain_visual_preflight_issue(topic, sources),
            "Brain Lens Short contains 1 AI-generated visual(s); zero are allowed",
        )

    def test_brain_long_preflight_allows_one_late_ai_generated_visual(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        topic = make_topic()
        topic.content_kind = "video"
        topic.subject = "Planning Initiative"
        topic.title = "Planning Initiative: How Shared Effort Makes Dating Clearer"
        sources = [
            {
                "source": "pexels_video",
                "url": f"https://videos.pexels.com/couple-date-{index}.mp4",
                "asset_title": "adult romantic couple walking together on a date",
                "scene_index": index,
            }
            for index in range(1, 5)
        ]
        sources.append(
            {
                "source": "pollinations_ai",
                "url": "pollinations.ai/generated",
                "asset_title": "adult romantic couple planning a respectful date",
                "scene_index": 5,
            }
        )

        self.assertIsNone(factory._brain_visual_preflight_issue(topic, sources))

    def test_brain_long_preflight_rejects_ai_generated_opening(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        topic = make_topic()
        topic.content_kind = "video"
        topic.subject = "Planning Initiative"
        topic.title = "Planning Initiative: How Shared Effort Makes Dating Clearer"
        sources = [
            {
                "source": "pollinations_ai" if index == 1 else "pexels_video",
                "url": f"https://videos.example/couple-date-{index}.mp4",
                "asset_title": "adult romantic couple planning a respectful date",
                "scene_index": index,
            }
            for index in range(1, 6)
        ]

        self.assertEqual(
            factory._brain_visual_preflight_issue(topic, sources),
            "Brain Lens long video uses an AI-generated visual in the opening",
        )

    def test_brain_long_preflight_rejects_multiple_ai_generated_visuals(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        topic = make_topic()
        topic.content_kind = "video"
        topic.subject = "Planning Initiative"
        topic.title = "Planning Initiative: How Shared Effort Makes Dating Clearer"
        sources = [
            {
                "source": "pollinations_ai" if index in {5, 6} else "pexels_video",
                "url": f"https://videos.example/couple-date-{index}.mp4",
                "asset_title": "adult romantic couple planning a respectful date",
                "scene_index": index,
            }
            for index in range(1, 7)
        ]

        self.assertEqual(
            factory._brain_visual_preflight_issue(topic, sources),
            "Brain Lens long video contains 2 AI-generated visual(s); maximum is 1",
        )

    def test_brain_short_preflight_allows_one_late_fact_card(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        topic = make_topic()
        topic.content_kind = "short"
        topic.subject = "Reply Time Anxiety"
        topic.title = "Reply Time Anxiety: What the Pattern Actually Means"
        sources = [
            {
                "source": "pexels_video",
                "url": f"https://videos.pexels.com/couple-phone-{index}.mp4",
                "asset_title": "adult couple discussing a phone reply",
                "scene_index": index,
            }
            for index in range(1, 12)
        ]
        sources.append(
            {
                "source": "local_fact_card",
                "url": "local_fallback",
                "asset_title": "dating pattern summary",
                "scene_index": 12,
            }
        )

        self.assertIsNone(factory._brain_visual_preflight_issue(topic, sources))

    def test_brain_short_preflight_rejects_opening_fact_card(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        topic = make_topic()
        topic.content_kind = "short"
        topic.subject = "Reply Time Anxiety"
        topic.title = "Reply Time Anxiety: What the Pattern Actually Means"
        sources = [
            {
                "source": "local_fact_card",
                "url": "local_fallback",
                "asset_title": "dating pattern summary",
                "scene_index": 1,
            },
            *[
                {
                    "source": "pexels_video",
                    "url": f"https://videos.pexels.com/couple-phone-{index}.mp4",
                    "asset_title": "adult couple discussing a phone reply",
                    "scene_index": index,
                }
                for index in range(2, 13)
            ],
        ]

        self.assertEqual(
            factory._brain_visual_preflight_issue(topic, sources),
            "Brain Lens Short uses a local fact-card fallback in the opening",
        )

    def test_brain_long_preflight_rejects_any_generated_or_fallback_visual(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        candidate = make_topic()
        candidate.content_kind = "video"
        candidate.subject = "Ghosting Recovery"
        candidate.title = "Why Ghosting Keeps Your Mind Searching for an Ending"
        sources = [
            {
                "source": "pexels_video",
                "url": f"https://videos.pexels.com/couple-phone-{index}.mp4",
                "asset_title": "adult couple discussing a phone message",
                "scene_index": index,
            }
            for index in range(1, 20)
        ]
        sources.append(
            {
                "source": "local_fact_card",
                "url": "local_fallback",
                "asset_title": "generic fallback card",
                "scene_index": 20,
            }
        )

        self.assertEqual(
            factory._brain_visual_preflight_issue(candidate, sources),
            "Brain Lens long video contains 1 local fact-card fallback(s); maximum is 0",
        )

    def test_brain_short_reuse_pool_requires_two_real_sources_and_scene_match(self) -> None:
        fetcher = HybridMediaFetcher()
        backgrounds = [Path(f"clip-{index}.mp4") for index in range(1, 5)]
        manifest = [
            {
                "file": path.name,
                "source": "pexels_video",
                "url": f"https://videos.pexels.com/{path.name}",
                "source_page": f"https://www.pexels.com/video/{index}/",
                "source_page_verified": "true",
                "license": "Pexels license",
                "asset_title": "adult couple discussing a phone message in conversation",
            }
            for index, path in enumerate(backgrounds, start=1)
        ]

        pool = fetcher._brain_short_verified_reuse_pool(
            "You wait for a reply on the phone",
            backgrounds,
            manifest,
            {},
        )
        self.assertEqual([path.name for path, _ in pool], [path.name for path in backgrounds])

        topic_only_manifest = [dict(item, asset_title="thoughtful adult portrait outdoors") for item in manifest]
        topic_only_pool = fetcher._brain_short_verified_reuse_pool(
            "You wait for a reply on the phone",
            backgrounds,
            topic_only_manifest,
            {},
        )
        self.assertEqual(
            [path.name for path, _ in topic_only_pool],
            [path.name for path in backgrounds],
        )

        below_floor = fetcher._brain_short_verified_reuse_pool(
            "You wait for a reply on the phone",
            backgrounds[:1],
            manifest[:1],
            {},
        )
        self.assertEqual(below_floor, [])

        early_pool = fetcher._brain_short_verified_reuse_pool(
            "You wait for a reply on the phone",
            backgrounds[:2],
            manifest[:2],
            {},
        )
        self.assertEqual([path.name for path, _ in early_pool], [path.name for path in backgrounds[:2]])

    def test_brain_long_reuse_is_scene_matched_and_rejects_finance_stock(self) -> None:
        fetcher = HybridMediaFetcher()
        backgrounds = [Path(f"clip-{index}.mp4") for index in range(1, 7)]
        manifest = []
        for index, path in enumerate(backgrounds, start=1):
            title = (
                "adult couple talking after a phone argument"
                if index < 6
                else "couple talking to a mortgage broker at a financial meeting"
            )
            manifest.append(
                {
                    "file": path.name,
                    "source": "pexels_video",
                    "url": f"https://videos.pexels.com/{path.name}",
                    "source_page": f"https://www.pexels.com/video/{index}/",
                    "source_page_verified": "true",
                    "license": "Pexels license",
                    "asset_title": title,
                }
            )

        pool = fetcher._brain_long_verified_reuse_pool(
            "Do not perform indifference or post for a reaction after the false power move.",
            backgrounds,
            manifest,
            {},
        )

        self.assertEqual(len(pool), 5)
        self.assertFalse(any("mortgage" in item[1]["asset_title"] for item in pool))
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                "Do not perform indifference or post for a reaction.",
                manifest[-1]["url"],
                manifest[-1],
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                "Regulate before reacting and keep the conversation calm.",
                "https://www.pexels.com/photo/heated-discussion/",
                {"asset_title": "A man and woman having a heated discussion"},
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                "Write observable relationship facts in one column and predictions in the other.",
                "https://www.pexels.com/video/couple-computing-their-expenses/",
                {"asset_title": "A couple computing their expenses"},
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                "A charged reunion can feel like resolution, but repair asks for more and both people must name what happened.",
                "https://www.pexels.com/video/man-writing-a-diary/",
                {"asset_title": "Man writing diary while his wife reads a book in the background"},
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                "Read the relationship answer through words, specificity, and follow-through.",
                "https://pixabay.com/photo/calendar/",
                {"asset_title": "calendar, business, office, deadline, weekly schedule"},
            )
        )
        self.assertFalse(
            fetcher._asset_matches_scene_intent(
                "brain_lens",
                "Listen to the words, the specificity, and the follow-through. Grade the pattern, not tone alone.",
                "https://www.pexels.com/video/man-and-woman-looking-at-a-paper-7577973/",
                {"asset_title": "Man and woman looking at a paper"},
            )
        )
        self.assertIn("pexels_video:7577973", fetcher.BRAIN_LENS_BLOCKED_ASSET_IDS)

    def test_brain_long_repairs_early_generated_card_from_verified_real_pool(self) -> None:
        fetcher = HybridMediaFetcher()
        candidate = make_topic()
        candidate.content_kind = "video"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = root / "raw_01_fallback_card.jpg"
            fallback.write_bytes(b"fallback")
            real_paths = []
            manifest = [
                {
                    "file": fallback.name,
                    "source": "local_fact_card",
                    "url": "local_fallback",
                    "license": "Generated in-app fallback",
                    "scene_index": 1,
                }
            ]
            for index in range(1, 7):
                path = root / f"real_{index}.mp4"
                path.write_bytes(b"real")
                real_paths.append(path)
                manifest.append(
                    {
                        "file": path.name,
                        "source": "pexels_video",
                        "url": f"https://videos.pexels.com/real-{index}.mp4",
                        "source_page": f"https://www.pexels.com/video/real-{index}/",
                        "source_page_verified": "true",
                        "license": "Pexels License",
                        "asset_title": "adult couple having a calm relationship conversation",
                        "scene_index": index + 1,
                    }
                )
            backgrounds = [fallback, *real_paths]
            reuse_counts: dict[str, int] = {}

            fetcher._repair_brain_long_generated_backgrounds(
                candidate,
                backgrounds,
                manifest,
                reuse_counts,
            )

            self.assertIn(backgrounds[0], real_paths)
            self.assertFalse(fallback.exists())
            repaired = next(item for item in manifest if item["scene_index"] == 1)
            self.assertEqual(repaired["source"], "pexels_video")
            self.assertTrue(repaired["reused_for_scene"])
            self.assertEqual(reuse_counts[backgrounds[0].name], 1)

    def test_ancient_short_preflight_requires_minimum_unique_verified_real_sources(self) -> None:
        # Derived from the constant rather than hard-coded: the threshold is a
        # tunable editorial dial, and this test is about the shortfall being
        # reported, not about any particular value of it.
        threshold = ShortsFactory.ANCIENT_SHORT_MIN_VERIFIED_REAL_VISUALS
        shortfall = threshold - 1
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [
            self._verified_ancient_source(index) for index in range(1, shortfall + 1)
        ]
        # Generated diagrams must not count toward the verified-real quota.
        sources.extend(
            {
                "source": "local_documentary_diagram",
                "url": f"local_diagram_{index}",
                "scene_index": index,
            }
            for index in range(shortfall + 1, shortfall + 6)
        )

        issue = factory._ancient_visual_preflight_issue(ancient, sources, "short")

        self.assertEqual(
            issue,
            f"only {shortfall} unique verified real Ancient visuals; "
            f"needs at least {threshold}",
        )

    def test_ancient_short_preflight_accepts_eight_relevant_verified_sources(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 9)]
        sources.extend(
            {
                "source": "local_documentary_diagram",
                "url": f"local_diagram_{index}",
                "scene_index": index,
            }
            for index in range(9, 13)
        )

        issue = factory._ancient_visual_preflight_issue(ancient, sources, "short")

        self.assertIsNone(issue)

    def test_ancient_long_preflight_rejects_any_fact_card(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 8)]
        sources.extend(
            {
                "source": "local_documentary_diagram",
                "url": f"local_documentary_diagram:sogdian:{index}",
                "scene_index": index,
            }
            for index in range(8, 20)
        )
        sources.append(
            {
                "source": "local_fact_card",
                "url": "local_fallback:sogdian:20",
                "scene_index": 20,
            }
        )

        issue = factory._ancient_visual_preflight_issue(ancient, sources, "video")

        self.assertEqual(
            issue,
            "Ancient long video contains 1 local fact-card fallback(s); zero are allowed",
        )

    def test_ancient_long_counts_only_licensed_verified_real_sources(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 8)]
        sources[-1]["license"] = "unknown"
        sources.extend(
            {
                "source": "local_documentary_diagram",
                "url": f"local_documentary_diagram:sogdian:{index}",
                "scene_index": index,
            }
            for index in range(8, 21)
        )

        issue = factory._ancient_visual_preflight_issue(ancient, sources, "video")

        self.assertEqual(issue, "only 6 unique real historical visuals; needs at least 7")

    def test_ancient_long_rejects_more_than_two_reused_source_scenes(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 21)]
        for item in sources[-3:]:
            item["reused_for_scene"] = True

        issue = factory._ancient_visual_preflight_issue(
            ancient,
            sources,
            "video",
            source_visual_qa={"dark_real_sample_ratio": 0.0},
        )

        self.assertEqual(issue, "Ancient long video reuses 3 source scenes; maximum is 2")

    def test_ancient_long_rejects_underexposed_real_source_contact_sheet(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 21)]

        issue = factory._ancient_visual_preflight_issue(
            ancient,
            sources,
            "video",
            source_visual_qa={"dark_real_sample_ratio": 0.2},
        )

        self.assertEqual(
            issue,
            "real historical source frames are too dark (20%); maximum is 15%",
        )

    def test_ancient_short_preflight_rejects_even_one_off_subject_real_source(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        ancient = make_topic()
        ancient.niche_id = "ancient_history"
        ancient.title = "How Sogdian Merchants Connected Silk Road Cities"
        ancient.subject = "sogdian merchants"
        sources = [self._verified_ancient_source(index) for index in range(1, 9)]
        sources.append(self._verified_ancient_source(9, relevant=False))

        issue = factory._ancient_visual_preflight_issue(ancient, sources, "short")

        self.assertEqual(
            issue,
            "1 verified real Ancient Short visual(s) do not clearly match the selected subject",
        )

    def test_underexposed_ancient_background_is_lifted_for_readability(self) -> None:
        fetcher = HybridMediaFetcher()
        dark = Image.new("RGB", (720, 1280), (28, 22, 16))

        lifted = fetcher._lift_underexposed_ancient_background(dark)
        mean_luma, near_black = fetcher._frame_luma_stats(lifted)

        self.assertGreaterEqual(mean_luma, 68.0)
        self.assertEqual(near_black, 0.0)

    def test_signed_urls_share_a_stable_identity(self) -> None:
        fetcher = HybridMediaFetcher()
        left = fetcher._stable_asset_id("https://cdn.example/photo.jpg?token=one")
        right = fetcher._stable_asset_id("https://cdn.example/photo.jpg?token=two")
        self.assertEqual(left, right)

    def test_perceptual_hash_detects_near_duplicate(self) -> None:
        fetcher = HybridMediaFetcher()
        image = Image.new("RGB", (80, 80), "navy")
        almost_same = image.copy()
        almost_same.putpixel((5, 5), (1, 1, 1))
        self.assertTrue(fetcher._near_duplicate_hash(fetcher._dhash(almost_same), {fetcher._dhash(image)}))

    def test_scene_intent_rejects_known_axum_priest_mismatch(self) -> None:
        fetcher = HybridMediaFetcher()
        priest = {"asset_title": "Ethiopian Orthodox priest in Axum", "source_page": "https://pixabay.com/photos/123/"}
        obelisk = {"asset_title": "Obelisk of Axum funerary stele", "source_page": "https://commons.wikimedia.org/wiki/File:Axum.jpg"}
        scene = "The obelisks of Axum marked elite tombs and displayed engineering power"
        self.assertFalse(fetcher._asset_matches_scene_intent("ancient_history", scene, "", priest))
        self.assertTrue(fetcher._asset_matches_scene_intent("ancient_history", scene, "", obelisk))

    def test_scene_intent_rejects_kissing_clip_for_voice_change(self) -> None:
        fetcher = HybridMediaFetcher()
        kissing = {"asset_title": "close-up of a kissing couple"}
        talking = {"asset_title": "couple talking during a face-to-face conversation"}
        scene = "Their voice softens when they speak to you"
        self.assertFalse(fetcher._asset_matches_scene_intent("brain_lens", scene, "", kissing))
        self.assertTrue(fetcher._asset_matches_scene_intent("brain_lens", scene, "", talking))

    def test_regulation_scene_rejects_worried_phone_footage(self) -> None:
        fetcher = HybridMediaFetcher()
        scene = "A calmer body gives judgment room. Regulate before you investigate and lengthen the exhale."
        worried = {"asset_title": "A worried couple looking at the phone"}
        breathing = {"asset_title": "Young adult practicing calm breathing outdoors"}

        self.assertFalse(fetcher._asset_matches_scene_intent("brain_lens", scene, "", worried))
        self.assertTrue(fetcher._asset_matches_scene_intent("brain_lens", scene, "", breathing))

    def test_self_worth_scene_rejects_romantic_couple_substitution(self) -> None:
        fetcher = HybridMediaFetcher()
        scene = "Their uncertainty may hurt without proving you are not attractive enough or impossible to love. Keep the event separate from your worth."
        romantic = {"asset_title": "Romantic couple sharing tender moments outdoors"}
        friends = {"asset_title": "Young adult reconnecting with supportive friends after a setback"}

        self.assertFalse(fetcher._asset_matches_scene_intent("brain_lens", scene, "", romantic))
        self.assertTrue(fetcher._asset_matches_scene_intent("brain_lens", scene, "", friends))

    def test_micro_flirting_ambiguity_scene_rejects_overt_or_unrelated_footage(self) -> None:
        fetcher = HybridMediaFetcher()
        scene = "Friendliness can look similar, so treat one cue as a question rather than proof."
        acceptable = {"asset_title": "adult couple talking at a cafe during a friendly conversation"}
        rejected = (
            {"asset_title": "shirtless couple embracing in a bedroom"},
            {"asset_title": "passionate kissing couple close up"},
            {"asset_title": "couple wearing virtual reality headsets while gaming"},
            {"asset_title": "couple singing into a microphone at a nightclub"},
        )
        self.assertTrue(fetcher._asset_matches_scene_intent("brain_lens", scene, "", acceptable))
        for asset in rejected:
            with self.subTest(asset=asset["asset_title"]):
                self.assertFalse(fetcher._asset_matches_scene_intent("brain_lens", scene, "", asset))

    def test_emotional_safety_scenes_reject_overt_affection_without_an_explicit_intimacy_claim(self) -> None:
        fetcher = HybridMediaFetcher()
        scene = "Ask whether their actions matched and whether you felt clearer afterward."
        rejected = (
            {"asset_title": "A man kissing a woman on the forehead"},
            {"asset_title": "Romantic couple sharing an intimate moment indoors"},
            {"asset_title": "Extreme close-up of a woman kissing her partner"},
            {"asset_title": "A couple reflecting love and intimacy"},
        )
        acceptable = {"asset_title": "Adult couple talking through plans at a table with a calendar"}

        self.assertTrue(fetcher._asset_matches_scene_intent("brain_lens", scene, "", acceptable))
        for asset in rejected:
            with self.subTest(asset=asset["asset_title"]):
                self.assertFalse(fetcher._asset_matches_scene_intent("brain_lens", scene, "", asset))

    def test_brain_short_media_search_uses_full_narration_not_truncated_visual_text(self) -> None:
        fetcher = RecordingFetcher()
        topic = make_topic()
        topic.subject = "Emotional Safety in Dating"
        topic.title = "Emotional Safety in Dating: The Pattern to Watch"
        topic.narration = (
            "You stop measuring promises and notice whether the pace, effort, and care are actually mutual."
        )
        topic.scene_plan = [
            ScenePlanItem(
                narration=topic.narration,
                visual_text="You stop measuring promises",
                search_terms=[],
            )
        ]
        manifest: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            fetcher._fetch_scene_backgrounds(
                topic,
                Path(tmp),
                ["You stop measuring promises"],
                manifest,
                media_memory={},
            )

        self.assertEqual(
            fetcher.queries[0],
            "adult couple respectful mutual conversation eye contact daytime",
        )

    def test_brain_long_phone_hook_prioritizes_scene_specific_opening_search(self) -> None:
        fetcher = RecordingFetcher()
        topic = make_topic()
        topic.content_kind = "video"
        topic.subject = "Ghosting Recovery"
        topic.title = "Why You Keep Checking Your Phone After Being Ghosted"
        topic.narration = (
            "They disappear, the phone stays silent, and your mind drafts the missing answer."
        )
        topic.scene_plan = [
            ScenePlanItem(
                narration=topic.narration,
                visual_text="The silence that starts the story",
                search_terms=[
                    "young adult couple emotional distance ignored phone message relationship",
                    "young adult couple emotional relationship body language",
                ],
            )
        ]
        topic.narration_beats = [topic.narration]

        with tempfile.TemporaryDirectory() as tmp:
            fetcher._fetch_scene_backgrounds(
                topic,
                Path(tmp),
                ["The silence that starts the story"],
                [],
                media_memory={},
            )

        self.assertEqual(
            fetcher.queries[0],
            "young adult couple emotional distance ignored phone message relationship",
        )

    def test_micro_flirting_short_queries_follow_each_spoken_behavior(self) -> None:
        fetcher = HybridMediaFetcher()
        cases = {
            "They lean in, mirror your smile, and keep the conversation going.":
                "adult couple subtle flirting cafe eye contact smiling daytime",
            "Friendliness can look similar, so treat one cue as a question rather than proof.":
                "adult couple casual cafe conversation friendly body language daytime",
            "Look for several returned signals across the conversation instead of one charged second.":
                "adult couple respectful mutual conversation eye contact daytime",
            "Reciprocity, comfort, and clear consent matter more than one charged cue.":
                "adult couple respectful mutual conversation eye contact daytime",
            "Ask whether it repeated, whether their actions matched, and whether you felt clearer afterward.":
                "adult couple reviewing plans together at a table calendar conversation daytime",
        }
        for scene, expected in cases.items():
            with self.subTest(scene=scene):
                query = fetcher._brain_short_relationship_context_query(scene)
                self.assertEqual(expected, query)
                self.assertIn("adult couple", query)
                self.assertNotIn("gaming", query)
                self.assertNotIn("music", query)
                self.assertNotIn("kissing", query)

    def test_short_query_candidates_are_capped(self) -> None:
        fetcher = RecordingFetcher()
        topic = make_topic(search_terms=[f"candidate {index}" for index in range(10)])
        with tempfile.TemporaryDirectory() as tmp:
            fetcher._fetch_scene_backgrounds(topic, Path(tmp), [topic.scene_plan[0].visual_text], [], media_memory={})
        self.assertEqual(
            fetcher.queries,
            [
                "adult couple face to face conversation speaking and listening daytime",
                "candidate 0",
                "candidate 1",
                "candidate 2",
            ],
        )

    def test_expired_fetch_budget_uses_offline_fallback_without_search(self) -> None:
        fetcher = RecordingFetcher()
        fetcher.short_fetch_budget_seconds = -0.01
        topic = make_topic(search_terms=["candidate one", "candidate two"])
        with tempfile.TemporaryDirectory() as tmp:
            backgrounds = fetcher._fetch_scene_backgrounds(
                topic, Path(tmp), [topic.scene_plan[0].visual_text], [], media_memory={}
            )
            self.assertEqual(fetcher.queries, [])
            self.assertIsNotNone(backgrounds[0])
            self.assertTrue(backgrounds[0].is_file())

    def test_ancient_short_does_not_reuse_one_source_across_scenes(self) -> None:
        fetcher = RecordingFetcher()
        topic = make_topic(search_terms=["Axum obelisk"])
        topic.niche_id = "ancient_history"
        topic.subject = "Obelisk of Axum"
        topic.scene_plan = []
        topic.image_queries = ["Axum obelisk"]
        manifest: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            backgrounds = fetcher._fetch_scene_backgrounds(
                topic,
                Path(tmp),
                ["The Obelisk of Axum marked elite tombs", "False doors and windows decorate the stele"],
                manifest,
                media_memory={},
            )
            self.assertEqual(len({path.name for path in backgrounds if path is not None}), 2)
            self.assertEqual(sum(1 for item in manifest if item.get("source") == "local_fact_card"), 2)

    def test_ancient_short_reuses_verified_assets_once_after_eight_unique_sources(self) -> None:
        fetcher = BoundedAncientSourceFetcher(successful_scenes=8)
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.subject = "Tikal"
        topic.title = "Inside Tikal: Maya Kings, Reservoirs, and Jungle Temples"
        topic.scene_plan = []
        topic.image_queries = ["Tikal Guatemala Maya Great Plaza"]
        manifest: list[dict] = []
        scene_texts = [f"Tikal Maya temple and stela scene {index}" for index in range(1, 13)]

        with tempfile.TemporaryDirectory() as tmp:
            backgrounds = fetcher._fetch_scene_backgrounds(
                topic,
                Path(tmp),
                scene_texts,
                manifest,
                media_memory={},
            )

        names = [path.name for path in backgrounds if path is not None]
        self.assertEqual(len(names), 12)
        self.assertEqual(len(set(names)), 8)
        self.assertLessEqual(max(Counter(names).values()), 2)
        self.assertEqual(sum(bool(item.get("reused_for_scene")) for item in manifest), 4)
        self.assertFalse(any(item.get("source") == "local_fact_card" for item in manifest))

    def test_ancient_short_stops_after_two_fail_closed_cards_below_eight_unique_sources(self) -> None:
        fetcher = BoundedAncientSourceFetcher(successful_scenes=7)
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.subject = "Tikal"
        topic.title = "Inside Tikal: Maya Kings, Reservoirs, and Jungle Temples"
        topic.scene_plan = []
        topic.image_queries = ["Tikal Guatemala Maya Great Plaza"]
        manifest: list[dict] = []
        scene_texts = [f"Tikal Maya temple and stela scene {index}" for index in range(1, 13)]

        with tempfile.TemporaryDirectory() as tmp:
            backgrounds = fetcher._fetch_scene_backgrounds(
                topic,
                Path(tmp),
                scene_texts,
                manifest,
                media_memory={},
            )

        self.assertEqual(len(backgrounds), 9)
        self.assertEqual(sum(1 for item in manifest if item.get("source") == "local_fact_card"), 2)
        self.assertFalse(any(item.get("reused_for_scene") for item in manifest))

    def test_source_contact_sheet_samples_moving_video(self) -> None:
        fetcher = PreviewFetcher()
        topic = make_topic()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            video.write_bytes(b"not decoded because preview is mocked")
            report = fetcher._source_visual_qa([video], root / "contact.jpg", topic)
            self.assertEqual(report["video_sources"], 1)
            self.assertEqual(report["sampled_frames"], 3)
            self.assertTrue((root / "contact.jpg").is_file())
            self.assertTrue((root / "source_visual_qa.json").is_file())

    def test_source_contact_sheet_excludes_dark_documentary_diagrams_from_real_ratio(self) -> None:
        fetcher = HybridMediaFetcher()
        topic = make_topic()
        topic.niche_id = "ancient_history"
        topic.content_kind = "video"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diagram = root / "scene_01.jpg"
            archive = root / "scene_02.jpg"
            Image.new("RGB", (640, 360), (8, 8, 8)).save(diagram)
            Image.new("RGB", (640, 360), (120, 110, 100)).save(archive)
            report = fetcher._source_visual_qa(
                [diagram, archive],
                root / "contact.jpg",
                topic,
                source_manifest=[
                    {"scene_index": 1, "source": "local_documentary_diagram"},
                    {"scene_index": 2, "source": "wikimedia"},
                ],
            )

        self.assertEqual(report["dark_sample_ratio"], 0.5)
        self.assertEqual(report["dark_real_sample_ratio"], 0.0)
        self.assertEqual(report["real_sampled_frames"], 1)

    def test_final_visual_qa_samples_caption_and_timeline_midpoints(self) -> None:
        fetcher = PreviewFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "short.mp4"
            video.write_bytes(b"mock video")
            (root / "subtitles.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nCaption one\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\nCaption two\n",
                encoding="utf-8",
            )
            (root / "visual_timeline.json").write_text(
                json.dumps([{"start": 0.0, "end": 3.0}, {"start": 3.0, "end": 6.0}]),
                encoding="utf-8",
            )

            report = fetcher.write_final_visual_qa(video, max_samples=20)

            self.assertEqual(report["caption_midpoints_requested"], 2)
            self.assertEqual(report["visual_midpoints_requested"], 2)
            self.assertGreaterEqual(report["decoded_samples"], 6)
            self.assertTrue((root / "frame_check" / "final_contact_sheet.jpg").is_file())
            self.assertTrue((root / "frame_check" / "final_visual_qa.json").is_file())

    def test_final_visual_qa_does_not_seek_past_last_decodable_frame(self) -> None:
        fetcher = PreviewFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "short.mp4"
            video.write_bytes(b"mock video")

            with (
                patch.object(fetcher, "_probe_video", return_value=(1080, 1920, 6.52)),
                patch.object(
                    fetcher,
                    "_extract_video_frame",
                    side_effect=lambda path, timestamp=0.5, timeout=10.0: (
                        None
                        if timestamp > 6.45
                        else Image.new("RGB", (108, 192), (90, 120, 160))
                    ),
                ),
            ):
                report = fetcher.write_final_visual_qa(video, max_samples=20)

        self.assertEqual(report["requested_samples"], report["decoded_samples"])
        self.assertFalse(any("failed to decode" in issue for issue in report["issues"]))

    def test_final_visual_qa_retries_a_transient_frame_decode(self) -> None:
        fetcher = PreviewFetcher()
        attempts = {"failed_once": False}

        def transient_decode(path, timestamp=0.5, timeout=10.0):
            if abs(timestamp - 2.5) < 0.001 and not attempts["failed_once"]:
                attempts["failed_once"] = True
                return None
            return Image.new("RGB", (108, 192), (90, 120, 160))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "short.mp4"
            video.write_bytes(b"mock video")
            with patch.object(fetcher, "_extract_video_frame", side_effect=transient_decode):
                report = fetcher.write_final_visual_qa(video, max_samples=20)

        self.assertTrue(attempts["failed_once"])
        self.assertGreaterEqual(report["decode_retry_attempts"], 1)
        self.assertEqual(report["requested_samples"], report["decoded_samples"])
        self.assertFalse(any("failed to decode" in issue for issue in report["issues"]))

    def test_background_freeze_scan_parses_full_duration_intervals(self) -> None:
        fetcher = HybridMediaFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"0" * 100_001)
            completed = Mock(
                returncode=0,
                stderr=(
                    b"lavfi.freezedetect.freeze_start: 12.366667\n"
                    b"lavfi.freezedetect.freeze_duration: 8.100000\n"
                    b"lavfi.freezedetect.freeze_end: 20.466667\n"
                ),
            )
            with patch("yt_auto.images.subprocess.run", return_value=completed) as run:
                report = fetcher._background_freeze_scan(video, 30.0)

        self.assertEqual("pass", report["status"])
        self.assertEqual(
            [{"start": 12.367, "end": 20.467, "duration": 8.1}],
            report["events"],
        )
        command = run.call_args.args[0]
        self.assertIn(
            "crop=iw:ih*0.55:0:ih*0.08",
            next(command[index + 1] for index, value in enumerate(command) if value == "-vf"),
        )

    def test_background_freeze_scan_closes_a_trailing_eof_freeze(self) -> None:
        fetcher = HybridMediaFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"0" * 100_001)
            with patch(
                "yt_auto.images.subprocess.run",
                return_value=Mock(
                    returncode=0,
                    stderr=b"lavfi.freezedetect.freeze_start: 8.000000\n",
                ),
            ):
                report = fetcher._background_freeze_scan(video, 12.0)

        self.assertEqual(
            [{"start": 8.0, "end": 12.0, "duration": 4.0}],
            report["events"],
        )

    def test_background_freeze_scan_rejects_malformed_event_order(self) -> None:
        fetcher = HybridMediaFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"0" * 100_001)
            with patch(
                "yt_auto.images.subprocess.run",
                return_value=Mock(
                    returncode=0,
                    stderr=(
                        b"lavfi.freezedetect.freeze_start: 2.000000\n"
                        b"lavfi.freezedetect.freeze_start: 4.000000\n"
                    ),
                ),
            ):
                report = fetcher._background_freeze_scan(video, 12.0)

        self.assertEqual("error", report["status"])
        self.assertIn("malformed", report["error"])

    def test_final_visual_qa_holds_a_four_second_background_freeze(self) -> None:
        fetcher = PreviewFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "short.mp4"
            video.write_bytes(b"mock video")
            with patch.object(
                fetcher,
                "_background_freeze_scan",
                return_value={
                    "status": "pass",
                    "events": [{"start": 1.0, "end": 5.2, "duration": 4.2}],
                },
            ):
                report = fetcher.write_final_visual_qa(video, max_samples=20)

        self.assertEqual("hold", report["status"])
        self.assertTrue(report["background_freeze_blocking_intervals"])
        self.assertTrue(any("frozen background" in issue for issue in report["issues"]))

    def test_capped_final_visual_qa_uniformly_covers_long_video_and_each_sample_type(self) -> None:
        fetcher = LongPreviewFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "long.mp4"
            video.write_bytes(b"mock video")

            captions = []
            timeline = []
            for index in range(120):
                captions.append(
                    f"{index + 1}\n00:00:{index:02d},000 --> 00:00:{index:02d},400\nCaption {index + 1}\n"
                )
                timeline.append({"start": index + 0.6, "end": index + 0.8})
            (root / "subtitles.srt").write_text("\n".join(captions), encoding="utf-8")
            (root / "visual_timeline.json").write_text(json.dumps(timeline), encoding="utf-8")

            first = fetcher.write_final_visual_qa(video, out_dir=root / "qa-one", max_samples=12)
            second = fetcher.write_final_visual_qa(video, out_dir=root / "qa-two", max_samples=12)

            first_samples = [(sample["timestamp"], sample["tags"]) for sample in first["samples"]]
            second_samples = [(sample["timestamp"], sample["tags"]) for sample in second["samples"]]
            self.assertEqual(first_samples, second_samples)
            self.assertEqual(first["requested_samples"], 12)
            self.assertEqual(first["decoded_samples"], 12)
            sampled_times = [timestamp for timestamp, _ in first_samples]
            for mandatory in (0.5, 1.5, 2.5, 117.5, 119.5):
                self.assertIn(mandatory, sampled_times)
            self.assertEqual(3, sum(timestamp <= 2.5 for timestamp in sampled_times))
            for tag in ("T", "V", "C"):
                tagged = [timestamp for timestamp, tags in first_samples if tag in tags]
                self.assertTrue(any(timestamp < 30.0 for timestamp in tagged), tag)
                self.assertTrue(any(timestamp > 90.0 for timestamp in tagged), tag)

    def test_dense_caption_midpoints_do_not_turn_two_seconds_into_a_five_second_freeze(self) -> None:
        fetcher = ClusteredMidpointFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "short.mp4"
            video.write_bytes(b"mock video")
            cues = []
            for index, start in enumerate((2.8, 3.0, 3.2, 3.4, 3.6), start=1):
                cues.append(
                    f"{index}\n00:00:0{start:0.3f}".replace(".", ",")
                    + f" --> 00:00:0{start + 0.18:0.3f}".replace(".", ",")
                    + "\nCaption\n"
                )
            (root / "subtitles.srt").write_text("\n".join(cues), encoding="utf-8")
            (root / "visual_timeline.json").write_text(
                json.dumps([{"start": 0.0, "end": 2.5}, {"start": 2.5, "end": 5.0}, {"start": 5.0, "end": 6.0}]),
                encoding="utf-8",
            )

            report = fetcher.write_final_visual_qa(video, max_samples=30)

            self.assertEqual("pass", report["status"])
            self.assertLess(report["longest_static_transition_run"], 4)
            self.assertGreater(report["hook_motion_max_hash_distance"], 2)

    def test_ending_sentinels_do_not_duplicate_uniform_freeze_samples(self) -> None:
        fetcher = EndingSentinelFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "short.mp4"
            video.write_bytes(b"mock video")

            report = fetcher.write_final_visual_qa(video, max_samples=30)

            ending_samples = [
                sample for sample in report["samples"] if "E" in sample["tags"]
            ]
            self.assertEqual(2, len(ending_samples))
            self.assertEqual("pass", report["status"])
            self.assertLess(report["longest_static_transition_run"], 4)

    def test_local_vision_relevance_is_cached_and_fail_open(self) -> None:
        fetcher = HybridMediaFetcher()
        fetcher.enable_local_visual_relevance = True
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "scene.jpg"
            Image.new("RGB", (720, 1280), "blue").save(image_path)
            with patch("yt_auto.images.requests.post", return_value=FakeResponse()) as post:
                first = fetcher._local_visual_relevance(image_path, "attraction", "two adults talking", time.monotonic() + 5)
                second = fetcher._local_visual_relevance(image_path, "attraction", "two adults talking", time.monotonic() + 5)
            self.assertEqual(first, second)
            self.assertEqual(post.call_count, 1)

            with patch("yt_auto.images.requests.post", side_effect=TimeoutError):
                skipped = fetcher._local_visual_relevance(image_path, "new subject", "new scene", time.monotonic() + 5)
            self.assertIsNone(skipped)


if __name__ == "__main__":
    unittest.main()
