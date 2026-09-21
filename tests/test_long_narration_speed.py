"""Speaking rate and the caption gate are one setting seen from two sides.

Three Ancient long builds in a row died on the same beat needing 8.52%, 8.7%
and 9.2% narration stretch against an 8.5% ceiling. The cause was not the
script: measured on the same beat, 1.10 needs +8.52%, 1.05 needs +5.48% and
1.00 needs +2.42%. Trimming the text does not help, because fewer characters
take proportionally less time and the rate does not move.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from yt_auto.config import load_config
from yt_auto.models import ContentProfile
from yt_auto.subtitles import SubtitleComposer, SubtitleSegment
from yt_auto.tts_engine import NarrationEngine
from yt_auto.video_builder import VideoBuilder

ROOT = Path(__file__).resolve().parents[1]


def engine(voices, **kwargs) -> NarrationEngine:
    return NarrationEngine(voices, "single", "kokoro", **kwargs)


class SpeedOverrideTests(unittest.TestCase):
    def test_the_channel_rate_is_used_when_nothing_overrides_it(self):
        self.assertAlmostEqual(engine(["kokoro-bm_george@1.10"]).kokoro_speed, 1.10)

    def test_a_long_video_can_ask_for_a_slower_read(self):
        made = engine(["kokoro-bm_george@1.10"], speed_override=1.0)
        self.assertAlmostEqual(made.kokoro_speed, 1.0)

    def test_zero_means_keep_the_channel_rate(self):
        made = engine(["kokoro-bm_george@1.10"], speed_override=0.0)
        self.assertAlmostEqual(made.kokoro_speed, 1.10)

    def test_a_negative_rate_is_ignored_rather_than_applied(self):
        made = engine(["kokoro-bm_george@1.10"], speed_override=-1.0)
        self.assertAlmostEqual(made.kokoro_speed, 1.10)

    def test_the_voice_itself_is_untouched(self):
        made = engine(["kokoro-bm_george@1.10"], speed_override=1.0)
        self.assertEqual(made.kokoro_voice, "bm_george")


class ConfiguredForTheCaptionGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config" / "settings.yaml")
        self.channels = {channel.id: channel for channel in self.cfg.channels}

    def test_ancient_long_form_reads_slower_than_its_shorts(self):
        channel = self.channels["ancient_history"]
        shorts_rate = float(channel.voices[0].split("@")[1])
        self.assertGreater(channel.videos.voice_speed, 0)
        self.assertLess(channel.videos.voice_speed, shorts_rate)

    def test_the_shorts_rate_is_left_alone(self):
        # Shorts already compress narration to hit their window; slowing the
        # read would push that compression past its own guard.
        for channel_id in ("ancient_history", "brain_lens"):
            with self.subTest(channel=channel_id):
                self.assertEqual(self.channels[channel_id].shorts.voice_speed, 0.0)

    def test_the_default_profile_overrides_nothing(self):
        profile = ContentProfile(
            enabled=True, schedule_times=[], min_duration_seconds=240,
            max_duration_seconds=360, target_size=(1920, 1080),
        )
        self.assertEqual(profile.voice_speed, 0.0)


class TheRateAndTheGateAgreeTests(unittest.TestCase):
    """A beat read at the configured rate must fit under the stretch limit."""

    # The fixed framing beat every Ancient long video carries, and the audio
    # Kokoro actually produced for it at each speed.
    BEAT = (
        "Begin where the story of the Dead Sea Scrolls has to begin: with the physical setting "
        "and the first dated traces. Then move through expansion, pressure, conflict, adaptation, "
        "collapse, or survival without skipping the links between them. Objects, buildings, "
        "inscriptions, landscapes, and written accounts will not always agree. Those disagreements "
        "are useful because they reveal which parts are secure, which depend on later memory, and "
        "where the strongest competing explanation still survives."
    )
    MEASURED = {1.10: 26.05, 1.05: 26.80, 1.00: 27.60, 0.96: 28.375}

    def required_stretch(self, audio_seconds: float) -> float:
        cfg = load_config(ROOT / "config" / "settings.yaml")
        composer = SubtitleComposer(max_words_per_caption=cfg.app.subtitles.max_words_per_caption)
        target = max(10.0, composer.max_cps - VideoBuilder.LONG_CAPTION_TARGET_CPS_OFFSET)
        cues = composer.caption_segments_from_scene_segments(
            [SubtitleSegment(start=0.0, end=audio_seconds, text=self.BEAT)]
        )
        worst = max(
            composer._character_count(cue.text) / max(0.001, cue.end - cue.start)
            for cue in cues
        )
        return worst / target

    def test_the_old_rate_had_no_headroom(self):
        # It needed 8.52% against an 8.5% ceiling and died three builds
        # running. A later caption-splitter change shaved it to 8.29%, which
        # clears the limit by two hundredths of a point -- close enough that
        # any change in wording puts it back over. The point of the fix is
        # headroom, so what is pinned here is how little the old rate had.
        old_stretch = self.required_stretch(self.MEASURED[1.10])
        self.assertGreater(old_stretch, 1.08)

    def test_the_configured_rate_has_real_headroom(self):
        cfg = load_config(ROOT / "config" / "settings.yaml")
        channel = next(c for c in cfg.channels if c.id == "ancient_history")
        stretch = self.required_stretch(self.MEASURED[channel.videos.voice_speed])
        self.assertLess(stretch, 1.05)

    def test_the_configured_rate_fits_with_room_to_spare(self):
        cfg = load_config(ROOT / "config" / "settings.yaml")
        channel = next(c for c in cfg.channels if c.id == "ancient_history")
        audio = self.MEASURED[channel.videos.voice_speed]
        self.assertLess(self.required_stretch(audio), VideoBuilder.MAX_LONG_BEAT_STRETCH)

    def test_a_slower_read_always_needs_less_stretch(self):
        stretches = [self.required_stretch(self.MEASURED[s]) for s in (1.10, 1.05, 1.00, 0.96)]
        self.assertEqual(stretches, sorted(stretches, reverse=True))


if __name__ == "__main__":
    unittest.main()
