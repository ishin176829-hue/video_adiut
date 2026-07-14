import os
import tempfile

from fastapi.testclient import TestClient

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-admin-test-")

from video_review.main import app
from video_review.models import AdminStatsResponse


def test_admin_stats_accepts_created_time_filters(monkeypatch):
    captured = {}

    async def fake_fetch_admin_stats(**kwargs):
        captured.update(kwargs)
        return AdminStatsResponse(total=0)

    monkeypatch.setattr("video_review.main.fetch_admin_stats", fake_fetch_admin_stats)
    client = TestClient(app)

    response = client.get(
        "/video/admin/stats",
        params={
            "limit": "50",
            "status": "completed",
            "created_from": "2026-07-01T06:00:00Z",
            "created_to": "2026-07-01T10:00:00Z",
        },
    )

    assert response.status_code == 200
    assert captured["limit"] == 50
    assert captured["status"] == "completed"
    assert captured["created_from"].isoformat().startswith("2026-07-01T06:00:00")
    assert captured["created_to"].isoformat().startswith("2026-07-01T10:00:00")
