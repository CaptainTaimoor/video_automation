"""The writer's target and the gate's word band must come from one number.

This has gone wrong twice in the same shape: a limit is changed in one place
and its partner is left behind. Captions were told 19.5 while approval still
said 17.0 and every finished Ancient Short was rejected. Long videos were
configured for 4-6 minutes while a 1200-word floor written for nine minutes
still stood, so the script came out long and then needed a 43% speed-up to
fit the window -- which the narration guard refused, correctly.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from yt_auto.config import load_config
from yt_auto.pipeline import ShortsFactory
from yt_auto.script_writer import ScriptWriter
from yt_auto.video_builder import VideoBuilder

ROOT = Path(__file__).resolve().parents[1]


def factory() -> ShortsFactory:
    made = ShortsFactory.__new__(ShortsFactory)
    made.script_writer = ScriptWriter.__new__(ScriptWriter)
    return made


def channel(min_seconds: int, max_seconds: int, channel_id: str = "ancient_history") -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        videos=SimpleNamespace(min_duration_seconds=min_seconds, max_duration_seconds=max_seconds),
        shorts=SimpleNamespace(min_duration_seconds=50, max_duration_seconds=59),
    )


class WindowFollowsDurationTests(unittest.TestCase):
    def test_a_shorter_channel_accepts_a_shorter_script(self):
        short_lo, _ = factory()._long_word_window(channel(240, 360))
        long_lo, _ = factory()._long_word_window(channel(480, 600))
        self.assertLess(short_lo, long_lo)

    def test_the_writers_target_lands_inside_the_gates_band(self):
        for mn, mx in ((240, 360), (300, 420), (480, 600)):
            with self.subTest(duration=(mn, mx)):
                ch = channel(mn, mx)
                made = factory()
                low, high = made._long_word_window(ch)
                target = made.script_writer._target_words(ch, "video")
                self.assertGreaterEqual(target, low, "the writer aims below what the gate accepts")
                self.assertLessEqual(target, high, "the writer aims above what the gate accepts")

    def test_the_band_is_the_right_way_round(self):
        low, high = factory()._long_word_window(channel(240, 360))
        self.assertLess(low, high)

    def test_a_tiny_configured_duration_still_gives_a_usable_floor(self):
        low, _ = factory()._long_word_window(channel(10, 30))
        self.assertGreaterEqual(low, 200)


class WorkspaceConfigTests(unittest.TestCase):
    """The configured channels must themselves be self-consistent."""

    def setUp(self):
        self.config = load_config(ROOT / "config" / "settings.yaml")
        self.factory = factory()

    def test_every_channel_can_actually_produce_a_passing_long_script(self):
        for ch in self.config.channels:
            with self.subTest(channel=ch.id):
                low, high = self.factory._long_word_window(ch)
                target = self.factory.script_writer._target_words(ch, "video")
                self.assertTrue(low <= target <= high, f"{ch.id}: target {target} outside {low}-{high}")

    def test_long_videos_are_configured_for_four_to_six_minutes(self):
        for ch in self.config.channels:
            with self.subTest(channel=ch.id):
                self.assertEqual(ch.videos.min_duration_seconds, 240)
                self.assertEqual(ch.videos.max_duration_seconds, 360)

    def test_the_renderer_never_targets_faster_captions_than_the_checker_allows(self):
        from yt_auto.subtitles import SubtitleComposer

        self.assertLessEqual(VideoBuilder.SHORT_CAPTION_TARGET_CPS, SubtitleComposer.DEFAULT_MAX_CPS)


class PacingFloorFollowsDurationTests(unittest.TestCase):
    """The builder's "is this a real script" floor is the third partner.

    It was a flat 850, written when long videos ran eight to ten minutes. At
    four to six the writer aims at 775, so every correctly sized script read
    as a stub, pacing normalization switched off, and a finished build was
    thrown away over a beat needing 8.7% stretch where 8.5% was allowed.
    """

    @staticmethod
    def pacing_floor(min_seconds: int) -> int:
        return max(1, int(min_seconds * VideoBuilder.MIN_LONG_NARRATION_WPM / 60.0))

    def test_a_script_the_gate_accepts_is_never_treated_as_a_stub(self):
        for mn, mx in ((240, 360), (300, 420), (480, 600), (600, 900)):
            with self.subTest(duration=(mn, mx)):
                gate_low, _ = factory()._long_word_window(channel(mn, mx))
                self.assertLessEqual(
                    self.pacing_floor(mn),
                    gate_low,
                    "a script long enough to pass the editorial gate would "
                    "still lose pacing normalization",
                )

    def test_the_writers_own_target_keeps_pacing_on(self):
        for mn, mx in ((240, 360), (300, 420), (480, 600)):
            with self.subTest(duration=(mn, mx)):
                ch = channel(mn, mx)
                target = factory().script_writer._target_words(ch, "video")
                self.assertGreaterEqual(target, self.pacing_floor(mn))

    def test_the_floor_rises_with_the_configured_length(self):
        floors = [self.pacing_floor(mn) for mn in (240, 300, 480, 600)]
        self.assertEqual(floors, sorted(floors))
        self.assertEqual(len(set(floors)), len(floors))

    def test_a_genuine_stub_is_still_refused_pacing(self):
        self.assertGreater(self.pacing_floor(240), 200)


if __name__ == "__main__":
    unittest.main()
