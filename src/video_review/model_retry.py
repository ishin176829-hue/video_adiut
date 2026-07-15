from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import ValidationError

from .config import settings


T = TypeVar("T")


class ModelContractError(ValueError):
    def __init__(self, message: str, *, kind: str = "validation") -> None:
        super().__init__(message)
        self.kind = kind

RATE_LIMIT_ERROR_TOKENS = (
    "429",
    "resource_exhausted",
    "rate limit",
    "too many requests",
)

TRANSIENT_MODEL_ERROR_TOKENS = (
    "504",
    "502",
    "503",
    "gateway time-out",
    "gateway timeout",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "server disconnected",
)


def _status_code(exc: BaseException) -> int | None:
    candidates = [exc, getattr(exc, "response", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        for name in ("status_code", "status", "http_status"):
            value = getattr(candidate, name, None)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    candidates = [exc, getattr(exc, "response", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        value = getattr(candidate, "retry_after", None)
        headers = getattr(candidate, "headers", None)
        if headers:
            value = headers.get("Retry-After") or headers.get("retry-after") or value
        try:
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return None


@dataclass
class ModelRetryBudget:
    max_extra_attempts: int
    extra_attempts_used: int = 0

    def __post_init__(self) -> None:
        self.max_extra_attempts = max(0, int(self.max_extra_attempts))
        self._lock = asyncio.Lock()

    async def try_consume(self) -> bool:
        async with self._lock:
            if self.extra_attempts_used >= self.max_extra_attempts:
                return False
            self.extra_attempts_used += 1
            return True


def classify_model_error(exc: BaseException) -> str:
    if isinstance(exc, ModelContractError):
        return exc.kind
    status_code = _status_code(exc)
    if status_code in {401, 403}:
        return "auth"
    if status_code in {400, 404, 422}:
        return "permanent"
    if status_code == 429:
        return "rate_limit"
    if status_code in {500, 502, 503, 504}:
        return "transient"
    if isinstance(exc, json.JSONDecodeError):
        return "parse"
    if isinstance(exc, ValidationError):
        return "validation"
    if isinstance(exc, TimeoutError):
        text = str(exc).lower()
        if "模型熔断" in text or "模型并发槽位" in text or "模型 qpm 令牌" in text:
            return "circuit"
        return "transient"
    text = str(exc).lower()
    if "模型熔断" in text or "模型并发槽位" in text or "模型 qpm 令牌" in text:
        return "circuit"
    if any(token in text for token in RATE_LIMIT_ERROR_TOKENS):
        return "rate_limit"
    if any(token in text for token in TRANSIENT_MODEL_ERROR_TOKENS):
        return "transient"
    if "jsondecodeerror" in text or "expecting value" in text or "invalid json" in text:
        return "parse"
    if "validation error" in text or "field required" in text or "input should" in text:
        return "validation"
    return "other"


def is_transient_model_error(exc: BaseException) -> bool:
    return classify_model_error(exc) == "transient"


def is_rate_limit_error(exc: BaseException) -> bool:
    return classify_model_error(exc) == "rate_limit"


def is_retryable_model_error(exc: BaseException) -> bool:
    return classify_model_error(exc) in {"rate_limit", "transient", "parse", "validation", "circuit"}


def _attempt_limit_for_kind(
    kind: str,
    *,
    parse_attempts: int,
    transient_attempts: int,
    rate_limit_attempts: int,
) -> int:
    if kind in {"parse", "validation"}:
        return max(1, parse_attempts)
    if kind == "rate_limit":
        return max(1, rate_limit_attempts)
    if kind == "transient":
        return max(1, transient_attempts)
    if kind == "circuit":
        return max(1, transient_attempts)
    return 1


async def _maybe_call(callback: Callable[[dict[str, Any]], Any] | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(event)
    if isinstance(result, Awaitable):
        await result


def _retry_delay(
    kind: str,
    attempt: int,
    *,
    base_backoff_seconds: float,
    rate_limit_backoff_seconds: float,
    jitter_seconds: float,
    retry_after: float | None,
) -> float:
    base = rate_limit_backoff_seconds if kind == "rate_limit" else base_backoff_seconds
    delay = max(0.0, base) * (2 ** max(0, attempt - 1))
    if retry_after is not None:
        delay = max(delay, retry_after)
    if jitter_seconds > 0:
        delay += random.uniform(0, jitter_seconds)
    return delay


async def call_model_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    label: str = "model_call",
    timeout_seconds: float | None = None,
    qpm_slot_factory: Callable[[], Any] | None = None,
    retry_budget: ModelRetryBudget | None = None,
    on_retry: Callable[[dict[str, Any]], Any] | None = None,
    on_attempt_result: Callable[[dict[str, Any]], Any] | None = None,
    parse_attempts: int | None = None,
    transient_attempts: int | None = None,
    rate_limit_attempts: int | None = None,
    base_backoff_seconds: float | None = None,
    rate_limit_backoff_seconds: float | None = None,
    jitter_seconds: float | None = None,
) -> T:
    parse_attempts = settings.model_parse_retry_attempts if parse_attempts is None else parse_attempts
    transient_attempts = settings.model_transient_retry_attempts if transient_attempts is None else transient_attempts
    rate_limit_attempts = (
        settings.model_rate_limit_retry_attempts if rate_limit_attempts is None else rate_limit_attempts
    )
    base_backoff_seconds = (
        settings.model_call_retry_delay_seconds if base_backoff_seconds is None else base_backoff_seconds
    )
    rate_limit_backoff_seconds = (
        settings.model_rate_limit_backoff_seconds
        if rate_limit_backoff_seconds is None
        else rate_limit_backoff_seconds
    )
    jitter_seconds = settings.model_retry_jitter_seconds if jitter_seconds is None else jitter_seconds
    timeout_seconds = max(30, settings.model_call_timeout_seconds) if timeout_seconds is None else timeout_seconds

    attempt = 1
    while True:
        try:
            if qpm_slot_factory is not None:
                async with qpm_slot_factory():
                    result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
                    await _maybe_call(
                        on_attempt_result,
                        {"success": True, "error_kind": "ok", "attempt": attempt, "label": label},
                    )
                    return result
            result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
            await _maybe_call(
                on_attempt_result,
                {"success": True, "error_kind": "ok", "attempt": attempt, "label": label},
            )
            return result
        except Exception as exc:
            kind = classify_model_error(exc)
            retry_after = retry_after_seconds(exc)
            if kind != "circuit":
                await _maybe_call(
                    on_attempt_result,
                    {
                        "success": False,
                        "error_kind": kind,
                        "attempt": attempt,
                        "label": label,
                        "error": str(exc) or exc.__class__.__name__,
                    },
                )
            max_attempts = _attempt_limit_for_kind(
                kind,
                parse_attempts=parse_attempts,
                transient_attempts=transient_attempts,
                rate_limit_attempts=rate_limit_attempts,
            )
            if attempt >= max_attempts or kind == "other":
                raise
            if retry_budget is not None and not await retry_budget.try_consume():
                raise
            delay = _retry_delay(
                kind,
                attempt,
                base_backoff_seconds=base_backoff_seconds,
                rate_limit_backoff_seconds=rate_limit_backoff_seconds,
                jitter_seconds=jitter_seconds,
                retry_after=retry_after,
            )
            event = {
                "label": label,
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": max_attempts,
                "error_kind": kind,
                "error": str(exc) or exc.__class__.__name__,
                "delay_seconds": delay,
                "retry_after_seconds": retry_after,
            }
            await _maybe_call(on_retry, event)
            if delay > 0:
                await asyncio.sleep(delay)
            attempt += 1
