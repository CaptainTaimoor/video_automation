"""Never spend money by accident, and do not give up on a provider too soon."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt_auto.config import load_config
from yt_auto.script_writer import ScriptWriter

ROOT = Path(__file__).resolve().parents[1]


def writer(**cfg) -> ScriptWriter:
    made = ScriptWriter.__new__(ScriptWriter)
    base = dict(
        openai_compatible_url="https://openrouter.ai/api",
        openai_compatible_model="a:free",
        openai_compatible_models=["a:free"],
        openai_compatible_api_key_env="OPENROUTER_API_KEY",
        openai_compatible_allowed_hosts=["openrouter.ai"],
        openai_compatible_disable_thinking=True,
        openai_compatible_zero_cost_only=True,
        openai_compatible_timeout_seconds=30,
        provider="routed",
        provider_order=["openai_compatible"],
        provider_cooldown_seconds=90,
    )
    base.update(cfg)
    made.cfg = SimpleNamespace(**base)
    made._provider_backoff_until = {}
    return made


class ZeroCostGuardTests(unittest.TestCase):
    def sent_payload(self, **cfg):
        made = writer(**cfg)
        captured = {}

        class Reply:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "hi"}}]}

            @staticmethod
            def raise_for_status():
                return None

        def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=None):
            captured.update(json or {})
            return Reply()

        with patch("yt_auto.script_writer.requests.post", fake_post):
            made._generate_openai_compatible_once(
                "p", "a:free", "https://openrouter.ai/api", "k"
            )
        return captured

    def test_every_call_refuses_to_pay(self):
        guard = self.sent_payload().get("provider")
        self.assertEqual(guard["max_price"], {"prompt": 0, "completion": 0})

    def test_it_also_refuses_a_paid_fallback(self):
        self.assertIs(self.sent_payload()["provider"]["allow_fallbacks"], False)

    def test_the_guard_can_be_turned_off_deliberately(self):
        self.assertNotIn(
            "provider", self.sent_payload(openai_compatible_zero_cost_only=False)
        )

    def test_the_guard_is_on_by_default_in_the_real_config(self):
        cfg = load_config(ROOT / "config" / "settings.yaml").app.script_writer
        self.assertTrue(cfg.openai_compatible_zero_cost_only)

    def test_a_writer_with_no_such_setting_still_refuses_to_pay(self):
        made = ScriptWriter.__new__(ScriptWriter)
        made.cfg = SimpleNamespace()
        self.assertTrue(made._zero_cost_only())


class ProviderRetryTests(unittest.TestCase):
    def build(self, outcomes):
        made = writer(provider_order=["openai_compatible"])
        made.last_provider = ""
        made.last_provider_endpoint_host = ""
        made.last_provider_model = ""
        made._record_provider_attempts = lambda attempted: None
        made._provider_safe_details = lambda name: {"endpoint_host": "", "model": ""}
        self.calls = []

        def generate(*args, **kwargs):
            outcome = outcomes[len(self.calls)]
            self.calls.append(outcome)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        made._generate_openai_compatible = generate
        return made

    def test_a_provider_is_asked_twice_before_the_chain_moves_on(self):
        made = self.build([RuntimeError("429"), "good answer"])
        self.assertEqual(made._generate_ai("p", purpose="t"), "good answer")
        self.assertEqual(len(self.calls), 2)

    def test_a_first_success_is_not_retried(self):
        made = self.build(["good answer", RuntimeError("must not be asked")])
        made._generate_ai("p", purpose="t")
        self.assertEqual(len(self.calls), 1)

    def test_an_empty_first_reply_is_retried(self):
        made = self.build(["", "good answer"])
        self.assertEqual(made._generate_ai("p", purpose="t"), "good answer")
        self.assertEqual(len(self.calls), 2)

    def test_two_failures_give_up_on_that_provider(self):
        made = self.build([RuntimeError("429"), RuntimeError("429")])
        with self.assertRaises(RuntimeError):
            made._generate_ai("p", purpose="t")
        self.assertEqual(len(self.calls), 2)

    def test_the_attempt_count_is_stated_once(self):
        self.assertEqual(ScriptWriter.PROVIDER_ATTEMPTS, 2)


if __name__ == "__main__":
    unittest.main()


class TemperatureTests(unittest.TestCase):
    """Write creatively, judge consistently."""

    def test_writing_gets_room_to_surprise(self):
        for purpose in ("scene_rewrite", "title_generation", "script_draft",
                        "script_expansion"):
            with self.subTest(purpose=purpose):
                self.assertEqual(
                    ScriptWriter._temperature_for(purpose),
                    ScriptWriter.CREATIVE_TEMPERATURE,
                )

    def test_judging_is_near_deterministic(self):
        for purpose in ("quality_review", "topic_scoring"):
            with self.subTest(purpose=purpose):
                self.assertEqual(
                    ScriptWriter._temperature_for(purpose),
                    ScriptWriter.ANALYTICAL_TEMPERATURE,
                )

    def test_a_score_is_colder_than_a_draft(self):
        # A video that passes at 92 on Monday must not be held at 86 on
        # Tuesday because the scorer felt creative.
        self.assertLess(
            ScriptWriter.ANALYTICAL_TEMPERATURE,
            ScriptWriter.CREATIVE_TEMPERATURE,
        )

    def test_an_unknown_purpose_is_treated_as_writing(self):
        self.assertEqual(
            ScriptWriter._temperature_for("something_new"),
            ScriptWriter.CREATIVE_TEMPERATURE,
        )

    def test_a_blank_purpose_does_not_crash(self):
        self.assertEqual(
            ScriptWriter._temperature_for(""), ScriptWriter.CREATIVE_TEMPERATURE
        )

    def test_the_gateway_sends_the_temperature_it_was_given(self):
        made = writer()
        captured = {}

        class Reply:
            status_code = 200
            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "hi"}}]}
            @staticmethod
            def raise_for_status():
                return None

        def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=None):
            captured.update(json or {})
            return Reply()

        with patch("yt_auto.script_writer.requests.post", fake_post):
            made._generate_openai_compatible_once(
                "p", "a:free", "https://openrouter.ai/api", "k", temperature=0.2
            )
        self.assertAlmostEqual(captured["temperature"], 0.2)


class RateLimitWaitTests(unittest.TestCase):
    """A short rate limit is cheaper to wait out than to route around.

    Groq allows 30 requests a minute against 1,000 a day, so a burst trips
    the per-minute limit with the day barely touched. Leaving on that costs
    minutes: the next provider's fastest free model takes 59 seconds and its
    slowest 313, against three seconds on Groq.
    """

    class Reply:
        def __init__(self, headers):
            self.headers = headers

    def parse(self, headers):
        return ScriptWriter._retry_after_seconds(self.Reply(headers))

    def test_a_plain_seconds_header_is_read(self):
        self.assertEqual(self.parse({"retry-after": "3"}), 3.0)

    def test_groqs_own_duration_format_is_read(self):
        self.assertAlmostEqual(
            self.parse({"x-ratelimit-reset-requests": "1m26.4s"}), 86.4
        )

    def test_hours_are_read_too(self):
        self.assertAlmostEqual(
            self.parse({"x-ratelimit-reset-tokens": "9h59m2.4s"}), 35942.4
        )

    def test_no_header_means_no_opinion(self):
        self.assertIsNone(self.parse({}))

    def test_nonsense_does_not_crash(self):
        self.assertIsNone(self.parse({"retry-after": "soon"}))

    def test_a_short_wait_is_inside_the_cap(self):
        self.assertLessEqual(self.parse({"retry-after": "3"}),
                             ScriptWriter.MAX_RATE_LIMIT_WAIT_SECONDS)

    def test_a_minute_long_wait_is_not_worth_waiting_for(self):
        self.assertGreater(self.parse({"x-ratelimit-reset-requests": "1m26.4s"}),
                           ScriptWriter.MAX_RATE_LIMIT_WAIT_SECONDS)

    def test_the_cap_is_shorter_than_the_alternative(self):
        # The fallback provider's best free model was timed at 59 seconds.
        self.assertLess(ScriptWriter.MAX_RATE_LIMIT_WAIT_SECONDS, 59)
