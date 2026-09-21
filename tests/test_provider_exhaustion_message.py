"""An outage and a cooldown are different things and must read differently."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from yt_auto.script_writer import ScriptWriter


def writer(**cfg) -> ScriptWriter:
    made = ScriptWriter.__new__(ScriptWriter)
    made.cfg = SimpleNamespace(
        provider="routed",
        provider_order=["gemini", "openai_compatible"],
        provider_cooldown_seconds=180,
        **cfg,
    )
    made._provider_backoff_until = {}
    made.last_provider = ""
    made.last_provider_endpoint_host = ""
    made.last_provider_model = ""
    made._record_provider_attempts = lambda attempted: None
    made._provider_safe_details = lambda name: {"endpoint_host": "", "model": ""}
    return made


class MessageTests(unittest.TestCase):
    def call(self, made):
        with self.assertRaises(RuntimeError) as caught:
            made._generate_ai("prompt", purpose="test")
        return str(caught.exception)

    def test_a_real_failure_still_names_the_providers(self):
        made = writer()
        made._generate_gemini = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota"))
        made._generate_openai_compatible = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429"))
        message = self.call(made)
        self.assertIn("All configured AI providers failed", message)
        self.assertIn("quota", message)

    def test_an_all_cooldown_pass_does_not_trail_off_after_a_colon(self):
        import time

        made = writer()
        soon = time.monotonic() + 999
        made._provider_backoff_until = {"gemini": soon, "openai_compatible": soon}
        made._generate_gemini = lambda *a, **k: "never called"
        made._generate_openai_compatible = lambda *a, **k: "never called"
        message = self.call(made)
        self.assertNotIn("failed: ", message)
        self.assertIn("cooling down", message)

    def test_the_cooling_providers_are_named(self):
        import time

        made = writer()
        soon = time.monotonic() + 999
        made._provider_backoff_until = {"gemini": soon, "openai_compatible": soon}
        made._generate_gemini = lambda *a, **k: "x"
        made._generate_openai_compatible = lambda *a, **k: "x"
        message = self.call(made)
        self.assertIn("gemini", message)
        self.assertIn("openai_compatible", message)

    def test_the_wait_is_stated(self):
        import time

        made = writer()
        made._provider_backoff_until = {
            "gemini": time.monotonic() + 999,
            "openai_compatible": time.monotonic() + 999,
        }
        made._generate_gemini = lambda *a, **k: "x"
        made._generate_openai_compatible = lambda *a, **k: "x"
        self.assertIn("180s", self.call(made))


if __name__ == "__main__":
    unittest.main()
