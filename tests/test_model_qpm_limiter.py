import asyncio

from video_review import queue
from video_review.config import settings


def test_model_qpm_slot_acquires_redis_token(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.eval_args = None

        async def eval(self, script, key_count, key, now_ms, interval_ms, ttl_ms):
            self.eval_args = {
                "key": key,
                "interval_ms": interval_ms,
                "ttl_ms": ttl_ms,
            }
            return now_ms

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "model_qpm_limit", 500)
        monkeypatch.setattr(settings, "model_qpm_wait_seconds", 1)
        monkeypatch.setattr(settings, "model_circuit_enabled", False)
        monkeypatch.setattr(settings, "redis_model_qpm_key", "model:qpm")

        async with queue.model_qpm_slot():
            pass

        assert fake.eval_args["key"] == "model:qpm"
        assert fake.eval_args["interval_ms"] == 120
        assert fake.eval_args["ttl_ms"] >= 120_000

    asyncio.run(scenario())


def test_model_qpm_slot_paces_reserved_slots_instead_of_bursting(monkeypatch):
    class FakeRedis:
        async def eval(self, script, key_count, key, now_ms, interval_ms, ttl_ms):
            assert key == "model:qpm"
            assert interval_ms == 120
            return now_ms + 240

    async def scenario():
        sleeps = []

        async def fake_get_redis(*, strict=False):
            return FakeRedis()

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(queue.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(settings, "model_qpm_limit", 500)
        monkeypatch.setattr(settings, "model_qpm_wait_seconds", 1)
        monkeypatch.setattr(settings, "model_circuit_enabled", False)
        monkeypatch.setattr(settings, "redis_model_qpm_key", "model:qpm")

        await queue.acquire_model_qpm_token()

        assert sleeps == [0.24]

    asyncio.run(scenario())


def test_model_qpm_slot_waits_for_open_circuit_then_retries(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.circuit_checks = 0

        async def hgetall(self, key):
            self.circuit_checks += 1
            if self.circuit_checks == 1:
                return {"open_until_ms": str(int(queue.time.time() * 1000) + 250)}
            return {"open_until_ms": "0"}

        async def eval(self, script, key_count, key, now_ms, interval_ms, ttl_ms):
            return now_ms

    async def scenario():
        fake = FakeRedis()
        sleeps = []

        async def fake_get_redis(*, strict=False):
            return fake

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(queue.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(settings, "model_qpm_limit", 100)
        monkeypatch.setattr(settings, "model_qpm_wait_seconds", 1)
        monkeypatch.setattr(settings, "model_circuit_enabled", True)
        monkeypatch.setattr(settings, "redis_model_qpm_key", "model:qpm")
        monkeypatch.setattr(settings, "redis_model_circuit_key", "model:circuit")

        await queue.acquire_model_qpm_token()

        assert sleeps == [0.25]

    asyncio.run(scenario())


def test_model_qpm_slot_is_noop_when_limit_disabled(monkeypatch):
    async def scenario():
        async def fake_get_redis(*, strict=False):
            raise AssertionError("redis should not be used when limit is disabled")

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "model_qpm_limit", 0)
        monkeypatch.setattr(settings, "model_circuit_enabled", False)

        async with queue.model_qpm_slot():
            pass

    asyncio.run(scenario())


def test_model_concurrency_uses_local_fallback_when_redis_is_unavailable(monkeypatch):
    async def scenario():
        async def fake_get_redis(*, strict=False):
            return None

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "model_circuit_enabled", True)
        monkeypatch.setattr(settings, "model_concurrency_limit", 1)
        monkeypatch.setattr(settings, "model_concurrency_wait_seconds", 0.1)
        monkeypatch.setattr(settings, "model_local_concurrency_limit", 1)

        slot = await queue.acquire_model_concurrency_slot()
        assert slot.enabled is True
        assert slot.local is True
        await queue.release_model_concurrency_slot(slot)

    asyncio.run(scenario())


def test_record_model_call_result_opens_circuit_on_high_error_rate(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.events = []
            self.state = {}

        async def zadd(self, key, mapping):
            self.events.extend(mapping.keys())

        async def zremrangebyscore(self, key, minimum, maximum):
            return 0

        async def zrange(self, key, start, end):
            return self.events

        async def hset(self, key, mapping):
            self.state.update(mapping)

        async def expire(self, key, seconds):
            return True

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "model_circuit_enabled", True)
        monkeypatch.setattr(settings, "model_circuit_min_requests", 3)
        monkeypatch.setattr(settings, "model_circuit_degraded_error_rate", 0.25)
        monkeypatch.setattr(settings, "model_circuit_open_error_rate", 0.5)
        monkeypatch.setattr(settings, "model_circuit_open_seconds", 30)
        monkeypatch.setattr(settings, "redis_model_health_key", "model:health")
        monkeypatch.setattr(settings, "redis_model_circuit_key", "model:circuit")

        await queue.record_model_call_result(success=True)
        await queue.record_model_call_result(success=False, error_kind="transient")
        await queue.record_model_call_result(success=False, error_kind="parse")

        assert fake.state["mode"] == "open"
        assert float(fake.state["error_rate"]) >= 0.5
        assert int(fake.state["open_until_ms"]) > int(fake.state["updated_at_ms"])

    asyncio.run(scenario())


def test_model_qpm_slot_rejects_when_circuit_is_open(monkeypatch):
    class FakeRedis:
        async def hgetall(self, key):
            return {
                "mode": "open",
                "open_until_ms": str(int(queue.time.time() * 1000) + 60_000),
            }

    async def scenario():
        async def fake_get_redis(*, strict=False):
            return FakeRedis()

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "model_circuit_enabled", True)
        monkeypatch.setattr(settings, "model_qpm_limit", 0)
        monkeypatch.setattr(settings, "model_concurrency_limit", 10)
        monkeypatch.setattr(settings, "model_concurrency_wait_seconds", 0)
        monkeypatch.setattr(settings, "redis_model_circuit_key", "model:circuit")

        try:
            async with queue.model_qpm_slot():
                raise AssertionError("slot should not be granted")
        except TimeoutError as exc:
            assert "模型熔断中" in str(exc)
        else:
            raise AssertionError("expected circuit timeout")

    asyncio.run(scenario())


def test_model_qpm_slot_acquires_and_releases_dynamic_concurrency_slot(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.eval_calls = []
            self.released = []

        async def hgetall(self, key):
            return {"mode": "closed", "open_until_ms": "0", "multiplier": "1"}

        async def eval(self, script, key_count, *args):
            self.eval_calls.append((key_count, args))
            return [1, "closed", 10]

        async def zrem(self, key, member):
            self.released.append((key, member))

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "model_circuit_enabled", True)
        monkeypatch.setattr(settings, "model_qpm_limit", 0)
        monkeypatch.setattr(settings, "model_concurrency_limit", 10)
        monkeypatch.setattr(settings, "model_concurrency_wait_seconds", 0)
        monkeypatch.setattr(settings, "redis_model_active_key", "model:active")
        monkeypatch.setattr(settings, "redis_model_circuit_key", "model:circuit")

        async with queue.model_qpm_slot():
            pass

        assert fake.eval_calls
        assert fake.released[0][0] == "model:active"

    asyncio.run(scenario())
