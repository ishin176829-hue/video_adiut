from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import settings
from .models import CreateReviewRequest


@dataclass(frozen=True)
class WorkflowRetryPlan:
    request: CreateReviewRequest
    stage: str
    attempt: int
    delay_seconds: float
    started_at: datetime
    deadline_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_retry_delays(raw: str | Iterable[float]) -> list[float]:
    values = raw.split(",") if isinstance(raw, str) else raw
    parsed: list[float] = []
    for value in values:
        try:
            parsed.append(max(0.0, float(value)))
        except (TypeError, ValueError):
            continue
    return parsed or [5.0]


def workflow_deadline(
    request: CreateReviewRequest,
    *,
    started_at: datetime,
    deadline_seconds: int | None = None,
) -> tuple[datetime, datetime]:
    metadata = request.metadata
    start = _parse_datetime(metadata.get("workflow_started_at")) or _utc(started_at)
    duration = max(1, deadline_seconds or settings.workflow_deadline_seconds)
    deadline = _parse_datetime(metadata.get("workflow_deadline_at")) or start + timedelta(seconds=duration)
    return start, deadline


def plan_stage_retry(
    request: CreateReviewRequest,
    *,
    stage: str,
    reason: str,
    error_kind: str,
    started_at: datetime,
    now: datetime | None = None,
    deadline_seconds: int | None = None,
    delays: str | Iterable[float] | None = None,
) -> WorkflowRetryPlan | None:
    now = _utc(now or datetime.now(timezone.utc))
    start, deadline = workflow_deadline(
        request,
        started_at=started_at,
        deadline_seconds=deadline_seconds,
    )
    remaining = (deadline - now).total_seconds()
    if remaining <= 0:
        return None

    normalized_stage = str(stage)
    attempt_key = f"{normalized_stage}_retry_attempt"
    attempt = int(request.metadata.get(attempt_key) or 0) + 1
    configured_delays = parse_retry_delays(delays or settings.model_task_retry_delays_seconds)
    configured_delay = configured_delays[min(attempt - 1, len(configured_delays) - 1)]
    delay = min(configured_delay, max(0.0, remaining - 0.001))

    metadata = dict(request.metadata)
    metadata.update(
        {
            "workflow_started_at": start.isoformat(),
            "workflow_deadline_at": deadline.isoformat(),
            attempt_key: attempt,
            f"{normalized_stage}_retry_reason": reason,
            f"{normalized_stage}_retry_error_kind": error_kind,
        }
    )
    return WorkflowRetryPlan(
        request=request.model_copy(update={"metadata": metadata}),
        stage=normalized_stage,
        attempt=attempt,
        delay_seconds=delay,
        started_at=start,
        deadline_at=deadline,
    )
