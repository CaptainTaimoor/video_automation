"""The Today page's dots, and reading past Pexels' first page."""

from __future__ import annotations

import unittest

from yt_auto import api_keys
from yt_auto.dashboard_views import today_payload
from yt_auto.images import HybridMediaFetcher

TODAY = "2026-09-20"
NAMES = {"ancient_history": "Secrets of Time", "brain_lens": "Brain Lens"}


def item(channel, kind, slot, status, **extra):
    return {
        "channel": channel,
        "content_kind": kind,
        "target_slot": slot,
        "target_date": TODAY,
        "status": status,
        **extra,
    }


class TodayDotTests(unittest.TestCase):
    def setUp(self):
        self.queue = {
            "today": TODAY,
            "items": [
                item("brain_lens", "short", "09:20", "uploaded"),
                item("brain_lens", "short", "13:20", "rendered"),
                item("brain_lens", "short", "21:20", "failed"),
                item("brain_lens", "video", "21:00", "planned"),
                item("ancient_history", "short", "07:10", "uploaded"),
            ],
        }
        self.out = today_payload(self.queue, NAMES, now_minutes=12 * 60)
        self.by_id = {c["id"]: c for c in self.out["channels"]}

    def test_every_channel_gets_a_card(self):
        self.assertEqual(set(self.by_id), set(NAMES))

    def test_a_past_hour_reads_as_published_and_a_future_one_as_scheduled(self):
        states = {s["time"]: s["state"] for s in self.by_id["brain_lens"]["kinds"]["short"]["slots"]}
        self.assertEqual(states["09:20"], "published")
        self.assertEqual(states["13:20"], "ready")
        self.assertEqual(states["21:20"], "failed")

    def test_a_missed_hour_means_the_channel_is_behind(self):
        behind = today_payload(
            {"today": TODAY, "items": [item("brain_lens", "short", "07:00", "failed")]},
            {"brain_lens": "Brain Lens"},
            now_minutes=12 * 60,
        )
        self.assertFalse(behind["channels"][0]["on_track"])

    def test_a_future_slot_does_not_make_the_channel_behind(self):
        ahead = today_payload(
            {"today": TODAY, "items": [item("brain_lens", "short", "21:00", "planned")]},
            {"brain_lens": "Brain Lens"},
            now_minutes=12 * 60,
        )
        self.assertTrue(ahead["channels"][0]["on_track"])

    def test_the_drawn_plan_wins_over_what_the_queue_happens_to_hold(self):
        # The page once showed the configured times and claimed 7 Shorts on a
        # day that planned 9.
        out = today_payload(
            self.queue,
            {"brain_lens": "Brain Lens"},
            now_minutes=12 * 60,
            planned_slots={("brain_lens", "short"): ["09:20", "13:20", "21:20", "23:20"]},
        )
        self.assertEqual(out["channels"][0]["kinds"]["short"]["planned"], 4)

    def test_an_unfilled_planned_slot_shows_as_owed(self):
        out = today_payload(
            self.queue,
            {"brain_lens": "Brain Lens"},
            now_minutes=12 * 60,
            planned_slots={("brain_lens", "short"): ["09:20", "23:20"]},
        )
        states = {s["time"]: s["state"] for s in out["channels"][0]["kinds"]["short"]["slots"]}
        self.assertEqual(states["23:20"], "owed")

    def test_next_up_lists_only_slots_still_to_come(self):
        upcoming = self.by_id["brain_lens"]["next"]
        self.assertTrue(all(s["time"] >= "12:00" for s in upcoming))

    def test_an_empty_day_does_not_crash(self):
        out = today_payload({"today": TODAY, "items": []}, NAMES, now_minutes=600)
        self.assertTrue(out["ok"])
        self.assertEqual(out["channels"][0]["planned"], 0)


class StockPagingTests(unittest.TestCase):
    """Only page one was read, and page one is the most-used clips."""

    def setUp(self):
        self.fetcher = HybridMediaFetcher.__new__(HybridMediaFetcher)

    def test_a_small_ask_is_still_one_request(self):
        self.assertEqual(self.fetcher._stock_pages(4), [(1, 4)])

    def test_a_large_ask_reads_several_pages(self):
        pages = self.fetcher._stock_pages(40)
        self.assertGreater(len(pages), 1)
        self.assertEqual([p for p, _ in pages], list(range(1, len(pages) + 1)))

    def test_paging_is_capped_so_a_scene_cannot_stall(self):
        self.assertLessEqual(len(self.fetcher._stock_pages(500)), HybridMediaFetcher.STOCK_SEARCH_PAGES)

    def test_a_bigger_ask_never_sees_fewer_candidates(self):
        seen = [sum(per for _, per in self.fetcher._stock_pages(n)) for n in (4, 10, 20, 45)]
        self.assertEqual(seen, sorted(seen))

    def test_zero_is_treated_as_one(self):
        self.assertEqual(self.fetcher._stock_pages(0), [(1, 1)])


class ProviderCardTests(unittest.TestCase):
    def test_every_known_provider_gets_a_card_even_with_no_key(self):
        view = api_keys.KeyStore().public_view()
        self.assertEqual([e["provider"] for e in view["providers"]], list(api_keys.PROVIDER_ORDER))

    def test_each_card_explains_what_the_provider_is_for(self):
        for entry in api_keys.KeyStore().public_view()["providers"]:
            with self.subTest(provider=entry["provider"]):
                self.assertTrue(entry["note"])

    def test_the_rotation_still_only_uses_providers_that_have_keys(self):
        store = api_keys.KeyStore()
        store.add("gemini", "g-1")
        self.assertEqual(store.ordered_providers(), ["gemini"])


if __name__ == "__main__":
    unittest.main()
