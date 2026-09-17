from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from yt_auto.pipeline import ShortsFactory, _load_optional_dotenv
from yt_auto.scheduler_service import ScheduleService
from yt_auto.utils import ensure_dir


def _config_path() -> Path:
    _load_optional_dotenv()
    env_path = os.getenv("YT_AUTOMATION_CONFIG", "config/settings.yaml")
    return Path(env_path)


def cmd_init(factory: ShortsFactory) -> int:
    ensure_dir(factory.config.app.output_root)
    ensure_dir(factory.config.app.music_dir)
    ensure_dir(factory.config.app.state_dir)
    ensure_dir(factory.config.app.topic_cache_dir)

    for channel in factory.config.channels:
        ensure_dir(channel.output_dir)
        ensure_dir(channel.youtube.token_file.parent)

    print("Initialization complete.")
    print("Put copyright-safe music files in assets/music.")
    print("Keep YouTube upload in draft mode until tests pass.")
    return 0


def cmd_build(factory: ShortsFactory, args: argparse.Namespace) -> int:
    artifacts = factory.build_one(
        channel_id=args.channel,
        upload=False if getattr(args, "dry_run", False) else args.upload,
        force_public=args.public,
        content_kind=args.kind,
    )
    print(f"Build complete: {artifacts.video_path}")
    if artifacts.youtube_video_id:
        print(f"YouTube video id: {artifacts.youtube_video_id}")
    return 0


def cmd_schedule(factory: ShortsFactory, args: argparse.Namespace) -> int:
    schedule = ScheduleService(factory)
    upload = None
    if args.upload:
        upload = True
    elif args.no_upload:
        upload = False
    schedule.run(upload=upload, channels=args.channel, mode=args.mode)
    return 0


