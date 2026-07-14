from __future__ import annotations

import asyncio
import json
from pathlib import Path
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import types

from .config import settings
from .api_key_pool import get_google_api_key_pool
from .model_retry import ModelContractError, classify_model_error
from .models import ReportNarrative, SegmentPlan, SegmentReviewResult, VideoAsset
from .policies import load_policy
from .prompts import (
    SYSTEM_PROMPT,
    build_frame_sheet_review_prompt,
    build_frames_review_prompt,
    build_narrative_prompt,
    build_review_prompt,
    build_subtitle_review_prompt,
)


VALID_SEVERITIES = {"low", "medium", "high", "critical"}
CONFIDENCE_LABELS = {
    "low": 0.3,
    "低": 0.3,
    "medium": 0.6,
    "中": 0.6,
    "high": 0.9,
    "高": 0.9,
    "critical": 1.0,
    "严重": 1.0,
}
INCEST_TAXONOMY_TERMS = {
    "乱伦",
    "近亲",
    "同父异母",
    "同母异父",
    "继兄妹",
    "继姐弟",
    "继亲",
    "继父",
    "继母",
    "公媳",
    "父女暧昧",
    "母子暧昧",
}
FALLBACK_SEVERITY_BY_CATEGORY = {
    "minor": "critical",
    "ethics": "high",
    "bad_values": "high",
    "public": "high",
    "politic": "high",
    "judiciary": "high",
    "national": "critical",
    "history": "critical",
    "sexual": "medium",
    "lowbrow": "medium",
    "violence": "high",
    "harm": "high",
    "gambling": "high",
    "drugs": "high",
    "crime": "high",
    "brand": "medium",
    "logo": "medium",
    "quality": "low",
    "duplicate": "low",
}


def _normalize_value_advice(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"main": value}
    if not isinstance(value, dict):
        return {}
    return {
        "opening": str(value.get("opening") or ""),
        "main": str(value.get("main") or value.get("advice") or value.get("suggestion") or ""),
        "ending": str(value.get("ending") or ""),
        "overall": str(value.get("overall") or ""),
    }


def _normalize_text(value: Any, *, fallback: str = "") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_normalize_text(item) for item in value]
        return "；".join(part for part in parts if part) or fallback
    if isinstance(value, dict):
        keys = ("main", "overall", "opening", "ending", "advice", "suggestion", "action", "recommendation")
        parts = [_normalize_text(value.get(key)) for key in keys]
        return "；".join(part for part in parts if part) or fallback
    return str(value)


def _normalize_confidence(value: Any) -> float:
    if value is None or value == "":
        return 0.5
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
        label_value = CONFIDENCE_LABELS.get(value.lower())
        if label_value is not None:
            return label_value
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.5
    if confidence > 1:
        confidence = confidence / 10 if confidence <= 10 else confidence / 100
    return max(0, min(1, confidence))


def _normalize_bool(value: Any, *, fallback: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    negative_tokens = ("不通过", "未通过", "拒绝", "驳回", "false", "no", "否", "reject", "fail")
    if any(token in text for token in negative_tokens):
        return False
    positive_tokens = ("通过", "true", "yes", "是", "pass")
    if any(token in text for token in positive_tokens):
        return True
    return fallback


def _normalize_string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, dict):
            text = _normalize_text(
                item.get("category")
                or item.get("name")
                or item.get("label")
                or item.get("id")
                or item
            )
        else:
            text = _normalize_text(item)
        if text:
            output.append(text)
    return output


def _normalize_story_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    genres = value.get("genres") or []
    signals = value.get("signals") or []
    if isinstance(genres, str):
        genres = [genres]
    if isinstance(signals, str):
        signals = [signals]
    return {
        "background": str(value.get("background") or value.get("era") or ""),
        "genres": [str(item) for item in genres if item],
        "signals": [str(item) for item in signals if item],
    }


