import json

from video_review.config import Settings
from video_review.analyzer import normalize_segment_payload
from video_review.models import SegmentPlan


def test_settings_combines_legacy_and_pool_keys():
    settings = Settings(
        google_api_key="legacy-key",
        google_api_keys="new-key-a, new-key-b, legacy-key",
        video_review_model="gemini-2.5-flash",
    )

    assert settings.video_review_model == "gemini-2.5-flash"
    assert settings.google_api_key_pool == ["legacy-key", "new-key-a", "new-key-b"]


def test_normalize_segment_payload_fills_model_omissions():
    segment = SegmentPlan(
        segment_index=1,
        start_seconds=0,
        end_seconds=58,
        start_time="00:00",
        end_time="00:58",
    )
    raw = {
        "risk_score": 75,
        "findings": [
            {
                "category": "violence_harm",
                "start_time": "00:03",
                "end_time": "00:07",
                "evidence": "画面出现持刀威胁",
                "reason": "暴力伤害风险",
                "suggested_action": "删除或弱化台词",
                "confidence": 0.8,
            }
        ],
    }

    result = normalize_segment_payload(json.dumps(raw, ensure_ascii=False), segment)

    assert result.segment_index == 1
    assert result.start_time == "00:00"
    assert result.end_time == "00:58"
    assert result.findings[0].severity == "high"
    assert result.findings[0].sub_category == "violence_harm"
    assert result.findings[0].value_correction_advice.model_dump() == {
        "opening": "",
        "main": "",
        "ending": "",
        "overall": "",
    }


def test_normalize_segment_payload_accepts_percent_confidence():
    segment = SegmentPlan(
        segment_index=1,
        start_seconds=0,
        end_seconds=58,
        start_time="00:00",
        end_time="00:58",
    )
    raw = {
        "risk_score": 75,
        "findings": [
            {
                "category": "violence_harm",
                "severity": "high",
                "start_time": "00:03",
                "end_time": "00:07",
                "evidence": "画面出现持刀威胁",
                "reason": "暴力伤害风险",
                "suggested_action": "删除或弱化台词",
                "confidence": 75,
            },
            {
                "category": "sexual_lowbrow",
                "severity": "medium",
                "start_time": "00:10",
                "end_time": "00:12",
                "evidence": "字幕出现低俗表达",
                "reason": "低俗擦边风险",
                "suggested_action": "改台词",
                "confidence": "86%",
            },
        ],
    }

    result = normalize_segment_payload(json.dumps(raw, ensure_ascii=False), segment)

    assert result.findings[0].confidence == 0.75
    assert result.findings[1].confidence == 0.86


def test_normalize_segment_payload_stringifies_structured_suggested_action():
    segment = SegmentPlan(
        segment_index=1,
        start_seconds=0,
        end_seconds=58,
        start_time="00:00",
        end_time="00:58",
    )
    raw = {
        "risk_score": 75,
        "findings": [
            {
                "category": "violence_harm",
                "severity": "high",
                "start_time": "00:03",
                "end_time": "00:07",
                "evidence": "画面出现持刀威胁",
                "reason": "暴力伤害风险",
                "suggested_action": ["删除持刀威胁画面", "改写台词"],
                "confidence": 0.8,
            },
            {
                "category": "national_history_ethnic",
                "severity": "critical",
                "start_time": "00:12",
                "end_time": "00:14",
                "evidence": "字幕出现民族历史敏感表达",
                "reason": "严重违反民族历史敏感规则",
                "suggested_action": {"main": "删除对应片段", "overall": "重新设定冲突背景"},
                "confidence": 0.9,
            },
        ],
    }

    result = normalize_segment_payload(json.dumps(raw, ensure_ascii=False), segment)

    assert result.findings[0].suggested_action == "删除持刀威胁画面；改写台词"
    assert result.findings[1].suggested_action == "删除对应片段；重新设定冲突背景"


def test_normalize_segment_payload_moves_incest_to_bad_values_category():
    segment = SegmentPlan(
        segment_index=1,
        start_seconds=0,
        end_seconds=120,
        start_time="00:00",
        end_time="02:00",
    )
    raw = {
        "risk_score": 85,
        "findings": [
            {
                "category": "低俗擦边",
                "sub_category": "乱伦暗示",
                "severity": "medium",
                "start_time": "00:40",
                "end_time": "00:55",
                "evidence": "字幕和剧情交代男女角色为同父异母兄妹，并出现暧昧关系推进。",
                "reason": "亲属关系中出现暧昧线。",
                "suggested_action": "删除或改人物关系",
                "confidence": 0.82,
            }
        ],
    }

    result = normalize_segment_payload(json.dumps(raw, ensure_ascii=False), segment)

    assert result.findings[0].category == "不良价值观"
    assert result.findings[0].sub_category == "乱伦/近亲伦理"
    assert result.findings[0].risk_level == "高风险"
    assert result.findings[0].rule_tag == "禁止"
    assert result.findings[0].severity == "high"
