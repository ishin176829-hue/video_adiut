import asyncio
import importlib
import sys

from video_review.models import CreateReviewRequest
from video_review.queue import ReviewQueueMessage, ReviewQueueStage


def import_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("VIDEO_REVIEW_DATA_DIR", str(tmp_path / "data"))
    for name in list(sys.modules):
        if name in {"video_review.config", "video_review.store", "video_review.worker"}:
            sys.modules.pop(name)
    return importlib.import_module("video_review.worker")


def test_worker_dispatches_preprocess_without_global_slot(monkeypatch, tmp_path):
    worker = import_worker(monkeypatch, tmp_path)

    called = []

    async def fake_preprocess(review_id, request):
        called.append(("preprocess", review_id, request.oss_key))

    async def fake_model(review_id, request):
        called.append(("model", review_id, request.oss_key))

    class FailingSlot:
        async def __aenter__(self):
            raise AssertionError("preprocess must not acquire global model slot")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def scenario():
        monkeypatch.setattr(worker, "run_preprocess_stage", fake_preprocess)
        monkeypatch.setattr(worker, "run_model_stage", fake_model)
        monkeypatch.setattr(worker, "global_review_slot", lambda review_id: FailingSlot())
        message = ReviewQueueMessage(
            stream_id="1-0",
            review_id="review_1",
            request=CreateReviewRequest(oss_bucket="bucket", oss_key="a.mp4"),
            payload={},
            stage=ReviewQueueStage.PREPROCESS,
            stream="stream:preprocess",
            group="group:preprocess",
        )

        await worker.process_message(message)

    asyncio.run(scenario())

    assert called == [("preprocess", "review_1", "a.mp4")]


def test_worker_dispatches_model_with_global_slot(monkeypatch, tmp_path):
    worker = import_worker(monkeypatch, tmp_path)

    called = []

    async def fake_preprocess(review_id, request):
        called.append(("preprocess", review_id))

    async def fake_model(review_id, request):
        called.append(("model", review_id))

    class RecordingSlot:
        async def __aenter__(self):
            called.append(("slot", "enter"))

        async def __aexit__(self, exc_type, exc, tb):
            called.append(("slot", "exit"))
            return False

    async def scenario():
        monkeypatch.setattr(worker, "run_preprocess_stage", fake_preprocess)
        monkeypatch.setattr(worker, "run_model_stage", fake_model)
        monkeypatch.setattr(worker, "global_review_slot", lambda review_id: RecordingSlot())
        message = ReviewQueueMessage(
            stream_id="1-0",
            review_id="review_2",
            request=CreateReviewRequest(oss_bucket="bucket", oss_key="b.mp4"),
            payload={},
            stage=ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )

        await worker.process_message(message)

    asyncio.run(scenario())

    assert called == [("slot", "enter"), ("model", "review_2"), ("slot", "exit")]


def test_worker_acks_recovered_message_when_database_job_is_already_terminal(monkeypatch, tmp_path):
    worker = import_worker(monkeypatch, tmp_path)
    acknowledged = []

    async def fake_fetch_review_job_states(review_ids):
        return {"review_finished": {"status": "failed", "phase": "error", "error": "STALE_PROCESSING_TIMEOUT"}}

    async def fail_process_message(message):
        raise AssertionError("terminal message must not be reprocessed")

    async def fake_ack_review(message):
        acknowledged.append(message.stream_id)

    async def scenario():
        monkeypatch.setattr(worker, "fetch_review_job_states", fake_fetch_review_job_states)
        monkeypatch.setattr(worker, "process_message", fail_process_message)
        monkeypatch.setattr(worker, "ack_review", fake_ack_review)
        message = ReviewQueueMessage(
            stream_id="9-0",
            review_id="review_finished",
            request=CreateReviewRequest(oss_bucket="bucket", oss_key="finished.mp4"),
            payload={},
            stage=ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )
        await worker.handle_message(message, asyncio.Semaphore(1), "consumer-test")

    asyncio.run(scenario())

    assert acknowledged == ["9-0"]
