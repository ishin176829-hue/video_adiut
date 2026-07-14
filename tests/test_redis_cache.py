from video_review.queue import frame_batch_cache_key
import asyncio

from video_review.config import settings
from video_review import queue


def test_frame_batch_cache_key_is_stable_and_model_sensitive():
    frames = [
        {"frame_index": 1, "timestamp_seconds": 0},
        {"frame_index": 2, "timestamp_seconds": 1},
    ]

    key = frame_batch_cache_key(
        video_sha256="abc",
        policy_version="policy-1",
        model="gemini",
        fps=1,
        frames=frames,
    )
    same_key = frame_batch_cache_key(
        video_sha256="abc",
        policy_version="policy-1",
        model="gemini",
        fps=1,
        frames=frames,
    )
    different_model_key = frame_batch_cache_key(
        video_sha256="abc",
        policy_version="policy-1",
        model="other-model",
        fps=1,
        frames=frames,
    )

    assert key == same_key
    assert key != different_model_key
    assert key.startswith("sn2s:video_review:cache:frame_batch:")


def test_global_review_slot_acquires_and_releases_redis_member(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.eval_args = None
            self.released = None

        async def eval(self, script, key_count, key, member, now_seconds, ttl_seconds, limit):
            self.eval_args = {
                "key_count": key_count,
                "key": key,
                "member": member,
                "ttl_seconds": ttl_seconds,
                "limit": limit,
            }
            return 1

        async def zrem(self, key, member):
            self.released = (key, member)

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(settings, "global_active_limit", 7)
        monkeypatch.setattr(settings, "global_active_ttl_seconds", 60)
        monkeypatch.setattr(settings, "global_active_wait_seconds", 1)
        monkeypatch.setattr(settings, "redis_global_active_key", "active:test")
        monkeypatch.setattr(queue, "get_redis", fake_get_redis)

        async with queue.global_review_slot("review_test") as slot:
            assert slot.enabled is True
            assert fake.eval_args["key"] == "active:test"
            assert fake.eval_args["limit"] == 7
            assert fake.eval_args["ttl_seconds"] == 60
            assert "review_test" in fake.eval_args["member"]

        assert fake.released == ("active:test", fake.eval_args["member"])

    asyncio.run(scenario())
