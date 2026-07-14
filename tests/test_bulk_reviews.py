import asyncio
import os
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-bulk-test-")

from video_review.config import settings

settings.data_dir = Path(os.environ["VIDEO_REVIEW_DATA_DIR"])

from video_review.main import create_bulk_reviews
from video_review.models import BulkCreateReviewRequest


def test_bulk_url_reviews_create_one_job_per_unique_url(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    dispatched = []

    async def fake_dispatch(job, request, background_tasks):
        dispatched.append((job.review_id, request.video_url, request.video_title, request.session_id))

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)
    request = BulkCreateReviewRequest(
        video_urls=[
            "https://qiniu.duanju.com/a.mp4",
            "https://qiniu.duanju.com/a.mp4",
            "https://qiniu.duanju.com/b.mp4",
        ],
        video_title_prefix="短剧",
    )

    response = asyncio.run(create_bulk_reviews(request, BackgroundTasks()))

    assert response.count == 2
    assert len(response.reviews) == 2
    assert [item[1] for item in dispatched] == [
        "https://qiniu.duanju.com/a.mp4",
        "https://qiniu.duanju.com/b.mp4",
    ]
    assert [item[2] for item in dispatched] == ["短剧-01", "短剧-02"]
    assert dispatched[0][3] == dispatched[1][3]
