from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .analyzer import MultimodalAnalyzer
from .api_key_pool import current_google_api_key_id
from .cleanup import cleanup_review_artifacts
from .config import settings
from .content_risk_compat import build_callback_notification, is_compat_callback
from .db import (
    mark_stale_processing_jobs_failed,
    persist_asset,
    persist_cache_index,
    persist_event,
    persist_job,
    persist_report,
    persist_segment,
)
from .downloader import SourceDownloadError, download_oss_video, download_video, register_local_video
from .judge import build_report
from .model_retry import (
    ModelRetryBudget,
    call_model_with_retry,
    classify_model_error,
    is_rate_limit_error,
    is_retryable_model_error,
    is_transient_model_error,
)
from .models import CreateReviewRequest, ReportNarrative, ReviewJob, ReviewStatus, SegmentReviewResult, VideoAsset
from .policies import load_policy
from .preprocessor import (
    build_frame_sheet,
    enrich_asset,
    extract_frames,
    extract_subtitle_text,
    filter_distinct_frames,
    list_segment_frames,
    make_segment_plan,
)
from .queue import (
    ReviewQueueStage,
    enqueue_review_stage,
    frame_batch_cache_key,
    get_frame_batch_cache,
    model_qpm_slot,
    record_model_call_result,
    schedule_download_retry,
    schedule_stage_retry,
    set_frame_batch_cache,
)
from .store import store
from .utils import new_id
from .workflow import plan_stage_retry, workflow_deadline


semaphore = asyncio.Semaphore(settings.max_concurrent)

async def _safe_persist(awaitable) -> None:
    try:
        await awaitable
    except Exception:
        return


async def _persist_job_with_retry(job: ReviewJob, *, attempts: int = 3) -> None:
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            await persist_job(job)
            return
        except Exception:
            if attempt >= max_attempts:
                raise
            await asyncio.sleep(min(2 ** (attempt - 1), 5))


async def persist_created_job(job: ReviewJob, request: CreateReviewRequest) -> None:
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            await persist_job(job, request, strict=True)
            break
        except Exception:
            if attempt >= max_attempts:
                raise
            await asyncio.sleep(min(2 ** (attempt - 1), 5))
    await _safe_persist(persist_event(job.review_id, "status", {"text": "任务已创建"}))


async def update_job(review_id: str, **kwargs) -> ReviewJob | None:
    job = store.update_job(review_id, **kwargs)
    if job:
        if job.status in {ReviewStatus.COMPLETED, ReviewStatus.FAILED, ReviewStatus.CANCELLED, ReviewStatus.SOURCE_UNAVAILABLE}:
            await _persist_job_with_retry(job)
        else:
            await _safe_persist(persist_job(job))
    return job


async def add_event(review_id: str, event_type: str, data: dict) -> None:
    store.add_event(review_id, event_type, data)
    await _safe_persist(persist_event(review_id, event_type, data))


async def save_report(report) -> str:
    report_path = store.save_report(report)
    await _safe_persist(persist_report(report, str(report_path)))
    return str(report_path)


async def cleanup_terminal_artifacts(review_id: str) -> None:
    if not settings.cleanup_enabled:
        return
    job = store.get_job(review_id)
    if not job or job.status not in {ReviewStatus.COMPLETED, ReviewStatus.FAILED, ReviewStatus.CANCELLED, ReviewStatus.SOURCE_UNAVAILABLE}:
        return
    result = await asyncio.to_thread(cleanup_review_artifacts, job, dry_run=False)
    if result.items:
        await add_event(
            review_id,
            "storage_cleanup",
            {
                "watermark_action": result.watermark_action,
                "disk_used_percent": round(result.disk_used_percent, 2),
                "deleted_count": result.deleted_count,
                "deleted_bytes": result.deleted_bytes,
                "items": [item.__dict__ for item in result.items],
            },
        )


async def fail_workflow(
    review_id: str,
    request: CreateReviewRequest,
    *,
    message: str,
    error: str,
) -> None:
    await update_job(
        review_id,
        status=ReviewStatus.FAILED,
        phase="error",
        message=message,
        error=error,
    )
    await add_event(review_id, "error", {"error": error})
    try:
        await send_review_callback(review_id, request, ReviewStatus.FAILED.value, error=error)
    except Exception as callback_exc:
        await add_event(review_id, "status", {"text": f"审核失败回调失败：{callback_exc}"})
    await cleanup_terminal_artifacts(review_id)


async def reconcile_stale_reviews(
    *,
    older_than_minutes: int | None = None,
    limit: int = 500,
) -> int:
    stale_jobs = await mark_stale_processing_jobs_failed(
        older_than_minutes=older_than_minutes,
        limit=limit,
    )
    for stale_job in stale_jobs:
        review_id = str(stale_job["review_id"])
        error = "STALE_PROCESSING_TIMEOUT"
        await update_job(
            review_id,
            status=ReviewStatus.FAILED,
            phase="error",
            message="审核处理超时，已终止",
            error=error,
        )
        await add_event(review_id, "status", {"text": "审核处理超时，正在发送失败回调"})
        try:
            request = CreateReviewRequest.model_validate(stale_job.get("request") or {})
            await send_review_callback(review_id, request, ReviewStatus.FAILED.value, error=error)
        except Exception as callback_exc:
            await add_event(review_id, "status", {"text": f"审核超时失败回调失败：{callback_exc}"})
        await cleanup_terminal_artifacts(review_id)
    return len(stale_jobs)


def _asset_snapshot_path(video_id: str) -> Path:
    return settings.derived_dir / video_id / "asset.json"


