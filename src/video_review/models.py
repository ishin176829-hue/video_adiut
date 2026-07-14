from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Severity = Literal["low", "medium", "high", "critical"]
Decision = Literal["pass", "warn", "reject", "manual_review"]


class ReviewStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SOURCE_UNAVAILABLE = "source_unavailable"


class StoryContext(BaseModel):
    background: str = ""
    genres: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)


class ValueCorrectionAdvice(BaseModel):
    opening: str = ""
    main: str = ""
    ending: str = ""
    overall: str = ""


class ReviewFinding(BaseModel):
    category: str
    sub_category: str = ""
    risk_level: str = ""
    rule_tag: str = ""
    severity: Severity
    start_time: str
    end_time: str
    evidence: str
    reason: str
    suggested_action: str
    original_text: str = ""
    context_note: str = ""
    plot_impact: str = ""
    value_correction_advice: ValueCorrectionAdvice = Field(default_factory=ValueCorrectionAdvice)
    confidence: float = Field(default=0.5, ge=0, le=1)


class SegmentReviewResult(BaseModel):
    segment_index: int
    start_time: str
    end_time: str
    summary: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)
    risk_score: float = Field(default=0, ge=0, le=100)


class StoryPhaseAssessment(BaseModel):
    phase: str
    phase_name: str = ""
    time_range: str = ""
    plot_summary: str = ""
    value_judgement: str = ""
    risk_points: list[str] = Field(default_factory=list)
    correction_advice: str = ""


class FinalVerdict(BaseModel):
    passed: bool = True
    conclusion: str = ""
    reason: str = ""
    high_risk_categories: list[str] = Field(default_factory=list)
    medium_risk_categories: list[str] = Field(default_factory=list)


class ReportNarrative(BaseModel):
    main_plot: str = ""
    story_context: StoryContext = Field(default_factory=StoryContext)
    plot_structure: list[StoryPhaseAssessment] = Field(default_factory=list)
    value_correction_advice: ValueCorrectionAdvice = Field(default_factory=ValueCorrectionAdvice)
    final_verdict: FinalVerdict = Field(default_factory=FinalVerdict)
    overall_summary: str = ""


class VideoReviewReport(BaseModel):
    review_id: str
    video_id: str
    policy_version: str
    decision: Decision
    risk_score: float = Field(ge=0, le=100)
    summary: str
    main_plot: str = ""
    story_context: StoryContext = Field(default_factory=StoryContext)
    plot_structure: list[StoryPhaseAssessment] = Field(default_factory=list)
    value_correction_advice: ValueCorrectionAdvice = Field(default_factory=ValueCorrectionAdvice)
    final_verdict: FinalVerdict = Field(default_factory=FinalVerdict)
    findings: list[ReviewFinding] = Field(default_factory=list)
    segments: list[SegmentReviewResult] = Field(default_factory=list)
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class ReviewJob(BaseModel):
    review_id: str
    app_id: str | None = None
    platform_task_id: str | None = None
    feishu_user_id: str | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_name: str | None = None
    feishu_tenant_key: str | None = None
    uploader_info: str = ""
    drama_title: str = ""
    status: ReviewStatus
    phase: str
    message: str
    video_id: str | None = None
    source_url: str | None = None
    local_path: str | None = None
    oss_bucket: str | None = None
    oss_key: str | None = None
    progress: dict = Field(default_factory=dict)
    error: str | None = None
    report_path: str | None = None
    upload_started_at: str | None = None
    upload_completed_at: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class CreateReviewRequest(BaseModel):
    app_id: str | None = None
    platform_task_id: str | None = None
    feishu_user_id: str | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_name: str | None = None
    feishu_tenant_key: str | None = None
    uploader_info: str = ""
    drama_title: str = ""
    video_url: str | None = None
    local_path: str | None = None
    oss_bucket: str | None = None
    oss_key: str | None = None
    oss_endpoint: str | None = None
    oss_etag: str | None = None
    oss_size: int | None = Field(default=None, ge=0)
    session_id: str | None = None
    video_title: str | None = None
    policy_version: str | None = None
    model: str | None = None
    fps: int = Field(default=1, ge=1, le=10)
    segment_seconds: int | None = Field(default=None, ge=30, le=600)
    start_seconds: int | None = Field(default=None, ge=0)
    end_seconds: int | None = Field(default=None, ge=1)
    upload_started_at: str | None = None
    upload_completed_at: str | None = None
    callback_url: str | None = None
    callback_secret: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BulkCreateReviewRequest(BaseModel):
    video_urls: list[str] = Field(default_factory=list)
    video_titles: list[str] = Field(default_factory=list)
    video_title_prefix: str | None = None
    session_id: str | None = None
    policy_version: str | None = None
    model: str | None = None
    fps: int = Field(default=1, ge=1, le=10)
    segment_seconds: int | None = Field(default=None, ge=30, le=600)
    start_seconds: int | None = Field(default=None, ge=0)
    end_seconds: int | None = Field(default=None, ge=1)


