from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yt_auto.config import load_config
from yt_auto.images import HybridMediaFetcher
from yt_auto.models import ScenePlanItem, ScriptWriterConfig, TopicCandidate
from yt_auto.pipeline import ShortsFactory
from yt_auto.research import ContentResearcher
from yt_auto.script_writer import ScriptWriter
from yt_auto.seo import _source_url_is_live, build_youtube_metadata
from yt_auto.subtitles import SubtitleComposer, SubtitleSegment
from yt_auto.thumbnailer import ThumbnailMaker
from yt_auto.title_lab import TitleLab
from yt_auto.topics import TopicPlanner


def topic(**overrides) -> TopicCandidate:
    data = {
        "niche_id": "brain_lens",
        "style": "explainer",
        "trend_terms": [],
        "title": "What a Voice Change Can Signal About Attraction",
        "hook": "Their voice shifts when you walk over, and you wonder whether that tiny change means attraction.",
        "narration": "",
        "visual_captions": [],
        "source_urls": ["https://example.org/research"],
        "image_queries": [],
        "hashtags": [],
        "engagement_score": 1.0,
        "subject": "Voice Change Attraction",
    }
    data.update(overrides)
    return TopicCandidate(**data)


class SubtitleQualityTests(unittest.TestCase):
    def test_hyphenated_compound_counts_as_one_caption_word(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4, max_chars_per_second=100.0)
        self.assertEqual(1, composer.visible_word_count("stone-built"))
        self.assertEqual(2, composer.visible_word_count("Great Zimbabwe's"))
        scenes = [
            SubtitleSegment(
                0.0,
                12.0,
                (
                    "Great Zimbabwe was a stone-built city. "
                    "Trade and cattle wealth sustained its power."
                ),
            ),
            SubtitleSegment(
                12.0,
                24.0,
                (
                    "Builders raised Great Zimbabwe's dry-stone walls. "
                    "They used no mortar."
                ),
            ),
        ]

        captions = composer.caption_segments_from_scene_segments(scenes)

        self.assertEqual([], composer.quality_issues(captions))
        self.assertTrue(any("stone-built city" in caption.text for caption in captions))
        self.assertFalse(any(caption.text.lower().startswith("of trade") for caption in captions))
        self.assertFalse(any(caption.text.lower().startswith("without mortar") for caption in captions))

    def test_great_zimbabwe_short_has_no_flash_or_midphrase_captions(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4)
        beats = [
            "Great Zimbabwe rose in southern Africa. Its mortarless walls still stand. Imported beads and gold reveal trade.",
            "The stone-built city became a center. Trade and cattle wealth sustained power. Political authority shaped life there.",
            "Builders raised stone walls without mortar. They enclosed homes and elite spaces. Rituals unfolded there too.",
            "Imported beads and ceramics crossed oceans. Indian Ocean trade reached the city. Cattle and gold supported local power.",
            "Walls and trade goods reveal wealth. Soapstone birds reveal local belief. Great Zimbabwe joined global exchange.",
        ]
        durations = [6.88619, 7.15329, 7.11156, 7.19502, 7.55394]
        scenes = composer.scene_segments_from_durations(
            beats,
            durations,
            total_duration=35.9,
        )

        captions = composer.caption_segments_from_scene_segments(scenes)

        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))
        word_counts = [composer.visible_word_count(caption.text) for caption in captions]
        self.assertGreaterEqual(min(word_counts), 3)
        self.assertLessEqual(max(word_counts), 6)

    def test_abbreviation_protection_does_not_match_word_suffixes(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=7)

        self.assertEqual(
            [
                "The engineering contest.",
                "As the attackers moved.",
                "The conquest.",
                "The city survived.",
            ],
            composer._sentence_beats(
                "The engineering contest. As the attackers moved. "
                "The conquest. The city survived."
            ),
        )
        self.assertEqual(
            ["Meet at St. Louis.", "Then continue."],
            composer._sentence_beats("Meet at St. Louis. Then continue."),
        )

    def test_lachish_caption_boundaries_do_not_start_midphrase(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=8, max_chars_per_second=100.0)
        narrations = [
            "Assyrian engineers answered that problem with a massive ramp of stone and earth.",
            "That response changes the scene from a simple story of overwhelming force into an engineering contest.",
            (
                "A recent archaeomagnetic study examined a mudbrick tower. "
                "The tower stood in the outer defenses. Its burning dates to the Iron Age. "
                "The 701 BCE siege is the likeliest event."
            ),
            "One detail appears to show water protecting an engine from burning material thrown from above.",
            (
                "For Assyria, the victory became both strategic control and palace memory. "
                "For Judah, it became part of a longer argument about invasion, judgment, endurance, and Jerusalem."
            ),
        ]
        scenes = [
            SubtitleSegment(index * 30.0, (index + 1) * 30.0, narration)
            for index, narration in enumerate(narrations)
        ]

        captions = composer.caption_segments_from_scene_segments(scenes)

        self.assertEqual([], composer.quality_issues(captions))
        self.assertLessEqual(
            max(len(re.findall(r"[A-Za-z0-9']+", caption.text)) for caption in captions),
            8,
        )
        forbidden_starts = (
            "with a massive ramp",
            "from a simple story",
            "to the iron age",
            "to show water",
            "of a longer argument",
        )
        self.assertFalse(
            any(
                caption.text.lower().startswith(forbidden_starts)
                for caption in captions
            )
        )

    def test_configured_six_word_ceiling_survives_protected_history_name(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=6)
        text = (
            "Great Zimbabwe rose in southern Africa. Its mortarless walls still stand. "
            "Imported beads and gold reveal trade."
        )

        segments = composer.segments(text, 7.0)

        self.assertTrue(segments)
        self.assertLessEqual(
            max(len(re.findall(r"[A-Za-z0-9']+", segment.text)) for segment in segments),
            6,
        )
        self.assertEqual([], composer.quality_issues(segments))

    def test_measurements_and_entities_are_not_torn_apart(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4)
        scene = SubtitleSegment(
            0.0,
            8.0,
            "The Obelisk of Axum is a 4th-century CE, 24-metre (79 ft) tall phonolite stele, weighing 160 tonnes.",
        )
        segments = composer.caption_segments_from_scene_segments([scene])
        self.assertFalse(any(segment.text.count("(") != segment.text.count(")") for segment in segments))
        joined = " | ".join(segment.text for segment in segments)
        self.assertIn("The Obelisk of Axum", joined)
        self.assertIn("24-metre (79 ft) tall", joined)
        self.assertNotIn("24-metre (79 |", joined)

    def test_character_timing_respects_cps_when_scene_has_enough_time(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4, max_chars_per_second=17.0)
        scene = SubtitleSegment(
            0.0,
            7.0,
            "Their voice shifts. The whole conversation suddenly feels different.",
        )
        segments = composer.caption_segments_from_scene_segments([scene])
        self.assertEqual([], composer.quality_issues(segments))
        self.assertTrue(any("the whole conversation" in segment.text.lower() for segment in segments))

    def test_cps_gate_ignores_floating_point_noise_at_exact_limit(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4, max_chars_per_second=17.0)
        segments = [SubtitleSegment(0.0, 1.0 - 1e-12, "five seven eleven")]

        self.assertEqual([], composer.quality_issues(segments, cps_tolerance=0.0))

    def test_default_cps_tolerance_allows_only_rounding_margin(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4, max_chars_per_second=17.0)
        within_margin = [SubtitleSegment(0.0, 17.0 / 17.04, "five seven eleven")]
        over_margin = [SubtitleSegment(0.0, 17.0 / 17.06, "five seven eleven")]

        self.assertEqual([], composer.quality_issues(within_margin))
        self.assertTrue(any("CPS" in issue for issue in composer.quality_issues(over_margin)))

    def test_caption_chunks_do_not_strand_history_connectors(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4)
        text = (
            "Kushite rulers once controlled Egypt as the Twenty-Fifth Dynasty, leaving pyramids, temples and royal inscriptions. "
            "Surviving structures linked to the Nubian Pyramids show how power, belief, and labor shaped the site."
        )
        segments = composer.segments(text, 16.0)
        incomplete = [issue for issue in composer.quality_issues(segments) if "incomplete phrase" in issue]
        self.assertEqual([], incomplete)
        bad_tails = {"as", "the", "to", "linked", "show", "shows"}
        self.assertFalse(
            any(segment.text.rstrip(" ,;:!?.").lower().split()[-1] in bad_tails for segment in segments[:-1])
        )

    def test_caption_chunks_do_not_end_on_about(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=7)
        scene = SubtitleSegment(
            start=0.0,
            end=21.275,
            text=(
                "Start with a strict limit: examine the specific behavior around crush idealization; "
                "do not diagnose either person from a label. What happened more than once? "
                "What expectation had actually been discussed? What did each person do after tension appeared? "
                "One painful moment cannot rate your worth or reveal their hidden motives."
            ),
        )

        segments = composer.caption_segments_from_scene_segments([scene])

        self.assertEqual([], composer.quality_issues(segments))
        self.assertFalse(
            any(segment.text.rstrip(" ,;:!?.").lower().endswith(" about") for segment in segments)
        )

    def test_long_caption_chunks_keep_complements_with_their_heads(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=8)
        scene = SubtitleSegment(
            start=0.0,
            end=41.0,
            text=(
                "The body can register intensity before the agreement is clear. "
                "What did each person do after tension appeared? "
                "Keep one painful moment from becoming a verdict about your worth. "
                "Ask what new behavior would genuinely change your conclusion. "
                "If no outcome could change it, you are defending a story. "
                "One false move means you treat potential as commitment. "
                "Keep your normal week intact and compare every promise with one action. "
                "Direct communication cannot guarantee mutual interest. "
                "It can replace hours of decoding. Both people gain shared information."
            ),
        )

        segments = composer.caption_segments_from_scene_segments([scene])

        self.assertEqual([], composer.quality_issues(segments))
        forbidden_tails = {
            "before", "each", "becoming", "your", "new", "defending",
            "treat", "keep", "compare", "it",
        }
        self.assertFalse(
            any(
                segment.text.rstrip(" ,;:!?.").lower().split()[-1] in forbidden_tails
                for segment in segments[:-1]
            )
        )

    def test_sentence_final_continuation_still_counts_as_midphrase(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=8, max_chars_per_second=100.0)
        segments = [
            SubtitleSegment(0.0, 2.0, "The National Museum of Korea and Institute"),
            SubtitleSegment(2.0, 4.0, "of Cultural Heritage preserved it."),
        ]
        issues = composer.quality_issues(segments)

        self.assertTrue(
            any(segment.text.startswith("of Cultural Heritage") for segment in segments)
        )
        self.assertTrue(any("begins in the middle of a phrase" in issue for issue in issues))

    def test_protected_name_cannot_bypass_hard_caption_word_ceiling(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4, max_chars_per_second=100.0)
        scene = SubtitleSegment(
            0.0,
            30.0,
            "The Ministry of Culture and Department of Historical Records preserved it.",
        )

        segments = composer.caption_segments_from_scene_segments([scene])
        issues = composer.quality_issues(segments)

        self.assertGreater(
            max(composer.visible_word_count(segment.text) for segment in segments),
            composer.hard_word_limit(),
        )
        self.assertTrue(any("contains 8 words (limit 6)" in issue for issue in issues))

    def test_reply_time_short_uses_complete_caption_phrases(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4)
        beats = [
            "Their reply slows down. Suddenly, the timestamp feels brutal. The whole connection seems at risk.",
            "Warm texts feel intimate at night. Attention has fewer places to escape. Silence leaves room for fear.",
            "A slow reply can feel threatening. Nothing may have changed yet. Context matters more than one delay.",
            "Your brain measures interest through timestamps. It watches punctuation and typing bubbles.",
            "Use the full pattern instead. Track plans, effort, and clear words.",
            "One reply gives one data point. Repeated care reveals the pattern.",
        ]
        durations = [5.95, 6.725, 6.1, 5.9, 5.3, 4.525]
        scenes = composer.scene_segments_from_durations(
            beats,
            durations,
            total_duration=sum(durations),
        )

        captions = composer.caption_segments_from_scene_segments(scenes)

        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))
        self.assertTrue(any(caption.text == "Their reply slows down." for caption in captions))
        self.assertTrue(any("one data point" in caption.text.lower() for caption in captions))
        self.assertTrue(any("repeated care" in caption.text.lower() for caption in captions))
        self.assertFalse(
            any(
                caption.text.lower() in {"down", "message", "point", "pattern"}
                for caption in captions
            )
        )

    def test_brain_question_list_uses_complete_caption_phrases(self) -> None:
        composer = SubtitleComposer(max_words_per_caption=4)
        beats = [
            "Ask three questions. Did it repeat? Did their actions match? Did you feel clearer afterward?",
            "One intense moment creates chemistry. Reliable patterns need time and consistency.",
            "Name what happened. Decide what it may mean. Keep the relationship context in view.",
        ]
        scenes = [
            SubtitleSegment(index * 8.0, (index + 1) * 8.0, beat)
            for index, beat in enumerate(beats)
        ]

        captions = composer.caption_segments_from_scene_segments(scenes)

        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.0))

    def test_pipeline_rejects_brain_short_with_unrenderable_caption_phrases(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "brain_lens")
        factory = ShortsFactory.__new__(ShortsFactory)
        beats = [
            "You stop measuring promises and start noticing whether the pace, effort, and care are actually mutual.",
            "Ask three things: did it repeat, did their actions match, and did you feel clearer afterward?",
            "One intense moment can create chemistry, but a reliable pattern needs time and consistency.",
            "Name what actually happened before guessing what it means about the relationship.",
            "Judge the ordinary pattern, not the most exciting moment.",
        ]
        candidate = topic(
            title="Emotional Safety in Dating: The Pattern to Watch",
            subject="Emotional Safety in Dating",
            narration=" ".join(beats),
            narration_beats=beats,
            content_kind="short",
        )

        issues = factory._script_quality_issues(channel, candidate)

        self.assertTrue(
            any("cannot form clean caption phrases" in issue for issue in issues),
            issues,
        )


class MetadataQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        cls.channels = {channel.id: channel for channel in config.channels}

    def test_description_uses_complete_narration_not_truncated_visual_labels(self) -> None:
        axum = topic(
            niche_id="ancient_history",
            title="What the Surviving Record Reveals About Axum Obelisks",
            subject="Axum Obelisks",
            hook="The obelisks of Axum marked elite tombs and displayed royal engineering power.",
            narration="The false doors and window-like tiers made each stele resemble a multistorey building.",
            narration_beats=[
                "The obelisks of Axum marked elite tombs and displayed royal engineering power.",
                "The false doors and window-like tiers made each stele resemble a multistorey building.",
                "At Axum, monumental stone turned elite burial into a public claim of royal power.",
            ],
            visual_captions=["The Obelisk of Axum is a 4th-century CE", "It is ornamented with false doors resembling"],
            source_urls=["https://whc.unesco.org/en/list/15/"],
        )
        with patch("yt_auto.seo._source_url_is_live", return_value=True):
            metadata = build_youtube_metadata(self.channels["ancient_history"], axum, [], "short")
        bullets = [line[2:] for line in metadata["description"].splitlines() if line.startswith("- ")]
        self.assertTrue(bullets)
        self.assertTrue(all(line.endswith((".", "!", "?")) for line in bullets))
        self.assertFalse(any(line.endswith((" CE", " resembling")) for line in bullets))
        self.assertNotIn("roman empire", [tag.lower() for tag in metadata["tags"]])

    def test_ancient_long_subscribe_line_has_no_leftover_short_preposition(self) -> None:
        candidate = topic(
            niche_id="ancient_history",
            title="How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence",
            subject="Assyrian Siege of Lachish",
            content_kind="video",
            source_urls=[f"https://example.org/source-{index}" for index in range(1, 7)],
        )

        with patch("yt_auto.seo._source_url_is_live", return_value=True):
            metadata = build_youtube_metadata(
                self.channels["ancient_history"], candidate, [], "video"
            )

        self.assertIn(
            "Subscribe for evidence-led ancient history with deeper context.",
            metadata["description"],
        )
        self.assertNotIn("in with deeper context", metadata["description"])
        self.assertEqual(6, len(metadata["source_urls"]))

    def test_only_curated_doi_can_survive_source_checker_403(self) -> None:
        curated = "https://onlinelibrary.wiley.com/doi/10.1111/ojoa.12231"
        arbitrary = "https://example.org/blocked-but-unverified"
        blocked = Mock(status_code=403)
        blocked.close = Mock()
        _source_url_is_live.cache_clear()

        with patch("yt_auto.seo.requests.get", return_value=blocked) as get:
            self.assertTrue(_source_url_is_live(curated))
            get.assert_not_called()
            self.assertFalse(_source_url_is_live(arbitrary))

        get.assert_called_once()

    def test_keywords_are_natural_and_bad_sources_do_not_survive(self) -> None:
        voice = topic(source_urls=["not-a-url"])
        with patch("yt_auto.seo._source_url_is_live", return_value=False):
            metadata = build_youtube_metadata(self.channels["brain_lens"], voice, [], "short")
        self.assertIn("voice changes and attraction", [item.lower() for item in metadata["seo_keywords"]])
        self.assertNotIn("voice change attraction", [item.lower() for item in metadata["seo_keywords"]])
        self.assertEqual([], metadata["source_urls"])
        self.assertIn("missing a validated source URL", metadata["seo_issues"])

    def test_short_description_does_not_repeat_the_full_script(self) -> None:
        hook = "They go warm, disappear, then return before your hope switches off."
        voice = topic(
            title="Why Mixed Signals Feel So Intense After Hot-and-Cold Attention",
            subject="Mixed Attachment Signals",
            hook=hook,
            narration=f"{hook} Ask once for clarity. Repeated care is the evidence.",
            narration_beats=[hook, "Ask once for clarity.", "Repeated care is the evidence."],
        )
        with patch("yt_auto.seo._source_url_is_live", return_value=True):
            metadata = build_youtube_metadata(self.channels["brain_lens"], voice, [], "short")
        self.assertEqual(metadata["description"].count(hook), 1)
        self.assertIn("Takeaway:\nRepeated care is the evidence.", metadata["description"])


