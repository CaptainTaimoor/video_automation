from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import socket
try:
    import yaml
except Exception:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "state"
OUTPUT = ROOT / "output"
CHANNELS = [
    {
        "id": "ancient_history",
        "name": "Secrets of Time",
        "accent": "#d4a017",
        "tagline": "Ancient empires, lost cities, and history that still shocks",
    },
    {
        "id": "brain_lens",
        "name": "Brain Lens",
        "accent": "#2dd4bf",
        "tagline": "Relationship psychology, attraction science, and human behavior",
    },
]

RECOVERY_PROBLEM_STATUSES = {
    "recovery_failed",
    "scheduled_build_failed",
    "worker_finished_without_upload",
    "recovery_already_queued",
}


def _duration_label(min_seconds, max_seconds, fallback="-"):
    try:
        lo = int(min_seconds) if min_seconds is not None else None
        hi = int(max_seconds) if max_seconds is not None else None
    except Exception:
        return fallback
    if lo is None and hi is None:
        return fallback
    if lo is None:
        lo = hi
    if hi is None:
        hi = lo
    if lo >= 120 or hi >= 120:
        lo_m = max(1, round(lo / 60))
        hi_m = max(lo_m, round(hi / 60))
        if lo_m == hi_m:
            return f"{lo_m} min"
        return f"{lo_m}–{hi_m} min"
    if lo == hi:
        return f"{lo}s"
    return f"{lo}–{hi}s"


def settings_summary():
    if not yaml:
        return {}
    path = ROOT / "config" / "settings.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    for channel in data.get("channels", []) or []:
        cid = channel.get("id")
        profiles = channel.get("content_profiles", {}) or {}
        shorts = profiles.get("shorts", {}) or {}
        videos = profiles.get("videos", {}) or {}
        presenter = channel.get("presenter", {}) or {}
        heygen = channel.get("heygen", {}) or {}
        out[cid] = {
            "display_name": channel.get("display_name") or cid,
            "niche": channel.get("niche_description") or "",
            "shorts_per_day": len(shorts.get("schedule_times", []) or channel.get("schedule_times", []) or []),
            "longs_per_day": len(videos.get("schedule_times", []) or []) if videos.get("enabled", False) else 0,
            "daily_upload_cap": int(channel.get("daily_upload_cap", 0) or 0),
            "max_upload_gap_hours": float(channel.get("max_upload_gap_hours", 4.0) or 4.0),
            "presenter": "local" if presenter.get("enabled") else "off",
            "heygen": "on" if heygen.get("enabled") else "off",
            "short_times": shorts.get("schedule_times", []) or channel.get("schedule_times", []) or [],
            "long_times": videos.get("schedule_times", []) or [],
            "short_duration": _duration_label(
                shorts.get("min_duration_seconds"),
                shorts.get("max_duration_seconds"),
                "50–59s",
            ),
            "long_duration": _duration_label(
                videos.get("min_duration_seconds"),
                videos.get("max_duration_seconds"),
                "8–10 min",
            ),
        }
    return out


def read_json(path: Path, fallback):
    try:
        if not path.exists():
            return fallback
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return fallback


def read_jsonl_tail(path: Path, limit: int = 20):
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def short_text(value, limit=84):
    text = str(value or "-")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_run_time(run_dir):
    match = re.search(r"\\(\d{8})_(\d{6})(?:\\)?$", str(run_dir or ""))
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    except Exception:
        return None


