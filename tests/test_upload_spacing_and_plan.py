"""Daily upload plans, and keeping a channel's videos apart on the clock.

Two videos once went live 24 seconds apart, and the day's plan stopped at the
minimum so the upper half of every range was never used. These lock both.
"""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from yt_auto import publish_spacing, upload_plan


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 20, hour, minute)


class SpacingTests(unittest.TestCase):
    def test_a_clear_time_is_left_where_it_is(self):
        self.assertEqual(
            publish_spacing.next_free_slot(at(17, 0), [at(12, 0)]),
            at(17, 0),
        )

    def test_a_video_too_close_is_pushed_later(self):
        placed = publish_spacing.next_free_slot(at(17, 10), [at(17, 0)])
        self.assertGreaterEqual((placed - at(17, 0)).total_seconds() / 60, 30)

    def test_a_video_is_never_moved_earlier(self):
        for desired in (at(9, 0), at(17, 5), at(23, 45)):
            with self.subTest(desired=desired):
                placed = publish_spacing.next_free_slot(desired, [at(17, 0), at(17, 30)])
                self.assertGreaterEqual(placed, desired)

    def test_the_exact_collision_that_caused_this(self):
        # Two uploads asked for the same minute; the second must move.
        first = publish_spacing.next_free_slot(at(21, 0), [])
        second = publish_spacing.next_free_slot(at(21, 0), [first])
        self.assertNotEqual(first, second)
        self.assertGreaterEqual((second - first).total_seconds() / 60, 30)

    def test_is_clear_reads_both_directions(self):
        self.assertFalse(publish_spacing.is_clear(at(17, 20), [at(17, 0)]))
        self.assertFalse(publish_spacing.is_clear(at(16, 40), [at(17, 0)]))
        self.assertTrue(publish_spacing.is_clear(at(17, 30), [at(17, 0)]))

    def test_a_whole_list_is_spread_out_in_order(self):
        placed = publish_spacing.space_out([at(21, 0), at(21, 0), at(21, 10)])
        self.assertEqual(placed, sorted(placed))
        for earlier, later in zip(placed, placed[1:]):
            self.assertGreaterEqual((later - earlier).total_seconds() / 60, 30)

    def test_minutes_until_clear_reports_the_shift(self):
        self.assertEqual(publish_spacing.minutes_until_clear(at(17, 0), []), 0)
        self.assertGreater(publish_spacing.minutes_until_clear(at(17, 5), [at(17, 0)]), 0)

    def test_a_congested_day_delays_rather_than_drops(self):
        taken = [at(0, 0) + timedelta(minutes=30 * i) for i in range(48)]
        placed = publish_spacing.next_free_slot(at(12, 0), taken, max_shift_hours=2)
        self.assertIsInstance(placed, datetime)


class DailyCountTests(unittest.TestCase):
    def test_the_count_stays_inside_the_range(self):
        rng = random.Random(0)
        for _ in range(200):
            self.assertIn(upload_plan.draw_count(6, 8, rng=rng), {6, 7, 8})

    def test_the_range_is_actually_used_not_just_its_floor(self):
        rng = random.Random(1)
        drawn = {upload_plan.draw_count(10, 12, rng=rng) for _ in range(200)}
        self.assertEqual(drawn, {10, 11, 12})

    def test_a_reversed_range_still_works(self):
        self.assertIn(upload_plan.draw_count(8, 6, rng=random.Random(2)), {6, 7, 8})


class BestHoursTests(unittest.TestCase):
    def test_a_single_lucky_hour_does_not_beat_a_steady_one(self):
        samples = {1: [100.0], 21: [80.0, 82.0, 79.0, 81.0]}
        self.assertEqual(upload_plan.best_hours(samples)[0], 21)

    def test_the_quiet_window_is_never_chosen(self):
        samples = {hour: [50.0] for hour in range(24)}
        for hour in upload_plan.best_hours(samples, limit=24):
            self.assertFalse(upload_plan.is_quiet_hour(hour))

    def test_an_unrated_hour_is_judged_by_its_neighbours(self):
        filled = upload_plan.fill_unrated({10: [1.0] and 10.0, 12: 20.0})
        self.assertIn(11, filled)
        self.assertEqual(filled[11], 15.0)

    def test_no_data_gives_no_hours(self):
        self.assertEqual(upload_plan.best_hours({}), [])


class PlanSlotTests(unittest.TestCase):
    def test_shorts_are_at_least_an_hour_apart(self):
        slots = upload_plan.plan_slots(10, list(range(6, 24)), content_kind="short", rng=random.Random(3))
        minutes = sorted(upload_plan.MIN_GAP_MINUTES and _mins(s) for s in slots)
        for earlier, later in zip(minutes, minutes[1:]):
            self.assertGreaterEqual(later - earlier, 60)

    def test_long_videos_are_at_least_two_hours_apart(self):
        slots = upload_plan.plan_slots(6, list(range(6, 24)), content_kind="video", rng=random.Random(4))
        minutes = sorted(_mins(s) for s in slots)
        for earlier, later in zip(minutes, minutes[1:]):
            self.assertGreaterEqual(later - earlier, 120)

    def test_the_whole_count_is_planned_even_on_few_good_hours(self):
        slots = upload_plan.plan_slots(8, [21, 19], content_kind="short", rng=random.Random(5))
        self.assertEqual(len(slots), 8)

    def test_nothing_lands_in_the_quiet_window(self):
        slots = upload_plan.plan_slots(12, list(range(24)), content_kind="short", rng=random.Random(6))
        for slot in slots:
            self.assertFalse(upload_plan.is_quiet_hour(_mins(slot) // 60), slot)

    def test_slots_come_back_sorted_and_well_formed(self):
        slots = upload_plan.plan_slots(6, [21, 19, 17, 20], content_kind="short", rng=random.Random(7))
        self.assertEqual(slots, sorted(slots))
        for slot in slots:
            self.assertRegex(slot, r"^\d{2}:\d{2}$")

    def test_build_plan_draws_and_places_in_one_call(self):
        slots = upload_plan.build_plan(low=6, high=8, hours=[21, 19, 17], content_kind="video", rng=random.Random(8))
        self.assertGreaterEqual(len(slots), 6)
        self.assertLessEqual(len(slots), 8)


class SavedPlanTests(unittest.TestCase):
    def test_a_saved_plan_is_what_the_page_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            upload_plan.save_plan(state, "brain_lens", "2026-09-20", {"short": ["09:20", "13:20"], "video": ["21:00"]})
            self.assertEqual(upload_plan.saved_slots(state, "brain_lens", "short", "2026-09-20"), ["09:20", "13:20"])
            self.assertEqual(upload_plan.saved_slots(state, "brain_lens", "video", "2026-09-20"), ["21:00"])

    def test_no_plan_reads_as_none_not_an_empty_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(upload_plan.saved_slots(Path(tmp), "brain_lens", "short", "2026-09-20"))

    def test_a_corrupt_plan_does_not_crash_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            upload_plan.plan_path(state, "brain_lens", "2026-09-20").write_text("{not json", encoding="utf-8")
            self.assertIsNone(upload_plan.saved_slots(state, "brain_lens", "short", "2026-09-20"))


def _mins(slot: str) -> int:
    hour, minute = slot.split(":")
    return int(hour) * 60 + int(minute)


if __name__ == "__main__":
    unittest.main()
