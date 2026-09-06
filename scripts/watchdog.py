from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
STATE = ROOT / "data" / "state"
DASH_PORT = int(os.environ.get("YT_DASHBOARD_PORT", "8787"))


def _write_watchdog_heartbeat(status: str = "running", note: str = "tick") -> None:
    """Persist a small liveness marker for the external service guard."""
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "watchdog_heartbeat.json"
    payload = {
        "pid": os.getpid(),
        "status": status,
        "note": note,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _run_powershell(command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _matching_python_process(pattern: str) -> list[int]:
    escaped = pattern.replace("'", "''")
    command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -eq 'python.exe' -and $_.CommandLine -match '{escaped}' }} | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json"
    )
    raw = _run_powershell(command)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [int(pid) for pid in parsed if str(pid).isdigit()]
    return [int(parsed)] if str(parsed).isdigit() else []


def _dashboard_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", DASH_PORT)) == 0


def _heartbeat_age_seconds() -> int | None:
    path = STATE / "scheduler_heartbeat.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        updated = datetime.fromisoformat(str(data.get("updated_at")))
        now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
        return max(0, int((now - updated).total_seconds()))
    except Exception:
        return None


def _continuity_age_seconds() -> int | None:
    path = STATE / "upload_continuity_state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        updated = datetime.fromisoformat(str(data.get("updated_at")))
        now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
        return max(0, int((now - updated).total_seconds()))
    except Exception:
        return 10**9


def _uploads_enabled() -> bool:
    return str(os.environ.get("YT_WATCHDOG_ALLOW_UPLOAD", "0")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def _active_build_pids() -> list[int]:
    active_dir = STATE / "active_builds"
    pids: list[int] = []
    for path in active_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            pid = int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if pid > 0:
            pids.append(pid)
    return sorted(set(pids))


def _clear_active_build_markers() -> None:
    for path in (STATE / "active_builds").glob("*.json"):
        try:
            path.unlink()
        except OSError:
            continue


def _start_scheduler() -> None:
    # A watchdog must never turn a monitoring restart into an unreviewed
    # publication action.  Uploads require an explicit operator opt-in.
    allow_upload = _uploads_enabled()
    command = [str(PYTHON), "run.py", "schedule", "--mode", "cron"]
    command.append("--upload" if allow_upload else "--no-upload")
    subprocess.Popen(
        command,
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _start_dashboard() -> None:
    env = os.environ.copy()
    env["YT_DASHBOARD_HOST"] = "0.0.0.0"
    env["YT_DASHBOARD_PORT"] = str(DASH_PORT)
    subprocess.Popen(
        [str(PYTHON), "scripts/live_dashboard_server.py", "--no-open"],
        cwd=str(ROOT),
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _terminate_process_trees(pids: list[int]) -> None:
    for pid in sorted({int(value) for value in pids if int(value) > 0}):
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=20,
            )
        else:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                continue


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_single_instance_lock() -> Path | None:
    """Create a PID lock atomically, reclaiming it only after a crash."""
    path = STATE / "watchdog.lock"
    payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now().astimezone().isoformat()})
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8-sig"))
                owner_pid = int(existing.get("pid") or 0)
                if _pid_is_running(owner_pid):
                    return None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            try:
                path.unlink()
            except OSError:
                return None
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            return path
    return None


def main() -> int:
    if not PYTHON.exists():
        print(f"Python not found: {PYTHON}")
        return 1
    STATE.mkdir(parents=True, exist_ok=True)
    lock_path = _acquire_single_instance_lock()
    if lock_path is None:
        print("YT watchdog already running.")
        return 0
    _write_watchdog_heartbeat(note="started")
    print("YT watchdog active. It will restart scheduler/dashboard if they stop.")
    stale_scheduler_checks = 0
    stale_continuity_checks = 0
    continuity_restart_grace_until = time.time() + 900
    while True:
        try:
            _write_watchdog_heartbeat(note="checking")
            scheduler_pids = _matching_python_process(r"run.py schedule")
            heartbeat_age = _heartbeat_age_seconds()
            if not scheduler_pids:
                stale_scheduler_checks = 0
                stale_continuity_checks = 0
                orphan_pids = _active_build_pids()
                if orphan_pids:
                    print(
                        f"[{datetime.now():%H:%M:%S}] Scheduler is absent; "
                        f"terminating orphaned build process trees {orphan_pids}."
                    )
                    _terminate_process_trees(orphan_pids)
                    _clear_active_build_markers()
                    time.sleep(2)
                _start_scheduler()
                continuity_restart_grace_until = time.time() + 900
                print(f"[{datetime.now():%H:%M:%S}] Scheduler restart requested.")
                time.sleep(12)
            elif heartbeat_age is None or heartbeat_age > 420:
                stale_scheduler_checks += 1
                stale_continuity_checks = 0
                if stale_scheduler_checks >= 2:
                    print(
                        f"[{datetime.now():%H:%M:%S}] Scheduler heartbeat remained stale; "
                        f"restarting process tree {scheduler_pids}."
                    )
                    _terminate_process_trees(scheduler_pids)
                    _clear_active_build_markers()
                    time.sleep(3)
                    _start_scheduler()
                    stale_scheduler_checks = 0
                    continuity_restart_grace_until = time.time() + 900
                    time.sleep(12)
                else:
                    print(
                        f"[{datetime.now():%H:%M:%S}] Scheduler heartbeat stale "
                        f"({heartbeat_age}s); confirming once before restart."
                    )
            else:
                stale_scheduler_checks = 0
                continuity_age = _continuity_age_seconds()
                build_pids = _matching_python_process(r"run.py build")
                continuity_limit = 3600 if build_pids else 1800
                continuity_stale = (
                    _uploads_enabled()
                    and time.time() >= continuity_restart_grace_until
                    and continuity_age is not None
                    and continuity_age > continuity_limit
                )
                if continuity_stale:
                    stale_continuity_checks += 1
                    if stale_continuity_checks >= 2:
                        print(
                            f"[{datetime.now():%H:%M:%S}] Upload-gap monitor remained stale "
                            f"({continuity_age}s); restarting scheduler process tree {scheduler_pids}."
                        )
                        _terminate_process_trees(scheduler_pids)
                        _clear_active_build_markers()
                        time.sleep(3)
                        _start_scheduler()
                        stale_continuity_checks = 0
                        continuity_restart_grace_until = time.time() + 900
                        time.sleep(12)
                    else:
                        print(
                            f"[{datetime.now():%H:%M:%S}] Upload-gap monitor stale "
                            f"({continuity_age}s); confirming once before restart."
                        )
                else:
                    stale_continuity_checks = 0

            dashboard_pids = _matching_python_process(r"live_dashboard_server.py")
            port_open = _dashboard_port_open()
            if not port_open and not dashboard_pids:
                _start_dashboard()
                print(f"[{datetime.now():%H:%M:%S}] Dashboard restart requested.")
                time.sleep(4)
            elif port_open and len(dashboard_pids) > 1:
                # Keep the newest dashboard PID; stop extras so POST APIs stay on one process.
                extras = sorted(dashboard_pids)[:-1]
                print(
                    f"[{datetime.now():%H:%M:%S}] Extra dashboard PIDs {extras}; "
                    "terminating duplicates."
                )
                _terminate_process_trees(extras)
        except Exception as exc:
            _write_watchdog_heartbeat(status="warning", note=type(exc).__name__)
            print(f"[{datetime.now():%H:%M:%S}] Watchdog warning: {exc}")
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
