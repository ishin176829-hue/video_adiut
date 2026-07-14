from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .models import PlatformCreateReviewRequest, ReviewFinding, ReviewStatus, VideoReviewReport


COMPAT_MODE = "content_risk"


class ContentRiskTaskRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    data_id: str = Field(validation_alias=AliasChoices("data_id", "DataId", "dataID", "DataID"))
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("parameters", "Parameters"),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("parameters", mode="before")
    @classmethod
    def parse_parameters(cls, value: Any) -> dict[str, Any]:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("parameters JSON must be an object")
            return parsed
        return value


class ContentRiskBatchResultRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    data_ids: list[str] = Field(
        validation_alias=AliasChoices("data_ids", "DataIds", "dataIDs", "DataIDs"),
        min_length=1,
        max_length=500,
    )

    @field_validator("data_ids")
    @classmethod
    def clean_data_ids(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("data_ids 不能为空")
        return cleaned


def parameter_value(parameters: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in parameters:
            return parameters[name]
    normalized = {_normalize_key(key): value for key, value in parameters.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized:
            return normalized[key]
    return None


def _normalize_key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def to_platform_review_request(request: ContentRiskTaskRequest) -> PlatformCreateReviewRequest:
    parameters = request.parameters or {}
    title = parameter_value(parameters, "title", "Title", "video_title", "VideoTitle")
    callback_url = parameter_value(parameters, "callback_url", "CallbackUrl", "callbackUrl")
    callback_secret = parameter_value(parameters, "callback_secret", "CallbackSecret", "callbackSecret")
    interval = parameter_value(parameters, "interval", "Interval", "fps", "Fps", "FPS")
    metadata = dict(request.metadata or {})
    metadata.update(
        {
            "compat_mode": COMPAT_MODE,
            "data_id": request.data_id,
        }
    )
    return PlatformCreateReviewRequest(
        platform_task_id=request.data_id,
        video_url=parameter_value(parameters, "video_url", "VideoUrl", "videoUrl", "source_url", "SourceUrl"),
        oss_bucket=parameter_value(parameters, "oss_bucket", "OssBucket", "ossBucket"),
        oss_key=parameter_value(parameters, "oss_key", "OssKey", "ossKey"),
        oss_endpoint=parameter_value(parameters, "oss_endpoint", "OssEndpoint", "ossEndpoint"),
        oss_etag=parameter_value(parameters, "oss_etag", "OssEtag", "ossETag"),
        oss_size=parameter_value(parameters, "oss_size", "OssSize", "ossSize"),
        video_title=str(title) if title is not None else None,
        drama_title=str(parameter_value(parameters, "drama_title", "DramaTitle", "dramaTitle") or title or ""),
        uploader_info=str(parameter_value(parameters, "uploader_info", "UploaderInfo", "uploaderInfo") or ""),
        fps=_safe_int(interval, default=1, minimum=1, maximum=10),
        segment_seconds=_optional_int(parameter_value(parameters, "segment_seconds", "SegmentSeconds", "segmentSeconds")),
        start_seconds=_optional_int(parameter_value(parameters, "start_seconds", "StartSeconds", "startSeconds")),
        end_seconds=_optional_int(parameter_value(parameters, "end_seconds", "EndSeconds", "endSeconds")),
        callback_url=str(callback_url) if callback_url is not None else None,
        callback_secret=str(callback_secret) if callback_secret is not None else None,
        metadata=metadata,
    )


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def task_response(*, data_id: str, review_id: str, status: ReviewStatus | str, idempotent: bool = False) -> dict[str, Any]:
    return {
        "code": 0,
        "message": "success",
        "data_id": data_id,
        "task_id": data_id,
        "review_id": review_id,
        "status": compat_status(status),
        "idempotent": idempotent,
    }


def compat_status(status: ReviewStatus | str) -> str:
    raw = status.value if isinstance(status, ReviewStatus) else str(status)
    return {
        "pending": "submitted",
        "processing": "processing",
        "completed": "completed",
        "failed": "failed",
        "source_unavailable": "failed",
        "cancelled": "cancelled",
    }.get(raw, raw)


def empty_result(*, data_id: str, status: ReviewStatus | str, error_message: str | None = None) -> dict[str, Any]:
    payload = {
        "code": 0,
        "message": "success",
        "data_id": data_id,
        "task_id": data_id,
        "status": compat_status(status),
        "final_label": "",
        "decision_label": "",
        "video_results": {"decision": "", "frames": []},
        "audio_results": {"decision": "", "details": []},
        "annotations": [],
    }
    if error_message:
        payload["error_message"] = error_message
    return payload


def report_result(*, data_id: str, status: ReviewStatus | str, report: VideoReviewReport) -> dict[str, Any]:
    decision = decision_label(report.decision)
    frames = [_finding_frame(finding) for finding in report.findings if finding_decision(finding) in {"BLOCK", "REVIEW"}]
    details = [
        _finding_audio_detail(finding)
        for finding in report.findings
        if finding_decision(finding) in {"BLOCK", "REVIEW"} and _is_text_or_audio_finding(finding)
    ]
    annotations = [_annotation("视频", finding) for finding in report.findings if finding_decision(finding) in {"BLOCK", "REVIEW"}]
    return {
        "code": 0,
        "message": "success",
        "data_id": data_id,
        "task_id": data_id,
        "status": compat_status(status),
        "final_label": decision,
        "decision_label": decision,
        "summary": report.summary,
        "risk_score": report.risk_score,
        "video_results": {
            "decision": decision,
            "frames": frames,
        },
        "audio_results": {
            "decision": "REVIEW" if details and decision != "BLOCK" else ("BLOCK" if decision == "BLOCK" and details else "PASS"),
            "details": details,
        },
        "annotations": annotations,
    }


def decision_label(decision: str | None) -> str:
    return {
        "pass": "PASS",
        "warn": "REVIEW",
        "manual_review": "REVIEW",
        "reject": "BLOCK",
    }.get(str(decision or "").lower(), "")


def finding_decision(finding: ReviewFinding) -> str:
    severity = str(finding.severity or "").lower()
    if severity in {"critical", "high"}:
        return "BLOCK"
    if severity in {"medium", "low"}:
        return "REVIEW"
    return "REVIEW"


def parse_time_seconds(value: str | None) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    if ":" not in raw:
        try:
            return float(raw)
        except ValueError:
            return 0.0
    parts = raw.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds
    return 0.0


def _finding_frame(finding: ReviewFinding) -> dict[str, Any]:
    return {
        "time": parse_time_seconds(finding.start_time),
        "end_time": parse_time_seconds(finding.end_time),
        "label": finding.category,
        "sub_label": finding.sub_category,
        "decision": finding_decision(finding),
        "decision_detail": _decision_detail(finding),
        "risk_level": finding.risk_level,
        "confidence": finding.confidence,
    }


def _finding_audio_detail(finding: ReviewFinding) -> dict[str, Any]:
    return {
        "start_time": parse_time_seconds(finding.start_time),
        "end_time": parse_time_seconds(finding.end_time),
        "label": finding.category,
        "sub_label": finding.sub_category,
        "decision": finding_decision(finding),
        "decision_detail": _decision_detail(finding),
        "text": finding.original_text,
        "confidence": finding.confidence,
    }


def _is_text_or_audio_finding(finding: ReviewFinding) -> bool:
    text = " ".join(
        [
            finding.category,
            finding.sub_category,
            finding.evidence,
            finding.reason,
            finding.original_text,
        ]
    ).lower()
    return any(token in text for token in ["字幕", "台词", "文本", "音频", "dialog", "subtitle", "audio", "text"])


def _decision_detail(finding: ReviewFinding) -> str:
    if finding.evidence and finding.reason:
        return f"{finding.evidence}；{finding.reason}"
    return finding.evidence or finding.reason or finding.suggested_action


def _annotation(kind: str, finding: ReviewFinding) -> str:
    start = parse_time_seconds(finding.start_time)
    label = finding.category
    if finding.sub_category:
        label = f"{label}-{finding.sub_category}"
    return f"{kind}{start:g}秒 命中 {label}：{_decision_detail(finding)}"


def is_compat_callback(metadata: dict[str, Any] | None) -> bool:
    return bool(metadata and metadata.get("compat_mode") == COMPAT_MODE)


def build_callback_notification(
    *,
    callback_url: str,
    callback_secret: str | None,
    app_id: str,
    data_id: str,
    status: str,
) -> tuple[str, bytes, dict[str, str]]:
    query = {"data_id": data_id, "status": compat_status(status)}
    if callback_secret:
        query["sig"] = hmac.new(callback_secret.encode("utf-8"), data_id.encode("utf-8"), hashlib.sha256).hexdigest()
    url = _append_query(callback_url, query)
    payload = {
        "data_id": data_id,
        "task_id": data_id,
        "status": compat_status(status),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return url, body, {"Content-Type": "application/json", "X-App-Id": app_id}


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))
