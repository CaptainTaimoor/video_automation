"""Kokoro is handed phonemes for the names it guesses wrong."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from yt_auto.tts_engine import NarrationEngine

ROOT = Path(__file__).resolve().parents[1]


def engine() -> NarrationEngine:
    return NarrationEngine.__new__(NarrationEngine)


class MarkupTests(unittest.TestCase):
    def setUp(self):
        self.engine = engine()

    def test_a_name_kokoro_mispronounces_is_given_phonemes(self):
        out = self.engine._with_kokoro_phonemes("The Nazca lines run north.")
        self.assertIn("[Nazca](/", out)
        self.assertTrue(out.startswith("The [Nazca](/"))

    def test_a_sentence_with_nothing_to_fix_is_left_exactly_alone(self):
        text = "Temple pyramids rise above the reservoirs."
        self.assertEqual(self.engine._with_kokoro_phonemes(text), text)

    def test_several_names_in_one_sentence_are_all_marked(self):
        out = self.engine._with_kokoro_phonemes("Nazca and Teotihuacan and Axum.")
        self.assertEqual(out.count("](/"), 3)

    def test_the_visible_spelling_is_the_one_that_was_written(self):
        out = self.engine._with_kokoro_phonemes("the nazca desert")
        self.assertIn("[nazca](/", out)

    def test_a_longer_name_wins_over_a_shorter_one_inside_it(self):
        out = self.engine._with_kokoro_phonemes("Chichen Itza at dawn.")
        self.assertEqual(out.count("](/"), 1)
        self.assertIn("[Chichen Itza](/", out)

    def test_a_name_inside_another_word_is_not_touched(self):
        self.assertNotIn("](/", self.engine._with_kokoro_phonemes("Nazcalike patterns."))

    def test_already_marked_text_is_not_marked_again(self):
        once = self.engine._with_kokoro_phonemes("The Nazca lines.")
        self.assertEqual(self.engine._with_kokoro_phonemes(once), once)

    def test_empty_text_is_returned_unchanged(self):
        self.assertEqual(self.engine._with_kokoro_phonemes(""), "")

    def test_a_possessive_keeps_its_ending_outside_the_markup(self):
        out = self.engine._with_kokoro_phonemes("Nazca's desert.")
        self.assertIn("[Nazca](/", out)
        self.assertTrue(out.rstrip().endswith("'s desert."))


class OverrideTableTests(unittest.TestCase):
    def setUp(self):
        self.overrides = engine()._kokoro_phoneme_overrides()

    def test_the_voices_file_can_add_a_name_without_touching_code(self):
        data = yaml.safe_load((ROOT / "assets" / "voices" / "pronunciations.yaml").read_text(encoding="utf-8"))
        for term in data.get("ipa", {}):
            with self.subTest(term=term):
                self.assertIn(term, self.overrides)

    def test_no_override_carries_the_slashes_that_wrap_it(self):
        for term, phonemes in self.overrides.items():
            with self.subTest(term=term):
                self.assertNotIn("/", phonemes, f"{term} would produce empty markup")

    def test_names_kokoro_already_says_correctly_are_left_out(self):
        # Checked against the live G2P: overriding these could only make them
        # worse, and a wrong override is harder to notice than a wrong guess.
        for term in ("Tikal", "stelae", "Thermopylae", "Xerxes", "Lascaux", "Euphrates"):
            with self.subTest(term=term):
                self.assertNotIn(term, self.overrides)

    def test_the_respelling_table_is_not_reused_for_kokoro(self):
        # Kokoro's G2P reads "NAHZ kah" as the letters N-A-H-Z.
        for phonemes in self.overrides.values():
            with self.subTest(phonemes=phonemes):
                self.assertFalse(
                    phonemes.upper() == phonemes and phonemes.isascii(),
                    "an all-caps respelling would be read as an initialism",
                )


class FallbackSafetyTests(unittest.TestCase):
    """Piper, Edge and pyttsx3 would read the markup aloud as punctuation."""

    def test_the_shared_normalizer_adds_no_markup(self):
        made = engine()
        made.backend_preference = "kokoro"
        self.assertNotIn("](/", made._normalize_text("The Nazca lines run north."))


if __name__ == "__main__":
    unittest.main()