class ChunkUploadInitRequest(BaseModel):
    filename: str
    size: int = Field(default=0, ge=0)


class ChunkUploadInitResponse(BaseModel):
    success: bool
    upload_id: str
    chunk_size: int


class ChunkUploadPartResponse(BaseModel):
    success: bool
    upload_id: str
    chunk_index: int


class ChunkUploadCompleteRequest(BaseModel):
    filename: str | None = None
    chunk_count: int = Field(ge=1)
    session_id: str | None = None
    video_title: str | None = None
    policy_version: str | None = None
    model: str | None = None
    fps: int = Field(default=1, ge=1, le=10)
    segment_seconds: int | None = Field(default=None, ge=30, le=600)
    start_seconds: int | None = Field(default=None, ge=0)
    end_seconds: int | None = Field(default=None, ge=1)


class OssUploadCredentials(BaseModel):
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str


class OssUploadInitRequest(BaseModel):
    filename: str
    size: int = Field(default=0, ge=0)
    content_type: str | None = None


class OssUploadInitResponse(BaseModel):
    success: bool
    upload_id: str
    video_id: str
    bucket: str
    region: str
    endpoint: str
    object_key: str
    upload_started_at: str
    credentials: OssUploadCredentials


class OssUploadCompleteRequest(BaseModel):
    upload_id: str
    app_id: str | None = None
    platform_task_id: str | None = None
    feishu_user_id: str | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_name: str | None = None
    feishu_tenant_key: str | None = None
    uploader_info: str = ""
    drama_title: str = ""
    filename: str | None = None
    etag: str | None = None
    size: int | None = Field(default=None, ge=0)
    session_id: str | None = None
    video_title: str | None = None
    policy_version: str | None = None
    model: str | None = None
    fps: int = Field(default=1, ge=1, le=10)
    segment_seconds: int | None = Field(default=None, ge=30, le=600)
    start_seconds: int | None = Field(default=None, ge=0)
    end_seconds: int | None = Field(default=None, ge=1)
    callback_url: str | None = None
    callback_secret: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlatformCreateReviewRequest(BaseModel):
    platform_task_id: str = Field(min_length=1, max_length=128)
    feishu_user_id: str | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_name: str | None = None
    feishu_tenant_key: str | None = None
    uploader_info: str = ""
    drama_title: str = ""
    video_url: str | None = None
    oss_bucket: str | None = None
    oss_key: str | None = None
    oss_endpoint: str | None = None
    oss_etag: str | None = None
    oss_size: int | None = Field(default=None, ge=0)
    video_title: str | None = None
    policy_version: str | None = None
    model: str | None = None
    fps: int = Field(default=1, ge=1, le=10)
    segment_seconds: int | None = Field(default=None, ge=30, le=600)
    start_seconds: int | None = Field(default=None, ge=0)
    end_seconds: int | None = Field(default=None, ge=1)
    callback_url: str | None = None
    callback_secret: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlatformBatchCreateReviewRequest(BaseModel):
    items: list[PlatformCreateReviewRequest] = Field(min_length=1, max_length=50)


class PlatformCreateReviewResponse(BaseModel):
    success: bool
    review_id: str
    platform_task_id: str
    status: ReviewStatus
    idempotent: bool = False
    status_url: str
    result_url: str
    cancel_url: str


