from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .content_risk_compat import (
    ContentRiskBatchResultRequest,
    ContentRiskTaskRequest,
    empty_result,
    report_result,
    task_response,
    to_platform_review_request,
)
from .dataset import import_csv, import_xlsx
from .db import (
    close_pool,
    db_last_error,
    fetch_admin_database_rows,
    fetch_admin_stats,
    fetch_platform_review_history,
    fetch_review_job_states,
    schema_table_names,
)
from .infra import init_infra
from .models import (
    AdminStatsResponse,
    AdminDatabaseRowsResponse,
    AdminDatabaseTableListResponse,
    BulkCreateReviewRequest,
    BulkCreateReviewResponse,
    ChunkUploadCompleteRequest,
    ChunkUploadInitRequest,
    ChunkUploadInitResponse,
    ChunkUploadPartResponse,
    CreateReviewRequest,
    CreateReviewResponse,
    OssUploadCompleteRequest,
    OssUploadCredentials,
    OssUploadInitRequest,
    OssUploadInitResponse,
    PlatformBatchCreateReviewItem,
    PlatformBatchCreateReviewRequest,
    PlatformBatchCreateReviewResponse,
    PlatformCreateReviewRequest,
    PlatformCreateReviewResponse,
    PlatformReviewHistoryResponse,
    PlatformReviewResultResponse,
    PlatformReviewStatusResponse,
    ReviewStatus,
    SystemMetricsResponse,
)
from .oss import build_oss_object_key, create_upload_credentials, head_oss_object
from .policies import dump_policy, load_policy
from .queue import ReviewQueueStage, close_redis, enqueue_review, enqueue_review_stage, get_redis, redis_last_error
from .store import store
from .system_monitor import start_system_monitor, stop_system_monitor, system_metrics
from .tasks import add_event, create_job, persist_created_job, reconcile_stale_reviews, run_review, update_job
from .utils import new_id, safe_filename


app = FastAPI(title="SN2S Video Review", version="0.1.0")
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
UPLOAD_ID_RE = re.compile(r"^upload_session_[a-f0-9]{16}$")
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_api_nonce_cache: dict[str, float] = {}


@dataclass(frozen=True)
class PlatformAuth:
    app_id: str
    feishu_user_id: str | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_name: str | None = None
    feishu_tenant_key: str | None = None
    is_admin: bool = False


@dataclass(frozen=True)
class PlatformCreateLock:
    review_id: str
    redis_key: str | None = None
    local_path: Path | None = None


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"success": False, "error_code": code, "message": message})


def platform_review_id(app_id: str, platform_task_id: str) -> str:
    raw = f"{app_id}:{platform_task_id}".encode("utf-8")
    return f"review_{hashlib.sha256(raw).hexdigest()[:16]}"


def platform_urls(review_id: str) -> dict[str, str]:
    return {
        "status_url": f"/api/v1/reviews/{review_id}",
        "result_url": f"/api/v1/reviews/{review_id}/result",
        "cancel_url": f"/api/v1/reviews/{review_id}/cancel",
    }


def parse_api_auth_secrets() -> dict[str, str]:
    secrets: dict[str, str] = {}
    if settings.api_auth_secret:
        secrets["*"] = settings.api_auth_secret
    for item in (settings.api_auth_secrets or "").split(","):
        raw = item.strip()
        if not raw:
            continue
        if ":" in raw:
            app_id, secret = raw.split(":", 1)
            if app_id.strip() and secret:
                secrets[app_id.strip()] = secret
        else:
            secrets["*"] = raw
    return secrets


def csv_values(raw: str | None) -> set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def header_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def build_uploader_info(feishu_user_name: str | None, feishu_user_id: str | None) -> str:
    if feishu_user_name and feishu_user_id:
        return f"{feishu_user_name}（{feishu_user_id}）"
    return feishu_user_name or feishu_user_id or ""


def parse_platform_auth(request: Request) -> PlatformAuth:
    app_id = (request.headers.get("x-app-id") or "default").strip() or "default"
    feishu_user_id = (request.headers.get("x-feishu-user-id") or "").strip() or None
    feishu_open_id = (request.headers.get("x-feishu-open-id") or "").strip() or None
    feishu_union_id = (request.headers.get("x-feishu-union-id") or "").strip() or None
    feishu_user_name = (request.headers.get("x-feishu-user-name") or "").strip() or None
    feishu_tenant_key = (request.headers.get("x-feishu-tenant-key") or "").strip() or None
    admin_user_ids = csv_values(settings.api_admin_feishu_user_ids)
    admin_app_ids = csv_values(settings.api_admin_app_ids)
    is_admin = (
        header_bool(request.headers.get("x-feishu-is-admin"))
        or (feishu_user_id is not None and feishu_user_id in admin_user_ids)
        or app_id in admin_app_ids
    )
    return PlatformAuth(
        app_id=app_id,
        feishu_user_id=feishu_user_id,
        feishu_open_id=feishu_open_id,
        feishu_union_id=feishu_union_id,
        feishu_user_name=feishu_user_name,
        feishu_tenant_key=feishu_tenant_key,
        is_admin=is_admin,
    )