def _save_asset_snapshot(asset: VideoAsset) -> None:
    target = _asset_snapshot_path(asset.video_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(asset.model_dump_json(indent=2), encoding="utf-8")


def _load_asset_snapshot(review_id: str) -> VideoAsset:
    job = store.get_job(review_id)
    if not job or not job.video_id:
        raise RuntimeError(f"任务 {review_id} 缺少预处理资产")
    path = _asset_snapshot_path(job.video_id)
    if path.exists():
        return VideoAsset.model_validate_json(path.read_text(encoding="utf-8"))
    if not job.local_path:
        raise RuntimeError(f"任务 {review_id} 缺少本地视频路径")
    return VideoAsset(video_id=job.video_id, local_path=job.local_path, sha256="")


async def _prepare_review_asset(review_id: str, request: CreateReviewRequest) -> VideoAsset:
    await update_job(review_id, status=ReviewStatus.PROCESSING, phase="ingest", message="正在准备视频")
    await add_event(review_id, "status", {"text": "正在准备视频"})
    if request.video_url:
        asset = await download_video(request.video_url, request.video_title)
    elif request.oss_bucket and request.oss_key:
        asset = await download_oss_video(
            request.oss_bucket,
            request.oss_key,
            title=request.video_title,
            endpoint=request.oss_endpoint,
            etag=request.oss_etag,
            content_length=request.oss_size,
        )
    elif request.local_path:
        asset = register_local_video(request.local_path, request.video_title)
    else:
        raise ValueError("video_url、oss_bucket/oss_key 或 local_path 至少提供一个")
    _save_asset_snapshot(asset)
    await _safe_persist(persist_asset(asset))
    await update_job(review_id, video_id=asset.video_id, local_path=asset.local_path)
    return asset


async def _preprocess_review_asset(review_id: str, request: CreateReviewRequest, asset: VideoAsset) -> VideoAsset:
    await update_job(review_id, phase="preprocess", message="正在读取视频元数据")
    await add_event(review_id, "status", {"text": "正在读取视频元数据"})
    asset = enrich_asset(asset)
    _save_asset_snapshot(asset)
    await _safe_persist(persist_asset(asset))
    segment_seconds = request.segment_seconds or settings.segment_seconds
    segments = make_segment_plan(
        asset.duration_seconds or 0,
        segment_seconds,
        start_seconds=request.start_seconds,
        end_seconds=request.end_seconds,
    )
    await update_job(
        review_id,
        progress={"current_segment": 0, "total_segments": len(segments), "percentage": 10},
    )
    frame_fps = max(1, min(int(request.fps or settings.frame_fps), 10))
    try:
        frame_dir = extract_frames(
            asset,
            fps=frame_fps,
            start_seconds=request.start_seconds,
            end_seconds=request.end_seconds,
        )
        await add_event(review_id, "status", {"text": f"证据帧已生成：{frame_dir}"})
    except Exception as exc:
        await add_event(review_id, "status", {"text": f"抽帧失败，继续审核：{exc}"})
    return asset


def _download_task_retry_delay_seconds(attempt: int) -> float:
    values: list[float] = []
    for raw_value in settings.download_task_retry_delays_seconds.split(","):
        try:
            values.append(max(0.0, float(raw_value.strip())))
        except ValueError:
            continue
    if not values:
        return 60.0
    return values[min(max(0, attempt - 1), len(values) - 1)]


async def _handle_source_download_error(
    review_id: str,
    request: CreateReviewRequest,
    exc: SourceDownloadError,
) -> None:
    current_attempt = int(request.metadata.get("download_retry_attempt") or 0)
    if exc.retryable and current_attempt < max(0, settings.download_task_retry_attempts):
        next_attempt = current_attempt + 1
        delay_seconds = _download_task_retry_delay_seconds(next_attempt)
        metadata = dict(request.metadata)
        metadata.update(
            {
                "download_retry_attempt": next_attempt,
                "download_retry_reason": exc.code,
            }
        )
        retry_request = request.model_copy(update={"metadata": metadata})
        scheduled = await schedule_download_retry(
            review_id,
            retry_request,
            delay_seconds=delay_seconds,
            attempt=next_attempt,
        )
        if scheduled:
            await update_job(
                review_id,
                status=ReviewStatus.PENDING,
                phase="download_retry",
                message=f"源视频下载异常，{int(delay_seconds)} 秒后第 {next_attempt} 次重试",
                error=str(exc),
            )
            await add_event(
                review_id,
                "download_retry",
                {
                    "code": exc.code,
                    "host": exc.host,
                    "http_status": exc.status_code,
                    "request_attempts": exc.attempts,
                    "task_retry_attempt": next_attempt,
                    "delay_seconds": delay_seconds,
                },
            )
            return

    await update_job(
        review_id,
        status=ReviewStatus.SOURCE_UNAVAILABLE,
        phase="source_unavailable",
        message="源视频不可获取，审核未开始",
        error=str(exc),
    )
    await add_event(
        review_id,
        "source_unavailable",
        {
            "code": exc.code,
            "host": exc.host,
            "http_status": exc.status_code,
            "request_attempts": exc.attempts,
            "task_retry_attempt": current_attempt,
        },
    )
    try:
        await send_review_callback(review_id, request, ReviewStatus.SOURCE_UNAVAILABLE.value, error=str(exc))
    except Exception as callback_exc:
        await add_event(review_id, "status", {"text": f"源视频不可获取回调失败：{callback_exc}"})
    await cleanup_terminal_artifacts(review_id)


def create_job(request: CreateReviewRequest, review_id: str | None = None) -> ReviewJob:
    review_id = review_id or new_id("review")
    job = ReviewJob(
        review_id=review_id,
        app_id=request.app_id,
        platform_task_id=request.platform_task_id,
        feishu_user_id=request.feishu_user_id,
        feishu_open_id=request.feishu_open_id,
        feishu_union_id=request.feishu_union_id,
        feishu_user_name=request.feishu_user_name,
        feishu_tenant_key=request.feishu_tenant_key,
        uploader_info=request.uploader_info,
        drama_title=request.drama_title,
        status=ReviewStatus.PENDING,
        phase="pending",
        message="任务已创建",
        source_url=request.video_url or (f"oss://{request.oss_bucket}/{request.oss_key}" if request.oss_bucket and request.oss_key else None),
        local_path=request.local_path,
        oss_bucket=request.oss_bucket,
        oss_key=request.oss_key,
        upload_started_at=request.upload_started_at,
        upload_completed_at=request.upload_completed_at,
    )
    store.save_job(job)
    store.add_event(review_id, "status", {"text": "任务已创建"})
    return job


def _callback_host_allowed(callback_url: str) -> bool:
    parsed = urlparse(callback_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.scheme == "http" and not settings.api_callback_allow_http:
        return False
    if not parsed.hostname:
        return False
    allowed_hosts = {
        host.strip().lower()
        for host in (settings.api_callback_allowed_hosts or "").split(",")
        if host.strip()
    }
    return not allowed_hosts or parsed.hostname.lower() in allowed_hosts


def _callback_signature(secret: str, timestamp: str, body: bytes) -> str:
    body_sha256 = hashlib.sha256(body).hexdigest()
    base = f"{timestamp}\n{body_sha256}"
    digest = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def send_review_callback(
    review_id: str,
    request: CreateReviewRequest,
    status: str,
    *,
    report=None,
    error: str | None = None,
) -> None:
    if not request.callback_url:
        return
    if not _callback_host_allowed(request.callback_url):
        await add_event(review_id, "status", {"text": "回调地址未通过白名单或协议校验，已跳过"})
        return

    if is_compat_callback(request.metadata):
        data_id = str(request.metadata.get("data_id") or request.platform_task_id or review_id)
        url, body, headers = build_callback_notification(
            callback_url=request.callback_url,
            callback_secret=request.callback_secret,
            app_id=request.app_id or "default",
            data_id=data_id,
            status=status,
        )
        async with httpx.AsyncClient(timeout=settings.api_callback_timeout_seconds) as client:
            response = await client.post(url, content=body, headers=headers)
            response.raise_for_status()
        return

    event_status = "completed" if status == ReviewStatus.COMPLETED.value or status == "completed" else status
    payload = {
        "event": f"review.{event_status}",
        "review_id": review_id,
        "platform_task_id": request.platform_task_id,
        "status": event_status,
        "result_url": f"/api/v1/reviews/{review_id}/result",
        "error": error,
        "report": report.model_dump(mode="json") if report is not None else None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    headers = {
        "Content-Type": "application/json",
        "X-App-Id": request.app_id or "default",
        "X-Timestamp": timestamp,
    }
    if request.callback_secret:
        headers["X-Signature"] = _callback_signature(request.callback_secret, timestamp, body)

    async with httpx.AsyncClient(timeout=settings.api_callback_timeout_seconds) as client:
        response = await client.post(request.callback_url, content=body, headers=headers)
        response.raise_for_status()


def _is_transient_model_error(exc: BaseException) -> bool:
    return is_transient_model_error(exc)


def _is_rate_limit_error(exc: BaseException) -> bool:
    return is_rate_limit_error(exc)


def _exception_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return f"{exc.__class__.__name__}: {text}"
    if isinstance(exc, TimeoutError):
        return "TimeoutError: 模型调用超时"
    return exc.__class__.__name__


async def _analyze_subtitle_resilient(
    *,
    review_id: str,
    analyzer: MultimodalAnalyzer,
    subtitle_text: str,
    video_title: str | None,
    subtitle_source: str,
    retry_budget: ModelRetryBudget | None = None,
) -> SegmentReviewResult | None:
    try:
        return await _call_model_with_timeout(
            lambda: analyzer.analyze_subtitle_text(
                subtitle_text,
                video_title=video_title,
                source=subtitle_source,
            ),
            review_id=review_id,
            label="字幕文本审核",
            retry_budget=retry_budget,
        )
    except Exception as exc:
        await add_event(
            review_id,
            "status",
            {"text": f"字幕模型审核失败，进入任务级重试：{_exception_text(exc)}"},
        )
        raise


def _model_retry_event_handler(review_id: str):
    async def on_retry(event: dict) -> None:
        await add_event(
            review_id,
            "status",
            {
                "text": (
                    f"{event['label']} 调用失败（{event['error_kind']}），"
                    f"{event['delay_seconds']:.1f} 秒后进行第 {event['next_attempt']}/"
                    f"{event['max_attempts']} 次尝试"
                )
            },
        )

    return on_retry


async def _call_model_with_timeout(
    operation,
    *,
    review_id: str | None = None,
    label: str = "模型",
    retry_budget: ModelRetryBudget | None = None,
    parse_attempts: int | None = None,
    transient_attempts: int | None = None,
    rate_limit_attempts: int | None = None,
):
    async def on_attempt_result(event: dict) -> None:
        await record_model_call_result(
            success=bool(event.get("success")),
            error_kind=str(event.get("error_kind") or "unknown"),
            api_key_id=current_google_api_key_id.get(),
        )

    return await call_model_with_retry(
        operation,
        label=label,
        qpm_slot_factory=model_qpm_slot,
        retry_budget=retry_budget,
        on_retry=_model_retry_event_handler(review_id) if review_id else None,
        on_attempt_result=on_attempt_result,
        parse_attempts=parse_attempts,
        transient_attempts=transient_attempts,
        rate_limit_attempts=rate_limit_attempts,
    )


async def _analyze_frame_batch_resilient(
    *,
    review_id: str,
    analyzer: MultimodalAnalyzer,
    policy,
    asset,
    segment,
    batch: list[dict],
    frame_fps: int,
    video_title: str | None,
    retry_budget: ModelRetryBudget | None = None,
    split_depth: int = 0,
) -> list[SegmentReviewResult]:
    cache_model = analyzer.model
    if settings.frame_sheet_enabled:
        cache_model = f"{analyzer.model}:sheet-{settings.frame_sheet_rows}x{settings.frame_sheet_cols}"
    cache_key = frame_batch_cache_key(
        video_sha256=asset.sha256,
        policy_version=policy.version,
        model=cache_model,
        fps=frame_fps,
        frames=batch,
    )
    batch_result = await get_frame_batch_cache(cache_key)
    if batch_result:
        batch_result.segment_index = segment.segment_index
        batch_result.start_time = segment.start_time
        batch_result.end_time = segment.end_time
        await add_event(
            review_id,
            "status",
            {"text": f"命中帧批次缓存：{batch[0]['timestamp']} - {batch[-1]['timestamp']}"},
        )
        return [batch_result]

    async def analyze_once() -> SegmentReviewResult:
        if settings.frame_sheet_enabled:
            sheet = build_frame_sheet(asset, segment, batch)
            return await _call_model_with_timeout(
                lambda: analyzer.analyze_frame_sheets_segment([sheet], segment, video_title=video_title),
                review_id=review_id,
                label=f"第 {segment.segment_index} 段帧拼图 {batch[0]['timestamp']} - {batch[-1]['timestamp']}",
                retry_budget=retry_budget,
            )
        return await _call_model_with_timeout(
            lambda: analyzer.analyze_frames_segment(batch, segment, video_title=video_title),
            review_id=review_id,
            label=f"第 {segment.segment_index} 段帧批次 {batch[0]['timestamp']} - {batch[-1]['timestamp']}",
            retry_budget=retry_budget,
        )

    try:
        batch_result = await analyze_once()
        await set_frame_batch_cache(cache_key, batch_result)
        await _safe_persist(
            persist_cache_index(
                cache_key=cache_key,
                video_id=asset.video_id,
                video_sha256=asset.sha256,
                policy_version=policy.version,
                model=cache_model,
                fps=frame_fps,
                start_time=batch[0]["timestamp"],
                end_time=batch[-1]["timestamp"],
                frame_count=len(batch),
                ttl_seconds=settings.redis_cache_ttl_seconds,
            )
        )
        return [batch_result]
    except Exception as exc:
        if _is_rate_limit_error(exc):
            await add_event(
                review_id,
                "status",
                {
                    "text": (
                        f"帧批次 {batch[0]['timestamp']} - {batch[-1]['timestamp']} "
                        "模型限流重试耗尽，进入任务级重试"
                    )
                },
            )
            raise
        if classify_model_error(exc) in {"parse", "validation"}:
            await add_event(
                review_id,
                "status",
                {
                    "text": (
                        f"帧批次 {batch[0]['timestamp']} - {batch[-1]['timestamp']} "
                        "结构化响应重试耗尽，进入任务级重试"
                    )
                },
            )
            raise
        if not _is_transient_model_error(exc):
            raise
        min_size = max(1, settings.frame_batch_min_size)
        max_split_depth = max(0, settings.frame_batch_max_split_depth)
        if len(batch) > min_size and split_depth < max_split_depth:
            midpoint = max(1, len(batch) // 2)
            await add_event(
                review_id,
                "status",
                {
                    "text": (
                        f"帧批次 {batch[0]['timestamp']} - {batch[-1]['timestamp']} 模型超时，"
                        f"拆成 {len(batch[:midpoint])} 帧 + {len(batch[midpoint:])} 帧重试"
                    )
                },
            )
            left = await _analyze_frame_batch_resilient(
                review_id=review_id,
                analyzer=analyzer,
                policy=policy,
                asset=asset,
                segment=segment,
                batch=batch[:midpoint],
                frame_fps=frame_fps,
                video_title=video_title,
                retry_budget=retry_budget,
                split_depth=split_depth + 1,
            )
            right = await _analyze_frame_batch_resilient(
                review_id=review_id,
                analyzer=analyzer,
                policy=policy,
                asset=asset,
                segment=segment,
                batch=batch[midpoint:],
                frame_fps=frame_fps,
                video_title=video_title,
                retry_budget=retry_budget,
                split_depth=split_depth + 1,
            )
            return left + right

        if split_depth >= max_split_depth and len(batch) > min_size:
            await add_event(
                review_id,
                "status",
                {
                    "text": (
                        f"帧批次 {batch[0]['timestamp']} - {batch[-1]['timestamp']} "
                        f"达到最大拆分深度 {max_split_depth}，进入任务级重试"
                    )
                },
            )
            raise

        try:
            batch_result = await analyze_once()
            return [batch_result]
        except Exception as retry_exc:
            if not _is_transient_model_error(retry_exc):
                raise
            await add_event(
                review_id,
                "status",
                {
                    "text": (
                        f"帧批次 {batch[0]['timestamp']} - {batch[-1]['timestamp']} 重试仍超时，"
                        "进入任务级重试"
                    )
                },
            )
            raise


async def _run_frame_batches_concurrently(
    *,
    review_id: str,
    analyzer: MultimodalAnalyzer,
    policy,
    asset,
    segment,
    batches: list[tuple[int, list[dict]]],
    batch_total: int,
    frame_fps: int,
    video_title: str | None,
    retry_budget: ModelRetryBudget | None = None,
) -> list[SegmentReviewResult]:
    batch_semaphore = asyncio.Semaphore(max(1, settings.frame_batch_concurrency))

    async def run_one(batch_number: int, batch: list[dict]) -> tuple[int, list[SegmentReviewResult]]:
        async with batch_semaphore:
            await add_event(
                review_id,
                "status",
                {
                    "text": (
                        f"第 {segment.segment_index} 段抽帧审核 "
                        f"{batch_number}/{batch_total}，帧 {batch[0]['timestamp']} - {batch[-1]['timestamp']}"
                    )
                },
            )
            sub_results = await _analyze_frame_batch_resilient(
                review_id=review_id,
                analyzer=analyzer,
                policy=policy,
                asset=asset,
                segment=segment,
                batch=batch,
                frame_fps=frame_fps,
                video_title=video_title,
                retry_budget=retry_budget,
            )
            for batch_result in sub_results:
                for finding in batch_result.findings:
                    await add_event(review_id, "finding", finding.model_dump())
            return batch_number, sub_results

    completed = await asyncio.gather(*(run_one(batch_number, batch) for batch_number, batch in batches))
    ordered_results: list[SegmentReviewResult] = []
    for _batch_number, sub_results in sorted(completed, key=lambda item: item[0]):
        ordered_results.extend(sub_results)
    return ordered_results


def _job_created_at(job: ReviewJob | None) -> datetime:
    if job is None:
        return datetime.now(timezone.utc)
    try:
        value = datetime.fromisoformat(job.created_at.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


def _workflow_deadline_expired(review_id: str, request: CreateReviewRequest) -> bool:
    _started_at, deadline_at = workflow_deadline(
        request,
        started_at=_job_created_at(store.get_job(review_id)),
    )
    return datetime.now(timezone.utc) >= deadline_at


async def _run_model_review(review_id: str, request: CreateReviewRequest, asset: VideoAsset) -> None:
    segment_seconds = request.segment_seconds or settings.segment_seconds
    segments = make_segment_plan(
        asset.duration_seconds or 0,
        segment_seconds,
        start_seconds=request.start_seconds,
        end_seconds=request.end_seconds,
    )
    frame_fps = max(1, min(int(request.fps or settings.frame_fps), 10))
    results: list[SegmentReviewResult] = []
    narrative: ReportNarrative | None = None
    if settings.mode != "model":
        raise RuntimeError("VIDEO_REVIEW_MODE 必须为 model；本地模拟审核已停用")
    if not settings.google_api_key_pool:
        raise RuntimeError("未配置 GOOGLE_API_KEY 或 GOOGLE_API_KEYS，无法调用多模态审核 API")

    analyzer = MultimodalAnalyzer(model=request.model, fps=request.fps)
    retry_budget = ModelRetryBudget(settings.model_retry_budget_extra_attempts)
    policy = load_policy()
    try:
        segment_frame_map: dict[int, list[dict]] = {
            segment.segment_index: list_segment_frames(asset, segment, fps=frame_fps)
            for segment in segments
        }
        all_frames = [
            frame
            for segment in segments
            for frame in segment_frame_map.get(segment.segment_index, [])
        ]
        if settings.subtitle_review_enabled:
            await add_event(review_id, "status", {"text": "正在抽取字幕并进入文本审核通道"})
            subtitle_text, subtitle_source = extract_subtitle_text(asset, all_frames)
            if subtitle_text.strip():
                await add_event(
                    review_id,
                    "status",
                    {
                        "text": (
                            f"字幕抽取完成：{subtitle_source}，"
                            f"{len(subtitle_text)} 字符，正在进行语言模型审核"
                        )
                    },
                )
                subtitle_result = await _analyze_subtitle_resilient(
                    review_id=review_id,
                    analyzer=analyzer,
                    subtitle_text=subtitle_text,
                    video_title=request.video_title,
                    subtitle_source=subtitle_source,
                    retry_budget=retry_budget,
                )
                if subtitle_result and subtitle_result.findings:
                    results.append(subtitle_result)
                    await _safe_persist(persist_segment(review_id, subtitle_result))
                    for finding in subtitle_result.findings:
                        await add_event(review_id, "finding", finding.model_dump())
                    await add_event(review_id, "segment_complete", subtitle_result.model_dump())
                else:
                    await add_event(review_id, "status", {"text": "字幕文本审核未发现明确风险"})
            else:
                await add_event(review_id, "status", {"text": "未提取到可用字幕，继续视觉审核"})
        for index, segment in enumerate(segments, start=1):
            await update_job(
                review_id,
                phase="scan",
                message=f"正在审核第 {index}/{len(segments)} 段",
                progress={
                    "current_segment": index,
                    "total_segments": len(segments),
                    "percentage": int(10 + (index - 1) / len(segments) * 75),
                },
            )
            await add_event(review_id, "segment_start", segment.model_dump())
            if settings.input_mode == "frames":
                segment_frames = segment_frame_map.get(segment.segment_index) or []
                if not segment_frames:
                    raise ValueError(f"第 {segment.segment_index} 段没有可审核帧")
                original_frame_count = len(segment_frames)
                if settings.frame_dedup_enabled:
                    segment_frames = filter_distinct_frames(
                        segment_frames,
                        threshold=settings.frame_hash_distance_threshold,
                        max_gap_seconds=settings.frame_dedup_max_gap_seconds,
                    )
                    if not segment_frames:
                        segment_frames = segment_frame_map.get(segment.segment_index, [])[:1]
                    await add_event(
                        review_id,
                        "status",
                        {
                            "text": (
                                f"第 {segment.segment_index} 段感知哈希去重："
                                f"{original_frame_count} 帧 -> {len(segment_frames)} 帧"
                            )
                        },
                    )
                batch_size = max(1, settings.frame_batch_size)
                await add_event(
                    review_id,
                    "status",
                    {
                        "text": (
                            f"第 {segment.segment_index} 段共 {len(segment_frames)} 帧，"
                            f"每批 {batch_size} 帧，预计 {((len(segment_frames) - 1) // batch_size) + 1} 次模型调用"
                            f"，并发 {max(1, settings.frame_batch_concurrency)}"
                            f"{'，4x4 拼图模式' if settings.frame_sheet_enabled else ''}"
                        )
                    },
                )
                batches: list[tuple[int, list[dict]]] = []
                for batch_start in range(0, len(segment_frames), batch_size):
                    batch = segment_frames[batch_start : batch_start + batch_size]
                    batch_number = batch_start // batch_size + 1
                    batches.append((batch_number, batch))
                batch_results = await _run_frame_batches_concurrently(
                    review_id=review_id,
                    analyzer=analyzer,
                    policy=policy,
                    asset=asset,
                    segment=segment,
                    batches=batches,
                    batch_total=len(batches),
                    frame_fps=frame_fps,
                    video_title=request.video_title,
                    retry_budget=retry_budget,
                )
                result = SegmentReviewResult(
                    segment_index=segment.segment_index,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    summary="; ".join([r.summary for r in batch_results if r.summary])[:1000],
                    findings=[finding for r in batch_results for finding in r.findings],
                    risk_score=max([r.risk_score for r in batch_results] or [0]),
                )
            else:
                result = await _call_model_with_timeout(
                    lambda: analyzer.analyze_segment(asset, segment, video_title=request.video_title),
                    review_id=review_id,
                    label=f"第 {segment.segment_index} 段视频审核",
                    retry_budget=retry_budget,
                )
                for finding in result.findings:
                    await add_event(review_id, "finding", finding.model_dump())
            results.append(result)
            await _safe_persist(persist_segment(review_id, result))
            await add_event(review_id, "segment_complete", result.model_dump())
        if settings.synthesize_narrative:
            await update_job(review_id, phase="judge", message="正在生成整片剧情与价值观评估")
            await add_event(review_id, "status", {"text": "正在生成整片剧情主线、价值观判断和回正建议"})
            narrative = await _call_model_with_timeout(
                lambda: analyzer.synthesize_narrative_report(results, video_title=request.video_title),
                review_id=review_id,
                label="整片叙事评估",
                retry_budget=retry_budget,
            )
    finally:
        await analyzer.close()

    if _workflow_deadline_expired(review_id, request):
        raise TimeoutError("模型审核超过30分钟工作流截止时间")
    await update_job(review_id, phase="judge", message="正在生成最终裁决")
    report = build_report(review_id, asset.video_id, results, narrative=narrative)
    report_path = await save_report(report)
    await update_job(
        review_id,
        status=ReviewStatus.COMPLETED,
        phase="done",
        message="审核完成",
        progress={"current_segment": len(segments), "total_segments": len(segments), "percentage": 100},
        report_path=str(report_path),
    )
    await add_event(review_id, "complete", report.model_dump())
    try:
        await send_review_callback(review_id, request, ReviewStatus.COMPLETED.value, report=report)
    except Exception as callback_exc:
        await add_event(review_id, "status", {"text": f"审核完成回调失败：{callback_exc}"})
    await cleanup_terminal_artifacts(review_id)


async def run_review(review_id: str, request: CreateReviewRequest) -> None:
    async with semaphore:
        try:
            await update_job(review_id, status=ReviewStatus.PROCESSING, phase="ingest", message="正在准备视频")
            await add_event(review_id, "status", {"text": "正在准备视频"})
            if request.video_url:
                asset = await download_video(request.video_url, request.video_title)
            elif request.oss_bucket and request.oss_key:
                asset = await download_oss_video(
                    request.oss_bucket,
                    request.oss_key,
                    title=request.video_title,
                    endpoint=request.oss_endpoint,
                    etag=request.oss_etag,
                    content_length=request.oss_size,
                )
            elif request.local_path:
                asset = register_local_video(request.local_path, request.video_title)
            else:
                raise ValueError("video_url、oss_bucket/oss_key 或 local_path 至少提供一个")
            await _safe_persist(persist_asset(asset))
            await update_job(review_id, video_id=asset.video_id, local_path=asset.local_path)

            await update_job(review_id, phase="preprocess", message="正在读取视频元数据")
            await add_event(review_id, "status", {"text": "正在读取视频元数据"})
            asset = enrich_asset(asset)
            await _safe_persist(persist_asset(asset))
            segment_seconds = request.segment_seconds or settings.segment_seconds
            segments = make_segment_plan(
                asset.duration_seconds or 0,
                segment_seconds,
                start_seconds=request.start_seconds,
                end_seconds=request.end_seconds,
            )
            await update_job(
                review_id,
                progress={"current_segment": 0, "total_segments": len(segments), "percentage": 10},
            )
            frame_fps = max(1, min(int(request.fps or settings.frame_fps), 10))
            try:
                frame_dir = extract_frames(
                    asset,
                    fps=frame_fps,
                    start_seconds=request.start_seconds,
                    end_seconds=request.end_seconds,
                )
                await add_event(review_id, "status", {"text": f"证据帧已生成：{frame_dir}"})
            except Exception as exc:
                await add_event(review_id, "status", {"text": f"抽帧失败，继续审核：{exc}"})

            results: list[SegmentReviewResult] = []
            narrative: ReportNarrative | None = None
            if settings.mode != "model":
                raise RuntimeError("VIDEO_REVIEW_MODE 必须为 model；本地模拟审核已停用")
            if not settings.google_api_key_pool:
                raise RuntimeError("未配置 GOOGLE_API_KEY 或 GOOGLE_API_KEYS，无法调用多模态审核 API")
            else:
                analyzer = MultimodalAnalyzer(model=request.model, fps=request.fps)
                retry_budget = ModelRetryBudget(settings.model_retry_budget_extra_attempts)
                policy = load_policy()
                try:
                    segment_frame_map: dict[int, list[dict]] = {
                        segment.segment_index: list_segment_frames(asset, segment, fps=frame_fps)
                        for segment in segments
                    }
                    all_frames = [
                        frame
                        for segment in segments
                        for frame in segment_frame_map.get(segment.segment_index, [])
                    ]
                    if settings.subtitle_review_enabled:
                        await add_event(review_id, "status", {"text": "正在抽取字幕并进入文本审核通道"})
                        subtitle_text, subtitle_source = extract_subtitle_text(asset, all_frames)
                        if subtitle_text.strip():
                            await add_event(
                                review_id,
                                "status",
                                {
                                    "text": (
                                        f"字幕抽取完成：{subtitle_source}，"
                                        f"{len(subtitle_text)} 字符，正在进行语言模型审核"
                                    )
                                },
                            )
                            subtitle_result = await _analyze_subtitle_resilient(
                                review_id=review_id,
                                analyzer=analyzer,
                                subtitle_text=subtitle_text,
                                video_title=request.video_title,
                                subtitle_source=subtitle_source,
                                retry_budget=retry_budget,
                            )
                            if subtitle_result and subtitle_result.findings:
                                results.append(subtitle_result)
                                await _safe_persist(persist_segment(review_id, subtitle_result))
                                for finding in subtitle_result.findings:
                                    await add_event(review_id, "finding", finding.model_dump())
                                await add_event(review_id, "segment_complete", subtitle_result.model_dump())
                            else:
                                await add_event(review_id, "status", {"text": "字幕文本审核未发现明确风险"})
                        else:
                            await add_event(review_id, "status", {"text": "未提取到可用字幕，继续视觉审核"})
                    for index, segment in enumerate(segments, start=1):
                        await update_job(
                            review_id,
                            phase="scan",
                            message=f"正在审核第 {index}/{len(segments)} 段",
                            progress={
                                "current_segment": index,
                                "total_segments": len(segments),
                                "percentage": int(10 + (index - 1) / len(segments) * 75),
                            },
                        )
                        await add_event(review_id, "segment_start", segment.model_dump())
                        if settings.input_mode == "frames":
                            segment_frames = segment_frame_map.get(segment.segment_index) or []
                            if not segment_frames:
                                raise ValueError(f"第 {segment.segment_index} 段没有可审核帧")
                            original_frame_count = len(segment_frames)
                            if settings.frame_dedup_enabled:
                                segment_frames = filter_distinct_frames(
                                    segment_frames,
                                    threshold=settings.frame_hash_distance_threshold,
                                    max_gap_seconds=settings.frame_dedup_max_gap_seconds,
                                )
                                if not segment_frames:
                                    segment_frames = segment_frame_map.get(segment.segment_index, [])[:1]
                                await add_event(
                                    review_id,
                                    "status",
                                    {
                                        "text": (
                                            f"第 {segment.segment_index} 段感知哈希去重："
                                            f"{original_frame_count} 帧 -> {len(segment_frames)} 帧"
                                        )
                                    },
                                )
                            batch_size = max(1, settings.frame_batch_size)
                            await add_event(
                                review_id,
                                "status",
                                {
                                    "text": (
                                        f"第 {segment.segment_index} 段共 {len(segment_frames)} 帧，"
                                        f"每批 {batch_size} 帧，预计 {((len(segment_frames) - 1) // batch_size) + 1} 次模型调用"
                                        f"，并发 {max(1, settings.frame_batch_concurrency)}"
                                        f"{'，4x4 拼图模式' if settings.frame_sheet_enabled else ''}"
                                    )
                                },
                            )
                            batches: list[tuple[int, list[dict]]] = []
                            for batch_start in range(0, len(segment_frames), batch_size):
                                batch = segment_frames[batch_start : batch_start + batch_size]
                                batch_number = batch_start // batch_size + 1
                                batches.append((batch_number, batch))
                            batch_total = len(batches)
                            batch_results = await _run_frame_batches_concurrently(
                                review_id=review_id,
                                analyzer=analyzer,
                                policy=policy,
                                asset=asset,
                                segment=segment,
                                batches=batches,
                                batch_total=batch_total,
                                frame_fps=frame_fps,
                                video_title=request.video_title,
                                retry_budget=retry_budget,
                            )
                            merged = SegmentReviewResult(
                                segment_index=segment.segment_index,
                                start_time=segment.start_time,
                                end_time=segment.end_time,
                                summary="; ".join([r.summary for r in batch_results if r.summary])[:1000],
                                findings=[finding for r in batch_results for finding in r.findings],
                                risk_score=max([r.risk_score for r in batch_results] or [0]),
                            )
                            result = merged
                        else:
                            result = await _call_model_with_timeout(
                                lambda: analyzer.analyze_segment(asset, segment, video_title=request.video_title),
                                review_id=review_id,
                                label=f"第 {segment.segment_index} 段视频审核",
                                retry_budget=retry_budget,
                            )
                            for finding in result.findings:
                                await add_event(review_id, "finding", finding.model_dump())
                        results.append(result)
                        await _safe_persist(persist_segment(review_id, result))
                        await add_event(review_id, "segment_complete", result.model_dump())
                    if settings.synthesize_narrative:
                        await update_job(review_id, phase="judge", message="正在生成整片剧情与价值观评估")
                        await add_event(review_id, "status", {"text": "正在生成整片剧情主线、价值观判断和回正建议"})
                        narrative = await _call_model_with_timeout(
                            lambda: analyzer.synthesize_narrative_report(
                                results,
                                video_title=request.video_title,
                            ),
                            review_id=review_id,
                            label="整片叙事评估",
                            retry_budget=retry_budget,
                        )
                finally:
                    await analyzer.close()

            await update_job(review_id, phase="judge", message="正在生成最终裁决")
            report = build_report(review_id, asset.video_id, results, narrative=narrative)
            report_path = await save_report(report)
            await update_job(
                review_id,
                status=ReviewStatus.COMPLETED,
                phase="done",
                message="审核完成",
                progress={"current_segment": len(segments), "total_segments": len(segments), "percentage": 100},
                report_path=str(report_path),
            )
            await add_event(review_id, "complete", report.model_dump())
            try:
                await send_review_callback(review_id, request, ReviewStatus.COMPLETED.value, report=report)
            except Exception as callback_exc:
                await add_event(review_id, "status", {"text": f"审核完成回调失败：{callback_exc}"})
            await cleanup_terminal_artifacts(review_id)
        except SourceDownloadError as exc:
            await _handle_source_download_error(review_id, request, exc)
        except asyncio.CancelledError:
            await update_job(review_id, status=ReviewStatus.CANCELLED, phase="cancelled", message="审核已取消")
            await add_event(review_id, "error", {"error": "审核已取消"})
            try:
                await send_review_callback(review_id, request, ReviewStatus.CANCELLED.value, error="审核已取消")
            except Exception as callback_exc:
                await add_event(review_id, "status", {"text": f"审核取消回调失败：{callback_exc}"})
            await cleanup_terminal_artifacts(review_id)
        except Exception as exc:
            error_text = _exception_text(exc)
            await update_job(review_id, status=ReviewStatus.FAILED, phase="error", message="审核失败", error=error_text)
            await add_event(review_id, "error", {"error": error_text})
            try:
                await send_review_callback(review_id, request, ReviewStatus.FAILED.value, error=error_text)
            except Exception as callback_exc:
                await add_event(review_id, "status", {"text": f"审核失败回调失败：{callback_exc}"})
            await cleanup_terminal_artifacts(review_id)


async def run_preprocess_stage(review_id: str, request: CreateReviewRequest) -> None:
    try:
        asset = await _prepare_review_asset(review_id, request)
        await _preprocess_review_asset(review_id, request, asset)
        stream_id = await enqueue_review_stage(review_id, request, ReviewQueueStage.MODEL)
        if stream_id:
            await update_job(review_id, phase="model_queued", message="预处理完成，等待模型审核")
            await add_event(review_id, "queued", {"stream_id": stream_id, "stage": ReviewQueueStage.MODEL.value})
            return
        await add_event(review_id, "status", {"text": "模型队列不可用，降级为本进程执行模型审核"})
        asset = _load_asset_snapshot(review_id)
        await _run_model_review(review_id, request, asset)
    except SourceDownloadError as exc:
        await _handle_source_download_error(review_id, request, exc)
    except asyncio.CancelledError:
        await update_job(review_id, status=ReviewStatus.CANCELLED, phase="cancelled", message="审核已取消")
        await add_event(review_id, "error", {"error": "审核已取消"})
        try:
            await send_review_callback(review_id, request, ReviewStatus.CANCELLED.value, error="审核已取消")
        except Exception as callback_exc:
            await add_event(review_id, "status", {"text": f"审核取消回调失败：{callback_exc}"})
        await cleanup_terminal_artifacts(review_id)
    except Exception as exc:
        error_text = _exception_text(exc)
        await update_job(review_id, status=ReviewStatus.FAILED, phase="error", message="预处理失败", error=error_text)
        await add_event(review_id, "error", {"error": error_text})
        try:
            await send_review_callback(review_id, request, ReviewStatus.FAILED.value, error=error_text)
        except Exception as callback_exc:
            await add_event(review_id, "status", {"text": f"预处理失败回调失败：{callback_exc}"})
        await cleanup_terminal_artifacts(review_id)


async def run_model_stage(review_id: str, request: CreateReviewRequest) -> None:
    try:
        await update_job(
            review_id,
            status=ReviewStatus.PROCESSING,
            phase="model",
            message="正在执行模型审核",
        )
        asset = _load_asset_snapshot(review_id)
        _started_at, deadline_at = workflow_deadline(
            request,
            started_at=_job_created_at(store.get_job(review_id)),
        )
        remaining_seconds = (deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining_seconds <= 0:
            raise TimeoutError("工作流已超过30分钟截止时间")
        await asyncio.wait_for(
            _run_model_review(review_id, request, asset),
            timeout=remaining_seconds,
        )
    except asyncio.CancelledError:
        await update_job(review_id, status=ReviewStatus.CANCELLED, phase="cancelled", message="审核已取消")
        await add_event(review_id, "error", {"error": "审核已取消"})
        try:
            await send_review_callback(review_id, request, ReviewStatus.CANCELLED.value, error="审核已取消")
        except Exception as callback_exc:
            await add_event(review_id, "status", {"text": f"审核取消回调失败：{callback_exc}"})
        await cleanup_terminal_artifacts(review_id)
    except Exception as exc:
        error_text = _exception_text(exc)
        if is_retryable_model_error(exc):
            job = store.get_job(review_id)
            started_at = _job_created_at(job)
            retry_plan = plan_stage_retry(
                request,
                stage=ReviewQueueStage.MODEL.value,
                reason=error_text,
                error_kind=classify_model_error(exc),
                started_at=started_at,
            )
            if retry_plan is not None:
                scheduled = await schedule_stage_retry(
                    review_id,
                    retry_plan.request,
                    stage=ReviewQueueStage.MODEL,
                    delay_seconds=retry_plan.delay_seconds,
                    attempt=retry_plan.attempt,
                )
                if not scheduled:
                    await add_event(review_id, "status", {"text": "模型延迟重试队列不可用，保留原队列消息等待恢复"})
                    raise RuntimeError("模型延迟重试队列不可用") from exc
                try:
                    await update_job(
                        review_id,
                        status=ReviewStatus.PENDING,
                        phase="model_retry_wait",
                        message=(
                            f"模型审核临时失败，{retry_plan.delay_seconds:.1f}秒后进行"
                            f"第{retry_plan.attempt}次任务级重试"
                        ),
                        error=error_text,
                    )
                    await add_event(
                        review_id,
                        "model_retry",
                        {
                            "attempt": retry_plan.attempt,
                            "delay_seconds": retry_plan.delay_seconds,
                            "deadline_at": retry_plan.deadline_at.isoformat(),
                            "error_kind": classify_model_error(exc),
                            "error": error_text,
                        },
                    )
                except Exception as state_exc:
                    store.add_event(
                        review_id,
                        "status",
                        {"text": f"模型重试已持久化，但任务状态更新失败：{state_exc}"},
                    )
                return
            failure_message = "模型审核超过30分钟截止时间"
            error_text = f"WORKFLOW_DEADLINE_EXCEEDED: {error_text}"
        else:
            failure_message = "模型审核失败"
        await fail_workflow(
            review_id,
            request,
            message=failure_message,
            error=error_text,
        )
