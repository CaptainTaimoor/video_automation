"""Knowing what a picture shows, and letting the script writer know too.

The library used to know only what an asset was searched for. A Chichen Itza
folder turned out to hold a topographic map, a 3D render and an iguana on a
wall -- all filed under the same two words. Matching a narration line to
those by search words is how footage and script drifted apart.
"""

from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from yt_auto.images import HybridMediaFetcher
from yt_auto.script_writer import ScriptWriter
from yt_auto.visual_captioner import _parse
from yt_auto.visual_library import CATALOG_FIELDS, VisualLibrary


class ParseTests(unittest.TestCase):
    def test_a_clean_reply_is_read(self):
        text, tags = _parse('{"description": "A carved stone stela.", "tags": ["stone", "stela"]}')
        self.assertEqual(text, "A carved stone stela.")
        self.assertEqual(tags, ["stone", "stela"])

    def test_chatter_around_the_json_is_ignored(self):
        text, _ = _parse('Sure! Here it is:\n{"description": "A cave wall.", "tags": []}\nHope that helps.')
        self.assertEqual(text, "A cave wall.")

    def test_tags_given_as_a_string_are_split(self):
        _, tags = _parse('{"description": "x", "tags": "cave, horse; painting"}')
        self.assertEqual(tags, ["cave", "horse", "painting"])

    def test_no_json_still_keeps_the_first_sentence(self):
        text, tags = _parse("A ruined temple under trees. It is old.")
        self.assertEqual(text, "A ruined temple under trees.")
        self.assertEqual(tags, [])


class LibraryFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.library = VisualLibrary(self.root)

    def add(self, name, subject="chichen itza", description="", tags=""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        catalog = self.root / "catalog.csv"
        fresh = not catalog.exists()
        row = {f: "" for f in CATALOG_FIELDS}
        row.update(file=name, channel="ancient_history", subject=subject,
                   kind="image", tags=tags or subject, description=description,
                   added_at="2026-09-01T00:00:00")
        with catalog.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
            if fresh:
                writer.writeheader()
            writer.writerow(row)
        self.library._catalog = None


class DescriptionMatchingTests(LibraryFixture):
    def test_what_a_picture_shows_beats_what_it_was_filed_under(self):
        self.add("a/iguana.jpg", description="A large iguana rests on a weathered stone wall.")
        self.add("a/pyramid.jpg", description="A stepped pyramid with a central staircase.")
        hits = self.library.lookup("ancient_history", "chichen itza", "stepped pyramid staircase")
        self.assertEqual(hits[0]["file"], "a/pyramid.jpg")

    def test_saved_descriptions_survive_a_reload(self):
        self.add("a/one.jpg")
        self.assertEqual(
            self.library.update_descriptions({"a/one.jpg": ("A stepped pyramid.", ["pyramid"])}), 1)
        reloaded = VisualLibrary(self.root).catalog()
        self.assertEqual(reloaded[0]["description"], "A stepped pyramid.")
        self.assertIn("pyramid", reloaded[0]["tags"])

    def test_existing_tags_are_kept_when_new_ones_arrive(self):
        self.add("a/one.jpg", tags="chichen itza maya")
        self.library.update_descriptions({"a/one.jpg": ("x", ["pyramid"])})
        tags = VisualLibrary(self.root).catalog()[0]["tags"].split()
        self.assertIn("maya", tags)
        self.assertIn("pyramid", tags)

    def test_nothing_to_save_writes_nothing(self):
        self.add("a/one.jpg")
        self.assertEqual(self.library.update_descriptions({}), 0)

    def test_the_writer_can_be_told_what_exists(self):
        self.add("a/p.jpg", description="A stepped pyramid with a central staircase.")
        self.add("a/q.jpg", description="")
        shown = self.library.descriptions_for("ancient_history", "chichen itza")
        self.assertEqual(shown, ["A stepped pyramid with a central staircase."])


class ScriptSeesLibraryTests(LibraryFixture):
    def writer(self):
        made = ScriptWriter.__new__(ScriptWriter)
        return made

    def topic(self):
        return SimpleNamespace(subject="chichen itza", title="Chichen Itza")

    def test_no_library_means_no_block(self):
        made = self.writer()
        made.visual_library = None
        self.assertEqual(made._available_visuals_block(SimpleNamespace(id="ancient_history"),
                                                       self.topic()), "")

    def test_the_prompt_lists_what_the_library_can_show(self):
        self.add("a/p.jpg", description="A stepped pyramid with a central staircase.")
        made = self.writer()
        made.visual_library = self.library
        block = made._available_visuals_block(SimpleNamespace(id="ancient_history"), self.topic())
        self.assertIn("A stepped pyramid with a central staircase.", block)
        self.assertIn("do not invent facts", block)


class LocalFileDownloadTests(unittest.TestCase):
    def test_a_library_file_is_copied_rather_than_fetched(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.jpg"
            from PIL import Image

            Image.new("RGB", (1200, 800), (120, 100, 80)).save(source, format="JPEG")
            fetcher = HybridMediaFetcher.__new__(HybridMediaFetcher)
            target = Path(tmp) / "out.jpg"
            self.assertTrue(fetcher._download(source.as_uri(), target))
            self.assertTrue(target.is_file())

    def test_a_missing_library_file_fails_cleanly(self):
        with TemporaryDirectory() as tmp:
            fetcher = HybridMediaFetcher.__new__(HybridMediaFetcher)
            missing = (Path(tmp) / "gone.jpg").as_uri()
            self.assertFalse(fetcher._download(missing, Path(tmp) / "out.jpg"))


if __name__ == "__main__":
    unittest.main()