def human_age(dt):
    if not dt:
        return "-"
    seconds = max(0, int((datetime.now() - dt).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def iso_age_seconds(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0, int((now - dt).total_seconds()))
    except Exception:
        return None


def format_next_time(value):
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = int((dt - now).total_seconds())
        clock = dt.strftime("%H:%M")
        if delta < -120:
            return f"{clock} (passed)"
        if delta < 0:
            return f"{clock} (due)"
        if delta < 3600:
            return f"{clock} (in {max(1, delta // 60)}m)"
        if delta < 86400:
            hours = delta // 3600
            mins = (delta % 3600) // 60
            return f"{clock} (in {hours}h {mins}m)" if mins else f"{clock} (in {hours}h)"
        return dt.strftime("%a %H:%M")
    except Exception:
        return short_text(value, 32)


def friendly_job_label(job_id: str):
    raw = str(job_id or "").strip()
    if not raw:
        return "Scheduled job"
    lower = raw.lower()
    if lower == "scheduler_heartbeat":
        return "Scheduler heartbeat"
    if lower == "upload_gap_monitor":
        return "Upload gap monitor"
    if lower.startswith("global_cleanup"):
        return "Global cleanup"
    if "post_failure_recovery" in lower:
        channel = "Secrets of Time" if "ancient_history" in lower else "Brain Lens" if "brain_lens" in lower else "Channel"
        kind = "long" if lower.endswith("_video") or "_video_" in lower else "short"
        return f"{channel} recovery {kind}"
    channel = None
    if lower.startswith("ancient_history"):
        channel = "Secrets of Time"
        rest = lower[len("ancient_history_") :]
    elif lower.startswith("brain_lens"):
        channel = "Brain Lens"
        rest = lower[len("brain_lens_") :]
    else:
        rest = lower
    kind = "Short"
    if rest.startswith("video"):
        kind = "Long"
        rest = rest[len("video_") :] if rest.startswith("video_") else rest[len("video") :]
    elif rest.startswith("short"):
        kind = "Short"
        rest = rest[len("short_") :] if rest.startswith("short_") else rest[len("short") :]
    time_match = re.search(r"(\d{1,2})_(\d{2})", rest or "")
    when = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    if channel and when:
        return f"{channel} {kind.lower()} @ {when}"
    if channel:
        return f"{channel} {kind.lower()}"
    return raw.replace("_", " ")


def upcoming_jobs(heartbeat, channel_id=None, limit=6):
    jobs = heartbeat.get("jobs", []) if isinstance(heartbeat, dict) else []
    if not isinstance(jobs, list):
        return []
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        # Channel view: only jobs that mention this channel id.
        if channel_id and channel_id not in job_id:
            continue
        next_run = job.get("next_run_time")
        sort_key = datetime.max
        try:
            sort_key = datetime.fromisoformat(str(next_run))
            if sort_key.tzinfo:
                sort_key = sort_key.replace(tzinfo=None)
        except Exception:
            pass
        rows.append(
            {
                "id": job_id,
                "label": friendly_job_label(job_id),
                "next_run_time": next_run,
                "when": format_next_time(next_run),
                "_sort": sort_key,
            }
        )
    rows.sort(key=lambda item: item["_sort"])
    cleaned = []
    for row in rows[: max(1, int(limit or 6))]:
        cleaned.append({k: v for k, v in row.items() if k != "_sort"})
    return cleaned


def explain_channel(channel, backlog, uploads, recovery, gates, latest_short, latest_long, plan):
    name = channel.get("name") or channel.get("id") or "Channel"
    gap = recovery.get("gap_hours")
    if gap is None:
        gap = (uploads.get("last_upload") or {}).get("gap_hours")
    try:
        gap = float(gap) if gap is not None else None
    except Exception:
        gap = None
    threshold = recovery.get("threshold_hours")
    if threshold is None:
        threshold = plan.get("max_upload_gap_hours", 4.0)
    try:
        threshold = float(threshold)
    except Exception:
        threshold = 4.0
    status = str(recovery.get("status") or "").strip() or "unknown"
    stage = recovery.get("recovery_stage") or 0
    failures = recovery.get("consecutive_failures") or 0
    category = short_text(recovery.get("last_failure_category") or "", 48)
    summary = short_text(recovery.get("last_failure_summary") or "", 140)
    next_recovery = format_next_time(recovery.get("next_recovery_at"))

    if backlog.get("failed"):
        return {
            "headline": f"{name} has failed uploads waiting",
            "detail": f"{backlog['failed']} failed backlog item(s) need attention before new publishes stack up.",
            "tone": "bad",
        }
    if status in {"recovery_failed", "scheduled_build_failed", "worker_finished_without_upload"}:
        detail_bits = [f"Status: {status.replace('_', ' ')}."]
        if category:
            detail_bits.append(f"Last issue: {category}.")
        if summary:
            detail_bits.append(summary)
        if next_recovery and next_recovery != "-":
            detail_bits.append(f"Next recovery: {next_recovery}.")
        return {
            "headline": f"{name} automatic recovery is active (stage {stage})",
            "detail": " ".join(detail_bits),
            "tone": "bad",
        }
    if status == "recovery_already_queued":
        return {
            "headline": f"{name} recovery job is already queued",
            "detail": (
                f"Upload gap is {gap:.1f}h over the {threshold:.1f}h threshold. "
                f"Stage {stage}, {failures} consecutive failure(s). Next attempt {next_recovery}."
                if gap is not None
                else f"A recovery build is queued at stage {stage}. Next attempt {next_recovery}."
            ),
            "tone": "warn",
        }
    if gates.get("streak", 0) >= 2:
        return {
            "headline": f"{name} quality gate is holding builds",
            "detail": f"Current hold streak is {gates['streak']}. Replacements should run; do not force-upload held videos.",
            "tone": "bad",
        }
    if gap is None:
        return {
            "headline": f"{name} has no successful upload on record",
            "detail": "Confirm auth tokens and wait for the next scheduled short or long.",
            "tone": "bad",
        }
    if gap > max(threshold * 2, 36):
        return {
            "headline": f"{name} upload gap is critical ({gap:.1f}h)",
            "detail": f"Threshold is {threshold:.1f}h. Check scheduler health, auth, and recovery status.",
            "tone": "bad",
        }
    if gap > threshold:
        return {
            "headline": f"{name} upload gap is growing ({gap:.1f}h)",
            "detail": f"Above the {threshold:.1f}h continuity threshold. Recovery monitor should fill the gap soon.",
            "tone": "warn",
        }
    if latest_short.get("status") not in ("ok",) or latest_long.get("status") not in ("ok",):
        return {
            "headline": f"{name} latest render needs a check",
            "detail": (
                f"Short: {latest_short.get('status', '-')}; "
                f"Long: {latest_long.get('status', '-')}."
            ),
            "tone": "warn",
        }
    if backlog.get("pending"):
        return {
            "headline": f"{name} has {backlog['pending']} pending backlog item(s)",
            "detail": "Queue is waiting to upload. Skip anything held by the quality gate.",
            "tone": "warn",
        }
    return {
        "headline": f"{name} looks healthy",
        "detail": (
            f"Last upload {uploads.get('last_upload', {}).get('age', '-')}; "
            f"gap {gap:.1f}h under the {threshold:.1f}h threshold."
        ),
        "tone": "good",
    }


def file_info(path_value):
    if not path_value:
        return {"exists": False, "size_mb": None, "updated": None, "path": None}
    path = Path(path_value)
    try:
        if not path.exists():
            return {"exists": False, "size_mb": None, "updated": None, "path": str(path)}
        stat = path.stat()
        return {
            "exists": True,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "updated": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "path": str(path),
        }
    except Exception:
        return {"exists": False, "size_mb": None, "updated": None, "path": str(path)}


def latest_output(channel_id: str, kind: str):
    base = OUTPUT / channel_id / kind
    expected_name = "short.mp4" if kind == "short" else "video.mp4"
    if not base.exists():
        return {
            "kind": kind,
            "status": "missing",
            "title": "-",
            "file": None,
            "size_mb": None,
            "updated": None,
            "encoder": None,
            "duration_seconds": None,
        }
    try:
        date_dirs = sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)[:7]
        for date_dir in date_dirs:
            run_dirs = sorted([p for p in date_dir.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
            for run_dir in run_dirs:
                video = run_dir / expected_name
                meta = read_json(run_dir / "metadata.json", {})
                if video.exists():
                    info = file_info(video)
                    duration = meta.get("duration_seconds")
                    if duration is None:
                        duration = meta.get("duration")
                    try:
                        duration = float(duration) if duration is not None else None
                    except Exception:
                        duration = None
                    encoder = meta.get("video_encoder") or meta.get("encoder")
                    return {
                        "kind": kind,
                        "status": "ok" if info["size_mb"] and info["size_mb"] > 0.5 else "small",
                        "title": short_text(meta.get("title") or run_dir.name, 92),
                        "run_dir": str(run_dir),
                        "file": info,
                        "size_mb": info.get("size_mb"),
                        "updated": info.get("updated"),
                        "encoder": encoder,
                        "duration_seconds": duration,
                    }
    except Exception as exc:
        return {
            "kind": kind,
            "status": "error",
            "title": str(exc),
            "file": None,
            "size_mb": None,
            "updated": None,
            "encoder": None,
            "duration_seconds": None,
        }
    return {
        "kind": kind,
        "status": "empty",
        "title": "No video found",
        "file": None,
        "size_mb": None,
        "updated": None,
        "encoder": None,
        "duration_seconds": None,
    }


def _is_scheduler_command(command: str) -> bool:
    text = str(command or "").lower()
    if "live_dashboard_server.py" in text:
        return False
    return bool(
        re.search(r"run\.py\s+schedule\b", text)
        or re.search(r"\bschedule\s+runner\b", text)
        or "scheduler_service" in text
    )


def process_rows():
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.Name -match 'python|ffmpeg|node|cmd' "
        "-and $_.CommandLine -match 'run.py schedule|run.py build|schedule runner|ffmpeg|heygen|yt-dlp' "
        "-and $_.CommandLine -notmatch 'live_dashboard_server.py' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 2"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        parsed = json.loads(result.stdout)
        rows = parsed if isinstance(parsed, list) else [parsed]
        return [
            {
                "pid": row.get("ProcessId"),
                "name": row.get("Name"),
                "command": short_text(row.get("CommandLine"), 150),
                "is_scheduler": _is_scheduler_command(row.get("CommandLine")),
            }
            for row in rows
        ]
    except Exception:
        return []


def backlog_summary(channel_id: str):
    data = read_json(STATE / f"{channel_id}_backlog.json", {"items": []})
    items = data.get("items", data if isinstance(data, list) else [])
    if not isinstance(items, list):
        items = []
    by_status = {}
    for item in items:
        status = item.get("status", "unknown") if isinstance(item, dict) else "unknown"
        by_status[status] = by_status.get(status, 0) + 1
    pending = [item for item in items if isinstance(item, dict) and item.get("status") == "pending"][:6]
    failed = [item for item in items if isinstance(item, dict) and item.get("status") == "failed"][:6]
    return {
        "total": len(items),
        "pending": by_status.get("pending", 0),
        "posted": by_status.get("posted", 0),
        "failed": by_status.get("failed", 0),
        "skipped_quality": by_status.get("skipped_quality", 0),
        "pending_items": [
            {
                "title": short_text(item.get("title") or item.get("run_dir"), 90),
                "kind": "long" if "\\video\\" in str(item.get("run_dir", "")) else "short",
                "error": item.get("error"),
                "youtube_id": item.get("youtube_id"),
            }
            for item in pending
        ],
        "failed_items": [
            {"title": short_text(item.get("title") or item.get("run_dir"), 90), "error": short_text(item.get("error"), 100)}
            for item in failed
        ],
    }


def performance_summary(channel_id: str):
    report = read_json(STATE / f"{channel_id}_performance_report.json", {})
    top = report.get("top_videos", []) if isinstance(report, dict) else []
    weak = report.get("weak_videos", report.get("bottom_videos", [])) if isinstance(report, dict) else []
    totals = report.get("totals", {}) if isinstance(report, dict) else {}
    content = report.get("content_kind_summary", report.get("formats", report.get("format_summary", {}))) if isinstance(report, dict) else {}

    if not totals and top:
        totals = {
            "views": sum(int(v.get("views", 0) or 0) for v in top),
            "likes": sum(int(v.get("likes", 0) or 0) for v in top),
            "comments": sum(int(v.get("comments", 0) or 0) for v in top),
        }

    analytics_summary = report.get("analytics_summary", {}) if isinstance(report, dict) else {}
    retention = report.get("retention", report.get("average_retention", None)) if isinstance(report, dict) else None
    if retention is None and isinstance(analytics_summary, dict):
        retention = analytics_summary.get("average_view_percentage")

    return {
        "date_range": report.get("date_range", {}) if isinstance(report, dict) else {},
        "totals": totals,
        "retention": retention,
        "content": content,
        "best_publish_hours": report.get("best_publish_hours", []) if isinstance(report, dict) else [],
        "duration_buckets": report.get("duration_buckets", {}) if isinstance(report, dict) else {},
        "recommendations": report.get("recommendations", []) if isinstance(report, dict) else [],
        "analytics_summary": analytics_summary,
        "top_by_analytics": report.get("top_by_analytics", [])[:7] if isinstance(report, dict) else [],
        "top_videos": [
            {
                "title": short_text(v.get("title"), 84),
                "views": v.get("views", 0),
                "likes": v.get("likes", 0),
                "comments": v.get("comments", 0),
                "kind": v.get("content_kind", "-"),
                "hour": v.get("published_hour", "-"),
            }
            for v in top[:7]
        ],
        "weak_videos": [
            {
                "title": short_text(v.get("title"), 84),
                "views": v.get("views", 0),
                "likes": v.get("likes", 0),
                "comments": v.get("comments", 0),
                "kind": v.get("content_kind", "-"),
            }
            for v in weak[:7]
        ],
    }


def today_stats(channel_id: str):
    rows = recent_run_rows(channel_id, 500)
    today = datetime.now().date()
    today_rows = [row for row in rows if (parse_run_time(row.get("run_dir")) or datetime.min).date() == today]
    return {
        "generated": len(today_rows),
        "uploaded": sum(1 for row in today_rows if row.get("uploaded")),
        "skipped": sum(1 for row in today_rows if row.get("upload_skipped") or row.get("upload_blocked") or row.get("youtube_error")),
        "shorts": sum(1 for row in today_rows if row.get("content_kind") == "short"),
        "longs": sum(1 for row in today_rows if row.get("content_kind") == "video"),
    }


def quality_gate_summary(channel_id: str):
    rows = recent_run_rows(channel_id, 250)
    held = [
        row for row in rows
        if row.get("upload_skipped") == "quality_gate" or row.get("quality_decision") in {"hold", "reject"}
    ]
    reasons = {}
    streak = 0
    for row in reversed(rows):
        if row.get("upload_skipped") == "quality_gate":
            streak += 1
        elif row.get("uploaded"):
            break
    for row in held[-40:]:
        meta_path = Path(str(row.get("metadata_path") or ""))
        quality_path = meta_path.with_name("quality_review.json") if meta_path.name else Path("")
        review = read_json(quality_path, {})
        for issue in (review.get("issues", []) if isinstance(review, dict) else [])[:3]:
            key = short_text(issue, 80)
            reasons[key] = reasons.get(key, 0) + 1
        if not review:
            key = short_text(row.get("upload_skipped") or "quality gate", 80)
            reasons[key] = reasons.get(key, 0) + 1
    top_reasons = sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:5]
    return {
        "held_recent": len(held[-40:]),
        "streak": streak,
        "top_reasons": [{"reason": reason, "count": count} for reason, count in top_reasons],
    }


def recent_run_rows(channel_id: str, limit: int = 160):
    rows = [row for row in read_jsonl_tail(STATE / "runs.jsonl", limit) if row.get("channel") == channel_id]
    rows.sort(key=lambda row: parse_run_time(row.get("run_dir")) or datetime.min)
    return rows


def upload_health(channel_id: str):
    rows = recent_run_rows(channel_id)
    uploaded = [row for row in rows if row.get("uploaded") and row.get("youtube_id")]
    skipped = [
        row
        for row in rows
        if not row.get("uploaded") and (row.get("upload_skipped") or row.get("upload_blocked") or row.get("youtube_error"))
    ]
    last_upload = uploaded[-1] if uploaded else None
    last_build = rows[-1] if rows else None
    last_dt = parse_run_time(last_upload.get("run_dir")) if last_upload else None
    build_dt = parse_run_time(last_build.get("run_dir")) if last_build else None
    return {
        "last_upload": {
            "title": short_text(last_upload.get("title"), 96) if last_upload else "-",
            "kind": last_upload.get("content_kind") if last_upload else "-",
            "youtube_id": last_upload.get("youtube_id") if last_upload else None,
            "age": human_age(last_dt),
            "time": last_dt.strftime("%Y-%m-%d %H:%M") if last_dt else "-",
            "gap_hours": round((datetime.now() - last_dt).total_seconds() / 3600, 1) if last_dt else None,
        },
        "last_build": {
            "title": short_text(last_build.get("title"), 96) if last_build else "-",
            "kind": last_build.get("content_kind") if last_build else "-",
            "uploaded": bool(last_build and last_build.get("uploaded")),
            "age": human_age(build_dt),
            "time": build_dt.strftime("%Y-%m-%d %H:%M") if build_dt else "-",
        },
        "recent_skips": [
            {
                "title": short_text(row.get("title"), 80),
                "kind": row.get("content_kind"),
                "reason": short_text(row.get("upload_skipped") or row.get("upload_blocked") or row.get("youtube_error"), 80),
            }
            for row in skipped[-5:]
        ],
    }


def channel_snapshot(channel, plan, heartbeat=None):
    channel_id = channel["id"]
    continuity_state = read_json(STATE / "upload_continuity_state.json", {"channels": {}})
    recovery_raw = (
        continuity_state.get("channels", {}).get(channel_id, {})
        if isinstance(continuity_state, dict)
        else {}
    )
    if not isinstance(recovery_raw, dict):
        recovery_raw = {}
    recovery = {
        "status": recovery_raw.get("status"),
        "gap_hours": recovery_raw.get("gap_hours"),
        "threshold_hours": recovery_raw.get("threshold_hours"),
        "consecutive_failures": recovery_raw.get("consecutive_failures"),
        "recovery_stage": recovery_raw.get("recovery_stage"),
        "last_failure_category": recovery_raw.get("last_failure_category"),
        "last_failure_summary": recovery_raw.get("last_failure_summary"),
        "next_recovery_at": recovery_raw.get("next_recovery_at"),
        **{k: v for k, v in recovery_raw.items() if k not in {
            "status", "gap_hours", "threshold_hours", "consecutive_failures",
            "recovery_stage", "last_failure_category", "last_failure_summary", "next_recovery_at",
        }},
    }
    backlog = backlog_summary(channel_id)
    perf = performance_summary(channel_id)
    recent_runs = recent_run_rows(channel_id, 100)[-8:]
    latest_short = latest_output(channel_id, "short")
    latest_long = latest_output(channel_id, "video")
    uploads = upload_health(channel_id)
    gates = quality_gate_summary(channel_id)
    channel_plan = plan.get(channel_id, {}) if isinstance(plan, dict) else {}
    display_name = channel_plan.get("display_name") or channel.get("name")
    channel_view = {
        **channel,
        "name": display_name,
        "tagline": channel.get("tagline") or channel_plan.get("niche") or "",
    }
    story = explain_channel(
        channel_view, backlog, uploads, recovery, gates, latest_short, latest_long, channel_plan
    )
    severity = story.get("tone") or "good"
    action = story.get("headline") or "Looks healthy"
    # Keep action concise for KPI pills while story holds the long form.
    if len(str(action)) > 56:
        upload_gap = recovery.get("gap_hours")
        if upload_gap is None:
            upload_gap = uploads["last_upload"].get("gap_hours")
        recovery_status = str(recovery.get("status") or "")
        if backlog["failed"]:
            action = "Fix failed uploads first"
        elif recovery_status in RECOVERY_PROBLEM_STATUSES - {"recovery_already_queued"}:
            action = "Automatic recovery active"
        elif recovery_status == "recovery_already_queued":
            action = "Recovery already queued"
        elif gates["streak"] >= 2:
            action = "Quality gate streak"
        elif upload_gap is None or (isinstance(upload_gap, (int, float)) and upload_gap > 36):
            action = "Upload gap warning"
        elif isinstance(upload_gap, (int, float)) and upload_gap > 18:
            action = "Upload gap growing"
        elif latest_short.get("status") not in ("ok",) or latest_long.get("status") not in ("ok",):
            action = "Check latest render output"
        elif backlog["pending"]:
            action = "Pending items waiting"
        else:
            action = "Looks healthy"
    hb = heartbeat if isinstance(heartbeat, dict) else {}
    return {
        **channel_view,
        "backlog": backlog,
        "performance": perf,
        "latest": {"short": latest_short, "long": latest_long},
        "uploads": uploads,
        "recovery": recovery,
        "daily_plan": channel_plan,
        "today": today_stats(channel_id),
        "quality_gates": gates,
        "story": story,
        "upcoming": upcoming_jobs(hb, channel_id=channel_id, limit=6),
        "recent_runs": [
            {
                "title": short_text(row.get("title"), 90),
                "kind": row.get("content_kind"),
                "uploaded": bool(row.get("uploaded")),
                "youtube_id": row.get("youtube_id"),
                "error": None
                if row.get("uploaded")
                else row.get("youtube_error") or row.get("facebook_error") or row.get("upload_blocked") or row.get("upload_skipped"),
                "age": human_age(parse_run_time(row.get("run_dir"))),
            }
            for row in recent_runs
        ],
        "action": action,
        "severity": severity,
    }


def build_snapshot():
    processes = process_rows()
    scheduler = [row for row in processes if row["is_scheduler"]]
    heartbeat = read_json(STATE / "scheduler_heartbeat.json", {})
    heartbeat_age = iso_age_seconds(heartbeat.get("updated_at"))
    scheduler_state = "stopped"
    if scheduler and heartbeat_age is not None and heartbeat_age <= 180:
        scheduler_state = "running"
    elif scheduler:
        scheduler_state = "stale"
    elif heartbeat_age is not None and heartbeat_age <= 180:
        # Heartbeat fresh but process filter missed schedule runner variants.
        scheduler_state = "running"
    plan = settings_summary()
    channels = [channel_snapshot(channel, plan, heartbeat=heartbeat) for channel in CHANNELS]
    encoder_pref = os.environ.get("YT_VIDEO_ENCODER", "auto") or "auto"
    encoder_note = {
        "auto": "Auto-select encoder for this machine",
        "cpu": "CPU encode (slowest, most compatible)",
        "nvenc": "NVIDIA NVENC hardware encode",
        "qsv": "Intel Quick Sync encode",
    }.get(str(encoder_pref).lower(), f"Encoder preference: {encoder_pref}")
    return {
        "root": str(ROOT),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "system": {
            "encoder_pref": encoder_pref,
            "encoder_note": encoder_note,
            "short_target": "50–59s",
            "long_target": "8–10 min",
        },
        "scheduler": {
            "running": bool(scheduler) or scheduler_state == "running",
            "pids": [row["pid"] for row in scheduler],
            "state": scheduler_state,
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_note": heartbeat.get("note"),
            "heartbeat_updated_at": heartbeat.get("updated_at"),
            "upcoming": upcoming_jobs(heartbeat, limit=8),
        },
        "workers": [row for row in processes if not row["is_scheduler"]][:10],
        "channels": channels,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/dashboard"):
            html_path = Path(__file__).with_name("live_dashboard.html")
            body = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            body = json.dumps(build_snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main():
    port = int(os.environ.get("YT_DASHBOARD_PORT", "8787"))
    host = os.environ.get("YT_DASHBOARD_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    local_url = f"http://127.0.0.1:{port}/"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan_ip = "YOUR-PC-IP"
    lan_url = f"http://{lan_ip}:{port}/"
    print(f"Live dashboard running: {local_url}")
    print(f"Same-network URL:      {lan_url}")
    print("Press CTRL+C to stop.")
    if "--no-open" not in sys.argv:
        time.sleep(0.5)
        webbrowser.open(local_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
