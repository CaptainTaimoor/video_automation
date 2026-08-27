import unittest

from yt_auto.quality_v2.audio import bounded_atempo, chunk_narration, ffmpeg_audio_filter, review_pacing


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
