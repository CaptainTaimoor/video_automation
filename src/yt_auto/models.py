from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple


@dataclass
class YouTubeConfig:
    upload_enabled: bool
    client_secrets_file: Path
    token_file: Path
    privacy_status: str = "private"


@dataclass
class HeyGenConfig:
    enabled: bool = False
    use_for_shorts: bool = True
    use_for_videos: bool = False
    avatar_name: str = ""
    avatar_names: List[str] = field(default_factory=list)
    avatar_rotation_mode: str = "sequential"
    avatar_page_url: str = "https://app.heygen.com/avatar/my-avatars"
    motion_engine: str = "Avatar III"
    chrome_user_data_dir: str = ""
    chrome_executable_path: str = ""
    chrome_profile_directory: str = ""
    headless: bool = False
    wait_for_login: bool = True
    login_email: str = ""
    login_password: str = ""
    render_timeout_seconds: int = 900
    allow_native_download_fallback: bool = True
    allow_streamvault_extension_fallback: bool = False
    use_streamvault_extension: bool = False
    extension_path: str = ""


@dataclass
class PresenterConfig:
    enabled: bool = False
    engine: str = "local_composer"
    avatar_pool_dir: Path | None = None
    layout_mode: str = "rotating_avatar_broll"
    rotation_mode: str = "sequential"
    use_for_shorts: bool = True
    use_for_videos: bool = False
    sidecar_python: Path | None = None
    sidecar_root: Path | None = None
    checkpoint_dir: Path | None = None
    rhubarb_path: Path | None = None
    hook_seconds: float = 6.0
    timeout_seconds: int = 1800
    expression_scale: float = 0.9
    pose_style: int = 0
    render_size: int = 256


@dataclass
class FacebookConfig:
    upload_enabled: bool
    page_id: str
    access_token_env_var: str = "FB_PAGE_ACCESS_TOKEN"


@dataclass
class ContentProfile:
    enabled: bool
    schedule_times: List[str]
    min_duration_seconds: int
    max_duration_seconds: int
    target_size: Tuple[int, int]
    # How many of this kind to publish per day. A count is drawn inside the
    # range each day, so the upper half of the range actually gets used;
    # schedule_times is the fallback when no range is configured.
    daily_min: int = 0
    daily_max: int = 0


@dataclass
class MonetizationConfig:
    enabled: bool = True
    long_form_playlist_url: str = ""
    long_form_cta: str = ""
    pinned_comment_template: str = ""
    # Each item: {"label": str, "url_env": str} — URL resolved from .env at runtime.
    affiliates: List[dict] = field(default_factory=list)


@dataclass
class ChannelConfig:
    id: str
    display_name: str
    niche_description: str
    seed_keywords: List[str]
    styles: List[str]
    schedule_times: List[str]
    voice_mode: str
    voices: List[str]
    tts_backend: str
    hashtags: List[str]
    output_dir: Path
    youtube: YouTubeConfig
    facebook: FacebookConfig | None = None
    heygen: HeyGenConfig = field(default_factory=HeyGenConfig)
    presenter: PresenterConfig = field(default_factory=PresenterConfig)
    shorts: ContentProfile = field(default_factory=lambda: ContentProfile(True, [], 30, 60, (1080, 1920)))
    videos: ContentProfile = field(default_factory=lambda: ContentProfile(False, [], 180, 600, (1920, 1080)))
    daily_upload_cap: int = 0
    # Additional FB-specific scheduling
    schedule_interval_hours: int | None = None
    backlog_schedule_interval_minutes: int | None = None
    max_backlog_age_days: int = 0
    schedule_retry_attempts: int = 3
    schedule_retry_delay_minutes: int = 5
    max_upload_gap_hours: float = 4.0
    upload_gap_retry_cooldown_minutes: int = 20
    short_build_timeout_minutes: int = 45
    video_build_timeout_minutes: int = 90
    short_build_stall_minutes: int = 12
    video_build_stall_minutes: int = 25
    # Viral DNA: list of YouTube channel URLs to clone style from
    viral_dna_channels: List[str] = field(default_factory=list)
    # Visual style for AI image generation (documentary, dark-tech, anime, etc.)
    visual_style: str = "documentary"
    monetization: MonetizationConfig = field(default_factory=MonetizationConfig)


