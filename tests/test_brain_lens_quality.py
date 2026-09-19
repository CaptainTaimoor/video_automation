"""The Brain Lens rules, the gate, and the composer that answers to them.

The bug these guard against: the writer and the gate each kept their own copy
of the rules, so a hand-written line could be composed and then rejected for a
rule the composer never applied. The invariant tests below fail the moment the
shipped lines and the shared rule set drift apart again.
"""

from __future__ import annotations

import random
import unittest

from yt_auto import brain_hooks, pipeline_quality, short_composer


class SharedRuleInvariantTests(unittest.TestCase):
    """Everything shipped must satisfy the rule everything is judged by."""

    def test_every_shipped_opener_passes_the_shared_rule(self):
        for opener in brain_hooks.OPENERS:
            with self.subTest(opener=opener):
                self.assertEqual(brain_hooks.opener_issues(opener), [])

    def test_every_topic_opener_passes_the_shared_rule(self):
        for _, opener in brain_hooks.TOPIC_OPENERS:
            with self.subTest(opener=opener):
                self.assertEqual(brain_hooks.opener_issues(opener), [])

    def test_every_fact_carries_a_midpoint_turn(self):
        for fact in brain_hooks.RELATIONSHIP_FACTS:
            with self.subTest(fact=fact):
                self.assertTrue(pipeline_quality.has_midpoint_turn(["hook", fact, "close"]))


class OpenerRuleTests(unittest.TestCase):
    def test_topic_statement_is_rejected(self):
        issues = brain_hooks.opener_issues("To understand attachment you need context.")
        self.assertIn("opener states the topic instead of showing a moment", issues)

    def test_too_short_is_named_with_the_count(self):
        self.assertTrue(any("too short" in i for i in brain_hooks.opener_issues("You wait.")))

    def test_definition_phrasing_is_rejected(self):
        issues = brain_hooks.opener_issues(
            "Cognitive dissonance represents an enduring structural tension in belief systems."
        )
        self.assertIn("opener defines the topic instead of showing it", issues)

    def test_a_concrete_line_without_a_pronoun_is_still_accepted(self):
        # Guards the false-rejection bug: these lines are fine and must not be
        # thrown out just because no person is named in them.
        for line in (
            "One late-night reply can feel more intimate than an actual conversation.",
            "One delayed text turns a calm evening into a search for reassurance.",
        ):
            with self.subTest(line=line):
                self.assertEqual(brain_hooks.opener_issues(line), [])

    def test_empty_opener_is_rejected(self):
        self.assertEqual(brain_hooks.opener_issues(""), ["opener is empty"])


class JoinedOpenerTests(unittest.TestCase):
    def test_two_sentences_become_one_so_the_cue_survives_the_split(self):
        self.assertEqual(
            brain_hooks.joined_opener("You felt the chemistry. One silence made your stomach drop."),
            "You felt the chemistry, and one silence made your stomach drop.",
        )

    def test_single_sentence_is_left_alone(self):
        line = "They text one dry word, and suddenly you want them more."
        self.assertEqual(brain_hooks.joined_opener(line), line)

    def test_blank_is_safe(self):
        self.assertEqual(brain_hooks.joined_opener(""), "")


class GateTests(unittest.TestCase):
    def test_midpoint_turn_is_detected(self):
        self.assertTrue(pipeline_quality.has_midpoint_turn(["a", "x but y", "c"]))
        self.assertFalse(pipeline_quality.has_midpoint_turn(["a", "x y z", "c"]))

    def test_thin_script_is_named(self):
        issues = pipeline_quality.short_length_issues(["one two three"])
        self.assertTrue(any("too thin" in i for i in issues))

    def test_empty_script_is_rejected(self):
        self.assertEqual(pipeline_quality.brain_lens_issues([]), ["script is empty"])

    def test_estimated_seconds_tracks_word_count(self):
        short = pipeline_quality.estimated_seconds(["one two three four five six"])
        longer = pipeline_quality.estimated_seconds(["one two three four five six seven eight nine ten"])
        self.assertLess(short, longer)

    def test_ancient_history_hook_needs_subject_or_evidence(self):
        issues = pipeline_quality.ancient_history_issues(
            ["Something vague happened.", "and then more.", "the end."], {"concrete"}
        )
        self.assertIn("Ancient History hook lacks its subject or a concrete surviving clue", issues)

    def test_ancient_history_accepts_a_physical_clue(self):
        issues = pipeline_quality.ancient_history_issues(
            [
                "The inscription on the wall names a date.",
                "That date contradicts the legend, because the ruler was already dead.",
                "The concrete record settles it.",
            ],
            {"concrete"},
        )
        self.assertNotIn("Ancient History hook lacks its subject or a concrete surviving clue", issues)


class ComposerTests(unittest.TestCase):
    def test_composed_shorts_always_satisfy_the_gate(self):
        for seed in range(150):
            with self.subTest(seed=seed):
                lines = short_composer.compose("almost relationships", rng=random.Random(seed))
                self.assertEqual(pipeline_quality.brain_lens_issues(lines), [])

    def test_subject_keyed_opener_wins_when_it_matches(self):
        opener = short_composer.pick_opener("dopamine", rng=random.Random(0))
        self.assertIn("reward", opener)

    def test_repair_replaces_only_the_beat_that_was_wrong(self):
        lines = ["To understand this you need context.", "a because b", "closer"]
        fixed = short_composer.repair(lines, ["opener states the topic"], "attachment", rng=random.Random(1))
        self.assertNotEqual(fixed[0], lines[0])
        self.assertEqual(fixed[1:], lines[1:])

    def test_repair_does_not_repeat_a_beat_already_used(self):
        fact = brain_hooks.RELATIONSHIP_FACTS[0]
        fixed = short_composer.repair(
            ["hook", fact, "closer"], ["midpoint"], "attachment", rng=random.Random(3)
        )
        self.assertNotEqual(fixed[1], fact)

    def test_composed_short_is_a_plausible_length(self):
        lines = short_composer.compose("attachment", rng=random.Random(11))
        self.assertGreaterEqual(pipeline_quality.estimated_seconds(lines), 8)


if __name__ == "__main__":
    unittest.main()
