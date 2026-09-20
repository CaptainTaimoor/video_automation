"""Free models for script writing, tried in turn, and read correctly.

A free model answers 429 about as often as it answers, so one of them is not
a provider -- asked back to back, four of seven were busy. It also thinks out
loud and restates the example from the question, which a naive reader took as
the answer.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from yt_auto.config import load_config
from yt_auto.script_writer import ScriptWriter

ROOT = Path(__file__).resolve().parents[1]


def writer_with(**cfg) -> ScriptWriter:
    writer = ScriptWriter.__new__(ScriptWriter)
    writer.cfg = SimpleNamespace(**cfg)
    return writer


class LadderTests(unittest.TestCase):
    def test_the_configured_ladder_is_used_in_order(self):
        writer = writer_with(
            openai_compatible_model="openrouter/free",
            openai_compatible_models=["openrouter/free", "a:free", "b:free"],
        )
        self.assertEqual(writer._openai_compatible_models(), ["openrouter/free", "a:free", "b:free"])

    def test_the_headline_model_leads_even_if_absent_from_the_list(self):
        writer = writer_with(openai_compatible_model="first:free", openai_compatible_models=["b:free"])
        self.assertEqual(writer._openai_compatible_models()[0], "first:free")

    def test_a_single_model_still_works(self):
        writer = writer_with(openai_compatible_model="only:free", openai_compatible_models=[])
        self.assertEqual(writer._openai_compatible_models(), ["only:free"])

    def test_blank_entries_are_ignored(self):
        writer = writer_with(openai_compatible_model="", openai_compatible_models=["a:free", "", "  "])
        self.assertEqual(writer._openai_compatible_models(), ["a:free"])


class ConfiguredForFreeTests(unittest.TestCase):
    """The account's credit must not be spent; it only raises the free limit."""

    def setUp(self):
        self.cfg = load_config(ROOT / "config" / "settings.yaml").app.script_writer

    def test_the_gateway_points_at_openrouter(self):
        self.assertEqual(self.cfg.openai_compatible_url, "https://openrouter.ai/api")

    def test_openrouters_own_key_is_used_not_the_hugging_face_one(self):
        self.assertEqual(self.cfg.openai_compatible_api_key_env, "OPENROUTER_API_KEY")

    def test_every_model_in_the_ladder_is_a_free_one(self):
        for model in self.cfg.openai_compatible_models:
            with self.subTest(model=model):
                self.assertTrue(
                    model.endswith(":free") or model == "openrouter/free",
                    f"{model} is not a free model, so it would spend credit",
                )

    def test_there_is_more_than_one_model_to_fall_back_to(self):
        self.assertGreater(len(self.cfg.openai_compatible_models), 1)

    def test_the_host_is_allowed_or_every_call_is_refused(self):
        self.assertIn("openrouter.ai", self.cfg.openai_compatible_allowed_hosts)

    def test_thinking_out_loud_stays_suppressed(self):
        self.assertTrue(self.cfg.openai_compatible_disable_thinking)


class AnswerReadingTests(unittest.TestCase):
    def setUp(self):
        self.writer = ScriptWriter.__new__(ScriptWriter)

    def test_a_clean_reply_is_read_as_is(self):
        self.assertEqual(self.writer._json_from_ai('{"beats": ["one", "two"]}'), {"beats": ["one", "two"]})

    def test_the_answer_wins_over_the_thinking_before_it(self):
        raw = 'We need JSON only: {"beats": ["...", "..."]}. Now the answer.\n{"beats": ["real one", "real two"]}'
        self.assertEqual(self.writer._json_from_ai(raw), {"beats": ["real one", "real two"]})

    def test_a_fenced_answer_is_unwrapped(self):
        self.assertEqual(self.writer._json_from_ai('```json\n{"beats": ["a", "b"]}\n```'), {"beats": ["a", "b"]})

    def test_the_last_answer_wins_when_a_model_revises_itself(self):
        raw = '{"beats": ["draft one"]}\nActually, better:\n{"beats": ["final one"]}'
        self.assertEqual(self.writer._json_from_ai(raw), {"beats": ["final one"]})

    def test_a_reply_with_no_json_gives_nothing(self):
        self.assertEqual(self.writer._json_from_ai("sorry, I cannot do that"), {})

    def test_an_empty_reply_gives_nothing(self):
        self.assertEqual(self.writer._json_from_ai(""), {})

    def test_placeholder_wording_is_recognised_as_an_echo(self):
        self.assertTrue(ScriptWriter._looks_like_prompt_echo({"beats": ["...", "..."]}))
        self.assertTrue(ScriptWriter._looks_like_prompt_echo({"beats": ["first beat here"]}))

    def test_a_real_answer_is_not_mistaken_for_an_echo(self):
        self.assertFalse(ScriptWriter._looks_like_prompt_echo({"beats": ["They text one dry word."]}))

    def test_an_empty_object_counts_as_an_echo_not_an_answer(self):
        self.assertTrue(ScriptWriter._looks_like_prompt_echo({}))


if __name__ == "__main__":
    unittest.main()
