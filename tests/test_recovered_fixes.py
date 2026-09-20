"""Fixes for failures that each cost a day of uploads on the old machine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yt_auto import ready_queue as rq
from yt_auto.research import _HEADLINE_FILLER
from yt_auto.script_writer import ScriptWriter


def channel(min_seconds: int) -> SimpleNamespace:
    return SimpleNamespace(
        videos=SimpleNamespace(min_duration_seconds=min_seconds),
        shorts=SimpleNamespace(min_duration_seconds=50, max_duration_seconds=59),
    )


class LongVideoBudgetTests(unittest.TestCase):
    """A 1250-word floor meant the duration setting did nothing."""

    def setUp(self):
        self.writer = ScriptWriter.__new__(ScriptWriter)

    def test_a_shorter_configured_video_gets_a_shorter_script(self):
        four = self.writer._target_words(channel(240), "video")
        eight = self.writer._target_words(channel(480), "video")
        self.assertLess(four, eight)

    def test_a_four_minute_channel_is_no_longer_forced_to_nine_minutes(self):
        self.assertLess(self.writer._target_words(channel(240), "video"), 800)

    def test_the_budget_rises_with_the_configured_duration(self):
        budgets = [self.writer._target_words(channel(s), "video") for s in (240, 360, 480, 600)]
        self.assertEqual(budgets, sorted(budgets))

    def test_a_silly_duration_still_gives_a_usable_script(self):
        self.assertGreaterEqual(self.writer._target_words(channel(1), "video"), 320)


class HeadlineFillerTests(unittest.TestCase):
    """Topic titles are headlines; no encyclopedia has a page under one."""

    def test_framing_words_are_treated_as_filler(self):
        for word in ("why", "still", "matters", "historians", "inside"):
            with self.subTest(word=word):
                self.assertIn(word, _HEADLINE_FILLER)

    def test_real_subjects_are_not_filler(self):
        for word in ("nazca", "angkor", "pompeii", "lines"):
            with self.subTest(word=word):
                self.assertNotIn(word, _HEADLINE_FILLER)


class FailedUploadTests(unittest.TestCase):
    """A finished video whose upload failed is worth keeping."""

    def queue_with(self, state: Path, item: dict) -> None:
        base = {
            "channel": "brain_lens",
            "content_kind": "short",
            "target_date": "2026-09-20",
            "target_slot": "09:20",
            "priority": 0,
            "force_flags": {},
            "created_at": "2026-09-20T00:05:00",
        }
        rq.save_queue(state, "brain_lens", {"channel": "brain_lens", "items": [dict(base, **item)]})

    def test_a_dropped_connection_does_not_throw_the_video_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            video = state / "done.mp4"
            video.write_bytes(b"x" * 2048)
            self.queue_with(state, {"id": "a", "status": "queued_upload", "video_path": str(video)})
            updated = rq.note_upload_failure(state, "a", "connection reset")
            self.assertEqual(updated["status"], "rendered", "it should wait for another try")
            self.assertEqual(updated["upload_attempts"], 1)

    def test_it_gives_up_after_repeated_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            video = state / "done.mp4"
            video.write_bytes(b"x" * 2048)
            self.queue_with(
                state,
                {"id": "a", "status": "queued_upload", "video_path": str(video),
                 "upload_attempts": rq.MAX_UPLOAD_ATTEMPTS - 1},
            )
            self.assertEqual(rq.note_upload_failure(state, "a", "still failing")["status"], "failed")

    def test_a_video_that_is_gone_is_not_kept_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.queue_with(state, {"id": "a", "status": "queued_upload", "video_path": str(state / "missing.mp4")})
            self.assertEqual(rq.note_upload_failure(state, "a", "file vanished")["status"], "failed")

    def test_the_reason_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            video = state / "done.mp4"
            video.write_bytes(b"x" * 2048)
            self.queue_with(state, {"id": "a", "status": "queued_upload", "video_path": str(video)})
            self.assertIn("timed out", rq.note_upload_failure(state, "a", "upload timed out")["error"])

    def test_an_unknown_item_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rq.note_upload_failure(Path(tmp), "nope", "x"))


if __name__ == "__main__":
    unittest.main()
