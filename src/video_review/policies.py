from __future__ import annotations

import json
from pathlib import Path

from .models import PolicyRules, ReviewFinding


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = PROJECT_ROOT / "policies" / "default.json"

SEVERITY_RISK_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "critical": "不予过审",
}

DEFAULT_FINE_GRAINED_META = {
    "minor_protection": ("未成年人成人化", "不予过审", "禁止"),
    "sexual_lowbrow": ("软色情擦边", "中风险", "避免或少量出现"),
    "bad_values_ethics": ("乱伦/近亲伦理", "高风险", "禁止"),
    "violence_harm": ("血腥暴力", "高风险", "避免或少量出现"),
    "public_politics_symbols": ("公职人员抹黑", "高风险", "禁止"),
    "national_history_ethnic": ("历史虚无", "不予过审", "禁止"),
    "brand_ip_logo": ("品牌商标/IP风险", "高风险", "禁止"),
    "gambling_drugs_crime": ("严重违法犯罪", "高风险", "禁止"),
    "medical_superstition": ("医疗与封建迷信", "中风险", "避免或少量出现"),
    "quality_duplicate": ("质量问题", "低风险", "返工"),
    "dialogue_subtitle": ("台词字幕风险", "中风险", "随命中内容继承风险"),
}


def load_policy(path: Path | None = None) -> PolicyRules:
    policy_path = path or DEFAULT_POLICY_PATH
    return PolicyRules.model_validate_json(policy_path.read_text(encoding="utf-8"))


def dump_policy(policy: PolicyRules) -> dict:
    return json.loads(policy.model_dump_json())


def keyword_findings(text: str, policy: PolicyRules) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for category in policy.categories:
        matched = [kw for kw in category.keywords if kw and kw in text]
        if not matched:
            continue
        sub_category, risk_level, rule_tag = DEFAULT_FINE_GRAINED_META.get(
            category.id,
            (category.name, SEVERITY_RISK_LABELS.get(category.severity, category.severity), "规则命中"),
        )
        findings.append(
            ReviewFinding(
                category=category.id,
                sub_category=sub_category,
                risk_level=risk_level,
                rule_tag=rule_tag,
                severity=category.severity,
                start_time="00:00",
                end_time="00:00",
                evidence="、".join(matched[:8]),
                reason=category.rule,
                suggested_action=category.default_action,
                context_note="本地关键词兜底命中，需结合画面/字幕/音频进行人工或模型复核。",
                plot_impact="命中内容可能影响主线价值导向或平台合规结论。",
                value_correction_advice={
                    "main": category.default_action,
                    "overall": "按具体时间戳删除、弱化或改写命中内容，并补充正向后果或价值回正。",
                },
                confidence=0.55,
            )
        )
    return findings
