import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone

from video_review.models import CreateReviewRequest, VideoAsset
from video_review.queue import ReviewQueueStage


def import_tasks(monkeypatch, tmp_path):
    monkeypatch.setenv("VIDEO_REVIEW_DATA_DIR", str(tmp_path / "data"))
    for name in list(sys.modules):
        if name in {"video_review.config", "video_review.store", "video_review.tasks"}:
            sys.modules.pop(name)
    tasks = importlib.import_module("video_review.tasks")
    config = importlib.import_module("video_review.config")
    return tasks, config.settings


def test_preprocess_stage_prepares_asset_and_enqueues_model(monkeypatch, tmp_path):
    tasks, settings = import_tasks(monkeypatch, tmp_path)

    async def scenario():
        settings.data_dir = tmp_path
        settings.ensure_dirs()
        video_path = tmp_path / "raw.mp4"
        video_path.write_bytes(b"fake")
        request = CreateReviewRequest(oss_bucket="bucket", oss_key="key.mp4", video_title="demo")
        tasks.create_job(request, review_id="review_stage")
        enqueued = []

        async def fake_download_oss_video(bucket, object_key, **kwargs):
            assert bucket == "bucket"
            assert object_key == "key.mp4"
            return VideoAsset(
                video_id="video_stage",
                source_url="oss://bucket/key.mp4",
                local_path=str(video_path),
                sha256="sha",
                content_length=4,
            )

        def fake_enrich(asset):
            asset.duration_seconds = 12
            asset.width = 640
            asset.height = 360
            return asset

        def fake_extract_frames(asset, **kwargs):
            frame_dir = settings.derived_dir / asset.video_id / "frames"
            frame_dir.mkdir(parents=True, exist_ok=True)
            (frame_dir / "000001.jpg").write_bytes(b"jpg")
            return frame_dir

        async def fake_enqueue(review_id, queued_request, stage):
            enqueued.append((review_id, queued_request.oss_key, stage))
            return "1-0"

        monkeypatch.setattr(tasks, "download_oss_video", fake_download_oss_video)
        monkeypatch.setattr(tasks, "enrich_asset", fake_enrich)
        monkeypatch.setattr(tasks, "extract_frames", fake_extract_frames)
        monkeypatch.setattr(tasks, "enqueue_review_stage", fake_enqueue)

        await tasks.run_preprocess_stage("review_stage", request)

        job = tasks.store.get_job("review_stage")
        assert job.video_id == "video_stage"
        assert job.local_path == str(video_path)
        assert (settings.derived_dir / "video_stage" / "asset.json").exists()
        assert enqueued == [("review_stage", "key.mp4", ReviewQueueStage.MODEL)]

    asyncio.run(scenario())


def test_model_stage_reuses_preprocessed_asset_without_downloading(monkeypatch, tmp_path):
    tasks, settings = import_tasks(monkeypatch, tmp_path)

    async def scenario():
        settings.data_dir = tmp_path
        settings.ensure_dirs()
        video_path = tmp_path / "raw.mp4"
        video_path.write_bytes(b"fake")
        request = CreateReviewRequest(oss_bucket="bucket", oss_key="key.mp4", video_title="demo")
        tasks.create_job(request, review_id="review_model_stage")
        asset = VideoAsset(video_id="video_model", local_path=str(video_path), sha256="sha", duration_seconds=12)
        asset_dir = settings.derived_dir / asset.video_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / "asset.json").write_text(asset.model_dump_json(), encoding="utf-8")
        await tasks.update_job("review_model_stage", video_id=asset.video_id, local_path=asset.local_path)
        called = []

        async def fake_download_oss_video(*args, **kwargs):
            raise AssertionError("model stage must not download source video")

        async def fake_run_model(review_id, model_request, model_asset):
            called.append((review_id, model_asset.video_id, model_asset.local_path))

        monkeypatch.setattr(tasks, "download_oss_video", fake_download_oss_video)
        monkeypatch.setattr(tasks, "_run_model_review", fake_run_model)

        await tasks.run_model_stage("review_model_stage", request)

        assert called == [("review_model_stage", "video_model", str(video_path))]

    asyncio.run(scenario())


