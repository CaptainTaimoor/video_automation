"""The music bed is levelled, and its provenance is recorded."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from yt_auto.video_builder import VideoBuilder

ROOT = Path(__file__).resolve().parents[1]
MUSIC = ROOT / "assets" / "music"


class BedLevellingTests(unittest.TestCase):
    """A fixed gain lets the track's own mastering decide the mix."""

    def test_the_filter_levels_the_bed_before_applying_the_gain(self):
        import inspect

        source = inspect.getsource(VideoBuilder._render_fast_audio_track)
        self.assertIn("loudnorm=I={self.MUSIC_BED_LUFS", source)
        levelled = source.index("MUSIC_BED_LUFS")
        gained = source.index("volume={music_volume")
        self.assertLess(levelled, gained, "levelling must come before the gain")

    def test_the_bed_target_leaves_room_under_the_mix_target(self):
        self.assertLess(VideoBuilder.MUSIC_BED_LUFS, -16.0)


class ProvenanceTests(unittest.TestCase):
    """Music files are gitignored, so the record of where they came from
    is the only thing a fresh clone keeps."""

    def setUp(self):
        path = MUSIC / "SOURCES.json"
        if not path.exists():
            self.skipTest("no music fetched in this checkout")
        self.entries = json.loads(path.read_text(encoding="utf-8"))

    def test_every_track_is_public_domain(self):
        for entry in self.entries:
            with self.subTest(track=entry.get("file")):
                self.assertEqual(str(entry.get("license", "")).lower(), "cc0")

    def test_every_track_names_where_it_came_from(self):
        for entry in self.entries:
            with self.subTest(track=entry.get("file")):
                self.assertTrue(str(entry.get("source_page") or "").startswith("http"))
                self.assertTrue(str(entry.get("creator") or "").strip())

    def test_both_channels_have_something_to_draw_from(self):
        channels = {entry.get("channel") for entry in self.entries}
        self.assertIn("ancient_history", channels)
        self.assertIn("brain_lens", channels)

    def test_no_channel_is_left_with_a_single_track_on_repeat(self):
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry["channel"]] = counts.get(entry["channel"], 0) + 1
        for channel, count in counts.items():
            with self.subTest(channel=channel):
                self.assertGreaterEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
