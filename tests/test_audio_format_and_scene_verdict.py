"""Audio the builder can actually read, and footage kept instead of discarded.

Both are regressions that already cost builds: a provider switched to raw PCM
with no file header and 18 builds failed, and a filter that answered one
yes/no question for two different things left one scene with 4 candidates out
of 45 before falling back to a generated card.
"""

from __future__ import annotations

import io
import unittest
import wave

from yt_auto.images import HybridMediaFetcher
from yt_auto.tts_engine import NarrationEngine


class AudioFormatTests(unittest.TestCase):
    def test_known_containers_are_recognised(self):
        for body, expected in (
            (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
            (b"ID3\x04\x00\x00\x00\x00\x00\x00", "mp3"),
            (b"\xff\xfb\x90\x00", "mp3"),
            (b"OggS\x00\x02\x00\x00", "ogg"),
            (b"fLaC\x00\x00\x00\x22", "flac"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(NarrationEngine.audio_format_of(body), expected)

    def test_headerless_audio_is_called_raw_not_mistaken_for_a_container(self):
        self.assertEqual(NarrationEngine.audio_format_of(b"\x00\x01" * 64), "raw")

    def test_nothing_at_all_is_empty(self):
        self.assertEqual(NarrationEngine.audio_format_of(b""), "empty")

    def test_raw_pcm_is_wrapped_into_something_readable(self):
        wrapped = NarrationEngine.readable_audio(b"\x00\x01" * 4096)
        self.assertEqual(NarrationEngine.audio_format_of(wrapped), "wav")
        with wave.open(io.BytesIO(wrapped)) as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getframerate(), 24000)
            self.assertGreater(handle.getnframes(), 0)

    def test_the_sample_rate_is_honoured(self):
        wrapped = NarrationEngine.readable_audio(b"\x00\x01" * 4096, sample_rate=48000)
        with wave.open(io.BytesIO(wrapped)) as handle:
            self.assertEqual(handle.getframerate(), 48000)

    def test_a_container_is_passed_through_untouched(self):
        body = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 4096
        self.assertIs(NarrationEngine.readable_audio(body), body)

    def test_a_short_headerless_body_is_rejected_as_an_error_page(self):
        self.assertIsNone(NarrationEngine.readable_audio(b"upstream error"))

    def test_nothing_in_gives_nothing_back(self):
        self.assertIsNone(NarrationEngine.readable_audio(b""))


class DeepgramTests(unittest.TestCase):
    def test_a_missing_key_fails_quietly_rather_than_raising(self):
        engine = NarrationEngine.__new__(NarrationEngine)
        from pathlib import Path

        self.assertFalse(engine._render_deepgram("hello", Path("unused.mp3"), "", ""))

    def test_empty_text_is_not_sent(self):
        engine = NarrationEngine.__new__(NarrationEngine)
        from pathlib import Path

        self.assertFalse(engine._render_deepgram("   ", Path("unused.mp3"), "key", ""))


class SceneVerdictTests(unittest.TestCase):
    """Three answers, not two: blocked, weak, match."""

    def setUp(self):
        self.fetcher = HybridMediaFetcher.__new__(HybridMediaFetcher)

    def verdict(self, scene, title):
        return self.fetcher._asset_scene_verdict("brain_lens", scene, f"https://x/{title}.mp4", {"asset_title": title})

    def test_an_unrelated_activity_is_blocked_outright(self):
        self.assertEqual(self.verdict("they text you back", "video game gamer"), "blocked")

    def test_overt_intimacy_on_a_subtle_scene_is_blocked(self):
        self.assertEqual(self.verdict("one cue of friendliness", "couple kissing passionate"), "blocked")

    def test_a_loose_but_real_clip_is_weak_not_discarded(self):
        # This is the case that used to be thrown away, leaving a generated card.
        self.assertEqual(self.verdict("they text you back", "adult couple walking"), "weak")

    def test_no_scene_wording_accepts_anything_not_blocked(self):
        self.assertEqual(self.verdict("", "anything at all"), "match")

    def test_the_old_yes_no_answer_still_means_a_clean_match(self):
        for scene, title in (("they text you back", "video game gamer"), ("they text you back", "adult couple walking")):
            with self.subTest(title=title):
                self.assertFalse(
                    self.fetcher._asset_matches_scene_intent("brain_lens", scene, f"https://x/{title}", {"asset_title": title})
                )

    def test_ranking_keeps_matches_ahead_of_weak_and_drops_blocked(self):
        scene = "they text you back"
        candidates = [
            ("https://x/game.mp4", {"asset_title": "video game gamer"}),
            ("https://x/walk.mp4", {"asset_title": "adult couple walking"}),
            ("https://x/phone.mp4", {"asset_title": "woman checking phone text message"}),
        ]
        ranked, loose = [], []
        for url, meta in candidates:
            got = self.fetcher._asset_scene_verdict("brain_lens", scene, url, meta)
            if got == "blocked":
                continue
            (ranked if got == "match" else loose).append(url)
        ordered = ranked + loose
        self.assertEqual(ordered[0], "https://x/phone.mp4")
        self.assertIn("https://x/walk.mp4", ordered)
        self.assertNotIn("https://x/game.mp4", ordered)


if __name__ == "__main__":
    unittest.main()
