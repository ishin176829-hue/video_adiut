import asyncio
import importlib
import sys
from types import SimpleNamespace

def test_worker_keeps_fetching_until_local_concurrency_is_full(monkeypatch, tmp_path):
    monkeypatch.setenv("VIDEO_REVIEW_DATA_DIR", str(tmp_path / "data"))
    for name in list(sys.modules):
        if name == "video_review.config" or name == "video_review.store" or name == "video_review.worker":
            sys.modules.pop(name)
    worker = importlib.import_module("video_review.worker")
    messages = [SimpleNamespace(review_id=f"review_{index}", request={}) for index in range(5)]
    active = 0
    max_active = 0
    empty_reads = 0

    async def fake_init_infra(*, strict=False):
        return {"redis": {"ok": True}}

    async def fake_reconcile_stale_reviews(**kwargs):
        return 0

    async def fake_dequeue_reviews(consumer, *, count=1, stage=None):
        nonlocal empty_reads
        if messages:
            return [messages.pop(0)]
        empty_reads += 1
        if empty_reads > 2:
            raise asyncio.CancelledError
        await asyncio.sleep(0.01)
        return []

    async def fake_handle_message(message, worker_semaphore, consumer_name):
        nonlocal active, max_active
        async with worker_semaphore:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    monkeypatch.setattr(worker, "init_infra", fake_init_infra)
    monkeypatch.setattr(worker, "reconcile_stale_reviews", fake_reconcile_stale_reviews)
    monkeypatch.setattr(worker, "dequeue_reviews", fake_dequeue_reviews)
    monkeypatch.setattr(worker, "handle_message", fake_handle_message)
    monkeypatch.setattr(worker.settings, "stale_processing_reconcile_on_worker_start", False)

    async def run_test():
        try:
            await worker.run_worker(consumer_name="test-worker", once=False, count=5, concurrency=5)
        except asyncio.CancelledError:
            return
        raise AssertionError("expected CancelledError")

    asyncio.run(run_test())

    assert max_active >= 3


def test_worker_recovers_pending_before_reading_new_messages(monkeypatch, tmp_path):
    worker = importlib.import_module("video_review.worker")
    calls = []
    handled = []
    stale_message = SimpleNamespace(review_id="review_stale", request={})

    async def fake_init_infra(*, strict=False):
        return {"redis": {"ok": True}}

    async def fake_reconcile_stale_reviews(**kwargs):
        return 0

    async def fake_claim_stale_reviews(consumer, *, count=1, stage=None):
        calls.append("claim")
        return [stale_message]

    async def fake_dequeue_reviews(consumer, *, count=1, stage=None):
        calls.append("dequeue")
        return []

    async def fake_handle_message(message, worker_semaphore, consumer_name):
        handled.append(message.review_id)

    monkeypatch.setattr(worker, "init_infra", fake_init_infra)
    monkeypatch.setattr(worker, "reconcile_stale_reviews", fake_reconcile_stale_reviews)
    monkeypatch.setattr(worker, "claim_stale_reviews", fake_claim_stale_reviews)
    monkeypatch.setattr(worker, "dequeue_reviews", fake_dequeue_reviews)
    monkeypatch.setattr(worker, "handle_message", fake_handle_message)
    monkeypatch.setattr(worker.settings, "stale_processing_reconcile_on_worker_start", False)

    asyncio.run(worker.run_worker(consumer_name="test-worker", once=True, count=1, concurrency=1))

    assert calls[0] == "claim"
    assert handled == ["review_stale"]


def test_worker_keeps_consuming_when_stale_reconcile_temporarily_fails(monkeypatch):
    worker = importlib.import_module("video_review.worker")
    handled = []
    message = SimpleNamespace(review_id="review_pending", request={})

    async def fake_init_infra(*, strict=False):
        return {"redis": {"ok": True}}

    async def failing_reconcile(**kwargs):
        raise RuntimeError("deadlock detected")

    async def fake_claim_stale_reviews(consumer, *, count=1, stage=None):
        return [message]

    async def fake_handle_message(item, worker_semaphore, consumer_name):
        handled.append(item.review_id)

    monkeypatch.setattr(worker, "init_infra", fake_init_infra)
    monkeypatch.setattr(worker, "reconcile_stale_reviews", failing_reconcile)
    monkeypatch.setattr(worker, "claim_stale_reviews", fake_claim_stale_reviews)
    monkeypatch.setattr(worker, "handle_message", fake_handle_message)

    asyncio.run(worker.run_worker(consumer_name="test-worker", once=True, count=1, concurrency=1))

    assert handled == ["review_pending"]
