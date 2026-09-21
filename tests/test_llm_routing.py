from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt_auto.channel_analyzer import ChannelAnalyzer
from yt_auto.config import load_config
from yt_auto.models import ScriptWriterConfig
from yt_auto.script_writer import ScriptWriter


def _config(**overrides) -> ScriptWriterConfig:
    values = {
        "provider": "routed",
        "ollama_model": "test-local",
        "ollama_url": "http://localhost:11434/api/generate",
        "timeout_seconds": 10,
        "provider_order": ["gemini", "openai_compatible", "ollama"],
        "provider_cooldown_seconds": 0,
        "ollama_timeout_seconds": 90,
        "openai_compatible_timeout_seconds": 5,
    }
    values.update(overrides)
    return ScriptWriterConfig(**values)


class RoutedScriptWriterTests(unittest.TestCase):
    def test_long_quality_review_samples_beginning_middle_and_end(self) -> None:
        beginning = "BEGIN_MARKER " + ("alpha " * 500)
        middle = ("bravo " * 250) + "MIDDLE_MARKER " + ("bravo " * 250)
        ending = ("charlie " * 500) + "END_MARKER"

        excerpt = ScriptWriter._quality_review_narration(
            beginning + middle + ending,
            "video",
        )

        self.assertIn("[BEGINNING]", excerpt)
        self.assertIn("[MIDDLE]", excerpt)
        self.assertIn("[ENDING]", excerpt)
        self.assertIn("BEGIN_MARKER", excerpt)
        self.assertIn("MIDDLE_MARKER", excerpt)
        self.assertIn("END_MARKER", excerpt)
        self.assertLess(len(excerpt), 3800)

    def test_routed_mode_enables_guarded_ai_refinements(self) -> None:
        self.assertTrue(ScriptWriter(_config(provider="routed"))._ai_enabled())
        self.assertTrue(ScriptWriter(_config(provider="openai_compatible"))._ai_enabled())
        self.assertFalse(ScriptWriter(_config(provider="template"))._ai_enabled())

    def test_routed_provider_falls_back_in_declared_order(self) -> None:
        writer = ScriptWriter(_config())
        with (
            patch.object(writer, "_generate_gemini", side_effect=RuntimeError("quota")),
            patch.object(
                writer,
                "_generate_openai_compatible",
                side_effect=RuntimeError("not configured"),
            ),
            patch.object(writer, "_generate_ollama", return_value="local result"),
        ):
            result = writer._generate_ai("hello", max_output_tokens=50)

        self.assertEqual(result, "local result")
        self.assertEqual(writer.last_provider, "ollama")
        self.assertEqual(
            [item["provider"] for item in writer.provider_attempts],
            ["gemini", "openai_compatible", "ollama"],
        )
        self.assertEqual(writer.provider_attempts[-1]["status"], "ok")

    def test_semantically_invalid_json_falls_through_to_next_provider(self) -> None:
        writer = ScriptWriter(_config())
        invalid_review = json.dumps({
            "score": "not-a-number",
            "verdict": "pass",
            "issues": [],
            "strengths": [],
        })
        valid_review = json.dumps({
            "score": 91,
            "verdict": "pass",
            "issues": [],
            "strengths": ["specific"],
        })
        with (
            patch.object(writer, "_generate_gemini", return_value=invalid_review),
            patch.object(
                writer,
                "_generate_openai_compatible",
                return_value=valid_review,
            ),
            patch.object(writer, "_generate_ollama") as ollama,
        ):
            result = writer._generate_ai(
                "review",
                response_mime_type="application/json",
                purpose="quality_review",
                validator=writer._quality_review_payload_is_valid,
            )

        self.assertEqual(result, valid_review)
        self.assertEqual(
            [item["status"] for item in writer.provider_attempts],
            ["rejected", "ok"],
        )
        self.assertEqual(
            [item["call_purpose"] for item in writer.provider_attempts],
            ["quality_review", "quality_review"],
        )
        self.assertFalse(writer.provider_attempts[0]["accepted_content"])
        self.assertFalse(writer.provider_attempts[1]["accepted_content"])
        self.assertEqual(writer.provider_output_metadata()["last_ai_provider"], "")
        writer._accept_last_ai_content("quality_review")
        self.assertTrue(writer.provider_attempts[1]["accepted_content"])
        self.assertEqual(writer.provider_attempts[1]["accepted_as"], "quality_review")
        self.assertFalse(writer.provider_attempt_history[0]["accepted_content"])
        self.assertTrue(writer.provider_attempt_history[1]["accepted_content"])
        self.assertEqual(
            writer.provider_attempt_history[1]["accepted_as"],
            "quality_review",
        )
        self.assertEqual(
            writer.provider_output_metadata()["last_ai_provider"],
            "openai_compatible",
        )
        self.assertEqual(writer.provider_output_metadata()["script_provider"], "")
        ollama.assert_not_called()

    @staticmethod
    def _review_topic() -> SimpleNamespace:
        return SimpleNamespace(
            content_kind="short",
            title="Why Reply Time Feels Personal",
            hook="You read the same message twice.",
            narration="You read the same message twice, then pause before replying.",
            subject="reply time anxiety",
            narration_beats=["You read the same message twice."],
            trend_terms=[],
            visual_captions=[],
        )

    @staticmethod
    def _review_channel() -> SimpleNamespace:
        return SimpleNamespace(
            id="brain_lens",
            display_name="Brain Lens",
            niche_description="relationship psychology",
        )

    def test_quality_review_retries_after_invalid_gemini_schema(self) -> None:
        writer = ScriptWriter(_config())
        valid_review = json.dumps({
            "score": 94,
            "verdict": "pass",
            "issues": [],
            "strengths": ["Clear behavior-first hook"],
        })
        with (
            patch.object(writer, "_generate_gemini", return_value="not json"),
            patch.object(
                writer,
                "_generate_openai_compatible",
                return_value=valid_review,
            ),
            patch.object(writer, "_generate_ollama") as ollama,
        ):
            review = writer.review_content_quality(
                self._review_channel(),
                self._review_topic(),
                {"duration_seconds": 36, "title": "Why Reply Time Feels Personal"},
            )

        self.assertEqual(review["score"], 94)
        self.assertEqual(review["verdict"], "pass")
        self.assertEqual(
            [item["status"] for item in writer.provider_attempts],
            ["rejected", "ok"],
        )
        self.assertTrue(writer.provider_attempts[-1]["accepted_content"])
        self.assertEqual(writer.provider_attempts[-1]["accepted_as"], "quality_review")
        self.assertEqual(writer.provider_output_metadata()["script_provider"], "")
        ollama.assert_not_called()

    def test_topic_scoring_retries_after_invalid_gemini_schema(self) -> None:
        writer = ScriptWriter(_config())
        invalid_score = json.dumps({
            "score": 99,
            "reasons": "not-an-array",
            "risks": [],
        })
        valid_score = json.dumps({
            "score": 87,
            "reasons": ["specific viewer tension"],
            "risks": ["stock footage repetition"],
        })
        with (
            patch.object(writer, "_generate_gemini", return_value=invalid_score),
            patch.object(
                writer,
                "_generate_openai_compatible",
                return_value=valid_score,
            ),
            patch.object(writer, "_generate_ollama") as ollama,
        ):
            score = writer.score_topic_candidate(
                self._review_channel(),
                self._review_topic(),
            )

        self.assertEqual(score["score"], 87)
        self.assertEqual(
            [item["status"] for item in writer.provider_attempts],
            ["rejected", "ok"],
        )
        self.assertTrue(writer.provider_attempts[-1]["accepted_content"])
        self.assertEqual(writer.provider_attempts[-1]["accepted_as"], "topic_score")
        self.assertEqual(
            writer.provider_output_metadata()["last_ai_provider"],
            "openai_compatible",
        )
        self.assertEqual(writer.provider_output_metadata()["script_provider"], "")
        ollama.assert_not_called()

    def test_explicit_provider_does_not_silently_switch(self) -> None:
        writer = ScriptWriter(_config(provider="gemini", provider_order=[]))
        with patch.object(writer, "_generate_gemini", side_effect=RuntimeError("quota")):
            with self.assertRaisesRegex(RuntimeError, "All configured AI providers failed"):
                writer._generate_ai("hello")
        self.assertEqual([item["provider"] for item in writer.provider_attempts], ["gemini"])

    def test_openai_compatible_endpoint_is_optional_and_standard(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://api.example.com/gateway",
                openai_compatible_model="free-combo",
                openai_compatible_allowed_hosts=["api.example.com"],
                openai_compatible_disable_thinking=True,
            )
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "gateway result"}}]
        }
        response.raise_for_status.return_value = None
        with (
            patch.dict(
                os.environ,
                {
                    "AI_GATEWAY_URL": "",
                    "AI_GATEWAY_MODEL": "",
                    "AI_GATEWAY_API_KEY": "secret-test-key",
                },
                clear=False,
            ),
            patch("yt_auto.script_writer.requests.post", return_value=response) as post,
        ):
            result = writer._generate_ai("hello", max_output_tokens=77)

        self.assertEqual(result, "gateway result")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.example.com/gateway/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "free-combo")
        # Free reasoning models spend their budget thinking before they
        # answer: asked for 96 one returned empty content and stopped on
        # "length". The floor buys room to think and still reply.
        self.assertEqual(kwargs["json"]["max_tokens"], ScriptWriter.MIN_GATEWAY_OUTPUT_TOKENS)
        self.assertTrue(kwargs["json"]["messages"][0]["content"].startswith("/no_think\n"))
        self.assertEqual(
            kwargs["json"]["chat_template_kwargs"],
            {"enable_thinking": False},
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-test-key")
        self.assertFalse(kwargs["allow_redirects"])

    def test_fixed_hugging_face_profile_uses_its_scoped_key(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://router.huggingface.co",
                openai_compatible_model="Qwen/Qwen3-8B",
                openai_compatible_api_key_env="HF_API_KEY",
            )
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "fixed profile result"}}]
        }
        response.raise_for_status.return_value = None
        with (
            patch.dict(
                os.environ,
                {
                    "HF_API_KEY": "hf-profile-key",
                    "AI_GATEWAY_API_KEY": "generic-key-must-not-be-used",
                },
                clear=True,
            ),
            patch("yt_auto.script_writer.requests.post", return_value=response) as post,
        ):
            self.assertEqual(writer._generate_ai("hello"), "fixed profile result")

        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer hf-profile-key",
        )

    def test_gateway_env_override_uses_only_generic_gateway_key(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://router.huggingface.co",
                openai_compatible_model="Qwen/Qwen3-8B",
                openai_compatible_api_key_env="HF_API_KEY",
                openai_compatible_allowed_hosts=[
                    "router.huggingface.co",
                    "api.example.com",
                ],
            )
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "override result"}}]
        }
        response.raise_for_status.return_value = None
        with (
            patch.dict(
                os.environ,
                {
                    "AI_GATEWAY_URL": "https://api.example.com",
                    "AI_GATEWAY_MODEL": "example/model",
                    "AI_GATEWAY_API_KEY": "generic-profile-key",
                    "HF_API_KEY": "hf-key-must-not-leave-hugging-face",
                },
                clear=True,
            ),
            patch("yt_auto.script_writer.requests.post", return_value=response) as post,
        ):
            self.assertEqual(writer._generate_ai("hello"), "override result")

        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer generic-profile-key",
        )
        self.assertNotIn(
            "hf-key-must-not-leave-hugging-face",
            json.dumps(post.call_args.kwargs),
        )

    def test_partial_gateway_env_override_never_mixes_with_fixed_profile(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://router.huggingface.co",
                openai_compatible_model="Qwen/Qwen3-8B",
                openai_compatible_api_key_env="HF_API_KEY",
                openai_compatible_allowed_hosts=[
                    "router.huggingface.co",
                    "api.example.com",
                ],
            )
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AI_GATEWAY_URL": "https://api.example.com",
                    "HF_API_KEY": "hf-key-must-not-be-used",
                },
                clear=True,
            ),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "AI_GATEWAY_URL and AI_GATEWAY_MODEL"),
        ):
            writer._generate_ai("hello")
        post.assert_not_called()

    def test_provider_errors_redact_configured_keys(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://api.example.com",
                openai_compatible_model="test",
                openai_compatible_allowed_hosts=["api.example.com"],
            )
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AI_GATEWAY_URL": "https://api.example.com",
                    "AI_GATEWAY_MODEL": "test",
                    "AI_GATEWAY_API_KEY": "never-print-this",
                },
                clear=False,
            ),
            patch.object(
                writer,
                "_generate_openai_compatible",
                side_effect=RuntimeError("bad token never-print-this"),
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                writer._generate_ai("hello")

        self.assertNotIn("never-print-this", str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))

    def test_loopback_gateway_is_rejected(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="http://localhost:20128",
                openai_compatible_model="free-combo",
                openai_compatible_allowed_hosts=["localhost"],
            )
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "require HTTPS"),
        ):
            writer._generate_ai("hello")
        post.assert_not_called()

    def test_remote_http_gateway_is_rejected_even_when_allowlisted(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="http://api.example.com",
                openai_compatible_model="example/model",
                openai_compatible_allowed_hosts=["api.example.com"],
            )
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "require HTTPS"),
        ):
            writer._generate_ai("hello")

        post.assert_not_called()
        self.assertEqual(writer.provider_attempts[0]["endpoint_host"], "api.example.com")
        self.assertEqual(writer.provider_attempts[0]["model"], "example/model")

    def test_remote_https_gateway_requires_an_exact_allowed_host(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://not-router.huggingface.co",
                openai_compatible_model="Qwen/Qwen3-8B",
            )
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "not allowed by policy"),
        ):
            writer._generate_ai("hello")

        post.assert_not_called()

    def test_allowed_https_gateway_persists_only_safe_provenance(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://api.example.com/gateway",
                openai_compatible_model="example/model-v1",
                openai_compatible_allowed_hosts=["api.example.com"],
            )
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "gateway result"}}]
        }
        response.raise_for_status.return_value = None
        with (
            patch.dict(
                os.environ,
                {"AI_GATEWAY_API_KEY": "secret-test-key"},
                clear=True,
            ),
            patch("yt_auto.script_writer.requests.post", return_value=response) as post,
        ):
            result = writer._generate_ai("hello", purpose="script_draft")

        self.assertEqual(result, "gateway result")
        self.assertEqual(
            post.call_args.args[0],
            "https://api.example.com/gateway/v1/chat/completions",
        )
        self.assertEqual(
            writer.provider_attempts,
            [{
                "provider": "openai_compatible",
                "status": "ok",
                "endpoint_host": "api.example.com",
                "model": "example/model-v1",
                "call_purpose": "script_draft",
                "accepted_content": False,
                "accepted_as": "",
            }],
        )
        unaccepted_metadata = writer.provider_output_metadata()
        self.assertEqual(unaccepted_metadata["last_ai_provider"], "")
        self.assertEqual(unaccepted_metadata["script_provider"], "")

        writer._accept_last_ai_content("script")
        output_metadata = writer.provider_output_metadata()
        self.assertEqual(output_metadata["last_ai_provider"], "openai_compatible")
        self.assertEqual(output_metadata["script_provider"], "openai_compatible")
        self.assertEqual(
            output_metadata["script_provider_endpoint_host"],
            "api.example.com",
        )
        self.assertEqual(output_metadata["script_provider_model"], "example/model-v1")
        self.assertTrue(writer.provider_attempts[0]["accepted_content"])
        self.assertEqual(writer.provider_attempts[0]["accepted_as"], "script")
        self.assertNotIn("secret-test-key", json.dumps(output_metadata))

    def test_accepted_title_never_claims_script_authorship(self) -> None:
        writer = ScriptWriter(_config(provider="ollama", provider_order=[]))
        topic = SimpleNamespace(
            subject="reply time anxiety",
            title="Reply Time Anxiety",
            content_kind="short",
        )
        with patch.object(
            writer,
            "_generate_ollama",
            return_value="Reply Time Anxiety: What the Pattern Actually Means",
        ):
            generated = writer.generate_viral_title(self._review_channel(), topic)

        self.assertEqual(
            generated,
            "Reply Time Anxiety: What the Pattern Actually Means",
        )
        metadata = writer.provider_output_metadata()
        self.assertEqual(metadata["last_ai_provider"], "ollama")
        self.assertEqual(metadata["script_provider"], "")
        self.assertEqual(writer.provider_attempts[0]["accepted_as"], "title")

    def test_selected_topic_controls_script_provenance(self) -> None:
        writer = ScriptWriter(_config(provider="ollama", provider_order=[]))
        with patch.object(writer, "_generate_ollama", return_value="accepted prose"):
            writer._generate_ai("script", purpose="script_draft")
        writer._accept_last_ai_content("script")

        deterministic_topic = SimpleNamespace(
            script_provider="",
            script_provider_endpoint_host="",
            script_provider_model="",
        )
        metadata = writer.provider_output_metadata(deterministic_topic)
        self.assertEqual(metadata["last_ai_provider"], "ollama")
        self.assertEqual(metadata["script_provider"], "")

        ai_topic = SimpleNamespace(
            script_provider="openai_compatible",
            script_provider_endpoint_host="router.huggingface.co",
            script_provider_model="Qwen/Qwen3-8B",
        )
        selected_metadata = writer.provider_output_metadata(ai_topic)
        self.assertEqual(selected_metadata["script_provider"], "openai_compatible")
        self.assertEqual(
            selected_metadata["script_provider_endpoint_host"],
            "router.huggingface.co",
        )

    def test_gateway_credentials_and_query_are_rejected_without_leaking(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://router.huggingface.co",
                openai_compatible_model="Qwen/Qwen3-8B",
            )
        )
        unsafe_url = (
            "https://user:password@router.huggingface.co"
            "?api_key=never-record-this"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AI_GATEWAY_URL": unsafe_url,
                    "AI_GATEWAY_MODEL": "Qwen/Qwen3-8B",
                    "AI_GATEWAY_API_KEY": "secret-test-key",
                },
                clear=True,
            ),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaises(RuntimeError) as raised,
        ):
            writer._generate_ai("hello")

        post.assert_not_called()
        audit_text = json.dumps({
            "error": str(raised.exception),
            "attempts": writer.provider_attempts,
            "output": writer.provider_output_metadata(),
        })
        self.assertNotIn("password", audit_text)
        self.assertNotIn("never-record-this", audit_text)
        self.assertNotIn("secret-test-key", audit_text)
        self.assertEqual(
            writer.provider_attempts[0]["endpoint_host"],
            "router.huggingface.co",
        )

    def test_ollama_timeout_is_bounded(self) -> None:
        response = Mock()
        response.json.return_value = {"response": "local result"}
        response.raise_for_status.return_value = None
        writer = ScriptWriter(
            _config(
                provider="ollama",
                provider_order=[],
                ollama_timeout_seconds=999,
            )
        )
        with patch("yt_auto.script_writer.requests.post", return_value=response) as post:
            self.assertEqual(writer._generate_ai("hello"), "local result")
        self.assertEqual(post.call_args.kwargs["timeout"], 180)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_script_writer_rejects_nonloopback_ollama_before_request(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="ollama",
                provider_order=[],
                ollama_url="http://ollama.example/api/generate",
            )
        )
        with (
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "loopback host"),
        ):
            writer._generate_ai("hello")
        post.assert_not_called()

    def test_channel_analyzer_rejects_nonloopback_ollama(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "loopback host"):
            ChannelAnalyzer(
                "http://ollama.example/api/generate",
                "test-local",
            )

    def test_openai_timeout_is_bounded(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "gateway result"}}]
        }
        response.raise_for_status.return_value = None
        writer = ScriptWriter(
            _config(
                provider="openai_compatible",
                provider_order=[],
                openai_compatible_url="https://api.example.com",
                openai_compatible_model="test",
                openai_compatible_allowed_hosts=["api.example.com"],
                openai_compatible_timeout_seconds=999,
            )
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("yt_auto.script_writer.requests.post", return_value=response) as post,
        ):
            self.assertEqual(writer._generate_ai("hello"), "gateway result")
        self.assertEqual(post.call_args.kwargs["timeout"], 120)

    def test_gemini_rejects_nonofficial_endpoint_before_sending_key(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="gemini",
                provider_order=[],
                gemini_url="https://evil.example/v1beta/models",
            )
        )
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "never-send-this"}, clear=True),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "official HTTPS API host"),
        ):
            writer._generate_ai("hello")
        post.assert_not_called()
        self.assertNotIn("never-send-this", json.dumps(writer.provider_attempts))

    def test_gemini_ignores_legacy_multi_key_variable(self) -> None:
        writer = ScriptWriter(_config(provider="gemini", provider_order=[]))
        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEYS": "trial-one,trial-two"},
                clear=True,
            ),
            patch("yt_auto.script_writer.requests.post") as post,
            self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY is not configured"),
        ):
            writer._generate_ai("hello")
        post.assert_not_called()

    def test_provider_audit_is_cumulative_until_build_reset(self) -> None:
        writer = ScriptWriter(_config(provider="ollama", provider_order=[]))
        with patch.object(writer, "_generate_ollama", side_effect=["first", "second"]):
            self.assertEqual(writer._generate_ai("one"), "first")
            self.assertEqual(writer._generate_ai("two"), "second")
        self.assertEqual(len(writer.provider_attempts), 1)
        self.assertEqual(len(writer.provider_attempt_history), 2)
        self.assertEqual(
            [item["call_index"] for item in writer.provider_attempt_history],
            [1, 2],
        )
        writer.reset_provider_audit()
        self.assertEqual(writer.provider_attempt_history, [])
        self.assertEqual(writer.provider_output_metadata()["last_ai_provider"], "")
        self.assertEqual(writer.provider_output_metadata()["script_provider"], "")

    def test_workspace_config_has_safe_gateway_and_cold_start_defaults(self) -> None:
        root = Path(__file__).resolve().parents[1]
        writer_cfg = load_config(root / "config" / "settings.yaml").app.script_writer
        # The order is a live operational choice, so this pins what matters
        # rather than the exact list: every provider stays reachable, and the
        # one that is actually available leads. Gemini's free quota lets
        # roughly one call through per day, and with it first every run spent
        # its first call finding that out and then cooled the whole chain.
        self.assertEqual(
            sorted(writer_cfg.provider_order),
            ["gemini", "groq", "ollama", "openai_compatible"],
        )
        self.assertEqual(writer_cfg.provider_order[0], "openai_compatible")
        self.assertEqual(writer_cfg.ollama_timeout_seconds, 90)
        self.assertEqual(writer_cfg.ollama_model, "qwen2.5:7b")
        # Groq's free tier is 14,400 calls a day on Llama 3.3 70B and only
        # 1,000 on gpt-oss-120b, on the same account. Volume decides this one.
        self.assertEqual(writer_cfg.groq_model, "llama-3.3-70b-versatile")
        # openrouter.ai leads: the gateway moved there for the free models,
        # and a host missing from this list has every call refused.
        self.assertEqual(
            writer_cfg.openai_compatible_allowed_hosts,
            ["openrouter.ai", "router.huggingface.co", "api.groq.com", "api.mistral.ai"],
        )
        self.assertEqual(writer_cfg.openai_compatible_url, "https://openrouter.ai/api")
        self.assertEqual(writer_cfg.openai_compatible_api_key_env, "OPENROUTER_API_KEY")

    def test_groq_is_tried_after_gemini_in_default_routed_order(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="routed",
                provider_order=["gemini", "groq", "openai_compatible", "ollama"],
            )
        )
        with (
            patch.object(writer, "_generate_gemini", side_effect=RuntimeError("503")),
            patch.object(writer, "_generate_named_openai_compatible", return_value="from groq") as groq,
            patch.object(writer, "_generate_openai_compatible", side_effect=AssertionError("hf")),
            patch.object(writer, "_generate_ollama", side_effect=AssertionError("ollama")),
        ):
            self.assertEqual(writer._generate_ai("prompt", purpose="script_draft"), "from groq")
        groq.assert_called_once()
        self.assertEqual(writer.last_provider, "groq")

    def test_tiny_ollama_refuses_full_script_drafts(self) -> None:
        writer = ScriptWriter(_config(provider="ollama", ollama_model="llama3.2:3b"))
        with self.assertRaisesRegex(RuntimeError, r"refusing full scripts on tiny Ollama"):
            writer._generate_ollama("Write a long narration", purpose="script_draft")

    def test_missing_groq_key_falls_through_to_next_provider(self) -> None:
        writer = ScriptWriter(
            _config(
                provider="routed",
                provider_order=["gemini", "groq", "openai_compatible", "ollama"],
            )
        )
        with (
            patch.object(writer, "_generate_gemini", side_effect=RuntimeError("quota")),
            patch.object(
                writer,
                "_generate_named_openai_compatible",
                side_effect=RuntimeError("groq API key is missing"),
            ),
            patch.object(writer, "_generate_openai_compatible", return_value="from hf"),
            patch.object(writer, "_generate_ollama", side_effect=AssertionError("ollama")),
        ):
            self.assertEqual(writer._generate_ai("prompt", purpose="script_draft"), "from hf")
        self.assertEqual(writer.last_provider, "openai_compatible")
        self.assertEqual(
            [item["provider"] for item in writer.provider_attempts],
            ["gemini", "groq", "openai_compatible"],
        )


if __name__ == "__main__":
    unittest.main()
