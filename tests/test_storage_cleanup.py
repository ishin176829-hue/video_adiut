import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from video_review.cleanup import cleanup_review_artifacts, cleanup_storage
from video_review.config import settings
from video_review.models import ReviewJob, ReviewStatus
from video_review.store import store


def _iso(hours_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _write_job(tmp_path: Path, *, review_id: str, status: ReviewStatus, hours_ago: int, video_id: str) -> ReviewJob:
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    job = ReviewJob(
        review_id=review_id,
        status=status,
        phase="done" if status == ReviewStatus.COMPLETED else "scan",
        message="test",
        video_id=video_id,
        local_path=str(settings.raw_dir / video_id / "demo.mp4"),
        report_path=str(settings.reports_dir / f"{review_id}.json"),
        updated_at=_iso(hours_ago),
    )
    store.save_job(job)
    path = store.job_path(review_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = _iso(hours_ago)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ReviewJob.model_validate(payload)


def test_cleanup_deletes_terminal_raw_and_derived_after_ttl(tmp_path):
    job = _write_job(
        tmp_path,
        review_id="review_old_done",
        status=ReviewStatus.COMPLETED,
        hours_ago=30,
        video_id="video_old_done",
    )
    raw_file = Path(job.local_path)
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(b"video")
    derived_file = settings.derived_dir / job.video_id / "frames" / "0001.jpg"
    derived_file.parent.mkdir(parents=True)
    derived_file.write_bytes(b"frame")
    report_file = settings.reports_dir / f"{job.review_id}.json"
    report_file.write_text("{}", encoding="utf-8")

    result = cleanup_storage(raw_ttl_hours=24, derived_ttl_hours=24, dry_run=False)

    assert not raw_file.parent.exists()
    assert not derived_file.parent.parent.exists()
    assert report_file.exists()
    assert result.deleted_count == 2


def test_cleanup_keeps_processing_and_recent_terminal_files(tmp_path):
    processing = _write_job(
        tmp_path,
        review_id="review_processing",
        status=ReviewStatus.PROCESSING,
        hours_ago=100,
        video_id="video_processing",
    )
    recent = _write_job(
        tmp_path,
        review_id="review_recent",
        status=ReviewStatus.COMPLETED,
        hours_ago=2,
        video_id="video_recent",
    )
    for job in [processing, recent]:
        raw_file = Path(job.local_path)
        raw_file.parent.mkdir(parents=True)
        raw_file.write_bytes(b"video")
        derived_file = settings.derived_dir / job.video_id / "frames" / "0001.jpg"
        derived_file.parent.mkdir(parents=True)
        derived_file.write_bytes(b"frame")

    result = cleanup_storage(raw_ttl_hours=24, derived_ttl_hours=24, dry_run=False)

    assert Path(processing.local_path).exists()
    assert Path(recent.local_path).exists()
    assert result.deleted_count == 0


def test_cleanup_uses_emergency_ttl_when_disk_watermark_is_high(tmp_path):
    job = _write_job(
        tmp_path,
        review_id="review_emergency",
        status=ReviewStatus.COMPLETED,
        hours_ago=3,
        video_id="video_emergency",
    )
    raw_file = Path(job.local_path)
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(b"video")

    result = cleanup_storage(
        raw_ttl_hours=24,
        derived_ttl_hours=24,
        disk_used_percent=86,
        dry_run=False,
    )

    assert not raw_file.parent.exists()
    assert result.watermark_action == "critical"


def test_cleanup_removes_stale_upload_sessions_but_keeps_recent(tmp_path):
    settings.data_dir = tmp_path
    settings.ensure_dirs()
    stale = settings.upload_sessions_dir / "upload_session_stale"
    recent = settings.upload_sessions_dir / "upload_session_recent"
    stale.mkdir(parents=True)
    recent.mkdir(parents=True)
    (stale / "metadata.json").write_text("{}", encoding="utf-8")
    (recent / "metadata.json").write_text("{}", encoding="utf-8")
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=25)).timestamp()
    for path in [stale, stale / "metadata.json"]:
        path.touch()
        path.chmod(0o700)
        import os

        os.utime(path, (stale_time, stale_time))

    result = cleanup_storage(upload_session_ttl_hours=24, dry_run=False)

    assert not stale.exists()
    assert recent.exists()
    assert any(item.reason == "stale_upload_session" for item in result.items)


def test_cleanup_review_artifacts_only_scans_one_terminal_job(tmp_path):
    job = _write_job(
        tmp_path,
        review_id="review_single",
        status=ReviewStatus.COMPLETED,
        hours_ago=30,
        video_id="video_single",
    )
    other = _write_job(
        tmp_path,
        review_id="review_other",
        status=ReviewStatus.COMPLETED,
        hours_ago=30,
        video_id="video_other",
    )
    for candidate in [job, other]:
        raw_file = Path(candidate.local_path)
        raw_file.parent.mkdir(parents=True)
        raw_file.write_bytes(b"video")

    result = cleanup_review_artifacts(job, raw_ttl_hours=24, derived_ttl_hours=24, dry_run=False)

    assert not Path(job.local_path).parent.exists()
    assert Path(other.local_path).exists()
    assert result.deleted_count == 1
