import asyncio
import json
from types import SimpleNamespace

import pytest

import video_review.analyzer as analyzer_module
import video_review.model_retry as model_retry_module
from video_review.analyzer import MultimodalAnalyzer, normalize_segment_payload
from video_review.models import SegmentPlan


def _segment() -> SegmentPlan:
    return SegmentPlan(
        segment_index=3,
        start_seconds=60,
        end_seconds=120,
        start_time="01:00",
        end_time="02:00",
    )


def test_empty_model_response_is_a_contract_error():
    with pytest.raises(model_retry_module.ModelContractError, match="空内容"):
        analyzer_module._response_text(SimpleNamespace(text="  "))


def test_segment_normalization_owns_context_fields_and_common_confidence_scales():
    raw = {
        "segment_index": 999,
        "start_time": "00:00",
        "end_time": "99:99",
        "risk_score": 60,
        "findings": [
            {
                "category": "violence_harm",
                "severity": "high",
                "evidence": "持刀威胁",
                "reason": "暴力风险",
                "suggested_action": "删除",
                "confidence": 7,
            },
            {
                "category": "violence_harm",
                "severity": "high",
                "evidence": "殴打",
                "reason": "暴力风险",
                "suggested_action": "删除",
                "confidence": "high",
            },
        ],
    }

    result = normalize_segment_payload(json.dumps(raw, ensure_ascii=False), _segment())

    assert result.segment_index == 3
    assert result.start_time == "01:00"
    assert result.end_time == "02:00"
    assert result.findings[0].confidence == 0.7
    assert result.findings[1].confidence == 0.9


def test_narrative_normalization_flattens_historical_structured_values():
    raw = {
        "main_plot": "主线剧情",
        "plot_structure": [
            {
                "phase": "opening",
                "risk_points": [
                    {
                        "category": "暴力",
                        "time_code": "00:12",
                        "evidence": "人物持刀威胁",
                        "reason": "存在暴力胁迫",
                    }
                ],
                "correction_advice": {"action": "删除持刀镜头"},
            }
        ],
        "final_verdict": {
            "passed": "不通过",
            "conclusion": {"main": "需修改后复审"},
            "reason": ["存在持刀威胁", "缺少回正"],
            "high_risk_categories": [{"category": "暴力伤害"}],
            "medium_risk_categories": "低俗表达",
        },
    }

    result = analyzer_module.normalize_narrative_payload(json.dumps(raw, ensure_ascii=False))

    assert result.plot_structure[0].risk_points == ["暴力；00:12；人物持刀威胁；存在暴力胁迫"]
    assert result.plot_structure[0].correction_advice == "删除持刀镜头"
    assert result.final_verdict.passed is False
    assert result.final_verdict.conclusion == "需修改后复审"
    assert result.final_verdict.reason == "存在持刀威胁；缺少回正"
    assert result.final_verdict.high_risk_categories == ["暴力伤害"]
    assert result.final_verdict.medium_risk_categories == ["低俗表达"]


def test_provider_marks_key_failed_when_response_parser_rejects_output(monkeypatch):
    releases = []
    lease = SimpleNamespace(api_key="key-a", key_id="key-a", lease_id=None, scheduler_index=0)

    class FakePool:
        async def acquire(self):
            return lease

        async def release(self, lease_arg, *, success, error_kind=None):
            releases.append((lease_arg, success, error_kind))

    class FakeModels:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(text="not-json")

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    monkeypatch.setattr(analyzer_module, "get_google_api_key_pool", lambda: FakePool())

    async def scenario():
        analyzer = MultimodalAnalyzer()
        analyzer._clients["key-a"] = fake_client
        with pytest.raises(json.JSONDecodeError):
            await analyzer._generate_content(
                contents=["prompt"],
                config=None,
                parser=lambda response: json.loads(analyzer_module._response_text(response)),
            )

    asyncio.run(scenario())

    assert releases == [(lease, False, "parse")]
