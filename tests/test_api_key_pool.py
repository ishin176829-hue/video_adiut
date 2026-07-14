import asyncio

from video_review.api_key_pool import GoogleApiKeyPool, api_key_fingerprint


def test_google_api_key_pool_deduplicates_and_round_robins():
    async def scenario():
        async def no_redis():
            return None

        pool = GoogleApiKeyPool(
            ["legacy-key", "new-key-a", "new-key-b", "new-key-a"],
            redis_getter=no_redis,
            key_concurrency_limit=100,
        )

        selected = [await pool.acquire() for _ in range(5)]

        assert [lease.api_key for lease in selected] == [
            "legacy-key",
            "new-key-a",
            "new-key-b",
            "legacy-key",
            "new-key-a",
        ]
        assert all(lease.key_id == api_key_fingerprint(lease.api_key) for lease in selected)
        assert all("legacy" not in lease.key_id for lease in selected)

    asyncio.run(scenario())


def test_google_api_key_pool_rejects_empty_configuration():
    try:
        GoogleApiKeyPool([])
    except RuntimeError as exc:
        assert "GOOGLE_API_KEY" in str(exc)
    else:
        raise AssertionError("expected missing-key error")


def test_google_api_key_pool_uses_redis_global_scheduler_and_reports_failures():
    class FakeRedis:
        def __init__(self):
            self.eval_calls = []

        async def eval(self, script, key_count, *args):
            self.eval_calls.append((script, key_count, args))
            if len(self.eval_calls) == 1:
                return [3, "lease-1"]
            return 1

    async def scenario():
        fake = FakeRedis()

        async def redis_getter():
            return fake

        pool = GoogleApiKeyPool(
            ["key-a", "key-b", "key-c"],
            redis_getter=redis_getter,
            key_concurrency_limit=4,
            lease_ttl_seconds=30,
        )

        lease = await pool.acquire()
        assert lease.api_key == "key-c"
        assert lease.key_id == api_key_fingerprint("key-c")
        assert fake.eval_calls[0][1] == 6
        assert fake.eval_calls[0][2][6] == 3
        assert fake.eval_calls[0][2][10] == 4

        await pool.release(lease, success=False, error_kind="rate_limit")
        assert len(fake.eval_calls) == 3

    asyncio.run(scenario())
