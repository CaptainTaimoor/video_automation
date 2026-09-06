from __future__ import annotations

import json
import os
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
WATCHDOG_SCRIPT = ROOT / "scripts" / "watchdog.py"
WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS = 150


def _write_heartbeat(status: str = "running", note: str = "checking") -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "service_guard_heartbeat.json"
    payload = {
        "pid": os.getpid(),
        "status": status,
        "note": note,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _log(message: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "service_guard.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def _watchdog_pids() -> list[int]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'scripts\\\\watchdog.py|scripts/watchdog.py' } | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        raw = result.stdout.strip()
        if not raw:
            return []
        values = json.loads(raw)
        if not isinstance(values, list):
            values = [values]
        return [int(value) for value in values if str(value).isdigit()]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return []


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


def _watchdog_lock_pid() -> int:
    try:
        payload = json.loads((STATE / "watchdog.lock").read_text(encoding="utf-8-sig"))
        return int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _watchdog_heartbeat_age_seconds() -> int | None:
    try:
        payload = json.loads(
            (STATE / "watchdog_heartbeat.json").read_text(encoding="utf-8-sig")
        )
        updated = datetime.fromisoformat(str(payload.get("updated_at")))
        now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
        return max(0, int((now - updated).total_seconds()))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _watchdog_status() -> tuple[bool, list[int], str]:
    owner_pid = _watchdog_lock_pid()
    heartbeat_age = _watchdog_heartbeat_age_seconds()
    if _pid_is_running(owner_pid):
        if heartbeat_age is not None and heartbeat_age <= WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS:
            return True, [owner_pid], f"lock_owner:heartbeat:{heartbeat_age}s"
        evidence = (
            f"stale_heartbeat:{heartbeat_age}s"
            if heartbeat_age is not None
            else "missing_heartbeat"
        )
        return False, [owner_pid], evidence
    watchdog_pids = _watchdog_pids()
    if watchdog_pids:
        return True, watchdog_pids, "process_scan"
    if owner_pid > 0:
        return False, [], "stale_lock"
    if heartbeat_age is not None and heartbeat_age <= WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS:
        return True, [], f"heartbeat:{heartbeat_age}s"
    return False, [], "absent"


def _start_watchdog() -> None:
    environment = os.environ.copy()
    environment["YT_WATCHDOG_ALLOW_UPLOAD"] = "1"
    subprocess.Popen(
        [str(PYTHON), str(WATCHDOG_SCRIPT)],
        cwd=str(ROOT),
        env=environment,
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


def _acquire_lock() -> Path | None:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "service_guard.lock"
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
    lock_path = _acquire_lock()
    if lock_path is None:
        return 0
    _write_heartbeat(note="started")
    _log("Service guard started.")
    try:
        while True:
            watchdog_live, watchdog_pids, evidence = _watchdog_status()
            if not watchdog_live:
                if watchdog_pids:
                    _terminate_process_trees(watchdog_pids)
                    _log(
                        f"Watchdog was unhealthy ({evidence}); terminated process tree "
                        f"{watchdog_pids}."
                    )
                    time.sleep(2)
                _start_watchdog()
                _log(f"Watchdog restart requested ({evidence}).")
                _write_heartbeat(note=f"watchdog_restart_requested:{evidence}")
                time.sleep(5)
            else:
                pid_note = f":{','.join(map(str, watchdog_pids))}" if watchdog_pids else ""
                _write_heartbeat(note=f"watchdog_live:{evidence}{pid_note}")
            time.sleep(30)
    except KeyboardInterrupt:
        _write_heartbeat(status="stopped", note="operator_interrupt")
        return 0
    except Exception as exc:
        _write_heartbeat(status="warning", note=type(exc).__name__)
        _log(f"Service guard warning: {exc}")
        return 1
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
