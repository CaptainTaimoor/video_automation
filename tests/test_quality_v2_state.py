from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from yt_auto.quality_v2.state import QualityStateStore, RetryClass, classify_failure, content_hash


class StateStoreTests(unittest.TestCase):
    def test_failure_classification(self):
        self.assertIs(RetryClass.QUOTA, classify_failure(429))
        self.assertIs(RetryClass.TRANSIENT, classify_failure(503))
        self.assertIs(RetryClass.CONTENT, classify_failure(400, "invalid schema"))
        self.assertIs(RetryClass.PERMANENT, classify_failure(401))

    def test_checkpoint_and_cache_survive_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quality.sqlite"
            key = content_hash("research", {"topic": "Rome"})
            with QualityStateStore(path) as store:
                store.start_job("job", "ancient_history", "short")
                store.checkpoint("job", "research", key, "complete", "research.json", {"sources": 2})
                store.cache_artifact(key, "research", "research.json", {"sources": 2})
            with QualityStateStore(path) as store:
                self.assertEqual("complete", store.checkpoint_for("job", "research", key)["state"])
                self.assertEqual(2, store.cached_artifact(key)["metadata"]["sources"])

    def test_circuit_opens_after_three_transient_failures(self):
        with tempfile.TemporaryDirectory() as temporary, QualityStateStore(Path(temporary) / "quality.sqlite") as store:
            for _ in range(3):
                decision = store.record_provider_result("gemini", "model", "draft", False, RetryClass.TRANSIENT)
            self.assertFalse(decision.allowed)
            self.assertEqual("open", store.circuit_decision("gemini", "model", "draft").state)

    def test_circuit_uses_last_ten_results_not_lifetime_failure_rate(self):
        with tempfile.TemporaryDirectory() as temporary, QualityStateStore(Path(temporary) / "quality.sqlite") as store:
            for success in [True, False] * 5:
                decision = store.record_provider_result(
                    "groq",
                    "model",
                    "review",
                    success,
                    None if success else RetryClass.TRANSIENT,
                )
            self.assertFalse(decision.allowed)
            self.assertEqual("open", store.circuit_decision("groq", "model", "review").state)

    def test_poison_input_is_held_after_repeat(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temporary, QualityStateStore(Path(temporary) / "quality.sqlite") as store:
            self.assertFalse(store.quarantine("caption-hash", "caption CPS", now=now))
            self.assertTrue(store.quarantine("caption-hash", "caption CPS", now=now + timedelta(minutes=1)))
            self.assertTrue(store.is_quarantined("caption-hash", now=now + timedelta(minutes=2)))
