import os
import tempfile
from pathlib import Path
import asyncio
import pytest

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-test-")

from video_review.models import CreateReviewRequest, ReviewJob, ReviewStatus, SegmentPlan, SegmentReviewResult, VideoAsset
from video_review.model_retry import ModelContractError
from video_review.config import settings

settings.data_dir = Path(os.environ["VIDEO_REVIEW_DATA_DIR"])

import video_review.tasks as tasks
from video_review.tasks import (
    _analyze_frame_batch_resilient,
    _analyze_subtitle_resilient,
    _exception_text,
    _is_rate_limit_error,
    _is_transient_model_error,
    _persist_job_with_retry,
    _run_frame_batches_concurrently,
)


def test_gateway_timeout_is_transient_model_error():
    exc = RuntimeError("504 Gateway Time-out")

    assert _is_transient_model_error(exc)


def test_rate_limit_error_is_detected_separately():
    assert _is_rate_limit_error(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert not _is_rate_limit_error(RuntimeError("504 Gateway Time-out"))


def test_timeout_error_has_non_empty_error_text():
    assert _exception_text(asyncio.TimeoutError()) == "TimeoutError: 模型调用超时"


def test_stale_reconcile_sends_failed_callback(monkeypatch):
    async def scenario():
        request = CreateReviewRequest(
            video_url="https://example.com/a.mp4",
            callback_url="https://audit.example.com/callback",
            callback_secret="callback-secret",
        )
        calls = []

        async def fake_mark_stale_processing_jobs_failed(**kwargs):
            return [{"review_id": "review_stale", "request": request.model_dump(mode="json")}]

        async def fake_update_job(review_id, **kwargs):
            calls.append(("update", review_id, kwargs))

        async def fake_add_event(review_id, event_type, data):
            calls.append(("event", review_id, event_type, data))

        async def fake_send_review_callback(review_id, callback_request, status, **kwargs):
            calls.append(("callback", review_id, callback_request, status, kwargs))

        monkeypatch.setattr(tasks, "mark_stale_processing_jobs_failed", fake_mark_stale_processing_jobs_failed)
        monkeypatch.setattr(tasks, "update_job", fake_update_job)
        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "send_review_callback", fake_send_review_callback)

        count = await tasks.reconcile_stale_reviews()

        assert count == 1
        callback = next(item for item in calls if item[0] == "callback")
        assert callback[1] == "review_stale"
        assert callback[3] == "failed"
        assert callback[4]["error"] == "STALE_PROCESSING_TIMEOUT"

    asyncio.run(scenario())


def test_model_timeout_delegates_qpm_control_to_provider_adapter(monkeypatch):
    async def scenario():
        calls = []

        async def operation():
            calls.append("called")
            return "ok"

        async def fake_record_model_call_result(*args, **kwargs):
            return None

        monkeypatch.setattr(tasks, "record_model_call_result", fake_record_model_call_result)

        assert await tasks._call_model_with_timeout(operation) == "ok"
        assert calls == ["called"]


def test_terminal_job_persistence_retries_transient_database_failure(monkeypatch):
    async def scenario():
        attempts = 0

        async def fake_persist_job(job):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("pool busy")

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(tasks, "persist_job", fake_persist_job)
        monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)
        job = ReviewJob(
            review_id="review_terminal",
            status=ReviewStatus.COMPLETED,
            phase="done",
            message="审核完成",
        )

        await _persist_job_with_retry(job, attempts=5)

        assert attempts == 3

    asyncio.run(scenario())


def test_created_job_requires_durable_request_before_queueing(monkeypatch):
    async def scenario():
        attempts = []

        async def fake_persist_job(job, request=None, *, strict=False):
            attempts.append((request, strict))
            if len(attempts) < 3:
                raise RuntimeError("pool busy")

        async def fake_persist_event(*args, **kwargs):
            return None

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(tasks, "persist_job", fake_persist_job)
        monkeypatch.setattr(tasks, "persist_event", fake_persist_event)
        monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)
        request = CreateReviewRequest(video_url="https://example.com/a.mp4")
        job = ReviewJob(
            review_id="review_created",
            status=ReviewStatus.PENDING,
            phase="pending",
            message="任务已创建",
        )

        await tasks.persist_created_job(job, request)

        assert len(attempts) == 3
        assert all(item == (request, True) for item in attempts)

    asyncio.run(scenario())


def test_subtitle_timeout_propagates_for_stage_retry(monkeypatch):
    async def scenario():
        events = []

        class Analyzer:
            async def analyze_subtitle_text(self, *args, **kwargs):
                raise asyncio.TimeoutError

        async def fake_add_event(review_id, event_type, data):
            events.append((review_id, event_type, data))

        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(settings, "model_qpm_limit", 0)

        with pytest.raises(asyncio.TimeoutError):
            await _analyze_subtitle_resilient(
                review_id="review_test",
                analyzer=Analyzer(),
                subtitle_text="测试字幕",
                video_title="测试视频",
                subtitle_source="ocr",
            )

        assert "进入任务级重试" in events[-1][2]["text"]
        assert "TimeoutError" in events[-1][2]["text"]

    asyncio.run(scenario())


