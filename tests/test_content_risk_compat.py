import asyncio
import hashlib
import hmac
import json
import os
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="video-review-compat-test-")

from video_review.config import settings
import video_review.main as main_module
from video_review.main import app, platform_review_id
from video_review.models import CreateReviewRequest, ReviewFinding, ReviewJob, ReviewStatus, VideoReviewReport
from video_review.store import store


@pytest.fixture(autouse=True)
def isolate_content_risk_tests(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "use_redis_queue", False)

    async def empty_job_states(review_ids):
        return {}

    monkeypatch.setattr(main_module, "fetch_review_job_states", empty_job_states)


def test_content_risk_task_creates_internal_review_from_neutral_payload(monkeypatch):
    captured = {}

    async def fake_dispatch(job, request, background_tasks):
        captured["review_id"] = job.review_id
        captured["request"] = request

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)
    client = TestClient(app)

    response = client.post(
        "/api/compat/content-risk/video/tasks",
        json={
            "data_id": "episode-001",
            "parameters": {
                "video_url": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
                "title": "第1集",
                "interval": 1,
                "callback_url": "https://delivery.example.com/audit/callback",
                "callback_secret": "callback-secret",
            },
        },
        headers={"X-App-Id": "delivery-system", "X-Feishu-User-Id": "ou_owner"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 0
    assert data["data_id"] == "episode-001"
    assert data["task_id"] == "episode-001"
    assert data["status"] == "submitted"
    assert data["review_id"] == platform_review_id("delivery-system", "episode-001")

    request = captured["request"]
    assert request.platform_task_id == "episode-001"
    assert request.video_url == "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4"
    assert request.video_title == "第1集"
    assert request.fps == 1
    assert request.callback_url == "https://delivery.example.com/audit/callback"
    assert request.metadata["compat_mode"] == "content_risk"
    assert request.metadata["data_id"] == "episode-001"


def test_content_risk_result_returns_processing_payload_without_409(monkeypatch):
    async def fake_dispatch(job, request, background_tasks):
        return None

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)
    client = TestClient(app)
    client.post(
        "/api/compat/content-risk/video/tasks",
        json={
            "DataId": "episode-002",
            "Parameters": {
                "VideoUrl": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
                "Title": "第2集",
            },
        },
        headers={"X-App-Id": "delivery-system"},
    )

    response = client.get(
        "/api/compat/content-risk/video/results?data_id=episode-002",
        headers={"X-App-Id": "delivery-system"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 0
    assert data["data_id"] == "episode-002"
    assert data["status"] == "submitted"
    assert data["final_label"] == ""
    assert data["decision_label"] == ""
    assert data["video_results"] == {"decision": "", "frames": []}
    assert data["audio_results"] == {"decision": "", "details": []}


def test_content_risk_task_accepts_parameters_as_json_string(monkeypatch):
    captured = {}

    async def fake_dispatch(job, request, background_tasks):
        captured["request"] = request

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)
    client = TestClient(app)

    response = client.post(
        "/api/compat/content-risk/video/tasks",
        json={
            "DataId": "episode-json-parameters",
            "Parameters": json.dumps(
                {
                    "VideoUrl": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
                    "Title": "JSON参数集",
                },
                ensure_ascii=False,
            ),
        },
        headers={"X-App-Id": "delivery-system"},
    )

    assert response.status_code == 200
    assert captured["request"].platform_task_id == "episode-json-parameters"
    assert captured["request"].video_title == "JSON参数集"


def test_content_risk_result_maps_report_findings_to_video_and_audio_details(tmp_path):
    review_id = platform_review_id("delivery-system", "episode-003")
    store.save_job(
        ReviewJob(
            review_id=review_id,
            app_id="delivery-system",
            platform_task_id="episode-003",
            status=ReviewStatus.COMPLETED,
            phase="done",
            message="审核完成",
        )
    )
    store.save_report(
        VideoReviewReport(
            review_id=review_id,
            video_id="video_003",
            policy_version="test",
            decision="reject",
            risk_score=92,
            summary="命中风险",
            findings=[
                ReviewFinding(
                    category="violence_harm",
                    sub_category="暴力威胁",
                    risk_level="高",
                    rule_tag="暴力",
                    severity="high",
                    start_time="00:12",
                    end_time="00:13",
                    evidence="字幕出现威胁性台词",
                    reason="存在威胁表达",
                    suggested_action="改台词",
                    original_text="再看把你眼睛挖了",
                    confidence=0.93,
                )
            ],
        )
    )

    client = TestClient(app)
    response = client.get(
        "/api/compat/content-risk/video/results?data_id=episode-003",
        headers={"X-App-Id": "delivery-system"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["final_label"] == "BLOCK"
    assert data["decision_label"] == "BLOCK"
    assert data["video_results"]["decision"] == "BLOCK"
    assert data["video_results"]["frames"][0]["time"] == 12.0
    assert data["video_results"]["frames"][0]["label"] == "violence_harm"
    assert data["video_results"]["frames"][0]["sub_label"] == "暴力威胁"
    assert data["audio_results"]["details"][0]["start_time"] == 12.0
    assert data["audio_results"]["details"][0]["end_time"] == 13.0
    assert "字幕出现威胁性台词" in data["annotations"][0]


def test_content_risk_result_does_not_finish_before_postgres_terminal_state(monkeypatch):
    review_id = platform_review_id("delivery-system", "episode-db-lag")
    store.save_job(
        ReviewJob(
            review_id=review_id,
            app_id="delivery-system",
            platform_task_id="episode-db-lag",
            status=ReviewStatus.COMPLETED,
            phase="done",
            message="审核完成",
        )
    )
    store.save_report(
        VideoReviewReport(
            review_id=review_id,
            video_id="video_db_lag",
            policy_version="test",
            decision="pass",
            risk_score=0,
            summary="本地报告已经生成",
        )
    )

    async def fake_fetch_review_job_states(review_ids):
        return {
            review_id: {
                "status": "processing",
                "phase": "judge",
                "error": None,
            }
        }

    monkeypatch.setattr(main_module, "fetch_review_job_states", fake_fetch_review_job_states, raising=False)
    client = TestClient(app)

    response = client.get(
        "/api/compat/content-risk/video/results?data_id=episode-db-lag",
        headers={"X-App-Id": "delivery-system"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"


def test_content_risk_batch_result_returns_per_data_id_items(monkeypatch):
    async def fake_dispatch(job, request, background_tasks):
        return None

    monkeypatch.setattr("video_review.main.dispatch_review", fake_dispatch)
    client = TestClient(app)
    client.post(
        "/api/compat/content-risk/video/tasks",
        json={
            "data_id": "episode-batch-001",
            "parameters": {
                "video_url": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
                "title": "批量1",
            },
        },
        headers={"X-App-Id": "delivery-system"},
    )

    response = client.post(
        "/api/compat/content-risk/video/results/batch",
        json={"data_ids": ["episode-batch-001", "missing-episode"]},
        headers={"X-App-Id": "delivery-system"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 0
    assert data["count"] == 2
    assert data["items"][0]["data_id"] == "episode-batch-001"
    assert data["items"][0]["status"] == "submitted"
    assert data["items"][1]["code"] == 40404
    assert data["items"][1]["data_id"] == "missing-episode"


def test_content_risk_callback_uses_notification_payload_and_data_id_signature(monkeypatch):
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
        app_id="delivery-system",
        platform_task_id="episode-004",
        video_url="https://qiniu.duanju.com/a.mp4",
        callback_url="https://delivery.example.com/audit/callback",
        callback_secret="callback-secret",
        metadata={"compat_mode": "content_risk", "data_id": "episode-004"},
    )

    asyncio.run(tasks.send_review_callback("review_004", request, "completed"))

    assert len(sent) == 1
    parsed = urlparse(sent[0]["url"])
    query = parse_qs(parsed.query)
    assert query["data_id"] == ["episode-004"]
    assert query["status"] == ["completed"]
    expected_sig = hmac.new(b"callback-secret", b"episode-004", hashlib.sha256).hexdigest()
    assert query["sig"] == [expected_sig]
    payload = json.loads(sent[0]["content"])
    assert payload == {"data_id": "episode-004", "task_id": "episode-004", "status": "completed"}
    assert "report" not in payload
