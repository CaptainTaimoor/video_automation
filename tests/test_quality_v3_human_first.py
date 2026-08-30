import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from yt_auto.config import load_config
from yt_auto.pipeline import ShortsFactory
from yt_auto.subtitles import SubtitleComposer, SubtitleSegment


class QualityV3HumanFirstTests(unittest.TestCase):
    def test_v3_always_enables_and_enforces_v2_gates(self) -> None:
        with patch.dict(
            os.environ,
            {
                "YT_QUALITY_V3": "1",
                "YT_QUALITY_V2": "0",
                "YT_QUALITY_V2_ENFORCE": "0",
            },
            clear=True,
        ):
            self.assertEqual(
                (True, True, True),
                ShortsFactory._quality_modes_from_environment(),
            )

    def test_enabled_v2_is_fail_closed_when_enforce_switch_is_omitted(self) -> None:
        with patch.dict(os.environ, {"YT_QUALITY_V2": "1"}, clear=True):
            self.assertEqual(
                (False, True, True),
                ShortsFactory._quality_modes_from_environment(),
            )

    def test_editorial_and_visual_holds_can_never_finish_as_pass(self) -> None:
        review = {
            "score": 100,
            "decision": "pass",
            "issues": [],
            "blocking_issues": [],
            "subscores": {"script": 100, "visuals": 100},
        }

        result = ShortsFactory._apply_quality_v2_holds(
            review,
            {"approved": False, "issues": ["script repeats a published episode"]},
            {"approved": False, "issues": ["six visuals are reused"]},
        )

        self.assertEqual("hold", result["decision"])
        self.assertEqual(69, result["score"])
        self.assertEqual(35, result["subscores"]["script"])
        self.assertEqual(35, result["subscores"]["visuals"])
        self.assertEqual(2, len(result["blocking_issues"]))

    def test_voice_first_channel_does_not_require_background_music(self) -> None:
        channel = SimpleNamespace(background_music_enabled=False)

        report = ShortsFactory._background_music_report(channel, "")

        self.assertTrue(report["approved"])
        self.assertFalse(report["enabled"])
        self.assertIn("voice-first", report["strength"])

    def test_brain_long_rejects_non_model_outage_template(self) -> None:
        candidate = SimpleNamespace(script_provider="")

        issue = ShortsFactory._quality_v3_script_provider_issue(
            "brain_lens",
            "video",
            candidate,
        )

        self.assertIn("model-authored", issue or "")
        candidate.script_provider = "gemini"
        self.assertIsNone(
            ShortsFactory._quality_v3_script_provider_issue(
                "brain_lens",
                "video",
                candidate,
            )
        )

    def test_v3_dispatches_one_continuous_narration(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.quality_v3_enabled = True
        engine = Mock()
        engine.synthesize_beats_continuous.return_value = ("edge", [2.0, 2.5])

        result = factory._synthesize_narration_beats(
            engine,
            ["First beat.", "Second beat."],
            Path("segments"),
            Path("narration.wav"),
        )

        self.assertEqual(("edge", [2.0, 2.5]), result)
        engine.synthesize_beats_continuous.assert_called_once()
        engine.synthesize_beats.assert_not_called()

    def test_v3_channel_defaults_use_edge_and_voice_first_audio(self) -> None:
        config = load_config(Path("config/settings.yaml"))
        channels = {channel.id: channel for channel in config.channels}

        self.assertEqual("edge", channels["ancient_history"].tts_backend)
        self.assertEqual("edge", channels["brain_lens"].tts_backend)
        self.assertFalse(channels["ancient_history"].background_music_enabled)
        self.assertFalse(channels["brain_lens"].background_music_enabled)
        self.assertTrue(channels["ancient_history"].voices[0].endswith("Neural"))
        self.assertTrue(channels["brain_lens"].voices[0].endswith("Neural"))

    def test_missing_continuous_pcm_narration_is_held(self) -> None:
        report = ShortsFactory._quality_v3_audio_report(
            "ancient_history",
            "short",
            Path("definitely-missing-narration.wav"),
            80,
        )

        self.assertFalse(report["approved"])
        self.assertIn("missing", report["issues"][0])

    def test_dash_parenthetical_keeps_transitive_verb_with_its_object(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=6, max_chars_per_second=100)
        text = (
            "The Great Ball Court—the largest known in Mesoamerica—"
            "linked play with elite ceremony."
        )
        chunks = composer._chunk_text(text)
        segments = [
            SubtitleSegment(index * 10.0, (index + 1) * 10.0, chunk)
            for index, chunk in enumerate(chunks)
        ]

        self.assertIn("linked play", " | ".join(chunks))
        self.assertEqual([], composer.quality_issues(segments, cps_tolerance=0.0))


if __name__ == "__main__":
    unittest.main()
