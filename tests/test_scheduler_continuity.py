from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yt_auto.scheduler_service import BuildExecutionResult, ScheduleService
from yt_auto.utils import now_in_tz, write_json


class SchedulerContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        state_dir = Path(self.temp_dir.name)
        self.channel = SimpleNamespace(
            id="test_channel",
            youtube=SimpleNamespace(upload_enabled=True),
            daily_upload_cap=8,
            max_upload_gap_hours=4.0,
            upload_gap_retry_cooldown_minutes=75,
            short_build_timeout_minutes=45,
            video_build_timeout_minutes=90,
            short_build_stall_minutes=12,
            video_build_stall_minutes=25,
            output_dir=state_dir / "output" / "test_channel",
        )
        app = SimpleNamespace(
            timezone="Asia/Karachi",
            state_dir=state_dir,
            upload_gap_check_minutes=10,
            schedule_uploads=True,
        )
        self.factory = MagicMock()
        self.factory.config = SimpleNamespace(app=app, channels=[self.channel])
        self.factory._channel.side_effect = lambda channel_id: self.channel
        self.factory._daily_upload_cap_reached.return_value = False
        self.factory._current_upload_block.return_value = None
        self.service = ScheduleService(self.factory)

    def tearDown(self) -> None:
        self.service.scheduler.shutdown(wait=False) if self.service.scheduler.running else None
        self.temp_dir.cleanup()

    def _write_run(self, timestamp, uploaded: bool = True) -> None:
        run_name = timestamp.strftime("%Y%m%d_%H%M%S")
        row = {
            "channel": self.channel.id,
            "run_dir": str(Path("output") / self.channel.id / "short" / run_name),
            "uploaded": uploaded,
            "youtube_id": "video-id" if uploaded else None,
        }
        with (self.factory.config.app.state_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def test_latest_successful_upload_ignores_build_only_rows(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        self._write_run(now - timedelta(hours=2), uploaded=True)
        self._write_run(now - timedelta(minutes=5), uploaded=False)

        latest = self.service._latest_successful_upload_at(self.channel.id)

        self.assertIsNotNone(latest)
        self.assertLess(abs((latest - (now - timedelta(hours=2))).total_seconds()), 2)

    def test_gap_monitor_queues_recovery_short_when_overdue(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        self._write_run(now - timedelta(hours=6), uploaded=True)

        with (
            patch.object(self.service.scheduler, "get_job", return_value=None),
            patch.object(self.service.scheduler, "add_job") as add_job,
        ):
            self.service._run_upload_gap_monitor([self.channel.id], upload=True)

        self.assertEqual(add_job.call_count, 1)
        self.assertEqual(add_job.call_args.kwargs["id"], "upload_gap_recovery_test_channel")
        self.assertEqual(add_job.call_args.kwargs["kwargs"]["content_kind"], "short")
        state = json.loads(self.service._continuity_state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["channels"][self.channel.id]["status"], "recovery_scheduled")

    def test_gap_monitor_obeys_persistent_retry_cooldown(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        self._write_run(now - timedelta(hours=6), uploaded=True)
        write_json(
            self.service._continuity_state_path(),
            {
                "channels": {
                    self.channel.id: {
                        "last_recovery_scheduled_at": (now - timedelta(minutes=20)).isoformat()
                    }
                }
            },
        )

        with patch.object(self.service.scheduler, "add_job") as add_job:
            self.service._run_upload_gap_monitor([self.channel.id], upload=True)

        add_job.assert_not_called()
        state = json.loads(self.service._continuity_state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["channels"][self.channel.id]["status"], "recovery_cooldown")

    def test_fast_recovery_cooldown_never_grows_beyond_thirty_minutes(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        self.channel.upload_gap_retry_cooldown_minutes = 20
        self._write_run(now - timedelta(hours=6), uploaded=True)
        write_json(
            self.service._continuity_state_path(),
            {
                "channels": {
                    self.channel.id: {
                        "consecutive_failures": 4,
                        "last_failure_category": "visual_sources",
                        "last_recovery_scheduled_at": (now - timedelta(minutes=31)).isoformat(),
                    }
                }
            },
        )

        with (
            patch.object(self.service.scheduler, "get_job", return_value=None),
            patch.object(self.service.scheduler, "add_job") as add_job,
        ):
            self.service._run_upload_gap_monitor([self.channel.id], upload=True)

        self.assertEqual(add_job.call_count, 1)
        state = json.loads(self.service._continuity_state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["channels"][self.channel.id]["effective_cooldown_minutes"], 30)

    def test_gap_monitor_obeys_daily_upload_cap(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        self._write_run(now - timedelta(hours=6), uploaded=True)
        self.factory._daily_upload_cap_reached.return_value = True

        with patch.object(self.service.scheduler, "add_job") as add_job:
            self.service._run_upload_gap_monitor([self.channel.id], upload=True)

        add_job.assert_not_called()
        state = json.loads(self.service._continuity_state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["channels"][self.channel.id]["status"], "daily_cap_reached")

    def test_gap_monitor_escalates_recovery_stage_after_failure(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        self._write_run(now - timedelta(hours=8), uploaded=True)
        write_json(
            self.service._continuity_state_path(),
            {
                "channels": {
                    self.channel.id: {
                        "consecutive_failures": 1,
                        "last_failure_category": "visual_sources",
                        "last_recovery_scheduled_at": (now - timedelta(hours=4)).isoformat(),
                    }
                }
            },
        )

        with (
            patch.object(self.service.scheduler, "get_job", return_value=None),
            patch.object(self.service.scheduler, "add_job") as add_job,
        ):
            self.service._run_upload_gap_monitor([self.channel.id], upload=True)

        self.assertEqual(
            add_job.call_args.kwargs["kwargs"]["recovery_stage"],
            2,
        )

    def test_scheduled_worker_does_not_inherit_continuity_mode(self) -> None:
        process = MagicMock()
        process.wait.return_value = 0
        with (
            patch.dict(os.environ, {"YT_CONTINUITY_RECOVERY": "1"}),
            patch("yt_auto.scheduler_service.subprocess.Popen", return_value=process) as popen,
        ):
            result = self.service._execute_build_subprocess(
                self.channel,
                upload=True,
                content_kind="video",
                trigger_source="scheduled",
            )

        self.assertTrue(result.success)
        worker_env = popen.call_args.kwargs["env"]
        self.assertNotIn("YT_CONTINUITY_RECOVERY", worker_env)
        self.assertNotIn("YT_RECOVERY_STAGE", worker_env)
        self.assertEqual(worker_env["PYTHONUNBUFFERED"], "1")

    def test_recovery_worker_receives_targeted_asset_profile(self) -> None:
        process = MagicMock()
        process.wait.return_value = 0
        with patch(
            "yt_auto.scheduler_service.subprocess.Popen",
            return_value=process,
        ) as popen:
            result = self.service._execute_build_subprocess(
                self.channel,
                upload=True,
                content_kind="short",
                trigger_source="upload_gap_recovery",
                recovery_stage=2,
            )

        self.assertTrue(result.success)
        worker_env = popen.call_args.kwargs["env"]
        self.assertEqual(worker_env["YT_CONTINUITY_RECOVERY"], "1")
        self.assertEqual(worker_env["YT_RECOVERY_STAGE"], "2")
        self.assertEqual(worker_env["YT_VISUAL_SHORT_QUERY_LIMIT"], "8")
        self.assertEqual(worker_env["YT_VISUAL_PROVIDER_DEADLINE_SECONDS"], "12")
        self.assertEqual(worker_env["YT_VISUAL_SHORT_SCENE_DEADLINE_SECONDS"], "35")
        self.assertEqual(worker_env["YT_VISUAL_SHORT_FETCH_DEADLINE_SECONDS"], "240")
        self.assertEqual(worker_env["YT_RECOVERY_FORCE_ARCHIVE_RICH"], "1")

    def test_inactive_worker_is_terminated_before_absolute_timeout(self) -> None:
        process = MagicMock(pid=4321)
        process.wait.side_effect = subprocess.TimeoutExpired(cmd="build", timeout=15)
        active_path = self.service._active_build_path(self.channel.id)

        with (
            patch.object(self.service, "_build_activity_signature", return_value=(0, 0, 0, 0, 0)),
            patch.object(self.service, "_terminate_process_tree") as terminate,
            patch("yt_auto.scheduler_service.monotonic", side_effect=[0.0, 61.0]),
        ):
            result = self.service._monitor_build_process(
                process=process,
                active_path=active_path,
                log_path=self.factory.config.app.state_dir / "worker.log",
                output_base=self.channel.output_dir / "short",
                worker_state={"pid": process.pid},
                absolute_timeout_seconds=600,
                stall_timeout_seconds=60,
                poll_seconds=15,
            )

        self.assertEqual(result[1], "stuck_worker")
        self.assertTrue(result[3])
        terminate.assert_called_once_with(process)

    def test_new_output_activity_keeps_worker_alive(self) -> None:
        process = MagicMock(pid=4322)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="build", timeout=15),
            0,
        ]
        active_path = self.service._active_build_path(self.channel.id)

        with (
            patch.object(
                self.service,
                "_build_activity_signature",
                side_effect=[(0, 0, 0, 0, 0), (0, 0, 1, 1024, 10)],
            ),
            patch.object(self.service, "_terminate_process_tree") as terminate,
            patch("yt_auto.scheduler_service.monotonic", side_effect=[0.0, 61.0]),
        ):
            result = self.service._monitor_build_process(
                process=process,
                active_path=active_path,
                log_path=self.factory.config.app.state_dir / "worker.log",
                output_base=self.channel.output_dir / "short",
                worker_state={"pid": process.pid},
                absolute_timeout_seconds=600,
                stall_timeout_seconds=60,
                poll_seconds=15,
            )

        self.assertEqual(result, (0, "", "", False))
        terminate.assert_not_called()

    def test_stuck_and_timeout_failures_are_immediately_repairable(self) -> None:
        self.assertIn("stuck_worker", ScheduleService.REPAIRABLE_BUILD_FAILURES)
        self.assertIn("timeout", ScheduleService.REPAIRABLE_BUILD_FAILURES)

    def test_build_failure_classifier_reports_visual_source_block(self) -> None:
        log_path = self.factory.config.app.state_dir / "worker.log"
        log_path.write_text(
            "RuntimeError: Failed visual preflight: Ancient Short contains "
            "1 local fact-card fallback(s); zero are allowed\n",
            encoding="utf-8",
        )

        category, summary = self.service._classify_build_failure(log_path)

        self.assertEqual(category, "visual_sources")
        self.assertIn("local fact-card fallback", summary)

    def test_failed_scheduled_build_starts_immediate_recovery_replacement(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        previous_upload = now - timedelta(hours=2)
        failure_log = self.factory.config.app.state_dir / "scheduled_failure.log"
        success_log = self.factory.config.app.state_dir / "recovery_success.log"
        self.factory._load_backlog.return_value = []

        with (
            patch.object(
                self.service,
                "_latest_successful_upload_at",
                side_effect=[previous_upload, previous_upload, now],
            ),
            patch.object(
                self.service,
                "_execute_build_subprocess",
                side_effect=[
                    BuildExecutionResult(
                        success=False,
                        log_path=failure_log,
                        failure_category="visual_sources",
                        failure_summary="one local fact-card fallback",
                    ),
                    BuildExecutionResult(success=True, log_path=success_log),
                ],
            ) as execute,
        ):
            succeeded = self.service._run_main_job(
                channel_id=self.channel.id,
                upload=True,
                content_kind="short",
                trigger_source="scheduled",
            )

        self.assertTrue(succeeded)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            execute.call_args_list[1].kwargs["trigger_source"],
            "upload_gap_recovery",
        )
        self.assertEqual(execute.call_args_list[1].kwargs["recovery_stage"], 1)

    def test_failed_scheduled_long_caption_build_starts_immediate_replacement(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        previous_upload = now - timedelta(hours=2)
        failure_log = self.factory.config.app.state_dir / "long_caption_failure.log"
        success_log = self.factory.config.app.state_dir / "long_caption_recovery.log"
        self.factory._load_backlog.return_value = []

        with (
            patch.object(
                self.service,
                "_latest_successful_upload_at",
                side_effect=[previous_upload, previous_upload, now],
            ),
            patch.object(
                self.service,
                "_execute_build_subprocess",
                side_effect=[
                    BuildExecutionResult(
                        success=False,
                        log_path=failure_log,
                        failure_category="caption_structure",
                        failure_summary="beat 2 requires 8.2% stretch",
                    ),
                    BuildExecutionResult(success=True, log_path=success_log),
                ],
            ) as execute,
        ):
            succeeded = self.service._run_main_job(
                channel_id=self.channel.id,
                upload=True,
                content_kind="video",
                trigger_source="scheduled",
            )

        self.assertTrue(succeeded)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args_list[1].args[2], "video")
        self.assertEqual(
            execute.call_args_list[1].kwargs["trigger_source"],
            "upload_gap_recovery",
        )
        self.assertEqual(execute.call_args_list[1].kwargs["recovery_stage"], 1)

    def test_exhausted_immediate_repairs_schedule_persistent_delayed_retry(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        previous_upload = now - timedelta(hours=2)
        retry_at = now + timedelta(minutes=20)
        failure_log = self.factory.config.app.state_dir / "caption_failure.log"
        self.factory._load_backlog.return_value = []
        failed = BuildExecutionResult(
            success=False,
            log_path=failure_log,
            failure_category="caption_structure",
            failure_summary="short caption timing cannot fit",
        )

        with (
            patch.object(
                self.service,
                "_latest_successful_upload_at",
                side_effect=[previous_upload, previous_upload],
            ),
            patch.object(self.service, "_execute_build_subprocess", return_value=failed) as execute,
            patch.object(
                self.service,
                "_schedule_delayed_recovery",
                return_value=retry_at,
            ) as schedule_retry,
        ):
            succeeded = self.service._run_main_job(
                channel_id=self.channel.id,
                upload=True,
                content_kind="short",
                trigger_source="scheduled",
            )

        self.assertFalse(succeeded)
        self.assertEqual(execute.call_count, 3)
        schedule_retry.assert_called_once()
        self.assertEqual(schedule_retry.call_args.args[:2], (self.channel.id, "short"))
        state = json.loads(self.service._continuity_state_path().read_text(encoding="utf-8"))
        channel_state = state["channels"][self.channel.id]
        self.assertEqual(channel_state["status"], "recovery_retry_scheduled")
        self.assertEqual(channel_state["last_failure_content_kind"], "short")
        self.assertEqual(channel_state["next_recovery_at"], retry_at.isoformat())

    def test_scheduler_restart_restores_recent_failed_recovery(self) -> None:
        now = now_in_tz(self.factory.config.app.timezone)
        write_json(
            self.service._continuity_state_path(),
            {
                "channels": {
                    self.channel.id: {
                        "status": "recovery_failed",
                        "latest_successful_upload_at": (now - timedelta(hours=2)).isoformat(),
                        "last_failure_at": (now - timedelta(minutes=2)).isoformat(),
                        "last_failure_category": "caption_structure",
                        "last_failure_content_kind": "short",
                        "recovery_stage": 2,
                    }
                }
            },
        )
        retry_at = now + timedelta(seconds=30)

        with patch.object(
            self.service,
            "_schedule_delayed_recovery",
            return_value=retry_at,
        ) as schedule_retry:
            self.service._restore_failed_recovery_jobs([self.channel], upload=True)

        schedule_retry.assert_called_once_with(
            self.channel.id,
            "short",
            upload=True,
            recovery_stage=3,
            delay_seconds=30,
        )
        state = json.loads(self.service._continuity_state_path().read_text(encoding="utf-8"))
        self.assertEqual(
            state["channels"][self.channel.id]["status"],
            "recovery_retry_scheduled",
        )


if __name__ == "__main__":
    unittest.main()
