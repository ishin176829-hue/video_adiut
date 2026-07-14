import asyncio
import json
from dataclasses import dataclass

from video_review.model_retry import ModelRetryBudget, call_model_with_retry, classify_model_error


@dataclass
class ResponseWithRetryAfter:
    status_code: int = 429
    headers: dict[str, str] | None = None

    def __post_init__(self):
        if self.headers is None:
            self.headers = {"Retry-After": "7"}


class RateLimitError(RuntimeError):
    def __init__(self):
        super().__init__("upstream rate limited")
        self.status_code = 429
        self.response = ResponseWithRetryAfter()


def test_classify_model_error_marks_json_decode_as_parse_error():
    exc = json.JSONDecodeError("Expecting value", "", 0)

    assert classify_model_error(exc) == "parse"


def test_call_model_with_retry_retries_parse_error_once(monkeypatch):
    async def scenario():
        sleeps = []
        attempts = 0
        retry_events = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async def flaky_call():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return {"ok": True}

        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)

        result = await call_model_with_retry(
            flaky_call,
            label="json-test",
            parse_attempts=2,
            transient_attempts=1,
            rate_limit_attempts=1,
            base_backoff_seconds=0.2,
            jitter_seconds=0,
            on_retry=lambda event: retry_events.append(event),
        )

        assert result == {"ok": True}
        assert attempts == 2
        assert sleeps == [0.2]
        assert retry_events[0]["error_kind"] == "parse"
        assert retry_events[0]["attempt"] == 1
        assert retry_events[0]["next_attempt"] == 2

    asyncio.run(scenario())


def test_call_model_with_retry_uses_shared_retry_budget(monkeypatch):
    async def scenario():
        attempts = 0
        budget = ModelRetryBudget(max_extra_attempts=1)

        async def fake_sleep(seconds):
            return None

        async def always_timeout():
            nonlocal attempts
            attempts += 1
            raise TimeoutError("timed out")

        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)

        try:
            await call_model_with_retry(
                always_timeout,
                label="budget-test",
                retry_budget=budget,
                transient_attempts=3,
                parse_attempts=1,
                rate_limit_attempts=1,
                base_backoff_seconds=0.1,
                jitter_seconds=0,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected timeout")

        assert attempts == 2
        assert budget.extra_attempts_used == 1

    asyncio.run(scenario())


def test_call_model_with_retry_records_attempt_results(monkeypatch):
    async def scenario():
        attempts = 0
        attempt_results = []

        async def fake_sleep(seconds):
            return None

        async def flaky_call():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("503 temporarily unavailable")
            return {"ok": True}

        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)

        result = await call_model_with_retry(
            flaky_call,
            transient_attempts=2,
            parse_attempts=1,
            rate_limit_attempts=1,
            base_backoff_seconds=0.1,
            jitter_seconds=0,
            on_attempt_result=lambda event: attempt_results.append(event),
        )

        assert result == {"ok": True}
        assert [event["success"] for event in attempt_results] == [False, True]
        assert attempt_results[0]["error_kind"] == "transient"
        assert attempt_results[1]["error_kind"] == "ok"

    asyncio.run(scenario())


def test_call_model_with_retry_does_not_record_local_circuit_as_model_failure(monkeypatch):
    async def scenario():
        attempt_results = []

        async def fake_sleep(seconds):
            return None

        async def blocked_call():
            raise TimeoutError("模型熔断中，等待恢复超时")

        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)

        try:
            await call_model_with_retry(
                blocked_call,
                transient_attempts=1,
                parse_attempts=1,
                rate_limit_attempts=1,
                on_attempt_result=lambda event: attempt_results.append(event),
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected circuit timeout")

        assert classify_model_error(TimeoutError("模型熔断中，等待恢复超时")) == "circuit"
        assert attempt_results == []

    asyncio.run(scenario())


def test_model_retry_honors_retry_after_header(monkeypatch):
    async def scenario():
        calls = 0
        sleeps = []

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimitError()
            return "ok"

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)
        result = await call_model_with_retry(
            operation,
            rate_limit_attempts=2,
            rate_limit_backoff_seconds=1,
            jitter_seconds=0,
        )

        assert result == "ok"
        assert calls == 2
        assert sleeps == [7]

    asyncio.run(scenario())


def test_model_retry_does_not_retry_permission_error():
    async def scenario():
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            error = RuntimeError("permission denied")
            error.status_code = 403
            raise error

        try:
            await call_model_with_retry(operation, rate_limit_attempts=3)
        except RuntimeError as exc:
            assert "permission denied" in str(exc)
        else:
            raise AssertionError("expected permission error")

        assert calls == 1

    asyncio.run(scenario())
