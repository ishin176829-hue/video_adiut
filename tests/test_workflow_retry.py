from datetime import datetime, timedelta, timezone

from video_review.models import CreateReviewRequest
from video_review.workflow import plan_stage_retry


def test_plan_stage_retry_increments_attempt_and_preserves_deadline():
    started_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    now = started_at + timedelta(minutes=1)
    request = CreateReviewRequest(video_url="https://example.com/a.mp4")

    first = plan_stage_retry(
        request,
        stage="model",
        reason="429 RESOURCE_EXHAUSTED",
        error_kind="rate_limit",
        started_at=started_at,
        now=now,
        deadline_seconds=1800,
        delays=[5, 15, 30],
    )

    assert first is not None
    assert first.attempt == 1
    assert first.delay_seconds == 5
    assert first.deadline_at == started_at + timedelta(minutes=30)
    assert first.request.metadata["model_retry_attempt"] == 1
    assert first.request.metadata["model_retry_reason"] == "429 RESOURCE_EXHAUSTED"
    assert first.request.metadata["model_retry_error_kind"] == "rate_limit"

    second = plan_stage_retry(
        first.request,
        stage="model",
        reason="504 Gateway Time-out",
        error_kind="transient",
        started_at=started_at + timedelta(minutes=10),
        now=now + timedelta(seconds=10),
        deadline_seconds=1800,
        delays=[5, 15, 30],
    )

    assert second is not None
    assert second.attempt == 2
    assert second.delay_seconds == 15
    assert second.deadline_at == first.deadline_at


def test_plan_stage_retry_refuses_to_schedule_after_deadline():
    started_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    request = CreateReviewRequest(video_url="https://example.com/a.mp4")

    plan = plan_stage_retry(
        request,
        stage="model",
        reason="模型熔断中",
        error_kind="circuit",
        started_at=started_at,
        now=started_at + timedelta(minutes=30),
        deadline_seconds=1800,
        delays=[5, 15, 30],
    )

    assert plan is None


def test_plan_stage_retry_caps_delay_before_deadline():
    started_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    request = CreateReviewRequest(video_url="https://example.com/a.mp4")

    plan = plan_stage_retry(
        request,
        stage="model",
        reason="临时错误",
        error_kind="transient",
        started_at=started_at,
        now=started_at + timedelta(minutes=29, seconds=55),
        deadline_seconds=1800,
        delays=[30],
    )

    assert plan is not None
    assert 0 <= plan.delay_seconds < 5
