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
from urllib.parse import urlparse, parse_qs
import hmac
import ipaddress
import socket
try:
    import yaml
except Exception:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "state"
OUTPUT = ROOT / "output"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

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
        facebook = channel.get("facebook", {}) or {}
        money = channel.get("monetization", {}) or {}
        out[cid] = {
            "display_name": channel.get("display_name") or cid,
            "niche": channel.get("niche_description") or "",
            "shorts_per_day": len(shorts.get("schedule_times", []) or channel.get("schedule_times", []) or []),
            "longs_per_day": len(videos.get("schedule_times", []) or []) if videos.get("enabled", False) else 0,
            "daily_upload_cap": int(channel.get("daily_upload_cap", 0) or 0),
            "max_upload_gap_hours": float(channel.get("max_upload_gap_hours", 4.0) or 4.0),
            "presenter": "local" if presenter.get("enabled") else "off",
            "heygen": "on" if heygen.get("enabled") else "off",
            "facebook_upload_enabled": bool(facebook.get("upload_enabled")),
            "monetization_enabled": bool(money.get("enabled", True)),
            "affiliate_slots": len(list(money.get("affiliates") or [])),
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


def friendly_error(raw) -> dict:
    """Turn ugly upload errors into short plain-English messages."""
    text = str(raw or "").strip()
    if not text:
        return {"raw": "", "plain": "Unknown error", "category": "unknown"}
    lower = text.lower()
    rules = [
        (("facebook", "limited", "temporarily"), "Facebook blocked the post (account limited). YouTube may still be OK.", "facebook"),
        (("facebook", "oauth", "368"), "Facebook login / permission problem (OAuth 368).", "facebook"),
        (("facebook",), "Facebook upload failed.", "facebook"),
        (("quota",), "YouTube API quota ran out for now. Wait and retry.", "youtube"),
        (("invalid_grant", "token", "refresh"), "YouTube login expired. Re-auth the channel token.", "auth"),
        (("unauthorized", "401"), "YouTube auth failed. Re-connect the channel.", "auth"),
        (("403", "forbidden"), "YouTube refused the upload (permission / policy).", "youtube"),
        (("timeout", "timed out"), "Network timed out during upload.", "network"),
        (("connection", "dns", "name resolution"), "Network / DNS problem during upload.", "network"),
        (("quality",), "Held by quality gate (not a hard upload failure).", "quality"),
    ]
    for needles, plain, category in rules:
        if all(n in lower for n in needles):
            return {"raw": short_text(text, 220), "plain": plain, "category": category}
    # Single-keyword fallbacks
    if "youtube" in lower and "fail" in lower:
        return {"raw": short_text(text, 220), "plain": "YouTube upload failed.", "category": "youtube"}
    return {"raw": short_text(text, 220), "plain": short_text(text, 120), "category": "other"}


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
    failed = [item for item in items if isinstance(item, dict) and item.get("status") == "failed"][:8]

    def _kind(item: dict) -> str:
        run = str(item.get("run_dir") or "").replace("/", "\\").lower()
        if "\\video\\" in run:
            return "long"
        return "short"

    failed_items = []
    for item in failed:
        err = friendly_error(
            item.get("error") or item.get("facebook_error") or item.get("youtube_error")
        )
        failed_items.append(
            {
                "title": short_text(item.get("title") or item.get("run_dir"), 90),
                "kind": _kind(item),
                "youtube_id": item.get("youtube_id"),
                "youtube_ok": bool(item.get("youtube_id")),
                "error": err["raw"],
                "error_plain": err["plain"],
                "error_category": err["category"],
            }
        )
    return {
        "total": len(items),
        "pending": by_status.get("pending", 0),
        "posted": by_status.get("posted", 0),
        "failed": by_status.get("failed", 0),
        "skipped_quality": by_status.get("skipped_quality", 0),
        "pending_items": [
            {
                "title": short_text(item.get("title") or item.get("run_dir"), 90),
                "kind": _kind(item),
                "error": item.get("error"),
                "youtube_id": item.get("youtube_id"),
            }
            for item in pending
        ],
        "failed_items": failed_items,
    }


def _video_row(v: dict, include_hour: bool = False) -> dict:
    row = {
        "title": short_text(v.get("title"), 84),
        "video_id": v.get("video_id"),
        "views": int(v.get("views") or 0),
        "likes": int(v.get("likes") or 0),
        "comments": int(v.get("comments") or 0),
        "shares": int(v.get("shares") or 0),
        "kind": v.get("content_kind") or "-",
        "retention": v.get("average_view_percentage"),
        "avg_duration": v.get("average_view_duration"),
        "engaged_rate": v.get("engaged_view_rate"),
        "subs_gained": int(v.get("subscribers_gained") or 0),
        "virality_score": v.get("virality_score"),
        "url": f"https://youtu.be/{v.get('video_id')}" if v.get("video_id") else None,
    }
    if include_hour:
        row["hour"] = v.get("published_hour", "-")
    return row


def _daily_from_report(report: dict) -> list[dict]:
    """Build chronological day series from analytics API rows (preferred) or top_days."""
    daily: list[dict] = []
    api = report.get("analytics_api") if isinstance(report, dict) else {}
    if isinstance(api, dict) and not api.get("error"):
        for row in api.get("rows") or []:
            if not isinstance(row, (list, tuple)) or not row:
                continue
            try:
                daily.append(
                    {
                        "day": str(row[0]),
                        "views": int(row[1] or 0),
                        "engaged_views": int(row[2] or 0) if len(row) > 2 else 0,
                        "watch_minutes": int(row[3] or 0) if len(row) > 3 else 0,
                        "avg_duration": int(row[4] or 0) if len(row) > 4 else 0,
                        "avg_pct": round(float(row[5] or 0), 1) if len(row) > 5 else 0.0,
                        "likes": int(row[6] or 0) if len(row) > 6 else 0,
                        "subs_gained": int(row[9] or 0) if len(row) > 9 else 0,
                    }
                )
            except Exception:
                continue
        if daily:
            daily.sort(key=lambda item: item["day"])
            return daily
    summary = report.get("analytics_summary") if isinstance(report, dict) else {}
    top_days = (summary or {}).get("top_days") if isinstance(summary, dict) else []
    if isinstance(top_days, list):
        for row in top_days:
            if not isinstance(row, dict):
                continue
            daily.append(
                {
                    "day": str(row.get("day") or ""),
                    "views": int(row.get("views") or 0),
                    "engaged_views": int(row.get("engaged_views") or 0),
                    "watch_minutes": 0,
                    "avg_duration": int(row.get("average_view_duration") or 0),
                    "avg_pct": float(row.get("average_view_percentage") or 0),
                    "likes": 0,
                    "subs_gained": 0,
                }
            )
        daily.sort(key=lambda item: item["day"])
    return daily


def performance_summary(channel_id: str):
    path = STATE / f"{channel_id}_performance_report.json"
    report = read_json(path, {})
    if not isinstance(report, dict):
        report = {}

    analytics_summary = report.get("analytics_summary") or {}
    if not isinstance(analytics_summary, dict):
        analytics_summary = {}

    top_analytics = [v for v in (report.get("top_by_analytics") or []) if isinstance(v, dict)]
    top_catalog = [v for v in (report.get("top_videos") or []) if isinstance(v, dict)]
    weak_source = report.get("weak_videos") or report.get("bottom_videos") or []
    weak = [v for v in weak_source if isinstance(v, dict)]
    # Prefer analytics ranking for "top"; fall back to catalog views.
    top = top_analytics or top_catalog
    # Enrich weak catalog rows with analytics retention when video ids match.
    analytics_by_id = {
        str(v.get("video_id")): v for v in top_analytics if v.get("video_id")
    }
    enriched_weak = []
    for v in weak[:12]:
        match = analytics_by_id.get(str(v.get("video_id") or ""))
        if match:
            merged = {
                **v,
                **{
                    k: match.get(k)
                    for k in (
                        "average_view_percentage",
                        "average_view_duration",
                        "engaged_view_rate",
                        "shares",
                        "subscribers_gained",
                        "virality_score",
                    )
                    if match.get(k) is not None
                },
            }
            enriched_weak.append(merged)
        else:
            enriched_weak.append(v)
    weak = enriched_weak or weak
    if not weak and top_analytics:
        weak = sorted(
            top_analytics,
            key=lambda v: (int(v.get("views") or 0), float(v.get("engaged_view_rate") or 0)),
        )[:8]

    content = report.get("format_summary") or report.get("content_kind_summary") or report.get("formats") or {}
    if not isinstance(content, dict):
        content = {}

    totals = report.get("totals") or {}
    if not isinstance(totals, dict):
        totals = {}
    if not totals:
        totals = {
            "views": int(analytics_summary.get("total_views") or 0),
            "likes": int(analytics_summary.get("likes") or 0),
            "comments": int(analytics_summary.get("comments") or 0),
            "shares": int(analytics_summary.get("shares") or 0),
            "subscribers_gained": int(analytics_summary.get("subscribers_gained") or 0),
            "watch_minutes": int(analytics_summary.get("estimated_minutes_watched") or 0),
        }

    retention = report.get("retention", report.get("average_retention"))
    if retention is None:
        retention = analytics_summary.get("average_view_percentage")

    engaged_rate = analytics_summary.get("engaged_view_rate")
    try:
        engaged_rate = float(engaged_rate) if engaged_rate is not None else None
    except Exception:
        engaged_rate = None

    hours_raw = report.get("best_publish_hours") or []
    hours = []
    if isinstance(hours_raw, list):
        for row in hours_raw:
            if not isinstance(row, dict):
                continue
            try:
                hour = int(row.get("key"))
            except Exception:
                continue
            hours.append(
                {
                    "hour": hour,
                    "label": f"{hour:02d}:00",
                    "avg_views": float(row.get("avg_views") or 0),
                    "count": int(row.get("count") or 0),
                    "max_views": int(row.get("max_views") or 0),
                }
            )

    daily = _daily_from_report(report)
    # Keep chart readable: last 28 days if longer.
    if len(daily) > 28:
        daily = daily[-28:]

    report_mtime = None
    report_age_hours = None
    report_stale = True
    try:
        if path.exists():
            report_mtime = datetime.fromtimestamp(path.stat().st_mtime)
            report_age_hours = round((datetime.now() - report_mtime).total_seconds() / 3600, 1)
            report_stale = report_age_hours > 48
    except Exception:
        pass

    best_hour = hours[0]["label"] if hours else None
    recommendations = [str(r) for r in (report.get("recommendations") or []) if r][:8]

    kpis = {
        "views": int(analytics_summary.get("total_views") or totals.get("views") or 0),
        "engaged_views": int(analytics_summary.get("total_engaged_views") or 0),
        "engaged_rate": engaged_rate,
        "retention": retention,
        "watch_hours": round(float(analytics_summary.get("estimated_minutes_watched") or totals.get("watch_minutes") or 0) / 60.0, 1),
        "avg_duration": analytics_summary.get("average_view_duration"),
        "likes": int(analytics_summary.get("likes") or totals.get("likes") or 0),
        "comments": int(analytics_summary.get("comments") or totals.get("comments") or 0),
        "shares": int(analytics_summary.get("shares") or totals.get("shares") or 0),
        "subs_gained": int(analytics_summary.get("subscribers_gained") or totals.get("subscribers_gained") or 0),
        "best_hour": best_hour,
    }

    return {
        "available": bool(analytics_summary or top or daily),
        "date_range": report.get("date_range") or {},
        "updated_at": report_mtime.strftime("%Y-%m-%d %H:%M") if report_mtime else None,
        "age_hours": report_age_hours,
        "stale": report_stale,
        "kpis": kpis,
        "totals": totals,
        "retention": retention,
        "content": content,
        "daily": daily,
        "hours": hours,
        "best_publish_hours": report.get("best_publish_hours") or [],
        "duration_buckets": report.get("duration_buckets") or {},
        "recommendations": recommendations,
        "analytics_summary": analytics_summary,
        "top_by_analytics": [_video_row(v) for v in top_analytics[:10]],
        "top_videos": [_video_row(v, include_hour=True) for v in top[:10]],
        "weak_videos": [_video_row(v, include_hour=True) for v in weak[:8]],
    }


def monetization_summary(channel_id: str, channel_plan: dict | None = None):
    """Phase D money checklist + YPP progress bars from latest performance report."""
    report = read_json(STATE / f"{channel_id}_performance_report.json", {})
    if not isinstance(report, dict):
        report = {}
    analytics = report.get("analytics_summary") or {}
    if not isinstance(analytics, dict):
        analytics = {}
    format_summary = report.get("format_summary") or {}
    if not isinstance(format_summary, dict):
        format_summary = {}
    shorts_block = format_summary.get("shorts") or {}
    longs_block = format_summary.get("long_videos") or {}

    shorts_views = int((shorts_block or {}).get("views") or 0)
    if not shorts_views:
        for row in report.get("top_by_analytics") or []:
            if isinstance(row, dict) and str(row.get("content_kind") or "") == "short":
                shorts_views += int(row.get("views") or 0)

    total_minutes = float(analytics.get("estimated_minutes_watched") or 0)
    # Prefer long-form watch minutes when split is available.
    long_minutes = 0.0
    for row in report.get("top_by_analytics") or report.get("video_analytics") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("content_kind") or "") == "video":
            long_minutes += float(row.get("estimated_minutes_watched") or 0)
    watch_hours = round((long_minutes or total_minutes) / 60.0, 1)
    subs_gained = int(analytics.get("subscribers_gained") or 0)

    env_keys = {
        "ancient_history": ["AFFILIATE_ANCIENT_BOOK_URL", "AFFILIATE_ANCIENT_TOOL_URL"],
        "brain_lens": ["AFFILIATE_BRAIN_BOOK_URL", "AFFILIATE_BRAIN_TOOL_URL"],
    }.get(channel_id, [])
    affiliate_ready = sum(1 for key in env_keys if len((os.getenv(key) or "").strip()) > 8)
    plan = channel_plan if isinstance(channel_plan, dict) else {}
    fb_on = bool(plan.get("facebook_upload_enabled"))

    def _bar(current: float, target: float, label: str, note: str) -> dict:
        target = float(target) or 1.0
        current = max(0.0, float(current or 0))
        pct = min(100.0, round(current / target * 100.0, 1))
        return {
            "label": label,
            "current": current,
            "target": target,
            "pct": pct,
            "done": current >= target,
            "note": note,
        }

    ypp = {
        "subs_gained_sample": subs_gained,
        "shorts_views_sample": shorts_views,
        "watch_hours_sample": watch_hours,
        "path": "shorts" if shorts_views >= watch_hours * 100 else "watch_hours",
        "bars": [
            _bar(
                subs_gained,
                1000,
                "Subscribers (sample gained in period)",
                "Studio total may differ — this is period gain from Analytics, not lifetime total.",
            ),
            _bar(
                shorts_views,
                10_000_000,
                "Shorts views (report sample)",
                "YPP path A needs 10M Shorts views in 90 days (Studio).",
            ),
            _bar(
                watch_hours,
                4000,
                "Watch hours (report sample)",
                "YPP path B needs 4,000 public long-form watch hours in 12 months.",
            ),
        ],
    }

    checklist = [
        {
            "id": "ypp_subs",
            "label": "YPP: 1,000 subscribers",
            "done": False,
            "detail": f"Period subs gained ≈ {subs_gained} (check Studio for exact total)",
        },
        {
            "id": "ypp_shorts",
            "label": "YPP path A: 10M Shorts views / 90d",
            "done": shorts_views >= 10_000_000,
            "detail": f"Sample Shorts views in report ≈ {shorts_views:,}",
        },
        {
            "id": "ypp_hours",
            "label": "YPP path B: 4,000 long watch hours / 12mo",
            "done": watch_hours >= 4000,
            "detail": f"Sample watch hours in report ≈ {watch_hours}",
        },
        {
            "id": "affiliates",
            "label": "Affiliate links configured in .env",
            "done": affiliate_ready > 0,
            "detail": f"{affiliate_ready}/{len(env_keys)} affiliate URL slots filled",
        },
        {
            "id": "facebook",
            "label": "Facebook Reels cross-post",
            "done": fb_on,
            "detail": "Enabled" if fb_on else "Disabled in settings",
        },
        {
            "id": "short_long_funnel",
            "label": "Short → long CTA in descriptions",
            "done": True,
            "detail": "Active in SEO metadata for Shorts",
        },
    ]
    done_count = sum(1 for item in checklist if item["done"])
    return {
        "phase": "D",
        "checklist": checklist,
        "progress": f"{done_count}/{len(checklist)}",
        "done_count": done_count,
        "total_count": len(checklist),
        "ypp": ypp,
        "total_views_sample": int(analytics.get("total_views") or 0),
        "watch_hours_sample": watch_hours,
        "shorts_views_sample": shorts_views,
        "affiliates_ready": affiliate_ready,
        "shorts_count": int((shorts_block or {}).get("count") or 0),
        "longs_count": int((longs_block or {}).get("count") or 0),
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
    money = monetization_summary(channel_id, channel_plan)
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
        "monetization": money,
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


def _service_pulse(path: Path, ok_seconds: int = 180) -> dict:
    data = read_json(path, {})
    age = iso_age_seconds(data.get("updated_at") if isinstance(data, dict) else None)
    if age is None:
        return {"state": "missing", "age_seconds": None, "updated_at": None}
    state = "running" if age <= ok_seconds else "stale"
    return {
        "state": state,
        "age_seconds": age,
        "updated_at": data.get("updated_at") if isinstance(data, dict) else None,
        "note": data.get("note") if isinstance(data, dict) else None,
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
    watchdog = _service_pulse(STATE / "watchdog_heartbeat.json", ok_seconds=300)
    guard = _service_pulse(STATE / "service_guard_heartbeat.json", ok_seconds=300)
    money_done = sum(int((c.get("monetization") or {}).get("done_count") or 0) for c in channels)
    money_total = sum(int((c.get("monetization") or {}).get("total_count") or 0) for c in channels)
    return {
        "root": str(ROOT),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "system": {
            "encoder_pref": encoder_pref,
            "encoder_note": encoder_note,
            "short_target": "50–59s",
            "long_target": "8–10 min",
            "watchdog": watchdog,
            "service_guard": guard,
            "money_progress": f"{money_done}/{money_total}" if money_total else "—",
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
        "ready_queue": ready_queue_snapshot(),
    }


def _sys_path_src():
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def ready_queue_snapshot():
    _sys_path_src()
    try:
        from yt_auto import ready_queue as rq
    except Exception as exc:
        return {"enabled": False, "error": str(exc), "items": [], "by_status": {}, "tabs": []}
    tz = "Asia/Karachi"
    try:
        if yaml:
            settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
            tz = str((settings.get("app") or {}).get("timezone") or tz)
    except Exception:
        pass
    channel_ids = [c["id"] for c in CHANNELS]
    summary = rq.queue_summary(STATE, channel_ids, timezone_name=tz, root=ROOT)
    summary["flag_path"] = str(rq.flag_path(ROOT))
    # The page groups by plain cause rather than raw status; keep that shaping
    # in one tested place instead of in the browser.
    try:
        from yt_auto.dashboard_views import queue_tabs

        summary["tabs"] = queue_tabs(summary)
    except Exception as exc:  # a view bug must not blank the whole queue page
        summary["tabs"] = []
        summary["tabs_error"] = str(exc)
    return summary


def viral_dna_summary(channel_id: str) -> dict:
    data = read_json(STATE / f"{channel_id}_viral_dna.json", {})
    if not isinstance(data, dict):
        data = {}
    return {
        "available": bool(data),
        "analyzed_at": data.get("analyzed_at"),
        "hooks": (data.get("hooks") or data.get("winning_hooks") or [])[:12],
        "forbidden_phrases": (data.get("forbidden_phrases") or data.get("avoid_phrases") or [])[:12],
        "best_hours": data.get("best_hours") or data.get("best_publish_hours") or [],
        "patterns": data.get("patterns") or data.get("title_patterns") or [],
        "notes": data.get("notes") or data.get("summary") or "",
        "raw_keys": sorted(list(data.keys()))[:20],
    }


def analytics_payload(channel_id: str) -> dict:
    plan = settings_summary().get(channel_id) or {}
    return {
        "channel_id": channel_id,
        "name": next((c["name"] for c in CHANNELS if c["id"] == channel_id), channel_id),
        "performance": performance_summary(channel_id),
        "viral_dna": viral_dna_summary(channel_id),
        "monetization": monetization_summary(channel_id, plan),
        "plan": plan,
    }


def suggestions_payload() -> dict:
    items = []
    rq = ready_queue_snapshot()
    if not rq.get("enabled"):
        items.append(
            {
                "priority": "medium",
                "channel": "system",
                "text": "Ready Queue is OFF — enable it on the Queue page to prep videos ahead of slot time.",
            }
        )
    else:
        for channel_id, pack in (rq.get("packs") or {}).items():
            rendered = int(pack.get("rendered_shorts") or 0)
            target = int(pack.get("buffer_shorts") or 2)
            if not pack.get("exists"):
                items.append(
                    {
                        "priority": "high",
                        "channel": channel_id,
                        "text": f"No day pack for today on {channel_id} — generate a day pack.",
                    }
                )
            elif rendered < target:
                items.append(
                    {
                        "priority": "high",
                        "channel": channel_id,
                        "text": f"{channel_id} Ready Queue has {rendered}/{target} Shorts rendered — force prep before the next slot.",
                    }
                )
    for channel in CHANNELS:
        cid = channel["id"]
        perf = performance_summary(cid)
        for rec in (perf.get("recommendations") or [])[:4]:
            items.append({"priority": "medium", "channel": cid, "text": rec})
        dna = viral_dna_summary(cid)
        for hook in (dna.get("hooks") or [])[:3]:
            items.append({"priority": "low", "channel": cid, "text": f"Viral DNA hook: {hook}"})
        money = monetization_summary(cid, settings_summary().get(cid) or {})
        for check in money.get("checklist") or []:
            if isinstance(check, dict) and not check.get("done"):
                items.append(
                    {
                        "priority": "low",
                        "channel": cid,
                        "text": f"Money todo: {check.get('label')} — {check.get('detail') or ''}".strip(),
                    }
                )
    return {"items": items[:40], "ready_queue_enabled": bool(rq.get("enabled"))}


def _flag_state(name: str) -> bool:
    path = ROOT / "controls" / "flags" / f"{name}.enabled"
    if not path.exists():
        return False
    raw = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _set_flag(name: str, enabled: bool) -> None:
    path = ROOT / "controls" / "flags" / f"{name}.enabled"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n" if enabled else "0\n", encoding="utf-8")


def _env_present(key: str) -> bool:
    return bool(str(os.getenv(key) or "").strip())


def _token_file_status(rel_or_abs: str | None) -> dict:
    if not rel_or_abs:
        return {"present": False, "path": None, "mtime": None}
    path = Path(rel_or_abs)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return {"present": False, "path": str(path), "mtime": None}
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        mtime = None
    return {"present": True, "path": str(path), "mtime": mtime}


def settings_payload() -> dict:
    plan = settings_summary()
    full_channels = []
    if yaml:
        try:
            data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
            for channel in data.get("channels") or []:
                if not isinstance(channel, dict):
                    continue
                cid = channel.get("id")
                profiles = channel.get("content_profiles") or {}
                shorts = profiles.get("shorts") or {}
                videos = profiles.get("videos") or {}
                yt = channel.get("youtube") or {}
                fb = channel.get("facebook") or {}
                money = channel.get("monetization") or {}
                full_channels.append(
                    {
                        "id": cid,
                        "display_name": channel.get("display_name") or cid,
                        "shorts_enabled": bool(shorts.get("enabled", True)),
                        "longs_enabled": bool(videos.get("enabled", False)),
                        "short_times": list(shorts.get("schedule_times") or channel.get("schedule_times") or []),
                        "long_times": list(videos.get("schedule_times") or []),
                        "daily_upload_cap": int(channel.get("daily_upload_cap") or 0),
                        "max_upload_gap_hours": float(channel.get("max_upload_gap_hours") or 0),
                        "facebook_upload_enabled": bool(fb.get("upload_enabled")),
                        "youtube_upload_enabled": bool(yt.get("upload_enabled", True)),
                        "monetization_enabled": bool(money.get("enabled", False)),
                        "youtube_token": _token_file_status(yt.get("token_file")),
                    }
                )
        except Exception:
            full_channels = []

    affiliate_keys = [
        "AFFILIATE_ANCIENT_BOOK_URL",
        "AFFILIATE_ANCIENT_TOOL_URL",
        "AFFILIATE_BRAIN_BOOK_URL",
        "AFFILIATE_BRAIN_TOOL_URL",
    ]
    return {
        "ready_queue_enabled": ready_queue_snapshot().get("enabled"),
        "flags": {
            "ready_queue": _flag_state("ready_queue"),
            "lan": _flag_state("lan"),
            "autostart": _flag_state("autostart"),
            "surge_ancient": _flag_state("surge_ancient"),
            "phase_a": _flag_state("phase_a"),
            "phase_b": _flag_state("phase_b"),
            "phase_c": _flag_state("phase_c"),
            "phase_d": _flag_state("phase_d"),
            "service_guard": _flag_state("service_guard") if (ROOT / "controls" / "flags" / "service_guard.enabled").exists() else None,
        },
        "encoder_pref": os.environ.get("YT_VIDEO_ENCODER", "auto") or "auto",
        "channels": full_channels or [
            {"id": cid, **(plan.get(cid) or {})} for cid in [c["id"] for c in CHANNELS]
        ],
        "keys": {
            "GEMINI_API_KEY": _env_present("GEMINI_API_KEY"),
            "GROQ_API_KEY": _env_present("GROQ_API_KEY"),
            "MISTRAL_API_KEY": _env_present("MISTRAL_API_KEY"),
            "HF_API_KEY": _env_present("HF_API_KEY"),
            "PEXELS_API_KEY": _env_present("PEXELS_API_KEY"),
            "PIXABAY_API_KEY": _env_present("PIXABAY_API_KEY"),
            "YOUTUBE_API_KEY": _env_present("YOUTUBE_API_KEY") or _env_present("GOOGLE_API_KEY"),
            "FB_USER_ACCESS_TOKEN": _env_present("FB_USER_ACCESS_TOKEN"),
            "YT_DASHBOARD_TOKEN": _env_present("YT_DASHBOARD_TOKEN"),
        },
        "affiliate_urls_filled": sum(1 for k in affiliate_keys if len((os.getenv(k) or "").strip()) > 8),
        "affiliate_urls_total": len(affiliate_keys),
        "dashboard_token_required": _env_present("YT_DASHBOARD_TOKEN"),
        "env_path": str(ROOT / ".env"),
        "settings_path": str(ROOT / "config" / "settings.yaml"),
        "lan_hint": "Open http://<PC-LAN-IP>:8787 from your phone when LAN is enabled.",
    }


def runs_payload(limit: int = 40) -> dict:
    runs = read_jsonl_tail(STATE / "runs.jsonl", limit=limit)
    active = []
    active_dir = STATE / "active_builds"
    if active_dir.exists():
        for path in active_dir.glob("*.json"):
            data = read_json(path, {})
            if isinstance(data, dict) and data:
                active.append(data)
    return {"runs": runs, "active_builds": active}


def log_tail(name: str, lines: int = 120) -> dict:
    safe = Path(name).name
    candidates = [
        STATE / "scheduler_jobs" / safe,
        STATE / safe,
        ROOT / "logs" / safe,
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None and (STATE / "scheduler_jobs").exists():
        # Allow bare stem match against newest log.
        matches = sorted((STATE / "scheduler_jobs").glob(f"*{safe}*"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = matches[0] if matches else None
    if path is None or not path.exists():
        return {"ok": False, "error": "log not found", "name": safe, "lines": []}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        return {"ok": False, "error": str(exc), "name": safe, "lines": []}
    return {
        "ok": True,
        "name": path.name,
        "path": str(path),
        "lines": text[-max(20, min(lines, 400)) :],
    }


def _dashboard_control_token() -> str:
    return str(os.getenv("YT_DASHBOARD_TOKEN") or "").strip()


def _client_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    try:
        return ipaddress.ip_address(handler.client_address[0]).is_loopback
    except (ValueError, IndexError, TypeError):
        return False


def _presented_token(handler: BaseHTTPRequestHandler) -> str:
    header = str(handler.headers.get("X-Dashboard-Token") or "").strip()
    if header:
        return header
    auth = str(handler.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Query fallback so a one-off curl works without header juggling.
    query = parse_qs(urlparse(handler.path).query)
    values = query.get("token") or []
    return str(values[0]).strip() if values else ""


def _dashboard_token_ok(handler: BaseHTTPRequestHandler) -> bool:
    """Gate state-changing dashboard actions.

    The server binds 0.0.0.0 and the installer opens port 8787 to the LAN, so
    control actions (force-upload among them) were reachable by anyone on the
    network. With YT_DASHBOARD_TOKEN set, every caller must present it. Without
    it, one-click local ops still work but only from this machine -- a remote
    caller is refused rather than silently trusted.
    """
    configured = _dashboard_control_token()
    if not configured:
        return _client_is_loopback(handler)
    return hmac.compare_digest(_presented_token(handler), configured)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _require_token(handler: BaseHTTPRequestHandler) -> bool:
    """Return True when the request may proceed; otherwise answer 401."""
    if _dashboard_token_ok(handler):
        return True
    if _dashboard_control_token():
        error = "invalid_or_missing_token"
        detail = "Send the dashboard token as X-Dashboard-Token."
    else:
        error = "remote_control_disabled"
        detail = (
            "Control actions from another machine need YT_DASHBOARD_TOKEN set "
            "in .env; local (loopback) use needs no token."
        )
    _json_response(handler, {"ok": False, "error": error, "detail": detail}, status=401)
    return False


def patch_settings(body: dict) -> dict:
    """Apply safe settings patches: flags, schedule times, caps."""
    changed = []
    flags = body.get("flags")
    if isinstance(flags, dict):
        for name, value in flags.items():
            if name not in {
                "ready_queue",
                "lan",
                "autostart",
                "surge_ancient",
                "phase_a",
                "phase_b",
                "phase_c",
                "phase_d",
            }:
                continue
            if name == "ready_queue":
                _sys_path_src()
                from yt_auto import ready_queue as rq

                rq.set_ready_queue_enabled(bool(value), ROOT)
            else:
                _set_flag(str(name), bool(value))
            changed.append(f"flag:{name}")

    if "ready_queue_enabled" in body:
        _sys_path_src()
        from yt_auto import ready_queue as rq

        rq.set_ready_queue_enabled(bool(body.get("ready_queue_enabled")), ROOT)
        changed.append("ready_queue_enabled")

    channel_patches = body.get("channels")
    if isinstance(channel_patches, list) and yaml:
        settings_path = ROOT / "config" / "settings.yaml"
        backup = ROOT / "config" / f"settings.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        raw = settings_path.read_text(encoding="utf-8")
        backup.write_text(raw, encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        by_id = {c.get("id"): c for c in (data.get("channels") or []) if isinstance(c, dict)}
        for patch in channel_patches:
            if not isinstance(patch, dict):
                continue
            cid = patch.get("id")
            channel = by_id.get(cid)
            if not channel:
                continue
            profiles = channel.setdefault("content_profiles", {})
            shorts = profiles.setdefault("shorts", {})
            videos = profiles.setdefault("videos", {})
            if "short_times" in patch and isinstance(patch["short_times"], list):
                shorts["schedule_times"] = [str(t) for t in patch["short_times"]]
                changed.append(f"{cid}:short_times")
            if "long_times" in patch and isinstance(patch["long_times"], list):
                videos["schedule_times"] = [str(t) for t in patch["long_times"]]
                changed.append(f"{cid}:long_times")
            if "shorts_enabled" in patch:
                shorts["enabled"] = bool(patch["shorts_enabled"])
                changed.append(f"{cid}:shorts_enabled")
            if "longs_enabled" in patch:
                videos["enabled"] = bool(patch["longs_enabled"])
                changed.append(f"{cid}:longs_enabled")
            if "daily_upload_cap" in patch:
                channel["daily_upload_cap"] = int(patch["daily_upload_cap"])
                changed.append(f"{cid}:daily_upload_cap")
            if "max_upload_gap_hours" in patch:
                channel["max_upload_gap_hours"] = float(patch["max_upload_gap_hours"])
                changed.append(f"{cid}:max_upload_gap_hours")
            if "facebook_upload_enabled" in patch:
                fb = channel.setdefault("facebook", {})
                fb["upload_enabled"] = bool(patch["facebook_upload_enabled"])
                changed.append(f"{cid}:facebook_upload_enabled")
            if "monetization_enabled" in patch:
                money = channel.setdefault("monetization", {})
                money["enabled"] = bool(patch["monetization_enabled"])
                changed.append(f"{cid}:monetization_enabled")
        settings_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        changed.append(f"backup:{backup.name}")

    return {"ok": True, "changed": changed, "settings": settings_payload()}


def restart_scheduler() -> dict:
    """Stop schedule runners and start one fresh cron+upload scheduler (in-process)."""
    killed: list[int] = []
    for row in process_rows():
        if not row.get("is_scheduler"):
            continue
        pid = row.get("pid")
        if not pid:
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            killed.append(int(pid))
        except Exception:
            pass

    time.sleep(1.5)
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = Path(sys.executable)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    try:
        creation = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(
            [str(py), str(ROOT / "run.py"), "schedule", "--mode", "cron", "--upload"],
            cwd=str(ROOT),
            env=env,
            creationflags=creation,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {
            "ok": True,
            "method": "python",
            "killed": killed,
            "started_pid": int(proc.pid),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "killed": killed}


def refresh_analytics(channel_id: str | None = None) -> dict:
    channels = [channel_id] if channel_id else [c["id"] for c in CHANNELS]
    results = []
    for cid in channels:
        cmd = [sys.executable, str(ROOT / "run.py"), "performance-report", "--channel", cid]
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
            results.append({"channel": cid, "ok": proc.returncode == 0, "code": proc.returncode})
        except Exception as exc:
            results.append({"channel": cid, "ok": False, "error": str(exc)})
    return {"ok": all(r.get("ok") for r in results), "results": results}


def queue_action(action: str, item_id: str | None = None, body: dict | None = None) -> dict:
    _sys_path_src()
    from yt_auto import ready_queue as rq

    body = body or {}
    tz = "Asia/Karachi"
    try:
        if yaml:
            settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
            tz = str((settings.get("app") or {}).get("timezone") or tz)
    except Exception:
        pass

    if action == "toggle":
        enabled = bool(body.get("enabled"))
        rq.set_ready_queue_enabled(enabled, ROOT)
        return {"ok": True, "enabled": enabled, "queue": ready_queue_snapshot()}

    if action == "day-pack":
        # Build packs without importing heavy pipeline when possible.
        if yaml:
            data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
        else:
            data = {}
        rebuild = bool(body.get("rebuild"))
        plans = []
        for channel in data.get("channels") or []:
            if not isinstance(channel, dict):
                continue
            cid = channel.get("id")
            if body.get("channel") and body.get("channel") != cid:
                continue
            # Lightweight stand-in object with attribute access.
            class _C:
                pass

            obj = _C()
            obj.id = cid
            profiles = channel.get("content_profiles") or {}
            shorts = profiles.get("shorts") or {}
            videos = profiles.get("videos") or {}

            class _P:
                pass

            s = _P()
            s.enabled = bool(shorts.get("enabled", True))
            s.schedule_times = list(shorts.get("schedule_times") or channel.get("schedule_times") or [])
            v = _P()
            v.enabled = bool(videos.get("enabled", False))
            v.schedule_times = list(videos.get("schedule_times") or [])
            obj.shorts = s
            obj.videos = v
            obj.schedule_times = list(channel.get("schedule_times") or [])
            plans.append(
                rq.build_day_pack(
                    state_dir=STATE,
                    root=ROOT,
                    channel=obj,
                    timezone_name=tz,
                    rebuild=rebuild,
                )
            )
        return {"ok": True, "plans": plans, "queue": ready_queue_snapshot()}

    if action == "clear-cancelled":
        removed = rq.clear_cancelled(STATE, [c["id"] for c in CHANNELS])
        return {"ok": True, "removed": removed, "queue": ready_queue_snapshot()}

    if not item_id:
        return {"ok": False, "error": "item_id_required"}

    item = rq.get_item(STATE, item_id)
    if not item:
        return {"ok": False, "error": "not_found"}

    if action == "cancel":
        return {"ok": True, "item": rq.cancel_item(STATE, item_id), "queue": ready_queue_snapshot()}
    if action == "retry":
        return {"ok": True, "item": rq.retry_item(STATE, item_id), "queue": ready_queue_snapshot()}
    if action == "force-render":
        updated = rq.update_item(STATE, item_id, status="planned", force_flags={"force_render": True}, error=None)
        return {"ok": True, "item": updated, "queue": ready_queue_snapshot()}
    if action == "force-upload":
        if str(item.get("status") or "") == "held_quality":
            return {"ok": False, "error": "quality_held", "item": item}
        updated = rq.update_item(
            STATE,
            item_id,
            force_flags={"skip_slot_wait": True, "force_next": True},
        )
        # Best-effort in-process publish when file exists.
        video_path = Path(str(item.get("video_path") or ""))
        if video_path.exists():
            try:
                from yt_auto.pipeline import ShortsFactory

                factory = ShortsFactory(ROOT / "config" / "settings.yaml")
                result = factory.publish_ready_video(
                    str(item.get("channel")),
                    video_path=video_path,
                    run_dir=item.get("run_dir"),
                    content_kind=str(item.get("content_kind") or "short"),
                    force_public=True,
                    title_hint=item.get("title"),
                )
                if result.get("ok"):
                    rq.mark_uploaded(STATE, item_id, youtube_id=result.get("youtube_id"))
                else:
                    err = result.get("youtube_error") or result.get("upload_skipped") or "upload_failed"
                    if result.get("upload_skipped") == "quality_gate":
                        rq.update_item(STATE, item_id, status="held_quality", error=str(err))
                    else:
                        rq.mark_uploaded(STATE, item_id, error=str(err))
                return {"ok": bool(result.get("ok")), "result": result, "queue": ready_queue_snapshot()}
            except Exception as exc:
                return {"ok": False, "error": str(exc), "item": updated, "queue": ready_queue_snapshot()}
        return {"ok": True, "item": updated, "note": "marked_for_next_publish", "queue": ready_queue_snapshot()}

    return {"ok": False, "error": f"unknown_action:{action}"}


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/dashboard"):
            html_path = Path(__file__).with_name("live_dashboard.html")
            body = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/status":
            _json_response(self, build_snapshot())
            return
        if path == "/api/health":
            _json_response(
                self,
                {
                    "ok": True,
                    "service": "live_dashboard",
                    "post_supported": True,
                    "pid": os.getpid(),
                    "ready_queue_apis": True,
                },
            )
            return
        if path == "/api/queue":
            _json_response(self, ready_queue_snapshot())
            return
        if path.startswith("/api/analytics/"):
            channel_id = path.split("/api/analytics/", 1)[-1].strip("/")
            if channel_id not in {c["id"] for c in CHANNELS}:
                _json_response(self, {"ok": False, "error": "unknown_channel"}, status=404)
                return
            _json_response(self, analytics_payload(channel_id))
            return
        if path == "/api/suggestions":
            _json_response(self, suggestions_payload())
            return
        if path == "/api/settings":
            _json_response(self, settings_payload())
            return
        if path == "/api/runs":
            _json_response(self, runs_payload())
            return
        if path.startswith("/api/logs/"):
            name = path.split("/api/logs/", 1)[-1]
            _json_response(self, log_tail(name))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not _require_token(self):
            return
        body = _read_json_body(self)

        if path == "/api/queue/toggle":
            _json_response(self, queue_action("toggle", body=body))
            return
        if path == "/api/queue/day-pack":
            _json_response(self, queue_action("day-pack", body=body))
            return
        if path == "/api/queue/clear-cancelled":
            _json_response(self, queue_action("clear-cancelled", body=body))
            return
        if path.startswith("/api/queue/items/"):
            rest = path[len("/api/queue/items/") :]
            parts = rest.strip("/").split("/")
            if len(parts) != 2:
                _json_response(self, {"ok": False, "error": "bad_path"}, status=400)
                return
            item_id, action = parts[0], parts[1]
            action_map = {
                "force-render": "force-render",
                "force-upload": "force-upload",
                "cancel": "cancel",
                "retry": "retry",
            }
            mapped = action_map.get(action)
            if not mapped:
                _json_response(self, {"ok": False, "error": "unknown_action"}, status=404)
                return
            _json_response(self, queue_action(mapped, item_id=item_id, body=body))
            return
        if path == "/api/refresh-analytics":
            _json_response(self, refresh_analytics(body.get("channel")))
            return
        if path == "/api/settings":
            _json_response(self, patch_settings(body))
            return
        if path == "/api/controls/restart-scheduler":
            _json_response(self, restart_scheduler())
            return
        _json_response(self, {"ok": False, "error": "not_found"}, status=404)


def _acquire_dashboard_singleton() -> object | None:
    """Exclusive lock so watchdog + manual starts cannot double-serve."""
    STATE.mkdir(parents=True, exist_ok=True)
    lock_path = STATE / "dashboard.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main():
    port = int(os.environ.get("YT_DASHBOARD_PORT", "8787"))
    host = os.environ.get("YT_DASHBOARD_HOST", "0.0.0.0")
    lock_handle = _acquire_dashboard_singleton()
    if lock_handle is None:
        print(f"Dashboard already running (lock held); not starting a duplicate on port {port}.")
        return
    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError as exc:
        print(f"Could not bind dashboard on {host}:{port}: {exc}")
        try:
            lock_handle.close()
        except Exception:
            pass
        return
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "dashboard_heartbeat.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": port,
                    "post_supported": True,
                    "updated_at": datetime.now().isoformat(),
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
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
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
