from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import settings
from .models import ReviewJob, ReviewStatus


TERMINAL_STATUSES = {
    ReviewStatus.COMPLETED,
    ReviewStatus.FAILED,
    ReviewStatus.CANCELLED,
    ReviewStatus.SOURCE_UNAVAILABLE,
}


@dataclass
class CleanupItem:
    path: str
    reason: str
    bytes: int = 0
    deleted: bool = False


@dataclass
class CleanupResult:
    dry_run: bool
    disk_used_percent: float
    watermark_action: str
    items: list[CleanupItem] = field(default_factory=list)

    @property
    def deleted_count(self) -> int:
        return sum(1 for item in self.items if item.deleted)

    @property
    def deleted_bytes(self) -> int:
        return sum(item.bytes for item in self.items if item.deleted)

    @property
    def candidate_count(self) -> int:
        return len(self.items)


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: str | None, *, now: datetime) -> float:
    return (now - _parse_dt(value)).total_seconds() / 3600


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_raw_delete_target(local_path: str | None) -> Path | None:
    if not local_path:
        return None
    path = Path(local_path)
    if not _is_relative_to(path, settings.raw_dir):
        return None
    uploads_dir = settings.raw_dir / "uploads"
    if _is_relative_to(path, uploads_dir):
        return path if path.exists() else None
    if path.parent.parent == settings.raw_dir and path.parent.name.startswith("video_"):
        return path.parent if path.parent.exists() else None
    return path if path.exists() and path.is_file() else None


def _safe_derived_delete_target(video_id: str | None) -> Path | None:
    if not video_id:
        return None
    target = settings.derived_dir / video_id
    if not _is_relative_to(target, settings.derived_dir):
        return None
    return target if target.exists() else None


def _delete_path(path: Path, *, dry_run: bool) -> bool:
    if dry_run:
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return True


def _load_jobs() -> Iterable[ReviewJob]:
    if not settings.jobs_dir.exists():
        return []
    jobs: list[ReviewJob] = []
    for path in settings.jobs_dir.glob("*.json"):
        try:
            jobs.append(ReviewJob.model_validate_json(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            continue
    return jobs


def _disk_used_percent() -> float:
    settings.ensure_dirs()
    usage = shutil.disk_usage(settings.data_dir)
    return usage.used / usage.total * 100


def _watermark_ttls(
    *,
    raw_ttl_hours: float,
    derived_ttl_hours: float,
    disk_used_percent: float,
) -> tuple[float, float, str]:
    if disk_used_percent >= 90:
        return 0, 0, "emergency"
    if disk_used_percent >= 85:
        return min(raw_ttl_hours, 2), min(derived_ttl_hours, 2), "critical"
    if disk_used_percent >= 75:
        return min(raw_ttl_hours, 12), min(derived_ttl_hours, 12), "warning"
    return raw_ttl_hours, derived_ttl_hours, "normal"


def _add_candidate(result: CleanupResult, path: Path, reason: str, *, dry_run: bool) -> None:
    size = _path_size(path)
    deleted = _delete_path(path, dry_run=dry_run)
    result.items.append(CleanupItem(path=str(path), reason=reason, bytes=size, deleted=deleted))


def cleanup_review_artifacts(
    job: ReviewJob,
    *,
    raw_ttl_hours: float | None = None,
    derived_ttl_hours: float | None = None,
    disk_used_percent: float | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> CleanupResult:
    settings.ensure_dirs()
    now = now or datetime.now(timezone.utc)
    measured_percent = _disk_used_percent() if disk_used_percent is None else disk_used_percent
    raw_ttl = settings.cleanup_raw_ttl_hours if raw_ttl_hours is None else raw_ttl_hours
    derived_ttl = settings.cleanup_derived_ttl_hours if derived_ttl_hours is None else derived_ttl_hours
    effective_raw_ttl, effective_derived_ttl, action = _watermark_ttls(
        raw_ttl_hours=raw_ttl,
        derived_ttl_hours=derived_ttl,
        disk_used_percent=measured_percent,
    )
    result = CleanupResult(dry_run=dry_run, disk_used_percent=measured_percent, watermark_action=action)
    if job.status not in TERMINAL_STATUSES:
        return result
    age = _age_hours(job.updated_at, now=now)
    if age >= effective_raw_ttl:
        raw_target = _safe_raw_delete_target(job.local_path)
        if raw_target:
            _add_candidate(result, raw_target, f"terminal_raw_{job.status.value}", dry_run=dry_run)
    if age >= effective_derived_ttl:
        derived_target = _safe_derived_delete_target(job.video_id)
        if derived_target:
            _add_candidate(result, derived_target, f"terminal_derived_{job.status.value}", dry_run=dry_run)
    return result


def cleanup_storage(
    *,
    raw_ttl_hours: float | None = None,
    derived_ttl_hours: float | None = None,
    upload_session_ttl_hours: float | None = None,
    disk_used_percent: float | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> CleanupResult:
    settings.ensure_dirs()
    now = now or datetime.now(timezone.utc)
    measured_percent = _disk_used_percent() if disk_used_percent is None else disk_used_percent
    raw_ttl = settings.cleanup_raw_ttl_hours if raw_ttl_hours is None else raw_ttl_hours
    derived_ttl = settings.cleanup_derived_ttl_hours if derived_ttl_hours is None else derived_ttl_hours
    upload_ttl = settings.cleanup_upload_session_ttl_hours if upload_session_ttl_hours is None else upload_session_ttl_hours
    effective_raw_ttl, effective_derived_ttl, action = _watermark_ttls(
        raw_ttl_hours=raw_ttl,
        derived_ttl_hours=derived_ttl,
        disk_used_percent=measured_percent,
    )
    result = CleanupResult(dry_run=dry_run, disk_used_percent=measured_percent, watermark_action=action)

    for session_dir in settings.upload_sessions_dir.glob("upload_session_*"):
        if not session_dir.is_dir():
            continue
        age = (now.timestamp() - session_dir.stat().st_mtime) / 3600
        if age >= upload_ttl:
            _add_candidate(result, session_dir, "stale_upload_session", dry_run=dry_run)

    for job in _load_jobs():
        review_result = cleanup_review_artifacts(
            job,
            raw_ttl_hours=effective_raw_ttl,
            derived_ttl_hours=effective_derived_ttl,
            disk_used_percent=0,
            dry_run=dry_run,
            now=now,
        )
        result.items.extend(review_result.items)

    return result
