"""Footage that already passed the checks is kept and reused.

Downloading during a build is the slow part: one clip can take 30-60 seconds
and a Short needs about six, which is most of a scene's budget. Anything
verified once is worth not fetching again.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_auto.visual_library import VisualLibrary, terms_of


def make_file(root: Path, name: str, size: int = 1024) -> Path:
    path = root / name
    path.write_bytes(b"x" * size)
    return path


META = {
    "asset_id": "wikimedia:nubian-1",
    "asset_title": "Nubian pyramids at Meroe",
    "source": "wikimedia",
    "source_page": "https://example.org/item",
}


class TermTests(unittest.TestCase):
    def test_short_words_are_not_indexed(self):
        self.assertNotIn("at", terms_of("Nubian pyramids at Meroe"))

    def test_words_are_case_and_punctuation_insensitive(self):
        self.assertEqual(terms_of("Nubian, Pyramids!"), terms_of("nubian pyramids"))

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(terms_of(""), set())


class StoreAndLookupTests(unittest.TestCase):
    def test_a_stored_asset_comes_back_for_a_related_subject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            lib.store(make_file(root, "a.jpg"), channel_id="ancient_history", meta=META, terms=["nubian pyramids"])
            found = lib.lookup("ancient_history", ["nubian pyramids meroe"])
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0][1]["asset_id"], "wikimedia:nubian-1")

    def test_an_unrelated_subject_finds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            lib.store(make_file(root, "a.jpg"), channel_id="ancient_history", meta=META, terms=["nubian pyramids"])
            self.assertEqual(lib.lookup("ancient_history", ["dating anxiety"]), [])

    def test_another_channel_does_not_share_the_shelf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            lib.store(make_file(root, "a.jpg"), channel_id="ancient_history", meta=META, terms=["nubian pyramids"])
            self.assertEqual(lib.lookup("brain_lens", ["nubian pyramids"]), [])

    def test_the_closest_match_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            lib.store(make_file(root, "a.jpg"), channel_id="c",
                      meta={"asset_id": "one", "asset_title": ""}, terms=["pyramids"])
            lib.store(make_file(root, "b.jpg"), channel_id="c",
                      meta={"asset_id": "two", "asset_title": ""}, terms=["nubian pyramids meroe"])
            found = lib.lookup("c", ["nubian pyramids meroe"])
            self.assertEqual(found[0][1]["asset_id"], "two")

    def test_a_missing_file_is_not_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            stored = lib.store(make_file(root, "a.jpg"), channel_id="c", meta=META, terms=["nubian"])
            stored.unlink()
            self.assertEqual(lib.lookup("c", ["nubian"]), [])

    def test_storing_something_that_is_not_there_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(VisualLibrary(root).store(root / "nope.jpg", channel_id="c", meta=META))

    def test_the_limit_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            for i in range(5):
                lib.store(make_file(root, f"f{i}.jpg"), channel_id="c",
                          meta={"asset_id": f"a{i}", "asset_title": ""}, terms=["pyramids"])
            self.assertEqual(len(lib.lookup("c", ["pyramids"], limit=2)), 2)

    def test_the_library_lives_outside_the_repo_history(self):
        self.assertIn(".runtime", str(VisualLibrary(Path("/tmp/x")).base))


class PruneTests(unittest.TestCase):
    def test_nothing_is_dropped_while_it_fits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root, max_bytes=10_000)
            lib.store(make_file(root, "a.jpg", 500), channel_id="c", meta=META, terms=["x1234"])
            self.assertEqual(lib.prune(), 0)

    def test_the_least_recently_used_file_goes_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root, max_bytes=2_500)
            lib.store(make_file(root, "old.jpg", 1000), channel_id="c",
                      meta={"asset_id": "old", "asset_title": ""}, terms=["pyramids"], now=100)
            lib.store(make_file(root, "new.jpg", 1000), channel_id="c",
                      meta={"asset_id": "new", "asset_title": ""}, terms=["pyramids"], now=900)
            lib.store(make_file(root, "big.jpg", 2000), channel_id="c",
                      meta={"asset_id": "big", "asset_title": ""}, terms=["pyramids"], now=950)
            remaining = {meta["asset_id"] for _, meta in lib.lookup("c", ["pyramids"], limit=10)}
            self.assertNotIn("old", remaining)

    def test_the_library_reports_what_it_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            lib.store(make_file(root, "a.jpg", 700), channel_id="brain_lens", meta=META, terms=["dating"])
            stats = lib.stats()
            self.assertEqual(stats["files"], 1)
            self.assertEqual(stats["by_channel"]["brain_lens"], 1)
            self.assertGreaterEqual(stats["bytes"], 700)


class ResilienceTests(unittest.TestCase):
    def test_a_corrupt_index_behaves_like_an_empty_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = VisualLibrary(root)
            lib.base.mkdir(parents=True, exist_ok=True)
            lib.index_path.write_text("{not json", encoding="utf-8")
            self.assertEqual(lib.lookup("c", ["pyramids"]), [])
            self.assertEqual(lib.stats()["files"], 0)

    def test_an_empty_library_is_simply_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(VisualLibrary(Path(tmp)).lookup("c", ["anything"]), [])


if __name__ == "__main__":
    unittest.main()
