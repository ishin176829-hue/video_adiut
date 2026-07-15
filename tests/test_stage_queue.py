import asyncio
import json

from video_review import queue
from video_review.config import settings
from video_review.models import CreateReviewRequest


def test_enqueue_review_stage_routes_to_stage_stream(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def xadd(self, stream, fields):
            self.calls.append((stream, fields))
            return "1-0"

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "redis_preprocess_stream", "stream:preprocess")
        monkeypatch.setattr(settings, "redis_model_stream", "stream:model")

        request = CreateReviewRequest(oss_bucket="bucket", oss_key="key.mp4")
        preprocess_id = await queue.enqueue_review_stage("review_1", request, queue.ReviewQueueStage.PREPROCESS)
        model_id = await queue.enqueue_review_stage("review_1", request, queue.ReviewQueueStage.MODEL)

        assert preprocess_id == "1-0"
        assert model_id == "1-0"
        assert fake.calls[0][0] == "stream:preprocess"
        assert fake.calls[0][1]["stage"] == "preprocess"
        assert fake.calls[1][0] == "stream:model"
        assert fake.calls[1][1]["stage"] == "model"

    asyncio.run(scenario())


def test_ack_review_uses_message_stream_and_group(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.acked = None

        async def xack(self, stream, group, stream_id):
            self.acked = (stream, group, stream_id)

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        message = queue.ReviewQueueMessage(
            stream_id="9-0",
            review_id="review_1",
            request=CreateReviewRequest(video_url="https://example.com/a.mp4"),
            payload={},
            stage=queue.ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )

        await queue.ack_review(message)

        assert fake.acked == ("stream:model", "group:model", "9-0")

    asyncio.run(scenario())


def test_claim_stale_reviews_parses_autoclaimed_stage_messages(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.args = None

        async def xautoclaim(self, stream, group, consumer, min_idle_time, start_id, count):
            self.args = (stream, group, consumer, min_idle_time, start_id, count)
            return (
                "0-0",
                [
                    (
                        "7-0",
                        {
                            "review_id": "review_7",
                            "payload": '{"video_url":"https://example.com/a.mp4"}',
                            "stage": "model",
                        },
                    )
                ],
                [],
            )

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "redis_model_stream", "stream:model")
        monkeypatch.setattr(settings, "redis_model_group", "group:model")
        monkeypatch.setattr(settings, "redis_pending_claim_min_idle_ms", 1234)

        messages = await queue.claim_stale_reviews(
            "consumer-1",
            stage=queue.ReviewQueueStage.MODEL,
            count=2,
        )

        assert fake.args == ("stream:model", "group:model", "consumer-1", 1234, "0-0", 2)
        assert len(messages) == 1
        assert messages[0].stream_id == "7-0"
        assert messages[0].stage == queue.ReviewQueueStage.MODEL
        assert messages[0].stream == "stream:model"
        assert messages[0].group == "group:model"

    asyncio.run(scenario())


def test_renew_review_claim_refreshes_pending_idle_time(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.args = None

        async def xclaim(self, stream, group, consumer, min_idle_time, message_ids, retrycount, justid):
            self.args = (stream, group, consumer, min_idle_time, message_ids, retrycount, justid)
            return ["7-0"]

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        message = queue.ReviewQueueMessage(
            stream_id="7-0",
            review_id="review_7",
            request=CreateReviewRequest(video_url="https://example.com/a.mp4"),
            payload={},
            stage=queue.ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )

        await queue.renew_review_claim(message, "consumer-1")

        assert fake.args == ("stream:model", "group:model", "consumer-1", 0, ["7-0"], 0, True)

    asyncio.run(scenario())


def test_schedule_stage_retry_persists_model_request_in_sorted_set(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.zadd_args = None
            self.expire_args = None

        async def zadd(self, key, mapping):
            self.zadd_args = (key, mapping)

        async def expire(self, key, seconds):
            self.expire_args = (key, seconds)

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "redis_model_retry_key", "retry:model", raising=False)
        request = CreateReviewRequest(
            video_url="https://example.com/a.mp4",
            metadata={"model_retry_attempt": 1},
        )

        entry = await queue.schedule_stage_retry(
            "review_1",
            request,
            stage=queue.ReviewQueueStage.MODEL,
            delay_seconds=15,
            attempt=1,
        )

        assert entry is not None
        assert fake.zadd_args[0] == "retry:model"
        raw = next(iter(fake.zadd_args[1]))
        payload = json.loads(raw)
        assert payload["review_id"] == "review_1"
        assert payload["stage"] == "model"
        assert json.loads(payload["payload"])["metadata"]["model_retry_attempt"] == 1

    asyncio.run(scenario())


def test_promote_due_stage_retries_targets_model_stream(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.eval_args = None

        async def eval(self, script, key_count, *args):
            self.eval_args = (script, key_count, args)
            return 3

    async def scenario():
        fake = FakeRedis()

        async def fake_get_redis(*, strict=False):
            return fake

        monkeypatch.setattr(queue, "get_redis", fake_get_redis)
        monkeypatch.setattr(settings, "redis_model_retry_key", "retry:model", raising=False)
        monkeypatch.setattr(settings, "redis_model_stream", "stream:model")
        monkeypatch.setattr(settings, "model_retry_promote_count", 100, raising=False)

        promoted = await queue.promote_due_stage_retries(queue.ReviewQueueStage.MODEL)

        assert promoted == 3
        assert fake.eval_args[1] == 2
        assert fake.eval_args[2][0:2] == ("retry:model", "stream:model")
        assert fake.eval_args[2][-1] == "model"

    asyncio.run(scenario())
