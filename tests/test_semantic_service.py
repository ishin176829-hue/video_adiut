import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

os.environ["VIDEO_REVIEW_DATA_DIR"] = tempfile.mkdtemp(prefix="semantic-service-test-")

from fastapi.testclient import TestClient

from video_review.config import settings
from video_review.semantic_service import (
    CreateSemanticJobRequest,
    app,
    dispatch_semantic_job,
    get_semantic_job,
    make_semantic_job,
    save_semantic_job,
)


async def complete_job(job_id: str) -> None:
    job = get_semantic_job(job_id)
    job.status = "completed"
    job.progress = {"completed": len(job.videos), "total": len(job.videos)}
    for video in job.videos:
        video.status = "completed"
        video.output_path = f"/tmp/{video.video_id}.json"
        video.frames_sent = 10
        video.segments = 1
    save_semantic_job(job)


def test_create_semantic_job_from_local_paths(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"fake-video")
    monkeypatch.setenv("SEMANTIC_SERVICE_RUN_INLINE", "1")
    monkeypatch.setattr("video_review.semantic_service.run_semantic_job", complete_job)

    client = TestClient(app)
    response = client.post(
        "/semantic/jobs",
        json={
            "local_paths": [str(video_path)],
            "titles": ["第一集"],
            "segment_seconds": 240,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    stored = client.get(f"/semantic/jobs/{payload['job_id']}").json()
    assert stored["status"] == "completed"
    assert stored["videos"][0]["title"] == "第一集"
    assert stored["videos"][0]["frames_sent"] == 10


def test_root_page_explains_service_endpoints(tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "SN2S Semantic Video Extraction" in response.text
    assert "/semantic/uploads" in response.text
    assert 'id="upload-form"' in response.text
    assert 'type="file"' in response.text
    assert 'id="jobs-table"' in response.text


def test_upload_semantic_job_saves_files(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    monkeypatch.setenv("SEMANTIC_SERVICE_RUN_INLINE", "1")
    monkeypatch.setattr("video_review.semantic_service.run_semantic_job", complete_job)

    client = TestClient(app)
    response = client.post(
        "/semantic/uploads",
        data={"segment_seconds": "240"},
        files=[
            ("files", ("a.mp4", b"aaa", "video/mp4")),
            ("files", ("b.mp4", b"bbb", "video/mp4")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    stored = client.get(f"/semantic/jobs/{payload['job_id']}").json()
    assert stored["status"] == "completed"
    assert len(stored["videos"]) == 2
    assert Path(stored["videos"][0]["local_path"]).read_bytes() == b"aaa"
    assert Path(stored["videos"][1]["local_path"]).read_bytes() == b"bbb"


def test_semantic_job_result_loads_completed_outputs(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    output = tmp_path / "result.json"
    output.write_text('{"title": "demo", "segments": []}', encoding="utf-8")

    async def complete_with_output(job_id: str) -> None:
        job = get_semantic_job(job_id)
        job.status = "completed"
        job.videos[0].status = "completed"
        job.videos[0].output_path = str(output)
        save_semantic_job(job)

    monkeypatch.setattr("video_review.semantic_service.run_semantic_job", complete_with_output)
    monkeypatch.setenv("SEMANTIC_SERVICE_RUN_INLINE", "1")
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"fake-video")

    client = TestClient(app)
    created = client.post("/semantic/jobs", json={"local_paths": [str(video_path)]}).json()
    result = client.get(f"/semantic/jobs/{created['job_id']}/result")

    assert result.status_code == 200
    assert result.json()["results"][0]["payload"]["title"] == "demo"


def test_dispatch_semantic_job_keeps_event_loop_responsive(monkeypatch, tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    monkeypatch.delenv("SEMANTIC_SERVICE_RUN_INLINE", raising=False)
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"fake-video")
    job = make_semantic_job(
        CreateSemanticJobRequest(
            local_paths=[str(video_path)],
            titles=["demo"],
            model="gemini-3.1-flash-lite",
            fps=1,
            segment_seconds=240,
        )
    )

    def slow_extract(*args, **kwargs):
        time.sleep(0.25)
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="slow failure")

    monkeypatch.setattr("video_review.semantic_service.run_extract_command", slow_extract)

    async def exercise() -> float:
        await dispatch_semantic_job(job.job_id)
        start = time.perf_counter()
        await asyncio.sleep(0)
        elapsed = time.perf_counter() - start
        for _ in range(20):
            if get_semantic_job(job.job_id).videos[0].status == "failed":
                break
            await asyncio.sleep(0.02)
        return elapsed

    elapsed = asyncio.run(exercise())
    assert elapsed < 0.1