def test_preprocess_requeues_retryable_source_failure_without_marking_review_failed(monkeypatch, tmp_path):
    tasks, settings = import_tasks(monkeypatch, tmp_path)

    async def scenario():
        from video_review.downloader import SourceDownloadError

        request = CreateReviewRequest(video_url="https://qiniu.duanju.com/demo.mp4")
        tasks.create_job(request, review_id="review_download_retry")
        scheduled = []

        async def fake_download_video(*args, **kwargs):
            raise SourceDownloadError(
                code="source_connect_timeout",
                retryable=True,
                host="qiniu.duanju.com",
                attempts=3,
            )

        async def fake_schedule(review_id, retry_request, *, delay_seconds, attempt):
            scheduled.append((review_id, retry_request.metadata, delay_seconds, attempt))
            return "retry-1"

        monkeypatch.setattr(tasks, "download_video", fake_download_video)
        monkeypatch.setattr(tasks, "schedule_download_retry", fake_schedule)
        monkeypatch.setattr(settings, "download_task_retry_attempts", 3, raising=False)
        monkeypatch.setattr(settings, "download_task_retry_delays_seconds", "60,300,900", raising=False)

        await tasks.run_preprocess_stage("review_download_retry", request)

        job = tasks.store.get_job("review_download_retry")
        assert job.status.value == "pending"
        assert job.phase == "download_retry"
        assert scheduled == [
            (
                "review_download_retry",
                {"download_retry_attempt": 1, "download_retry_reason": "source_connect_timeout"},
                60,
                1,
            )
        ]

    asyncio.run(scenario())


def test_model_stage_schedules_retryable_failure_without_marking_failed(monkeypatch, tmp_path):
    tasks, settings = import_tasks(monkeypatch, tmp_path)

    async def scenario():
        settings.cleanup_enabled = False
        settings.workflow_deadline_seconds = 1800
        settings.model_task_retry_delays_seconds = "5,15,30"
        video_path = tmp_path / "raw.mp4"
        video_path.write_bytes(b"fake")
        request = CreateReviewRequest(video_url="https://example.com/a.mp4")
        tasks.create_job(request, review_id="review_model_retry")
        asset = VideoAsset(video_id="video_retry", local_path=str(video_path), sha256="sha", duration_seconds=12)
        asset_dir = settings.derived_dir / asset.video_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / "asset.json").write_text(asset.model_dump_json(), encoding="utf-8")
        await tasks.update_job("review_model_retry", video_id=asset.video_id, local_path=asset.local_path)
        scheduled = []

        async def fake_run_model(*args, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        async def fake_schedule(review_id, retry_request, *, stage, delay_seconds, attempt):
            scheduled.append((review_id, retry_request, stage, delay_seconds, attempt))
            return "retry-entry"

        monkeypatch.setattr(tasks, "_run_model_review", fake_run_model)
        monkeypatch.setattr(tasks, "schedule_stage_retry", fake_schedule)

        await tasks.run_model_stage("review_model_retry", request)

        job = tasks.store.get_job("review_model_retry")
        assert job.status.value == "pending"
        assert job.phase == "model_retry_wait"
        assert len(scheduled) == 1
        assert scheduled[0][2] == ReviewQueueStage.MODEL
        assert scheduled[0][4] == 1
        assert scheduled[0][1].metadata["model_retry_error_kind"] == "rate_limit"

    asyncio.run(scenario())


def test_model_stage_marks_failed_when_workflow_deadline_has_expired(monkeypatch, tmp_path):
    tasks, settings = import_tasks(monkeypatch, tmp_path)

    async def scenario():
        settings.cleanup_enabled = False
        now = datetime.now(timezone.utc)
        request = CreateReviewRequest(
            video_url="https://example.com/a.mp4",
            metadata={
                "workflow_started_at": (now - timedelta(minutes=31)).isoformat(),
                "workflow_deadline_at": (now - timedelta(minutes=1)).isoformat(),
            },
        )
        tasks.create_job(request, review_id="review_model_expired")
        video_path = tmp_path / "raw-expired.mp4"
        video_path.write_bytes(b"fake")
        asset = VideoAsset(video_id="video_expired", local_path=str(video_path), sha256="sha", duration_seconds=12)
        asset_dir = settings.derived_dir / asset.video_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / "asset.json").write_text(asset.model_dump_json(), encoding="utf-8")
        await tasks.update_job("review_model_expired", video_id=asset.video_id, local_path=asset.local_path)

        async def fake_run_model(*args, **kwargs):
            raise RuntimeError("504 Gateway Time-out")

        async def fail_schedule(*args, **kwargs):
            raise AssertionError("expired workflow must not be rescheduled")

        monkeypatch.setattr(tasks, "_run_model_review", fake_run_model)
        monkeypatch.setattr(tasks, "schedule_stage_retry", fail_schedule)

        await tasks.run_model_stage("review_model_expired", request)

        job = tasks.store.get_job("review_model_expired")
        assert job.status.value == "failed"
        assert job.phase == "error"
        assert "30分钟" in job.message

    asyncio.run(scenario())