def test_frame_batch_contract_error_propagates_for_stage_retry(monkeypatch):
    async def scenario():
        settings.frame_sheet_enabled = False

        class Analyzer:
            model = "test-model"

            async def analyze_frames_segment(self, *args, **kwargs):
                return None

        async def fake_call(*args, **kwargs):
            raise ModelContractError("模型返回空内容", kind="parse")

        async def fake_add_event(*args, **kwargs):
            return None

        async def fake_get_cache(*args, **kwargs):
            return None

        monkeypatch.setattr(tasks, "_call_model_with_timeout", fake_call)
        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "get_frame_batch_cache", fake_get_cache)

        segment = SegmentPlan(
            segment_index=1,
            start_seconds=0,
            end_seconds=2,
            start_time="00:00",
            end_time="00:02",
        )
        batch = [{"timestamp": "00:01", "timestamp_seconds": 1, "frame_index": 1}]
        asset = type("Asset", (), {"sha256": "hash"})()
        policy = type("Policy", (), {"version": "v1"})()

        with pytest.raises(ModelContractError, match="模型返回空内容"):
            await _analyze_frame_batch_resilient(
                review_id="review_test",
                analyzer=Analyzer(),
                policy=policy,
                asset=asset,
                segment=segment,
                batch=batch,
                frame_fps=1,
                video_title="test",
            )

    asyncio.run(scenario())


def test_frame_batches_run_with_bounded_concurrency(monkeypatch):
    async def scenario():
        settings.frame_batch_concurrency = 2
        segment = SegmentPlan(
            segment_index=1,
            start_seconds=0,
            end_seconds=4,
            start_time="00:00",
            end_time="00:04",
        )
        active = 0
        max_active = 0

        async def fake_add_event(*args, **kwargs):
            return None

        async def fake_analyze(**kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            batch = kwargs["batch"]
            return [
                SegmentReviewResult(
                    segment_index=segment.segment_index,
                    start_time=batch[0]["timestamp"],
                    end_time=batch[-1]["timestamp"],
                    summary=batch[0]["timestamp"],
                )
            ]

        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "_analyze_frame_batch_resilient", fake_analyze)
        batches = [
            (1, [{"timestamp": "00:00", "timestamp_seconds": 0, "frame_index": 1}]),
            (2, [{"timestamp": "00:01", "timestamp_seconds": 1, "frame_index": 2}]),
            (3, [{"timestamp": "00:02", "timestamp_seconds": 2, "frame_index": 3}]),
        ]

        results = await _run_frame_batches_concurrently(
            review_id="review_test",
            analyzer=object(),
            policy=object(),
            asset=object(),
            segment=segment,
            batches=batches,
            batch_total=len(batches),
            frame_fps=1,
            video_title="test",
        )

        assert max_active == 2
        assert [result.summary for result in results] == ["00:00", "00:01", "00:02"]

    asyncio.run(scenario())


def test_frame_batch_timeout_propagates_after_configured_split_depth(monkeypatch):
    async def scenario():
        settings.frame_sheet_enabled = False
        settings.frame_batch_min_size = 4
        settings.frame_batch_max_split_depth = 1
        calls = 0

        class Analyzer:
            model = "test-model"

            async def analyze_frames_segment(self, *args, **kwargs):
                return None

        async def fake_timeout(operation, **kwargs):
            nonlocal calls
            calls += 1
            raise asyncio.TimeoutError

        async def fake_add_event(*args, **kwargs):
            return None

        async def fake_get_cache(*args, **kwargs):
            return None

        monkeypatch.setattr(tasks, "_call_model_with_timeout", fake_timeout)
        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "get_frame_batch_cache", fake_get_cache)

        segment = SegmentPlan(
            segment_index=1,
            start_seconds=0,
            end_seconds=16,
            start_time="00:00",
            end_time="00:16",
        )
        batch = [
            {"timestamp": f"00:{index:02d}", "timestamp_seconds": index, "frame_index": index}
            for index in range(16)
        ]
        asset = type("Asset", (), {"sha256": "hash"})()
        policy = type("Policy", (), {"version": "v1"})()

        with pytest.raises(asyncio.TimeoutError):
            await _analyze_frame_batch_resilient(
                review_id="review_test",
                analyzer=Analyzer(),
                policy=policy,
                asset=asset,
                segment=segment,
                batch=batch,
                frame_fps=1,
                video_title="test",
            )

        assert calls >= 2

    asyncio.run(scenario())


