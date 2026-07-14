import os
import tempfile
import asyncio
from pathlib import Path

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-oss-test-")

from fastapi.testclient import TestClient

from video_review.config import settings
from video_review.main import app
from video_review.models import CreateReviewRequest, VideoAsset


def test_build_oss_object_key_keeps_uploads_in_generated_video_prefix():
    from video_review.oss import build_oss_object_key

    key = build_oss_object_key(
        prefix="sn2s-video-audit/test",
        video_id="video_abc123",
        filename="第 1 集 unsafe../demo.mp4",
    )

    assert key.startswith("sn2s-video-audit/test/uploads/video_abc123/original/")
    assert key.endswith(".mp4")
    assert ".." not in key
    assert " " not in key


def test_oss_upload_complete_verifies_object_and_dispatches_review(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.oss_bucket = "hd-audit-oss"
    settings.oss_region = "oss-cn-hangzhou"
    settings.oss_endpoint = "https://oss-cn-hangzhou.aliyuncs.com"
    settings.oss_prefix = "sn2s-video-audit/test"
    settings.ensure_dirs()
    dispatched = []

    async def fake_create_upload_credentials(*, upload_id, object_key, filename, size):
        return {
            "access_key_id": "STS.fake",
            "access_key_secret": "secret",
            "security_token": "token",
            "expiration": "2026-07-06T10:00:00Z",
        }

    async def fake_head_oss_object(bucket, object_key):
        assert bucket == "hd-audit-oss"
        assert object_key.startswith("sn2s-video-audit/test/uploads/")
        return {
            "etag": "etag-from-head",
            "content_length": 11,
            "content_type": "video/mp4",
        }

    async def fake_dispatch(job, request, background_tasks):
        dispatched.append((job.review_id, request))

    monkeypatch.setattr("video_review.main.create_upload_credentials", fake_create_upload_credentials)
    monkeypatch.setattr("video_review.main.head_oss_object", fake_head_oss_object)
    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    init_response = client.post("/video/oss/uploads/init", json={"filename": "demo.mp4", "size": 11})

    assert init_response.status_code == 200
    init_data = init_response.json()
    assert init_data["bucket"] == "hd-audit-oss"
    assert init_data["object_key"].endswith(".mp4")
    assert init_data["credentials"]["security_token"] == "token"

    complete_response = client.post(
        "/video/oss/uploads/complete",
        json={
            "upload_id": init_data["upload_id"],
            "etag": "etag-from-browser",
            "size": 11,
            "filename": "demo.mp4",
            "fps": 1,
            "segment_seconds": 180,
        },
    )

    assert complete_response.status_code == 200
    assert complete_response.json()["review_id"].startswith("review_")
    assert len(dispatched) == 1
    request = dispatched[0][1]
    assert request.oss_bucket == "hd-audit-oss"
    assert request.oss_key == init_data["object_key"]
    assert request.oss_etag == "etag-from-head"
    assert request.oss_size == 11
    assert request.video_title == "demo.mp4"
    assert not (settings.upload_sessions_dir / init_data["upload_id"]).exists()


def test_oss_upload_init_accepts_apple_mov_files(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.oss_bucket = "hd-audit-oss"
    settings.oss_region = "oss-cn-hangzhou"
    settings.oss_endpoint = "https://oss-cn-hangzhou.aliyuncs.com"
    settings.oss_prefix = "sn2s-video-audit/test"
    settings.ensure_dirs()

    async def fake_create_upload_credentials(*, upload_id, object_key, filename, size):
        assert filename == "apple-demo.mov"
        assert object_key.endswith(".mov")
        return {
            "access_key_id": "STS.fake",
            "access_key_secret": "secret",
            "security_token": "token",
            "expiration": "2026-07-06T10:00:00Z",
        }

    monkeypatch.setattr("video_review.main.create_upload_credentials", fake_create_upload_credentials)

    client = TestClient(app)
    response = client.post(
        "/video/oss/uploads/init",
        json={"filename": "apple-demo.mov", "size": 11, "content_type": "video/quicktime"},
    )

    assert response.status_code == 200
    assert response.json()["object_key"].endswith(".mov")


def test_oss_upload_init_reports_sts_configuration_errors(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.oss_bucket = "hd-audit-oss"
    settings.ensure_dirs()

    async def fake_create_upload_credentials(*, upload_id, object_key, filename, size):
        raise RuntimeError("未配置 ALIYUN_STS_ROLE_ARN")

    monkeypatch.setattr("video_review.main.create_upload_credentials", fake_create_upload_credentials)

    client = TestClient(app)
    response = client.post("/video/oss/uploads/init", json={"filename": "demo.mp4", "size": 11})

    assert response.status_code == 503
    assert "未配置 ALIYUN_STS_ROLE_ARN" in response.json()["detail"]


def test_run_review_routes_oss_request_to_oss_downloader(monkeypatch, tmp_path):
    import video_review.tasks as tasks

    async def scenario():
        settings.data_dir = tmp_path
        settings.ensure_dirs()
        video_path = tmp_path / "demo.mp4"
        video_path.write_bytes(b"not-a-real-video")
        called = {}
        request = CreateReviewRequest(
            oss_bucket="hd-audit-oss",
            oss_key="sn2s-video-audit/test/uploads/video_x/original/demo.mp4",
            oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            video_title="demo.mp4",
            fps=1,
            segment_seconds=180,
        )
        tasks.create_job(request, review_id="review_oss_route")

        async def fake_download_oss_video(bucket, object_key, *, title=None, endpoint=None, etag=None, content_length=None):
            called["args"] = {
                "bucket": bucket,
                "object_key": object_key,
                "title": title,
                "endpoint": endpoint,
                "etag": etag,
                "content_length": content_length,
            }
            return VideoAsset(video_id="video_oss", local_path=str(video_path), sha256="hash")

        def stop_after_download(asset):
            raise RuntimeError(f"stop after {asset.video_id}")

        monkeypatch.setattr(tasks, "download_oss_video", fake_download_oss_video)
        monkeypatch.setattr(tasks, "enrich_asset", stop_after_download)

        await tasks.run_review("review_oss_route", request)

        assert called["args"]["bucket"] == "hd-audit-oss"
        assert called["args"]["object_key"].endswith("demo.mp4")
        assert called["args"]["endpoint"] == "https://oss-cn-hangzhou.aliyuncs.com"

    asyncio.run(scenario())
