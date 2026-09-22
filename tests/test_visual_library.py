"""The shared store of footage that already passed the checks.

Its whole job is to make a real photograph available when a build would
otherwise run out of time and draw a grey card. So the tests are about what
it offers a build, what it refuses to offer twice too soon, and what happens
when the drive it lives on is not there.

The on-disk format is CSV on purpose -- readable without this program, and a
half-written row costs one asset rather than the index.
"""

from __future__ import annotations

import csv
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from yt_auto.visual_library import (
    CATALOG_FIELDS,
    DEFAULT_REUSE_COOLDOWN_DAYS,
    USAGE_FIELDS,
    VisualLibrary,
    terms_of,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LibraryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.library = VisualLibrary(self.root, reuse_cooldown_days=15)

    def add(self, name: str, *, channel="ancient_history", subject="tikal",
            kind="image", tags="", added=None, make_file=True) -> None:
        path = self.root / name
        if make_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not really an image, but it is on disk")
        row = {field: "" for field in CATALOG_FIELDS}
        row.update(file=name, channel=channel, subject=subject, kind=kind,
                   tags=tags or subject, source="wikimedia", license="CC0",
                   added_at=(added or _now()).isoformat(timespec="seconds"))
        catalog = self.root / "catalog.csv"
        fresh = not catalog.exists()
        with catalog.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
            if fresh:
                writer.writeheader()
            writer.writerow(row)
        self.library._catalog = None

    def mark_used(self, name: str, when: datetime) -> None:
        usage = self.root / "usage.csv"
        fresh = not usage.exists()
        with usage.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=USAGE_FIELDS)
            if fresh:
                writer.writeheader()
            writer.writerow({"url": "", "file": name, "channel": "", "subject": "",
                             "run": "", "used_at": when.isoformat(timespec="seconds")})
        self.library._used_at = None


