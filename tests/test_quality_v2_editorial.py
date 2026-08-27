import unittest

from yt_auto.quality_v2.editorial import Claim, CorpusMemory, EditorialGate, StoryBrief


class EditorialGateTests(unittest.TestCase):
    def test_history_requires_supported_claims(self):
        brief = StoryBrief("Why?", "A hook.", "The answer.", ("c1",))
        report = EditorialGate().review(
            "A history title",
            "A sufficiently long history narration. " * 24,
            claims=[Claim("c1", "A fact", (), "approved")],
            brief=brief,
            history_required=True,
        )
        self.assertFalse(report.approved)
        self.assertIn("unsupported claim: c1", report.issues)

    def test_reused_sentence_blocks_publication(self):
        old = "The bronze tablet survived the fire and changed the investigation."
        corpus = CorpusMemory(narrations=[old])
        report = EditorialGate().review(
            "New subject",
            (old + " A different conclusion follows from the remaining evidence. ") * 8,
            corpus=corpus,
            content_kind="short",
        )
        self.assertFalse(report.approved)
        self.assertIn("exact sentence reused from published history", report.issues)

    def test_clean_short_is_approved_without_history_requirement(self):
        narration = "A sealed room held one small clue. " * 14
        report = EditorialGate().review("A distinct discovery", narration, content_kind="short")
        self.assertTrue(report.approved)
