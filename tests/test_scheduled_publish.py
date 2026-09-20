"""Scheduled publishing, on every uploader, and the caption limits staying in step.

Both guard mistakes that have already cost finished videos: an argument added
to one of the two uploader classes, and a renderer told it may produce captions
that the gate then rejects.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone

from yt_auto import publish_spacing
from yt_auto import youtube_upload as legacy_uploader
from yt_auto.subtitles import SubtitleComposer
from yt_auto.uploaders import youtube as active_uploader
from yt_auto.video_builder import VideoBuilder

UPLOADER_MODULES = (legacy_uploader, active_uploader)


def uploader_class(module):
    return next(
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and callable(getattr(obj, "upload", None))
    )


class EveryUploaderSchedulesTests(unittest.TestCase):
    """This repo has two uploader classes. Both take publish_at, always.

    A scheduled-publish change once went into the class the pipeline does not
    use; the other raised "unexpected keyword argument 'publish_at'" and two
    finished long videos were marked failed.
    """

    def test_both_uploaders_accept_publish_at(self):
        for module in UPLOADER_MODULES:
            with self.subTest(module=module.__name__):
                params = inspect.signature(uploader_class(module).upload).parameters
                self.assertIn("publish_at", params)

    def test_both_uploaders_convert_local_time_to_utc(self):
        moment = datetime(2026, 9, 20, 21, 30)
        expected = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for module in UPLOADER_MODULES:
            with self.subTest(module=module.__name__):
                self.assertEqual(module._rfc3339_utc(moment), expected)

    def test_an_aware_time_is_not_shifted_twice(self):
        aware = datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)
        for module in UPLOADER_MODULES:
            with self.subTest(module=module.__name__):
                self.assertEqual(module._rfc3339_utc(aware), "2026-09-20T16:30:00Z")


class CaptionLimitsAgreeTests(unittest.TestCase):
    """What the renderer is told to make must be what the gate accepts.

    Raising the renderer's limit and leaving the checker's behind rejected
    every finished Ancient Short until someone noticed.
    """

    def test_the_renderer_never_targets_faster_than_the_checker_allows(self):
        self.assertLessEqual(VideoBuilder.SHORT_CAPTION_TARGET_CPS, SubtitleComposer.DEFAULT_MAX_CPS)


class SpacedScheduleTests(unittest.TestCase):
    def test_a_day_of_slots_can_be_scheduled_without_collisions(self):
        day = datetime(2026, 9, 20, 17, 0)
        wanted = [day + timedelta(minutes=offset) for offset in (0, 10, 20, 40, 300)]
        placed = publish_spacing.space_out(wanted)
        self.assertEqual(len(placed), len(wanted))
        for earlier, later in zip(placed, placed[1:]):
            self.assertGreaterEqual((later - earlier).total_seconds() / 60, 30)

    def test_scheduling_never_brings_a_video_forward(self):
        day = datetime(2026, 9, 20, 17, 0)
        wanted = [day, day + timedelta(minutes=5)]
        for original, placed in zip(wanted, publish_spacing.space_out(wanted)):
            self.assertGreaterEqual(placed, original)


if __name__ == "__main__":
    unittest.main()
