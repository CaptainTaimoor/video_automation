"""A subject must not be covered again just because nobody listed it.

Ancient History has 77 published videos and 13 titles shared by more than one
of them. "Inside Tikal: Maya Kings, Reservoirs, and Jungle Temples" is live
five times. Every one of those subjects was missing from the hand-written
angle table; the two that are in it, petra and nazca, were correctly refused
in the same build.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from yt_auto.pipeline import ShortsFactory


def topic(subject: str, title: str = "A title") -> SimpleNamespace:
    return SimpleNamespace(title=title, subject=subject, content_kind="short")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.factory = ShortsFactory.__new__(ShortsFactory)
        self.runs: list[dict] = []
        self.factory._read_run_log = lambda: self.runs

    def channel(self, channel_id="ancient_history"):
        return SimpleNamespace(id=channel_id)

    def log(self, channel_id, subject, times):
        for _ in range(times):
            self.runs.append({"channel": channel_id, "subject": subject, "title": subject})

    def test_an_unlisted_subject_is_refused_once_it_repeats(self):
        self.log("ancient_history", "tikal", 1)
        issue = self.factory._recent_angle_issue(self.channel(), topic("tikal"))
        self.assertIsNotNone(issue)
        self.assertIn("tikal", issue)

    def test_a_subject_seen_five_times_says_how_many(self):
        self.log("ancient_history", "pompeii plaster casts", 5)
        issue = self.factory._recent_angle_issue(self.channel(), topic("pompeii plaster casts"))
        self.assertIn("5 times", issue)

    def test_a_fresh_subject_is_allowed(self):
        self.log("ancient_history", "tikal", 4)
        self.assertIsNone(
            self.factory._recent_angle_issue(self.channel(), topic("hanging gardens of babylon"))
        )

    def test_another_channels_runs_do_not_count(self):
        self.log("brain_lens", "tikal", 6)
        self.assertIsNone(self.factory._recent_angle_issue(self.channel(), topic("tikal")))

    def test_brain_lens_allows_one_repeat_before_refusing(self):
        channel = self.channel("brain_lens")
        self.log("brain_lens", "social media jealousy", 1)
        self.assertIsNone(
            self.factory._recent_angle_issue(channel, topic("social media jealousy"))
        )
        self.log("brain_lens", "social media jealousy", 1)
        self.assertIsNotNone(
            self.factory._recent_angle_issue(channel, topic("social media jealousy"))
        )

    def test_punctuation_and_case_do_not_hide_a_repeat(self):
        self.log("ancient_history", "Axum's Obelisks", 1)
        self.assertIsNotNone(
            self.factory._recent_angle_issue(self.channel(), topic("axum s obelisks"))
        )

    def test_a_curated_entry_keeps_its_own_threshold(self):
        # petra is in the table at 1, and the generic check must not
        # double-count or shadow it.
        self.log("ancient_history", "petra", 1)
        issue = self.factory._recent_angle_issue(self.channel(), topic("petra", "Petra's city"))
        self.assertEqual(issue, "overused recent angle: petra")

    def test_a_missing_subject_is_not_treated_as_a_repeat(self):
        self.runs.append({"channel": "ancient_history", "subject": "", "title": "x"})
        self.assertIsNone(self.factory._recent_angle_issue(self.channel(), topic("")))

    def test_a_very_short_subject_is_left_alone(self):
        # Two or three characters match too much to be a safe key.
        self.log("ancient_history", "ur", 5)
        self.assertIsNone(self.factory._recent_angle_issue(self.channel(), topic("ur")))

    def test_only_the_recent_window_counts(self):
        self.log("ancient_history", "tikal", 1)
        self.log("ancient_history", "something else", 40)
        self.assertIsNone(self.factory._recent_angle_issue(self.channel(), topic("tikal")))


if __name__ == "__main__":
    unittest.main()
