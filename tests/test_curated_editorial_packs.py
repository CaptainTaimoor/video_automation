from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yt_auto.config import load_config
from yt_auto.images import HybridMediaFetcher
from yt_auto.models import TopicCandidate
from yt_auto.pipeline import ShortsFactory
from yt_auto.research import ContentResearcher
from yt_auto.script_writer import ScriptWriter
from yt_auto.subtitles import SubtitleComposer
from yt_auto.title_lab import TitleLab
from yt_auto.topics import TopicPlanner


def candidate(niche_id: str, subject: str) -> TopicCandidate:
    return TopicCandidate(
        niche_id=niche_id,
        style="explainer",
        trend_terms=[],
        title=subject,
        subject=subject,
        hook="",
        narration="",
        visual_captions=[],
        source_urls=[],
        image_queries=[],
        hashtags=[],
        engagement_score=1.0,
        content_kind="short",
    )


class CuratedShortPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "config" / "settings.yaml")
        cls.channels = {channel.id: channel for channel in cls.config.channels}
        cls.writer = ScriptWriter(cls.config.app.script_writer)

    def test_great_zimbabwe_uses_voice_calibrated_evidence_plan(self) -> None:
        polished = self.writer.improve(
            self.channels["ancient_history"],
            candidate("ancient_history", "Great Zimbabwe"),
            content_kind="short",
        )

        self.assertGreaterEqual(len(polished.scene_plan), 6)
        self.assertLessEqual(len(polished.scene_plan), 12)
        self.assertEqual(len(polished.scene_plan), len(polished.narration_beats))
        self.assertGreaterEqual(self.writer._word_count(polished.narration), 65)
        self.assertLessEqual(self.writer._word_count(polished.narration), 70)
        self.assertLessEqual(
            max(self.writer._word_count(line) for line in polished.narration_beats),
            10,
        )
        narration = polished.narration.lower()
        for fact in (
            "colonial writers denied great zimbabwe's origins",
            "finds confirmed its african origins",
            "shona ancestors",
            "key african capital",
            "1100 through 1450",
            "granite walls rose without any mortar",
            "cattle may have fueled elite wealth",
            "rich gold lands",
            "imported beads and ceramics",
            "broad indian ocean trade",
            "evidence confirms african origins",
        ):
            self.assertIn(fact, narration)
        self.assertEqual(
            [],
            self.writer.editorial_quality_issues(
                polished,
                polished.narration_beats,
                content_kind="short",
            ),
        )

        lab = TitleLab()
        chosen, ranked = lab.choose(
            "ancient_history",
            polished,
            lab.make_variants(polished, count=4),
            {},
            epsilon=0.0,
        )
        expected = "Great Zimbabwe: How Archaeology Overturned a Colonial Myth"
        self.assertEqual(expected, chosen.title)
        self.assertEqual([expected], [variant.title for variant in ranked])

    def test_reply_time_anxiety_uses_voice_calibrated_scenes(self) -> None:
        polished = self.writer.improve(
            self.channels["brain_lens"],
            candidate("brain_lens", "Reply Time Anxiety"),
            content_kind="short",
        )

        self.assertGreaterEqual(len(polished.scene_plan), 5)
        self.assertLessEqual(len(polished.scene_plan), 8)
        self.assertEqual(len(polished.scene_plan), len(polished.narration_beats))
        self.assertGreaterEqual(self.writer._word_count(polished.narration), 68)
        self.assertLessEqual(self.writer._word_count(polished.narration), 74)
        self.assertLessEqual(
            max(self.writer._word_count(line) for line in polished.narration_beats),
            13,
        )
        self.assertIn("appears after a long wait", polished.narration.lower())
        self.assertNotIn("late on-screen", polished.narration.lower())
        narration = polished.narration.lower()
        for bounded_claim in (
            "one study surveyed 302 partnered undergraduates",
            "reported attachment and messaging patterns",
            "wanted more messages",
            "replied faster than partners",
            "associations, not causes",
            "sample limits broad claims",
            "check urgency",
            "usual pace",
            "plans, reasons, and later actions",
            "judge the pattern",
        ):
            self.assertIn(bounded_claim, narration)
        self.assertNotIn("at night", narration)
        self.assertNotIn("nervous system", narration)
        self.assertEqual(
            [],
            self.writer.editorial_quality_issues(
                polished,
                polished.narration_beats,
                content_kind="short",
            ),
        )

        lab = TitleLab()
        chosen, ranked = lab.choose(
            "brain_lens",
            polished,
            lab.make_variants(polished, count=4),
            {},
            epsilon=0.0,
        )
        expected = "Late Replies and Attachment Anxiety: What One Study Found"
        self.assertEqual(expected, chosen.title)
        self.assertEqual([expected], [variant.title for variant in ranked])

    def test_reply_deterministic_factory_fallback_uses_same_bounded_pack(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.image_fetcher = HybridMediaFetcher()
        factory.topic_planner = TopicPlanner(timezone="UTC")
        fallback = factory._brain_short_deterministic_fallback(
            self.channels["brain_lens"],
            {"micro flirting"},
        )

        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual("Reply Time Anxiety", fallback.subject)
        self.assertEqual(
            "Late Replies and Attachment Anxiety: What One Study Found",
            fallback.title,
        )
        self.assertGreaterEqual(len(fallback.scene_plan), 5)
        self.assertLessEqual(len(fallback.scene_plan), 8)
        self.assertEqual(len(fallback.scene_plan), len(fallback.narration_beats))
        self.assertGreaterEqual(self.writer._word_count(fallback.narration), 68)
        self.assertLessEqual(self.writer._word_count(fallback.narration), 74)
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/35085449/", fallback.source_urls)
        self.assertIn(
            "https://www.apa.org/news/press/releases/2018/08/relationship-texting",
            fallback.source_urls,
        )

    def test_friends_with_benefits_long_plan_has_complete_caption_boundaries(self) -> None:
        topic = candidate("brain_lens", "Friends With Benefits Boundaries")
        topic.content_kind = "video"
        scenes = self.writer._brain_long_video_plan(
            self.channels["brain_lens"],
            topic,
            topic.subject,
        )
        narration = " ".join(scene.narration for scene in scenes)
        composer = SubtitleComposer(max_words_per_caption=8)
        captions = composer.caption_segments_from_scene_segments(
            composer.scene_segments([scene.narration for scene in scenes], 570.0)
        )
        structural_issues = [
            issue for issue in composer.quality_issues(captions) if "CPS" not in issue
        ]

        self.assertEqual(20, len(scenes))
        self.assertGreaterEqual(self.writer._word_count(narration), 1080)
        self.assertLessEqual(self.writer._word_count(narration), 1200)
        self.assertLessEqual(
            max(composer.visible_word_count(caption.text) for caption in captions),
            8,
        )
        self.assertEqual([], structural_issues)

    def test_caption_safe_long_sections_fail_before_tts_when_edited_too_long(self) -> None:
        safe = [("Safe scene", "Every sentence remains safely under eight words.")]
        unsafe = [("Unsafe scene", "This deliberately oversized sentence cannot remain whole beneath the strict eight word caption limit.")]

        self.assertEqual([], self.writer._caption_safe_section_issues(safe))
        with self.assertRaisesRegex(ValueError, "caption-safe script check failed"):
            self.writer._require_caption_safe_sections(unsafe, label="Regression fixture")


class CuratedSourcePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.researcher = ContentResearcher()

    def test_source_packs_merge_dedupe_and_keep_normal_validation(self) -> None:
        cases = {
            "Great Zimbabwe": {
                "https://whc.unesco.org/en/list/364/",
                "https://www.metmuseum.org/essays/great-zimbabwe-11th-15th-century",
            },
            "Reply Time Anxiety": {
                "https://pubmed.ncbi.nlm.nih.gov/35085449/",
                "https://www.apa.org/news/press/releases/2018/08/relationship-texting",
            },
            "Friends With Benefits Boundaries": {
                "https://pubmed.ncbi.nlm.nih.gov/34779977/",
                "https://rainn.org/share-the-facts/consent-101-respect-boundaries-and-building-trust/",
                "https://pmc.ncbi.nlm.nih.gov/articles/PMC12124867/",
            },
        }
        for subject, expected in cases.items():
            with self.subTest(subject=subject):
                first = next(iter(expected))
                sources = self.researcher.enrich_source_urls(
                    subject,
                    [first, first, "not-a-url"],
                    limit=6,
                )
                self.assertTrue(expected.issubset(set(sources)))
                self.assertEqual(len(sources), len(set(url.lower() for url in sources)))
                self.assertNotIn("not-a-url", sources)


if __name__ == "__main__":
    unittest.main()
