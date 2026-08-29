import unittest

from yt_auto.quality_v2.visuals import AssetRecord, RightsStatus, SceneVisualPlan, VisualHistory, validate_rights, validate_scene_coverage


class VisualQualityTests(unittest.TestCase):
    def test_rights_policy_rejects_noncommercial_assets(self):
        record = AssetRecord("a", "https://example.test/a", "CC BY-NC 4.0")
        self.assertIs(RightsStatus.REJECTED, validate_rights(record))

    def test_rights_policy_does_not_find_nc_or_nd_inside_source_words(self):
        for slug in ("lunch", "window", "man-and-woman"):
            with self.subTest(slug=slug):
                record = AssetRecord(
                    slug,
                    f"https://www.pexels.com/video/{slug}-1234/",
                    "Pexels License",
                    f"https://www.pexels.com/video/{slug}-1234/",
                )
                self.assertIs(RightsStatus.ALLOWED, validate_rights(record))

    def test_rights_policy_still_rejects_nd_license_components(self):
        record = AssetRecord(
            "nd",
            "https://example.test/asset",
            "CC BY 4.0",
            "https://creativecommons.org/licenses/by-nd/4.0/",
        )
        self.assertIs(RightsStatus.REJECTED, validate_rights(record))

    def test_history_rejects_exact_reuse(self):
        old = AssetRecord("old", "https://example.test/old", "CC0", checksum="abc")
        decision = VisualHistory([old]).evaluate(AssetRecord("new", "https://example.test/new", "CC0", checksum="abc"))
        self.assertFalse(decision.allowed)

    def test_every_scene_needs_primary_and_fallback(self):
        issues = validate_scene_coverage([SceneVisualPlan("b1", "", "", 4)])
        self.assertEqual(2, len(issues))
