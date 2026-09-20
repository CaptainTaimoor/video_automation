"""Openverse as an image source, and the archive rules that must include it.

Roughly a third of videos carried generated text-on-colour cards because
Wikimedia ran out of real photographs for niche subjects. Openverse covers
those subjects and needs no API key -- but only if the rules that were written
around Wikimedia's name also apply to it.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from yt_auto.images import HybridMediaFetcher


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def fetcher(payload):
    obj = HybridMediaFetcher.__new__(HybridMediaFetcher)
    obj.session = SimpleNamespace(get=lambda *a, **k: FakeResponse(payload))
    obj._deadline_timeout = lambda deadline, default: 10.0
    return obj


HIT = {
    "id": "abc-123",
    "url": "https://example.org/nubian.jpg",
    "title": "Nubian pyramids at Meroe",
    "creator": "A Photographer",
    "license": "by-sa",
    "foreign_landing_url": "https://example.org/item/1",
    "width": 800,
    "height": 600,
}


class OpenverseSearchTests(unittest.TestCase):
    def test_a_hit_becomes_a_usable_candidate(self):
        (url, meta), = fetcher({"results": [HIT]})._openverse_search("nubian pyramids")
        self.assertEqual(url, "https://example.org/nubian.jpg")
        self.assertEqual(meta["source"], "openverse")
        self.assertEqual(meta["artist"], "A Photographer")
        self.assertEqual(meta["asset_id"], "openverse:abc-123")

    def test_provenance_points_at_the_holding_institution(self):
        # The verifier wants the item's own page, not the media file.
        (_, meta), = fetcher({"results": [HIT]})._openverse_search("q")
        self.assertEqual(meta["source_page"], "https://example.org/item/1")

    def test_dimensions_are_carried_through_for_the_resolution_check(self):
        (_, meta), = fetcher({"results": [HIT]})._openverse_search("q")
        self.assertEqual((meta["media_width"], meta["media_height"]), ("800", "600"))

    def test_a_hit_with_no_url_is_skipped_not_crashed_on(self):
        self.assertEqual(fetcher({"results": [{"id": "x"}]})._openverse_search("q"), [])

    def test_an_empty_archive_returns_nothing(self):
        self.assertEqual(fetcher({"results": []})._openverse_search("q"), [])

    def test_only_commercial_use_licences_are_requested(self):
        # A monetising channel cannot ship non-commercial or no-derivatives work.
        for token in ("nc", "nd"):
            self.assertNotIn(token, HybridMediaFetcher.OPENVERSE_LICENCES.split(","))

    def test_the_page_size_stays_inside_the_api_limit(self):
        captured = {}

        obj = HybridMediaFetcher.__new__(HybridMediaFetcher)
        obj._deadline_timeout = lambda deadline, default: 10.0

        def get(endpoint, params=None, timeout=None):
            captured.update(params or {})
            return FakeResponse({"results": []})

        obj.session = SimpleNamespace(get=get)
        obj._openverse_search("q", limit=500)
        self.assertLessEqual(int(captured["page_size"]), 20)


class ArchiveRuleTests(unittest.TestCase):
    """Rules written around Wikimedia's name have to cover Openverse too."""

    def test_openverse_counts_as_a_small_archive(self):
        self.assertIn("openverse", HybridMediaFetcher.SMALL_ARCHIVE_SOURCES)

    def test_wikimedia_is_still_a_small_archive(self):
        self.assertIn("wikimedia", HybridMediaFetcher.SMALL_ARCHIVE_SOURCES)

    def test_stock_providers_do_not_get_the_exception(self):
        for source in ("pexels", "pixabay"):
            with self.subTest(source=source):
                self.assertNotIn(source, HybridMediaFetcher.SMALL_ARCHIVE_SOURCES)


if __name__ == "__main__":
    unittest.main()
