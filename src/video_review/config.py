from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


load_dotenv()


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=lambda: Path(os.getenv("VIDEO_REVIEW_DATA_DIR", "data")))
    database_url: str | None = os.getenv("DATABASE_URL") or None
    database_pool_min_size: int = int(os.getenv("DATABASE_POOL_MIN_SIZE", "1"))
    database_pool_max_size: int = int(os.getenv("DATABASE_POOL_MAX_SIZE", "8"))
    database_connect_timeout: float = float(os.getenv("DATABASE_CONNECT_TIMEOUT", "5"))
    redis_url: str | None = os.getenv("REDIS_URL") or None
    redis_review_stream: str = os.getenv("REDIS_REVIEW_STREAM", "sn2s:video_review:jobs")
    redis_preprocess_stream: str = os.getenv("REDIS_PREPROCESS_STREAM", "sn2s:video_review:preprocess")
    redis_model_stream: str = os.getenv("REDIS_MODEL_STREAM", "sn2s:video_review:model")
    redis_dead_stream: str = os.getenv("REDIS_DEAD_STREAM", "sn2s:video_review:jobs:dead")
    redis_review_group: str = os.getenv("REDIS_REVIEW_GROUP", "video-review-workers")
    redis_preprocess_group: str = os.getenv("REDIS_PREPROCESS_GROUP", "video-review-preprocess-workers")
    redis_model_group: str = os.getenv("REDIS_MODEL_GROUP", "video-review-model-workers")
    pipeline_mode: str = os.getenv("VIDEO_REVIEW_PIPELINE_MODE", "single")
    redis_cache_prefix: str = os.getenv("REDIS_CACHE_PREFIX", "sn2s:video_review:cache")
    redis_cache_ttl_seconds: int = int(os.getenv("REDIS_CACHE_TTL_SECONDS", "604800"))
    redis_connect_timeout: float = float(os.getenv("REDIS_CONNECT_TIMEOUT", "5"))
    redis_block_ms: int = int(os.getenv("REDIS_BLOCK_MS", "5000"))
    redis_pending_claim_min_idle_ms: int = int(os.getenv("REDIS_PENDING_CLAIM_MIN_IDLE_MS", "60000"))
    redis_pending_heartbeat_seconds: int = int(os.getenv("REDIS_PENDING_HEARTBEAT_SECONDS", "15"))
    redis_global_active_key: str = os.getenv(
        "REDIS_GLOBAL_ACTIVE_KEY",
        "sn2s:video_review:active_reviews",
    )
    redis_model_qpm_key: str = os.getenv("REDIS_MODEL_QPM_KEY", "sn2s:video_review:model_qpm")
    redis_model_active_key: str = os.getenv("REDIS_MODEL_ACTIVE_KEY", "sn2s:video_review:model_active")
    redis_model_health_key: str = os.getenv("REDIS_MODEL_HEALTH_KEY", "sn2s:video_review:model_health")
    redis_model_circuit_key: str = os.getenv("REDIS_MODEL_CIRCUIT_KEY", "sn2s:video_review:model_circuit")
    redis_model_key_pool_prefix: str = os.getenv(
        "REDIS_MODEL_KEY_POOL_PREFIX",
        "sn2s:video_review:model_key",
    )
    redis_download_retry_key: str = os.getenv("REDIS_DOWNLOAD_RETRY_KEY", "sn2s:video_review:download_retry")
    redis_download_host_prefix: str = os.getenv("REDIS_DOWNLOAD_HOST_PREFIX", "sn2s:video_review:download_host")
    model_qpm_limit: int = int(os.getenv("VIDEO_REVIEW_MODEL_QPM_LIMIT", "500"))
    model_qpm_wait_seconds: float = float(os.getenv("VIDEO_REVIEW_MODEL_QPM_WAIT_SECONDS", "60"))
    download_concurrency_per_process: int = int(os.getenv("VIDEO_REVIEW_DOWNLOAD_CONCURRENCY_PER_PROCESS", "1"))
    download_total_timeout_seconds: float = float(os.getenv("VIDEO_REVIEW_DOWNLOAD_TOTAL_TIMEOUT_SECONDS", "600"))
    download_retry_attempts: int = int(os.getenv("VIDEO_REVIEW_DOWNLOAD_RETRY_ATTEMPTS", "3"))
    download_retry_delay_seconds: float = float(os.getenv("VIDEO_REVIEW_DOWNLOAD_RETRY_DELAY_SECONDS", "1"))
    download_retry_jitter_seconds: float = float(os.getenv("VIDEO_REVIEW_DOWNLOAD_RETRY_JITTER_SECONDS", "0.5"))
    download_connect_timeout_seconds: float = float(os.getenv("VIDEO_REVIEW_DOWNLOAD_CONNECT_TIMEOUT_SECONDS", "15"))
    download_host_concurrency_limit: int = int(os.getenv("VIDEO_REVIEW_DOWNLOAD_HOST_CONCURRENCY_LIMIT", "8"))
    download_host_slot_ttl_seconds: int = int(os.getenv("VIDEO_REVIEW_DOWNLOAD_HOST_SLOT_TTL_SECONDS", "900"))
    download_host_wait_seconds: float = float(os.getenv("VIDEO_REVIEW_DOWNLOAD_HOST_WAIT_SECONDS", "120"))
    download_host_poll_seconds: float = float(os.getenv("VIDEO_REVIEW_DOWNLOAD_HOST_POLL_SECONDS", "0.2"))
    download_task_retry_attempts: int = int(os.getenv("VIDEO_REVIEW_DOWNLOAD_TASK_RETRY_ATTEMPTS", "3"))
    download_task_retry_delays_seconds: str = os.getenv("VIDEO_REVIEW_DOWNLOAD_TASK_RETRY_DELAYS_SECONDS", "60,300,900")
    download_retry_promote_count: int = int(os.getenv("VIDEO_REVIEW_DOWNLOAD_RETRY_PROMOTE_COUNT", "100"))
    use_redis_queue: bool = os.getenv("VIDEO_REVIEW_USE_REDIS_QUEUE", "0").lower() in {"1", "true", "yes"}
    redis_cache_enabled: bool = os.getenv("VIDEO_REVIEW_REDIS_CACHE_ENABLED", "1").lower() in {"1", "true", "yes"}
    infra_failure_backoff_seconds: int = int(os.getenv("INFRA_FAILURE_BACKOFF_SECONDS", "30"))
    api_auth_enabled: bool = os.getenv("VIDEO_REVIEW_API_AUTH_ENABLED", "0").lower() in {"1", "true", "yes"}
    api_auth_secret: str | None = os.getenv("VIDEO_REVIEW_API_SECRET") or None
    api_auth_secrets: str | None = os.getenv("VIDEO_REVIEW_API_SECRETS") or None
    api_auth_clock_skew_seconds: int = int(os.getenv("VIDEO_REVIEW_API_AUTH_CLOCK_SKEW_SECONDS", "300"))
    api_idempotency_wait_seconds: float = float(os.getenv("VIDEO_REVIEW_API_IDEMPOTENCY_WAIT_SECONDS", "3"))
    api_callback_timeout_seconds: float = float(os.getenv("VIDEO_REVIEW_API_CALLBACK_TIMEOUT_SECONDS", "5"))
    api_callback_allowed_hosts: str | None = os.getenv("VIDEO_REVIEW_API_CALLBACK_ALLOWED_HOSTS") or None
    api_callback_allow_http: bool = os.getenv("VIDEO_REVIEW_API_CALLBACK_ALLOW_HTTP", "0").lower() in {
        "1",
        "true",
        "yes",
    }
    api_video_url_allowed_hosts: str | None = os.getenv("VIDEO_REVIEW_API_VIDEO_URL_ALLOWED_HOSTS") or None
    api_admin_feishu_user_ids: str | None = os.getenv("VIDEO_REVIEW_API_ADMIN_FEISHU_USER_IDS") or None
    api_admin_app_ids: str | None = os.getenv("VIDEO_REVIEW_API_ADMIN_APP_IDS") or None
    upload_max_files: int = int(os.getenv("VIDEO_REVIEW_UPLOAD_MAX_FILES", "50"))
    upload_chunk_bytes: int = int(os.getenv("VIDEO_REVIEW_UPLOAD_CHUNK_BYTES", str(512 * 1024)))
    cleanup_enabled: bool = os.getenv("VIDEO_REVIEW_CLEANUP_ENABLED", "1").lower() in {"1", "true", "yes"}
    cleanup_raw_ttl_hours: float = float(os.getenv("VIDEO_REVIEW_CLEANUP_RAW_TTL_HOURS", "24"))
    cleanup_derived_ttl_hours: float = float(os.getenv("VIDEO_REVIEW_CLEANUP_DERIVED_TTL_HOURS", "72"))
    cleanup_upload_session_ttl_hours: float = float(os.getenv("VIDEO_REVIEW_CLEANUP_UPLOAD_SESSION_TTL_HOURS", "24"))
    aliyun_access_key_id: str | None = os.getenv("ALIYUN_ACCESS_KEY_ID") or os.getenv("OSS_ACCESS_KEY_ID") or None
    aliyun_access_key_secret: str | None = os.getenv("ALIYUN_ACCESS_KEY_SECRET") or os.getenv("OSS_ACCESS_KEY_SECRET") or None
    aliyun_sts_endpoint: str = os.getenv("ALIYUN_STS_ENDPOINT", "sts.aliyuncs.com")
    aliyun_sts_role_arn: str | None = os.getenv("ALIYUN_STS_ROLE_ARN") or None
    aliyun_sts_session_name_prefix: str = os.getenv("ALIYUN_STS_SESSION_NAME_PREFIX", "sn2s-video-audit")
    oss_bucket: str = os.getenv("ALIYUN_OSS_BUCKET", "")
    oss_region: str = os.getenv("ALIYUN_OSS_REGION", "oss-cn-hangzhou")
    oss_endpoint: str = os.getenv("ALIYUN_OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
    oss_internal_endpoint: str | None = os.getenv("ALIYUN_OSS_INTERNAL_ENDPOINT") or None
    oss_public_host: str | None = os.getenv("ALIYUN_OSS_PUBLIC_HOST") or None
    oss_prefix: str = os.getenv("ALIYUN_OSS_PREFIX", "sn2s-video-audit/prod")
    oss_sts_duration_seconds: int = int(os.getenv("ALIYUN_OSS_STS_DURATION_SECONDS", "3600"))
    worker_concurrency: int = int(os.getenv("VIDEO_REVIEW_WORKER_CONCURRENCY", "2"))
    worker_poll_count: int = int(os.getenv("VIDEO_REVIEW_WORKER_POLL_COUNT", "5"))
    global_active_limit: int = int(os.getenv("VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT", "0"))
    global_active_ttl_seconds: int = int(os.getenv("VIDEO_REVIEW_GLOBAL_ACTIVE_TTL_SECONDS", "1800"))
    global_active_wait_seconds: float = float(os.getenv("VIDEO_REVIEW_GLOBAL_ACTIVE_WAIT_SECONDS", "3600"))
    global_active_poll_seconds: float = float(os.getenv("VIDEO_REVIEW_GLOBAL_ACTIVE_POLL_SECONDS", "2"))
    stale_processing_minutes: int = int(os.getenv("VIDEO_REVIEW_STALE_PROCESSING_MINUTES", "60"))
    stale_processing_reconcile_interval_seconds: int = int(
        os.getenv("VIDEO_REVIEW_STALE_PROCESSING_RECONCILE_INTERVAL_SECONDS", "60")
    )
    stale_processing_reconcile_on_worker_start: bool = os.getenv(
        "VIDEO_REVIEW_STALE_PROCESSING_RECONCILE_ON_WORKER_START",
        "1",
    ).lower() in {"1", "true", "yes"}
    google_api_key: str | None = os.getenv("GOOGLE_API_KEY")
    google_api_keys: str | None = os.getenv("GOOGLE_API_KEYS") or None
    google_api_base_url: str | None = os.getenv("GOOGLE_API_BASE_URL") or None
    gemini_safety_threshold: str = os.getenv("GEMINI_SAFETY_THRESHOLD", "BLOCK_NONE")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    anthropic_api_base_url: str = os.getenv("ANTHROPIC_API_BASE_URL", "https://aihubmix.com")
    video_review_model: str = os.getenv("VIDEO_REVIEW_MODEL", "gemini-2.5-flash")
    policy_judge_model: str = os.getenv("POLICY_JUDGE_MODEL", "Codex-sonnet-4-5")
    max_concurrent: int = int(os.getenv("VIDEO_REVIEW_MAX_CONCURRENT", "5"))
    segment_seconds: int = int(os.getenv("VIDEO_REVIEW_SEGMENT_SECONDS", "180"))
    frame_fps: int = int(os.getenv("VIDEO_REVIEW_FRAME_FPS", "1"))
    frame_batch_size: int = int(os.getenv("VIDEO_REVIEW_FRAME_BATCH_SIZE", "16"))
    frame_batch_min_size: int = int(os.getenv("VIDEO_REVIEW_FRAME_BATCH_MIN_SIZE", "4"))
    frame_batch_max_split_depth: int = int(os.getenv("VIDEO_REVIEW_FRAME_BATCH_MAX_SPLIT_DEPTH", "1"))
    frame_batch_concurrency: int = int(os.getenv("VIDEO_REVIEW_FRAME_BATCH_CONCURRENCY", "2"))
    frame_dedup_enabled: bool = os.getenv("VIDEO_REVIEW_FRAME_DEDUP_ENABLED", "1").lower() in {"1", "true", "yes"}
    frame_hash_distance_threshold: int = int(os.getenv("VIDEO_REVIEW_FRAME_HASH_DISTANCE_THRESHOLD", "6"))
    frame_dedup_max_gap_seconds: float = float(os.getenv("VIDEO_REVIEW_FRAME_DEDUP_MAX_GAP_SECONDS", "4"))
    frame_sheet_enabled: bool = os.getenv("VIDEO_REVIEW_FRAME_SHEET_ENABLED", "1").lower() in {"1", "true", "yes"}
    frame_sheet_rows: int = int(os.getenv("VIDEO_REVIEW_FRAME_SHEET_ROWS", "4"))
    frame_sheet_cols: int = int(os.getenv("VIDEO_REVIEW_FRAME_SHEET_COLS", "4"))
    ffmpeg_threads: int = int(os.getenv("VIDEO_REVIEW_FFMPEG_THREADS", "1"))
    ffmpeg_filter_threads: int = int(os.getenv("VIDEO_REVIEW_FFMPEG_FILTER_THREADS", "1"))
    model_call_timeout_seconds: int = int(os.getenv("VIDEO_REVIEW_MODEL_CALL_TIMEOUT_SECONDS", "180"))
    model_call_retry_delay_seconds: float = float(os.getenv("VIDEO_REVIEW_MODEL_CALL_RETRY_DELAY_SECONDS", "2"))
    model_parse_retry_attempts: int = int(os.getenv("VIDEO_REVIEW_MODEL_PARSE_RETRY_ATTEMPTS", "3"))
    model_transient_retry_attempts: int = int(os.getenv("VIDEO_REVIEW_MODEL_TRANSIENT_RETRY_ATTEMPTS", "2"))
    model_rate_limit_retry_attempts: int = int(os.getenv("VIDEO_REVIEW_MODEL_RATE_LIMIT_RETRY_ATTEMPTS", "3"))
    model_rate_limit_backoff_seconds: float = float(
        os.getenv("VIDEO_REVIEW_MODEL_RATE_LIMIT_BACKOFF_SECONDS", "2")
    )
    model_retry_jitter_seconds: float = float(os.getenv("VIDEO_REVIEW_MODEL_RETRY_JITTER_SECONDS", "0.5"))
    model_retry_budget_extra_attempts: int = int(os.getenv("VIDEO_REVIEW_MODEL_RETRY_BUDGET_EXTRA_ATTEMPTS", "20"))
    model_circuit_enabled: bool = os.getenv("VIDEO_REVIEW_MODEL_CIRCUIT_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
    }
    model_concurrency_limit: int = int(os.getenv("VIDEO_REVIEW_MODEL_CONCURRENCY_LIMIT", "0"))
    model_concurrency_wait_seconds: float = float(os.getenv("VIDEO_REVIEW_MODEL_CONCURRENCY_WAIT_SECONDS", "120"))
    model_concurrency_ttl_seconds: int = int(os.getenv("VIDEO_REVIEW_MODEL_CONCURRENCY_TTL_SECONDS", "300"))
    model_key_concurrency_limit: int = int(os.getenv("VIDEO_REVIEW_MODEL_KEY_CONCURRENCY_LIMIT", "0"))
    model_key_cooldown_seconds: int = int(os.getenv("VIDEO_REVIEW_MODEL_KEY_COOLDOWN_SECONDS", "60"))
    model_key_failure_threshold: int = int(os.getenv("VIDEO_REVIEW_MODEL_KEY_FAILURE_THRESHOLD", "3"))
    model_local_concurrency_limit: int = int(os.getenv("VIDEO_REVIEW_MODEL_LOCAL_CONCURRENCY_LIMIT", "8"))
    model_health_window_seconds: int = int(os.getenv("VIDEO_REVIEW_MODEL_HEALTH_WINDOW_SECONDS", "60"))
    model_circuit_min_requests: int = int(os.getenv("VIDEO_REVIEW_MODEL_CIRCUIT_MIN_REQUESTS", "50"))
    model_circuit_degraded_error_rate: float = float(os.getenv("VIDEO_REVIEW_MODEL_DEGRADED_ERROR_RATE", "0.03"))
    model_circuit_open_error_rate: float = float(os.getenv("VIDEO_REVIEW_MODEL_OPEN_ERROR_RATE", "0.08"))
    model_circuit_open_seconds: int = int(os.getenv("VIDEO_REVIEW_MODEL_OPEN_SECONDS", "45"))
    model_circuit_degraded_multiplier: float = float(os.getenv("VIDEO_REVIEW_MODEL_DEGRADED_MULTIPLIER", "0.7"))
    subtitle_review_enabled: bool = os.getenv("VIDEO_REVIEW_SUBTITLE_REVIEW_ENABLED", "1").lower() in {"1", "true", "yes"}
    subtitle_ocr_enabled: bool = os.getenv("VIDEO_REVIEW_SUBTITLE_OCR_ENABLED", "1").lower() in {"1", "true", "yes"}
    subtitle_ocr_lang: str = os.getenv("VIDEO_REVIEW_SUBTITLE_OCR_LANG", "chi_sim+eng")
    subtitle_ocr_crop_ratio: float = float(os.getenv("VIDEO_REVIEW_SUBTITLE_OCR_CROP_RATIO", "0.42"))
    subtitle_ocr_min_interval_seconds: float = float(os.getenv("VIDEO_REVIEW_SUBTITLE_OCR_MIN_INTERVAL_SECONDS", "2"))
    subtitle_ocr_max_frames: int = int(os.getenv("VIDEO_REVIEW_SUBTITLE_OCR_MAX_FRAMES", "90"))
    tesseract_thread_limit: int = int(os.getenv("VIDEO_REVIEW_TESSERACT_THREAD_LIMIT", "1"))

    @property
    def google_api_key_pool(self) -> list[str]:
        candidates = [self.google_api_key or ""]
        candidates.extend((self.google_api_keys or "").split(","))
        return list(dict.fromkeys(key.strip() for key in candidates if key and key.strip()))
    synthesize_narrative: bool = os.getenv("VIDEO_REVIEW_SYNTHESIZE_NARRATIVE", "1").lower() in {"1", "true", "yes"}
    input_mode: str = os.getenv("VIDEO_REVIEW_INPUT_MODE", "frames")
    mode: str = os.getenv("VIDEO_REVIEW_MODE", "model")

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def upload_sessions_dir(self) -> Path:
        return self.raw_dir / "upload_sessions"

    def ensure_dirs(self) -> None:
        for path in [
            self.raw_dir,
            self.derived_dir,
            self.reports_dir,
            self.jobs_dir,
            self.events_dir,
            self.datasets_dir,
            self.upload_sessions_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
