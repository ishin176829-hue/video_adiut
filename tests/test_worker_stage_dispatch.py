import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def test_worker_model_exception_schedules_retry_before_ack(monkeypatch, tmp_path):
    worker = import_worker(monkeypatch, tmp_path)
    calls = []

    async def fake_terminal(review_id):
        return False

    async def fail_process(message):
        raise RuntimeError("模型熔断中，等待恢复超时")

    async def fake_schedule(review_id, request, *, stage, delay_seconds, attempt):
        calls.append(("schedule", review_id, stage, delay_seconds, attempt))
        return "retry-entry"

    async def fake_update_job(review_id, **kwargs):
        calls.append(("update", review_id, kwargs))

    async def fake_add_event(review_id, event_type, data):
        calls.append(("event", review_id, event_type, data))

    async def fake_ack(message):
        calls.append(("ack", message.stream_id))

    async def fail_dead_letter(*args, **kwargs):
        raise AssertionError("retryable worker failure must not be dead-lettered")

    def fake_plan(request, **kwargs):
        return SimpleNamespace(
            request=request,
            attempt=1,
            delay_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=29),
        )

    async def scenario():
        monkeypatch.setattr(worker, "_is_terminal_job", fake_terminal)
        monkeypatch.setattr(worker, "process_message", fail_process)
        monkeypatch.setattr(worker, "plan_stage_retry", fake_plan)
        monkeypatch.setattr(worker, "schedule_stage_retry", fake_schedule)
        monkeypatch.setattr(worker, "update_job", fake_update_job)
        monkeypatch.setattr(worker, "add_event", fake_add_event)
        monkeypatch.setattr(worker, "ack_review", fake_ack)
        monkeypatch.setattr(worker, "dead_letter_review", fail_dead_letter)
        message = ReviewQueueMessage(
            stream_id="10-0",
            review_id="review_retry",
            request=CreateReviewRequest(video_url="https://example.com/a.mp4"),
            payload={},
            stage=ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )

        await worker.handle_message(message, asyncio.Semaphore(1), "consumer-test")

    asyncio.run(scenario())

    assert calls[0][0] == "schedule"
    assert calls[-1] == ("ack", "10-0")


def test_worker_keeps_pending_message_when_retry_persistence_fails(monkeypatch, tmp_path):
    worker = import_worker(monkeypatch, tmp_path)
    acknowledged = []
    dead_lettered = []

    async def fake_terminal(review_id):
        return False

    async def fail_process(message):
        raise RuntimeError("database connection unavailable")

    async def fake_schedule(*args, **kwargs):
        return None

    async def fake_ack(message):
        acknowledged.append(message.stream_id)

    async def fake_dead_letter(message, error):
        dead_lettered.append(message.stream_id)

    def fake_plan(request, **kwargs):
        return SimpleNamespace(
            request=request,
            attempt=1,
            delay_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=29),
        )

    async def scenario():
        monkeypatch.setattr(worker, "_is_terminal_job", fake_terminal)
        monkeypatch.setattr(worker, "process_message", fail_process)
        monkeypatch.setattr(worker, "plan_stage_retry", fake_plan)
        monkeypatch.setattr(worker, "schedule_stage_retry", fake_schedule)
        monkeypatch.setattr(worker, "ack_review", fake_ack)
        monkeypatch.setattr(worker, "dead_letter_review", fake_dead_letter)
        message = ReviewQueueMessage(
            stream_id="11-0",
            review_id="review_pending",
            request=CreateReviewRequest(video_url="https://example.com/a.mp4"),
            payload={},
            stage=ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )

        await worker.handle_message(message, asyncio.Semaphore(1), "consumer-test")

    asyncio.run(scenario())

    assert acknowledged == []
    assert dead_lettered == []


def test_worker_marks_deadline_exhaustion_failed_before_dead_letter(monkeypatch, tmp_path):
    worker = import_worker(monkeypatch, tmp_path)
    calls = []

    async def fake_terminal(review_id):
        return False

    async def fail_process(message):
        raise RuntimeError("模型持续不可用")

    def expired_plan(request, **kwargs):
        return None

    async def fake_fail_workflow(review_id, request, *, message, error):
        calls.append(("fail", review_id, message, error))

    async def fake_dead_letter(message, error):
        calls.append(("dead", message.stream_id, error))

    async def scenario():
        monkeypatch.setattr(worker, "_is_terminal_job", fake_terminal)
        monkeypatch.setattr(worker, "process_message", fail_process)
        monkeypatch.setattr(worker, "plan_stage_retry", expired_plan)
        monkeypatch.setattr(worker, "fail_workflow", fake_fail_workflow)
        monkeypatch.setattr(worker, "dead_letter_review", fake_dead_letter)
        message = ReviewQueueMessage(
            stream_id="12-0",
            review_id="review_expired",
            request=CreateReviewRequest(video_url="https://example.com/a.mp4"),
            payload={},
            stage=ReviewQueueStage.MODEL,
            stream="stream:model",
            group="group:model",
        )

        await worker.handle_message(message, asyncio.Semaphore(1), "consumer-test")

    asyncio.run(scenario())

    assert calls[0][0:2] == ("fail", "review_expired")
    assert "30分钟" in calls[0][2]
    assert calls[1][0] == "dead"
