import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-platform-test-")

from video_review.config import settings
from video_review.main import app
from video_review.models import AdminDatabaseRowsResponse, CreateReviewRequest, PlatformReviewHistoryResponse, VideoReviewReport


@pytest.fixture(autouse=True)
def isolate_platform_api_tests(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "use_redis_queue", False)


def _sign(secret: str, *, body: dict, path: str, nonce: str = "nonce-1") -> dict[str, str]:
    raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    body_sha256 = hashlib.sha256(raw_body).hexdigest()
    base = "\n".join([timestamp, nonce, "POST", path, body_sha256])
    signature = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-App-Id": "audit-platform",
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }


def test_platform_review_create_is_idempotent_by_platform_task_id(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    dispatched = []

    async def fake_dispatch(job, request, background_tasks):
        dispatched.append((job.review_id, request.platform_task_id, request.app_id))

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    payload = {
        "platform_task_id": "task-001",
        "video_url": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
        "video_title": "短剧-第1集",
    }

    first = client.post("/api/v1/reviews", json=payload, headers={"X-App-Id": "audit-platform"})
    second = client.post("/api/v1/reviews", json=payload, headers={"X-App-Id": "audit-platform"})

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()
    second_data = second.json()
    assert first_data["review_id"] == second_data["review_id"]
    assert first_data["idempotent"] is False
    assert second_data["idempotent"] is True
    assert first_data["status_url"].startswith("/api/v1/reviews/")
    assert first_data["result_url"].endswith("/result")
    assert dispatched == [(first_data["review_id"], "task-001", "audit-platform")]


def test_platform_review_status_includes_uploader_and_drama_title(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()

    async def fake_dispatch(job, request, background_tasks):
        return None

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    create_response = client.post(
        "/api/v1/reviews",
        json={
            "platform_task_id": "task-owner-fields",
            "video_url": "https://qiniu.duanju.com/a.mp4",
            "video_title": "第1集",
            "drama_title": "掌心风暴",
            "uploader_info": "龚小龙",
        },
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_user_1",
        },
    )

    assert create_response.status_code == 200
    review_id = create_response.json()["review_id"]
    status_response = client.get(
        f"/api/v1/reviews/{review_id}",
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_user_1",
        },
    )

    assert status_response.status_code == 200
    data = status_response.json()
    assert data["上传人信息"] == "龚小龙"
    assert data["剧名"] == "掌心风暴"


