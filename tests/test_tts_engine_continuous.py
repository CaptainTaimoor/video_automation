from __future__ import annotations

from array import array
from io import BytesIO
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from yt_auto.tts_engine import NarrationEngine


def _wav_bytes(duration: float = 0.25, sample_rate: int = 8000) -> bytes:
    samples = array(
        "h",
        (
            round(math.sin(2.0 * math.pi * 220.0 * index / sample_rate) * 5000)
            for index in range(round(duration * sample_rate))
        ),
    )
    payload = BytesIO()
    with wave.open(payload, "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(sample_rate)
        destination.writeframes(samples.tobytes())
    return payload.getvalue()


class ContinuousNarrationTests(unittest.TestCase):
    def test_long_pause_compaction_preserves_words_and_keeps_periodic_breaths(self):
        text = (
            "First short sentence. Second short sentence. Third short sentence. "
            "Fourth short sentence. Fifth short sentence."
        )

        compact = NarrationEngine._compact_long_sentence_boundaries(text)

        self.assertEqual(
            NarrationEngine._timing_tokens(text),
            NarrationEngine._timing_tokens(compact),
        )
        self.assertEqual(
            "First short sentence, Second short sentence, Third short sentence, "
            "Fourth short sentence. Fifth short sentence.",
            compact,
        )

    def test_edge_renders_the_whole_story_once_and_returns_pcm_wav_timings(self):
        audio_payload = _wav_bytes()
        constructor_calls: list[dict] = []

        class FakeCommunicate:
            def __init__(self, **kwargs):
                constructor_calls.append(kwargs)

            async def stream(self):
                yield {"type": "audio", "data": audio_payload}
                yield {"type": "WordBoundary", "offset": 0, "duration": 400_000, "text": "Alpha"}
                yield {"type": "WordBoundary", "offset": 500_000, "duration": 400_000, "text": "beta"}
                yield {"type": "WordBoundary", "offset": 1_200_000, "duration": 400_000, "text": "Gamma"}
                yield {"type": "WordBoundary", "offset": 1_800_000, "duration": 400_000, "text": "delta"}

        engine = NarrationEngine(
            voices=["en-US-JennyNeural"],
            voice_mode="single",
            backend_preference="edge",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out_path = root / "narration.wav"
            with patch("yt_auto.tts_engine.edge_tts.Communicate", FakeCommunicate):
                backend, durations = engine.synthesize_beats_continuous(
                    ["Alpha beta.", "Gamma delta."],
                    root / "segments",
                    out_path,
                )

            with wave.open(str(out_path), "rb") as rendered:
                self.assertEqual("NONE", rendered.getcomptype())
                self.assertEqual(2, rendered.getsampwidth())
                total_duration = rendered.getnframes() / rendered.getframerate()

        self.assertEqual("en-US-JennyNeural", backend)
        self.assertEqual(1, len(constructor_calls))
        self.assertEqual("Alpha beta. Gamma delta.", constructor_calls[0]["text"])
        self.assertEqual("WordBoundary", constructor_calls[0]["boundary"])
        self.assertEqual("+2%", constructor_calls[0]["rate"])
        self.assertAlmostEqual(0.12, durations[0], places=4)
        self.assertAlmostEqual(total_duration, sum(durations), places=4)

    def test_edge_failure_uses_one_configured_kokoro_story_before_other_local_backends(self):
        engine = NarrationEngine(
            voices=["en-US-JennyNeural", "kokoro-af_heart@0.98"],
            voice_mode="single",
            backend_preference="edge",
        )
        calls: list[dict] = []

        def render_once(**kwargs):
            calls.append(kwargs)
            return kwargs["backend_id"]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(engine, "_render_edge_continuous", side_effect=RuntimeError("offline")),
                patch.object(engine, "_render_with_backend", side_effect=render_once),
                patch.object(engine, "_ensure_pcm_wav"),
                patch.object(engine, "_audio_duration_seconds", return_value=6.0),
            ):
                backend, durations = engine.synthesize_beats_continuous(
                    ["First complete thought.", "Second complete thought."],
                    root / "segments",
                    root / "narration.wav",
                )

        self.assertEqual("kokoro-af_heart", backend)
        self.assertEqual(1, len(calls))
        self.assertEqual("kokoro-af_heart", calls[0]["backend_id"])
        self.assertEqual(
            "First complete thought. Second complete thought.",
            calls[0]["text"],
        )
        self.assertEqual(2, len(durations))
        self.assertAlmostEqual(6.0, sum(durations), places=8)

    def test_missing_edge_metadata_falls_back_to_positive_exact_total_estimates(self):
        engine = NarrationEngine(
            voices=["en-US-JennyNeural"],
            voice_mode="single",
            backend_preference="edge",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(engine, "_render_edge_continuous", return_value=[]),
                patch.object(engine, "_ensure_pcm_wav"),
                patch.object(engine, "_audio_duration_seconds", return_value=0.5),
            ):
                _, durations = engine.synthesize_beats_continuous(
                    ["One.", "Two words.", "A longer final thought."],
                    root / "segments",
                    root / "narration.wav",
                )

        self.assertEqual(3, len(durations))
        self.assertTrue(all(duration > 0 for duration in durations))
        self.assertAlmostEqual(0.5, sum(durations), places=10)


if __name__ == "__main__":
    unittest.main()
