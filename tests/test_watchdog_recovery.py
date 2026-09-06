from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import watchdog


class WatchdogRecoveryTests(unittest.TestCase):
    def test_watchdog_heartbeat_records_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with patch.object(watchdog, "STATE", state_dir):
                watchdog._write_watchdog_heartbeat(note="test")

            payload = json.loads((state_dir / "watchdog_heartbeat.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["note"], "test")
            self.assertIn("updated_at", payload)

    def test_dead_watchdog_lock_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            lock_path = state_dir / "watchdog.lock"
            lock_path.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
            with (
                patch.object(watchdog, "STATE", state_dir),
                patch.object(watchdog, "_pid_is_running", return_value=False),
            ):
                acquired = watchdog._acquire_single_instance_lock()

            self.assertEqual(acquired, lock_path)
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertIn("pid", payload)
            lock_path.unlink()

    def test_live_watchdog_lock_is_not_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            lock_path = state_dir / "watchdog.lock"
            original = json.dumps({"pid": 4321})
            lock_path.write_text(original, encoding="utf-8")
            with (
                patch.object(watchdog, "STATE", state_dir),
                patch.object(watchdog, "_pid_is_running", return_value=True),
            ):
                acquired = watchdog._acquire_single_instance_lock()

            self.assertIsNone(acquired)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), original)

    def test_active_build_markers_supply_orphan_process_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            active_dir = state_dir / "active_builds"
            active_dir.mkdir(parents=True)
            (active_dir / "ancient_history.json").write_text(
                json.dumps({"pid": 4321}),
                encoding="utf-8",
            )
            (active_dir / "invalid.json").write_text("not-json", encoding="utf-8")

            with patch.object(watchdog, "STATE", state_dir):
                self.assertEqual(watchdog._active_build_pids(), [4321])

    def test_clearing_active_build_markers_removes_stale_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            active_dir = state_dir / "active_builds"
            active_dir.mkdir(parents=True)
            marker = active_dir / "brain_lens.json"
            marker.write_text(json.dumps({"pid": 9876}), encoding="utf-8")

            with patch.object(watchdog, "STATE", state_dir):
                watchdog._clear_active_build_markers()

            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
