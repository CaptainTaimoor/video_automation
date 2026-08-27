import unittest

from yt_auto.quality_v2.visuals import AssetRecord, RightsStatus, SceneVisualPlan, VisualHistory, validate_rights, validate_scene_coverage


class VisualQualityTests(unittest.TestCase):
    def test_rights_policy_rejects_noncommercial_assets(self):
        record = AssetRecord("a", "https://example.test/a", "CC BY-NC 4.0")
        self.assertIs(RightsStatus.REJECTED, validate_rights(record))

    def test_history_rejects_exact_reuse(self):
        old = AssetRecord("old", "https://example.test/old", "CC0", checksum="abc")
        decision = VisualHistory([old]).evaluate(AssetRecord("new", "https://example.test/new", "CC0", checksum="abc"))
        self.assertFalse(decision.allowed)

    def test_every_scene_needs_primary_and_fallback(self):
        issues = validate_scene_coverage([SceneVisualPlan("b1", "", "", 4)])
        self.assertEqual(2, len(issues))
