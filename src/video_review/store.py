from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings
from .models import ReviewJob, ReviewStatus, VideoReviewReport


class ReviewStore:
    def __init__(self) -> None:
        settings.ensure_dirs()

    def job_path(self, review_id: str) -> Path:
        return settings.jobs_dir / f"{review_id}.json"

    def event_path(self, review_id: str) -> Path:
        return settings.events_dir / f"{review_id}.jsonl"

    def report_path(self, review_id: str) -> Path:
        return settings.reports_dir / f"{review_id}.json"

    def save_job(self, job: ReviewJob) -> None:
        job.updated_at = datetime.now().isoformat()
        self.job_path(job.review_id).write_text(job.model_dump_json(indent=2), encoding="utf-8")

    def get_job(self, review_id: str) -> ReviewJob | None:
        path = self.job_path(review_id)
        if not path.exists():
            return None
        return ReviewJob.model_validate_json(path.read_text(encoding="utf-8"))

    def update_job(
        self,
        review_id: str,
        *,
        status: ReviewStatus | None = None,
        phase: str | None = None,
        message: str | None = None,
        progress: dict | None = None,
        error: str | None = None,
        report_path: str | None = None,
        local_path: str | None = None,
        video_id: str | None = None,
        upload_started_at: str | None = None,
        upload_completed_at: str | None = None,
    ) -> ReviewJob | None:
        job = self.get_job(review_id)
        if not job:
            return None
        if status is not None:
            job.status = status
        if phase is not None:
            job.phase = phase
        if message is not None:
            job.message = message
        if progress is not None:
            job.progress = progress
        if error is not None:
            job.error = error
        if report_path is not None:
            job.report_path = report_path
        if local_path is not None:
            job.local_path = local_path
        if video_id is not None:
            job.video_id = video_id
        if upload_started_at is not None:
            job.upload_started_at = upload_started_at
        if upload_completed_at is not None:
            job.upload_completed_at = upload_completed_at
        self.save_job(job)
        return job

    def add_event(self, review_id: str, event_type: str, data: dict[str, Any]) -> None:
        payload = {
            "ts": datetime.now().isoformat(),
            "type": event_type,
            "data": data,
        }
        with self.event_path(review_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_events(self, review_id: str, offset: int = 0) -> tuple[list[dict], int]:
        path = self.event_path(review_id)
        if not path.exists():
            return [], offset
        events = []
        with path.open(encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                if line.strip():
                    events.append(json.loads(line))
            return events, f.tell()

    def save_report(self, report: VideoReviewReport) -> Path:
        path = self.report_path(report.review_id)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return path

    def get_report(self, review_id: str) -> VideoReviewReport | None:
        path = self.report_path(review_id)
        if not path.exists():
            return None
        return VideoReviewReport.model_validate_json(path.read_text(encoding="utf-8"))


store = ReviewStore()