class SourceValidationTests(unittest.TestCase):
    def test_brain_visual_action_does_not_treat_headphones_as_phone_footage(self) -> None:
        self.assertEqual(
            "",
            ShortsFactory._brain_visual_action("a man wearing headphones and holding a camera"),
        )
        self.assertEqual(
            "phone",
            ShortsFactory._brain_visual_action("a couple texting on smartphones"),
        )

    def test_topic_rotation_counts_only_published_runs(self) -> None:
        researcher = ContentResearcher()
        self.assertTrue(researcher._run_counts_as_published({"uploaded": True}))
        self.assertTrue(researcher._run_counts_as_published({"youtube_id": "abc123"}))
        self.assertTrue(researcher._run_counts_as_published({"youtube_video_id": "legacy123"}))
        self.assertTrue(researcher._run_counts_as_published({"facebook_id": "fb123"}))
        self.assertTrue(
            researcher._run_counts_as_published(
                {"uploaded": False, "upload_skipped": "build_only", "quality_decision": "pass"}
            )
        )
        self.assertFalse(
            researcher._run_counts_as_published(
                {"uploaded": False, "upload_skipped": "build_only", "youtube_id": None}
            )
        )
        self.assertFalse(
            researcher._run_counts_as_published(
                {"uploaded": False, "upload_skipped": "quality_gate", "quality_decision": "hold"}
            )
        )

    def test_recent_title_cache_ignores_dry_runs_and_quality_holds(self) -> None:
        factory = object.__new__(ShortsFactory)
        factory._read_run_log = Mock(
            return_value=[
                {
                    "channel": "ancient_history",
                    "title": "Dry Run Tikal",
                    "subject": "Tikal",
                    "uploaded": False,
                    "upload_skipped": "build_only",
                },
                {
                    "channel": "ancient_history",
                    "title": "Held Axum",
                    "subject": "Axum Obelisks",
                    "uploaded": False,
                    "upload_skipped": "quality_gate",
                    "quality_decision": "hold",
                },
                {
                    "channel": "ancient_history",
                    "title": "Ready Mohenjo",
                    "subject": "Mohenjo-daro",
                    "uploaded": False,
                    "upload_skipped": "build_only",
                    "quality_decision": "pass",
                },
                {
                    "channel": "ancient_history",
                    "title": "Published Zimbabwe",
                    "subject": "Great Zimbabwe",
                    "youtube_id": "yt-123",
                },
            ]
        )
        self.assertEqual(
            {
                "ready mohenjo",
                "mohenjo-daro",
                "published zimbabwe",
                "great zimbabwe",
            },
            factory._recent_titles("ancient_history"),
        )

    def test_recent_story_fingerprints_ignore_dry_runs_and_quality_holds(self) -> None:
        factory = object.__new__(ShortsFactory)
        factory._read_run_log = Mock(
            return_value=[
                {
                    "channel": "ancient_history",
                    "title": "Great Zimbabwe: Mortarless Walls, Gold, and Indian Ocean Trade",
                    "subject": "Great Zimbabwe",
                    "uploaded": False,
                    "upload_skipped": "quality_gate",
                },
                {
                    "channel": "ancient_history",
                    "title": "Inside Tikal: Maya Kings and Jungle Temples",
                    "subject": "Tikal",
                    "youtube_video_id": "legacy-yt-id",
                },
            ]
        )

        fingerprints = factory._recent_story_fingerprints("ancient_history")

        self.assertNotIn("great zimbabwe mortarless walls gold", fingerprints)
        self.assertTrue(any("tikal" in fingerprint for fingerprint in fingerprints))

    def test_missing_wikipedia_page_does_not_become_an_invented_slug(self) -> None:
        researcher = ContentResearcher()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"query": {"pages": {"-1": {"title": "Invented Psychology Page", "missing": ""}}}}
        researcher.session.get = Mock(return_value=response)
        self.assertEqual("", researcher._default_source_url("Invented Psychology Page"))

    def test_live_source_filter_rejects_404(self) -> None:
        researcher = ContentResearcher()
        missing = Mock(status_code=404)
        missing.close.return_value = None
        live = Mock(status_code=200)
        live.close.return_value = None
        researcher.session.get = Mock(side_effect=[missing, live])
        result = researcher._validated_sources(
            ["https://example.org/missing", "https://example.org/live"],
            limit=2,
        )
        self.assertEqual(["https://example.org/live"], result)

    def test_relationship_subject_uses_a_resolved_context_source(self) -> None:
        researcher = ContentResearcher()
        researcher.session.get = Mock(side_effect=AssertionError("verified context source should not need network lookup"))
        self.assertEqual(
            "https://en.wikipedia.org/wiki/Attachment_theory",
            researcher._default_source_url("Mixed Attachment Signals"),
        )

    def test_curated_history_aliases_use_stable_registry_without_network(self) -> None:
        researcher = ContentResearcher()
        researcher.session.get = Mock(side_effect=AssertionError("registry lookup should not need network"))
        expected = {
            "Kingdom of Kush": "https://en.wikipedia.org/wiki/Kingdom_of_Kush",
            "Sogdian Merchants": "https://en.wikipedia.org/wiki/Sogdia",
            "Terrace Farming at Machu Picchu": "https://en.wikipedia.org/wiki/Machu_Picchu",
            "Nubian Pyramids": "https://en.wikipedia.org/wiki/Nubian_pyramids",
            "Lascaux": "https://en.wikipedia.org/wiki/Lascaux",
            "Boudica Revolt": "https://en.wikipedia.org/wiki/Boudican_revolt",
            "Justinianic Plague": "https://en.wikipedia.org/wiki/Plague_of_Justinian",
            "Bronze Age Collapse": "https://en.wikipedia.org/wiki/Late_Bronze_Age_collapse",
        }
        for subject, url in expected.items():
            with self.subTest(subject=subject):
                self.assertEqual(url, researcher._default_source_url(subject))

    def test_expanded_visual_rich_history_catalog_is_source_safe_and_short_ready(self) -> None:
        researcher = ContentResearcher()
        researcher.session.get = Mock(side_effect=AssertionError("registry-backed subjects should not need network"))
        expected = {
            "angkor wat": "https://en.wikipedia.org/wiki/Angkor_Wat",
            "tikal": "https://en.wikipedia.org/wiki/Tikal",
            "chichen itza": "https://en.wikipedia.org/wiki/Chichen_Itza",
            "great zimbabwe": "https://en.wikipedia.org/wiki/Great_Zimbabwe",
            "mohenjo-daro": "https://en.wikipedia.org/wiki/Mohenjo-daro",
            "axum obelisks": "https://en.wikipedia.org/wiki/Obelisk_of_Axum",
            "lascaux cave paintings": "https://en.wikipedia.org/wiki/Lascaux",
            "cyrus cylinder": "https://en.wikipedia.org/wiki/Cyrus_Cylinder",
            "olmec colossal heads": "https://en.wikipedia.org/wiki/Olmec_colossal_heads",
            "roman concrete": "https://en.wikipedia.org/wiki/Roman_concrete",
        }

        for subject, url in expected.items():
            with self.subTest(subject=subject):
                facts = researcher._curated_history_short_facts(subject)
                self.assertTrue(researcher._history_short_fact_budget_ok(facts))
                self.assertEqual(url, researcher._default_source_url(subject))
                self.assertGreaterEqual(len(researcher._history_visual_queries(subject, subject)), 4)
                if subject in researcher._HISTORY_SHORT_VISUAL_READY_SUBJECTS:
                    self.assertTrue(
                        all(len(re.findall(r"[A-Za-z0-9']+", fact)) <= 22 for fact in facts),
                        subject,
                    )

    def test_old_history_title_formulas_reduce_to_subject_for_memory(self) -> None:
        researcher = ContentResearcher()
        self.assertEqual(
            "mausoleum of qin shi huang",
            researcher._memory_subject("The artifact trail behind Mausoleum of Qin Shi Huang"),
        )
        self.assertEqual(
            "pompeii",
            researcher._memory_subject("3 pieces of evidence that reframe Pompeii"),
        )

    def test_ancient_short_fallback_survives_full_extract_outage(self) -> None:
        researcher = ContentResearcher()
        researcher.history_subjects = ["thin subject", "angkor wat"]
        researcher._load_used_topics = Mock(return_value=set())
        researcher.wikipedia_summary = Mock(return_value=None)
        researcher._default_source_url = Mock(return_value="https://example.org/verified-history")
        researcher._validated_sources = Mock(return_value=["https://example.org/verified-history"])

        with patch("yt_auto.research.random.shuffle", side_effect=lambda values: None):
            pack = researcher.history_pack(["ancient history"], content_kind="short")

        self.assertEqual("angkor wat", pack["subject"])
        self.assertTrue(researcher._history_short_fact_budget_ok(pack["bullets"]))
        joined = " ".join(pack["bullets"]).lower()
        self.assertFalse(any(marker in joined for marker in researcher._GENERIC_HISTORY_SUPPORT_MARKERS))

        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        planner = TopicPlanner(config.app.timezone)
        scene_plan = planner._build_scene_plan(
            channel,
            pack["headline"],
            pack["bullets"],
            [],
            [],
            content_kind="short",
        )
        candidate = TopicCandidate(
            niche_id="ancient_history",
            style="story",
            trend_terms=[],
            title=pack["headline"],
            hook=scene_plan[0].narration,
            narration=" ".join(scene.narration for scene in scene_plan),
            visual_captions=[scene.visual_text for scene in scene_plan[1:-1]],
            source_urls=pack["sources"],
            image_queries=[],
            hashtags=[],
            engagement_score=1.0,
            content_kind="short",
            subject=pack["subject"],
            narration_beats=[scene.narration for scene in scene_plan],
            scene_plan=scene_plan,
        )
        polished = ScriptWriter(config.app.script_writer).improve(channel, candidate, content_kind="short")
        word_count = len(re.findall(r"[A-Za-z0-9']+", polished.narration))
        self.assertGreaterEqual(word_count, 120)
        self.assertLessEqual(word_count, 148)
        researcher = ContentResearcher()
        researcher.history_subjects = [
            "angkor wat",
            "tikal",
            "thin subject",
        ]
        # Simulate an exhausted unused catalog: only the thin subject remains in
        # the first pool, so the fallback must recycle a safe older subject.
        researcher._load_used_topics = Mock(return_value={"tikal"})
        researcher.wikipedia_summary = Mock(return_value=None)
        researcher._default_source_url = Mock(return_value="https://example.org/verified-history")
        researcher._validated_sources = Mock(return_value=["https://example.org/verified-history"])

        with patch("yt_auto.research.random.shuffle", side_effect=lambda values: None):
            pack = researcher.history_pack(
                ["ancient history"],
                avoid_subjects={"angkor wat"},
                content_kind="short",
            )

        self.assertEqual("tikal", pack["subject"])
        self.assertNotEqual("angkor wat", pack["subject"])
        self.assertTrue(researcher._history_short_fact_budget_ok(pack["bullets"]))

    def test_ancient_continuity_fallbacks_are_polished_before_selection(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.topic_planner = TopicPlanner(config.app.timezone)
        factory.script_writer = ScriptWriter(config.app.script_writer)
        factory.title_lab = TitleLab()
        factory.config = config
        factory._recent_story_fingerprints = Mock(return_value={})

        candidates = list(factory._ancient_short_continuity_fallbacks(channel))
        prepared = []
        rejected = []
        for candidate in candidates:
            try:
                prepared.append(
                    factory._prepare_ancient_continuity_fallback(
                        channel,
                        candidate,
                        set(),
                    )
                )
            except ValueError as exc:
                rejected.append((getattr(candidate, "subject", candidate), str(exc)))

        # The point of this test is that every continuity fallback is already
        # publishable, not that the pool is any particular size -- subjects get
        # added over time. Pinning an exact count made this fail purely because
        # the pool grew, so assert the invariant and keep a regression floor.
        self.assertEqual([], rejected)
        self.assertEqual(len(candidates), len(prepared))
        self.assertGreaterEqual(len(prepared), 14)
        continuity_subjects = {
            "nubian pyramids",
            "roman concrete",
            "lascaux",
            "sogdian merchants",
            "boudica revolt",
            "mohenjo-daro",
            "justinianic plague",
            "bronze age collapse",
        }
        for candidate in prepared:
            words = len(re.findall(r"[A-Za-z0-9']+", candidate.narration))
            self.assertGreaterEqual(words, 120)
            self.assertLessEqual(words, 148)
            if len(candidate.narration_beats) >= 20 and candidate.subject not in continuity_subjects:
                self.assertLessEqual(
                    max(len(beat) for beat in candidate.narration_beats),
                    70,
                )
            if candidate.subject in continuity_subjects:
                self.assertLessEqual(
                    max(len(beat) for beat in candidate.narration_beats),
                    90,
                )

    def test_ancient_long_pack_never_counts_generic_context_as_researched_facts(self) -> None:
        researcher = ContentResearcher()
        researcher.history_subjects = ["test monument"]
        researcher._load_used_topics = Mock(return_value=set())
        researcher.wikipedia_summary = Mock(return_value={
            "title": "Test Monument",
            "url": "https://example.org/test-monument",
            "summary": "",
            "bullets": [],
            "image_urls": [],
        })
        researcher._override_fact_for_title = Mock(
            return_value="The Test Monument was built in 300 BCE beside a documented trade road."
        )
        researcher._history_supporting_bullets = Mock(return_value=[
            "The real stakes of Test Monument were practical: safety, work, wealth, status, and survival.",
            "Archaeologists read Test Monument through traces people left behind.",
        ])
        researcher._history_detail_sentences = Mock(return_value=[
            f"Excavated layer {index} preserves a distinct dated feature and material assemblage."
            for index in range(1, 16)
        ])
        researcher._validated_sources = Mock(return_value=["https://example.org/test-monument"])

        with patch("yt_auto.research.random.shuffle", side_effect=lambda values: None):
            pack = researcher.history_pack(["ancient history"], content_kind="video")

        joined = " ".join(pack["bullets"]).lower()
        self.assertEqual(16, len(pack["bullets"]))
        self.assertFalse(any(marker in joined for marker in researcher._GENERIC_HISTORY_SUPPORT_MARKERS))

    def test_history_research_rejects_trimmed_transitive_verb_fragments(self) -> None:
        researcher = ContentResearcher()
        fragment = (
            "The cremation burial from the later phase is one of many examples "
            "and demonstrates."
        )
        self.assertTrue(researcher._looks_incomplete(fragment))
        self.assertTrue(researcher._looks_incomplete("."))

    def test_history_research_repairs_mojibake_and_rejects_orphaned_when_clause(self) -> None:
        researcher = ContentResearcher()

        self.assertEqual(
            researcher._clean_text("Alexander\u00e2\u20ac\u2122s ships reached Tyre."),
            "Alexander's ships reached Tyre.",
        )
        self.assertTrue(
            researcher._looks_incomplete(
                "When his soldiers discovered that the seabed dropped sharply."
            )
        )
        self.assertFalse(
            researcher._looks_incomplete(
                "When the fleet arrived, Alexander could attack from the sea."
            )
        )

    def test_lachish_research_has_distinct_facts_and_authoritative_sources(self) -> None:
        researcher = ContentResearcher()
        facts = researcher._history_supporting_bullets("Siege of Lachish")
        sources = researcher.history_source_overrides["assyrian siege of lachish"]

        self.assertGreaterEqual(len(facts), 16)
        self.assertTrue(any("counter-ramp" in fact for fact in facts))
        self.assertTrue(any("859 arrowheads" in fact for fact in facts))
        self.assertTrue(any("Level III" in fact for fact in facts))
        self.assertTrue(any("britishmuseum.org" in url for url in sources))
        self.assertTrue(any("pmc.ncbi.nlm.nih.gov" in url for url in sources))

    def test_history_subject_override_is_bounded_to_local_qa_selection(self) -> None:
        researcher = ContentResearcher()
        researcher._load_used_topics = Mock(return_value=set())
        researcher.wikipedia_summary = Mock(
            return_value={
                "title": "Siege of Lachish",
                "url": "https://en.wikipedia.org/wiki/Siege_of_Lachish",
                "summary": "",
                "bullets": [],
                "image_urls": [],
            }
        )
        researcher._history_detail_sentences = Mock(return_value=[])
        researcher._validated_sources = Mock(
            return_value=["https://www.britishmuseum.org/collection/galleries/assyria-lion-hunts"]
        )

        with patch.dict("os.environ", {"YT_FORCE_HISTORY_SUBJECT": "assyrian siege of lachish"}):
            pack = researcher.history_pack(["ancient history"], content_kind="video")

        self.assertEqual(pack["subject"], "assyrian siege of lachish")
        self.assertGreaterEqual(len(pack["bullets"]), 14)

    def test_history_subject_override_filters_final_visual_candidates(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        gobekli = topic(
            niche_id="ancient_history",
            title="Gobekli Tepe",
            subject="gobekli tepe",
            content_kind="video",
        )
        lachish = topic(
            niche_id="ancient_history",
            title="Assyrian Siege of Lachish",
            subject="assyrian siege of lachish",
            content_kind="video",
        )
        candidates = [
            (99.0, {}, gobekli, []),
            (88.0, {}, lachish, []),
        ]

        with patch.dict("os.environ", {"YT_FORCE_HISTORY_SUBJECT": "assyrian siege of lachish"}):
            selected = factory._visual_asset_candidates(
                "ancient_history",
                "video",
                candidates,
            )

        self.assertEqual([item[2].subject for item in selected], ["assyrian siege of lachish"])

    def test_ancient_short_fact_budget_rejects_underpaced_pack(self) -> None:
        researcher = ContentResearcher()
        thin_facts = [
            "A surviving wall records the ruler's name.",
            "One excavated road connected the citadel and harbor.",
            "Imported pottery confirms contact with a neighboring region.",
            "A destruction layer marks the settlement's final conflict.",
        ]
        self.assertLess(
            sum(len(re.findall(r"[A-Za-z0-9']+", fact)) for fact in thin_facts),
            researcher._HISTORY_SHORT_MIN_FACT_WORDS,
        )
        self.assertFalse(researcher._history_short_fact_budget_ok(thin_facts))

    def test_ancient_short_writer_fails_closed_below_natural_pacing_floor(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        facts = [
            "A stone wall survives at the site.",
            "One inscription preserves a ruler's name.",
            "Imported pottery confirms regional contact.",
            "A burned layer records the final conflict.",
        ]
        scene_plan = [
            ScenePlanItem(
                narration=fact,
                visual_text=fact,
                search_terms=["Test Site"],
                preferred_image_url="",
                visual_prompt="Test Site archaeology",
            )
            for fact in facts
        ]
        candidate = topic(
            niche_id="ancient_history",
            title="Test Site",
            subject="Test Site",
            hook=facts[0],
            narration=" ".join(facts),
            narration_beats=facts,
            visual_captions=facts[1:-1],
            scene_plan=scene_plan,
        )
        writer = ScriptWriter(config.app.script_writer)
        with patch.object(writer, "_closer", return_value="Together, the surviving clues define what the evidence can support."):
            with self.assertRaisesRegex(ValueError, "evidence-backed words"):
                writer.improve(channel, candidate, content_kind="short")

    def test_sogdian_short_uses_distinct_facts_and_meets_pacing_floor(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        researcher = ContentResearcher()
        facts = researcher._curated_history_short_facts("sogdian merchants")
        self.assertTrue(researcher._history_short_fact_budget_ok(facts))

        planner = TopicPlanner(config.app.timezone)
        with patch("yt_auto.topics.random.choice", side_effect=lambda values: values[0]):
            scene_plan = planner._build_scene_plan(
                channel,
                "Sogdian Merchants",
                facts,
                [],
                [],
                content_kind="short",
            )
        candidate = TopicCandidate(
            niche_id="ancient_history",
            style="story",
            trend_terms=[],
            title="Sogdian Merchants",
            hook=scene_plan[0].narration,
            narration=" ".join(scene.narration for scene in scene_plan),
            visual_captions=[scene.visual_text for scene in scene_plan[1:-1]],
            source_urls=["https://en.wikipedia.org/wiki/Sogdia"],
            image_queries=[],
            hashtags=[],
            engagement_score=1.0,
            content_kind="short",
            subject="sogdian merchants",
            narration_beats=[scene.narration for scene in scene_plan],
            scene_plan=scene_plan,
        )
        with patch("yt_auto.script_writer.random.shuffle", side_effect=lambda values: None):
            polished = ScriptWriter(config.app.script_writer).improve(
                channel,
                candidate,
                content_kind="short",
            )

        word_count = len(re.findall(r"[A-Za-z0-9']+", polished.narration))
        self.assertGreaterEqual(word_count, 120)
        self.assertLessEqual(word_count, 148)
        lowered = polished.narration.lower()
        self.assertLessEqual(lowered.count("sogdian merchants"), 1)
        concrete_clues = ("ancient letters", "dunhuang", "lingua franca", "afrasia", "gold", "pepper", "murals")
        self.assertGreaterEqual(sum(clue in lowered for clue in concrete_clues), 4)
        normalized_beats = {
            re.sub(r"[^a-z0-9]+", " ", beat.lower()).strip()
            for beat in polished.narration_beats
        }
        self.assertEqual(len(polished.narration_beats), len(normalized_beats))


class ThumbnailQualityTests(unittest.TestCase):
    def test_core_topic_and_real_brand_are_preserved(self) -> None:
        maker = ThumbnailMaker()
        title = "What the surviving record reveals about Axum Obelisks"
        display = maker._display_title(title, "ancient_history")
        self.assertIn("AXUM", display)
        self.assertIn("OBELISKS", display)
        self.assertEqual("SECRETS OF TIME", maker._badge_text("ancient_history"))
        self.assertEqual([], maker.thumbnail_copy_issues(title, "ancient_history"))

    def test_voice_change_thumbnail_keeps_attraction(self) -> None:
        maker = ThumbnailMaker()
        title = "The tiny voice change that can reveal attraction"
        display = maker._display_title(title, "brain_lens")
        self.assertIn("VOICE", display)
        self.assertIn("CHANGE", display)
        self.assertIn("ATTRACTION", display)
        issues = maker.thumbnail_copy_issues(
            title,
            "brain_lens",
            display_text="TINY VOICE CHANGE",
            badge_text="BRAIN FACTS",
        )
        self.assertTrue(any("brand" in issue for issue in issues))
        self.assertTrue(any("attraction" in issue.lower() for issue in issues))

    def test_mixed_signal_thumbnail_is_short_and_searchable(self) -> None:
        maker = ThumbnailMaker()
        title = "Why Mixed Signals Feel So Intense After Hot-and-Cold Attention"
        self.assertEqual("MIXED SIGNALS LOOP", maker._display_title(title, "brain_lens"))
        self.assertEqual([], maker.thumbnail_copy_issues(title, "brain_lens"))

    def test_micro_flirting_thumbnail_uses_explicit_curiosity_copy(self) -> None:
        maker = ThumbnailMaker()
        title = "Micro Flirting: How to Read the Spark Without Calling It Proof"
        self.assertEqual(
            "MICRO FLIRTING OR FRIENDLINESS?",
            maker._display_title(title, "brain_lens"),
        )
        self.assertNotIn(" READ", maker._display_title(title, "brain_lens"))
        self.assertEqual([], maker.thumbnail_copy_issues(title, "brain_lens"))

    def test_almost_relationship_thumbnail_is_grammatical_and_specific(self) -> None:
        maker = ThumbnailMaker()
        title = "The Attachment Loop Behind Almost Relationships"
        self.assertEqual(
            "ALMOST RELATIONSHIPS: THE LOOP",
            maker._display_title(title, "brain_lens"),
        )
        self.assertEqual([], maker.thumbnail_copy_issues(title, "brain_lens"))

    def test_future_faking_thumbnail_avoids_generic_attachment_copy(self) -> None:
        maker = ThumbnailMaker()
        title = "The Attachment Loop Behind Future Faking and How to Break It"
        self.assertEqual(
            "FUTURE FAKING: THE TRAP",
            maker._display_title(title, "brain_lens"),
        )
        self.assertEqual([], maker.thumbnail_copy_issues(title, "brain_lens"))

    def test_friends_with_benefits_thumbnail_keeps_the_complete_phrase(self) -> None:
        maker = ThumbnailMaker()
        title = "Friends with Benefits Boundaries: When Feelings Change"
        self.assertEqual(
            "FRIENDS WITH BENEFITS?",
            maker._display_title(title, "brain_lens"),
        )
        self.assertEqual([], maker.thumbnail_copy_issues(title, "brain_lens"))

    def test_history_subject_specific_thumbnail_copy_stays_searchable(self) -> None:
        maker = ThumbnailMaker()
        cases = {
            "How the Rosetta Stone Unlocked Egyptian Hieroglyphs": "ROSETTA STONE",
            "Why the Cadaver Synod Put a Dead Pope on Trial": "CADAVER SYNOD",
            "How the Kingdom of Kush Ruled Egypt as the Twenty-Fifth Dynasty": "KINGDOM OF KUSH",
            "How Nubian Pyramids Made Kushite Royal Power Visible in Stone": "NUBIAN PYRAMIDS",
            "Inside Angkor Wat: The Towers, Moat, and Sacred Mountain": "ANGKOR WAT",
            "Inside Tikal: Maya Kings, Reservoirs, and Jungle Temples": "TIKAL",
            "Great Zimbabwe: How Archaeology Overturned a Colonial Myth": "GREAT ZIMBABWE: AFRICAN ORIGINS CONFIRMED",
            "Axum's Giant Stelae: Royal Tombs Built Like Stone Palaces": "AXUM",
            "Olmec Colossal Heads: Seventeen Rulers Carved in Basalt": "OLMEC COLOSSAL HEADS",
        }
        for title, expected in cases.items():
            display = maker._display_title(title, "ancient_history")
            self.assertIn(expected, display)
            self.assertEqual([], maker.thumbnail_copy_issues(title, "ancient_history"))

    def test_chichen_thumbnail_uses_specific_short_copy(self) -> None:
        maker = ThumbnailMaker()
        title = "Inside Chichen Itza: El Castillo, the Great Ball Court, and Sacred Cenote"
        self.assertEqual(
            "CHICHEN ITZA: RITUAL POWER",
            maker._display_title(title, "ancient_history"),
        )
        self.assertEqual([], maker.thumbnail_copy_issues(title, "ancient_history"))

    def test_stonehenge_thumbnail_keeps_the_subject_and_short_curiosity_copy(self) -> None:
        maker = ThumbnailMaker()
        title = "Stonehenge: What the Surviving Evidence Actually Reveals"
        self.assertEqual(
            "STONEHENGE: WHAT SURVIVED?",
            maker._display_title(title, "ancient_history"),
        )
        self.assertEqual([], maker.thumbnail_copy_issues(title, "ancient_history"))

    def test_lachish_thumbnail_uses_subject_specific_siege_copy(self) -> None:
        maker = ThumbnailMaker()
        title = "How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence"
        self.assertEqual(
            "LACHISH: ASSYRIA'S SIEGE MACHINE",
            maker._display_title(title, "ancient_history"),
        )
        self.assertEqual([], maker.thumbnail_copy_issues(title, "ancient_history"))


class LongVideoPackagingTests(unittest.TestCase):
    def test_lachish_forced_ancient_long_title_keeps_ranked_specific_copy(self) -> None:
        candidate = topic(
            niche_id="ancient_history",
            title="Assyrian Siege of Lachish",
            subject="Assyrian Siege of Lachish",
            content_kind="video",
        )
        factory = ShortsFactory.__new__(ShortsFactory)

        self.assertEqual(
            "How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence",
            factory._ancient_long_title(candidate),
        )

    def test_friends_with_benefits_long_titles_are_specific_and_conflict_driven(self) -> None:
        candidate = topic(
            title="Friends with Benefits Boundaries",
            subject="Friends with Benefits Boundaries",
            content_kind="video",
        )
        variants = TitleLab().make_variants(candidate, count=5)
        self.assertEqual(
            "Friends With Benefits Boundaries: When Feelings Change",
            variants[0].title,
        )
        self.assertTrue(all("Friends With Benefits" in item.title for item in variants[:3]))

    def test_video_chapters_use_actual_scene_starts_and_matching_labels(self) -> None:
        candidate = topic(
            content_kind="video",
            scene_plan=[
                ScenePlanItem(narration=f"Scene {index}", visual_text=label)
                for index, label in enumerate(("Opening", "Evidence", "Boundary", "Reset"))
            ],
        )
        timeline = [
            {"beat_index": 0, "start": 0.0},
            {"beat_index": 1, "start": 31.4},
            {"beat_index": 2, "start": 64.9},
            {"beat_index": 3, "start": 101.2},
        ]
        metadata = {"content_kind": "video", "description": "Description"}
        factory = ShortsFactory.__new__(ShortsFactory)

        factory._append_video_chapters(metadata, candidate, 220.0, timeline)

        self.assertIn(
            "Chapters:\n0:00 Opening\n0:31 Evidence\n1:04 Boundary\n1:41 Reset",
            metadata["description"],
        )
        self.assertEqual([0, 1, 2, 3], [item["scene_index"] for item in metadata["chapters"]])

    def test_stonehenge_long_titles_lead_with_a_specific_documented_conflict(self) -> None:
        candidate = topic(
            niche_id="ancient_history",
            title="Stonehenge",
            subject="Stonehenge",
            content_kind="video",
        )
        variants = TitleLab().make_variants(candidate, count=5)
        self.assertEqual(
            "Stonehenge Was Built Over 1,500 Years—Here’s What Survived",
            variants[0].title,
        )
    def test_lachish_long_titles_lead_with_ramp_reliefs_and_evidence(self) -> None:
        candidate = topic(
            niche_id="ancient_history",
            title="Assyrian Siege of Lachish",
            subject="Assyrian Siege of Lachish",
            content_kind="video",
        )
        variants = TitleLab().make_variants(candidate, count=5)

        self.assertEqual(
            "How Assyria Broke Lachish: Siege Ramp, Reliefs, and Evidence",
            variants[0].title,
        )
        self.assertTrue(all("Lachish" in item.title for item in variants[:3]))


class TitleSearchabilityTests(unittest.TestCase):
    def test_mixed_signal_title_keeps_the_searchable_subject(self) -> None:
        lab = TitleLab()
        candidate = topic(
            title="The attachment signal that keeps changing the answer",
            subject="Mixed Attachment Signals",
        )
        chosen, ranked = lab.choose(
            "brain_lens",
            candidate,
            lab.make_variants(candidate, count=4),
            {},
            epsilon=0.0,
        )
        self.assertTrue(lab.title_contains_search_core(candidate, chosen.title))
        self.assertIn("mixed", chosen.title.lower())
        self.assertTrue(all(lab.title_contains_search_core(candidate, item.title) for item in ranked))

    def test_micro_flirting_titles_match_the_signal_limits_script(self) -> None:
        lab = TitleLab()
        candidate = topic(title="Micro Flirting", subject="Micro Flirting")
        variants = lab.make_variants(candidate, count=3)
        self.assertTrue(any("without calling it proof" in item.title.lower() for item in variants))
        self.assertFalse(any("attention back" in item.title.lower() for item in variants))
        self.assertTrue(all(lab.title_contains_search_core(candidate, item.title) for item in variants))

    def test_default_brain_titles_do_not_make_every_topic_an_attention_loop(self) -> None:
        lab = TitleLab()
        relationship = topic(title="Conflict Repair", subject="Conflict Repair")
        general = topic(title="Decision Fatigue", subject="Decision Fatigue")
        relationship_titles = lab.make_variants(relationship, count=3)
        general_titles = lab.make_variants(general, count=3)
        self.assertFalse(any("attention back" in item.title.lower() for item in relationship_titles + general_titles))
        self.assertTrue(all(lab.title_contains_search_core(relationship, item.title) for item in relationship_titles))
        self.assertTrue(all(lab.title_contains_search_core(general, item.title) for item in general_titles))

    def test_history_titles_use_subject_specific_factual_curiosity(self) -> None:
        lab = TitleLab()
        cases = {
            "Rosetta Stone": "unlocked egyptian hieroglyphs",
            "Cadaver Synod": "dead pope on trial",
            "Kingdom of Kush": "twenty-fifth dynasty",
            "Nubian Pyramids": "kushite royal power",
        }
        for subject, expected in cases.items():
            candidate = topic(niche_id="ancient_history", title=subject, subject=subject)
            titles = [item.title.lower() for item in lab.make_variants(candidate, count=3)]
            self.assertTrue(any(expected in title for title in titles), subject)

    def test_chichen_title_names_the_three_visible_clues(self) -> None:
        candidate = topic(
            niche_id="ancient_history",
            title="Chichen Itza",
            subject="Chichen Itza",
        )
        titles = [item.title for item in TitleLab().make_variants(candidate, count=3)]
        self.assertIn(
            "Inside Chichen Itza: El Castillo, the Great Ball Court, and Sacred Cenote",
            titles,
        )

    def test_visual_ready_history_subjects_choose_the_specific_title(self) -> None:
        lab = TitleLab()
        expected = {
            "Angkor Wat": "Inside Angkor Wat: The Towers, Moat, and Sacred Mountain",
            "Tikal": "Inside Tikal: Maya Kings, Reservoirs, and Jungle Temples",
            "Chichen Itza": "Inside Chichen Itza: El Castillo, the Great Ball Court, and Sacred Cenote",
            "Great Zimbabwe": "Great Zimbabwe: How Archaeology Overturned a Colonial Myth",
            "Axum Obelisks": "Axum's Giant Stelae: Royal Tombs Built Like Stone Palaces",
            "Olmec Colossal Heads": "Olmec Colossal Heads: Seventeen Rulers Carved in Basalt",
        }
        for subject, exact_title in expected.items():
            with self.subTest(subject=subject):
                candidate = topic(
                    niche_id="ancient_history",
                    title=subject,
                    subject=subject,
                    content_kind="short",
                )
                chosen, ranked = lab.choose(
                    "ancient_history",
                    candidate,
                    lab.make_variants(candidate, count=4),
                    {},
                    epsilon=0.0,
                )
                self.assertEqual(exact_title, chosen.title)
                self.assertTrue(chosen.pattern_id.startswith("history_short_subject_"))
                self.assertEqual([exact_title], [item.title for item in ranked])


class ScriptEditorialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.writer = ScriptWriter(
            ScriptWriterConfig(
                provider="template",
                ollama_model="qwen3.6:latest",
                ollama_url="http://localhost:11434/api/generate",
                timeout_seconds=120,
            )
        )

    def test_voice_change_copy_is_context_dependent(self) -> None:
        voice = topic()
        closer = self.writer._closer(voice).lower()
        self.assertIn("question", closer)
        self.assertIn("reciprocity", closer)
        self.assertNotIn("tells you whether it is mutual", closer)

    def test_history_status_is_allowed_outside_the_actual_generic_stakes_template(self) -> None:
        generic = (
            "The real stakes of Test Monument were practical: safety, work, wealth, "
            "status, and survival."
        )
        specific = (
            "For people living through the reform, status changed through documented "
            "tax obligations and access to protected roads."
        )
        self.assertIsNotNone(self.writer._bad_ai_line_issue(generic))
        self.assertIsNone(self.writer._bad_ai_line_issue(specific))
        self.assertEqual(
            "line ends on a transitive verb without its object",
            self.writer._bad_ai_line_issue("One excavated burial is later and demonstrates."),
        )

    def test_pipeline_rejects_mojibake_and_orphaned_history_clause(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        factory = ShortsFactory.__new__(ShortsFactory)
        candidate = topic(
            niche_id="ancient_history",
            title="Alexander Siege of Tyre",
            subject="alexander siege of tyre",
            narration=(
                "Alexander\u00e2\u20ac\u2122s fleet approached the island. "
                "When the seabed dropped sharply."
            ),
            narration_beats=[
                "Alexander\u00e2\u20ac\u2122s fleet approached the island.",
                "When the seabed dropped sharply.",
            ],
        )

        issues = factory._script_quality_issues(channel, candidate)

        self.assertIn("Ancient script contains broken text encoding", issues)
        self.assertIn("Ancient script contains an orphaned dependent clause", issues)

    def test_pipeline_rejects_long_script_with_broken_parenthetical_caption(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        factory = ShortsFactory.__new__(ShortsFactory)
        candidate = topic(
            niche_id="ancient_history",
            title="Aksum's Granite Stelae",
            subject="axum obelisks",
            content_kind="video",
            narration=(
                "The tallest surviving monument measures about 24 metres "
                "(79 feet tall. Its carved doors imitate a royal building."
            ),
            narration_beats=[
                (
                    "The tallest surviving monument measures about 24 metres "
                    "(79 feet tall."
                ),
                "Its carved doors imitate a royal building.",
            ],
        )

        issues = factory._script_quality_issues(channel, candidate)

        self.assertTrue(
            any("Long video cannot form clean caption phrases" in issue for issue in issues),
            issues,
        )

    def test_lachish_long_plan_is_specific_source_led_and_free_of_boilerplate(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        candidate = topic(
            niche_id="ancient_history",
            title="Assyrian Siege of Lachish",
            subject="assyrian siege of lachish",
            content_kind="video",
        )

        plan = self.writer._lachish_long_video_plan(
            channel,
            candidate,
            "Assyrian Siege of Lachish",
        )
        narration = " ".join(scene.narration for scene in plan)
        lowered = narration.lower()

        self.assertEqual(len(plan), 20)
        self.assertGreaterEqual(len(re.findall(r"[A-Za-z0-9']+", narration)), 1200)
        self.assertIn("859 arrowheads", narration)
        self.assertIn("counter-ramp", narration)
        self.assertIn("archaeomagnetic", narration)
        self.assertIn("nearly a thousand kilometers", lowered)
        self.assertNotIn("thousands of kilometers", lowered)
        preferred = [scene.preferred_image_url for scene in plan if scene.preferred_image_url]
        self.assertEqual(len(preferred), 15)
        self.assertEqual(len(set(preferred)), 15)
        self.assertTrue(all(url.startswith("https://upload.wikimedia.org/") for url in preferred))
        by_caption = {scene.visual_text: scene.preferred_image_url for scene in plan}
        self.assertIn("Tel-Lakhish-V2-562.jpg", by_caption["Why Lachish mattered"])
        self.assertIn("LachishRamp053011.jpg", by_caption["Building the siege ramp"])
        for forbidden in (
            "ships, money",
            "panels 9, 10",
            "the familiar edge of the story",
            "the cleanest explanation",
            "now follow assyrian siege",
            "return to the opening clue",
        ):
            self.assertNotIn(forbidden, lowered)

        candidate.narration_beats = [scene.narration for scene in plan]
        candidate.narration = narration
        candidate.visual_captions = [scene.visual_text for scene in plan]
        factory = ShortsFactory.__new__(ShortsFactory)
        self.assertEqual(factory._script_quality_issues(channel, candidate), [])

    def test_priority_relationship_topics_have_behavior_first_fallback_hooks(self) -> None:
        for subject in (
            "Orbiting After Breakup",
            "Voice Note Intimacy",
            "Apology Consistency",
            "Friends With Benefits Boundaries",
            "Crush Idealization",
        ):
            candidate = topic(title=subject, subject=subject)
            opener = self.writer._opener(candidate)
            self.assertFalse(opener.lower().startswith("you notice"), subject)
            self.assertTrue(self.writer._brain_lens_behavior_first(opener), subject)

    def test_deterministic_brain_short_fallback_is_complete_and_rotates(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "brain_lens")
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.config = config
        factory.image_fetcher = HybridMediaFetcher()
        factory.topic_planner = TopicPlanner(timezone="UTC")

        first = factory._brain_short_deterministic_fallback(channel, set())
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.subject, "Micro Flirting")
        self.assertGreaterEqual(len(first.narration_beats), 6)
        self.assertTrue(self.writer._brain_lens_behavior_first(first.hook))
        self.assertEqual(
            self.writer.editorial_quality_issues(
                first,
                beats=first.narration_beats,
                content_kind="short",
            ),
            [],
        )
        self.assertTrue(first.source_urls)

        second = factory._brain_short_deterministic_fallback(
            channel,
            {first.subject.lower(), first.title.lower()},
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.subject, "Reply Time Anxiety")
        self.assertGreaterEqual(len(second.narration_beats), 6)

        third = factory._brain_short_deterministic_fallback(
            channel,
            {
                first.subject.lower(),
                first.title.lower(),
                second.subject.lower(),
                second.title.lower(),
            },
        )
        self.assertIsNotNone(third)
        assert third is not None
        self.assertEqual(third.subject, "Emotional Safety in Dating")

        fourth = factory._brain_short_deterministic_fallback(
            channel,
            {
                first.subject.lower(),
                first.title.lower(),
                second.subject.lower(),
                second.title.lower(),
                third.subject.lower(),
                third.title.lower(),
            },
        )
        self.assertIsNotNone(fourth)
        assert fourth is not None
        self.assertEqual(fourth.subject, "Conflict Repair")

        avoided = {
            first.subject.lower(),
            first.title.lower(),
            second.subject.lower(),
            second.title.lower(),
            third.subject.lower(),
            third.title.lower(),
            fourth.subject.lower(),
            fourth.title.lower(),
        }
        later_candidates = []
        for _ in range(16):
            candidate = factory._brain_short_deterministic_fallback(channel, avoided)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            later_candidates.append(candidate)
            avoided.add(candidate.subject.lower())
            avoided.add(candidate.title.lower())
        self.assertEqual(later_candidates[-1].subject, "Honest Attraction")

        fresh_candidates = []
        for _ in range(8):
            candidate = factory._brain_short_deterministic_fallback(channel, avoided)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            fresh_candidates.append(candidate)
            avoided.add(candidate.subject.lower())
            avoided.add(candidate.title.lower())
        self.assertEqual(fresh_candidates[-1].subject, "Follow-Up Questions")

        composer = SubtitleComposer(max_words_per_caption=4, max_chars_per_second=100.0)
        title_lab = TitleLab()
        for candidate in (first, second, third, fourth, *later_candidates, *fresh_candidates):
            word_count = len(candidate.narration.split())
            self.assertGreaterEqual(word_count, 122)
            self.assertLessEqual(word_count, 142)
            self.assertTrue(candidate.source_urls, candidate.title)
            self.assertIn(
                candidate.subject.lower(),
                factory.topic_planner.research.brain_lens_priority_subjects,
                candidate.title,
            )
            self.assertEqual(
                [],
                factory._script_quality_issues(channel, candidate),
                candidate.title,
            )
            self.assertTrue(
                title_lab.title_contains_search_core(candidate, candidate.title),
                candidate.title,
            )
            self.assertEqual(
                [],
                self.writer.editorial_quality_issues(
                    candidate,
                    beats=candidate.narration_beats,
                    content_kind="short",
                ),
                candidate.title,
            )
            scenes = [
                SubtitleSegment(index * 20.0, (index + 1) * 20.0, beat)
                for index, beat in enumerate(candidate.narration_beats)
            ]
            captions = composer.caption_segments_from_scene_segments(scenes)
            self.assertEqual(
                [],
                composer.quality_issues(captions, allow_clause_continuations=True),
                candidate.title,
            )

    def test_short_narration_target_protects_retention_pacing(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        # Below the 50s floor: clamp up so shorts never land under-length.
        self.assertEqual(
            factory._short_narration_target_seconds(79),
            50.0,
        )
        mid = factory._short_narration_target_seconds(130)
        self.assertAlmostEqual(mid, 130 * 60.0 / 142.0, places=2)
        self.assertGreaterEqual(mid, 50.0)
        self.assertLessEqual(mid, 58.9)
        self.assertEqual(factory._short_narration_target_seconds(160), 58.9)
        # Explicit legacy window still supports precise WPM math.
        self.assertAlmostEqual(
            factory._short_narration_target_seconds(
                79,
                maximum_seconds=35.9,
                minimum_seconds=32.0,
            ),
            33.38,
            places=2,
        )

    def test_short_narration_target_preserves_caption_readability(self) -> None:
        factory = ShortsFactory.__new__(ShortsFactory)
        beats = [
            "Axum's stone stelae rise.",
            "Elite tombs lie below.",
            "False doors cover stone.",
            "Windows copy palace fronts.",
            "Each monument used stone.",
            "Quarries supplied slabs.",
            "Builders moved each slab.",
            "Raising required teamwork.",
            "Tomb chambers held elites.",
            "Carvings displayed rank.",
            "Yet giant stelae fell.",
            "Failures exposed the risk.",
            "Survivors still mark Axum.",
            "Ethiopia guards this field.",
            "Scale signaled royal rank.",
            "Carvers cut hard granite.",
            "Tombs linked memory, rank.",
            "Craft turned stone upward.",
            "Engineering displayed rank.",
            "Axum built a skyline.",
        ]
        durations = [
            1.662, 1.643, 1.689, 1.598, 1.644,
            1.461, 1.661, 1.662, 1.689, 1.552,
            1.416, 1.616, 1.643, 1.689, 1.689,
            1.571, 1.616, 1.479, 1.707, 1.443,
        ]
        composer = SubtitleComposer(max_words_per_caption=4)

        target = factory._caption_safe_short_target_seconds(
            scene_durations=durations,
            narration_beats=beats,
            desired_seconds=32.13,
            maximum_seconds=35.9,
            composer=composer,
        )

        self.assertGreater(target, 33.5)
        self.assertLessEqual(target, 35.9)
        scale = target / sum(durations)
        scaled = [duration * scale for duration in durations]
        scaled[-1] += target - sum(scaled)
        scenes = composer.scene_segments_from_durations(beats, scaled, total_duration=target)
        captions = composer.caption_segments_from_scene_segments(scenes)
        maximum_cps = max(
            composer._character_count(caption.text) / (caption.end - caption.start)
            for caption in captions
        )
        self.assertLessEqual(maximum_cps, composer.max_cps - 0.19)

    def test_lascaux_recovery_script_fits_caption_and_pacing_gates(self) -> None:
        beats = [
            "Lascaux paintings faced a new danger.",
            "Teenagers found the cave first.",
            "That happened in 1940.",
            "Cave curves gave horses volume.",
            "Aurochs towered beside them.",
            "Pigments kept colors vivid.",
            "Rock contours shaped bodies.",
            "Then mass tourism arrived.",
            "Hot air entered the cave fast.",
            "Carbon dioxide levels climbed.",
            "Microbes threatened painted walls.",
            "France closed Lascaux in 1963.",
            "That decision protected the original.",
            "Replicas later welcomed visitors.",
            "The cave stayed shut and guarded.",
            "The find quickly caused a crisis.",
            "Closing Lascaux saved the art.",
        ]
        durations = [
            2.375, 2.075, 2.125, 2.225, 1.875, 1.675, 1.9, 1.9, 2.025,
            2.2, 2.25, 2.775, 2.3, 2.525, 2.15, 2.0, 2.1,
        ]
        composer = SubtitleComposer(max_words_per_caption=4)

        target = ShortsFactory._caption_safe_short_target_seconds(
            scene_durations=durations,
            narration_beats=beats,
            desired_seconds=32.53,
            maximum_seconds=35.9,
            composer=composer,
        )

        self.assertLessEqual(target, 35.9)
        word_count = len(re.findall(r"[A-Za-z0-9']+", " ".join(beats)))
        self.assertGreaterEqual(word_count / (target / 60.0), 135.0)
        scale = target / sum(durations)
        scaled = [duration * scale for duration in durations]
        scaled[-1] += target - sum(scaled)
        scenes = composer.scene_segments_from_durations(beats, scaled, total_duration=target)
        captions = composer.caption_segments_from_scene_segments(scenes)
        self.assertEqual([], composer.quality_issues(captions, cps_tolerance=0.05))

    def test_brain_long_reality_test_uses_complete_caption_safe_clauses(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "brain_lens")
        candidate = topic(
            title="Social Media Jealousy",
            subject="Social Media Jealousy",
            content_kind="video",
        )

        plan = self.writer._brain_long_video_plan(
            channel,
            candidate,
            candidate.subject,
        )
        reality_test = next(
            scene.narration
            for scene in plan
            if scene.visual_text == "A personal observation window"
        )

        self.assertIn("not a validated test", reality_test)
        for complete_clause in (
            "Keep work, sleep, movement, and friendships visible.",
            "Do not act unavailable, provoke jealousy, or use silence to control the outcome.",
        ):
            self.assertIn(complete_clause, reality_test)

    def test_future_faking_long_plan_has_no_midphrase_caption_splits(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "brain_lens")
        candidate = topic(
            title="The Attachment Loop Behind Future Faking and How to Break It",
            subject="Future Faking",
            content_kind="video",
        )
        plan = self.writer._brain_long_video_plan(channel, candidate, candidate.subject)
        scene_duration = 28.5
        scene_timeline = [
            SubtitleSegment(
                start=index * scene_duration,
                end=(index + 1) * scene_duration,
                text=scene.narration,
            )
            for index, scene in enumerate(plan)
        ]
        # This regression isolates phrase boundaries. Production CPS is checked
        # against the actual TTS scene timings, not equal synthetic scene slots.
        composer = SubtitleComposer(max_words_per_caption=8, max_chars_per_second=100.0)
        captions = composer.caption_segments_from_scene_segments(scene_timeline)

        self.assertEqual([], composer.quality_issues(captions))
        dangling = re.compile(
            r"\b(?:a|an|the|of|to|under|more|first|how|because|larger|less|is|are|was|were)[,;:]?$",
            flags=re.IGNORECASE,
        )
        continuation = re.compile(
            r"^(?:from|of|to|than|under|with|without)\b",
            flags=re.IGNORECASE,
        )
        awkward = []
        for index, caption in enumerate(captions):
            previous = captions[index - 1].text if index else ""
            has_next = index + 1 < len(captions)
            if has_next and dangling.search(caption.text):
                awkward.append(caption.text)
            if (
                index
                and continuation.search(caption.text)
                and not re.search(r'[.!?]["\u2019\u201d]?$', previous)
                and not re.search(r'[.!?]["\u2019\u201d]?$', caption.text)
            ):
                awkward.append(caption.text)
        self.assertEqual([], awkward)

    def test_brain_long_plan_is_progressive_and_fits_ten_minutes(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "brain_lens")
        candidate = topic(
            title="Ghosting Recovery: Stop Letting Silence Write the Story",
            subject="Ghosting Recovery",
            content_kind="video",
            hook="They disappear without an answer, and your mind keeps drafting the explanation they never gave.",
        )

        plan = self.writer._brain_long_video_plan(channel, candidate, candidate.subject)
        word_count = sum(
            len(re.findall(r"[A-Za-z0-9']+", scene.narration))
            for scene in plan
        )

        self.assertEqual(len(plan), 20)
        self.assertEqual(len({scene.visual_text for scene in plan}), 20)
        self.assertGreaterEqual(word_count, 1450)
        self.assertLessEqual(word_count, 1520)
        self.assertIn("phone", plan[0].narration.lower())
        self.assertTrue(
            any(
                marker in plan[0].search_terms[0].lower()
                for marker in ("phone", "message", "reply", "text")
            )
        )
        self.assertEqual(
            self.writer.editorial_quality_issues(
                candidate,
                beats=[scene.narration for scene in plan],
                content_kind="video",
            ),
            [],
        )
        for subject in (
            "Online Dating Burnout",
            "Friends with Benefits Boundaries",
            "Crush Idealization",
            "Fear of Abandonment",
        ):
            variant = topic(
                title=f"{subject}: The Pattern to Watch Before You React",
                subject=subject,
                content_kind="video",
            )
            variant_plan = self.writer._brain_long_video_plan(
                channel,
                variant,
                variant.subject,
            )
            variant_words = sum(
                len(re.findall(r"[A-Za-z0-9']+", scene.narration))
                for scene in variant_plan
            )
            self.assertLessEqual(variant_words, 1520, subject)
            variant_script = " ".join(scene.narration for scene in variant_plan).lower()
            self.assertNotIn(f"{subject.lower()} is leaving", variant_script)
            self.assertNotIn(f"if {subject.lower()} is linked", variant_script)
            if subject == "Friends with Benefits Boundaries":
                self.assertEqual(
                    variant_plan[0].visual_text,
                    "The moment that starts the story",
                )
                self.assertNotIn("phone", variant_plan[0].narration.lower())
                self.assertNotIn("phone", variant_plan[0].search_terms[0].lower())

    def test_context_word_does_not_false_trigger_phone_visuals(self) -> None:
        searches = self.writer._brain_lens_visual_search_terms(
            "Ghosting Recovery",
            "A licensed mental-health professional can help examine the full context.",
            content_kind="video",
        )
        self.assertIn("therapist", searches[0].lower())
        self.assertNotIn("phone", searches[0].lower())

    def test_brain_long_repair_scene_outranks_planning_language(self) -> None:
        searches = self.writer._brain_lens_visual_search_terms(
            "Online Dating Burnout",
            "A charged reunion can feel like resolution. Repair asks for more, with a realistic plan and follow-through through an ordinary week.",
            content_kind="video",
        )
        self.assertIn("rebuilding trust", searches[0].lower())
        self.assertNotIn("weekly schedule", searches[0].lower())

    def test_brain_long_app_boundary_outranks_app_topic_words(self) -> None:
        searches = self.writer._brain_lens_visual_search_terms(
            "Online Dating Burnout",
            "A usable boundary sounds like this: I will stop using the app when it makes people feel like inventory. A boundary under your control protects time.",
            content_kind="video",
        )
        self.assertIn("setting a calm boundary", searches[0].lower())
        self.assertNotIn("dating profile", searches[0].lower())

    def test_brain_long_dating_app_opener_uses_broad_real_footage_query(self) -> None:
        searches = self.writer._brain_lens_visual_search_terms(
            "Online Dating Burnout",
            "You open the app, compare profiles, and the phone stays silent.",
            content_kind="video",
        )
        self.assertEqual(
            searches[0],
            "adult couple using smartphones sitting apart relationship distance",
        )

    def test_brain_long_read_answer_scene_prefers_conversation_over_documents(self) -> None:
        searches = self.writer._brain_lens_visual_search_terms(
            "Fear of Abandonment",
            "Listen to the words, the specificity, and the follow-through. Grade the pattern, not tone alone.",
            content_kind="video",
        )
        self.assertIn("listening carefully", searches[0].lower())
        self.assertNotIn("calendar", searches[0].lower())

    def test_brain_long_dna_idea_cannot_override_curated_subject(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "brain_lens")
        planner = TopicPlanner(timezone="UTC")
        pack = {
            "headline": "Why ghosting keeps your brain searching for an ending",
            "subject": "ghosting recovery",
            "bullets": [
                "Ghosting removes direct feedback while the mind continues searching for an explanation.",
                "Repeated checking can give brief relief without creating clarity.",
                "Direct behavior over time is stronger evidence than an imagined motive.",
            ],
            "sources": ["https://example.org/relationship-research"],
            "visual_queries": ["adult checking phone after unanswered relationship message"],
            "image_urls": [],
        }
        dna = {
            "video_ideas": [{
                "title": "Your Memories Lie to You",
                "angle": "Memory reconstruction versus replay",
            }]
        }
        with (
            patch.object(planner.trends, "collect", return_value=[]),
            patch.object(planner.research, "brain_lens_pack", return_value=pack),
        ):
            planned = planner.plan(
                channel,
                avoid_titles=set(),
                content_kind="video",
                dna=dna,
            )

        self.assertEqual(planned.subject, "ghosting recovery")
        self.assertIn("ghosting", planned.title.lower())
        self.assertNotIn("memories", planned.title.lower())

    def test_brain_title_rejects_unsupported_relationship_certainty(self) -> None:
        self.assertIsNotNone(
            self.writer._title_formula_issue(
                "brain_lens",
                "When Their Texts Get Shorter, Your Brain Already Knows They're Gone",
            )
        )
        self.assertIsNotNone(
            self.writer._title_formula_issue(
                "brain_lens",
                "Your Brain Hates This One Text Move in Dating",
            )
        )
        long_topic = topic(content_kind="video")
        prompt = self.writer._ollama_title_prompt(
            load_config(ROOT / "config" / "settings.yaml").channels[1],
            long_topic,
        )
        self.assertIn("long-form YouTube video title", prompt)
        self.assertNotIn("title for Shorts", prompt)

    def test_future_faking_has_specific_script_and_couple_visual_queries(self) -> None:
        candidate = topic(title="Why Future Faking keeps pulling your attention back", subject="Future Faking")
        lines = self.writer._video_expansion_lines(candidate, "future faking")
        self.assertTrue(any("vague calendar" in line.lower() for line in lines))
        self.assertFalse(any("psychology term" in line.lower() for line in lines))
        searches = self.writer._brain_lens_visual_search_terms(
            "Future Faking",
            "The visible pattern is a vivid future, a vague calendar, and no dependable next step.",
        )
        self.assertTrue(any("couple" in query.lower() for query in searches[:2]))

    def test_crush_idealization_has_concrete_arc_visuals_and_titles(self) -> None:
        candidate = topic(title="Crush Idealization", subject="Crush Idealization")
        lines = self.writer._video_expansion_lines(candidate, "Crush Idealization")
        joined = " ".join(lines).lower()
        self.assertIn("one charming date", joined)
        self.assertIn("two lists", joined)
        self.assertIn("one real plan", joined)
        searches = self.writer._brain_lens_visual_search_terms(
            "Crush Idealization",
            "Idealization fills missing information with your best guesses.",
        )
        self.assertTrue(all("couple" in query.lower() for query in searches[:2]))
        lab = TitleLab()
        variants = lab.make_variants(candidate, count=3)
        self.assertTrue(any("before you know them" in item.title.lower() for item in variants))
        self.assertTrue(all(lab.title_contains_search_core(candidate, item.title) for item in variants))

    def test_axum_measurement_sentence_is_repaired(self) -> None:
        raw = (
            "The Obelisk of Axum is a 4th-century CE, 24-metre (79 ft) tall phonolite stele, "
            "weighing 160 tonnes, in the city of Axum."
        )
        repaired = self.writer._simplify_history_line(raw)
        self.assertIn("24-metre-tall", repaired)
        self.assertIn("dating to the fourth century CE", repaired)

    def test_axum_short_keeps_a_midpoint_contrast_after_word_budgeting(self) -> None:
        config = load_config(ROOT / "config" / "settings.yaml")
        channel = next(item for item in config.channels if item.id == "ancient_history")
        researcher = ContentResearcher()
        facts = researcher._curated_history_short_facts("Axum Obelisks")
        candidate = topic(
            niche_id="ancient_history",
            title="Axum Obelisks",
            subject="Axum Obelisks",
            content_kind="short",
            scene_plan=[
                ScenePlanItem(narration="Axum Obelisks", visual_text="Axum Obelisks"),
                *[
                    ScenePlanItem(narration=fact, visual_text=fact)
                    for fact in facts
                ],
                ScenePlanItem(narration="Axum Obelisks", visual_text="Axum Obelisks"),
            ],
        )

        polished = self.writer._polish_scene_plan(channel, candidate, content_kind="short")
        middle = " ".join(polished.narration_beats[1:-1]).lower()

        self.assertIn(" while ", middle)
        self.assertFalse(
            any(
                "lacks a midpoint" in issue
                for issue in self.writer.editorial_quality_issues(polished, content_kind="short")
            )
        )

    def test_nubian_pyramids_get_specific_facts_queries_and_payoff(self) -> None:
        researcher = ContentResearcher()
        facts = researcher._history_supporting_bullets("Nubian Pyramids")
        queries = researcher._history_visual_queries("Nubian Pyramids", "nubian pyramids")
        self.assertTrue(any("underground" in fact.lower() for fact in facts))
        self.assertTrue(any("meroe" in query.lower() for query in queries))
        candidate = topic(niche_id="ancient_history", title="Nubian Pyramids", subject="nubian pyramids")
        closer = self.writer._closer(candidate)
        self.assertIn("Meroe", closer)
        self.assertIn("underground chambers", closer)

    def test_chichen_facts_are_complete_distinct_and_fit_short_scenes(self) -> None:
        researcher = ContentResearcher()
        candidate = topic(
            niche_id="ancient_history",
            title="Chichen Itza",
            subject="Chichen Itza",
        )
        facts = [
            researcher.history_fact_overrides["chichen itza"],
            *researcher._history_supporting_bullets("Chichen Itza"),
        ]
        lines = [
            *facts,
            self.writer._closer(candidate),
        ]
        self.assertEqual(6, len(lines))
        self.assertTrue(any("Sacred Cenote" in line for line in lines))
        self.assertTrue(any("Great Ball Court" in line for line in lines))
        self.assertTrue(any("El Castillo" in line for line in lines))
        self.assertFalse(any(line.rstrip().endswith("visible.") for line in lines))
        self.assertFalse(any(line.rstrip().endswith("elite ceremony, sacrifice.") for line in lines))
        self.assertTrue(all(self.writer._word_count(line) <= 22 for line in lines))
        selected = [
            self.writer._retention_opener(candidate, [], "short"),
            *facts[1:4],
            self.writer._closer(candidate),
        ]
        joined = " ".join(selected).lower()
        self.assertEqual(1, joined.count("maya center"))
        self.assertGreaterEqual(self.writer._word_count(joined), 82)
        # Thin fact packs may still be under the final polished floor; improve() expands them.

    def test_visual_ready_history_openers_are_specific_complete_and_location_anchored(self) -> None:
        expected_location = {
            "Angkor Wat": "cambodia",
            "Tikal": "tikal",
            "Chichen Itza": "chichen itza",
            "Great Zimbabwe": "southern africa",
            "Axum Obelisks": "ethiopia",
            "Olmec Colossal Heads": "olmec",
        }
        for subject, anchor in expected_location.items():
            with self.subTest(subject=subject):
                candidate = topic(
                    niche_id="ancient_history",
                    title=subject,
                    subject=subject,
                    content_kind="short",
                )
                opener = self.writer._retention_opener(candidate, [], "short")
                self.assertIn(anchor, opener.lower())
                self.assertLessEqual(self.writer._word_count(opener), 22)
                self.assertIsNone(self.writer._bad_ai_line_issue(opener))
                self.assertIsNone(self.writer._bad_ai_line_issue(self.writer._closer(candidate)))

    def test_editorial_gate_rejects_coercion_and_false_certainty(self) -> None:
        bad_beats = [
            "You hear one voice change and instantly know what it means.",
            "This one sign proves they want you.",
            "Use it to make them obsessed.",
            "Keep pushing until they react.",
            "Guaranteed attraction is the answer.",
        ]
        issues = self.writer.editorial_quality_issues(topic(), bad_beats, "short")
        self.assertTrue(any("unsafe" in issue for issue in issues))
        self.assertTrue(any("unsupported certainty" in issue for issue in issues))

    def test_title_variants_use_curiosity_without_false_proof(self) -> None:
        titles = [variant.title.lower() for variant in TitleLab().make_variants(topic(), count=4)]
        self.assertTrue(any("cannot prove" in title or "may be" in title for title in titles))
        self.assertFalse(any("reveals voice change attraction" in title for title in titles))


class MusicPathResolutionTests(unittest.TestCase):
    def test_channel_music_dir_uses_channel_folder_when_tracks_exist(self) -> None:
        import tempfile
        from pathlib import Path

        factory = object.__new__(ShortsFactory)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            channel_dir = root / "ancient_history"
            channel_dir.mkdir(parents=True)
            (channel_dir / "empire_echo.wav").write_bytes(b"RIFF")
            factory.config = SimpleNamespace(app=SimpleNamespace(music_dir=root))
            resolved = factory._channel_music_dir(SimpleNamespace(id="ancient_history"))
            self.assertEqual(resolved, channel_dir)

    def test_resolved_music_root_falls_back_to_sibling_when_empty(self) -> None:
        import tempfile
        from pathlib import Path

        factory = object.__new__(ShortsFactory)
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty_music"
            empty.mkdir()
            sibling = Path(tmp) / "yt_automation" / "assets" / "music" / "brain_lens"
            sibling.mkdir(parents=True)
            (sibling / "warm_tension.wav").write_bytes(b"RIFF")
            # Simulate worktree music_dir: <tmp>/yt_automation_scale_v2/assets/music
            worktree_music = Path(tmp) / "yt_automation_scale_v2" / "assets" / "music"
            worktree_music.mkdir(parents=True)
            # Sibling live music at <tmp>/yt_automation/assets/music
            live_music = Path(tmp) / "yt_automation" / "assets" / "music"
            live_channel = live_music / "brain_lens"
            live_channel.mkdir(parents=True, exist_ok=True)
            (live_channel / "warm_tension.wav").write_bytes(b"RIFF")
            factory.config = SimpleNamespace(app=SimpleNamespace(music_dir=worktree_music))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("YT_MUSIC_DIR", None)
                resolved = factory._resolved_music_root()
            self.assertEqual(resolved, live_music)
            self.assertTrue(factory._music_root_has_tracks(resolved))

    def test_failed_topic_requeue_does_not_mark_used(self) -> None:
        import tempfile
        from pathlib import Path

        factory = object.__new__(ShortsFactory)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            factory.config = SimpleNamespace(
                app=SimpleNamespace(state_dir=state, timezone="Asia/Karachi")
            )
            factory.logger = SimpleNamespace(warning=Mock())
            factory._add_used_topic = Mock()
            topic_obj = SimpleNamespace(subject="Tikal", title="Inside Tikal")
            factory._requeue_failed_topic(
                "ancient_history",
                topic_obj,
                reason="caption CPS",
                status="render_failed",
                content_kind="short",
            )
            path = state / "failed_topics.jsonl"
            self.assertTrue(path.exists())
            factory._mark_topic_used_if_ready(
                channel_id="ancient_history",
                topic=topic_obj,
                quality_decision="hold",
                uploaded=False,
                upload_skipped="quality_gate",
            )
            factory._add_used_topic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
