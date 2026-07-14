from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from .utils import format_time


RiskLevel = Literal["none", "low", "medium", "high"]
AssetQuality = Literal["bad", "usable", "good"]


class ScriptElement(BaseModel):
    type: Literal["action", "visible_text", "shot", "transition"]
    timestamp: str
    content: str
    characters: list[str] = Field(default_factory=list)


class ScriptScene(BaseModel):
    scene_id: str
    start_time: str
    end_time: str
    location: str
    time_of_day: str = ""
    characters: list[str] = Field(default_factory=list)
    screenplay: list[ScriptElement] = Field(default_factory=list)


class CharacterCandidate(BaseModel):
    candidate_id: str
    role_label: str
    visual_description: str
    appearance_count: int = Field(ge=0)
    first_seen: str
    last_seen: str
    evidence_timestamps: list[str] = Field(default_factory=list)
    face_visible: bool = False
    asset_quality: AssetQuality = "usable"


class AssetObservation(BaseModel):
    asset_type: Literal["character", "face", "object", "location", "text"]
    label: str
    candidate_id: str | None = None
    timestamps: list[str] = Field(default_factory=list)
    evidence: str
    suggested_asset_use: str = ""


class AuditSignal(BaseModel):
    category: str
    evidence: str
    risk_level: RiskLevel


class SegmentScreenplaySemantic(BaseModel):
    episode_id: str
    title: str
    segment_index: int
    time_range: str
    script_scenes: list[ScriptScene] = Field(default_factory=list)
    character_candidates: list[CharacterCandidate] = Field(default_factory=list)
    asset_observations: list[AssetObservation] = Field(default_factory=list)
    audit_signals: list[AuditSignal] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class FullVideoScreenplaySemantic(BaseModel):
    episode_id: str
    title: str
    model: str
    frame_interval_seconds: int
    segment_seconds: int
    total_frames_sent: int
    segments: list[SegmentScreenplaySemantic] = Field(default_factory=list)


class BenchmarkResult(BaseModel):
    duration_seconds: int
    frames_sent: int
    success: bool
    latency_seconds: float
    output_valid_json: bool
    output_path: str | None = None
    raw_output_path: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class FrameBatch:
    segment_index: int
    start_seconds: int
    end_seconds: int
    start_time: str
    end_time: str
    frames: list[dict[str, Any]]


def json_payload(text: str, *, unwrap_key: str | None = None) -> dict[str, Any]:
    """Parse provider JSON while tolerating fences and one-field schema wrappers."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(cleaned[start : end + 1])
    if unwrap_key and isinstance(payload, dict) and set(payload.keys()) == {unwrap_key}:
        payload = payload[unwrap_key]
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 顶层必须是对象")
    return payload


def build_screenplay_semantic_prompt(
    *,
    episode_id: str,
    title: str,
    segment_index: int,
    start_time: str,
    end_time: str,
    frame_interval_seconds: int,
) -> str:
    schema = json.dumps(SegmentScreenplaySemantic.model_json_schema(), ensure_ascii=False)
    return f"""请将下面按时间顺序抽取的短剧视频帧转换成审核可用的剧本式语义 JSON。
episode_id：{episode_id}
视频标题：{title}
分段编号：{segment_index}
时间范围：{start_time}-{end_time}
这些帧是 {frame_interval_seconds} 秒 1 帧采样，代表该时间段内每一秒的视觉内容。只依据提供的帧图，不臆测音频。

抽取要求：
1. script_scenes 必须像剧本一样按场景组织，包含 location、characters 和 screenplay。
2. screenplay 只写画面动作、镜头变化、可见字幕/OCR/品牌/公共标识；没有音频时不要编造对白。
3. character_candidates 记录本段所有可见人物候选，appearance_count 按提供帧中的出现次数估计。
4. asset_observations 记录可进入资产库的线索，包括人物、清晰人脸、地点、关键物品和可见文字。
5. audit_signals 记录审核可用风险线索，没有风险时 risk_level 使用 none 或 low。
6. 必须返回一个合法 JSON object，不能返回 Markdown，不能返回 YAML，不能返回 key: value 文本。

JSON Schema：
{schema}"""


def segment_frame_batches(frames: list[dict[str, Any]], *, segment_seconds: int) -> list[FrameBatch]:
    if segment_seconds <= 0:
        raise ValueError("segment_seconds must be positive")
    if not frames:
        return []
    ordered = sorted(frames, key=lambda frame: float(frame.get("timestamp_seconds") or 0))
    start_bound = int(float(ordered[0].get("timestamp_seconds") or 0))
    end_bound = int(float(ordered[-1].get("timestamp_seconds") or 0)) + 1
    batches: list[FrameBatch] = []
    segment_index = 1
    start = start_bound
    while start < end_bound:
        end = min(start + segment_seconds, end_bound)
        batch_frames = [
            frame
            for frame in ordered
            if start <= float(frame.get("timestamp_seconds") or 0) < end
        ]
        if batch_frames:
            batches.append(
                FrameBatch(
                    segment_index=segment_index,
                    start_seconds=start,
                    end_seconds=end,
                    start_time=format_time(start),
                    end_time=format_time(end),
                    frames=batch_frames,
                )
            )
            segment_index += 1
        start = end
    return batches


def eligible_character_assets(
    candidates: list[CharacterCandidate],
    *,
    min_appearance_count: int = 4,
) -> list[CharacterCandidate]:
    return [
        candidate
        for candidate in candidates
        if candidate.appearance_count >= min_appearance_count
    ]
