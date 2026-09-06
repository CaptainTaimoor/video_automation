import unittest
from yt_auto.video_builder import VideoBuilder

class ClaimCardTests(unittest.TestCase):
    def setUp(self):
        self.vb = VideoBuilder(50, 59)

    def test_brain_claim_from_title(self):
        text = self.vb._short_claim_card_text("Why you freeze before small decisions", "brain_lens")
        self.assertTrue(len(text.split()) >= 2)
        self.assertEqual(text, text.upper())

    def test_ancient_has_no_claim(self):
        text = self.vb._short_claim_card_text("Why Carthage fell", "ancient_history")
        self.assertEqual(text, "")

    def test_ass_includes_claim_style(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c.ass"
            self.vb._write_ass_captions([], out, claim_card_text="MIXED SIGNALS LOOP")
            body = out.read_text(encoding="utf-8-sig")
            self.assertIn("Style: ClaimCard", body)
            self.assertIn("MIXED SIGNALS LOOP", body)
            self.assertEqual(self.vb.last_claim_card_text, "MIXED SIGNALS LOOP")

if __name__ == "__main__":
    unittest.main()