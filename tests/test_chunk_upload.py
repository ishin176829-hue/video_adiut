import os
import tempfile
from pathlib import Path

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-chunk-test-")

from fastapi.testclient import TestClient

from video_review.config import settings
from video_review.main import app


def test_chunk_upload_complete_creates_review(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    dispatched = []

    async def fake_dispatch(job, request, background_tasks):
        dispatched.append((job.review_id, Path(request.local_path), request.video_title))

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)
    client = TestClient(app)

    init_response = client.post("/video/uploads/init", json={"filename": "demo.mp4", "size": 11})
    assert init_response.status_code == 200
    upload_id = init_response.json()["upload_id"]

    part_a = client.post(
        f"/video/uploads/{upload_id}/chunk",
        data={"chunk_index": "0"},
        files={"chunk": ("0.part", b"hello ", "application/octet-stream")},
    )
    part_b = client.post(
        f"/video/uploads/{upload_id}/chunk",
        data={"chunk_index": "1"},
        files={"chunk": ("1.part", b"world", "application/octet-stream")},
    )
    assert part_a.status_code == 200
    assert part_b.status_code == 200

    complete_response = client.post(
        f"/video/uploads/{upload_id}/complete",
        json={"filename": "demo.mp4", "chunk_count": 2, "fps": 1, "segment_seconds": 180},
    )

    assert complete_response.status_code == 200
    assert complete_response.json()["review_id"].startswith("review_")
    assert len(dispatched) == 1
    assert dispatched[0][1].read_bytes() == b"hello world"
    assert dispatched[0][2] == "demo.mp4"
    assert not (settings.upload_sessions_dir / upload_id).exists()