def test_platform_review_status_rejects_other_feishu_user(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()

    async def fake_dispatch(job, request, background_tasks):
        return None

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    create_response = client.post(
        "/api/v1/reviews",
        json={
            "platform_task_id": "task-private",
            "video_url": "https://qiniu.duanju.com/a.mp4",
        },
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_owner",
            "X-Feishu-User-Name": "Owner",
        },
    )

    assert create_response.status_code == 200
    review_id = create_response.json()["review_id"]
    denied = client.get(
        f"/api/v1/reviews/{review_id}",
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_other",
            "X-Feishu-User-Name": "Other",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["error_code"] == "FORBIDDEN_REVIEW"


def test_platform_personal_history_uses_current_feishu_user(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    captured = {}

    async def fake_fetch_platform_review_history(**kwargs):
        captured.update(kwargs)
        return PlatformReviewHistoryResponse(success=True, total=0, items=[])

    monkeypatch.setattr("video_review.main.fetch_platform_review_history", fake_fetch_platform_review_history)

    client = TestClient(app)
    response = client.get(
        "/api/v1/reviews/history?created_from=2026-07-07T00:00:00Z&created_to=2026-07-07T23:59:59Z",
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_user_1",
        },
    )

    assert response.status_code == 200
    assert captured["feishu_user_id"] == "ou_user_1"
    assert captured["include_all"] is False


def test_platform_admin_history_can_query_all_users(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    captured = {}

    async def fake_fetch_platform_review_history(**kwargs):
        captured.update(kwargs)
        return PlatformReviewHistoryResponse(success=True, total=0, items=[])

    monkeypatch.setattr("video_review.main.fetch_platform_review_history", fake_fetch_platform_review_history)

    client = TestClient(app)
    denied = client.get(
        "/api/v1/admin/reviews/history",
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_user_1",
        },
    )
    allowed = client.get(
        "/api/v1/admin/reviews/history?feishu_user_id=ou_user_2",
        headers={
            "X-App-Id": "audit-platform",
            "X-Feishu-User-Id": "ou_admin",
            "X-Feishu-Is-Admin": "true",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["error_code"] == "ADMIN_REQUIRED"
    assert allowed.status_code == 200
    assert captured["feishu_user_id"] == "ou_user_2"
    assert captured["include_all"] is True


def test_platform_admin_database_rows_requires_admin(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()

    async def fake_fetch_admin_database_rows(**kwargs):
        raise AssertionError("non-admin request must not query database")

    monkeypatch.setattr("video_review.main.fetch_admin_database_rows", fake_fetch_admin_database_rows)

    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/database/review_jobs?limit=1",
        headers={"X-App-Id": "audit-platform", "X-Feishu-User-Id": "ou_user"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "ADMIN_REQUIRED"


def test_platform_admin_database_rows_queries_whitelisted_table(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    captured = {}

    async def fake_fetch_admin_database_rows(**kwargs):
        captured.update(kwargs)
        return AdminDatabaseRowsResponse(
            success=True,
            table="review_jobs",
            total=1,
            limit=1,
            offset=0,
            columns=["review_id", "request"],
            rows=[{"review_id": "review_x", "request": {"callback_secret": "***REDACTED***"}}],
        )

    monkeypatch.setattr("video_review.main.fetch_admin_database_rows", fake_fetch_admin_database_rows)

    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/database/review_jobs?limit=1&offset=0",
        headers={"X-App-Id": "audit-platform", "X-Feishu-Is-Admin": "true"},
    )

    assert response.status_code == 200
    assert captured == {"table": "review_jobs", "limit": 1, "offset": 0}
    assert response.json()["rows"][0]["request"]["callback_secret"] == "***REDACTED***"


def test_platform_admin_reconcile_stale_requires_admin_and_calls_reconciler(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    captured = {}

    async def fake_reconcile_stale_reviews(**kwargs):
        captured.update(kwargs)
        return 3

    monkeypatch.setattr(
        "video_review.main.reconcile_stale_reviews",
        fake_reconcile_stale_reviews,
    )

    client = TestClient(app)
    denied = client.post(
        "/api/v1/admin/reviews/reconcile-stale?older_than_minutes=30&limit=10",
        headers={"X-App-Id": "audit-platform", "X-Feishu-User-Id": "ou_user"},
    )
    allowed = client.post(
        "/api/v1/admin/reviews/reconcile-stale?older_than_minutes=30&limit=10",
        headers={"X-App-Id": "audit-platform", "X-Feishu-Is-Admin": "true"},
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["reconciled_count"] == 3
    assert captured == {"older_than_minutes": 30, "limit": 10}


def test_platform_admin_database_rows_rejects_unknown_table(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()

    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/database/pg_user?limit=1",
        headers={"X-App-Id": "audit-platform", "X-Feishu-Is-Admin": "true"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "DATABASE_TABLE_NOT_ALLOWED"


def test_platform_batch_review_returns_partial_failures(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    dispatched = []

    async def fake_dispatch(job, request, background_tasks):
        dispatched.append(job.review_id)

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    response = client.post(
        "/api/v1/reviews/batch",
        json={
            "items": [
                {
                    "platform_task_id": "task-ok",
                    "video_url": "https://qiniu.duanju.com/a.mp4",
                },
                {
                    "platform_task_id": "task-bad",
                },
            ]
        },
        headers={"X-App-Id": "audit-platform"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["accepted_count"] == 1
    assert data["failed_count"] == 1
    assert data["items"][0]["success"] is True
    assert data["items"][1]["success"] is False
    assert data["items"][1]["error_code"] == "INVALID_SOURCE"
    assert len(dispatched) == 1


def test_platform_review_rejects_private_video_url_hosts(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()

    async def fake_dispatch(job, request, background_tasks):
        return None

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    response = client.post(
        "/api/v1/reviews",
        json={
            "platform_task_id": "task-ssrf",
            "video_url": "http://169.254.169.254/latest/meta-data",
        },
        headers={"X-App-Id": "audit-platform"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "VIDEO_URL_NOT_ALLOWED"


def test_platform_review_returns_conflict_when_idempotency_lock_is_held(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    monkeypatch.setattr(settings, "api_idempotency_wait_seconds", 0)

    class FakeRedis:
        async def set(self, *args, **kwargs):
            return False

    async def fake_get_redis():
        return FakeRedis()

    async def fake_dispatch(job, request, background_tasks):
        raise AssertionError("locked duplicate request must not dispatch")

    monkeypatch.setattr("video_review.main.get_redis", fake_get_redis)
    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    response = client.post(
        "/api/v1/reviews",
        json={
            "platform_task_id": "task-lock-held",
            "video_url": "https://qiniu.duanju.com/a.mp4",
        },
        headers={"X-App-Id": "audit-platform"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "REVIEW_CREATE_IN_PROGRESS"


def test_platform_oss_complete_returns_conflict_when_idempotency_lock_is_held(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.oss_bucket = "hd-audit-oss"
    settings.ensure_dirs()
    monkeypatch.setattr(settings, "api_idempotency_wait_seconds", 0)

    upload_id = "upload_session_" + "a" * 16
    session_dir = settings.upload_sessions_dir / upload_id
    session_dir.mkdir(parents=True)
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "type": "oss",
                "filename": "demo.mp4",
                "size": 11,
                "upload_started_at": "2026-07-07T10:00:00+00:00",
                "bucket": "hd-audit-oss",
                "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
                "object_key": "sn2s-video-audit/test/uploads/video_x/original/demo.mp4",
            }
        ),
        encoding="utf-8",
    )

    class FakeRedis:
        async def set(self, *args, **kwargs):
            return False

    async def fake_get_redis():
        return FakeRedis()

    async def fake_head_oss_object(bucket, object_key):
        raise AssertionError("locked duplicate request must not verify OSS")

    monkeypatch.setattr("video_review.main.get_redis", fake_get_redis)
    monkeypatch.setattr("video_review.main.head_oss_object", fake_head_oss_object)

    client = TestClient(app)
    response = client.post(
        "/api/v1/uploads/oss/complete",
        json={
            "upload_id": upload_id,
            "platform_task_id": "task-oss-lock-held",
            "filename": "demo.mp4",
            "size": 11,
        },
        headers={"X-App-Id": "audit-platform"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "REVIEW_CREATE_IN_PROGRESS"


def test_platform_hmac_auth_can_be_enabled(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_auth_secret", "platform-secret")
    monkeypatch.setattr(settings, "api_auth_secrets", None)
    monkeypatch.setattr(settings, "redis_url", None)

    async def fake_dispatch(job, request, background_tasks):
        return None

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)

    client = TestClient(app)
    payload = {
        "platform_task_id": "task-auth",
        "video_url": "https://qiniu.duanju.com/a.mp4",
    }

    rejected = client.post("/api/v1/reviews", json=payload, headers={"X-App-Id": "audit-platform"})
    headers = _sign("platform-secret", body=payload, path="/api/v1/reviews", nonce="auth-nonce-1")
    accepted = client.post(
        "/api/v1/reviews",
        content=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        headers=headers,
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_review_callback_signs_completion_payload(monkeypatch):
    import video_review.tasks as tasks

    sent = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, content, headers):
            sent.append({"url": url, "content": content, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(tasks.httpx, "AsyncClient", FakeClient)

    request = CreateReviewRequest(
        video_url="https://qiniu.duanju.com/a.mp4",
        app_id="audit-platform",
        platform_task_id="task-callback",
        callback_url="https://audit.example.com/callback",
        callback_secret="callback-secret",
    )
    report = VideoReviewReport(
        review_id="review_callback",
        video_id="video_callback",
        policy_version="test",
        decision="pass",
        risk_score=0,
        summary="通过",
    )

    asyncio.run(tasks.send_review_callback("review_callback", request, "completed", report=report))

    assert len(sent) == 1
    assert sent[0]["url"] == "https://audit.example.com/callback"
    payload = json.loads(sent[0]["content"])
    assert payload["event"] == "review.completed"
    assert payload["platform_task_id"] == "task-callback"
    assert payload["review_id"] == "review_callback"
    assert sent[0]["headers"]["X-App-Id"] == "audit-platform"
    assert sent[0]["headers"]["X-Signature"].startswith("sha256=")
