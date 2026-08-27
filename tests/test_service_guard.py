from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import service_guard


class ServiceGuardTests(unittest.TestCase):
    def test_live_lock_owner_avoids_process_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "watchdog.lock").write_text(
                json.dumps({"pid": 4321}),
                encoding="utf-8",
            )
            with (
                patch.object(service_guard, "STATE", state_dir),
                patch.object(service_guard, "_pid_is_running", return_value=True),
                patch.object(service_guard, "_watchdog_pids") as process_scan,
            ):
                live, pids, evidence = service_guard._watchdog_status()

            self.assertTrue(live)
            self.assertEqual(pids, [4321])
            self.assertEqual(evidence, "lock_owner")
            process_scan.assert_not_called()

    def test_fresh_heartbeat_prevents_duplicate_restart_after_scan_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "watchdog_heartbeat.json").write_text(
                json.dumps({"updated_at": datetime.now().astimezone().isoformat()}),
                encoding="utf-8",
            )
            with (
                patch.object(service_guard, "STATE", state_dir),
                patch.object(service_guard, "_pid_is_running", return_value=False),
                patch.object(service_guard, "_watchdog_pids", return_value=[]),
            ):
                live, pids, evidence = service_guard._watchdog_status()

            self.assertTrue(live)
            self.assertEqual(pids, [])
            self.assertTrue(evidence.startswith("heartbeat:"))

    def test_dead_lock_owner_overrides_old_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "watchdog.lock").write_text(
                json.dumps({"pid": 4321}),
                encoding="utf-8",
            )
            (state_dir / "watchdog_heartbeat.json").write_text(
                json.dumps({"updated_at": datetime.now().astimezone().isoformat()}),
                encoding="utf-8",
            )
            with (
                patch.object(service_guard, "STATE", state_dir),
                patch.object(service_guard, "_pid_is_running", return_value=False),
                patch.object(service_guard, "_watchdog_pids", return_value=[]),
            ):
                live, pids, evidence = service_guard._watchdog_status()

            self.assertFalse(live)
            self.assertEqual(pids, [])
            self.assertEqual(evidence, "stale_lock")

    def test_live_guard_lock_is_not_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            lock_path = state_dir / "service_guard.lock"
            original = json.dumps({"pid": 9876})
            lock_path.write_text(original, encoding="utf-8")
            with (
                patch.object(service_guard, "STATE", state_dir),
                patch.object(service_guard, "_pid_is_running", return_value=True),
            ):
                acquired = service_guard._acquire_lock()

            self.assertIsNone(acquired)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
