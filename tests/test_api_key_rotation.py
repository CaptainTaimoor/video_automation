"""Several keys per provider, tried in the right order, never leaked to the page."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from yt_auto import api_keys


def store_with(**providers) -> api_keys.KeyStore:
    store = api_keys.KeyStore()
    for provider, values in providers.items():
        for value in values:
            store.add(provider, value, label=value[-2:])
    return store


class MaskingTests(unittest.TestCase):
    def test_a_key_is_never_shown_whole(self):
        key = api_keys.ApiKey(value="sk-abcdefghijklmnop")
        self.assertNotIn("cdefghijklm", key.masked)
        self.assertTrue(key.masked.startswith("sk-a"))
        self.assertTrue(key.masked.endswith("mnop"))

    def test_a_short_key_is_fully_hidden(self):
        self.assertEqual(api_keys.ApiKey(value="abc").masked, "***")

    def test_the_public_view_carries_no_key_value(self):
        store = store_with(deepgram=["sk-deepgram-secret-value"])
        blob = repr(store.public_view())
        self.assertNotIn("secret-value", blob)
        self.assertIn("masked", blob)


class RotationOrderTests(unittest.TestCase):
    """Same key a few times, then the next key, then the next provider."""

    def test_attempts_on_one_key_come_before_the_next_key(self):
        store = store_with(deepgram=["key-aa", "key-bb"])
        plan = api_keys.describe_plan(api_keys.attempt_plan(store, attempts_per_key=3))
        self.assertEqual(
            plan,
            [
                "deepgram aa try 1", "deepgram aa try 2", "deepgram aa try 3",
                "deepgram bb try 1", "deepgram bb try 2", "deepgram bb try 3",
            ],
        )

    def test_providers_follow_the_configured_order(self):
        store = store_with(gemini=["g-1"], deepgram=["d-1"])
        self.assertEqual(store.ordered_providers(), ["deepgram", "gemini"])

    def test_a_paused_key_is_skipped(self):
        store = store_with(deepgram=["key-aa", "key-bb"])
        store.set_paused("deepgram", 0, True)
        self.assertEqual([k.label for k in store.keys_for("deepgram")], ["bb"])

    def test_a_resting_key_is_skipped_until_its_cooldown_passes(self):
        store = store_with(deepgram=["key-aa"])
        store.providers["deepgram"][0].failed_until = time.time() + 300
        self.assertEqual(store.keys_for("deepgram"), [])
        self.assertEqual(len(store.keys_for("deepgram", now=time.time() + 400)), 1)


class RenderRotationTests(unittest.TestCase):
    def test_the_first_working_key_wins_and_nothing_else_is_tried(self):
        store = store_with(deepgram=["key-aa", "key-bb"])
        seen = []

        ok, used = api_keys.render_with_rotation(
            store, lambda p, k, a: (seen.append((p, k.label, a)), True)[1]
        )
        self.assertTrue(ok)
        self.assertEqual(used, "deepgram:aa")
        self.assertEqual(seen, [("deepgram", "aa", 1)])

    def test_a_flaky_provider_is_retried_on_the_same_key(self):
        store = store_with(deepgram=["key-aa", "key-bb"])
        seen = []

        def render(provider, key, attempt):
            seen.append((key.label, attempt))
            return attempt == 2  # succeeds on the retry

        ok, used = api_keys.render_with_rotation(store, render)
        self.assertTrue(ok)
        self.assertEqual(used, "deepgram:aa")
        self.assertEqual(seen, [("aa", 1), ("aa", 2)])

    def test_a_dead_key_hands_over_to_the_next_one(self):
        store = store_with(deepgram=["key-aa", "key-bb"])
        ok, used = api_keys.render_with_rotation(
            store, lambda p, k, a: k.label == "bb", attempts_per_key=2
        )
        self.assertTrue(ok)
        self.assertEqual(used, "deepgram:bb")

    def test_a_dead_provider_hands_over_to_the_next_provider(self):
        store = store_with(deepgram=["d-1"], openrouter=["o-1"])
        ok, used = api_keys.render_with_rotation(
            store, lambda p, k, a: p == "openrouter", attempts_per_key=2
        )
        self.assertTrue(ok)
        self.assertTrue(used.startswith("openrouter"))

    def test_everything_failing_falls_back_to_edge(self):
        store = store_with(deepgram=["d-1"])
        ok, used = api_keys.render_with_rotation(
            store, lambda p, k, a: False, fallback=lambda: True, attempts_per_key=2
        )
        self.assertTrue(ok)
        self.assertEqual(used, "edge")

    def test_an_exception_counts_as_a_failed_attempt_not_a_crash(self):
        store = store_with(deepgram=["d-1"])

        def render(provider, key, attempt):
            raise RuntimeError("provider is having a bad day")

        ok, used = api_keys.render_with_rotation(store, render, fallback=lambda: True, attempts_per_key=2)
        self.assertTrue(ok)
        self.assertEqual(used, "edge")

    def test_an_exhausted_key_is_rested(self):
        store = store_with(deepgram=["d-1"])
        api_keys.render_with_rotation(store, lambda p, k, a: False, attempts_per_key=2)
        self.assertFalse(store.providers["deepgram"][0].available())

    def test_no_keys_at_all_still_reaches_edge(self):
        ok, used = api_keys.render_with_rotation(
            api_keys.KeyStore(), lambda p, k, a: False, fallback=lambda: True
        )
        self.assertTrue(ok)
        self.assertEqual(used, "edge")


class PersistenceTests(unittest.TestCase):
    def test_keys_survive_a_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = store_with(deepgram=["key-aa"])
            store.providers["deepgram"][0].model = "aura-2"
            store.save(root)
            again = api_keys.KeyStore.load(root)
            self.assertEqual(again.providers["deepgram"][0].value, "key-aa")
            self.assertEqual(again.providers["deepgram"][0].model, "aura-2")

    def test_the_store_lives_inside_the_gitignored_secrets_directory(self):
        self.assertEqual(api_keys.store_path(Path("/tmp/x")).parent.name, "secrets")

    def test_a_corrupt_store_does_not_stop_the_bot_narrating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = api_keys.store_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(api_keys.KeyStore.load(root).providers, {})

    def test_environment_keys_are_adopted_once(self):
        store = api_keys.KeyStore()
        env = {"DEEPGRAM_API_KEY": "d-env", "GEMINI_API_KEY": "g-env"}
        self.assertEqual(store.merge_environment(env), 2)
        self.assertEqual(store.merge_environment(env), 0, "must not duplicate on a second pass")

    def test_an_edited_key_is_not_overwritten_by_a_stale_env_value(self):
        store = store_with(deepgram=["d-new"])
        store.merge_environment({"DEEPGRAM_API_KEY": "d-old"})
        self.assertEqual([k.value for k in store.providers["deepgram"]], ["d-new", "d-old"])


if __name__ == "__main__":
    unittest.main()