def test_frame_batch_rate_limit_retries_same_batch_without_splitting(monkeypatch):
    async def scenario():
        settings.frame_sheet_enabled = False
        settings.model_rate_limit_retry_attempts = 2
        settings.model_rate_limit_backoff_seconds = 0
        settings.model_retry_jitter_seconds = 0
        calls = 0

        class Analyzer:
            model = "test-model"

            async def analyze_frames_segment(self, batch, segment, video_title=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return SegmentReviewResult(
                    segment_index=segment.segment_index,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                )

        async def fake_add_event(*args, **kwargs):
            return None

        async def fake_get_cache(*args, **kwargs):
            return None

        async def fake_set_cache(*args, **kwargs):
            return None

        async def fake_safe_persist(awaitable):
            awaitable.close()

        async def fake_sleep(seconds):
            return None

        async def fake_record_model_call_result(*args, **kwargs):
            return None

        monkeypatch.setattr(tasks, "record_model_call_result", fake_record_model_call_result)
        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)
        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "get_frame_batch_cache", fake_get_cache)
        monkeypatch.setattr(tasks, "set_frame_batch_cache", fake_set_cache)
        monkeypatch.setattr(tasks, "_safe_persist", fake_safe_persist)

        segment = SegmentPlan(
            segment_index=1,
            start_seconds=0,
            end_seconds=16,
            start_time="00:00",
            end_time="00:16",
        )
        batch = [
            {"timestamp": f"00:{index:02d}", "timestamp_seconds": index, "frame_index": index}
            for index in range(16)
        ]
        asset = type("Asset", (), {"sha256": "hash", "video_id": "video_test"})()
        policy = type("Policy", (), {"version": "v1"})()

        results = await _analyze_frame_batch_resilient(
            review_id="review_test",
            analyzer=Analyzer(),
            policy=policy,
            asset=asset,
            segment=segment,
            batch=batch,
            frame_fps=1,
            video_title="test",
        )

        assert calls == 2
        assert len(results) == 1

    asyncio.run(scenario())


def test_frame_batch_rate_limit_exhaustion_propagates_for_stage_retry(monkeypatch):
    async def scenario():
        settings.frame_sheet_enabled = False

        class Analyzer:
            model = "test-model"

            async def analyze_frames_segment(self, *args, **kwargs):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")

        async def fake_call(*args, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        async def fake_add_event(*args, **kwargs):
            return None

        async def fake_get_cache(*args, **kwargs):
            return None

        monkeypatch.setattr(tasks, "_call_model_with_timeout", fake_call)
        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "get_frame_batch_cache", fake_get_cache)

        segment = SegmentPlan(
            segment_index=1,
            start_seconds=0,
            end_seconds=2,
            start_time="00:00",
            end_time="00:02",
        )
        batch = [{"timestamp": "00:01", "timestamp_seconds": 1, "frame_index": 1}]
        asset = type("Asset", (), {"sha256": "hash"})()
        policy = type("Policy", (), {"version": "v1"})()

        with pytest.raises(RuntimeError, match="429"):
            await _analyze_frame_batch_resilient(
                review_id="review_test",
                analyzer=Analyzer(),
                policy=policy,
                asset=asset,
                segment=segment,
                batch=batch,
                frame_fps=1,
                video_title="test",
            )

    asyncio.run(scenario())


def test_narrative_failure_propagates_for_stage_retry(monkeypatch):
    async def scenario():
        class Analyzer:
            model = "test-model"

            def __init__(self, *args, **kwargs):
                pass

            async def synthesize_narrative_report(self, *args, **kwargs):
                raise asyncio.TimeoutError

            async def close(self):
                return None

        async def fake_call(operation, **kwargs):
            return await operation()

        async def fake_update_job(*args, **kwargs):
            return None

        async def fake_add_event(*args, **kwargs):
            return None

        async def fake_save_report(*args, **kwargs):
            return "report.json"

        async def fake_callback(*args, **kwargs):
            return None

        async def fake_cleanup(*args, **kwargs):
            return None

        class Report:
            def model_dump(self):
                return {}

        monkeypatch.setattr(settings, "mode", "model")
        monkeypatch.setattr(settings, "google_api_key", "test-key")
        monkeypatch.setattr(settings, "google_api_keys", None)
        monkeypatch.setattr(settings, "subtitle_review_enabled", False)
        monkeypatch.setattr(settings, "synthesize_narrative", True)
        monkeypatch.setattr(tasks, "MultimodalAnalyzer", Analyzer)
        monkeypatch.setattr(tasks, "make_segment_plan", lambda *args, **kwargs: [])
        monkeypatch.setattr(tasks, "load_policy", lambda: object())
        monkeypatch.setattr(tasks, "_call_model_with_timeout", fake_call)
        monkeypatch.setattr(tasks, "update_job", fake_update_job)
        monkeypatch.setattr(tasks, "add_event", fake_add_event)
        monkeypatch.setattr(tasks, "build_report", lambda *args, **kwargs: Report())
        monkeypatch.setattr(tasks, "save_report", fake_save_report)
        monkeypatch.setattr(tasks, "send_review_callback", fake_callback)
        monkeypatch.setattr(tasks, "cleanup_terminal_artifacts", fake_cleanup)
        asset = VideoAsset(video_id="video_test", local_path="/tmp/test.mp4", sha256="sha", duration_seconds=1)

        with pytest.raises(asyncio.TimeoutError):
            await tasks._run_model_review(
                "review_test",
                CreateReviewRequest(video_url="https://example.com/a.mp4"),
                asset,
            )

    asyncio.run(scenario())
