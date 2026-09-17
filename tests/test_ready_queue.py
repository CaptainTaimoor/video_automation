from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yt_auto import ready_queue as rq


class ReadyQueueTests(unittest.TestCase):
    def test_flag_default_off_and_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            flag = root / "controls" / "flags" / "ready_queue.enabled"
            flag.parent.mkdir(parents=True)
            flag.write_text("0\n", encoding="utf-8")
            self.assertFalse(rq.is_ready_queue_enabled(root))
            result = rq.set_ready_queue_enabled(True, root)
            self.assertTrue(result["enabled"])
            self.assertTrue(rq.is_ready_queue_enabled(root))
            self.assertIn("YT_READY_QUEUE=1", (root / ".env").read_text(encoding="utf-8"))

    def test_day_pack_creates_planned_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True)
            channel = SimpleNamespace(
                id="ancient_history",
                schedule_times=["10:00", "18:00"],
                shorts=SimpleNamespace(enabled=True, schedule_times=["10:00", "18:00"]),
                videos=SimpleNamespace(enabled=True, schedule_times=["20:00"]),
            )
            plan = rq.build_day_pack(
                state_dir=state_dir,
                root=root,
                channel=channel,
                timezone_name="Asia/Karachi",
                day="2026-09-06",
            )
            self.assertEqual(plan["day"], "2026-09-06")
            self.assertEqual(len(plan["slots"]), 3)
            items = rq.list_items(state_dir, ["ancient_history"])
            self.assertEqual(len(items), 3)
            self.assertTrue(all(item["status"] == "planned" for item in items))

            # Idempotent second call does not duplicate.
            rq.build_day_pack(
                state_dir=state_dir,
                root=root,
                channel=channel,
                timezone_name="Asia/Karachi",
                day="2026-09-06",
            )
            self.assertEqual(len(rq.list_items(state_dir, ["ancient_history"])), 3)

    def test_status_transitions_render_upload_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            channel = SimpleNamespace(
                id="brain_lens",
                schedule_times=["12:00"],
                shorts=SimpleNamespace(enabled=True, schedule_times=["12:00"]),
                videos=SimpleNamespace(enabled=False, schedule_times=[]),
            )
            rq.build_day_pack(
                state_dir=state_dir,
                root=root,
                channel=channel,
                timezone_name="UTC",
                day="2026-09-06",
            )
            item = rq.list_items(state_dir, ["brain_lens"])[0]
            item_id = item["id"]
            rq.mark_rendering(state_dir, item_id)
            video = root / "out.mp4"
            video.write_bytes(b"fake")
            updated = rq.attach_render_result(
                state_dir,
                item_id,
                run_dir=root,
                video_path=video,
                title="Test Short",
                quality_decision="pass",
            )
            self.assertEqual(updated["status"], "rendered")
            picked = rq.pick_ready_for_upload(state_dir, "brain_lens", "short", allow_skip_slot_wait=True)
            self.assertIsNotNone(picked)
            uploaded = rq.mark_uploaded(state_dir, item_id, youtube_id="abc123")
            self.assertEqual(uploaded["status"], "uploaded")

            # New item then cancel
            rq.build_day_pack(
                state_dir=state_dir,
                root=root,
                channel=channel,
                timezone_name="UTC",
                day="2026-09-07",
            )
            other = [i for i in rq.list_items(state_dir, ["brain_lens"]) if i["status"] == "planned"][0]
            cancelled = rq.cancel_item(state_dir, other["id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(rq.clear_cancelled(state_dir, ["brain_lens"]), 1)

    def test_quality_hold_not_uploadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            data = {
                "channel": "ancient_history",
                "items": [
                    {
                        "id": "rq_hold",
                        "channel": "ancient_history",
                        "content_kind": "short",
                        "status": "held_quality",
                        "video_path": str(state_dir / "missing.mp4"),
                        "target_slot": "10:00",
                        "target_date": "2026-09-06",
                        "priority": 0,
                    }
                ],
            }
            rq.save_queue(state_dir, "ancient_history", data)
            self.assertIsNone(
                rq.pick_ready_for_upload(state_dir, "ancient_history", "short", allow_skip_slot_wait=True)
            )


if __name__ == "__main__":
    unittest.main()
