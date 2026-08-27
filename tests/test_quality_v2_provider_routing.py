import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from yt_auto.models import ScriptWriterConfig
from yt_auto.quality_v2.state import QualityStateStore, RetryClass
from yt_auto.script_writer import ScriptWriter


def _writer(order):
    return ScriptWriter(
        ScriptWriterConfig(
            provider="routed",
            ollama_model="llama3.2:3b",
            ollama_url="http://localhost:11434/api/generate",
            timeout_seconds=20,
            provider_order=order,
        )
    )


class ProviderRoutingTests(unittest.TestCase):
    def test_groq_follows_gemini_as_a_separate_free_route(self):
        writer = _writer(["gemini", "groq", "ollama"])
        with patch.object(writer, "_generate_gemini", side_effect=RuntimeError("HTTP 429 quota")), patch.object(
            writer, "_generate_groq", return_value="good draft"
        ) as groq:
            self.assertEqual("good draft", writer._generate_ai("prompt", purpose="script_draft"))
        groq.assert_called_once()
        self.assertEqual("groq", writer.last_provider)

    def test_schema_rejection_does_not_open_provider_cooldown(self):
        writer = _writer(["gemini", "groq"])
        with patch.object(writer, "_generate_gemini", return_value="not json"), patch.object(
            writer, "_generate_groq", return_value='{"ok": true}'
        ):
            output = writer._generate_ai("prompt", validator=lambda value: value.startswith("{"))
        self.assertEqual('{"ok": true}', output)
        self.assertNotIn("gemini", writer._provider_backoff_until)

    def test_persistent_circuit_skips_dead_provider_after_restart_safe_failures(self):
        writer = _writer(["gemini", "groq"])
        with tempfile.TemporaryDirectory() as temporary, QualityStateStore(Path(temporary) / "quality.sqlite") as store:
            model = str(writer._provider_safe_details("gemini").get("model") or "default")
            for _ in range(3):
                store.record_provider_result(
                    "gemini", model, "script_draft", False, RetryClass.TRANSIENT
                )
            writer.attach_quality_state(store)
            with patch.object(writer, "_generate_gemini") as gemini, patch.object(
                writer, "_generate_groq", return_value="good draft"
            ):
                self.assertEqual("good draft", writer._generate_ai("prompt", purpose="script_draft"))
            gemini.assert_not_called()
        self.assertIn(
            "persistent_circuit_open",
            [entry["status"] for entry in writer.provider_attempts],
        )