def cmd_channels(factory: ShortsFactory) -> int:
    print("Configured channels:")
    for channel in factory.config.channels:
        print(f"- {channel.id}: {channel.display_name}")
    return 0


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _tail_jsonl(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    rows: deque[dict] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return list(rows)


def _scheduler_processes() -> list[dict]:
    if os.name == "nt":
        command = (
            "$procs = Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'run.py schedule' } | "
            "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine; "
            "$procs | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=10,
            )
            raw = result.stdout.strip()
            if not raw:
                return []
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []

    try:
        result = subprocess.run(["ps", "-eo", "pid,ppid,comm,args"], capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    processes = []
    for line in result.stdout.splitlines()[1:]:
        if "run.py schedule" not in line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        processes.append({"ProcessId": parts[0], "ParentProcessId": parts[1], "ExecutablePath": parts[2], "CommandLine": parts[3]})
    return processes


def _scheduler_heartbeat(factory: ShortsFactory) -> tuple[str, str]:
    data = _read_json(factory.config.app.state_dir / "scheduler_heartbeat.json", {})
    updated = data.get("updated_at") if isinstance(data, dict) else None
    if not updated:
        return "missing", "no heartbeat file yet"
    try:
        dt = datetime.fromisoformat(str(updated))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        age = max(0, int((now - dt).total_seconds()))
    except Exception:
        return "bad", "heartbeat timestamp unreadable"
    state = "live" if age <= 180 else "stale"
    note = str(data.get("note") or "")
    return state, f"{age}s ago{f' ({note})' if note else ''}"


def _short_title(text: str, max_len: int = 62) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _token_status(path: Path) -> str:
    if not path.exists():
        return "MISSING - run auth"
    data = _read_json(path, {})
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return "INVALID - re-auth"
    expiry_raw = str(data.get("expiry") or "").replace("Z", "+00:00")
    try:
        expiry = datetime.fromisoformat(expiry_raw)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except ValueError:
        return "OK - refresh token saved"
    if expiry <= datetime.now(timezone.utc):
        return "OK - access expired, refreshable"
    return f"OK - access until {expiry.strftime('%Y-%m-%d %H:%M UTC')}"


def _backlog_status(path: Path) -> tuple[int, str]:
    data = _read_json(path, {})
    items = data.get("items", []) if isinstance(data, dict) else []
    counts = Counter(str(item.get("status", "unknown")) for item in items if isinstance(item, dict))
    pending = sum(counts.get(status, 0) for status in ("pending", "failed", "in_progress"))
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "empty"
    return pending, summary


def _report_status(path: Path) -> tuple[str, str, str]:
    if not path.exists():
        return "no report", "top: n/a", "weak: n/a"
    report = _read_json(path, {})
    summary = report.get("analytics_summary") or {}
    modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    if summary and not summary.get("error"):
        headline = (
            f"views={summary.get('total_views', 0)} "
            f"engaged={summary.get('engaged_view_rate', 0)} "
            f"avg_pct={summary.get('average_view_percentage', 0)}% "
            f"subs={summary.get('subscribers_gained', 0)} "
            f"report={modified}"
        )
    else:
        headline = f"report={modified}"

    top = report.get("top_videos") or []
    weak = report.get("bottom_videos") or []
    top_text = "top: " + (" | ".join(f"{row.get('views', 0)} {_short_title(row.get('title', ''))}" for row in top[:2]) or "n/a")
    weak_text = "weak: " + (" | ".join(f"{row.get('views', 0)} {_short_title(row.get('title', ''))}" for row in weak[:2]) or "n/a")
    return headline, top_text, weak_text


def _report_metrics(path: Path) -> dict:
    if not path.exists():
        return {"views": "-", "retention": "-", "subs": "-", "updated": "no report", "top": "n/a", "weak": "n/a", "formats": "n/a"}
    report = _read_json(path, {})
    summary = report.get("analytics_summary") or {}
    formats = report.get("format_summary") or {}
    modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
    top = report.get("top_videos") or []
    weak = report.get("bottom_videos") or []
    retention = summary.get("average_view_percentage")
    short_row = formats.get("shorts") or {}
    long_row = formats.get("long_videos") or {}
    def _format_percentage(value) -> str:
        return "n/a" if value in (None, "") else f"{float(value):.1f}%"

    format_text = (
        f"shorts={short_row.get('views', 0)}v/{_format_percentage(short_row.get('average_view_percentage'))} "
        f"long={long_row.get('views', 0)}v/{_format_percentage(long_row.get('average_view_percentage'))}"
        if formats else "n/a"
    )
    return {
        "views": str(summary.get("total_views", "-")),
        "retention": "-" if retention in (None, "") else f"{float(retention):.1f}%",
        "subs": str(summary.get("subscribers_gained", "-")),
        "updated": modified,
        "top": " | ".join(f"{row.get('views', 0)} {_short_title(row.get('title', ''), 58)}" for row in top[:2]) or "n/a",
        "weak": " | ".join(f"{row.get('views', 0)} {_short_title(row.get('title', ''), 58)}" for row in weak[:2]) or "n/a",
        "formats": format_text,
    }


def _token_label(status: str) -> str:
    if status.startswith("OK"):
        return "OK"
    if status.startswith("MISSING") or status.startswith("INVALID"):
        return "AUTH"
    return "CHECK"


def _last_run_label(run: dict) -> str:
    if not run:
        return "-"
    if run.get("uploaded"):
        return "OK"
    if run.get("upload_skipped"):
        return "SKIP"
    return "FAIL"


def _display_last_label(run: dict, token: str, pending: int) -> str:
    label = _last_run_label(run)
    if label == "FAIL" and token == "OK" and pending == 0:
        error = str(run.get("youtube_error") or "")
        if "invalid_grant" in error or "expired or revoked" in error:
            return "FIXED"
    return label


def _issue_text(run: dict) -> str:
    reason = run.get("youtube_error") or run.get("upload_blocked") or run.get("upload_skipped") or "not uploaded"
    return f"{run.get('channel', '?')}: {_short_title(run.get('title', ''), 46)} -> {_short_title(str(reason), 90)}"


def _print_status(factory: ShortsFactory, detailed: bool = False) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    processes = _scheduler_processes()
    pids = ", ".join(str(proc.get("ProcessId")) for proc in processes if proc.get("ProcessId"))
    heartbeat_state, heartbeat_label = _scheduler_heartbeat(factory)
    print("=" * 72)
    print(f"YOUTUBE BOT LIVE STATUS   {now}")
    print("=" * 72)
    print(f"Scheduler : {'RUNNING' if pids else 'STOPPED'}{f' ({pids})' if pids else ''} | heartbeat {heartbeat_state}: {heartbeat_label}")

    runs = _tail_jsonl(factory.config.app.state_dir / "runs.jsonl", limit=300)
    continuity = _read_json(
        factory.config.app.state_dir / "upload_continuity_state.json",
        {"channels": {}},
    )
    continuity_channels = continuity.get("channels", {}) if isinstance(continuity, dict) else {}
    recent_issues = [
        run for run in runs[-80:]
        if str(run.get("upload_skipped") or "").lower() != "build_only"
        if run.get("youtube_error") or run.get("upload_blocked") or run.get("upload_skipped") or (run.get("uploaded") is False and not run.get("youtube_id"))
    ]

    actions: list[str] = []
    detail_lines: list[str] = []
    print()
    print(f"{'CHANNEL':<17} {'TOKEN':<6} {'BACKLOG':<7} {'VIEWS':<8} {'RET':<7} {'SUBS':<5} {'LAST':<5} {'REPORT':<11}")
    print("-" * 72)
    for channel in factory.config.channels:
        backlog_path = factory.config.app.state_dir / f"{channel.id}_backlog.json"
        pending, backlog_summary = _backlog_status(backlog_path)
        metrics = _report_metrics(factory.config.app.state_dir / f"{channel.id}_performance_report.json")
        token_full = _token_status(channel.youtube.token_file) if channel.youtube.upload_enabled else "disabled"
        token = _token_label(token_full)
        channel_runs = [
            run for run in runs
            if run.get("channel") == channel.id
            and str(run.get("upload_skipped") or "").lower() != "build_only"
        ]
        last_run = channel_runs[-1] if channel_runs else {}
        last_label = _display_last_label(last_run, token, pending)
        continuity_entry = (
            continuity_channels.get(channel.id, {})
            if isinstance(continuity_channels, dict)
            else {}
        )
        gap_hours = continuity_entry.get("gap_hours")
        threshold_hours = float(
            continuity_entry.get("threshold_hours")
            or getattr(channel, "max_upload_gap_hours", 4.0)
            or 4.0
        )
        try:
            overdue = gap_hours is not None and float(gap_hours) > threshold_hours
        except (TypeError, ValueError):
            overdue = False
        if overdue:
            last_label = "GAP"
        print(
            f"{channel.id:<17} {token:<6} {pending:<7} {metrics['views']:<8} "
            f"{metrics['retention']:<7} {metrics['subs']:<5} {last_label:<5} {metrics['updated']:<11}"
        )
        if not pids:
            actions.append("Start scheduler.")
        elif heartbeat_state == "stale":
            actions.append("Restart scheduler: heartbeat is stale.")
        if token != "OK":
            actions.append(f"{channel.id}: run auth.")
        if pending:
            actions.append(f"{channel.id}: process {pending} backlog item(s).")
        if overdue:
            failure_category = str(
                continuity_entry.get("last_failure_category") or "unknown"
            )
            actions.append(
                f"{channel.id}: upload gap {float(gap_hours):.1f}h; "
                f"automatic recovery is handling {failure_category}."
            )
        try:
            retention_value = float(str(metrics["retention"]).rstrip("%"))
            if retention_value < 45:
                actions.append(f"{channel.id}: retention is weak ({metrics['retention']}); improve first 2 seconds.")
        except ValueError:
            pass
        detail_lines.extend([
            f"{channel.id} token : {token_full}",
            f"{channel.id} backlog: {backlog_summary}",
            f"{channel.id} formats: {metrics['formats']}",
            f"{channel.id} top    : {metrics['top']}",
            f"{channel.id} weak   : {metrics['weak']}",
            f"{channel.id} last   : {last_label} {_short_title(last_run.get('title', ''), 68)}",
            (
                f"{channel.id} recovery: {continuity_entry.get('status', 'unknown')} | "
                f"gap={gap_hours if gap_hours is not None else '-'}h | "
                f"stage={continuity_entry.get('recovery_stage', 0)} | "
                f"{_short_title(str(continuity_entry.get('last_failure_summary') or ''), 90)}"
            ),
        ])

    print("-" * 72)
    current_actions = []
    for action in actions:
        if action not in current_actions:
            current_actions.append(action)
    if current_actions:
        print("Action needed:")
        for action in current_actions[:5]:
            print(f"- {action}")
    else:
        print("Action needed: none")

    if detailed:
        print()
        print("Details:")
        for line in detail_lines:
            print(f"- {line}")
        if recent_issues:
            print("Recent historical issues:")
            for issue in recent_issues[-5:]:
                print(f"- {_issue_text(issue)}")
        else:
            print("Recent historical issues: none")
    else:
        print("Tip: add --details for top/weak videos and recent issue history.")
    return 0


def cmd_status(factory: ShortsFactory, args: argparse.Namespace) -> int:
    watch = int(getattr(args, "watch", 0) or 0)
    detailed = bool(getattr(args, "details", False))
    if watch <= 0:
        return _print_status(factory, detailed=detailed)

    try:
        while True:
            if os.name == "nt":
                os.system("cls")
            else:
                os.system("clear")
            _print_status(factory, detailed=detailed)
            print(f"\nRefreshing every {watch}s. Press Ctrl+C to stop.")
            time.sleep(watch)
    except KeyboardInterrupt:
        print("\nStopped live status.")
        return 0


def cmd_auth(factory: ShortsFactory, args: argparse.Namespace) -> int:
    channel = factory._channel(args.channel)
    result = factory.youtube.authorize(channel)
    print(f"Authorized channel: {result.get('title', '')} ({result.get('channel_id', '')})")
    print(f"Saved token: {channel.youtube.token_file}")
    return 0


def cmd_unblock_upload(factory: ShortsFactory, args: argparse.Namespace) -> int:
    factory._clear_upload_block(args.channel)
    print(f"Cleared upload block for {args.channel}")
    return 0


def cmd_feedback_sync(factory: ShortsFactory, args: argparse.Namespace) -> int:
    result = factory.sync_feedback(channel_id=args.channel, max_results=args.max_results)
    print(f"Feedback sync complete for {args.channel}")
    print(f"Checked: {result['checked']}, Applied: {result['applied']}, Skipped: {result['skipped']}")
    return 0


def cmd_feedback_report(factory: ShortsFactory, args: argparse.Namespace) -> int:
    report = factory.feedback_report(channel_id=args.channel)
    print(f"Feedback report for {args.channel}")
    print("Top styles:")
    for name, meta in report.get("top_styles", []):
        print(f"- {name}: score={meta.get('score')} n={meta.get('n')}")
    print("Top title patterns:")
    for name, meta in report.get("top_patterns", []):
        print(f"- {name}: score={meta.get('score')} n={meta.get('n')}")
    print("Top terms:")
    for name, meta in report.get("top_terms", []):
        print(f"- {name}: score={meta.get('score')} n={meta.get('n')}")
    return 0


def cmd_performance_report(factory: ShortsFactory, args: argparse.Namespace) -> int:
    report = factory.performance_report(
        channel_id=args.channel,
        days=args.days,
        max_results=args.max_results,
    )
    print(f"Performance report for {args.channel}")
    print(f"Saved: {factory.config.app.state_dir / f'{args.channel}_performance_report.json'}")
    summary = report.get("analytics_summary") or {}
    if summary and not summary.get("error"):
        print(
            "Analytics summary: "
            f"views={summary.get('total_views', 0)} "
            f"engaged_rate={summary.get('engaged_view_rate', 0)} "
            f"avg_view={summary.get('average_view_duration', 0)}s "
            f"avg_pct={summary.get('average_view_percentage', 0)}% "
            f"subs={summary.get('subscribers_gained', 0)}"
        )
    format_summary = report.get("format_summary") or {}
    if format_summary:
        print("Format split:")
        for label, key in (("Shorts", "shorts"), ("Long videos", "long_videos")):
            row = format_summary.get(key) or {}
            engaged_rate = row.get("engaged_view_rate")
            average_view = row.get("average_view_duration")
            average_percentage = row.get("average_view_percentage")
            print(
                f"- {label}: count={row.get('count', 0)} views={row.get('views', 0)} "
                f"engaged_rate={'n/a' if engaged_rate is None else engaged_rate} "
                f"avg_view={'n/a' if average_view is None else f'{average_view}s'} "
                f"avg_pct={'n/a' if average_percentage is None else f'{average_percentage}%'}"
            )
            top_rows = row.get("top") or []
            if top_rows:
                top = top_rows[0]
                print(f"  top: {top.get('views', 0)} views | {top.get('title', 'n/a')}")
            weak_rows = row.get("weak") or []
            if weak_rows:
                weak = weak_rows[0]
                print(f"  weak: {weak.get('views', 0)} views | {weak.get('title', 'n/a')}")
    print("Best publish hours:")
    for row in report.get("best_publish_hours", [])[:8]:
        print(f"- {row['key']:02d}:00 avg_views={row['avg_views']} n={row['count']} max={row['max_views']}")
    print("Top videos:")
    for row in report.get("top_videos", [])[:8]:
        print(f"- {row['views']} views | {row['title']}")
    if report.get("top_by_analytics"):
        print("Top by virality score:")
        for row in report.get("top_by_analytics", [])[:8]:
            print(
                f"- score={row['virality_score']} views={row['views']} "
                f"confidence={row.get('sample_confidence', 0)} "
                f"engaged_rate={row['engaged_view_rate']} "
                f"avg_view={row['average_view_duration']}s "
                f"avg_pct={row.get('average_view_percentage', 0)}% | {row['title']}"
            )
    if report.get("recommendations"):
        print("Recommendations:")
        for note in report.get("recommendations", [])[:10]:
            print(f"- {note}")
    if report.get("analytics_api", {}).get("error"):
        print(f"Analytics API note: {report['analytics_api']['error']}")
    return 0


def cmd_backlog_init(factory: ShortsFactory, args: argparse.Namespace) -> int:
    items = factory.initialize_backlog(args.channel, rebuild=getattr(args, "rebuild", False))
    print(f"Backlog initialized for {args.channel}. {len(items)} items found.")
    return 0


def cmd_backlog_run(factory: ShortsFactory, args: argparse.Namespace) -> int:
    channel_id = args.channel
    print(f"Processing backlog items for {channel_id}...")
    count = 0
    limit = getattr(args, "limit", 5)
    while count < limit:
        success = factory.replay_backlog_once(channel_id)
        if not success:
            break
        count += 1
        print(f"   -> Item {count} posted.")

    print(f"Backlog run complete. {count} items processed.")
    return 0


def cmd_analyze(factory: ShortsFactory, args: argparse.Namespace) -> int:
    """Run Viral DNA Channel Cloner for the specified channel."""
    from yt_auto.channel_analyzer import ChannelAnalyzer
    from yt_auto.config import load_config

    channel_id = args.channel
    channel = factory._channel(channel_id)
    state_dir = factory.config.app.state_dir
    app_cfg = factory.config.app

    if not channel.viral_dna_channels:
        print(f"No viral_dna_channels configured for '{channel_id}' in settings.yaml.")
        print("Add: viral_dna_channels:\n  - https://www.youtube.com/@SomeChannel")
        return 1

    analyzer = ChannelAnalyzer(
        ollama_url=app_cfg.script_writer.ollama_url,
        ollama_model=app_cfg.script_writer.ollama_model,
        timeout=300,
    )

    max_videos = getattr(args, "max_videos", 15)
    all_dna = {}

    for channel_url in channel.viral_dna_channels:
        print(f"\n{'='*60}")
        dna = analyzer.analyze(
            channel_url=channel_url,
            state_dir=state_dir,
            channel_id=channel_id,
            max_videos=max_videos,
        )
        # Merge video_ideas from multiple channels
        if all_dna:
            existing_ideas = all_dna.get("video_ideas", [])
            new_ideas = dna.get("video_ideas", [])
            all_dna["video_ideas"] = (existing_ideas + new_ideas)[:20]
        else:
            all_dna = dna

    if all_dna:
        import json
        out_path = state_dir / f"{channel_id}_viral_dna.json"
        out_path.write_text(json.dumps(all_dna, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n{'='*60}")
        print(f"[DONE] Viral DNA analysis complete!")
        print(f"   Channel: {channel_id}")
        print(f"   Saved to: {out_path}")
        ideas = all_dna.get("video_ideas", [])
        if ideas:
            print(f"\n{len(ideas)} video ideas generated:")
            for i, idea in enumerate(ideas[:10], 1):
                print(f"  {i}. {idea.get('title', '?')}")
                if idea.get('angle'):
                    print(f"     -> {idea['angle']}")
        print(f"\nHook patterns extracted:")
        for pat in all_dna.get("hook_patterns", [])[:3]:
            print(f"  * {pat}")
        print(f"\nTone: {all_dna.get('tone', 'N/A')[:120]}")
        print(f"\nThese will automatically be used in future video generation for '{channel_id}'.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified YouTube and Facebook automation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create required directories")
    sub.add_parser("channels", help="List configured channels")

    p_status = sub.add_parser("status", help="Show bot health, backlog, token, and latest report status")
    p_status.add_argument("--watch", type=int, default=0, help="Refresh every N seconds for live status")
    p_status.add_argument("--details", action="store_true", help="Show top/weak videos and recent issue history")

    p_auth = sub.add_parser("auth", help="Authorize a YouTube channel")
    p_auth.add_argument("--channel", required=True, help="Channel id from config")

    p_build = sub.add_parser("build", help="Build one short or full video")
    p_build.add_argument("--channel", required=True, help="Channel id from config")
    p_build.add_argument("--kind", choices=["short", "video"], default="short")
    p_build.add_argument("--upload", action="store_true", help="Upload to all enabled platforms")
    p_build.add_argument("--public", action="store_true", help="Override privacy")
    p_build.add_argument("--dry-run", action="store_true", help="Build locally only")

    p_schedule = sub.add_parser("schedule", help="Run scheduler loop")
    p_schedule.add_argument("--upload", action="store_true")
    p_schedule.add_argument("--no-upload", action="store_true")
    p_schedule.add_argument("--channel", action="append")
    p_schedule.add_argument("--mode", choices=["interval", "cron"], default="cron")

    p_backlog_init = sub.add_parser("backlog-init", help="Scan for un-uploaded previous builds")
    p_backlog_init.add_argument("--channel", required=True)
    p_backlog_init.add_argument("--rebuild", action="store_true", help="Forget previous backlog state")

    p_backlog_run = sub.add_parser("backlog-run", help="Upload items from backlog")
    p_backlog_run.add_argument("--channel", required=True)
    p_backlog_run.add_argument("--limit", type=int, default=5)

    p_unblock = sub.add_parser("unblock-upload", help="Clear upload pause")
    p_unblock.add_argument("--channel", required=True)

    p_sync = sub.add_parser("feedback-sync", help="Sync YouTube metrics")
    p_sync.add_argument("--channel", required=True)
    p_sync.add_argument("--max-results", type=int, default=25)

    p_report = sub.add_parser("feedback-report", help="Show learned preferences")
    p_report.add_argument("--channel", required=True)

    p_perf = sub.add_parser("performance-report", help="Analyze recent performance and posting hours")
    p_perf.add_argument("--channel", required=True)
    p_perf.add_argument("--days", type=int, default=28)
    p_perf.add_argument("--max-results", type=int, default=100)

    p_analyze = sub.add_parser("analyze", help="Run Viral DNA channel analysis")
    p_analyze.add_argument("--channel", required=True, help="Channel id from config")
    p_analyze.add_argument("--max-videos", type=int, default=15,
                           help="Max videos to analyze per reference channel (default: 15)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    cfg = _config_path()
    factory = ShortsFactory(cfg)

    if args.command == "init":
        return cmd_init(factory)
    if args.command == "build":
        return cmd_build(factory, args)
    if args.command == "schedule":
        return cmd_schedule(factory, args)
    if args.command == "channels":
        return cmd_channels(factory)
    if args.command == "status":
        return cmd_status(factory, args)
    if args.command == "auth":
        return cmd_auth(factory, args)
    if args.command == "backlog-init":
        return cmd_backlog_init(factory, args)
    if args.command == "backlog-run":
        return cmd_backlog_run(factory, args)
    if args.command == "feedback-sync":
        return cmd_feedback_sync(factory, args)
    if args.command == "unblock-upload":
        return cmd_unblock_upload(factory, args)
    if args.command == "feedback-report":
        return cmd_feedback_report(factory, args)
    if args.command == "performance-report":
        return cmd_performance_report(factory, args)
    if args.command == "analyze":
        return cmd_analyze(factory, args)

    parser.print_help()
    return 1
