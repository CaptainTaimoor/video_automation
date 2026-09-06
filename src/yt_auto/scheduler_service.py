from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic, sleep, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from yt_auto.models import ChannelConfig
from yt_auto.pipeline import ShortsFactory
from yt_auto.utils import now_in_tz, read_json, write_json


@dataclass(frozen=True)
class BuildExecutionResult:
    success: bool
    log_path: Path
    failure_category: str = ""
    failure_summary: str = ""
    timed_out: bool = False


class ScheduleService:
    REPAIRABLE_BUILD_FAILURES = frozenset(
        {
            "caption_structure",
            "visual_diversity",
            "visual_sources",
            "topic_exhaustion",
            "quality_gate",
            "timeout",
            "stuck_worker",
            "unknown",
        }
    )
    # i5-4300U / 12GB RAM: never run more than two MoviePy/FFmpeg builds at once.
    MAX_CONCURRENT_BUILDS = 2
    MIN_FREE_RAM_MB = 1800

    def __init__(self, factory: ShortsFactory) -> None:
        self.factory = factory
        self.scheduler = BlockingScheduler(timezone=ZoneInfo(factory.config.app.timezone))
        self._lock_file = None
        self._channel_build_locks: dict[str, threading.Lock] = {
            channel.id: threading.Lock()
            for channel in factory.config.channels
        }
        self._build_semaphore = threading.Semaphore(self.MAX_CONCURRENT_BUILDS)
        self._continuity_state_lock = threading.RLock()

    def _available_ram_mb(self) -> float | None:
        try:
            import psutil
        except Exception:
            return None
        try:
            return float(psutil.virtual_memory().available) / (1024.0 * 1024.0)
        except Exception:
            return None

    def _wait_for_build_capacity(self, channel_id: str, content_kind: str) -> None:
        """Block until a concurrent-build slot is free and RAM looks safe enough."""
        while True:
            acquired = self._build_semaphore.acquire(blocking=True, timeout=5.0)
            if not acquired:
                continue
            available_mb = self._available_ram_mb()
            if available_mb is None or available_mb >= self.MIN_FREE_RAM_MB:
                return
            self._build_semaphore.release()
            self.factory.logger.warning(
                channel_id,
                f"Deferring {content_kind} build: only {available_mb:.0f}MB free RAM "
                f"(need >={self.MIN_FREE_RAM_MB}MB with max {self.MAX_CONCURRENT_BUILDS} concurrent builds).",
            )
            sleep(8.0)

    def _heartbeat_path(self) -> Path:
        return self.factory.config.app.state_dir / "scheduler_heartbeat.json"

    def _active_build_dir(self) -> Path:
        return self.factory.config.app.state_dir / "active_builds"

    def _active_build_path(self, channel_id: str) -> Path:
        safe_channel = "".join(character for character in channel_id if character.isalnum() or character in {"-", "_"})
        return self._active_build_dir() / f"{safe_channel or 'worker'}.json"

    def _active_build_records(self) -> list[dict]:
        records: list[dict] = []
        for path in self._active_build_dir().glob("*.json"):
            data = read_json(path, {})
            if isinstance(data, dict) and data:
                records.append(data)
        return records

    def _write_heartbeat(self, status: str = "running", note: str = "") -> None:
        try:
            jobs = []
            for job in self.scheduler.get_jobs():
                next_run = getattr(job, "next_run_time", None)
                jobs.append({
                    "id": job.id,
                    "next_run_time": next_run.isoformat() if next_run else None,
                })
            write_json(self._heartbeat_path(), {
                "pid": os.getpid(),
                "status": status,
                "note": note,
                "updated_at": now_in_tz(self.factory.config.app.timezone).isoformat(),
                "jobs": jobs,
                "active_builds": self._active_build_records(),
            })
        except Exception:
            pass

    def _acquire_singleton_lock(self) -> None:
        lock_path = self.factory.config.app.state_dir / "scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if lock_file.tell() == 0:
                    lock_file.write(" ")
                    lock_file.flush()
                    lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise RuntimeError("Another scheduler is already running. Stop the duplicate process before starting a new one.") from exc

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()} started={datetime.now().isoformat()}\n")
        lock_file.flush()
        self._lock_file = lock_file

    def _release_singleton_lock(self) -> None:
        lock_file = self._lock_file
        self._lock_file = None
        if not lock_file:
            return
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def _channel_slots(self, channel: ChannelConfig) -> list[tuple[str, str]]:
        slots: list[tuple[str, str]] = []
        if channel.videos.enabled:
            slots.extend((time_str, "video") for time_str in channel.videos.schedule_times)
        if channel.shorts.enabled:
            slots.extend((time_str, "short") for time_str in channel.shorts.schedule_times)
        if not slots:
            slots.extend((time_str, "short") for time_str in channel.schedule_times)
        slots.sort(key=lambda item: item[0])
        return slots

    def _state_path(self) -> Path:
        return self.factory.config.app.state_dir / "scheduler_interval_state.json"

    def _load_state(self) -> dict:
        path = self._state_path()
        data = read_json(path, {})
        if not isinstance(data, dict):
            return {}
        return data

    def _save_state(self, data: dict) -> None:
        write_json(self._state_path(), data)

    def _continuity_state_path(self) -> Path:
        return self.factory.config.app.state_dir / "upload_continuity_state.json"

    def _load_continuity_state(self) -> dict:
        with self._continuity_state_lock:
            data = read_json(self._continuity_state_path(), {"channels": {}})
            if not isinstance(data, dict):
                data = {"channels": {}}
            channels = data.get("channels")
            if not isinstance(channels, dict):
                data["channels"] = {}
            return data

    def _update_continuity_state(self, channel_id: str, **updates) -> None:
        with self._continuity_state_lock:
            data = self._load_continuity_state()
            channels = data.setdefault("channels", {})
            entry = channels.setdefault(channel_id, {})
            entry.update(updates)
            data["updated_at"] = now_in_tz(self.factory.config.app.timezone).isoformat()
            write_json(self._continuity_state_path(), data)

    @staticmethod
    def _run_timestamp(run: dict, timezone_name: str) -> datetime | None:
        tz = ZoneInfo(timezone_name)
        for key in ("uploaded_at", "created_at"):
            raw = str(run.get(key) or "").strip()
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
            except ValueError:
                pass

        run_name = Path(str(run.get("run_dir") or "")).name
        try:
            return datetime.strptime(run_name, "%Y%m%d_%H%M%S").replace(tzinfo=tz)
        except ValueError:
            return None

    def _latest_successful_upload_at(self, channel_id: str) -> datetime | None:
        runs_path = self.factory.config.app.state_dir / "runs.jsonl"
        if not runs_path.exists():
            return None

        latest: datetime | None = None
        try:
            with runs_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        run = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if str(run.get("channel") or "") != channel_id:
                        continue
                    if not (run.get("uploaded") is True or str(run.get("youtube_id") or "").strip()):
                        continue
                    timestamp = self._run_timestamp(run, self.factory.config.app.timezone)
                    if timestamp and (latest is None or timestamp > latest):
                        latest = timestamp
        except OSError:
            return None
        return latest

    def _terminate_process_tree(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    @staticmethod
    def _output_activity_signature(output_base: Path, started_epoch: float) -> tuple[int, int, int]:
        if not output_base.exists():
            return (0, 0, 0)

        run_dirs: list[Path] = []
        try:
            day_dirs = sorted(
                (path for path in output_base.iterdir() if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            )[:2]
            for day_dir in day_dirs:
                for run_dir in day_dir.iterdir():
                    if not run_dir.is_dir():
                        continue
                    try:
                        if run_dir.stat().st_mtime >= started_epoch - 120:
                            run_dirs.append(run_dir)
                    except OSError:
                        continue
        except OSError:
            return (0, 0, 0)

        total_size = 0
        latest_mtime_ns = 0
        file_count = 0
        for run_dir in run_dirs:
            try:
                files = run_dir.rglob("*")
                for path in files:
                    if not path.is_file():
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    total_size += int(stat.st_size)
                    latest_mtime_ns = max(latest_mtime_ns, int(stat.st_mtime_ns))
                    file_count += 1
            except OSError:
                continue
        return (file_count, total_size, latest_mtime_ns)

    def _build_activity_signature(
        self,
        log_path: Path,
        output_base: Path,
        started_epoch: float,
    ) -> tuple[int, int, int, int, int]:
        try:
            log_stat = log_path.stat()
            log_size = int(log_stat.st_size)
            log_mtime_ns = int(log_stat.st_mtime_ns)
        except OSError:
            log_size = 0
            log_mtime_ns = 0
        output_count, output_size, output_mtime_ns = self._output_activity_signature(
            output_base,
            started_epoch,
        )
        return (log_size, log_mtime_ns, output_count, output_size, output_mtime_ns)

    def _monitor_build_process(
        self,
        process: subprocess.Popen,
        active_path: Path,
        log_path: Path,
        output_base: Path,
        worker_state: dict,
        absolute_timeout_seconds: float,
        stall_timeout_seconds: float,
        poll_seconds: float,
    ) -> tuple[int | None, str, str, bool]:
        started_monotonic = monotonic()
        started_epoch = time()
        last_progress_monotonic = started_monotonic
        last_progress_at = now_in_tz(self.factory.config.app.timezone).isoformat()
        signature = self._build_activity_signature(log_path, output_base, started_epoch)
        active_path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            worker_state.update(
                {
                    "status": "running",
                    "last_checked_at": now_in_tz(self.factory.config.app.timezone).isoformat(),
                    "last_progress_at": last_progress_at,
                    "activity_signature": list(signature),
                }
            )
            write_json(active_path, worker_state)
            try:
                return_code = process.wait(timeout=poll_seconds)
                return return_code, "", "", False
            except subprocess.TimeoutExpired:
                current_monotonic = monotonic()
                current_signature = self._build_activity_signature(log_path, output_base, started_epoch)
                if current_signature != signature:
                    signature = current_signature
                    last_progress_monotonic = current_monotonic
                    last_progress_at = now_in_tz(self.factory.config.app.timezone).isoformat()

                elapsed_seconds = current_monotonic - started_monotonic
                idle_seconds = current_monotonic - last_progress_monotonic
                if elapsed_seconds >= absolute_timeout_seconds:
                    self._terminate_process_tree(process)
                    return (
                        None,
                        "timeout",
                        f"build exceeded {absolute_timeout_seconds / 60:.0f} minutes",
                        True,
                    )
                if idle_seconds >= stall_timeout_seconds:
                    self._terminate_process_tree(process)
                    return (
                        None,
                        "stuck_worker",
                        f"worker produced no log or output progress for {stall_timeout_seconds / 60:.0f} minutes",
                        True,
                    )

    def _execute_build_subprocess(
        self,
        channel: ChannelConfig,
        upload: bool,
        content_kind: str,
        trigger_source: str = "scheduled",
        recovery_stage: int = 0,
    ) -> BuildExecutionResult:
        self._wait_for_build_capacity(channel.id, content_kind)
        try:
            return self._execute_build_subprocess_unlocked(
                channel=channel,
                upload=upload,
                content_kind=content_kind,
                trigger_source=trigger_source,
                recovery_stage=recovery_stage,
            )
        finally:
            self._build_semaphore.release()

    def _execute_build_subprocess_unlocked(
        self,
        channel: ChannelConfig,
        upload: bool,
        content_kind: str,
        trigger_source: str = "scheduled",
        recovery_stage: int = 0,
    ) -> BuildExecutionResult:
        root = Path(__file__).resolve().parents[2]
        timeout_minutes = (
            int(channel.video_build_timeout_minutes)
            if content_kind == "video"
            else int(channel.short_build_timeout_minutes)
        )
        configured_stall_minutes = (
            int(getattr(channel, "video_build_stall_minutes", 25) or 25)
            if content_kind == "video"
            else int(getattr(channel, "short_build_stall_minutes", 12) or 12)
        )
        stall_minutes = max(5, min(timeout_minutes - 1, configured_stall_minutes))
        log_dir = self.factory.config.app.state_dir / "scheduler_jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_in_tz(self.factory.config.app.timezone).strftime("%Y%m%d_%H%M%S_%f")
        log_path = log_dir / f"{channel.id}_{content_kind}_{stamp}.log"
        command = [
            sys.executable,
            str(root / "run.py"),
            "build",
            "--channel",
            channel.id,
            "--kind",
            content_kind,
        ]
        command.append("--upload" if upload else "--dry-run")

        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        worker_env = os.environ.copy()
        worker_env["PYTHONUNBUFFERED"] = "1"
        is_continuity_recovery = trigger_source == "upload_gap_recovery"
        if is_continuity_recovery:
            worker_env["YT_CONTINUITY_RECOVERY"] = "1"
            worker_env["YT_RECOVERY_STAGE"] = str(max(1, int(recovery_stage or 1)))
            worker_env["YT_RECOVERY_ASSET_DIVERSITY"] = "1"
            worker_env["YT_VISUAL_SHORT_QUERY_LIMIT"] = "8"
            worker_env["YT_VISUAL_PROVIDER_DEADLINE_SECONDS"] = "12"
            worker_env["YT_VISUAL_SHORT_SCENE_DEADLINE_SECONDS"] = "35"
            worker_env["YT_VISUAL_SHORT_FETCH_DEADLINE_SECONDS"] = "240"
            if int(recovery_stage or 1) >= 2:
                worker_env["YT_RECOVERY_FORCE_ARCHIVE_RICH"] = "1"
        else:
            worker_env.pop("YT_CONTINUITY_RECOVERY", None)
            worker_env.pop("YT_RECOVERY_STAGE", None)
            worker_env.pop("YT_RECOVERY_ASSET_DIVERSITY", None)
            worker_env.pop("YT_RECOVERY_FORCE_ARCHIVE_RICH", None)
        output_base = Path(channel.output_dir) / content_kind
        if not output_base.is_absolute():
            output_base = root / output_base
        active_path = self._active_build_path(channel.id)
        started_at = now_in_tz(self.factory.config.app.timezone).isoformat()
        return_code: int | None = None
        monitor_category = ""
        monitor_summary = ""
        monitor_timed_out = False
        try:
            with log_path.open("a", encoding="utf-8") as log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=str(root),
                    env=worker_env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
                poll_seconds = max(
                    2.0,
                    min(60.0, float(os.environ.get("YT_BUILD_WATCH_POLL_SECONDS", "15"))),
                )
                worker_state = {
                    "pid": int(process.pid),
                    "scheduler_pid": os.getpid(),
                    "channel": channel.id,
                    "content_kind": content_kind,
                    "trigger_source": trigger_source,
                    "started_at": started_at,
                    "log_path": str(log_path),
                    "output_base": str(output_base),
                    "absolute_timeout_seconds": timeout_minutes * 60,
                    "stall_timeout_seconds": stall_minutes * 60,
                }
                return_code, monitor_category, monitor_summary, monitor_timed_out = self._monitor_build_process(
                    process=process,
                    active_path=active_path,
                    log_path=log_path,
                    output_base=output_base,
                    worker_state=worker_state,
                    absolute_timeout_seconds=timeout_minutes * 60,
                    stall_timeout_seconds=stall_minutes * 60,
                    poll_seconds=poll_seconds,
                )
        finally:
            active_path.unlink(missing_ok=True)

        if monitor_category:
            self.factory.logger.error(
                channel.id,
                f"{content_kind.title()} {monitor_category.replace('_', ' ')} was terminated; "
                f"{monitor_summary}. Continuity recovery will retry. Log: {log_path}",
            )
            return BuildExecutionResult(
                success=False,
                log_path=log_path,
                failure_category=monitor_category,
                failure_summary=monitor_summary,
                timed_out=monitor_timed_out,
            )

        if return_code != 0:
            failure_category, failure_summary = self._classify_build_failure(log_path)
            self.factory.logger.error(
                channel.id,
                f"{content_kind.title()} build process exited with code {return_code} "
                f"({failure_category}: {failure_summary}). Log: {log_path}",
            )
            return BuildExecutionResult(
                success=False,
                log_path=log_path,
                failure_category=failure_category,
                failure_summary=failure_summary,
            )
        return BuildExecutionResult(success=True, log_path=log_path)

    @staticmethod
    def _classify_build_failure(log_path: Path) -> tuple[str, str]:
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")[-250_000:]
        except OSError:
            return "unknown", "worker failed without a readable log"

        lowered = text.lower()
        categories = (
            (
                "upload_auth",
                (
                    "invalid_grant",
                    "expired or revoked",
                    "authentication failed",
                    "uploadautherror",
                ),
            ),
            (
                "upload_quota",
                (
                    "uploadlimitexceedederror",
                    "quotaexceeded",
                    "daily upload limit",
                ),
            ),
            (
                "caption_structure",
                (
                    "captionpreflighterror",
                    "caption preflight failed",
                    "begins in the middle of a phrase",
                    "flash fragment",
                ),
            ),
            (
                "visual_diversity",
                (
                    "repeats conversation footage",
                    "repeats phone footage",
                    "repeats affection footage",
                    "near-duplicate visual",
                    "lacks a clearly relevant relationship visual",
                    "lacks a phone-relevant opening visual",
                ),
            ),
            (
                "visual_sources",
                (
                    "local fact-card fallback",
                    "local fact-card fallback(s)",
                    "source short edge",
                    "visual preflight",
                    "not enough usable visuals",
                    "unique verified real",
                ),
            ),
            (
                "topic_exhaustion",
                (
                    "failed to generate a high-quality topic",
                    "topic drafts exhausted",
                    "source-safe research",
                ),
            ),
            (
                "quality_gate",
                (
                    "quality gate",
                    "quality_gate",
                    "skipped_quality",
                ),
            ),
        )
        category = "unknown"
        for candidate, markers in categories:
            if any(marker in lowered for marker in markers):
                category = candidate
                break

        summary = ""
        for raw_line in reversed(text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            if (
                line.startswith(("RuntimeError:", "ValueError:", "yt_auto."))
                or "Error:" in line
                or "failed" in line.lower()
            ):
                summary = line
                break
        if not summary:
            summary = "worker exited before completing the upload"
        return category, summary[:360]

    @staticmethod
    def _effective_recovery_cooldown_minutes(
        cooldown_minutes: int,
        consecutive_failures: int,
    ) -> int:
        base_minutes = max(15, int(cooldown_minutes))
        retry_cap_minutes = max(base_minutes, 30)
        exponential_minutes = base_minutes * (2 ** min(3, max(0, consecutive_failures - 1)))
        return min(retry_cap_minutes, exponential_minutes)

    @staticmethod
    def _delayed_recovery_job_id(channel_id: str, content_kind: str) -> str:
        return f"post_failure_recovery_{channel_id}_{content_kind}"

    def _schedule_delayed_recovery(
        self,
        channel_id: str,
        content_kind: str,
        upload: bool,
        recovery_stage: int,
        delay_minutes: int = 20,
        delay_seconds: int | None = None,
    ) -> datetime | None:
        if not upload:
            return None
        now = now_in_tz(self.factory.config.app.timezone)
        if delay_seconds is None:
            run_at = now + timedelta(minutes=max(1, int(delay_minutes)))
        else:
            run_at = now + timedelta(seconds=max(1, int(delay_seconds)))
        self.scheduler.add_job(
            self._run_main_job,
            trigger=DateTrigger(
                run_date=run_at,
                timezone=ZoneInfo(self.factory.config.app.timezone),
            ),
            id=self._delayed_recovery_job_id(channel_id, content_kind),
            replace_existing=True,
            kwargs={
                "channel_id": channel_id,
                "upload": True,
                "content_kind": content_kind,
                "trigger_source": "upload_gap_recovery",
                "recovery_stage": max(2, int(recovery_stage or 2)),
            },
            misfire_grace_time=600,
            max_instances=1,
        )
        return run_at

    def _restore_failed_recovery_jobs(
        self,
        scheduled_channels: list[ChannelConfig],
        upload: bool,
    ) -> None:
        if not upload:
            return
        state = self._load_continuity_state()
        state_channels = state.get("channels", {})
        if not isinstance(state_channels, dict):
            return
        for channel in scheduled_channels:
            entry = state_channels.get(channel.id, {})
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "") not in {
                "recovery_failed",
                "scheduled_build_failed",
                "worker_finished_without_upload",
                "recovery_retry_scheduled",
            }:
                continue
            if str(entry.get("last_failure_category") or "") not in self.REPAIRABLE_BUILD_FAILURES:
                continue
            last_failure_raw = str(entry.get("last_failure_at") or "")
            latest_upload_raw = str(entry.get("latest_successful_upload_at") or "")
            try:
                last_failure = datetime.fromisoformat(last_failure_raw)
            except ValueError:
                continue
            try:
                latest_upload = datetime.fromisoformat(latest_upload_raw)
            except ValueError:
                latest_upload = None
            if latest_upload is not None and latest_upload >= last_failure:
                continue
            content_kind = str(entry.get("last_failure_content_kind") or "").lower()
            if content_kind not in {"short", "video"}:
                log_name = Path(str(entry.get("last_failure_log") or "")).name.lower()
                content_kind = "video" if "_video_" in log_name else "short"
            next_stage = min(4, max(2, int(entry.get("recovery_stage") or 1) + 1))
            run_at = self._schedule_delayed_recovery(
                channel.id,
                content_kind,
                upload=True,
                recovery_stage=next_stage,
                delay_seconds=30,
            )
            if run_at is not None:
                self._update_continuity_state(
                    channel.id,
                    status="recovery_retry_scheduled",
                    recovery_stage=next_stage,
                    next_recovery_at=run_at.isoformat(),
                    last_failure_content_kind=content_kind,
                )

    def _run_upload_gap_monitor(self, channel_ids: list[str], upload: bool) -> None:
        if not upload:
            return

        now = now_in_tz(self.factory.config.app.timezone)
        state = self._load_continuity_state()
        state_channels = state.get("channels", {})
        for channel_id in channel_ids:
            channel = self.factory._channel(channel_id)
            threshold_hours = max(1.0, float(channel.max_upload_gap_hours or 4.0))
            cooldown_minutes = max(15, int(channel.upload_gap_retry_cooldown_minutes or 75))
            latest = self._latest_successful_upload_at(channel_id)
            gap_hours = (now - latest).total_seconds() / 3600.0 if latest else None
            common_state = {
                "last_checked_at": now.isoformat(),
                "latest_successful_upload_at": latest.isoformat() if latest else None,
                "gap_hours": round(gap_hours, 2) if gap_hours is not None else None,
                "threshold_hours": threshold_hours,
            }

            if gap_hours is not None and gap_hours <= threshold_hours:
                self._update_continuity_state(
                    channel_id,
                    status="healthy",
                    consecutive_failures=0,
                    recovery_stage=0,
                    last_failure_category="",
                    last_failure_summary="",
                    **common_state,
                )
                continue
            if not channel.youtube or not channel.youtube.upload_enabled:
                self._update_continuity_state(channel_id, status="youtube_disabled", **common_state)
                continue
            if self.factory._daily_upload_cap_reached(channel):
                self._update_continuity_state(channel_id, status="daily_cap_reached", **common_state)
                continue
            upload_block = self.factory._current_upload_block(channel_id)
            if upload_block:
                self._update_continuity_state(
                    channel_id,
                    status="upload_blocked",
                    upload_block=upload_block,
                    **common_state,
                )
                continue

            entry = state_channels.get(channel_id, {}) if isinstance(state_channels, dict) else {}
            consecutive_failures = max(0, int(entry.get("consecutive_failures") or 0))
            last_failure_category = str(entry.get("last_failure_category") or "")
            if last_failure_category in {"upload_auth", "upload_quota"}:
                self._update_continuity_state(
                    channel_id,
                    status=f"recovery_blocked_{last_failure_category}",
                    **common_state,
                )
                continue

            effective_cooldown_minutes = self._effective_recovery_cooldown_minutes(
                cooldown_minutes,
                consecutive_failures,
            )
            last_attempt_raw = str(entry.get("last_recovery_scheduled_at") or "")
            try:
                last_attempt = datetime.fromisoformat(last_attempt_raw)
                if last_attempt.tzinfo is None:
                    last_attempt = last_attempt.replace(tzinfo=ZoneInfo(self.factory.config.app.timezone))
            except ValueError:
                last_attempt = None
            if last_attempt and now - last_attempt < timedelta(minutes=effective_cooldown_minutes):
                self._update_continuity_state(
                    channel_id,
                    status="recovery_cooldown",
                    effective_cooldown_minutes=effective_cooldown_minutes,
                    **common_state,
                )
                continue

            lock = self._channel_build_locks.setdefault(channel_id, threading.Lock())
            if lock.locked():
                self._update_continuity_state(channel_id, status="build_in_progress", **common_state)
                continue

            recovery_job_id = f"upload_gap_recovery_{channel_id}"
            delayed_short_job_id = self._delayed_recovery_job_id(channel_id, "short")
            if self.scheduler.get_job(recovery_job_id) or self.scheduler.get_job(delayed_short_job_id):
                self._update_continuity_state(channel_id, status="recovery_already_queued", **common_state)
                continue

            self.factory.logger.warning(
                channel_id,
                "Upload continuity gap detected "
                f"({gap_hours:.1f}h > {threshold_hours:.1f}h)." if gap_hours is not None
                else "No successful upload history found; scheduling a continuity recovery Short.",
            )
            recovery_stage = min(3, max(1, consecutive_failures + 1))
            self.scheduler.add_job(
                self._run_main_job,
                trigger=DateTrigger(run_date=now, timezone=ZoneInfo(self.factory.config.app.timezone)),
                id=recovery_job_id,
                replace_existing=False,
                kwargs={
                    "channel_id": channel_id,
                    "upload": True,
                    "content_kind": "short",
                    "trigger_source": "upload_gap_recovery",
                    "recovery_stage": recovery_stage,
                },
                misfire_grace_time=600,
            )
            self._update_continuity_state(
                channel_id,
                status="recovery_scheduled",
                last_recovery_scheduled_at=now.isoformat(),
                recovery_stage=recovery_stage,
                effective_cooldown_minutes=effective_cooldown_minutes,
                **common_state,
            )

    def _register_housekeeping_jobs(
        self,
        upload: bool = False,
        scheduled_channels: list[ChannelConfig] | None = None,
    ) -> None:
        cleanup_job_id = "global_cleanup_7days"
        if not self.scheduler.get_job(cleanup_job_id):
            self.scheduler.add_job(
                self.factory.cleanup_old_outputs,
                trigger=IntervalTrigger(days=1, timezone=ZoneInfo(self.factory.config.app.timezone)),
                id=cleanup_job_id,
                replace_existing=True,
                kwargs={"days_old": 7},
            )

        heartbeat_job_id = "scheduler_heartbeat"
        if not self.scheduler.get_job(heartbeat_job_id):
            self.scheduler.add_job(
                self._write_heartbeat,
                trigger=IntervalTrigger(minutes=1, timezone=ZoneInfo(self.factory.config.app.timezone)),
                id=heartbeat_job_id,
                replace_existing=True,
                kwargs={"status": "running", "note": "tick"},
            )

        if upload and scheduled_channels:
            continuity_job_id = "upload_gap_monitor"
            check_minutes = max(
                2,
                int(getattr(self.factory.config.app, "upload_gap_check_minutes", 10) or 10),
            )
            if not self.scheduler.get_job(continuity_job_id):
                self.scheduler.add_job(
                    self._run_upload_gap_monitor,
                    trigger=IntervalTrigger(
                        minutes=check_minutes,
                        timezone=ZoneInfo(self.factory.config.app.timezone),
                    ),
                    id=continuity_job_id,
                    replace_existing=True,
                    kwargs={
                        "channel_ids": [channel.id for channel in scheduled_channels],
                        "upload": True,
                    },
                    next_run_time=now_in_tz(self.factory.config.app.timezone) + timedelta(seconds=30),
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=600,
                )
            self._restore_failed_recovery_jobs(scheduled_channels, upload=True)

    def _run_backlog_job(self, channel_id: str, upload: bool) -> None:
        self._write_heartbeat("running", f"backlog:{channel_id}:start")
        self.factory.logger.info(channel_id, "Checking backlog (staggered)...")
        success = self.factory.replay_backlog_once(channel_id)
        if success:
            self.factory.logger.success(channel_id, "Backlog item processed.")
        self._write_heartbeat("running", f"backlog:{channel_id}:done")

    def _run_main_job(
        self,
        channel_id: str,
        upload: bool,
        content_kind: str = "short",
        trigger_source: str = "scheduled",
        recovery_stage: int = 0,
    ) -> bool:
        build_lock = self._channel_build_locks.setdefault(channel_id, threading.Lock())
        if not build_lock.acquire(blocking=False):
            self.factory.logger.info(
                channel_id,
                f"Skipping overlapping {content_kind} build ({trigger_source}); another channel build is active.",
            )
            self._write_heartbeat("running", f"build:{channel_id}:{content_kind}:busy")
            return False

        self._write_heartbeat("running", f"build:{channel_id}:{content_kind}:start:{trigger_source}")
        channel = self.factory._channel(channel_id)
        try:
            if upload and self.factory._daily_upload_cap_reached(channel):
                cap = int(channel.daily_upload_cap or 0)
                self.factory.logger.info(channel_id, f"Skipping new generation: daily upload cap reached ({cap}).")
                self._write_heartbeat("running", f"build:{channel_id}:{content_kind}:cap")
                return False

            upload_block = self.factory._current_upload_block(channel_id) if upload else None
            if upload_block:
                reason = upload_block.get("reason", "upload_block")
                until = upload_block.get("blocked_until", "unknown")
                self.factory.logger.info(channel_id, f"Skipping new generation: uploads blocked by {reason} until {until}.")
                self._write_heartbeat("running", f"build:{channel_id}:{content_kind}:blocked")
                return False

            if upload:
                self.factory.replay_backlog_once(channel_id)
            pending_count = sum(
                1
                for item in self.factory._load_backlog(channel_id)
                if item.get("status") in {"pending", "failed"}
            )
            if channel_id == "brain_lens" and pending_count > 0:
                self.factory.logger.info(
                    channel_id,
                    f"Skipping new video generation: {pending_count} backlog videos still remaining.",
                )
                self._write_heartbeat("running", f"build:{channel_id}:{content_kind}:backlog_wait")
                return False

            previous_upload = self._latest_successful_upload_at(channel_id) if upload else None
            self.factory.logger.info(
                channel_id,
                f"Starting {trigger_source} {content_kind} build in an isolated worker...",
            )
            result = self._execute_build_subprocess(
                channel,
                upload,
                content_kind,
                trigger_source=trigger_source,
                recovery_stage=recovery_stage,
            )
            effective_trigger_source = trigger_source
            if (
                not result.success
                and upload
                and trigger_source == "scheduled"
                and result.failure_category in self.REPAIRABLE_BUILD_FAILURES
            ):
                latest_after_failure = self._latest_successful_upload_at(channel_id)
                if latest_after_failure is None or previous_upload is None or latest_after_failure <= previous_upload:
                    self.factory.logger.warning(
                        channel_id,
                        f"Scheduled {content_kind} build found {result.failure_category}; "
                        "starting one bounded continuity replacement now.",
                    )
                    effective_trigger_source = "upload_gap_recovery"
                    recovery_stage = 1
                    result = self._execute_build_subprocess(
                        channel,
                        upload,
                        content_kind,
                        trigger_source=effective_trigger_source,
                        recovery_stage=recovery_stage,
                    )
            if (
                not result.success
                and effective_trigger_source == "upload_gap_recovery"
                and int(recovery_stage or 1) < 2
                and result.failure_category in self.REPAIRABLE_BUILD_FAILURES
            ):
                self.factory.logger.warning(
                    channel_id,
                    f"Recovery stage 1 found {result.failure_category}; "
                    "starting one targeted stage-2 retry now.",
                )
                result = self._execute_build_subprocess(
                    channel,
                    upload,
                    content_kind,
                    trigger_source=effective_trigger_source,
                    recovery_stage=2,
                )
                recovery_stage = 2
            if not result.success:
                continuity = self._load_continuity_state()
                entry = continuity.get("channels", {}).get(channel_id, {})
                consecutive_failures = max(0, int(entry.get("consecutive_failures") or 0)) + 1
                next_recovery_at = None
                if upload and result.failure_category in self.REPAIRABLE_BUILD_FAILURES:
                    cooldown_minutes = self._effective_recovery_cooldown_minutes(
                        int(channel.upload_gap_retry_cooldown_minutes or 20),
                        consecutive_failures,
                    )
                    next_recovery_at = self._schedule_delayed_recovery(
                        channel_id,
                        content_kind,
                        upload=True,
                        recovery_stage=min(4, max(2, int(recovery_stage or 1) + 1)),
                        delay_minutes=cooldown_minutes,
                    )
                self._update_continuity_state(
                    channel_id,
                    status="recovery_retry_scheduled" if next_recovery_at else (
                        "recovery_failed" if effective_trigger_source == "upload_gap_recovery" else "scheduled_build_failed"
                    ),
                    consecutive_failures=consecutive_failures,
                    recovery_stage=max(0, int(recovery_stage or 0)),
                    last_failure_at=now_in_tz(self.factory.config.app.timezone).isoformat(),
                    last_failure_category=result.failure_category,
                    last_failure_summary=result.failure_summary,
                    last_failure_log=str(result.log_path),
                    last_failure_content_kind=content_kind,
                    next_recovery_at=next_recovery_at.isoformat() if next_recovery_at else None,
                )
                return False

            latest_upload = self._latest_successful_upload_at(channel_id) if upload else None
            uploaded = bool(
                upload
                and latest_upload
                and (previous_upload is None or latest_upload > previous_upload)
            )
            if upload and not uploaded:
                self.factory.logger.warning(
                    channel_id,
                    f"{content_kind.title()} worker finished without a successful upload; "
                    "the continuity monitor will retry after cooldown.",
                )
                continuity = self._load_continuity_state()
                entry = continuity.get("channels", {}).get(channel_id, {})
                self._update_continuity_state(
                    channel_id,
                    status="worker_finished_without_upload",
                    consecutive_failures=max(0, int(entry.get("consecutive_failures") or 0)) + 1,
                    recovery_stage=max(0, int(recovery_stage or 0)),
                    last_failure_at=now_in_tz(self.factory.config.app.timezone).isoformat(),
                    last_failure_category="no_upload",
                    last_failure_summary="worker completed but no new successful upload was recorded",
                    last_failure_log=str(result.log_path),
                )
            else:
                if upload and uploaded:
                    delayed_job = self.scheduler.get_job(
                        self._delayed_recovery_job_id(channel_id, content_kind)
                    )
                    if delayed_job is not None:
                        self.scheduler.remove_job(delayed_job.id)
                    self._update_continuity_state(
                        channel_id,
                        status="healthy",
                        consecutive_failures=0,
                        recovery_stage=0,
                        last_failure_category="",
                        last_failure_summary="",
                        latest_successful_upload_at=latest_upload.isoformat() if latest_upload else None,
                        gap_hours=0.0,
                    )
                self.factory.logger.success(channel_id, "Main build completed.")
            return result.success
        finally:
            self._write_heartbeat("running", f"build:{channel_id}:{content_kind}:done")
            build_lock.release()

    def register_jobs(self, upload: bool | None = None, channels: list[str] | None = None, mode: str = "interval") -> None:
        if upload is None:
            upload = bool(self.factory.config.app.schedule_uploads)
        channel_ids = set(channels) if channels else None

        scheduled_channels = [channel for channel in self.factory.config.channels if not channel_ids or channel.id in channel_ids]

        if mode == "cron":
            # Cron mode remains as is for specific times
            for channel in scheduled_channels:
                for hhmm, content_kind in self._channel_slots(channel):
                    hour, minute = hhmm.split(":")
                    trigger = CronTrigger(hour=int(hour), minute=int(minute), timezone=ZoneInfo(self.factory.config.app.timezone))
                    job_id = f"{channel.id}_{content_kind}_{hour}_{minute}"
                    self.scheduler.add_job(
                        self._run_main_job,
                        trigger=trigger,
                        id=job_id,
                        replace_existing=True,
                        kwargs={
                            "channel_id": channel.id,
                            "upload": upload,
                            "content_kind": content_kind,
                            "trigger_source": "scheduled",
                        },
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=900,
                    )
            self._register_housekeeping_jobs(upload=upload, scheduled_channels=scheduled_channels)
            return

        # interval mode with dual timers
        for channel in scheduled_channels:
            # 1. Main generation job (typically 1h)
            main_hours = channel.schedule_interval_hours or self.factory.config.app.schedule_interval_hours or 2
            main_job_id = f"{channel.id}_main_{main_hours}h"
            self.scheduler.add_job(
                self._run_main_job,
                trigger=IntervalTrigger(hours=main_hours, timezone=ZoneInfo(self.factory.config.app.timezone)),
                id=main_job_id,
                replace_existing=True,
                kwargs={"channel_id": channel.id, "upload": upload, "content_kind": "short"},
            )

            # 2. Backlog job (typically 30m)
            if channel.backlog_schedule_interval_minutes:
                backlog_job_id = f"{channel.id}_backlog_{channel.backlog_schedule_interval_minutes}m"
                self.scheduler.add_job(
                    self._run_backlog_job,
                    trigger=IntervalTrigger(minutes=channel.backlog_schedule_interval_minutes, timezone=ZoneInfo(self.factory.config.app.timezone)),
                    id=backlog_job_id,
                    replace_existing=True,
                    kwargs={"channel_id": channel.id, "upload": upload},
                )
                
        # 3. Global cleanup + heartbeat jobs
        self._register_housekeeping_jobs(upload=upload, scheduled_channels=scheduled_channels)

    def run(self, upload: bool | None = None, channels: list[str] | None = None, mode: str = "interval") -> None:
        self._acquire_singleton_lock()
        if upload is None:
            upload = bool(self.factory.config.app.schedule_uploads)
        channel_ids = set(channels) if channels else None

        try:
            self.register_jobs(upload=upload, channels=channels, mode=mode)
            self._write_heartbeat("running", "initialized")
            mode_label = f"{mode} build + upload" if upload else f"{mode} build only"
            self.factory.logger.header(f"Scheduler Initialized: {mode_label}")
            
            # Initialize backlogs for all channels
            scheduled_channels = [channel for channel in self.factory.config.channels if not channel_ids or channel.id in channel_ids]
            for channel in scheduled_channels:
                self.factory.logger.info(channel.id, "Initializing backlog...")
                items = self.factory.initialize_backlog(channel.id)
                pending_count = sum(1 for item in items if item.get("status") in {"pending", "failed"})
                if pending_count > 0 or channel.id == "brain_lens":
                    self.factory.logger.backlog_status(channel.id, pending_count)

            self.factory.logger.subheader("Planned Tasks")
            for job in self.scheduler.get_jobs():
                print(f"  - {job}")

            if mode == "interval":
                self.factory.logger.subheader("Executing Immediate First Slots (Build + Upload)")
                for index, channel in enumerate(scheduled_channels):
                    self.factory.logger.info(channel.id, "Starting immediate tasks...")
                    # 1. Start new generation (YT + Add to FB Backlog)
                    self._run_main_job(channel_id=channel.id, upload=upload, content_kind="short")
                    
                    # 2. Start backlog upload (FB)
                    self._run_backlog_job(channel_id=channel.id, upload=upload)
                    
                    self.factory.logger.success(channel.id, "Immediate tasks initiated.")

                    if index < len(scheduled_channels) - 1:
                        self.factory.logger.info("SYSTEM", "Waiting 20s between channels for safety...")
                        sleep(20)
            else:
                self.factory.logger.subheader("Cron mode active: waiting for optimized posting windows")

            now = datetime.now(ZoneInfo(self.factory.config.app.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
            self.factory.logger.header(f"Bot Active at {now}")
            self._write_heartbeat("running", "active")
            self.scheduler.start()
        finally:
            self._write_heartbeat("stopped", "scheduler exited")
            self._release_singleton_lock()
