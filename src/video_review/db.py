from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import asyncpg

from .config import settings
from .models import (
    AdminDatabaseRowsResponse,
    AdminReviewItem,
    AdminStatsResponse,
    CreateReviewRequest,
    PlatformReviewHistoryItem,
    PlatformReviewHistoryResponse,
    ReviewJob,
    SegmentReviewResult,
    VideoAsset,
    VideoReviewReport,
)


_STALE_PROCESSING_RECONCILE_LOCK_ID = 784633901001


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS video_assets (
    video_id TEXT PRIMARY KEY,
    source_url TEXT,
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content_length BIGINT,
    etag TEXT,
    oss_bucket TEXT,
    oss_key TEXT,
    oss_endpoint TEXT,
    duration_seconds NUMERIC,
    width INTEGER,
    height INTEGER,
    bit_rate BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_video_assets_sha256 ON video_assets (sha256);
CREATE INDEX IF NOT EXISTS idx_video_assets_source_url ON video_assets (source_url);
ALTER TABLE video_assets
    ADD COLUMN IF NOT EXISTS oss_bucket TEXT,
    ADD COLUMN IF NOT EXISTS oss_key TEXT,
    ADD COLUMN IF NOT EXISTS oss_endpoint TEXT;
CREATE INDEX IF NOT EXISTS idx_video_assets_oss_key ON video_assets (oss_bucket, oss_key);

CREATE TABLE IF NOT EXISTS review_jobs (
    review_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT NOT NULL,
    video_id TEXT REFERENCES video_assets(video_id) ON DELETE SET NULL,
    source_url TEXT,
    local_path TEXT,
    session_id TEXT,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    report_path TEXT,
    request JSONB,
    upload_started_at TIMESTAMPTZ,
    upload_completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_review_jobs_status_updated ON review_jobs (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_jobs_video_id ON review_jobs (video_id);
CREATE INDEX IF NOT EXISTS idx_review_jobs_session_id ON review_jobs (session_id);
CREATE INDEX IF NOT EXISTS idx_review_jobs_request_feishu_user ON review_jobs ((request->>'feishu_user_id'));
CREATE INDEX IF NOT EXISTS idx_review_jobs_request_platform_task ON review_jobs ((request->>'platform_task_id'));
ALTER TABLE review_jobs
    ADD COLUMN IF NOT EXISTS upload_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS upload_completed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_review_jobs_upload_started ON review_jobs (upload_started_at DESC);

CREATE TABLE IF NOT EXISTS review_events (
    id BIGSERIAL PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_events_review_id_id ON review_events (review_id, id);

CREATE TABLE IF NOT EXISTS review_segments (
    review_id TEXT NOT NULL REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_seconds INTEGER,
    end_seconds INTEGER,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    risk_score NUMERIC NOT NULL DEFAULT 0,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (review_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_review_segments_review_time ON review_segments (review_id, start_time, end_time);

CREATE TABLE IF NOT EXISTS review_findings (
    id BIGSERIAL PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    segment_index INTEGER,
    category TEXT NOT NULL,
    sub_category TEXT,
    risk_level TEXT,
    rule_tag TEXT,
    severity TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    evidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    original_text TEXT,
    context_note TEXT,
    plot_impact TEXT,
    value_correction_advice JSONB,
    confidence NUMERIC NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS sub_category TEXT,
    ADD COLUMN IF NOT EXISTS risk_level TEXT,
    ADD COLUMN IF NOT EXISTS rule_tag TEXT,
    ADD COLUMN IF NOT EXISTS original_text TEXT,
    ADD COLUMN IF NOT EXISTS context_note TEXT,
    ADD COLUMN IF NOT EXISTS plot_impact TEXT,
    ADD COLUMN IF NOT EXISTS value_correction_advice JSONB;

CREATE INDEX IF NOT EXISTS idx_review_findings_review_id ON review_findings (review_id);
CREATE INDEX IF NOT EXISTS idx_review_findings_category ON review_findings (category);
CREATE INDEX IF NOT EXISTS idx_review_findings_sub_category ON review_findings (sub_category);
CREATE INDEX IF NOT EXISTS idx_review_findings_risk_level ON review_findings (risk_level);
CREATE INDEX IF NOT EXISTS idx_review_findings_severity ON review_findings (severity);
CREATE INDEX IF NOT EXISTS idx_review_findings_time ON review_findings (review_id, start_time, end_time);

CREATE TABLE IF NOT EXISTS review_reports (
    review_id TEXT PRIMARY KEY REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    video_id TEXT,
    policy_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    risk_score NUMERIC NOT NULL,
    summary TEXT NOT NULL,
    report JSONB NOT NULL,
    report_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS frame_batch_cache_index (
    cache_key TEXT PRIMARY KEY,
    video_id TEXT,
    video_sha256 TEXT,
    policy_version TEXT,
    model TEXT,
    fps INTEGER,
    start_time TEXT,
    end_time TEXT,
    frame_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_frame_batch_cache_video ON frame_batch_cache_index (video_sha256, policy_version, model);
"""


_pool: asyncpg.Pool | None = None
_disabled_until = 0.0
_last_error: str | None = None


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


SENSITIVE_KEY_TOKENS = ("secret", "token", "password", "api_key", "access_key", "security_token")


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in SENSITIVE_KEY_TOKENS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _json_safe_row(row: asyncpg.Record) -> dict[str, Any]:
    return _redact_sensitive(json.loads(_json(dict(row))))


def _admin_table_order_column(table: str) -> str:
    if table == "review_events":
        return "id"
    if table == "frame_batch_cache_index":
        return "created_at"
    if table in {"review_segments", "review_findings", "review_reports", "review_jobs", "video_assets"}:
        return "created_at"
    return "1"


def db_last_error() -> str | None:
    return _last_error


async def get_pool(*, strict: bool = False) -> asyncpg.Pool | None:
    global _pool, _disabled_until, _last_error
    if not settings.database_url:
        return None
    if _pool is not None:
        return _pool
    if not strict and time.monotonic() < _disabled_until:
        return None
    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=max(1, settings.database_pool_min_size),
            max_size=max(settings.database_pool_min_size, settings.database_pool_max_size),
            timeout=settings.database_connect_timeout,
            command_timeout=settings.database_connect_timeout,
        )
        _last_error = None
        return _pool
    except Exception as exc:
        _last_error = str(exc) or exc.__class__.__name__
        _disabled_until = time.monotonic() + settings.infra_failure_backoff_seconds
        if strict:
            raise
        return None


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_schema(*, strict: bool = False) -> bool:
    pool = await get_pool(strict=strict)
    if pool is None:
        return False
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    return True


async def persist_asset(asset: VideoAsset) -> None:
    pool = await get_pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO video_assets (
                video_id, source_url, local_path, sha256, content_length, etag,
                oss_bucket, oss_key, oss_endpoint, duration_seconds, width, height, bit_rate
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (video_id) DO UPDATE SET
                source_url = EXCLUDED.source_url,
                local_path = EXCLUDED.local_path,
                sha256 = EXCLUDED.sha256,
                content_length = EXCLUDED.content_length,
                etag = EXCLUDED.etag,
                oss_bucket = EXCLUDED.oss_bucket,
                oss_key = EXCLUDED.oss_key,
                oss_endpoint = EXCLUDED.oss_endpoint,
                duration_seconds = EXCLUDED.duration_seconds,
                width = EXCLUDED.width,
                height = EXCLUDED.height,
                bit_rate = EXCLUDED.bit_rate,
                updated_at = now()
            """,
            asset.video_id,
            asset.source_url,
            asset.local_path,
            asset.sha256,
            asset.content_length,
            asset.etag,
            asset.oss_bucket,
            asset.oss_key,
            asset.oss_endpoint,
            asset.duration_seconds,
            asset.width,
            asset.height,
            asset.bit_rate,
        )


async def persist_job(job: ReviewJob, request: CreateReviewRequest | None = None) -> None:
    pool = await get_pool()
    if pool is None:
        return
    request_json = _json(request.model_dump(mode="json", exclude_none=True)) if request else None
    session_id = request.session_id if request else None
    upload_started_at = job.upload_started_at or (request.upload_started_at if request else None)
    upload_completed_at = job.upload_completed_at or (request.upload_completed_at if request else None)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO review_jobs (
                review_id, status, phase, message, video_id, source_url, local_path,
                session_id, progress, error, report_path, request,
                upload_started_at, upload_completed_at, started_at, completed_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11,
                $12::jsonb, $13::text::timestamptz, $14::text::timestamptz,
                CASE WHEN $2 = 'processing' THEN now() ELSE NULL END,
                CASE WHEN $2 IN ('completed', 'failed', 'cancelled', 'source_unavailable') THEN now() ELSE NULL END
            )
            ON CONFLICT (review_id) DO UPDATE SET
                status = EXCLUDED.status,
                phase = EXCLUDED.phase,
                message = EXCLUDED.message,
                video_id = EXCLUDED.video_id,
                source_url = EXCLUDED.source_url,
                local_path = EXCLUDED.local_path,
                session_id = COALESCE(EXCLUDED.session_id, review_jobs.session_id),
                progress = EXCLUDED.progress,
                error = EXCLUDED.error,
                report_path = EXCLUDED.report_path,
                request = COALESCE(EXCLUDED.request, review_jobs.request),
                upload_started_at = COALESCE(review_jobs.upload_started_at, EXCLUDED.upload_started_at),
                upload_completed_at = COALESCE(EXCLUDED.upload_completed_at, review_jobs.upload_completed_at),
                started_at = COALESCE(review_jobs.started_at, EXCLUDED.started_at),
                completed_at = CASE
                    WHEN EXCLUDED.status IN ('completed', 'failed', 'cancelled', 'source_unavailable') THEN COALESCE(review_jobs.completed_at, now())
                    ELSE review_jobs.completed_at
                END,
                updated_at = now()
            """,
            job.review_id,
            job.status.value,
            job.phase,
            job.message,
            job.video_id,
            job.source_url,
            job.local_path,
            session_id,
            _json(job.progress),
            job.error,
            job.report_path,
            request_json,
            upload_started_at,
            upload_completed_at,
        )


async def fetch_review_job_states(review_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not review_ids:
        return {}
    pool = await get_pool()
    if pool is None:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT review_id, status, phase, error
            FROM review_jobs
            WHERE review_id = ANY($1::text[])
            """,
            review_ids,
        )
    return {
        row["review_id"]: {
            "status": row["status"],
            "phase": row["phase"],
            "error": row["error"],
        }
        for row in rows
    }


async def fetch_admin_stats(
    limit: int = 100,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> AdminStatsResponse:
    pool = await get_pool()
    if pool is None:
        return AdminStatsResponse()
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                count(*)::int AS total,
                count(*) FILTER (WHERE j.status = 'pending')::int AS pending,
                count(*) FILTER (WHERE j.status = 'processing')::int AS processing,
                count(*) FILTER (WHERE j.status = 'completed')::int AS completed,
                count(*) FILTER (WHERE j.status = 'failed')::int AS failed,
                count(*) FILTER (WHERE j.status = 'cancelled')::int AS cancelled,
                COALESCE(sum(va.duration_seconds) / 60.0, 0) AS total_video_minutes,
                avg(EXTRACT(EPOCH FROM (j.completed_at - COALESCE(j.upload_started_at, j.created_at))))
                    FILTER (WHERE j.completed_at IS NOT NULL) AS avg_total_seconds,
                avg(EXTRACT(EPOCH FROM (j.completed_at - COALESCE(j.started_at, j.created_at))))
                    FILTER (WHERE j.completed_at IS NOT NULL) AS avg_review_seconds
            FROM review_jobs j
            LEFT JOIN video_assets va ON va.video_id = j.video_id
            WHERE ($1::text IS NULL OR j.status = $1)
              AND ($2::timestamptz IS NULL OR j.created_at >= $2)
              AND ($3::timestamptz IS NULL OR j.created_at <= $3)
            """,
            status,
            created_from,
            created_to,
        )
        rows = await conn.fetch(
            """
            SELECT
                j.review_id,
                j.status,
                j.phase,
                j.message,
                COALESCE(NULLIF(j.request->>'video_title', ''), NULLIF(j.source_url, ''), NULLIF(j.local_path, ''), j.review_id) AS video_title,
                j.source_url,
                j.local_path,
                rr.decision,
                rr.risk_score,
                va.duration_seconds,
                CASE WHEN va.duration_seconds IS NULL THEN NULL ELSE va.duration_seconds / 60.0 END AS duration_minutes,
                va.content_length,
                COALESCE(j.upload_started_at, j.created_at) AS upload_started_at,
                j.upload_completed_at,
                j.created_at,
                j.started_at,
                j.completed_at,
                j.updated_at,
                CASE
                    WHEN j.upload_started_at IS NULL THEN 0
                    ELSE EXTRACT(EPOCH FROM (COALESCE(j.upload_completed_at, j.created_at) - j.upload_started_at))
                END AS upload_seconds,
                CASE
                    WHEN j.started_at IS NULL THEN NULL
                    ELSE EXTRACT(EPOCH FROM (j.started_at - j.created_at))
                END AS queue_seconds,
                CASE
                    WHEN j.completed_at IS NULL THEN NULL
                    ELSE EXTRACT(EPOCH FROM (j.completed_at - COALESCE(j.started_at, j.created_at)))
                END AS review_seconds,
                EXTRACT(EPOCH FROM (
                    COALESCE(
                        j.completed_at,
                        CASE WHEN j.status IN ('completed', 'failed', 'cancelled') THEN j.updated_at ELSE now() END
                    ) - COALESCE(j.upload_started_at, j.created_at)
                )) AS total_seconds
            FROM review_jobs j
            LEFT JOIN video_assets va ON va.video_id = j.video_id
            LEFT JOIN review_reports rr ON rr.review_id = j.review_id
            WHERE ($2::text IS NULL OR j.status = $2)
              AND ($3::timestamptz IS NULL OR j.created_at >= $3)
              AND ($4::timestamptz IS NULL OR j.created_at <= $4)
            ORDER BY j.created_at DESC
            LIMIT $1
            """,
            limit,
            status,
            created_from,
            created_to,
        )
    reviews = [
        AdminReviewItem(
            review_id=row["review_id"],
            status=row["status"],
            phase=row["phase"],
            message=row["message"] or "",
            video_title=row["video_title"] or "",
            source_url=row["source_url"],
            local_path=row["local_path"],
            decision=row["decision"],
            risk_score=_float(row["risk_score"]),
            duration_seconds=_float(row["duration_seconds"]),
            duration_minutes=_float(row["duration_minutes"]),
            content_length=_int(row["content_length"]),
            upload_started_at=_iso(row["upload_started_at"]),
            upload_completed_at=_iso(row["upload_completed_at"]),
            created_at=_iso(row["created_at"]),
            started_at=_iso(row["started_at"]),
            completed_at=_iso(row["completed_at"]),
            updated_at=_iso(row["updated_at"]),
            upload_seconds=_float(row["upload_seconds"]),
            queue_seconds=_float(row["queue_seconds"]),
            review_seconds=_float(row["review_seconds"]),
            total_seconds=_float(row["total_seconds"]),
        )
        for row in rows
    ]
    return AdminStatsResponse(
        total=int(stats["total"] or 0),
        pending=int(stats["pending"] or 0),
        processing=int(stats["processing"] or 0),
        completed=int(stats["completed"] or 0),
        failed=int(stats["failed"] or 0),
        cancelled=int(stats["cancelled"] or 0),
        total_video_minutes=float(stats["total_video_minutes"] or 0),
        avg_total_seconds=_float(stats["avg_total_seconds"]),
        avg_review_seconds=_float(stats["avg_review_seconds"]),
        reviews=reviews,
    )


async def fetch_platform_review_history(
    *,
    feishu_user_id: str | None,
    include_all: bool = False,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> PlatformReviewHistoryResponse:
    pool = await get_pool()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    if pool is None:
        return PlatformReviewHistoryResponse(limit=limit, offset=offset)
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT count(*)::int
            FROM review_jobs j
            WHERE (($1::bool AND ($2::text = '' OR COALESCE(j.request->>'feishu_user_id', '') = $2))
                OR (NOT $1::bool AND COALESCE(j.request->>'feishu_user_id', '') = $2))
              AND ($3::text IS NULL OR j.status = $3)
              AND ($4::timestamptz IS NULL OR j.created_at >= $4)
              AND ($5::timestamptz IS NULL OR j.created_at <= $5)
            """,
            include_all,
            feishu_user_id or "",
            status,
            created_from,
            created_to,
        )
        rows = await conn.fetch(
            """
            SELECT
                j.review_id,
                j.status,
                j.phase,
                j.message,
                j.request->>'platform_task_id' AS platform_task_id,
                j.request->>'feishu_user_id' AS feishu_user_id,
                j.request->>'feishu_user_name' AS feishu_user_name,
                COALESCE(NULLIF(j.request->>'uploader_info', ''), NULLIF(j.request->>'feishu_user_name', ''), '') AS uploader_info,
                COALESCE(NULLIF(j.request->>'drama_title', ''), NULLIF(j.request->>'video_title', ''), '') AS drama_title,
                COALESCE(NULLIF(j.request->>'video_title', ''), NULLIF(j.source_url, ''), NULLIF(j.local_path, ''), j.review_id) AS video_title,
                rr.decision,
                rr.risk_score,
                va.duration_seconds,
                CASE WHEN va.duration_seconds IS NULL THEN NULL ELSE va.duration_seconds / 60.0 END AS duration_minutes,
                j.created_at,
                j.started_at,
                j.completed_at,
                j.updated_at,
                CASE
                    WHEN j.upload_started_at IS NULL THEN 0
                    ELSE EXTRACT(EPOCH FROM (COALESCE(j.upload_completed_at, j.created_at) - j.upload_started_at))
                END AS upload_seconds,
                CASE
                    WHEN j.started_at IS NULL THEN NULL
                    ELSE EXTRACT(EPOCH FROM (j.started_at - j.created_at))
                END AS queue_seconds,
                CASE
                    WHEN j.completed_at IS NULL THEN NULL
                    ELSE EXTRACT(EPOCH FROM (j.completed_at - COALESCE(j.started_at, j.created_at)))
                END AS review_seconds,
                EXTRACT(EPOCH FROM (
                    COALESCE(
                        j.completed_at,
                        CASE WHEN j.status IN ('completed', 'failed', 'cancelled') THEN j.updated_at ELSE now() END
                    ) - COALESCE(j.upload_started_at, j.created_at)
                )) AS total_seconds
            FROM review_jobs j
            LEFT JOIN video_assets va ON va.video_id = j.video_id
            LEFT JOIN review_reports rr ON rr.review_id = j.review_id
            WHERE (($3::bool AND ($4::text = '' OR COALESCE(j.request->>'feishu_user_id', '') = $4))
                OR (NOT $3::bool AND COALESCE(j.request->>'feishu_user_id', '') = $4))
              AND ($5::text IS NULL OR j.status = $5)
              AND ($6::timestamptz IS NULL OR j.created_at >= $6)
              AND ($7::timestamptz IS NULL OR j.created_at <= $7)
            ORDER BY j.created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
            include_all,
            feishu_user_id or "",
            status,
            created_from,
            created_to,
        )
    items = [
        PlatformReviewHistoryItem(
            review_id=row["review_id"],
            platform_task_id=row["platform_task_id"],
            uploader_info=row["uploader_info"] or "",
            drama_title=row["drama_title"] or "",
            feishu_user_id=row["feishu_user_id"],
            feishu_user_name=row["feishu_user_name"],
            status=row["status"],
            phase=row["phase"],
            message=row["message"] or "",
            video_title=row["video_title"] or "",
            decision=row["decision"],
            risk_score=_float(row["risk_score"]),
            duration_seconds=_float(row["duration_seconds"]),
            duration_minutes=_float(row["duration_minutes"]),
            created_at=_iso(row["created_at"]),
            started_at=_iso(row["started_at"]),
            completed_at=_iso(row["completed_at"]),
            updated_at=_iso(row["updated_at"]),
            upload_seconds=_float(row["upload_seconds"]),
            queue_seconds=_float(row["queue_seconds"]),
            review_seconds=_float(row["review_seconds"]),
            total_seconds=_float(row["total_seconds"]),
            status_url=f"/api/v1/reviews/{row['review_id']}",
            result_url=f"/api/v1/reviews/{row['review_id']}/result",
        )
        for row in rows
    ]
    return PlatformReviewHistoryResponse(
        success=True,
        total=int(total or 0),
        limit=limit,
        offset=offset,
        items=items,
    )


async def fetch_admin_database_rows(
    *,
    table: str,
    limit: int = 100,
    offset: int = 0,
) -> AdminDatabaseRowsResponse:
    allowed_tables = schema_table_names()
    if table not in allowed_tables:
        raise ValueError(f"table not allowed: {table}")
    pool = await get_pool()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    if pool is None:
        return AdminDatabaseRowsResponse(table=table, limit=limit, offset=offset)
    order_column = _admin_table_order_column(table)
    async with pool.acquire() as conn:
        total = await conn.fetchval(f'SELECT count(*)::int FROM "{table}"')
        rows = await conn.fetch(
            f'SELECT * FROM "{table}" ORDER BY "{order_column}" DESC LIMIT $1 OFFSET $2',
            limit,
            offset,
        )
        column_rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
    return AdminDatabaseRowsResponse(
        success=True,
        table=table,
        total=int(total or 0),
        limit=limit,
        offset=offset,
        columns=[row["column_name"] for row in column_rows],
        rows=[_json_safe_row(row) for row in rows],
    )


async def mark_stale_processing_jobs_failed(
    *,
    older_than_minutes: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    if pool is None:
        return []
    minutes = max(5, older_than_minutes or settings.stale_processing_minutes)
    row_limit = max(1, min(limit, 5000))
    message = f"processing 超过 {minutes} 分钟未更新，已由巡检标记失败"
    async with pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _STALE_PROCESSING_RECONCILE_LOCK_ID)
        if not locked:
            return []
        try:
            rows = await conn.fetch(
                """
                WITH stale AS (
                    SELECT review_id
                    FROM review_jobs
                    WHERE status = 'processing'
                      AND updated_at < now() - ($1::int || ' minutes')::interval
                    ORDER BY updated_at ASC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                    UPDATE review_jobs j
                    SET
                        status = 'failed',
                        phase = 'error',
                        message = $3,
                        error = 'STALE_PROCESSING_TIMEOUT',
                        completed_at = COALESCE(j.completed_at, now()),
                        updated_at = now()
                    FROM stale
                    WHERE j.review_id = stale.review_id
                    RETURNING j.review_id, j.request
                ),
                inserted AS (
                    INSERT INTO review_events (review_id, event_type, payload)
                    SELECT
                        review_id,
                        'error',
                        jsonb_build_object(
                            'error', 'STALE_PROCESSING_TIMEOUT',
                            'text', $3,
                            'older_than_minutes', $1
                        )
                    FROM updated
                    RETURNING 1
                )
                SELECT review_id, request FROM updated
                """,
                minutes,
                row_limit,
                message,
            )
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", _STALE_PROCESSING_RECONCILE_LOCK_ID)
    return [
        {
            "review_id": row["review_id"],
            "request": dict(row["request"] or {}),
        }
        for row in rows
    ]


async def persist_event(review_id: str, event_type: str, payload: dict[str, Any]) -> None:
    pool = await get_pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO review_events (review_id, event_type, payload)
            VALUES ($1, $2, $3::jsonb)
            """,
            review_id,
            event_type,
            _json(payload),
        )


async def persist_segment(review_id: str, segment: SegmentReviewResult) -> None:
    pool = await get_pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO review_segments (
                review_id, segment_index, start_time, end_time, summary, risk_score, result
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (review_id, segment_index) DO UPDATE SET
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                summary = EXCLUDED.summary,
                risk_score = EXCLUDED.risk_score,
                result = EXCLUDED.result,
                updated_at = now()
            """,
            review_id,
            segment.segment_index,
            segment.start_time,
            segment.end_time,
            segment.summary,
            segment.risk_score,
            segment.model_dump_json(),
        )


async def persist_report(report: VideoReviewReport, report_path: str | None = None) -> None:
    pool = await get_pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO review_reports (
                    review_id, video_id, policy_version, decision, risk_score, summary, report, report_path
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                ON CONFLICT (review_id) DO UPDATE SET
                    video_id = EXCLUDED.video_id,
                    policy_version = EXCLUDED.policy_version,
                    decision = EXCLUDED.decision,
                    risk_score = EXCLUDED.risk_score,
                    summary = EXCLUDED.summary,
                    report = EXCLUDED.report,
                    report_path = EXCLUDED.report_path,
                    updated_at = now()
                """,
                report.review_id,
                report.video_id,
                report.policy_version,
                report.decision,
                report.risk_score,
                report.summary,
                report.model_dump_json(),
                report_path,
            )
            await conn.execute("DELETE FROM review_findings WHERE review_id = $1", report.review_id)
            await conn.execute("DELETE FROM review_segments WHERE review_id = $1", report.review_id)
            for segment in report.segments:
                await conn.execute(
                    """
                    INSERT INTO review_segments (
                        review_id, segment_index, start_time, end_time, summary, risk_score, result
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    report.review_id,
                    segment.segment_index,
                    segment.start_time,
                    segment.end_time,
                    segment.summary,
                    segment.risk_score,
                    segment.model_dump_json(),
                )
                for finding in segment.findings:
                    await conn.execute(
                        """
                        INSERT INTO review_findings (
                            review_id, segment_index, category, sub_category, risk_level, rule_tag,
                            severity, start_time, end_time, evidence, reason, suggested_action,
                            original_text, context_note, plot_impact, value_correction_advice,
                            confidence, payload
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16::jsonb, $17, $18::jsonb
                        )
                        """,
                        report.review_id,
                        segment.segment_index,
                        finding.category,
                        finding.sub_category,
                        finding.risk_level,
                        finding.rule_tag,
                        finding.severity,
                        finding.start_time,
                        finding.end_time,
                        finding.evidence,
                        finding.reason,
                        finding.suggested_action,
                        finding.original_text,
                        finding.context_note,
                        finding.plot_impact,
                        _json(finding.value_correction_advice.model_dump(mode="json")),
                        finding.confidence,
                        finding.model_dump_json(),
                    )


async def persist_cache_index(
    *,
    cache_key: str,
    video_id: str,
    video_sha256: str,
    policy_version: str,
    model: str,
    fps: int,
    start_time: str,
    end_time: str,
    frame_count: int,
    ttl_seconds: int,
) -> None:
    pool = await get_pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO frame_batch_cache_index (
                cache_key, video_id, video_sha256, policy_version, model, fps,
                start_time, end_time, frame_count, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now() + ($10 || ' seconds')::interval)
            ON CONFLICT (cache_key) DO UPDATE SET
                video_id = EXCLUDED.video_id,
                video_sha256 = EXCLUDED.video_sha256,
                policy_version = EXCLUDED.policy_version,
                model = EXCLUDED.model,
                fps = EXCLUDED.fps,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                frame_count = EXCLUDED.frame_count,
                expires_at = EXCLUDED.expires_at
            """,
            cache_key,
            video_id,
            video_sha256,
            policy_version,
            model,
            fps,
            start_time,
            end_time,
            frame_count,
            ttl_seconds,
        )


def schema_table_names() -> set[str]:
    return {
        "video_assets",
        "review_jobs",
        "review_events",
        "review_segments",
        "review_findings",
        "review_reports",
        "frame_batch_cache_index",
    }
