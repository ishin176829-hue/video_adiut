from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import redis.asyncio as redis_async
from redis.exceptions import ResponseError

from .config import settings
from .models import CreateReviewRequest, SegmentReviewResult


class ReviewQueueStage(StrEnum):
    SINGLE = "single"
    PREPROCESS = "preprocess"
    MODEL = "model"


_redis: redis_async.Redis | None = None
_disabled_until = 0.0
_last_error: str | None = None


@dataclass(frozen=True)
class ReviewQueueMessage:
    stream_id: str
    review_id: str
    request: CreateReviewRequest
    payload: dict[str, Any]
    stage: ReviewQueueStage = ReviewQueueStage.SINGLE
    stream: str = ""
    group: str = ""


@dataclass(frozen=True)
class GlobalReviewSlot:
    key: str
    member: str
    ttl_seconds: int
    enabled: bool


@dataclass(frozen=True)
class ModelConcurrencySlot:
    key: str
    member: str
    enabled: bool
    local: bool = False
    local_semaphore: asyncio.Semaphore | None = None


@dataclass(frozen=True)
class DownloadHostSlot:
    key: str
    member: str
    ttl_seconds: int
    enabled: bool


_GLOBAL_SLOT_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local member = ARGV[1]
local now_seconds = tonumber(ARGV[2])
local ttl_seconds = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_seconds)
if redis.call('ZSCORE', key, member) then
  redis.call('ZADD', key, now_seconds + ttl_seconds, member)
  redis.call('EXPIRE', key, ttl_seconds)
  return 1
end
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now_seconds + ttl_seconds, member)
  redis.call('EXPIRE', key, ttl_seconds)
  return 1
end
return 0
"""

_MODEL_CONCURRENCY_ACQUIRE_SCRIPT = """
local active_key = KEYS[1]
local circuit_key = KEYS[2]
local member = ARGV[1]
local now_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local base_limit = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', active_key, '-inf', now_ms)
local open_until_ms = tonumber(redis.call('HGET', circuit_key, 'open_until_ms') or '0')
if open_until_ms > now_ms then
  return {0, 'open', 0}
end
local mode = redis.call('HGET', circuit_key, 'mode') or 'closed'
local multiplier = tonumber(redis.call('HGET', circuit_key, 'multiplier') or '1')
local limit = 0
if mode == 'open' and open_until_ms <= now_ms then
  mode = 'half_open'
  multiplier = 1
  limit = 1
  redis.call('HSET', circuit_key, 'mode', mode, 'multiplier', multiplier, 'open_until_ms', 0)
elseif mode == 'half_open' then
  limit = 1
elseif multiplier <= 0 then
  return {0, mode, 0}
end
if limit == 0 then
  limit = math.floor(base_limit * multiplier)
  if limit < 1 then
    limit = 1
  end
end
if redis.call('ZSCORE', active_key, member) then
  redis.call('ZADD', active_key, now_ms + ttl_ms, member)
  redis.call('PEXPIRE', active_key, ttl_ms + 60000)
  return {1, mode, limit}
end
if redis.call('ZCARD', active_key) < limit then
  redis.call('ZADD', active_key, now_ms + ttl_ms, member)
  redis.call('PEXPIRE', active_key, ttl_ms + 60000)
  return {1, mode, limit}
end
return {0, mode, limit}
"""

_MODEL_QPM_RESERVE_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local interval_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local next_ms = tonumber(redis.call('GET', key) or '0')
local reserved_at_ms = math.max(now_ms, next_ms)
redis.call('SET', key, reserved_at_ms + interval_ms, 'PX', ttl_ms)
return reserved_at_ms
"""

