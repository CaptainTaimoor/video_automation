"""Tests for the queue page's view models."""

from __future__ import annotations

import unittest

from yt_auto import dashboard_views as views


def _item(**kwargs):
    base = {"id": "rq_1", "channel": "brain_lens", "status": "planned", "attempts": 0}
    base.update(kwargs)
    return base


class PlainCauseTests(unittest.TestCase):
    def test_script_rejection_reads_as_one_plain_sentence(self):
        self.assertEqual(
            views.plain_cause("Rejected weak script: caption 3 ends on an incomplete phrase"),
            "Could not write a script that passed the checks",
        )

    def test_visual_shortfall_is_named_separately(self):
        self.assertEqual(views.plain_cause("not enough usable visuals"), "Not enough usable pictures")

    def test_blank_error_still_says_something(self):
        self.assertEqual(views.plain_cause(None), "Stopped without saying why")

    def test_unknown_error_falls_through(self):
        self.assertEqual(views.plain_cause("segfault in frobnicator"), "Something else went wrong")


class NextStepTests(unittest.TestCase):
    def test_healthy_rows_promise_nothing(self):
        self.assertEqual(views.next_step(_item(status="rendered")), "")

    def test_failed_row_names_the_wait_and_the_tries(self):
        text = views.next_step(_item(status="failed", attempts=3))
        self.assertIn("Retrying by itself in about", text)
        self.assertIn("tried 3×", text)

    def test_backoff_is_capped_like_the_scheduler(self):
        self.assertEqual(views.retry_wait_minutes(1), 15)
        self.assertEqual(views.retry_wait_minutes(2), 30)
        self.assertEqual(views.retry_wait_minutes(9), 30)

    def test_exhausted_row_says_it_gave_up(self):
        self.assertEqual(
            views.next_step(_item(status="failed", attempts=8)),
            "Gave up for today after 8 tries",
        )


class QueueTabTests(unittest.TestCase):
    def setUp(self):
        self.queue = {
            "today": "2026-09-19",
            "items": [
                _item(id="a", status="failed", error="weak script", attempts=2),
                _item(id="b", status="held_quality", error="weak script", attempts=1),
                _item(id="c", status="failed", error="not enough usable visuals", attempts=1),
                _item(id="d", status="rendering"),
                _item(id="e", status="rendered"),
                _item(id="f", status="planned"),
                _item(id="g", status="uploaded", updated_at="2026-09-19T10:00:00"),
                _item(id="h", status="uploaded", updated_at="2026-09-18T10:00:00"),
            ],
        }
        self.tabs = {tab["key"]: tab for tab in views.queue_tabs(self.queue)}

    def test_every_tab_is_present_and_counted(self):
        self.assertEqual(self.tabs["problems"]["count"], 3)
        self.assertEqual(self.tabs["building"]["count"], 1)
        self.assertEqual(self.tabs["ready"]["count"], 1)
        self.assertEqual(self.tabs["planned"]["count"], 1)

    def test_sent_today_excludes_yesterday(self):
        self.assertEqual([i["id"] for i in self.tabs["sent"]["items"]], ["g"])

    def test_problems_group_by_cause_worst_first(self):
        groups = self.tabs["problems"]["groups"]
        self.assertEqual(groups[0]["cause"], "Could not write a script that passed the checks")
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[1]["cause"], "Not enough usable pictures")

    def test_only_problem_tabs_get_groups(self):
        self.assertEqual(self.tabs["ready"]["groups"], [])

    def test_rows_carry_plain_status_and_next_step(self):
        row = self.tabs["problems"]["items"][0]
        self.assertTrue(row["plain_status"])
        self.assertTrue(row["next_step"])
        self.assertTrue(row["cause"])

    def test_decorate_leaves_the_original_row_untouched(self):
        original = _item(status="failed", error="weak script")
        views.decorate(original)
        self.assertNotIn("plain_status", original)


if __name__ == "__main__":
    unittest.main()
