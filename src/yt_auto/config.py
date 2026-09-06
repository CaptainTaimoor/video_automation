from __future__ import annotations

import os
from pathlib import Path

import yaml

from yt_auto.models import (
    AppConfig,
    AppCoreConfig,
    ChannelConfig,
    ContentProfile,
    FacebookConfig,
    HeyGenConfig,
    PresenterConfig,
    ScriptWriterConfig,
    SubtitleConfig,
    ThumbnailConfig,
    YouTubeConfig,
)


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (root / p).resolve()


def load_config(path: Path) -> AppConfig:
    root = path.resolve().parent.parent
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    app_raw = raw["app"]

    script_writer_raw = app_raw.get("script_writer", {})
    openai_allowed_hosts_raw = script_writer_raw.get(
        "openai_compatible_allowed_hosts",
        ["router.huggingface.co", "api.groq.com", "api.mistral.ai"],
    )
    if openai_allowed_hosts_raw is None:
        openai_allowed_hosts_raw = []
    elif isinstance(openai_allowed_hosts_raw, str):
        openai_allowed_hosts_raw = [openai_allowed_hosts_raw]
    script_writer = ScriptWriterConfig(
        provider=str(script_writer_raw.get("provider", "template")),
        ollama_model=str(script_writer_raw.get("ollama_model", "qwen2.5:7b")),
        ollama_url=str(script_writer_raw.get("ollama_url", "http://localhost:11434/api/generate")),
        timeout_seconds=min(
            120,
            max(15, int(script_writer_raw.get("timeout_seconds", 120))),
        ),
        gemini_model=str(script_writer_raw.get("gemini_model", "gemini-2.5-flash-lite")),
        gemini_url=str(script_writer_raw.get("gemini_url", "https://generativelanguage.googleapis.com/v1beta/models")),
        provider_order=[
            str(item).strip().lower()
            for item in script_writer_raw.get("provider_order", [])
            if str(item).strip()
        ],
        provider_cooldown_seconds=max(0, int(script_writer_raw.get("provider_cooldown_seconds", 180))),
        ollama_timeout_seconds=min(
            180,
            max(15, int(script_writer_raw.get("ollama_timeout_seconds", 90))),
        ),
        openai_compatible_url=str(script_writer_raw.get("openai_compatible_url", "")),
        openai_compatible_model=str(script_writer_raw.get("openai_compatible_model", "")),
        openai_compatible_api_key_env=str(
            script_writer_raw.get("openai_compatible_api_key_env", "AI_GATEWAY_API_KEY")
        ),
        openai_compatible_allowed_hosts=[
            str(item).strip().lower().rstrip(".")
            for item in openai_allowed_hosts_raw
            if str(item).strip()
        ],
        openai_compatible_timeout_seconds=min(
            120,
            max(
                5,
                int(script_writer_raw.get("openai_compatible_timeout_seconds", 90)),
            ),
        ),
        openai_compatible_disable_thinking=bool(
            script_writer_raw.get("openai_compatible_disable_thinking", False)
        ),
        groq_model=str(script_writer_raw.get("groq_model", "openai/gpt-oss-120b")),
        groq_url=str(script_writer_raw.get("groq_url", "https://api.groq.com/openai/v1")),
        groq_api_key_env=str(script_writer_raw.get("groq_api_key_env", "GROQ_API_KEY")),
        mistral_model=str(script_writer_raw.get("mistral_model", "mistral-small-latest")),
        mistral_url=str(script_writer_raw.get("mistral_url", "https://api.mistral.ai/v1")),
        mistral_api_key_env=str(script_writer_raw.get("mistral_api_key_env", "MISTRAL_API_KEY")),
    )

    subtitles_raw = app_raw.get("subtitles", {})
    subtitles = SubtitleConfig(
        enabled=bool(subtitles_raw.get("enabled", True)),
        max_words_per_caption=int(subtitles_raw.get("max_words_per_caption", 7)),
    )

    thumb_raw = app_raw.get("thumbnail", {})
    thumbnail = ThumbnailConfig(
        enabled=bool(thumb_raw.get("enabled", True)),
        width=int(thumb_raw.get("width", 1280)),
        height=int(thumb_raw.get("height", 720)),
    )

    app = AppCoreConfig(
        timezone=app_raw["timezone"],
        min_duration_seconds=int(app_raw["min_duration_seconds"]),
        max_duration_seconds=int(app_raw["max_duration_seconds"]),
        image_count_per_video=int(app_raw["image_count_per_video"]),
        output_root=_resolve(root, app_raw["output_root"]),
        music_dir=_resolve(root, app_raw["music_dir"]),
        state_dir=_resolve(root, app_raw["state_dir"]),
        topic_cache_dir=_resolve(root, app_raw["topic_cache_dir"]),
        script_writer=script_writer,
        subtitles=subtitles,
        thumbnail=thumbnail,
        ab_test_enabled=bool(app_raw.get("ab_test_enabled", True)),
        ab_test_variants=int(app_raw.get("ab_test_variants", 3)),
        feedback_enabled=bool(app_raw.get("feedback_enabled", True)),
        feedback_min_views=int(app_raw.get("feedback_min_views", 100)),
        feedback_lookback_days=int(app_raw.get("feedback_lookback_days", 30)),
        candidate_pool_size=max(1, int(app_raw.get("candidate_pool_size", 4))),
        schedule_interval_hours=int(app_raw.get("schedule_interval_hours", 2)),
        schedule_uploads=bool(app_raw.get("schedule_uploads", True)),
        upload_gap_check_minutes=max(2, int(app_raw.get("upload_gap_check_minutes", 10))),
    )

    channels = []
    for c in raw["channels"]:
        yt_raw = c.get("youtube", {})
        fb_raw = c.get("facebook")
        heygen_raw = c.get("heygen", {}) or {}
        presenter_raw = c.get("presenter", {}) or {}
        content_raw = c.get("content_profiles", {})

        short_raw = content_raw.get("shorts", {})
        short_times = list(short_raw.get("schedule_times", c.get("schedule_times", [])))
        shorts = ContentProfile(
            enabled=bool(short_raw.get("enabled", True)),
            schedule_times=short_times,
            min_duration_seconds=int(short_raw.get("min_duration_seconds", app.min_duration_seconds)),
            max_duration_seconds=int(short_raw.get("max_duration_seconds", app.max_duration_seconds)),
            target_size=(
                int(short_raw.get("target_width", app_raw.get("thumbnail", {}).get("width", 1080))),
                int(short_raw.get("target_height", app_raw.get("thumbnail", {}).get("height", 1920))),
            ),
        )

        video_raw = content_raw.get("videos", {})
        videos = ContentProfile(
            enabled=bool(video_raw.get("enabled", False)),
            schedule_times=list(video_raw.get("schedule_times", [])),
            min_duration_seconds=int(video_raw.get("min_duration_seconds", max(65, app.max_duration_seconds))),
            max_duration_seconds=int(video_raw.get("max_duration_seconds", max(120, app.max_duration_seconds + 60))),
            target_size=(
                int(video_raw.get("target_width", 1920)),
                int(video_raw.get("target_height", 1080)),
            ),
        )

        fb_config = None
        if fb_raw:
            fb_config = FacebookConfig(
                upload_enabled=bool(fb_raw.get("upload_enabled", False)),
                page_id=str(fb_raw.get("page_id", "")),
                access_token_env_var=str(fb_raw.get("access_token_env_var", "FB_PAGE_ACCESS_TOKEN")),
            )

        channel = ChannelConfig(
            id=c["id"],
            display_name=c["display_name"],
            niche_description=c["niche_description"],
            seed_keywords=list(c["seed_keywords"]),
            styles=list(c["styles"]),
            schedule_times=list(c.get("schedule_times", short_times)),
            voice_mode=c["voice_mode"],
            voices=list(c["voices"]),
            tts_backend=str(c.get("tts_backend", "")),
            hashtags=list(c["hashtags"]),
            output_dir=_resolve(root, c["output_dir"]),
            youtube=YouTubeConfig(
                upload_enabled=bool(yt_raw.get("upload_enabled", False)),
                client_secrets_file=_resolve(root, yt_raw.get("client_secrets_file", "client_secrets.json")),
                token_file=_resolve(root, yt_raw.get("token_file", str(Path("secrets") / "tokens" / f"{c['id']}_token.json"))),
                privacy_status=yt_raw.get("privacy_status", "private"),
            ),
            facebook=fb_config,
            heygen=HeyGenConfig(
                enabled=bool(heygen_raw.get("enabled", False)),
                use_for_shorts=bool(heygen_raw.get("use_for_shorts", True)),
                use_for_videos=bool(heygen_raw.get("use_for_videos", False)),
                avatar_name=str(heygen_raw.get("avatar_name", "")),
                avatar_names=list(heygen_raw.get("avatar_names", [])),
                avatar_rotation_mode=str(heygen_raw.get("avatar_rotation_mode", "sequential")),
                avatar_page_url=str(heygen_raw.get("avatar_page_url", "https://app.heygen.com/avatar/my-avatars")),
                motion_engine=str(heygen_raw.get("motion_engine", "Avatar III")),
                chrome_user_data_dir=str(heygen_raw.get("chrome_user_data_dir", "")),
                chrome_executable_path=str(heygen_raw.get("chrome_executable_path", "")),
                chrome_profile_directory=str(heygen_raw.get("chrome_profile_directory", "")),
                headless=bool(heygen_raw.get("headless", False)),
                wait_for_login=bool(heygen_raw.get("wait_for_login", True)),
                login_email=str(heygen_raw.get("login_email", os.getenv("HEYGEN_EMAIL", ""))),
                login_password=str(heygen_raw.get("login_password", os.getenv("HEYGEN_PASSWORD", ""))),
                render_timeout_seconds=int(heygen_raw.get("render_timeout_seconds", 900)),
                allow_native_download_fallback=bool(heygen_raw.get("allow_native_download_fallback", True)),
                allow_streamvault_extension_fallback=bool(heygen_raw.get("allow_streamvault_extension_fallback", False)),
                use_streamvault_extension=bool(heygen_raw.get("use_streamvault_extension", False)),
                extension_path=str(heygen_raw.get("extension_path", "")),
            ),
            presenter=PresenterConfig(
                enabled=bool(presenter_raw.get("enabled", False)),
                engine=str(presenter_raw.get("engine", "local_composer")),
                avatar_pool_dir=_resolve(root, str(presenter_raw.get("avatar_pool_dir", "assets/avatars/brain_lens"))),
                layout_mode=str(presenter_raw.get("layout_mode", "rotating_avatar_broll")),
                rotation_mode=str(presenter_raw.get("rotation_mode", "sequential")),
                use_for_shorts=bool(presenter_raw.get("use_for_shorts", True)),
                use_for_videos=bool(presenter_raw.get("use_for_videos", False)),
                sidecar_python=_resolve(root, str(presenter_raw.get("sidecar_python", ".runtime/sadtalker_env/Scripts/python.exe"))),
                sidecar_root=_resolve(root, str(presenter_raw.get("sidecar_root", ".runtime/SadTalker"))),
                checkpoint_dir=_resolve(root, str(presenter_raw.get("checkpoint_dir", ".runtime/SadTalker/checkpoints"))),
                rhubarb_path=_resolve(root, str(presenter_raw.get(
                    "rhubarb_path",
                    ".runtime/rhubarb-1.14.0/Rhubarb-Lip-Sync-1.14.0-Windows/rhubarb.exe",
                ))),
                hook_seconds=float(presenter_raw.get("hook_seconds", 6.0)),
                timeout_seconds=int(presenter_raw.get("timeout_seconds", 1800)),
                expression_scale=float(presenter_raw.get("expression_scale", 0.9)),
                pose_style=int(presenter_raw.get("pose_style", 0)),
                render_size=int(presenter_raw.get("render_size", 256)),
            ),
            shorts=shorts,
            videos=videos,
            daily_upload_cap=int(c.get("daily_upload_cap", 0)),
            schedule_interval_hours=c.get("schedule_interval_hours"),
            backlog_schedule_interval_minutes=c.get("backlog_schedule_interval_minutes"),
            max_backlog_age_days=int(c.get("max_backlog_age_days", 0)),
            schedule_retry_attempts=int(c.get("schedule_retry_attempts", 3)),
            schedule_retry_delay_minutes=int(c.get("schedule_retry_delay_minutes", 5)),
            max_upload_gap_hours=max(1.0, float(c.get("max_upload_gap_hours", 4.0))),
            upload_gap_retry_cooldown_minutes=max(
                15,
                int(c.get("upload_gap_retry_cooldown_minutes", 20)),
            ),
            short_build_timeout_minutes=max(
                15,
                int(c.get("short_build_timeout_minutes", 45)),
            ),
            video_build_timeout_minutes=max(
                30,
                int(c.get("video_build_timeout_minutes", 90)),
            ),
            short_build_stall_minutes=max(
                5,
                int(c.get("short_build_stall_minutes", 12)),
            ),
            video_build_stall_minutes=max(
                10,
                int(c.get("video_build_stall_minutes", 25)),
            ),
            viral_dna_channels=list(c.get("viral_dna_channels", [])),
            visual_style=str(c.get("visual_style", "documentary")),
        )
        channels.append(channel)


    return AppConfig(app=app, channels=channels)
