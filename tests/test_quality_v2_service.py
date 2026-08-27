import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from yt_auto.quality_v2.service import QualityV2Service


class QualityV2ServiceTests(unittest.TestCase):
    def test_imports_only_uploaded_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "topic.json").write_text(json.dumps({"title": "Old", "narration": "A fact. " * 20}), encoding="utf-8")
            runs = root / "runs.jsonl"
            runs.write_text(json.dumps({"uploaded": True, "run_dir": str(run_dir)}) + "\n", encoding="utf-8")
            service = QualityV2Service(root / "state")
            try:
                self.assertEqual(1, service.import_history(runs)["imported"])
                self.assertEqual(1, service.status()["titles"])
            finally:
                service.close()

    def test_history_topic_requires_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = QualityV2Service(Path(temporary))
            try:
                topic = SimpleNamespace(
                    title="Evidence title",
                    narration="A surviving tablet describes the event. " * 20,
                    narration_beats=["A surviving tablet describes the event. " * 20],
                    source_urls=[],
                    visual_captions=[],
                    hook="A surviving tablet.",
                    content_kind="short",
                )
                self.assertFalse(service.review_topic(topic, history_required=True).approved)
            finally:
                service.close()

    def test_visual_review_rejects_recent_reuse_and_unknown_rights(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = QualityV2Service(Path(temporary))
            try:
                topic = SimpleNamespace(
                    title="Evidence title",
                    subject="Evidence title",
                    narration_beats=["A tablet provides the evidence."],
                )
                report = service.review_visual_sources(
                    topic,
                    [{
                        "url": "https://example.test/reused.jpg",
                        "asset_id": "asset-1",
                        "license": "unknown",
                        "source": "direct_url",
                    }],
                    recent_urls={"https://example.test/reused.jpg"},
                )
                self.assertFalse(report["approved"])
                self.assertTrue(any("repeats" in issue for issue in report["issues"]))
                self.assertTrue(any("rights" in issue for issue in report["issues"]))
            finally:
                service.close()