_STAGE_RETRY_PROMOTE_SCRIPT = """
local retry_key = KEYS[1]
local target_stream = KEYS[2]
local now_seconds = tonumber(ARGV[1])
local count = tonumber(ARGV[2])
local stage = ARGV[3]
local entries = redis.call('ZRANGEBYSCORE', retry_key, '-inf', now_seconds, 'LIMIT', 0, count)
local promoted = 0
for _, raw in ipairs(entries) do
  local item = cjson.decode(raw)
  redis.call('XADD', target_stream, '*',
    'review_id', item.review_id,
    'payload', item.payload,
    'stage', stage,
    'enqueued_at', item.enqueued_at,
    'retry_attempt', tostring(item.attempt))
  if redis.call('ZREM', retry_key, raw) == 1 then
    promoted = promoted + 1
  end
end
return promoted
"""


_local_model_semaphore: asyncio.Semaphore | None = None
_local_model_semaphore_loop = None
_local_model_qpm_lock: asyncio.Lock | None = None
_local_model_qpm_lock_loop = None
_local_model_qpm_next_at = 0.0


def redis_last_error() -> str | None:
    return _last_error


async def get_redis(*, strict: bool = False) -> redis_async.Redis | None:
    global _redis, _disabled_until, _last_error
    if not settings.redis_url:
        return None
    if _redis is not None:
        return _redis
    if not strict and time.monotonic() < _disabled_until:
        return None
    try:
        _redis = redis_async.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout,
            socket_timeout=settings.redis_connect_timeout,
        )
        await _redis.ping()
        _last_error = None
        return _redis
    except Exception as exc:
        _last_error = str(exc) or exc.__class__.__name__
        _disabled_until = time.monotonic() + settings.infra_failure_backoff_seconds
        if _redis is not None:
            await _redis.aclose()
            _redis = None
        if strict:
            raise
        return None


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def init_review_queue(*, strict: bool = False) -> bool:
    client = await get_redis(strict=strict)
    if client is None:
        return False
    for stage in [ReviewQueueStage.SINGLE, ReviewQueueStage.PREPROCESS, ReviewQueueStage.MODEL]:
        stream, group = review_stream_group(stage)
        try:
            await client.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                if strict:
                    raise
                return False
    return True


def review_stream_group(stage: ReviewQueueStage | str | None = None) -> tuple[str, str]:
    normalized = ReviewQueueStage(stage or ReviewQueueStage.SINGLE)
    if normalized == ReviewQueueStage.PREPROCESS:
        return settings.redis_preprocess_stream, settings.redis_preprocess_group
    if normalized == ReviewQueueStage.MODEL:
        return settings.redis_model_stream, settings.redis_model_group
    return settings.redis_review_stream, settings.redis_review_group


async def enqueue_review(review_id: str, request: CreateReviewRequest) -> str | None:
    return await enqueue_review_stage(review_id, request, ReviewQueueStage.SINGLE)


async def enqueue_review_stage(
    review_id: str,
    request: CreateReviewRequest,
    stage: ReviewQueueStage | str,
) -> str | None:
    client = await get_redis()
    if client is None:
        return None
    normalized = ReviewQueueStage(stage)
    stream, _group = review_stream_group(normalized)
    payload = request.model_dump(mode="json", exclude_none=True)
    fields = {
        "review_id": review_id,
        "payload": json.dumps(payload, ensure_ascii=False),
        "stage": normalized.value,
        "enqueued_at": datetime.now().isoformat(),
    }
    stream_id = await client.xadd(stream, fields)
    return str(stream_id)


async def schedule_download_retry(
    review_id: str,
    request: CreateReviewRequest,
    *,
    delay_seconds: float,
    attempt: int,
) -> str | None:
    return await schedule_stage_retry(
        review_id,
        request,
        stage=ReviewQueueStage.PREPROCESS,
        delay_seconds=delay_seconds,
        attempt=attempt,
    )


def _stage_retry_key(stage: ReviewQueueStage) -> str:
    if stage == ReviewQueueStage.MODEL:
        return settings.redis_model_retry_key
    if stage == ReviewQueueStage.PREPROCESS:
        return settings.redis_download_retry_key
    raise ValueError(f"阶段 {stage.value} 不支持延迟重试")