class LookupTests(LibraryFixture):
    def test_an_asset_comes_back_for_its_own_subject(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        hits = self.library.lookup("ancient_history", "tikal")
        self.assertEqual([h["file"] for h in hits], ["ancient_history/tikal/a.jpg"])

    def test_a_related_subject_still_finds_it(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal",
                 tags="tikal maya temple pyramid")
        self.assertTrue(self.library.lookup("ancient_history", "maya pyramid"))

    def test_an_unrelated_subject_finds_nothing(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.assertEqual(self.library.lookup("ancient_history", "roman concrete"), [])

    def test_another_channel_does_not_share_the_shelf(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.assertEqual(self.library.lookup("brain_lens", "tikal"), [])

    def test_the_closest_match_leads(self):
        self.add("ancient_history/maya/general.jpg", subject="maya",
                 tags="maya mesoamerica")
        self.add("ancient_history/tikal/exact.jpg", subject="tikal",
                 tags="tikal maya temple")
        hits = self.library.lookup("ancient_history", "tikal temple")
        self.assertEqual(hits[0]["file"], "ancient_history/tikal/exact.jpg")

    def test_a_catalogued_file_that_is_gone_is_not_offered(self):
        self.add("ancient_history/tikal/missing.jpg", subject="tikal", make_file=False)
        self.assertEqual(self.library.lookup("ancient_history", "tikal"), [])

    def test_the_limit_is_respected(self):
        for index in range(8):
            self.add(f"ancient_history/tikal/{index}.jpg", subject="tikal")
        self.assertEqual(len(self.library.lookup("ancient_history", "tikal", limit=3)), 3)

    def test_video_and_image_are_asked_for_separately(self):
        self.add("brain_lens/couple/a.jpg", channel="brain_lens",
                 subject="couple", kind="image")
        self.add("brain_lens/couple/b.mp4", channel="brain_lens",
                 subject="couple", kind="video")
        hits = self.library.lookup("brain_lens", "couple", kind="video")
        self.assertEqual([h["file"] for h in hits], ["brain_lens/couple/b.mp4"])

    def test_a_caller_can_exclude_what_it_already_picked(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.add("ancient_history/tikal/b.jpg", subject="tikal")
        hits = self.library.lookup("ancient_history", "tikal",
                                   exclude_files=["ancient_history/tikal/a.jpg"])
        self.assertEqual([h["file"] for h in hits], ["ancient_history/tikal/b.jpg"])


class RestTests(LibraryFixture):
    """The same photograph must not appear twice in a fortnight of uploads."""

    def test_an_unused_asset_is_available(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.assertTrue(self.library.rested("ancient_history/tikal/a.jpg"))

    def test_an_asset_used_yesterday_is_held_back(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.mark_used("ancient_history/tikal/a.jpg", _now() - timedelta(days=1))
        self.assertFalse(self.library.rested("ancient_history/tikal/a.jpg"))
        self.assertEqual(self.library.lookup("ancient_history", "tikal"), [])

    def test_an_asset_used_long_ago_comes_back(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.mark_used("ancient_history/tikal/a.jpg", _now() - timedelta(days=40))
        self.assertTrue(self.library.rested("ancient_history/tikal/a.jpg"))
        self.assertTrue(self.library.lookup("ancient_history", "tikal"))

    def test_the_boundary_day_counts_as_rested(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.mark_used("ancient_history/tikal/a.jpg",
                       _now() - timedelta(days=15, minutes=1))
        self.assertTrue(self.library.rested("ancient_history/tikal/a.jpg"))

    def test_the_newest_use_decides(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.mark_used("ancient_history/tikal/a.jpg", _now() - timedelta(days=40))
        self.mark_used("ancient_history/tikal/a.jpg", _now() - timedelta(days=2))
        self.assertFalse(self.library.rested("ancient_history/tikal/a.jpg"))

    def test_a_zero_cooldown_lets_anything_repeat(self):
        library = VisualLibrary(self.root, reuse_cooldown_days=0)
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.mark_used("ancient_history/tikal/a.jpg", _now())
        self.assertTrue(library.rested("ancient_history/tikal/a.jpg"))

    def test_recording_a_use_holds_it_back_immediately(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        hit = self.library.lookup("ancient_history", "tikal")[0]
        self.assertTrue(self.library.record_use(hit, run_dir="somewhere"))
        self.assertEqual(self.library.lookup("ancient_history", "tikal"), [])

    def test_the_default_cooldown_is_a_fortnight_ish(self):
        self.assertEqual(DEFAULT_REUSE_COOLDOWN_DAYS, 15)


class StoreTests(LibraryFixture):
    def test_a_stored_asset_can_be_found_again(self):
        source = self.root / "incoming.jpg"
        source.write_bytes(b"bytes")
        name = self.library.store(source, channel_id="ancient_history",
                                  subject="Nubian Pyramids",
                                  meta={"source": "wikimedia", "license": "CC0",
                                        "perceptual_hash": "abc123def456ab"})
        self.assertTrue(name)
        self.assertTrue((self.root / name).is_file())
        self.assertTrue(self.library.lookup("ancient_history", "nubian pyramids"))

    def test_storing_something_that_is_not_there_is_not_an_error(self):
        self.assertEqual(
            self.library.store(self.root / "nope.jpg", channel_id="ancient_history",
                               subject="tikal", meta={}),
            "",
        )

    def test_an_asset_already_held_is_recognised(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        rows = self.library.catalog()
        rows[0]["url"] = "https://example.invalid/a.jpg"
        self.assertTrue(self.library.has_asset(url="https://example.invalid/a.jpg"))
        self.assertFalse(self.library.has_asset(url="https://example.invalid/b.jpg"))

    def test_asking_about_nothing_is_not_a_match(self):
        self.assertFalse(self.library.has_asset())


class ResilienceTests(LibraryFixture):
    """A drive can be unplugged between builds; that is slower, not broken."""

    def test_a_missing_drive_reports_unavailable(self):
        library = VisualLibrary(self.root / "not-mounted")
        self.assertFalse(library.available)
        self.assertEqual(library.lookup("ancient_history", "tikal"), [])

    def test_a_corrupt_catalogue_behaves_like_an_empty_one(self):
        (self.root / "catalog.csv").write_bytes(b"\x00\x01 not,a,csv\n\x00")
        self.assertEqual(VisualLibrary(self.root).lookup("ancient_history", "tikal"), [])

    def test_a_corrupt_usage_file_does_not_hide_the_catalogue(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        (self.root / "usage.csv").write_bytes(b"\x00 rubbish")
        self.assertTrue(self.library.lookup("ancient_history", "tikal"))

    def test_stats_describe_the_shelf(self):
        self.add("ancient_history/tikal/a.jpg", subject="tikal")
        self.add("brain_lens/couple/b.mp4", channel="brain_lens",
                 subject="couple", kind="video")
        stats = self.library.stats()
        self.assertEqual(stats["assets"], 2)
        self.assertEqual(stats["by_channel"]["ancient_history"], 1)
        self.assertEqual(stats["by_kind"]["video"], 1)
        self.assertEqual(stats["reuse_cooldown_days"], 15)


class TermsTests(unittest.TestCase):
    def test_short_words_are_not_worth_indexing(self):
        self.assertNotIn("of", terms_of("City of Tikal"))
        self.assertIn("tikal", terms_of("City of Tikal"))

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(terms_of("Pompeii, Plaster-Casts!"),
                         terms_of("pompeii plaster casts"))


if __name__ == "__main__":
    unittest.main()
