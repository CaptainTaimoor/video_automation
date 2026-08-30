from array import array
import math
from pathlib import Path
import tempfile
import unittest
import wave

from yt_auto.quality_v2.audio import (
    analyze_pcm_wav,
    bounded_atempo,
    chunk_narration,
    ffmpeg_audio_filter,
    review_pacing,
)


def _write_pcm16_wav(path: Path, samples: list[float], sample_rate: int = 1000) -> None:
    encoded = array(
        "h",
        (
            max(-32768, min(32767, round(sample * 32767)))
            for sample in samples
        ),
    )
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(sample_rate)
        destination.writeframes(encoded.tobytes())


class AudioQualityTests(unittest.TestCase):
    def test_chunks_preserve_punctuation_pause(self):
        chunks = chunk_narration("One short sentence. A question follows? Then an ending.", max_words=5)
        self.assertEqual(3, len(chunks))
        self.assertGreater(chunks[1].pause_after_seconds, chunks[0].pause_after_seconds)

    def test_atempo_refuses_large_speed_change(self):
        self.assertIsNotNone(bounded_atempo(60, 58))
        self.assertIsNone(bounded_atempo(60, 40))

    def test_pacing_catches_rushed_voice(self):
        report = review_pacing(180, 50, 1)
        self.assertFalse(report.acceptable)
        self.assertIn("narration is too fast", report.issues)

    def test_filter_has_sidechain_and_loudnorm(self):
        graph = ffmpeg_audio_filter()
        self.assertIn("sidechaincompress", graph)
        self.assertTrue(graph.endswith("[aout]"))

    def test_pcm_metrics_measure_only_qualified_silence_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "narration.wav"
            samples = (
                ([0.0] * 100)
                + ([0.5] * 200)
                + ([0.0] * 40)  # Too short to count as a silent region.
                + ([0.25] * 160)
                + ([0.0] * 100)
            )
            _write_pcm16_wav(path, samples)

            metrics = analyze_pcm_wav(path, word_count=40)

        self.assertAlmostEqual(0.6, metrics.duration_seconds, places=3)
        self.assertAlmostEqual(0.2, metrics.silence_seconds, places=3)
        self.assertAlmostEqual(0.4, metrics.active_speech_seconds, places=3)
        self.assertAlmostEqual(1 / 3, metrics.silence_ratio, places=3)
        self.assertEqual(2, metrics.silent_region_count)
        self.assertAlmostEqual(6000.0, metrics.active_speech_wpm, places=1)
        self.assertEqual(1000, metrics.sample_rate)
        self.assertEqual(1, metrics.channels)
        self.assertTrue(math.isfinite(metrics.rms_dbfs))
        self.assertGreater(metrics.peak_dbfs, metrics.active_rms_dbfs)
        self.assertGreaterEqual(metrics.dynamic_range_db, 5.5)

    def test_pcm_metrics_report_a_fully_silent_file_without_infinities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silence.wav"
            _write_pcm16_wav(path, [0.0] * 200)

            metrics = analyze_pcm_wav(path, word_count=12)

        self.assertEqual(0.0, metrics.active_speech_seconds)
        self.assertEqual(0.2, metrics.silence_seconds)
        self.assertEqual(1.0, metrics.silence_ratio)
        self.assertEqual(0.0, metrics.active_speech_wpm)
        self.assertEqual(-120.0, metrics.rms_dbfs)
        self.assertEqual(-120.0, metrics.peak_dbfs)
        self.assertEqual(0.0, metrics.crest_factor_db)
        self.assertEqual(1, metrics.silent_region_count)

    def test_pcm_metrics_validate_arguments_and_sample_width(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid_path = root / "valid.wav"
            _write_pcm16_wav(valid_path, [0.2] * 100)
            with self.assertRaisesRegex(ValueError, "word_count"):
                analyze_pcm_wav(valid_path, word_count=-1)
            with self.assertRaisesRegex(ValueError, "frame_ms"):
                analyze_pcm_wav(valid_path, frame_ms=0)

            unsupported_path = root / "unsupported.wav"
            with wave.open(str(unsupported_path), "wb") as destination:
                destination.setnchannels(1)
                destination.setsampwidth(1)
                destination.setframerate(1000)
                destination.writeframes(bytes([128] * 20))
            # Sanity check the supported unsigned 8-bit path as well.
            metrics = analyze_pcm_wav(unsupported_path)
            self.assertEqual(-120.0, metrics.rms_dbfs)