class PlatformBatchCreateReviewItem(BaseModel):
    success: bool
    platform_task_id: str | None = None
    review_id: str | None = None
    status: ReviewStatus | None = None
    idempotent: bool = False
    status_url: str | None = None
    result_url: str | None = None
    cancel_url: str | None = None
    error_code: str | None = None
    message: str | None = None


class PlatformBatchCreateReviewResponse(BaseModel):
    success: bool
    accepted_count: int
    failed_count: int
    items: list[PlatformBatchCreateReviewItem]


class PlatformReviewStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    review_id: str
    platform_task_id: str | None = None
    uploader_info: str = Field(default="", alias="上传人信息")
    drama_title: str = Field(default="", alias="剧名")
    feishu_user_id: str | None = None
    feishu_user_name: str | None = None
    status: ReviewStatus
    phase: str
    message: str
    progress: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str


class PlatformReviewResultResponse(BaseModel):
    success: bool
    review_id: str
    platform_task_id: str | None = None
    status: ReviewStatus
    report: VideoReviewReport


class PlatformReviewHistoryItem(BaseModel):
    review_id: str
    platform_task_id: str | None = None
    uploader_info: str = ""
    drama_title: str = ""
    feishu_user_id: str | None = None
    feishu_user_name: str | None = None
    status: str
    phase: str
    message: str = ""
    video_title: str = ""
    decision: str | None = None
    risk_score: float | None = None
    duration_seconds: float | None = None
    duration_minutes: float | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    upload_seconds: float | None = None
    queue_seconds: float | None = None
    review_seconds: float | None = None
    total_seconds: float | None = None
    status_url: str
    result_url: str


class PlatformReviewHistoryResponse(BaseModel):
    success: bool = True
    total: int = 0
    limit: int = 50
    offset: int = 0
    items: list[PlatformReviewHistoryItem] = Field(default_factory=list)


class AdminDatabaseTableListResponse(BaseModel):
    success: bool = True
    tables: list[str] = Field(default_factory=list)


class AdminDatabaseRowsResponse(BaseModel):
    success: bool = True
    table: str
    total: int = 0
    limit: int = 100
    offset: int = 0
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class AdminReviewItem(BaseModel):
    review_id: str
    status: str
    phase: str
    message: str = ""
    video_title: str = ""
    source_url: str | None = None
    local_path: str | None = None
    decision: str | None = None
    risk_score: float | None = None
    duration_seconds: float | None = None
    duration_minutes: float | None = None
    content_length: int | None = None
    upload_started_at: str | None = None
    upload_completed_at: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    upload_seconds: float | None = None
    queue_seconds: float | None = None
    review_seconds: float | None = None
    total_seconds: float | None = None


class AdminStatsResponse(BaseModel):
    total: int = 0
    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    total_video_minutes: float = 0
    avg_total_seconds: float | None = None
    avg_review_seconds: float | None = None
    reviews: list[AdminReviewItem] = Field(default_factory=list)


class SystemMetricPoint(BaseModel):
    timestamp: str
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    memory_used_bytes: int = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None


class SystemMetricsResponse(BaseModel):
    hostname: str
    cpu_count: int
    interval_seconds: int
    window_seconds: int
    latest: SystemMetricPoint | None = None
    points: list[SystemMetricPoint] = Field(default_factory=list)


class CreateReviewResponse(BaseModel):
    success: bool
    review_id: str
    status_url: str
    stream_url: str
    report_url: str


class BulkCreateReviewResponse(BaseModel):
    success: bool
    count: int
    reviews: list[CreateReviewResponse]


class PolicyCategory(BaseModel):
    id: str
    name: str
    severity: Severity
    risk_levels: dict[str, str] = Field(default_factory=dict)
    keywords: list[str]
    rule: str
    default_action: str


class PolicyRules(BaseModel):
    version: str
    categories: list[PolicyCategory]


class VideoAsset(BaseModel):
    video_id: str
    source_url: str | None = None
    local_path: str
    sha256: str
    content_length: int | None = None
    etag: str | None = None
    oss_bucket: str | None = None
    oss_key: str | None = None
    oss_endpoint: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    bit_rate: int | None = None


class SegmentPlan(BaseModel):
    segment_index: int
    start_seconds: int
    end_seconds: int
    start_time: str
    end_time: str