@dataclass
class ScriptWriterConfig:
    provider: str
    ollama_model: str
    ollama_url: str
    timeout_seconds: int
    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    provider_order: List[str] = field(default_factory=list)
    provider_cooldown_seconds: int = 180
    ollama_timeout_seconds: int = 90
    openai_compatible_url: str = ""
    openai_compatible_model: str = ""
    # Tried in order when the first is busy. A free model answers 429 as
    # often as it answers, so one of them is not a dependable provider.
    openai_compatible_models: list = field(default_factory=list)
    openai_compatible_api_key_env: str = "AI_GATEWAY_API_KEY"
    openai_compatible_allowed_hosts: List[str] = field(
        default_factory=lambda: [
            "router.huggingface.co",
            "api.groq.com",
            "api.mistral.ai",
        ]
    )
    openai_compatible_timeout_seconds: int = 90
    openai_compatible_disable_thinking: bool = False
    groq_model: str = "openai/gpt-oss-120b"
    groq_url: str = "https://api.groq.com/openai/v1"
    groq_api_key_env: str = "GROQ_API_KEY"
    mistral_model: str = "mistral-small-latest"
    mistral_url: str = "https://api.mistral.ai/v1"
    mistral_api_key_env: str = "MISTRAL_API_KEY"


@dataclass
class SubtitleConfig:
    enabled: bool
    max_words_per_caption: int


@dataclass
class ThumbnailConfig:
    enabled: bool
    width: int
    height: int


@dataclass
class AppCoreConfig:
    timezone: str
    min_duration_seconds: int
    max_duration_seconds: int
    image_count_per_video: int
    output_root: Path
    music_dir: Path
    state_dir: Path
    topic_cache_dir: Path
    script_writer: ScriptWriterConfig
    subtitles: SubtitleConfig
    thumbnail: ThumbnailConfig
    ab_test_enabled: bool
    ab_test_variants: int
    feedback_enabled: bool
    feedback_min_views: int
    feedback_lookback_days: int
    candidate_pool_size: int = 4
    schedule_interval_hours: int = 2
    schedule_uploads: bool = True
    upload_gap_check_minutes: int = 10
    # app.visuals from settings.yaml: scene and fetch budgets, how many
    # searches run at once, stock pages, and the reusable footage library.
    visuals: dict = field(default_factory=dict)


@dataclass
class AppConfig:
    app: AppCoreConfig
    channels: List[ChannelConfig]


@dataclass
class ScenePlanItem:
    narration: str
    visual_text: str
    search_terms: List[str] = field(default_factory=list)
    preferred_image_url: str = ""
    visual_prompt: str = ""


@dataclass
class TopicCandidate:
    niche_id: str
    style: str
    trend_terms: List[str]
    title: str
    hook: str
    narration: str
    visual_captions: List[str]
    source_urls: List[str]
    image_queries: List[str]
    hashtags: List[str]
    engagement_score: float
    content_kind: str = "short"
    subject: str = ""
    narration_beats: List[str] = field(default_factory=list)
    title_variants: List[str] = field(default_factory=list)
    selected_title_pattern: str = ""
    scene_plan: List[ScenePlanItem] = field(default_factory=list)
    transcripts: List[str] = field(default_factory=list)
    # Populated only when model-generated narration for this exact candidate
    # passed editorial validation and was accepted.
    script_provider: str = ""
    script_provider_endpoint_host: str = ""
    script_provider_model: str = ""


@dataclass
class BuildArtifacts:
    run_dir: Path
    topic_path: Path
    narration_path: Path
    video_path: Path
    metadata_path: Path
    sources_path: Path
    image_paths: List[Path] = field(default_factory=list)
    subtitles_path: Path | None = None
    thumbnail_path: Path | None = None
    youtube_video_id: str | None = None
    # FB specific
    facebook_video_id: str | None = None
    upload_provider: str | None = None
    upload_id: str | None = None
    public_url: str | None = None
    visibility_status: str | None = None
    job_kind: str = "new_generation"
    quality_flags: List[str] = field(default_factory=list)
    source_mix: dict[str, int] = field(default_factory=dict)
    content_kind: str = "short"