def expected_api_signature(secret: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    body_sha256 = hashlib.sha256(body).hexdigest()
    base = "\n".join([timestamp, nonce, method.upper(), path, body_sha256])
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


async def remember_api_nonce(app_id: str, nonce: str) -> bool:
    ttl = max(60, settings.api_auth_clock_skew_seconds)
    cache_key = f"{settings.redis_cache_prefix}:api_nonce:{app_id}:{nonce}"
    client = await get_redis()
    if client is not None:
        return bool(await client.set(cache_key, "1", ex=ttl, nx=True))

    now = time.time()
    expired = [key for key, expires_at in _api_nonce_cache.items() if expires_at <= now]
    for key in expired:
        _api_nonce_cache.pop(key, None)
    local_key = f"{app_id}:{nonce}"
    if local_key in _api_nonce_cache:
        return False
    _api_nonce_cache[local_key] = now + ttl
    return True


async def acquire_platform_create_lock(review_id: str) -> PlatformCreateLock | None:
    redis_key = f"{settings.redis_cache_prefix}:platform_create_lock:{review_id}"
    client = await get_redis()
    if client is not None:
        try:
            locked = await client.set(redis_key, "1", ex=30, nx=True)
            if locked:
                return PlatformCreateLock(review_id=review_id, redis_key=redis_key)
            return None
        except Exception:
            pass

    settings.ensure_dirs()
    local_path = settings.jobs_dir / f"{review_id}.create.lock"
    try:
        fd = os.open(local_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(now_iso())
    return PlatformCreateLock(review_id=review_id, local_path=local_path)


async def release_platform_create_lock(lock: PlatformCreateLock) -> None:
    if lock.redis_key:
        client = await get_redis()
        if client is not None:
            try:
                await client.delete(lock.redis_key)
            except Exception:
                return
        return
    if lock.local_path:
        try:
            lock.local_path.unlink()
        except FileNotFoundError:
            return


async def wait_for_existing_platform_job(review_id: str):
    deadline = time.monotonic() + max(0, settings.api_idempotency_wait_seconds)
    while time.monotonic() <= deadline:
        existing = store.get_job(review_id)
        if existing:
            return existing
        await asyncio.sleep(0.1)
    return store.get_job(review_id)


async def require_platform_auth(request: Request) -> PlatformAuth:
    auth = parse_platform_auth(request)
    if not settings.api_auth_enabled:
        return auth

    secrets = parse_api_auth_secrets()
    secret = secrets.get(auth.app_id) or secrets.get("*")
    if not secret:
        raise api_error(503, "API_AUTH_NOT_CONFIGURED", "平台 API 鉴权密钥未配置")

    timestamp = (request.headers.get("x-timestamp") or "").strip()
    nonce = (request.headers.get("x-nonce") or "").strip()
    signature = (request.headers.get("x-signature") or "").strip()
    if signature.startswith("sha256="):
        signature = signature.removeprefix("sha256=")
    if not timestamp or not nonce or not signature:
        raise api_error(401, "UNAUTHORIZED", "缺少 X-Timestamp、X-Nonce 或 X-Signature")
    try:
        timestamp_seconds = int(timestamp)
    except ValueError as exc:
        raise api_error(401, "INVALID_TIMESTAMP", "X-Timestamp 必须是 Unix 秒级时间戳") from exc
    now_seconds = int(datetime.now(timezone.utc).timestamp())
    if abs(now_seconds - timestamp_seconds) > settings.api_auth_clock_skew_seconds:
        raise api_error(401, "TIMESTAMP_EXPIRED", "签名时间戳已过期")
    if not await remember_api_nonce(auth.app_id, nonce):
        raise api_error(401, "REPLAYED_NONCE", "X-Nonce 已使用，疑似重放请求")

    body = await request.body()
    expected = expected_api_signature(secret, timestamp, nonce, request.method, request.url.path, body)
    if not hmac.compare_digest(signature, expected):
        raise api_error(401, "INVALID_SIGNATURE", "请求签名不正确")
    return auth


def require_feishu_user(auth: PlatformAuth) -> None:
    if not auth.feishu_user_id:
        raise api_error(401, "FEISHU_LOGIN_REQUIRED", "缺少飞书登录用户信息")


def require_admin(auth: PlatformAuth) -> None:
    if not auth.is_admin:
        raise api_error(403, "ADMIN_REQUIRED", "需要管理员权限")


def ensure_review_access(job, auth: PlatformAuth) -> None:
    if not job.feishu_user_id:
        return
    if auth.is_admin:
        return
    if auth.feishu_user_id == job.feishu_user_id:
        return
    raise api_error(403, "FORBIDDEN_REVIEW", "只能查询自己的审核数据")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
async def startup() -> None:
    settings.ensure_dirs()
    await init_infra(strict=False)
    start_system_monitor()


@app.on_event("shutdown")
async def shutdown() -> None:
    await stop_system_monitor()
    await close_pool()
    await close_redis()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "service": "sn2s-video-review",
        "postgres_configured": bool(settings.database_url),
        "redis_configured": bool(settings.redis_url),
        "redis_queue_enabled": settings.use_redis_queue,
        "postgres_last_error": db_last_error(),
        "redis_last_error": redis_last_error(),
    }


async def dispatch_review(job, request: CreateReviewRequest, background_tasks: BackgroundTasks) -> None:
    await persist_created_job(job, request)
    if settings.use_redis_queue:
        if settings.pipeline_mode == "staged":
            stream_id = await enqueue_review_stage(job.review_id, request, ReviewQueueStage.PREPROCESS)
        else:
            stream_id = await enqueue_review(job.review_id, request)
        if stream_id:
            event = {"stream_id": stream_id}
            if settings.pipeline_mode == "staged":
                event["stage"] = ReviewQueueStage.PREPROCESS.value
            await add_event(job.review_id, "queued", event)
            return
        await add_event(job.review_id, "status", {"text": "Redis 队列不可用，降级为本进程执行"})
    background_tasks.add_task(run_review, job.review_id, request)


def create_review_response(review_id: str) -> CreateReviewResponse:
    return CreateReviewResponse(
        success=True,
        review_id=review_id,
        status_url=f"/video/reviews/{review_id}",
        stream_url=f"/video/reviews/{review_id}/stream",
        report_url=f"/video/reviews/{review_id}/report",
    )


def validate_platform_source(request: PlatformCreateReviewRequest) -> None:
    has_url = bool((request.video_url or "").strip())
    has_oss = bool((request.oss_bucket or "").strip() and (request.oss_key or "").strip())
    if has_url == has_oss:
        raise api_error(400, "INVALID_SOURCE", "video_url 和 oss_bucket/oss_key 必须二选一")
    if has_url:
        validate_platform_video_url(request.video_url or "")


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    host = host.lower()
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != pattern[2:]
    return fnmatch.fnmatch(host, pattern)


def validate_platform_video_url(video_url: str) -> None:
    parsed = urlparse(video_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise api_error(400, "VIDEO_URL_NOT_ALLOWED", "video_url 必须是 http/https 外部可访问地址")
    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved):
        raise api_error(400, "VIDEO_URL_NOT_ALLOWED", "video_url 不能指向内网、localhost 或云元数据地址")
    allowed_hosts = [item.strip() for item in (settings.api_video_url_allowed_hosts or "").split(",") if item.strip()]
    if allowed_hosts and not any(_host_matches(host, item) for item in allowed_hosts):
        raise api_error(400, "VIDEO_URL_NOT_ALLOWED", "video_url 域名不在平台白名单内")


def to_internal_review_request(request: PlatformCreateReviewRequest, auth: PlatformAuth) -> CreateReviewRequest:
    validate_platform_source(request)
    feishu_user_id = auth.feishu_user_id or request.feishu_user_id
    feishu_open_id = auth.feishu_open_id or request.feishu_open_id
    feishu_union_id = auth.feishu_union_id or request.feishu_union_id
    feishu_user_name = auth.feishu_user_name or request.feishu_user_name
    feishu_tenant_key = auth.feishu_tenant_key or request.feishu_tenant_key
    uploader_info = request.uploader_info or build_uploader_info(feishu_user_name, feishu_user_id)
    drama_title = request.drama_title or request.video_title or ""
    return CreateReviewRequest(
        app_id=auth.app_id,
        platform_task_id=request.platform_task_id,
        feishu_user_id=feishu_user_id,
        feishu_open_id=feishu_open_id,
        feishu_union_id=feishu_union_id,
        feishu_user_name=feishu_user_name,
        feishu_tenant_key=feishu_tenant_key,
        uploader_info=uploader_info,
        drama_title=drama_title,
        video_url=request.video_url,
        oss_bucket=request.oss_bucket,
        oss_key=request.oss_key,
        oss_endpoint=request.oss_endpoint,
        oss_etag=request.oss_etag,
        oss_size=request.oss_size,
        session_id=f"{auth.app_id}:{request.platform_task_id}",
        video_title=request.video_title,
        policy_version=request.policy_version,
        model=request.model,
        fps=request.fps,
        segment_seconds=request.segment_seconds,
        start_seconds=request.start_seconds,
        end_seconds=request.end_seconds,
        callback_url=request.callback_url,
        callback_secret=request.callback_secret,
        metadata=request.metadata,
    )


def create_platform_response(job, platform_task_id: str, *, idempotent: bool = False) -> PlatformCreateReviewResponse:
    urls = platform_urls(job.review_id)
    return PlatformCreateReviewResponse(
        success=True,
        review_id=job.review_id,
        platform_task_id=platform_task_id,
        status=job.status,
        idempotent=idempotent,
        **urls,
    )


async def create_platform_review_job(
    request: PlatformCreateReviewRequest,
    auth: PlatformAuth,
    background_tasks: BackgroundTasks,
) -> PlatformCreateReviewResponse:
    review_id = platform_review_id(auth.app_id, request.platform_task_id)
    existing = store.get_job(review_id)
    if existing:
        return create_platform_response(existing, request.platform_task_id, idempotent=True)
    lock = await acquire_platform_create_lock(review_id)
    if lock is None:
        existing = await wait_for_existing_platform_job(review_id)
        if existing:
            return create_platform_response(existing, request.platform_task_id, idempotent=True)
        raise api_error(409, "REVIEW_CREATE_IN_PROGRESS", "同一平台任务正在创建中，请稍后用相同 platform_task_id 重试")
    try:
        existing = store.get_job(review_id)
        if existing:
            return create_platform_response(existing, request.platform_task_id, idempotent=True)
        review_request = to_internal_review_request(request, auth)
        job = create_job(review_request, review_id=review_id)
        await dispatch_review(job, review_request, background_tasks)
        return create_platform_response(job, request.platform_task_id)
    finally:
        await release_platform_create_lock(lock)


def validate_video_filename(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_VIDEO_SUFFIXES:
        raise HTTPException(400, "仅支持 mp4/mov/avi/mkv/webm 视频文件")
    return suffix


def validate_video_upload(file: UploadFile) -> str:
    return validate_video_filename(file.filename)


def upload_session_dir(upload_id: str) -> Path:
    if not UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(400, "上传会话无效")
    path = settings.upload_sessions_dir / upload_id
    if not path.exists():
        raise HTTPException(404, "上传会话不存在")
    return path


async def read_upload_metadata(upload_id: str) -> dict:
    metadata_path = upload_session_dir(upload_id) / "metadata.json"
    if not metadata_path.exists():
        raise HTTPException(404, "上传会话元数据不存在")
    async with aiofiles.open(metadata_path, encoding="utf-8") as f:
        return json.loads(await f.read())


def assemble_uploaded_chunks(session_dir: Path, target: Path, chunk_count: int) -> None:
    with target.open("wb") as output:
        for index in range(chunk_count):
            chunk_path = session_dir / f"{index:06d}.part"
            if not chunk_path.exists():
                raise FileNotFoundError(f"缺少第 {index + 1} 个上传分片")
            with chunk_path.open("rb") as chunk_file:
                shutil.copyfileobj(chunk_file, output, length=1024 * 1024)


async def save_uploaded_video(file: UploadFile) -> Path:
    suffix = validate_video_upload(file)
    settings.ensure_dirs()
    upload_dir = settings.raw_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_filename(file.filename or "upload.mp4", "upload.mp4")
    target = upload_dir / f"{new_id('upload')}_{Path(safe_name).stem}{suffix}"
    async with aiofiles.open(target, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)
    return target


async def create_review_for_local_file(
    target: Path,
    filename: str,
    payload: ChunkUploadCompleteRequest,
    background_tasks: BackgroundTasks,
    *,
    upload_started_at: str | None = None,
    upload_completed_at: str | None = None,
) -> CreateReviewResponse:
    request = CreateReviewRequest(
        local_path=str(target),
        session_id=payload.session_id,
        video_title=payload.video_title or filename,
        policy_version=payload.policy_version,
        model=payload.model,
        fps=payload.fps,
        segment_seconds=payload.segment_seconds,
        start_seconds=payload.start_seconds,
        end_seconds=payload.end_seconds,
        upload_started_at=upload_started_at,
        upload_completed_at=upload_completed_at,
    )
    job = create_job(request)
    await dispatch_review(job, request, background_tasks)
    return create_review_response(job.review_id)


@app.get("/", response_class=HTMLResponse)
async def web_app() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/admin", response_class=HTMLResponse)
async def admin_app() -> HTMLResponse:
    return await web_app()


@app.get("/video/reviews/policies/current")
async def current_policy() -> dict:
    return dump_policy(load_policy())


@app.get("/video/admin/stats", response_model=AdminStatsResponse)
async def admin_stats(
    limit: int = 100,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> AdminStatsResponse:
    normalized_status = (status or "").strip() or None
    return await fetch_admin_stats(
        limit=limit,
        status=normalized_status,
        created_from=created_from,
        created_to=created_to,
    )


@app.get("/video/admin/system-metrics", response_model=SystemMetricsResponse)
async def admin_system_metrics(window_seconds: int = 1800) -> SystemMetricsResponse:
    return system_metrics(window_seconds=window_seconds)


@app.get("/api/v1/health")
async def api_v1_health() -> dict:
    return await health()


@app.get("/api/v1/policies/current")
async def api_v1_current_policy(auth: PlatformAuth = Depends(require_platform_auth)) -> dict:
    return dump_policy(load_policy())


@app.post("/api/v1/reviews", response_model=PlatformCreateReviewResponse)
async def api_v1_create_review(
    request: PlatformCreateReviewRequest,
    background_tasks: BackgroundTasks,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> PlatformCreateReviewResponse:
    return await create_platform_review_job(request, auth, background_tasks)


@app.post("/api/v1/reviews/batch", response_model=PlatformBatchCreateReviewResponse)
async def api_v1_create_batch_reviews(
    request: PlatformBatchCreateReviewRequest,
    background_tasks: BackgroundTasks,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> PlatformBatchCreateReviewResponse:
    items: list[PlatformBatchCreateReviewItem] = []
    for child in request.items:
        try:
            created = await create_platform_review_job(child, auth, background_tasks)
            items.append(PlatformBatchCreateReviewItem(**created.model_dump(mode="json")))
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            items.append(
                PlatformBatchCreateReviewItem(
                    success=False,
                    platform_task_id=child.platform_task_id,
                    error_code=str(detail.get("error_code") or "CREATE_REVIEW_FAILED"),
                    message=str(detail.get("message") or exc.detail),
                )
            )
    accepted_count = sum(1 for item in items if item.success)
    failed_count = len(items) - accepted_count
    return PlatformBatchCreateReviewResponse(
        success=failed_count == 0,
        accepted_count=accepted_count,
        failed_count=failed_count,
        items=items,
    )


@app.get("/api/v1/reviews/history", response_model=PlatformReviewHistoryResponse)
async def api_v1_my_review_history(
    auth: PlatformAuth = Depends(require_platform_auth),
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PlatformReviewHistoryResponse:
    require_feishu_user(auth)
    return await fetch_platform_review_history(
        feishu_user_id=auth.feishu_user_id,
        include_all=False,
        status=(status or "").strip() or None,
        created_from=created_from or start_time,
        created_to=created_to or end_time,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/admin/reviews/history", response_model=PlatformReviewHistoryResponse)
async def api_v1_admin_review_history(
    auth: PlatformAuth = Depends(require_platform_auth),
    feishu_user_id: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PlatformReviewHistoryResponse:
    require_admin(auth)
    return await fetch_platform_review_history(
        feishu_user_id=(feishu_user_id or "").strip() or None,
        include_all=True,
        status=(status or "").strip() or None,
        created_from=created_from or start_time,
        created_to=created_to or end_time,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/admin/database", response_model=AdminDatabaseTableListResponse)
async def api_v1_admin_database_tables(
    auth: PlatformAuth = Depends(require_platform_auth),
) -> AdminDatabaseTableListResponse:
    require_admin(auth)
    return AdminDatabaseTableListResponse(tables=sorted(schema_table_names()))


@app.get("/api/v1/admin/database/{table}", response_model=AdminDatabaseRowsResponse)
async def api_v1_admin_database_rows(
    table: str,
    auth: PlatformAuth = Depends(require_platform_auth),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AdminDatabaseRowsResponse:
    require_admin(auth)
    if table not in schema_table_names():
        raise api_error(400, "DATABASE_TABLE_NOT_ALLOWED", "只能查询视频审核业务表")
    return await fetch_admin_database_rows(table=table, limit=limit, offset=offset)


@app.post("/api/v1/admin/reviews/reconcile-stale")
async def api_v1_admin_reconcile_stale_reviews(
    auth: PlatformAuth = Depends(require_platform_auth),
    older_than_minutes: int | None = Query(default=None, ge=5, le=1440),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    require_admin(auth)
    count = await reconcile_stale_reviews(
        older_than_minutes=older_than_minutes,
        limit=limit,
    )
    return {
        "success": True,
        "reconciled_count": count,
        "older_than_minutes": older_than_minutes or settings.stale_processing_minutes,
    }


@app.get("/api/v1/reviews/{review_id}", response_model=PlatformReviewStatusResponse)
async def api_v1_get_review(
    review_id: str,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> PlatformReviewStatusResponse:
    job = store.get_job(review_id)
    if not job:
        raise api_error(404, "REVIEW_NOT_FOUND", "审核任务不存在")
    ensure_review_access(job, auth)
    return PlatformReviewStatusResponse(
        review_id=job.review_id,
        platform_task_id=job.platform_task_id,
        uploader_info=job.uploader_info,
        drama_title=job.drama_title,
        feishu_user_id=job.feishu_user_id,
        feishu_user_name=job.feishu_user_name,
        status=job.status,
        phase=job.phase,
        message=job.message,
        progress=job.progress,
        error_code="REVIEW_FAILED" if job.error else None,
        error_message=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/api/v1/reviews/{review_id}/result", response_model=PlatformReviewResultResponse)
async def api_v1_get_review_result(
    review_id: str,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> PlatformReviewResultResponse:
    job = store.get_job(review_id)
    if not job:
        raise api_error(404, "REVIEW_NOT_FOUND", "审核任务不存在")
    ensure_review_access(job, auth)
    if job.status != ReviewStatus.COMPLETED:
        raise api_error(409, "RESULT_NOT_READY", f"审核尚未完成，当前状态：{job.status.value}")
    report = store.get_report(review_id)
    if not report:
        raise api_error(404, "REPORT_NOT_FOUND", "审核报告不存在")
    return PlatformReviewResultResponse(
        success=True,
        review_id=review_id,
        platform_task_id=job.platform_task_id,
        status=job.status,
        report=report,
    )


@app.post("/api/v1/reviews/{review_id}/cancel")
async def api_v1_cancel_review(
    review_id: str,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> dict:
    job = store.get_job(review_id)
    if not job:
        raise api_error(404, "REVIEW_NOT_FOUND", "审核任务不存在")
    ensure_review_access(job, auth)
    if job.status in {ReviewStatus.COMPLETED, ReviewStatus.FAILED, ReviewStatus.CANCELLED, ReviewStatus.SOURCE_UNAVAILABLE}:
        return {"success": False, "message": f"任务已结束：{job.status.value}"}
    await update_job(review_id, status=ReviewStatus.CANCELLED, phase="cancelled", message="已请求取消")
    await add_event(review_id, "error", {"error": "已请求取消"})
    return {"success": True}


@app.post("/video/uploads/init", response_model=ChunkUploadInitResponse)
async def init_chunk_upload(request: ChunkUploadInitRequest) -> ChunkUploadInitResponse:
    validate_video_filename(request.filename)
    settings.ensure_dirs()
    upload_id = new_id("upload_session")
    session_dir = settings.upload_sessions_dir / upload_id
    session_dir.mkdir(parents=True, exist_ok=False)
    metadata = {"filename": request.filename, "size": request.size, "upload_started_at": now_iso()}
    async with aiofiles.open(session_dir / "metadata.json", "w", encoding="utf-8") as f:
        await f.write(json.dumps(metadata, ensure_ascii=False))
    return ChunkUploadInitResponse(success=True, upload_id=upload_id, chunk_size=settings.upload_chunk_bytes)


@app.post("/video/oss/uploads/init", response_model=OssUploadInitResponse)
async def init_oss_upload(request: OssUploadInitRequest) -> OssUploadInitResponse:
    validate_video_filename(request.filename)
    if not settings.oss_bucket:
        raise HTTPException(500, "未配置 ALIYUN_OSS_BUCKET，无法使用 OSS 直传")
    settings.ensure_dirs()
    upload_id = new_id("upload_session")
    video_id = new_id("video")
    object_key = build_oss_object_key(prefix=settings.oss_prefix, video_id=video_id, filename=request.filename)
    session_dir = settings.upload_sessions_dir / upload_id
    session_dir.mkdir(parents=True, exist_ok=False)
    upload_started_at = now_iso()
    metadata = {
        "type": "oss",
        "filename": request.filename,
        "size": request.size,
        "content_type": request.content_type,
        "upload_started_at": upload_started_at,
        "video_id": video_id,
        "bucket": settings.oss_bucket,
        "region": settings.oss_region,
        "endpoint": settings.oss_endpoint,
        "object_key": object_key,
    }
    async with aiofiles.open(session_dir / "metadata.json", "w", encoding="utf-8") as f:
        await f.write(json.dumps(metadata, ensure_ascii=False))
    try:
        raw_credentials = await create_upload_credentials(
            upload_id=upload_id,
            object_key=object_key,
            filename=request.filename,
            size=request.size,
        )
    except RuntimeError as exc:
        await asyncio.to_thread(shutil.rmtree, session_dir, True)
        raise HTTPException(503, f"OSS STS 临时凭证签发配置不完整：{exc}") from exc
    except Exception as exc:
        await asyncio.to_thread(shutil.rmtree, session_dir, True)
        raise HTTPException(502, f"OSS STS 临时凭证签发失败：{exc}") from exc
    return OssUploadInitResponse(
        success=True,
        upload_id=upload_id,
        video_id=video_id,
        bucket=settings.oss_bucket,
        region=settings.oss_region,
        endpoint=settings.oss_endpoint,
        object_key=object_key,
        upload_started_at=upload_started_at,
        credentials=OssUploadCredentials.model_validate(raw_credentials),
    )


@app.post("/video/oss/uploads/complete", response_model=CreateReviewResponse)
async def complete_oss_upload(
    request: OssUploadCompleteRequest,
    background_tasks: BackgroundTasks,
) -> CreateReviewResponse:
    session_dir = upload_session_dir(request.upload_id)
    metadata = await read_upload_metadata(request.upload_id)
    if metadata.get("type") != "oss":
        raise HTTPException(400, "上传会话类型不是 OSS")
    filename = request.filename or metadata.get("filename") or "upload.mp4"
    validate_video_filename(filename)
    bucket = metadata["bucket"]
    object_key = metadata["object_key"]
    try:
        head = await head_oss_object(bucket, object_key)
    except Exception as exc:
        raise HTTPException(400, f"OSS 对象校验失败：{exc}") from exc
    head_size = int(head.get("content_length") or 0)
    expected_size = request.size if request.size is not None else int(metadata.get("size") or 0)
    if expected_size and head_size and head_size != expected_size:
        raise HTTPException(400, f"OSS 文件大小不一致：期望 {expected_size}，实际 {head_size}")
    upload_completed_at = now_iso()
    review_request = CreateReviewRequest(
        oss_bucket=bucket,
        oss_key=object_key,
        oss_endpoint=metadata.get("endpoint") or settings.oss_endpoint,
        oss_etag=head.get("etag") or request.etag,
        oss_size=head_size or expected_size,
        session_id=request.session_id,
        video_title=request.video_title or filename,
        policy_version=request.policy_version,
        model=request.model,
        fps=request.fps,
        segment_seconds=request.segment_seconds,
        start_seconds=request.start_seconds,
        end_seconds=request.end_seconds,
        upload_started_at=metadata.get("upload_started_at"),
        upload_completed_at=upload_completed_at,
    )
    job = create_job(review_request, review_id=metadata.get("video_id", "").replace("video_", "review_", 1) or None)
    await dispatch_review(job, review_request, background_tasks)
    await asyncio.to_thread(shutil.rmtree, session_dir, True)
    return create_review_response(job.review_id)


@app.post("/api/v1/uploads/oss/init", response_model=OssUploadInitResponse)
async def api_v1_init_oss_upload(
    request: OssUploadInitRequest,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> OssUploadInitResponse:
    return await init_oss_upload(request)


@app.post("/api/v1/uploads/oss/complete", response_model=PlatformCreateReviewResponse)
async def api_v1_complete_oss_upload(
    request: OssUploadCompleteRequest,
    background_tasks: BackgroundTasks,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> PlatformCreateReviewResponse:
    if not request.platform_task_id:
        raise api_error(400, "PLATFORM_TASK_ID_REQUIRED", "platform_task_id 不能为空")
    review_id = platform_review_id(auth.app_id, request.platform_task_id)
    existing = store.get_job(review_id)
    if existing:
        return create_platform_response(existing, request.platform_task_id, idempotent=True)

    lock = await acquire_platform_create_lock(review_id)
    if lock is None:
        existing = await wait_for_existing_platform_job(review_id)
        if existing:
            return create_platform_response(existing, request.platform_task_id, idempotent=True)
        raise api_error(409, "REVIEW_CREATE_IN_PROGRESS", "同一平台任务正在创建中，请稍后用相同 platform_task_id 重试")
    try:
        existing = store.get_job(review_id)
        if existing:
            return create_platform_response(existing, request.platform_task_id, idempotent=True)

        session_dir = upload_session_dir(request.upload_id)
        metadata = await read_upload_metadata(request.upload_id)
        if metadata.get("type") != "oss":
            raise api_error(400, "INVALID_UPLOAD_SESSION", "上传会话类型不是 OSS")
        filename = request.filename or metadata.get("filename") or "upload.mp4"
        validate_video_filename(filename)
        bucket = metadata["bucket"]
        object_key = metadata["object_key"]
        try:
            head = await head_oss_object(bucket, object_key)
        except Exception as exc:
            raise api_error(400, "OSS_OBJECT_VERIFY_FAILED", f"OSS 对象校验失败：{exc}") from exc
        head_size = int(head.get("content_length") or 0)
        expected_size = request.size if request.size is not None else int(metadata.get("size") or 0)
        if expected_size and head_size and head_size != expected_size:
            raise api_error(400, "OSS_OBJECT_SIZE_MISMATCH", f"OSS 文件大小不一致：期望 {expected_size}，实际 {head_size}")

        upload_completed_at = now_iso()
        feishu_user_id = auth.feishu_user_id or request.feishu_user_id
        feishu_open_id = auth.feishu_open_id or request.feishu_open_id
        feishu_union_id = auth.feishu_union_id or request.feishu_union_id
        feishu_user_name = auth.feishu_user_name or request.feishu_user_name
        feishu_tenant_key = auth.feishu_tenant_key or request.feishu_tenant_key
        uploader_info = request.uploader_info or build_uploader_info(feishu_user_name, feishu_user_id)
        drama_title = request.drama_title or request.video_title or filename
        review_request = CreateReviewRequest(
            app_id=auth.app_id,
            platform_task_id=request.platform_task_id,
            feishu_user_id=feishu_user_id,
            feishu_open_id=feishu_open_id,
            feishu_union_id=feishu_union_id,
            feishu_user_name=feishu_user_name,
            feishu_tenant_key=feishu_tenant_key,
            uploader_info=uploader_info,
            drama_title=drama_title,
            oss_bucket=bucket,
            oss_key=object_key,
            oss_endpoint=metadata.get("endpoint") or settings.oss_endpoint,
            oss_etag=head.get("etag") or request.etag,
            oss_size=head_size or expected_size,
            session_id=f"{auth.app_id}:{request.platform_task_id}",
            video_title=request.video_title or filename,
            policy_version=request.policy_version,
            model=request.model,
            fps=request.fps,
            segment_seconds=request.segment_seconds,
            start_seconds=request.start_seconds,
            end_seconds=request.end_seconds,
            upload_started_at=metadata.get("upload_started_at"),
            upload_completed_at=upload_completed_at,
            callback_url=request.callback_url,
            callback_secret=request.callback_secret,
            metadata=request.metadata,
        )
        job = create_job(review_request, review_id=review_id)
        await dispatch_review(job, review_request, background_tasks)
        await asyncio.to_thread(shutil.rmtree, session_dir, True)
        return create_platform_response(job, request.platform_task_id)
    finally:
        await release_platform_create_lock(lock)


@app.post("/api/compat/content-risk/video/tasks")
async def compat_content_risk_create_task(
    request: ContentRiskTaskRequest,
    background_tasks: BackgroundTasks,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> dict:
    platform_request = to_platform_review_request(request)
    created = await create_platform_review_job(platform_request, auth, background_tasks)
    return task_response(
        data_id=request.data_id,
        review_id=created.review_id,
        status=created.status,
        idempotent=created.idempotent,
    )


async def _compat_content_risk_result_payload(
    auth: PlatformAuth,
    data_id: str,
    *,
    raise_http: bool,
    persisted_states: dict[str, dict] | None = None,
) -> dict:
    resolved_data_id = data_id.strip()
    if not resolved_data_id:
        if raise_http:
            raise HTTPException(400, {"code": 40001, "message": "data_id 不能为空"})
        return {"code": 40001, "message": "data_id 不能为空", "data_id": data_id}
    review_id = platform_review_id(auth.app_id, resolved_data_id)
    job = store.get_job(review_id)
    if not job:
        if raise_http:
            raise HTTPException(404, {"code": 40404, "message": "任务不存在", "data_id": resolved_data_id})
        return {"code": 40404, "message": "任务不存在", "data_id": resolved_data_id, "task_id": resolved_data_id}
    try:
        ensure_review_access(job, auth)
    except HTTPException:
        if raise_http:
            raise
        return {"code": 40303, "message": "无权查询任务", "data_id": resolved_data_id, "task_id": resolved_data_id}
    states = persisted_states
    if states is None:
        states = await fetch_review_job_states([review_id])
    persisted = states.get(review_id)
    effective_status = persisted.get("status") if persisted else job.status
    effective_error = persisted.get("error") if persisted else job.error
    effective_status_value = effective_status.value if isinstance(effective_status, ReviewStatus) else str(effective_status)
    if effective_status_value != ReviewStatus.COMPLETED.value:
        return empty_result(data_id=resolved_data_id, status=effective_status, error_message=effective_error)
    report = store.get_report(review_id)
    if not report:
        return empty_result(data_id=resolved_data_id, status=effective_status, error_message="审核报告不存在")
    return report_result(data_id=resolved_data_id, status=effective_status, report=report)


@app.get("/api/compat/content-risk/video/results")
async def compat_content_risk_result(
    auth: PlatformAuth = Depends(require_platform_auth),
    data_id: str | None = None,
    DataId: str | None = None,
    dataID: str | None = None,
) -> dict:
    resolved_data_id = (data_id or DataId or dataID or "").strip()
    return await _compat_content_risk_result_payload(auth, resolved_data_id, raise_http=True)


@app.post("/api/compat/content-risk/video/results/batch")
async def compat_content_risk_batch_result(
    request: ContentRiskBatchResultRequest,
    auth: PlatformAuth = Depends(require_platform_auth),
) -> dict:
    review_ids = [
        platform_review_id(auth.app_id, data_id.strip())
        for data_id in request.data_ids
        if data_id.strip()
    ]
    persisted_states = await fetch_review_job_states(review_ids)
    items = [
        await _compat_content_risk_result_payload(
            auth,
            data_id,
            raise_http=False,
            persisted_states=persisted_states,
        )
        for data_id in request.data_ids
    ]
    return {
        "code": 0,
        "message": "success",
        "count": len(items),
        "items": items,
    }


@app.post("/video/uploads/{upload_id}/chunk", response_model=ChunkUploadPartResponse)
async def upload_chunk(
    upload_id: str,
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
) -> ChunkUploadPartResponse:
    if chunk_index < 0:
        raise HTTPException(400, "分片序号无效")
    session_dir = upload_session_dir(upload_id)
    target = session_dir / f"{chunk_index:06d}.part"
    async with aiofiles.open(target, "wb") as f:
        while data := await chunk.read(1024 * 1024):
            await f.write(data)
    return ChunkUploadPartResponse(success=True, upload_id=upload_id, chunk_index=chunk_index)


@app.post("/video/uploads/{upload_id}/complete", response_model=CreateReviewResponse)
async def complete_chunk_upload(
    upload_id: str,
    request: ChunkUploadCompleteRequest,
    background_tasks: BackgroundTasks,
) -> CreateReviewResponse:
    session_dir = upload_session_dir(upload_id)
    metadata = await read_upload_metadata(upload_id)
    filename = request.filename or metadata.get("filename") or "upload.mp4"
    upload_completed_at = now_iso()
    suffix = validate_video_filename(filename)
    upload_dir = settings.raw_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_filename(filename, "upload.mp4")
    target = upload_dir / f"{new_id('upload')}_{Path(safe_name).stem}{suffix}"
    try:
        await asyncio.to_thread(assemble_uploaded_chunks, session_dir, target, request.chunk_count)
    except FileNotFoundError as err:
        raise HTTPException(400, str(err)) from err
    response = await create_review_for_local_file(
        target,
        filename,
        request,
        background_tasks,
        upload_started_at=metadata.get("upload_started_at"),
        upload_completed_at=upload_completed_at,
    )
    await asyncio.to_thread(shutil.rmtree, session_dir, True)
    return response


@app.post("/video/reviews", response_model=CreateReviewResponse)
async def create_review(request: CreateReviewRequest, background_tasks: BackgroundTasks):
    job = create_job(request)
    await dispatch_review(job, request, background_tasks)
    return create_review_response(job.review_id)


@app.post("/video/reviews/bulk", response_model=BulkCreateReviewResponse)
async def create_bulk_reviews(request: BulkCreateReviewRequest, background_tasks: BackgroundTasks):
    video_urls = []
    seen = set()
    for raw_url in request.video_urls:
        url = (raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        video_urls.append(url)
    if not video_urls:
        raise HTTPException(400, "请至少填写一个视频链接")
    if len(video_urls) > settings.upload_max_files:
        raise HTTPException(400, f"单次最多提交 {settings.upload_max_files} 个视频")

    session_id = request.session_id or new_id("session")
    title_prefix = (request.video_title_prefix or "").strip()
    responses: list[CreateReviewResponse] = []
    for index, video_url in enumerate(video_urls, start=1):
        title = ""
        if index <= len(request.video_titles):
            title = (request.video_titles[index - 1] or "").strip()
        if not title and title_prefix:
            title = f"{title_prefix}-{index:02d}"
        child_request = CreateReviewRequest(
            video_url=video_url,
            session_id=session_id,
            video_title=title or None,
            policy_version=request.policy_version,
            model=request.model,
            fps=request.fps,
            segment_seconds=request.segment_seconds,
            start_seconds=request.start_seconds,
            end_seconds=request.end_seconds,
        )
        job = create_job(child_request)
        await dispatch_review(job, child_request, background_tasks)
        responses.append(create_review_response(job.review_id))
    return BulkCreateReviewResponse(success=True, count=len(responses), reviews=responses)


@app.post("/video/reviews/upload", response_model=CreateReviewResponse)
async def upload_and_create_review(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    video_title: str | None = Form(None),
    fps: int = Form(1),
    segment_seconds: int = Form(180),
    start_seconds: int | None = Form(None),
    end_seconds: int | None = Form(None),
):
    upload_started_at = now_iso()
    target = await save_uploaded_video(file)
    upload_completed_at = now_iso()
    request = CreateReviewRequest(
        local_path=str(target),
        video_title=video_title or file.filename,
        fps=fps,
        segment_seconds=segment_seconds,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        upload_started_at=upload_started_at,
        upload_completed_at=upload_completed_at,
    )
    job = create_job(request)
    await dispatch_review(job, request, background_tasks)
    return create_review_response(job.review_id)


@app.post("/video/reviews/uploads", response_model=BulkCreateReviewResponse)
async def upload_multiple_and_create_reviews(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    video_title_prefix: str | None = Form(None),
    fps: int = Form(1),
    segment_seconds: int = Form(180),
    start_seconds: int | None = Form(None),
    end_seconds: int | None = Form(None),
):
    if not files:
        raise HTTPException(400, "请至少选择一个视频文件")
    if len(files) > settings.upload_max_files:
        raise HTTPException(400, f"单次最多上传 {settings.upload_max_files} 个视频")

    responses: list[CreateReviewResponse] = []
    for index, file in enumerate(files, start=1):
        upload_started_at = now_iso()
        target = await save_uploaded_video(file)
        upload_completed_at = now_iso()
        title_prefix = (video_title_prefix or "").strip()
        title = f"{title_prefix}-{index:02d}" if title_prefix else file.filename
        request = CreateReviewRequest(
            local_path=str(target),
            video_title=title or file.filename,
            fps=fps,
            segment_seconds=segment_seconds,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            upload_started_at=upload_started_at,
            upload_completed_at=upload_completed_at,
        )
        job = create_job(request)
        await dispatch_review(job, request, background_tasks)
        responses.append(create_review_response(job.review_id))
    return BulkCreateReviewResponse(success=True, count=len(responses), reviews=responses)


@app.get("/video/reviews/{review_id}")
async def get_review(review_id: str):
    job = store.get_job(review_id)
    if not job:
        raise HTTPException(404, "review not found")
    return job


@app.get("/video/reviews/{review_id}/report")
async def get_report(review_id: str):
    report = store.get_report(review_id)
    if not report:
        raise HTTPException(404, "report not found")
    return report


@app.post("/video/reviews/{review_id}/cancel")
async def cancel_review(review_id: str):
    job = store.get_job(review_id)
    if not job:
        raise HTTPException(404, "review not found")
    if job.status in {ReviewStatus.COMPLETED, ReviewStatus.FAILED, ReviewStatus.CANCELLED, ReviewStatus.SOURCE_UNAVAILABLE}:
        return {"success": False, "message": f"任务已结束：{job.status.value}"}
    await update_job(review_id, status=ReviewStatus.CANCELLED, phase="cancelled", message="已请求取消")
    await add_event(review_id, "error", {"error": "已请求取消"})
    return {"success": True}


@app.get("/video/reviews/{review_id}/stream")
async def stream_review(review_id: str):
    if not store.get_job(review_id):
        raise HTTPException(404, "review not found")

    async def gen():
        offset = 0
        while True:
            events, offset = store.read_events(review_id, offset)
            for event in events:
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
            job = store.get_job(review_id)
            if job and job.status in {ReviewStatus.COMPLETED, ReviewStatus.FAILED, ReviewStatus.CANCELLED, ReviewStatus.SOURCE_UNAVAILABLE}:
                break
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/video/reviews/dataset/import")
async def import_dataset(file: UploadFile = File(...)):
    settings.ensure_dirs()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        raise HTTPException(400, "仅支持 .xlsx 或 .csv")
    target = settings.datasets_dir / f"upload_{file.filename}"
    async with aiofiles.open(target, "wb") as f:
        await f.write(await file.read())
    if suffix == ".xlsx":
        output = import_xlsx(target)
    else:
        output = import_csv(target)
    return {"success": True, "dataset_path": str(output)}
