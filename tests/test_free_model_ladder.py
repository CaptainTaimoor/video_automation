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



class LadderSeesTheValidatorTests(unittest.TestCase):
    """A 200 with unusable text must move the ladder on, not end the provider.

    Free models answer with a moderation header, with their own thinking, or
    with a beats array the token cap cut in half. All three are HTTP 200, so
    with the check applied only above the ladder the first model decided the
    whole run: the provider went on cooldown and the five behind it were never
    asked. Every script in that run then came from the template writer.
    """

    def build(self, replies):
        writer = ScriptWriter.__new__(ScriptWriter)
        writer.cfg = SimpleNamespace(
            openai_compatible_url="https://openrouter.ai/api",
            openai_compatible_model="first:free",
            openai_compatible_models=["first:free", "second:free", "third:free"],
            openai_compatible_api_key_env="OPENROUTER_API_KEY",
        )
        self.asked = []

        def fake_once(prompt, model, base_url, api_key, **kwargs):
            self.asked.append(model)
            outcome = replies[model]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        writer._openai_compatible_settings = lambda: (
            "https://openrouter.ai/api", "first:free", "k"
        )
        writer._generate_openai_compatible_once = fake_once
        return writer

    @staticmethod
    def wants_two_beats(raw: str) -> bool:
        import json

        try:
            beats = json.loads(raw).get("beats")
        except Exception:
            return False
        return isinstance(beats, list) and len(beats) == 2

    def test_a_moderation_header_moves_the_ladder_on(self):
        writer = self.build({
            "first:free": "User Safety: safe",
            "second:free": '{"beats": ["a", "b"]}',
            "third:free": '{"beats": ["c", "d"]}',
        })
        got = writer._generate_openai_compatible(
            "p", validator=self.wants_two_beats
        )
        self.assertEqual(got, '{"beats": ["a", "b"]}')
        self.assertEqual(self.asked, ["first:free", "second:free"])

    def test_an_answer_cut_off_by_the_token_cap_moves_the_ladder_on(self):
        writer = self.build({
            "first:free": '{"beats": ["a", "b"',
            "second:free": '{"beats": ["only one"]}',
            "third:free": '{"beats": ["c", "d"]}',
        })
        got = writer._generate_openai_compatible(
            "p", validator=self.wants_two_beats
        )
        self.assertEqual(got, '{"beats": ["c", "d"]}')
        self.assertEqual(self.asked, ["first:free", "second:free", "third:free"])

    def test_the_first_usable_answer_stops_the_ladder(self):
        writer = self.build({
            "first:free": '{"beats": ["a", "b"]}',
            "second:free": AssertionError("must not be asked"),
            "third:free": AssertionError("must not be asked"),
        })
        writer._generate_openai_compatible("p", validator=self.wants_two_beats)
        self.assertEqual(self.asked, ["first:free"])

    def test_without_a_validator_the_first_reply_is_taken(self):
        writer = self.build({
            "first:free": "User Safety: safe",
            "second:free": '{"beats": ["a", "b"]}',
            "third:free": '{"beats": ["c", "d"]}',
        })
        self.assertEqual(writer._generate_openai_compatible("p"), "User Safety: safe")

    def test_every_model_failing_the_check_is_reported_as_a_failure(self):
        writer = self.build({
            "first:free": "nope",
            "second:free": "also nope",
            "third:free": "still nope",
        })
        with self.assertRaises(RuntimeError) as caught:
            writer._generate_openai_compatible("p", validator=self.wants_two_beats)
        self.assertIn("unusable answer", str(caught.exception))
        self.assertEqual(self.asked, ["first:free", "second:free", "third:free"])

    def test_a_validator_that_raises_counts_as_unusable(self):
        def explodes(_raw):
            raise ValueError("bad validator")

        writer = self.build({
            "first:free": "anything",
            "second:free": "anything",
            "third:free": "anything",
        })
        with self.assertRaises(RuntimeError):
            writer._generate_openai_compatible("p", validator=explodes)
        self.assertEqual(self.asked, ["first:free", "second:free", "third:free"])


class RewriteTokenBudgetTests(unittest.TestCase):
    """The reply has to fit, or the beat count check reads a cut-off answer."""

    def test_a_short_gets_more_than_the_old_flat_budget(self):
        self.assertGreater(ScriptWriter._rewrite_token_budget(9), 512)

    def test_a_long_video_gets_far_more_than_a_short(self):
        self.assertGreater(
            ScriptWriter._rewrite_token_budget(25),
            ScriptWriter._rewrite_token_budget(9) * 2,
        )

    def test_the_budget_grows_with_the_beat_count(self):
        budgets = [ScriptWriter._rewrite_token_budget(n) for n in (7, 9, 14, 25)]
        self.assertEqual(budgets, sorted(budgets))
        self.assertEqual(len(set(budgets)), len(budgets))

    def test_a_tiny_script_still_leaves_room_to_think(self):
        self.assertGreaterEqual(ScriptWriter._rewrite_token_budget(1), 512)

    def test_a_nonsense_beat_count_does_not_crash(self):
        self.assertGreaterEqual(ScriptWriter._rewrite_token_budget(0), 512)

    def test_the_budget_is_capped_so_one_call_cannot_run_away(self):
        self.assertLessEqual(ScriptWriter._rewrite_token_budget(10_000), 4000)
if __name__ == "__main__":
    unittest.main()
