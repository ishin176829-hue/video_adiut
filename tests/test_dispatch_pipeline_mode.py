import asyncio
import importlib
import sys

from video_review.models import CreateReviewRequest
from video_review.queue import ReviewQueueStage


def import_main(monkeypatch, tmp_path):
    monkeypatch.setenv("VIDEO_REVIEW_DATA_DIR", str(tmp_path / "data"))
    for name in list(sys.modules):
        if name in {"video_review.config", "video_review.store", "video_review.main"}:
            sys.modules.pop(name)
    return importlib.import_module("video_review.main")


def test_dispatch_review_uses_preprocess_stage_when_staged(monkeypatch, tmp_path):
    main = import_main(monkeypatch, tmp_path)
    request = CreateReviewRequest(oss_bucket="bucket", oss_key="a.mp4")
    job = main.create_job(request, review_id="review_dispatch_stage")
    calls = []

    async def fake_persist_created_job(job_arg, request_arg):
        calls.append(("persist", job_arg.review_id))

    async def fake_enqueue_stage(review_id, queued_request, stage):
        calls.append(("enqueue", review_id, queued_request.oss_key, stage))
        return "1-0"

    monkeypatch.setattr(main.settings, "use_redis_queue", True)
    monkeypatch.setattr(main.settings, "pipeline_mode", "staged")
    monkeypatch.setattr(main, "persist_created_job", fake_persist_created_job)
    monkeypatch.setattr(main, "enqueue_review_stage", fake_enqueue_stage)

    asyncio.run(main.dispatch_review(job, request, main.BackgroundTasks()))

    assert calls == [
        ("persist", "review_dispatch_stage"),
        ("enqueue", "review_dispatch_stage", "a.mp4", ReviewQueueStage.PREPROCESS),
    ]
