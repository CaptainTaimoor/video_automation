"""A long script must not say the same sentence twice.

The per-category context pools are indexed with a modulo, so a subject whose
facts all fall in one category walks off the end and starts again. The
"writing" pool holds three lines; a Dead Sea Scrolls script drew six from it
and shipped two sentences word for word twice. The script subscore was 100.
"""

from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from yt_auto.config import load_config
from yt_auto.models import ScenePlanItem, TopicCandidate
from yt_auto.script_writer import ScriptWriter

ROOT = Path(__file__).resolve().parents[1]


def writer() -> ScriptWriter:
    return ScriptWriter(load_config(ROOT / "config" / "settings.yaml").app.script_writer)


def channel(channel_id: str = "ancient_history") -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        display_name="Secrets of Time",
        niche_description="ancient history",
        videos=SimpleNamespace(min_duration_seconds=240, max_duration_seconds=360),
        shorts=SimpleNamespace(min_duration_seconds=50, max_duration_seconds=59),
    )


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


class NoRepeatedSentenceTests(unittest.TestCase):
    # Every fact is about texts, so they all sort into the same category and
    # exhaust its pool. This is the shape that produced the repeats.
    ALL_ONE_CATEGORY = [
        "The scrolls were written on parchment and papyrus in Hebrew, Aramaic and Greek.",
        "A copy of the Book of Isaiah is a thousand years older than any previously known.",
        "Scribes corrected their own work between the lines of several manuscripts.",
        "Some texts are commentaries that quote a passage and then interpret it.",
        "One manuscript lists community rules for admission and punishment.",
        "A copper scroll records the locations of buried treasure in a plain register.",
        "Several fragments carry a script style used only for divine names.",
        "The collection includes letters that were never meant to be preserved.",
        "A calendar text divides the year into weeks of fixed length.",
        "Two copies of the same psalm differ in their closing lines.",
        "Ink analysis matched a fragment to a cave it was not catalogued in.",
        "One scroll was wrapped in linen before it was stored in a jar.",
    ]

    def build(self, points):
        # The plan reads its facts out of the middle of an existing scene
        # plan, so the first and last entries are framing the method drops.
        scenes = [ScenePlanItem(narration=text, visual_text=text[:40], search_terms=[])
                  for text in ["Opening frame.", *points, "Closing frame."]]
        topic = TopicCandidate(
            niche_id="ancient_history", style="story", trend_terms=[],
            title="Dead Sea Scrolls", hook="", narration="", visual_captions=[],
            source_urls=[], image_queries=[], hashtags=[], engagement_score=50.0,
            content_kind="video", subject="dead sea scrolls", narration_beats=[],
            title_variants=[], scene_plan=scenes, transcripts=[],
        )
        plan = writer()._history_long_video_plan(channel(), topic, "dead sea scrolls")
        return " ".join(scene.narration for scene in plan)

    def test_no_sentence_appears_twice_when_every_fact_shares_a_category(self):
        narration = self.build(self.ALL_ONE_CATEGORY)
        repeated = [line for line, count in Counter(sentences(narration)).items() if count > 1]
        self.assertEqual(repeated, [], f"repeated verbatim: {repeated[:3]}")

    def test_the_researched_facts_still_reach_the_script(self):
        # The plan trims to the channel's fact budget, so only the facts it
        # keeps are expected -- the point is that de-duplicating contexts did
        # not start dropping the facts themselves.
        needed = writer()._long_facts_needed(channel())
        narration = self.build(self.ALL_ONE_CATEGORY)
        for point in self.ALL_ONE_CATEGORY[:needed]:
            with self.subTest(point=point[:40]):
                self.assertIn(point, narration)

    def test_a_mixed_subject_also_stays_free_of_repeats(self):
        narration = self.build([
            "The city held reservoirs cut into bedrock.",
            "Carved stelae name rulers and record dates.",
            "Burials under the plaza contained jade and shell.",
            "Causeways linked the temple groups across the site.",
            "A siege is recorded on a monument at a rival city.",
            "Trade brought obsidian from highland sources.",
            "Later rebuilding reused earlier facing stones.",
            "Excavation notebooks disagree about one layer.",
            "A ball court marker carries a date in the long count.",
            "Postholes under a plaza floor predate the stone surface.",
            "Pollen cores record forest clearance around the reservoirs.",
            "A cache of eccentric flints was left beneath a stairway.",
        ])
        repeated = [line for line, count in Counter(sentences(narration)).items() if count > 1]
        self.assertEqual(repeated, [], f"repeated verbatim: {repeated[:3]}")

    def test_the_smallest_accepted_fact_list_is_unaffected(self):
        narration = self.build(self.ALL_ONE_CATEGORY[:9])
        repeated = [line for line, count in Counter(sentences(narration)).items() if count > 1]
        self.assertEqual(repeated, [])

    def test_the_pool_written_for_repeated_categories_is_actually_reached(self):
        # It was defined and never referenced, which is why the wraparound
        # had nowhere else to go.
        import inspect

        source = inspect.getsource(ScriptWriter._history_long_video_plan)
        defined = source.count("repeat_category_contexts")
        self.assertGreater(defined, 1, "the repeat pool is still only defined, never used")


class SubjectReadsAsEnglishTests(unittest.TestCase):
    """The subject is dropped straight into prose, so it has to carry its
    article. A finished video said "What survived from Dead Sea Scrolls"."""

    def test_a_common_noun_head_takes_an_article(self):
        for name in ("Dead Sea Scrolls", "Nazca Lines", "Terracotta Army",
                     "Nubian Pyramids", "Pompeii Plaster Casts"):
            with self.subTest(name=name):
                self.assertEqual(ScriptWriter._subject_phrase(name), f"the {name}")

    def test_a_place_or_person_name_takes_none(self):
        for name in ("Tikal", "Carthage", "Petra", "Machu Picchu",
                     "Angkor Wat", "Hatshepsut", "Xerxes", "Axum"):
            with self.subTest(name=name):
                self.assertEqual(ScriptWriter._subject_phrase(name), name)

    def test_an_of_phrase_takes_an_article(self):
        self.assertEqual(
            ScriptWriter._subject_phrase("Code of Hammurabi"), "the Code of Hammurabi"
        )

    def test_a_possessive_already_determines_the_noun(self):
        for name in ("Hadrian's Wall", "Axum's Giant Stelae", "Petra's Rock-Cut City"):
            with self.subTest(name=name):
                self.assertEqual(ScriptWriter._subject_phrase(name), name)

    def test_an_article_is_never_doubled(self):
        self.assertEqual(ScriptWriter._subject_phrase("the Indus Valley"), "the Indus Valley")

    def test_an_empty_subject_does_not_become_a_bare_the(self):
        self.assertEqual(ScriptWriter._subject_phrase(""), "")
        self.assertEqual(ScriptWriter._subject_phrase("   "), "")

    def test_the_narration_uses_the_article_form(self):
        narration = NoRepeatedSentenceTests().build(NoRepeatedSentenceTests.ALL_ONE_CATEGORY)
        self.assertIn("the dead sea scrolls", narration.lower())
        self.assertNotIn("from dead sea scrolls", narration.lower())

    def test_the_opening_no_longer_puts_people_inside_a_document(self):
        narration = NoRepeatedSentenceTests().build(NoRepeatedSentenceTests.ALL_ONE_CATEGORY)
        self.assertNotIn("the people inside", narration.lower())


if __name__ == "__main__":
    unittest.main()