def _normalize_plot_structure(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = [(str(item.get("phase") or ""), item) for item in value if isinstance(item, dict)]
    elif isinstance(value, dict):
        items = [(str(phase), item) for phase, item in value.items() if isinstance(item, dict)]
    else:
        return []
    output: list[dict[str, Any]] = []
    phase_names = {
        "opening": "开头",
        "middle": "中间",
        "ending": "结尾",
        "开头": "开头",
        "中间": "中间",
        "结尾": "结尾",
    }
    for phase, item in items:
        phase = str(item.get("phase") or phase)
        raw_risk_points = item.get("risk_points") or []
        if not isinstance(raw_risk_points, list):
            raw_risk_points = [raw_risk_points]
        risk_points = []
        for risk_point in raw_risk_points:
            if isinstance(risk_point, dict):
                parts = [
                    _normalize_text(risk_point.get(key))
                    for key in ("category", "sub_category", "time_code", "time_range", "evidence", "reason")
                ]
                text = "；".join(part for part in parts if part)
            else:
                text = _normalize_text(risk_point)
            if text:
                risk_points.append(text)
        normalized = {
            "phase": phase,
            "phase_name": _normalize_text(item.get("phase_name"), fallback=phase_names.get(phase, phase)),
            "time_range": _normalize_text(item.get("time_range")),
            "plot_summary": _normalize_text(item.get("plot_summary")),
            "value_judgement": _normalize_text(item.get("value_judgement")),
            "risk_points": risk_points,
            "correction_advice": _normalize_text(item.get("correction_advice")),
        }
        output.append(normalized)
    return output


def _normalize_final_verdict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "passed": _normalize_bool(value.get("passed"), fallback=True),
        "conclusion": _normalize_text(value.get("conclusion")),
        "reason": _normalize_text(value.get("reason")),
        "high_risk_categories": _normalize_string_list(value.get("high_risk_categories")),
        "medium_risk_categories": _normalize_string_list(value.get("medium_risk_categories")),
    }


def _response_text(response: Any) -> str:
    try:
        text = response.text
    except Exception as exc:
        raise ModelContractError("模型响应文本不可读取", kind="parse") from exc
    if not isinstance(text, str) or not text.strip():
        raise ModelContractError("模型返回空内容", kind="parse")
    return text.strip()


