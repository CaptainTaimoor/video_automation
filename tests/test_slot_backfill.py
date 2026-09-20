"""Today's failed and held slots get rebuilt while the encoder is idle.

A slot used to give up after three attempts and a quality-held video was never
remade, so the day ended with red and dotted slots while nothing was being
built. New work still comes first.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from yt_auto import dashboard_views as views
from yt_auto import ready_queue as rq

TODAY = "2026-09-20"


def ago(minutes: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


def queue_with(state_dir: Path, *items: dict) -> None:
    base = {
        "channel": "brain_lens",
        "content_kind": "short",
        "target_date": TODAY,
        "priority": 0,
        "force_flags": {},
        "created_at": "2026-09-20T00:05:00",
    }
    rq.save_queue(
        state_dir,
        "brain_lens",
        {"channel": "brain_lens", "items": [dict(base, **item) for item in items]},
    )


class BackfillTests(unittest.TestCase):
    def test_a_given_up_slot_is_rebuilt_when_nothing_else_is_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue_with(
                state,
                {"id": "failed_out", "status": "failed", "attempts": 3, "target_slot": "09:20", "updated_at": ago(45)},
                {"id": "held", "status": "held_quality", "attempts": 1, "target_slot": "07:20", "updated_at": ago(45)},
                {"id": "done", "status": "uploaded", "attempts": 1, "target_slot": "06:20", "updated_at": ago(45)},
            )
            picked = rq._pick_backfill(state, ["brain_lens"], today=TODAY)
            self.assertEqual((picked or {}).get("id"), "held", "the earliest owed slot goes first")

    def test_a_quality_hold_is_remade_not_just_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue_with(state, {"id": "held", "status": "held_quality", "attempts": 1, "target_slot": "08:00", "updated_at": ago(45)})
            self.assertEqual((rq._pick_backfill(state, ["brain_lens"], today=TODAY) or {}).get("id"), "held")

    def test_a_retry_waits_for_its_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue_with(state, {"id": "just_failed", "status": "failed", "attempts": 2, "target_slot": "09:20", "updated_at": ago(5)})
            self.assertIsNone(rq._pick_backfill(state, ["brain_lens"], today=TODAY))

    def test_a_stubborn_slot_stops_at_the_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue_with(
                state,
                {"id": "worn_out", "status": "failed", "attempts": rq.BACKFILL_MAX_ATTEMPTS, "target_slot": "08:20", "updated_at": ago(90)},
            )
            self.assertIsNone(rq._pick_backfill(state, ["brain_lens"], today=TODAY))

    def test_yesterdays_failures_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue_with(state, {"id": "old", "status": "failed", "attempts": 1, "target_slot": "09:20", "target_date": "2026-09-19", "updated_at": ago(600)})
            self.assertIsNone(rq._pick_backfill(state, ["brain_lens"], today=TODAY))

    def test_an_unreadable_timestamp_does_not_block_the_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            queue_with(state, {"id": "odd", "status": "failed", "attempts": 1, "target_slot": "09:20", "updated_at": "not-a-date"})
            self.assertEqual((rq._pick_backfill(state, ["brain_lens"], today=TODAY) or {}).get("id"), "odd")


class SlotStateTests(unittest.TestCase):
    def test_an_upload_whose_hour_has_passed_reads_as_published(self):
        self.assertEqual(views.slot_state({"status": "uploaded"}, 600, 700), "published")

    def test_an_upload_waiting_for_its_hour_reads_as_scheduled(self):
        self.assertEqual(views.slot_state({"status": "uploaded"}, 900, 700), "scheduled")

    def test_an_empty_slot_is_owed(self):
        self.assertEqual(views.slot_state(None, 600, 700), "owed")

    def test_every_build_status_maps_to_a_known_state(self):
        for status in ("rendered", "queued_upload", "scripted", "assets_ready", "rendering", "held_quality", "failed"):
            with self.subTest(status=status):
                self.assertIn(views.slot_state({"status": status}, 600, 700), views.SLOT_STATES)

    def test_a_passed_hour_still_owed_means_not_on_track(self):
        self.assertFalse(views.on_track(["owed"], ["09:00"], 700))
        self.assertTrue(views.on_track(["owed"], ["21:00"], 700))

    def test_a_bad_slot_string_does_not_crash(self):
        self.assertEqual(views.minutes_of("nonsense"), -1)


class RetryAnnotationTests(unittest.TestCase):
    """A problem card says what happens next, so "Retry all" never looks dead."""

    def setUp(self):
        self.now = datetime(2026, 9, 20, 19, 0)
        self.items = [
            {"id": "soon", "status": "failed", "attempts": 3, "target_date": TODAY,
             "updated_at": (self.now - timedelta(minutes=5)).isoformat(), "error": "weak script"},
            {"id": "worn", "status": "failed", "attempts": 8, "target_date": TODAY,
             "updated_at": (self.now - timedelta(minutes=50)).isoformat(), "error": "worker exited"},
            {"id": "held", "status": "held_quality", "attempts": 1, "target_date": TODAY, "updated_at": self.now.isoformat()},
            {"id": "old", "status": "failed", "attempts": 1, "target_date": "2026-09-19", "updated_at": self.now.isoformat()},
            {"id": "fine", "status": "uploaded", "attempts": 1, "target_date": TODAY},
        ]
        self.policy = views.annotate_retries(self.items, TODAY, now=self.now)
        self.by_id = {item["id"]: item for item in self.items}

    def test_the_policy_is_reported_to_the_page(self):
        self.assertEqual(self.policy, {"max_tries": 8, "cooldown_minutes": 20})

    def test_a_recent_failure_says_when_it_retries(self):
        row = self.by_id["soon"]
        self.assertEqual(row["retry_state"], "auto")
        self.assertEqual(row["next_try_in_minutes"], 15)
        self.assertEqual(row["last_tried_minutes_ago"], 5)

    def test_an_exhausted_row_says_it_gave_up(self):
        self.assertEqual(self.by_id["worn"]["retry_state"], "gave_up")

    def test_a_quality_hold_is_retried_too(self):
        self.assertEqual(self.by_id["held"]["retry_state"], "auto")

    def test_a_leftover_from_another_day_is_marked_as_such(self):
        self.assertEqual(self.by_id["old"]["retry_state"], "past_day")

    def test_finished_work_is_left_untouched(self):
        self.assertNotIn("retry_state", self.by_id["fine"])


if __name__ == "__main__":
    unittest.main()