async def schedule_stage_retry(
    review_id: str,
    request: CreateReviewRequest,
    *,
    stage: ReviewQueueStage | str,
    delay_seconds: float,
    attempt: int,
) -> str | None:
    client = await get_redis()
    if client is None:
        return None
    normalized = ReviewQueueStage(stage)
    retry_key = _stage_retry_key(normalized)
    enqueued_at = datetime.now().isoformat()
    entry = json.dumps(
        {
            "review_id": review_id,
            "payload": json.dumps(request.model_dump(mode="json", exclude_none=True), ensure_ascii=False),
            "enqueued_at": enqueued_at,
            "attempt": attempt,
            "stage": normalized.value,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    await client.zadd(retry_key, {entry: time.time() + max(0.0, delay_seconds)})
    await client.expire(retry_key, max(86_400, int(delay_seconds) + 86_400))
    return entry


async def promote_due_download_retries() -> int:
    return await promote_due_stage_retries(ReviewQueueStage.PREPROCESS)


async def promote_due_stage_retries(stage: ReviewQueueStage | str) -> int:
    client = await get_redis()
    if client is None:
        return 0
    normalized = ReviewQueueStage(stage)
    retry_key = _stage_retry_key(normalized)
    target_stream, _group = review_stream_group(normalized)
    promote_count = (
        settings.model_retry_promote_count
        if normalized == ReviewQueueStage.MODEL
        else settings.download_retry_promote_count
    )
    promoted = await client.eval(
        _STAGE_RETRY_PROMOTE_SCRIPT,
        2,
        retry_key,
        target_stream,
        time.time(),
        max(1, promote_count),
        normalized.value,
    )
    return int(promoted or 0)


async def dequeue_reviews(
    consumer_name: str,
    *,
    count: int = 1,
    stage: ReviewQueueStage | str | None = None,
) -> list[ReviewQueueMessage]:
    client = await get_redis(strict=True)
    assert client is not None
    normalized = ReviewQueueStage(stage or ReviewQueueStage.SINGLE)
    stream, group = review_stream_group(normalized)
    response = await client.xreadgroup(
        group,
        consumer_name,
        streams={stream: ">"},
        count=count,
        block=settings.redis_block_ms,
    )
    messages: list[ReviewQueueMessage] = []
    for _stream, entries in response:
        for stream_id, fields in entries:
            raw_payload = fields.get("payload") or "{}"
            payload = json.loads(raw_payload)
            review_id = fields.get("review_id") or payload.get("review_id")
            if not review_id:
                continue
            messages.append(
                ReviewQueueMessage(
                    stream_id=str(stream_id),
                    review_id=str(review_id),
                    request=CreateReviewRequest.model_validate(payload),
                    payload=payload,
                    stage=ReviewQueueStage(fields.get("stage") or normalized),
                    stream=str(_stream or stream),
                    group=group,
                )
            )
    return messages


def _parse_review_queue_entries(
    entries,
    *,
    stream: str,
    group: str,
    stage: ReviewQueueStage,
) -> list[ReviewQueueMessage]:
    messages: list[ReviewQueueMessage] = []
    for stream_id, fields in entries:
        raw_payload = fields.get("payload") or "{}"
        payload = json.loads(raw_payload)
        review_id = fields.get("review_id") or payload.get("review_id")
        if not review_id:
            continue
        messages.append(
            ReviewQueueMessage(
                stream_id=str(stream_id),
                review_id=str(review_id),
                request=CreateReviewRequest.model_validate(payload),
                payload=payload,
                stage=ReviewQueueStage(fields.get("stage") or stage),
                stream=stream,
                group=group,
            )
        )
    return messages


async def claim_stale_reviews(
    consumer_name: str,
    *,
    count: int = 1,
    stage: ReviewQueueStage | str | None = None,
) -> list[ReviewQueueMessage]:
    client = await get_redis(strict=True)
    assert client is not None
    normalized = ReviewQueueStage(stage or ReviewQueueStage.SINGLE)
    stream, group = review_stream_group(normalized)
    response = await client.xautoclaim(
        stream,
        group,
        consumer_name,
        settings.redis_pending_claim_min_idle_ms,
        "0-0",
        count=count,
    )
    if len(response) >= 2:
        entries = response[1]
    else:
        entries = []
    return _parse_review_queue_entries(entries, stream=stream, group=group, stage=normalized)


async def ack_review(message: ReviewQueueMessage) -> None:
    client = await get_redis()
    if client is None:
        return
    stream = message.stream or review_stream_group(message.stage)[0]
    group = message.group or review_stream_group(message.stage)[1]
    await client.xack(stream, group, message.stream_id)


async def renew_review_claim(message: ReviewQueueMessage, consumer_name: str) -> None:
    """Refresh the Redis pending idle timer while processing continues."""
    client = await get_redis()
    if client is None:
        return
    stream = message.stream or review_stream_group(message.stage)[0]
    group = message.group or review_stream_group(message.stage)[1]
    await client.xclaim(
        stream,
        group,
        consumer_name,
        min_idle_time=0,
        message_ids=[message.stream_id],
        retrycount=0,
        justid=True,
    )


async def dead_letter_review(message: ReviewQueueMessage, error: str) -> None:
    client = await get_redis()
    if client is None:
        return
    await client.xadd(
        settings.redis_dead_stream,
        {
            "review_id": message.review_id,
            "payload": json.dumps(message.payload, ensure_ascii=False),
            "source_stream_id": message.stream_id,
            "source_stream": message.stream or review_stream_group(message.stage)[0],
            "source_group": message.group or review_stream_group(message.stage)[1],
            "stage": message.stage.value,
            "error": error,
            "failed_at": datetime.now().isoformat(),
        },
    )
    await ack_review(message)


def _global_slot_member(review_id: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{review_id}"


def _download_host_member(host: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{host}:{time.time_ns()}"


async def acquire_download_host_slot(host: str) -> DownloadHostSlot:
    limit = max(0, settings.download_host_concurrency_limit)
    if limit <= 0:
        return DownloadHostSlot("", "", 0, enabled=False)
    client = await get_redis()
    if client is None:
        return DownloadHostSlot("", "", 0, enabled=False)
    key = f"{settings.redis_download_host_prefix}:{host.lower()}"
    member = _download_host_member(host)
    ttl_seconds = max(30, settings.download_host_slot_ttl_seconds)
    deadline = time.monotonic() + max(0, settings.download_host_wait_seconds)
    while True:
        acquired = await client.eval(
            _GLOBAL_SLOT_ACQUIRE_SCRIPT,
            1,
            key,
            member,
            time.time(),
            ttl_seconds,
            limit,
        )
        if int(acquired or 0) == 1:
            return DownloadHostSlot(key, member, ttl_seconds, enabled=True)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待下载域名并发槽位超时：host={host}, limit={limit}")
        await asyncio.sleep(max(0.05, settings.download_host_poll_seconds))


async def release_download_host_slot(slot: DownloadHostSlot) -> None:
    if not slot.enabled:
        return
    client = await get_redis()
    if client is not None:
        await client.zrem(slot.key, slot.member)


@asynccontextmanager
async def download_host_slot(host: str):
    slot = await acquire_download_host_slot(host)
    try:
        yield slot
    finally:
        await release_download_host_slot(slot)


async def acquire_global_review_slot(review_id: str) -> GlobalReviewSlot:
    limit = max(0, settings.global_active_limit)
    if limit <= 0:
        return GlobalReviewSlot("", "", 0, enabled=False)
    client = await get_redis()
    if client is None:
        return GlobalReviewSlot("", "", 0, enabled=False)

    key = settings.redis_global_active_key
    member = _global_slot_member(review_id)
    ttl_seconds = max(30, settings.global_active_ttl_seconds)
    poll_seconds = max(0.2, settings.global_active_poll_seconds)
    deadline = time.monotonic() + max(0, settings.global_active_wait_seconds)
    while True:
        now_seconds = time.time()
        acquired = await client.eval(
            _GLOBAL_SLOT_ACQUIRE_SCRIPT,
            1,
            key,
            member,
            now_seconds,
            ttl_seconds,
            limit,
        )
        if int(acquired or 0) == 1:
            return GlobalReviewSlot(key, member, ttl_seconds, enabled=True)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待全局审核并发槽位超时：limit={limit}")
        await asyncio.sleep(poll_seconds)


async def release_global_review_slot(slot: GlobalReviewSlot) -> None:
    if not slot.enabled:
        return
    client = await get_redis()
    if client is None:
        return
    await client.zrem(slot.key, slot.member)


def _model_active_member() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}"


def _model_health_event_member(*, success: bool, error_kind: str | None, now_ms: int) -> str:
    status = "ok" if success else "error"
    kind = error_kind or ("ok" if success else "unknown")
    return f"{now_ms}:{time.time_ns()}:{status}:{kind}"


def _count_model_health_errors(events: list[str]) -> int:
    return sum(1 for event in events if ":error:" in str(event))


_GLOBAL_CIRCUIT_ERROR_KINDS = {"rate_limit", "transient"}


async def record_model_call_result(
    *,
    success: bool,
    error_kind: str | None = None,
    api_key_id: str | None = None,
) -> None:
    if not settings.model_circuit_enabled:
        return
    client = await get_redis()
    if client is None:
        return
    now_ms = int(time.time() * 1000)
    window_ms = max(1, settings.model_health_window_seconds) * 1000
    health_key = settings.redis_model_health_key
    circuit_key = settings.redis_model_circuit_key
    state = await client.hgetall(circuit_key)
    current_mode = str(state.get("mode") or "closed") if state else "closed"
    current_open_until_ms = int(float(state.get("open_until_ms") or 0)) if state else 0
    error_kind = error_kind or ("ok" if success else "unknown")

    if current_mode == "half_open" and (success or error_kind not in _GLOBAL_CIRCUIT_ERROR_KINDS):
        await client.delete(health_key)
        await client.hset(
            circuit_key,
            mapping={
                "mode": "closed",
                "multiplier": "1.0",
                "open_until_ms": "0",
                "updated_at_ms": str(now_ms),
                "window_seconds": str(settings.model_health_window_seconds),
                "total": "0",
                "errors": "0",
                "error_rate": "0.000000",
            },
        )
        await client.expire(circuit_key, max(settings.model_health_window_seconds * 3, 120))
        return

    if not success and error_kind not in _GLOBAL_CIRCUIT_ERROR_KINDS:
        return

    member = _model_health_event_member(success=success, error_kind=error_kind, now_ms=now_ms)
    if api_key_id:
        member = f"{member}:key={api_key_id}"
    await client.zadd(health_key, {member: now_ms})
    await client.zremrangebyscore(health_key, "-inf", now_ms - window_ms)
    events = [str(event) for event in await client.zrange(health_key, 0, -1)]
    total = len(events)
    errors = _count_model_health_errors(events)
    error_rate = errors / total if total else 0.0
    min_requests = max(1, settings.model_circuit_min_requests)
    open_until_ms = 0
    mode = "closed"
    multiplier = 1.0
    if current_mode == "open" and current_open_until_ms > now_ms:
        mode = "open"
        multiplier = 0.0
        open_until_ms = current_open_until_ms
    elif current_mode == "half_open" and not success:
        mode = "open"
        multiplier = 0.0
        open_until_ms = now_ms + max(1, settings.model_circuit_open_seconds) * 1000
    elif total >= min_requests and error_rate >= settings.model_circuit_open_error_rate:
        mode = "open"
        multiplier = 0.0
        open_until_ms = now_ms + max(1, settings.model_circuit_open_seconds) * 1000
    elif total >= min_requests and error_rate >= settings.model_circuit_degraded_error_rate:
        mode = "degraded"
        multiplier = max(0.05, min(1.0, settings.model_circuit_degraded_multiplier))
    await client.hset(
        circuit_key,
        mapping={
            "mode": mode,
            "multiplier": str(multiplier),
            "open_until_ms": str(open_until_ms),
            "updated_at_ms": str(now_ms),
            "window_seconds": str(settings.model_health_window_seconds),
            "total": str(total),
            "errors": str(errors),
            "error_rate": f"{error_rate:.6f}",
        },
    )
    expire_seconds = max(settings.model_health_window_seconds * 3, settings.model_circuit_open_seconds * 2, 120)
    await client.expire(health_key, expire_seconds)
    await client.expire(circuit_key, expire_seconds)


async def _model_circuit_wait_seconds(client) -> float:
    if not settings.model_circuit_enabled:
        return 0.0
    state = await client.hgetall(settings.redis_model_circuit_key)
    now_ms = int(time.time() * 1000)
    open_until_ms = int(float(state.get("open_until_ms") or 0)) if state else 0
    return max(0.0, (open_until_ms - now_ms) / 1000)


async def _wait_for_model_circuit_recovery(client, deadline: float) -> bool:
    wait_seconds = await _model_circuit_wait_seconds(client)
    if wait_seconds <= 0:
        return False
    if time.monotonic() + wait_seconds > deadline:
        raise TimeoutError(f"模型熔断中，等待恢复超时（预计 {max(1, int(wait_seconds))} 秒）")
    await asyncio.sleep(wait_seconds)
    return True


async def acquire_model_concurrency_slot() -> ModelConcurrencySlot:
    if not settings.model_circuit_enabled or settings.model_concurrency_limit <= 0:
        return ModelConcurrencySlot("", "", enabled=False)
    client = await get_redis()
    if client is None:
        global _local_model_semaphore, _local_model_semaphore_loop
        loop = asyncio.get_running_loop()
        if _local_model_semaphore is None or _local_model_semaphore_loop is not loop:
            _local_model_semaphore = asyncio.Semaphore(
                max(1, settings.model_local_concurrency_limit or settings.model_concurrency_limit)
            )
            _local_model_semaphore_loop = loop
        try:
            await asyncio.wait_for(
                _local_model_semaphore.acquire(),
                timeout=max(0.0, settings.model_concurrency_wait_seconds),
            )
        except TimeoutError as exc:
            raise TimeoutError("本地模型并发槽位已满，等待超时") from exc
        return ModelConcurrencySlot("", "", enabled=True, local=True, local_semaphore=_local_model_semaphore)
    member = _model_active_member()
    ttl_ms = max(30, settings.model_concurrency_ttl_seconds) * 1000
    poll_seconds = 0.1
    deadline = time.monotonic() + max(0, settings.model_concurrency_wait_seconds)
    while True:
        if await _wait_for_model_circuit_recovery(client, deadline):
            continue
        now_ms = int(time.time() * 1000)
        result = await client.eval(
            _MODEL_CONCURRENCY_ACQUIRE_SCRIPT,
            2,
            settings.redis_model_active_key,
            settings.redis_model_circuit_key,
            member,
            now_ms,
            ttl_ms,
            max(1, settings.model_concurrency_limit),
        )
        acquired = int(result[0] if isinstance(result, (list, tuple)) else result or 0)
        reason = str(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else ""
        effective_limit = int(result[2]) if isinstance(result, (list, tuple)) and len(result) > 2 else 0
        if acquired == 1:
            return ModelConcurrencySlot(settings.redis_model_active_key, member, enabled=True)
        if reason == "open":
            if await _wait_for_model_circuit_recovery(client, deadline):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("模型熔断中，等待恢复超时")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待模型并发槽位超时：limit={effective_limit or settings.model_concurrency_limit}")
        await asyncio.sleep(poll_seconds)


async def release_model_concurrency_slot(slot: ModelConcurrencySlot) -> None:
    if not slot.enabled:
        return
    if slot.local:
        if slot.local_semaphore is not None:
            slot.local_semaphore.release()
        return
    client = await get_redis()
    if client is None:
        return
    await client.zrem(slot.key, slot.member)


async def acquire_model_qpm_token() -> None:
    limit = max(0, settings.model_qpm_limit)
    if limit <= 0:
        return
    client = await get_redis()
    if client is None:
        global _local_model_qpm_lock, _local_model_qpm_lock_loop, _local_model_qpm_next_at
        loop = asyncio.get_running_loop()
        if _local_model_qpm_lock is None or _local_model_qpm_lock_loop is not loop:
            _local_model_qpm_lock = asyncio.Lock()
            _local_model_qpm_lock_loop = loop
            _local_model_qpm_next_at = 0.0
        interval_seconds = 60.0 / max(1, limit)
        async with _local_model_qpm_lock:
            now = time.monotonic()
            reserved_at = max(now, _local_model_qpm_next_at)
            _local_model_qpm_next_at = reserved_at + interval_seconds
        wait_seconds = max(0.0, reserved_at - now)
        if wait_seconds > max(0.0, settings.model_qpm_wait_seconds):
            raise TimeoutError(f"本地模型 QPM 令牌等待超时：limit={limit}/min")
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
        return
    deadline = time.monotonic() + max(0, settings.model_qpm_wait_seconds)
    interval_ms = max(1, (60_000 + limit - 1) // limit)
    ttl_ms = max(120_000, interval_ms * limit * 2)
    while True:
        if await _wait_for_model_circuit_recovery(client, deadline):
            continue
        now_ms = int(time.time() * 1000)
        reserved_at_ms = int(await client.eval(
            _MODEL_QPM_RESERVE_SCRIPT,
            1,
            settings.redis_model_qpm_key,
            now_ms,
            interval_ms,
            ttl_ms,
        ) or now_ms)
        wait_seconds = max(0.0, (reserved_at_ms - now_ms) / 1000)
        if wait_seconds <= 0:
            return
        if time.monotonic() + wait_seconds > deadline:
            raise TimeoutError(f"等待模型 QPM 令牌超时：limit={limit}/min")
        await asyncio.sleep(wait_seconds)
        return


async def _renew_global_review_slot(slot: GlobalReviewSlot) -> None:
    if not slot.enabled:
        return
    interval = max(5, slot.ttl_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        client = await get_redis()
        if client is None:
            continue
        expires_at = time.time() + slot.ttl_seconds
        await client.zadd(slot.key, {slot.member: expires_at})
        await client.expire(slot.key, slot.ttl_seconds)


@asynccontextmanager
async def global_review_slot(review_id: str):
    slot = await acquire_global_review_slot(review_id)
    renew_task: asyncio.Task | None = None
    if slot.enabled:
        renew_task = asyncio.create_task(_renew_global_review_slot(slot))
    try:
        yield slot
    finally:
        if renew_task is not None:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
        await release_global_review_slot(slot)


@asynccontextmanager
async def model_qpm_slot():
    await acquire_model_qpm_token()
    slot = await acquire_model_concurrency_slot()
    try:
        yield
    finally:
        await release_model_concurrency_slot(slot)


def frame_batch_cache_key(
    *,
    video_sha256: str,
    policy_version: str,
    model: str,
    fps: int,
    frames: list[dict[str, Any]],
) -> str:
    frame_fingerprint = [
        {
            "index": frame.get("frame_index"),
            "ts": frame.get("timestamp_seconds"),
        }
        for frame in frames
    ]
    raw = json.dumps(
        {
            "video_sha256": video_sha256,
            "policy_version": policy_version,
            "model": model,
            "fps": fps,
            "frames": frame_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{settings.redis_cache_prefix}:frame_batch:{digest}"


async def get_frame_batch_cache(cache_key: str) -> SegmentReviewResult | None:
    if not settings.redis_cache_enabled:
        return None
    client = await get_redis()
    if client is None:
        return None
    raw = await client.get(cache_key)
    if not raw:
        return None
    return SegmentReviewResult.model_validate_json(raw)


async def set_frame_batch_cache(cache_key: str, result: SegmentReviewResult) -> None:
    if not settings.redis_cache_enabled:
        return
    client = await get_redis()
    if client is None:
        return
    await client.set(cache_key, result.model_dump_json(), ex=settings.redis_cache_ttl_seconds)


def default_consumer_name() -> str:
    return f"{socket.gethostname()}-{time.time_ns()}"