def _json_payload(text: str) -> dict[str, Any]:
    """Parse model JSON and tolerate accidental prose/fenced wrapping."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ModelContractError("模型返回的 JSON 顶层必须是对象", kind="validation")
    return payload


def _infer_severity(category: str, policy) -> str:
    category_key = (category or "").strip()
    by_id = {item.id: item.severity for item in policy.categories}
    by_name = {item.name: item.severity for item in policy.categories}
    if category_key in by_id:
        return by_id[category_key]
    if category_key in by_name:
        return by_name[category_key]
    lowered = category_key.lower()
    for token, severity in FALLBACK_SEVERITY_BY_CATEGORY.items():
        if token in lowered:
            return severity
    return "medium"


def _contains_incest_taxonomy_signal(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values)
    return any(term in text for term in INCEST_TAXONOMY_TERMS)


def normalize_segment_payload(text: str, segment: SegmentPlan) -> SegmentReviewResult:
    """Fill required schema fields that multimodal providers sometimes omit."""
    policy = load_policy()
    payload = _json_payload(text)
    payload["segment_index"] = segment.segment_index
    payload["start_time"] = segment.start_time
    payload["end_time"] = segment.end_time
    payload.setdefault("summary", "")
    payload.setdefault("risk_score", 0)

    raw_findings = payload.get("findings") or []
    if not isinstance(raw_findings, list):
        raw_findings = []

    normalized_findings = []
    for finding in raw_findings:
        if not isinstance(finding, dict):
            continue
        category = str(finding.get("category") or "unknown")
        sub_category = finding.get("sub_category") or category
        risk_level = finding.get("risk_level") or ""
        rule_tag = finding.get("rule_tag") or ""
        if _contains_incest_taxonomy_signal(
            category,
            sub_category,
            finding.get("evidence"),
            finding.get("original_text"),
            finding.get("context_note"),
        ):
            category = "不良价值观"
            sub_category = "乱伦/近亲伦理"
            risk_level = risk_level or "高风险"
            rule_tag = rule_tag or "禁止"
        severity = str(finding.get("severity") or "").lower()
        if severity not in VALID_SEVERITIES:
            severity = _infer_severity(category, policy)
        if category == "不良价值观" and sub_category == "乱伦/近亲伦理":
            severity = "high"
        normalized = {
            **finding,
            "category": category,
            "sub_category": sub_category,
            "risk_level": risk_level,
            "rule_tag": rule_tag,
            "severity": severity,
            "start_time": finding.get("start_time") or payload["start_time"],
            "end_time": finding.get("end_time") or payload["end_time"],
            "evidence": _normalize_text(finding.get("evidence")),
            "reason": _normalize_text(finding.get("reason")),
            "suggested_action": _normalize_text(
                finding.get("suggested_action")
                or finding.get("action")
                or finding.get("recommendation")
                or "人工复核",
                fallback="人工复核",
            ),
            "original_text": _normalize_text(finding.get("original_text")),
            "context_note": _normalize_text(finding.get("context_note")),
            "plot_impact": _normalize_text(finding.get("plot_impact")),
            "value_correction_advice": _normalize_value_advice(finding.get("value_correction_advice")),
            "confidence": _normalize_confidence(finding.get("confidence", 0.5)),
        }
        normalized_findings.append(normalized)
    payload["findings"] = normalized_findings
    return SegmentReviewResult.model_validate(payload)


def normalize_narrative_payload(text: str) -> ReportNarrative:
    payload = _json_payload(text)
    payload["main_plot"] = _normalize_text(payload.get("main_plot"))
    payload["story_context"] = _normalize_story_context(payload.get("story_context"))
    payload["plot_structure"] = _normalize_plot_structure(payload.get("plot_structure"))
    payload["value_correction_advice"] = _normalize_value_advice(payload.get("value_correction_advice"))
    payload["final_verdict"] = _normalize_final_verdict(payload.get("final_verdict"))
    payload["overall_summary"] = _normalize_text(payload.get("overall_summary"))
    return ReportNarrative.model_validate(payload)


class MultimodalAnalyzer:
    def __init__(self, model: str | None = None, fps: int = 1) -> None:
        self.model = model or settings.video_review_model
        self.fps = max(1, min(int(fps), 10))
        http_options: dict[str, Any] | None = None
        if settings.google_api_base_url:
            http_options = {"base_url": settings.google_api_base_url}
        self._http_options = http_options
        self._clients: dict[str, Any] = {}

    def _client_for_key(self, api_key: str):
        client = self._clients.get(api_key)
        if client is None:
            client = genai.Client(api_key=api_key, http_options=self._http_options)
            self._clients[api_key] = client
        return client

    async def _generate_content(self, *, contents, config, parser: Callable[[Any], Any] | None = None):
        pool = get_google_api_key_pool()
        lease = await pool.acquire()
        client = self._client_for_key(lease.api_key)
        try:
            response = await client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            result = parser(response) if parser is not None else response
        except Exception as exc:
            try:
                await pool.release(lease, success=False, error_kind=classify_model_error(exc))
            except Exception:
                pass
            raise
        try:
            await pool.release(lease, success=True)
        except Exception:
            pass
        return result

    def _safety_settings(self) -> list[types.SafetySetting] | None:
        threshold_name = (settings.gemini_safety_threshold or "").strip().upper()
        if not threshold_name:
            return None
        threshold = getattr(types.HarmBlockThreshold, threshold_name, None)
        if threshold is None:
            threshold = types.HarmBlockThreshold.BLOCK_ONLY_HIGH
        categories = [
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
            types.HarmCategory.HARM_CATEGORY_IMAGE_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_IMAGE_HATE,
            types.HarmCategory.HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT,
        ]
        return [types.SafetySetting(category=category, threshold=threshold) for category in categories]

    def _json_config(self, response_schema):
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=response_schema,
            temperature=0.2,
            safety_settings=self._safety_settings(),
        )

    async def _upload_video_with_lease(self, path: str):
        pool = get_google_api_key_pool()
        lease = await pool.acquire()
        client = self._client_for_key(lease.api_key)
        try:
            file = await asyncio.to_thread(client.files.upload, file=path)
            while file.state == "PROCESSING":
                await asyncio.sleep(2)
                file = await asyncio.to_thread(client.files.get, name=file.name)
            if file.state == "FAILED":
                raise RuntimeError(f"Gemini file processing failed: {getattr(file, 'error', None)}")
            return file, client, lease
        except Exception as exc:
            await pool.release(lease, success=False, error_kind=classify_model_error(exc))
            raise

    async def upload_video(self, path: str):
        file, client, lease = await self._upload_video_with_lease(path)
        await get_google_api_key_pool().release(lease, success=True)
        return file, client

    def _video_part(self, file):
        part = types.Part.from_uri(file_uri=file.uri, mime_type=file.mime_type or "video/mp4")
        part.video_metadata = types.VideoMetadata(fps=self.fps)
        return part

    async def analyze_segment(
        self,
        asset: VideoAsset,
        segment: SegmentPlan,
        *,
        video_title: str | None = None,
    ) -> SegmentReviewResult:
        policy = load_policy()
        pool = get_google_api_key_pool()
        file, client, lease = await self._upload_video_with_lease(asset.local_path)
        prompt = build_review_prompt(policy, segment, video_title)
        config = self._json_config(SegmentReviewResult)
        try:
            response = await client.aio.models.generate_content(
                model=self.model,
                contents=[self._video_part(file), prompt],
                config=config,
            )
        except Exception as exc:
            try:
                await pool.release(lease, success=False, error_kind=classify_model_error(exc))
            except Exception:
                pass
            raise
        try:
            result = normalize_segment_payload(_response_text(response), segment)
        except Exception as exc:
            try:
                await pool.release(lease, success=False, error_kind=classify_model_error(exc))
            except Exception:
                pass
            raise
        try:
            await pool.release(lease, success=True)
        except Exception:
            pass
        return result

    async def synthesize_narrative_report(
        self,
        segments: list[SegmentReviewResult],
        *,
        video_title: str | None = None,
    ) -> ReportNarrative:
        policy = load_policy()
        prompt = build_narrative_prompt(policy, segments, video_title)
        config = self._json_config(ReportNarrative)
        return await self._generate_content(
            contents=[prompt],
            config=config,
            parser=lambda response: normalize_narrative_payload(_response_text(response)),
        )

    async def analyze_frames_segment(
        self,
        frames: list[dict],
        segment: SegmentPlan,
        *,
        video_title: str | None = None,
    ) -> SegmentReviewResult:
        policy = load_policy()
        prompt = build_frames_review_prompt(policy, segment, video_title, len(frames))
        parts = [types.Part.from_text(text=prompt)]
        for frame in frames:
            frame_path = Path(frame["path"])
            parts.append(types.Part.from_text(text=f"FRAME timestamp={frame['timestamp']} index={frame['frame_index']}"))
            parts.append(types.Part.from_bytes(data=frame_path.read_bytes(), mime_type="image/jpeg"))
        config = self._json_config(SegmentReviewResult)
        return await self._generate_content(
            contents=parts,
            config=config,
            parser=lambda response: normalize_segment_payload(_response_text(response), segment),
        )

    async def analyze_frame_sheets_segment(
        self,
        sheets: list[dict],
        segment: SegmentPlan,
        *,
        video_title: str | None = None,
    ) -> SegmentReviewResult:
        policy = load_policy()
        frame_count = sum(int(sheet.get("frame_count") or 0) for sheet in sheets)
        prompt = build_frame_sheet_review_prompt(policy, segment, video_title, len(sheets), frame_count)
        parts = [types.Part.from_text(text=prompt)]
        for sheet_index, sheet in enumerate(sheets, start=1):
            sheet_path = Path(sheet["path"])
            frames = sheet.get("frames") or []
            timestamps = "；".join(f"{idx + 1}={frame['timestamp']}" for idx, frame in enumerate(frames))
            parts.append(types.Part.from_text(text=f"SHEET {sheet_index} timestamps: {timestamps}"))
            parts.append(types.Part.from_bytes(data=sheet_path.read_bytes(), mime_type="image/jpeg"))
        config = self._json_config(SegmentReviewResult)
        return await self._generate_content(
            contents=parts,
            config=config,
            parser=lambda response: normalize_segment_payload(_response_text(response), segment),
        )

    async def analyze_subtitle_text(
        self,
        subtitle_text: str,
        *,
        video_title: str | None = None,
        source: str = "unknown",
    ) -> SegmentReviewResult:
        policy = load_policy()
        prompt = build_subtitle_review_prompt(policy, video_title, subtitle_text, source)
        segment = SegmentPlan(
            segment_index=0,
            start_seconds=0,
            end_seconds=1,
            start_time="00:00",
            end_time="00:00",
        )
        config = self._json_config(SegmentReviewResult)
        result = await self._generate_content(
            contents=[prompt],
            config=config,
            parser=lambda response: normalize_segment_payload(_response_text(response), segment),
        )
        result.segment_index = 0
        return result

    async def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if close:
                close()
