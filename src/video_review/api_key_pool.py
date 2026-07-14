from __future__ import annotations

import asyncio
import hashlib
import socket
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from .config import settings


current_google_api_key_id: ContextVar[str | None] = ContextVar(
    "current_google_api_key_id",
    default=None,
)


def api_key_fingerprint(api_key: str) -> str:
    return f"google_{hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:12]}"


@dataclass(frozen=True)
class GoogleApiKeyLease:
    api_key: str
    key_id: str
    lease_id: str | None = None
    scheduler_index: int = 0


_KEY_POOL_ACQUIRE_SCRIPT = """
local count = tonumber(ARGV[1])
local now_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local lease_id = ARGV[4]
local best_index = 0
local best_active = 999999999
local best_last_selected = 999999999999999999
local best_probe = 1

for index = 1, count do
  local active_key = KEYS[index]
  local state_key = KEYS[count + index]
  redis.call('ZREMRANGEBYSCORE', active_key, '-inf', now_ms)
  local cooldown_until = tonumber(redis.call('HGET', state_key, 'cooldown_until_ms') or '0')
  local mode = redis.call('HGET', state_key, 'mode') or 'healthy'
  local probe_inflight = tonumber(redis.call('HGET', state_key, 'probe_inflight') or '0')
  local active_count = redis.call('ZCARD', active_key)
  local last_selected = tonumber(redis.call('HGET', state_key, 'last_selected_ms') or '0')
  local is_probe = 0
  if (mode == 'cooldown' or mode == 'disabled') and cooldown_until <= now_ms then
    if probe_inflight == 0 then
      is_probe = 1
    else
      active_count = tonumber(ARGV[5]) + 1
    end
  end
  if cooldown_until <= now_ms and active_count < tonumber(ARGV[5]) then
    if is_probe < best_probe or (is_probe == best_probe and (active_count < best_active or (active_count == best_active and last_selected < best_last_selected))) then
      best_index = index
      best_active = active_count
      best_last_selected = last_selected
      best_probe = is_probe
    end
  end
end

if best_index == 0 then
  return {0, 'busy'}
end

local chosen_active_key = KEYS[best_index]
local chosen_state_key = KEYS[count + best_index]
redis.call('ZADD', chosen_active_key, now_ms + ttl_ms, lease_id)
redis.call('PEXPIRE', chosen_active_key, ttl_ms + 60000)
if best_probe == 1 then
  redis.call('HSET', chosen_state_key, 'last_selected_ms', now_ms, 'mode', 'half_open', 'probe_inflight', 1)
else
  redis.call('HSET', chosen_state_key, 'last_selected_ms', now_ms, 'probe_inflight', 0)
end
redis.call('EXPIRE', chosen_state_key, 86400)
return {best_index, lease_id}
"""

_KEY_POOL_RELEASE_SCRIPT = """
redis.call('ZREM', KEYS[1], ARGV[1])
return 1
"""

_KEY_POOL_REPORT_SCRIPT = """
local state_key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local error_kind = ARGV[2]
local threshold = tonumber(ARGV[3])
local cooldown_ms = tonumber(ARGV[4])

if error_kind == 'ok' then
  redis.call('HINCRBY', state_key, 'total_requests', 1)
  redis.call('HSET', state_key, 'consecutive_failures', 0, 'mode', 'healthy', 'cooldown_until_ms', 0, 'probe_inflight', 0)
else
  redis.call('HINCRBY', state_key, 'total_requests', 1)
  redis.call('HINCRBY', state_key, 'total_errors', 1)
  local failures = redis.call('HINCRBY', state_key, 'consecutive_failures', 1)
  if error_kind == 'auth' then
    redis.call('HSET', state_key, 'mode', 'disabled', 'cooldown_until_ms', now_ms + cooldown_ms * 10, 'probe_inflight', 0)
  elseif error_kind == 'rate_limit' or failures >= threshold then
    redis.call('HSET', state_key, 'mode', 'cooldown', 'cooldown_until_ms', now_ms + cooldown_ms, 'probe_inflight', 0)
  else
    redis.call('HSET', state_key, 'mode', 'degraded', 'probe_inflight', 0)
  end
end
redis.call('EXPIRE', state_key, 86400)
return 1
"""


