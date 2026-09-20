"""A backup topic is not republished until its cooldown has passed.

Recovery draws from a short fixed list and rewrites the title for the same
curated facts. Checking the exact title therefore let the same subject ship
again under a new name -- Pompeii went out twelve times.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from yt_auto.pipeline import ShortsFactory


def factory_with(runs: list[dict]) -> ShortsFactory:
    factory = ShortsFactory.__new__(ShortsFactory)
    factory._read_run_log = Mock(return_value=runs)
    return factory


def run(subject: str, day: str, *, channel: str = "ancient_history", uploaded: bool = True) -> dict:
    return {
        "channel": channel,
        "subject": subject,
        "uploaded": uploaded,
        "youtube_id": "abc123" if uploaded else "",
        "run_dir": f"output/{channel}/short/{day}/{day.replace('-', '')}_120000",
    }


TODAY = "2026-09-20"


class RecentSubjectTests(unittest.TestCase):
    def test_a_subject_published_today_is_on_cooldown(self):
        f = factory_with([run("pompeii plaster casts", TODAY)])
        self.assertIn("pompeii plaster casts", f._recently_published_subjects("ancient_history", today=TODAY))

    def test_a_subject_published_long_ago_is_free_again(self):
        f = factory_with([run("pompeii plaster casts", "2026-01-01")])
        self.assertEqual(f._recently_published_subjects("ancient_history", today=TODAY), set())

    def test_the_boundary_is_the_configured_window(self):
        f = factory_with([run("rosetta stone", "2026-09-05")])
        self.assertIn("rosetta stone", f._recently_published_subjects("ancient_history", days=30, today=TODAY))
        self.assertEqual(f._recently_published_subjects("ancient_history", days=5, today=TODAY), set())

    def test_a_video_that_never_uploaded_does_not_block_its_subject(self):
        f = factory_with([run("gobekli tepe", TODAY, uploaded=False)])
        self.assertEqual(f._recently_published_subjects("ancient_history", today=TODAY), set())

    def test_another_channel_does_not_block_this_one(self):
        f = factory_with([run("pompeii plaster casts", TODAY, channel="brain_lens")])
        self.assertEqual(f._recently_published_subjects("ancient_history", today=TODAY), set())

    def test_subjects_are_compared_loosely_enough_to_match(self):
        f = factory_with([run("Pompeii  Plaster-Casts", TODAY)])
        self.assertIn("pompeii plaster casts", f._recently_published_subjects("ancient_history", today=TODAY))

    def test_a_run_with_no_usable_folder_is_still_counted(self):
        # Better to hold a subject back than to republish it on a parse slip.
        f = factory_with([{"channel": "ancient_history", "subject": "tikal", "uploaded": True, "run_dir": ""}])
        self.assertIn("tikal", f._recently_published_subjects("ancient_history", today=TODAY))

    def test_an_empty_log_blocks_nothing(self):
        self.assertEqual(factory_with([])._recently_published_subjects("ancient_history", today=TODAY), set())


class NormaliseTests(unittest.TestCase):
    def test_punctuation_and_case_do_not_make_two_subjects(self):
        self.assertEqual(
            ShortsFactory._normalise_subject("Rosetta Stone!"),
            ShortsFactory._normalise_subject("rosetta   stone"),
        )

    def test_a_missing_subject_is_empty_not_an_error(self):
        self.assertEqual(ShortsFactory._normalise_subject(None), "")

    def test_the_day_is_read_from_the_run_folder(self):
        self.assertEqual(
            ShortsFactory._run_log_day({"run_dir": "output/ancient_history/short/2026-09-20/20260920_000540"}),
            "2026-09-20",
        )
        self.assertEqual(ShortsFactory._run_log_day({"run_dir": "nonsense"}), "")


if __name__ == "__main__":
    unittest.main()