class GoogleApiKeyPool:
    def __init__(
        self,
        api_keys: list[str],
        *,
        redis_getter: Callable[[], Awaitable[object | None]] | None = None,
        key_concurrency_limit: int | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> None:
        unique_keys = list(dict.fromkeys(key.strip() for key in api_keys if key and key.strip()))
        if not unique_keys:
            raise RuntimeError("未配置 GOOGLE_API_KEY 或 GOOGLE_API_KEYS，无法调用多模态审核 API")
        self._api_keys = unique_keys
        self._index = 0
        self._lock = asyncio.Lock()
        self._redis_getter = redis_getter
        configured_limit = settings.model_key_concurrency_limit if key_concurrency_limit is None else key_concurrency_limit
        derived_limit = (
            (settings.model_concurrency_limit + len(unique_keys) - 1) // len(unique_keys)
            if settings.model_concurrency_limit > 0
            else 1
        )
        self._key_concurrency_limit = max(1, configured_limit or derived_limit)
        self._lease_ttl_seconds = max(
            30,
            lease_ttl_seconds if lease_ttl_seconds is not None else settings.model_concurrency_ttl_seconds,
        )
        self._local_active = [0 for _ in unique_keys]
        self._local_cooldown_until = [0.0 for _ in unique_keys]

    async def _get_redis(self):
        if self._redis_getter is not None:
            return await self._redis_getter()
        from .queue import get_redis

        return await get_redis()

    def _active_key(self, key_id: str) -> str:
        return f"{settings.redis_model_key_pool_prefix}:active:{key_id}"

    def _state_key(self, key_id: str) -> str:
        return f"{settings.redis_model_key_pool_prefix}:state:{key_id}"

    def _new_lease_id(self) -> str:
        return f"{socket.gethostname()}:{__import__('os').getpid()}:{uuid.uuid4().hex}"

    async def _acquire_redis(self, client) -> GoogleApiKeyLease | None:
        now_ms = int(time.time() * 1000)
        lease_id = self._new_lease_id()
        key_ids = [api_key_fingerprint(key) for key in self._api_keys]
        active_keys = [self._active_key(key_id) for key_id in key_ids]
        state_keys = [self._state_key(key_id) for key_id in key_ids]
        result = await client.eval(
            _KEY_POOL_ACQUIRE_SCRIPT,
            len(active_keys) + len(state_keys),
            *(active_keys + state_keys),
            len(key_ids),
            now_ms,
            self._lease_ttl_seconds * 1000,
            lease_id,
            self._key_concurrency_limit,
        )
        if not isinstance(result, (list, tuple)) or not result:
            return None
        index = int(result[0] or 0)
        if index <= 0 or index > len(self._api_keys):
            return None
        lease = GoogleApiKeyLease(
            api_key=self._api_keys[index - 1],
            key_id=key_ids[index - 1],
            lease_id=str(result[1] or lease_id) if len(result) > 1 else lease_id,
            scheduler_index=index - 1,
        )
        current_google_api_key_id.set(lease.key_id)
        return lease

    async def _acquire_local(self) -> GoogleApiKeyLease:
        async with self._lock:
            now = time.monotonic()
            candidates = [
                index
                for index, cooldown_until in enumerate(self._local_cooldown_until)
                if cooldown_until <= now and self._local_active[index] < self._key_concurrency_limit
            ]
            if not candidates:
                raise TimeoutError("所有模型 API Key 均处于冷却或并发已满")
            index = min(candidates, key=lambda item: (self._local_active[item], item))
            self._local_active[index] += 1
            lease = GoogleApiKeyLease(
                api_key=self._api_keys[index],
                key_id=api_key_fingerprint(self._api_keys[index]),
                scheduler_index=index,
            )
            current_google_api_key_id.set(lease.key_id)
            return lease

    async def acquire(self) -> GoogleApiKeyLease:
        client = await self._get_redis()
        if client is None:
            return await self._acquire_local()
        deadline = time.monotonic() + max(1.0, settings.model_concurrency_wait_seconds)
        while True:
            lease = await self._acquire_redis(client)
            if lease is not None:
                return lease
            if time.monotonic() >= deadline:
                raise TimeoutError("模型 API Key 池无可用 Key，等待并发或冷却恢复超时")
            await asyncio.sleep(0.1)

    async def release(
        self,
        lease: GoogleApiKeyLease,
        *,
        success: bool,
        error_kind: str | None = None,
    ) -> None:
        client = await self._get_redis()
        if client is None:
            async with self._lock:
                if 0 <= lease.scheduler_index < len(self._local_active):
                    self._local_active[lease.scheduler_index] = max(
                        0,
                        self._local_active[lease.scheduler_index] - 1,
                    )
                    if not success and error_kind in {"auth", "rate_limit"}:
                        cooldown = settings.model_key_cooldown_seconds
                        self._local_cooldown_until[lease.scheduler_index] = (
                            time.monotonic() + max(1, cooldown)
                        )
            return
        try:
            if lease.lease_id:
                await client.eval(
                    _KEY_POOL_RELEASE_SCRIPT,
                    1,
                    self._active_key(lease.key_id),
                    lease.lease_id,
                )
            await client.eval(
                _KEY_POOL_REPORT_SCRIPT,
                1,
                self._state_key(lease.key_id),
                int(time.time() * 1000),
                "ok" if success else (error_kind or "other"),
                max(1, settings.model_key_failure_threshold),
                max(1, settings.model_key_cooldown_seconds) * 1000,
            )
        except Exception:
            # Model results must not be retried solely because telemetry release failed.
            return


_pool: GoogleApiKeyPool | None = None
_pool_keys: tuple[str, ...] = ()


def get_google_api_key_pool() -> GoogleApiKeyPool:
    global _pool, _pool_keys
    keys = tuple(settings.google_api_key_pool)
    if _pool is None or keys != _pool_keys:
        _pool = GoogleApiKeyPool(list(keys))
        _pool_keys = keys
    return _pool
