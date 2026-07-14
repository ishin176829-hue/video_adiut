from __future__ import annotations

from .models import (
    Decision,
    FinalVerdict,
    ReportNarrative,
    ReviewFinding,
    SegmentReviewResult,
    StoryPhaseAssessment,
    ValueCorrectionAdvice,
    VideoReviewReport,
)
from .policies import load_policy


SEVERITY_SCORE = {
    "low": 20,
    "medium": 45,
    "high": 75,
    "critical": 95,
}


def _dedupe_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    seen = set()
    output: list[ReviewFinding] = []
    for item in findings:
        key = (item.category, item.start_time, item.end_time, item.evidence[:80], item.reason[:80])
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def decide(findings: list[ReviewFinding], risk_score: float) -> Decision:
    severities = {f.severity for f in findings}
    if "critical" in severities:
        return "reject"
    if "high" in severities:
        return "reject" if risk_score >= 75 else "manual_review"
    if "medium" in severities:
        return "manual_review" if risk_score >= 55 else "warn"
    if findings:
        return "warn"
    return "pass"


def _severity_label(severity: str) -> str:
    return {
        "low": "低风险",
        "medium": "中风险",
        "high": "高风险",
        "critical": "不予过审",
    }.get(severity, severity or "未知风险")


def _phase_name(phase: str) -> str:
    return {"opening": "开头", "middle": "中间", "ending": "结尾"}.get(phase, phase)


def _fallback_main_plot(segments: list[SegmentReviewResult]) -> str:
    summaries = [segment.summary.strip() for segment in segments if segment.summary.strip()]
    if summaries:
        return "；".join(summaries)[:1600]
    if segments:
        return f"视频按 {len(segments)} 个时间段完成审核，但模型未返回明确剧情摘要。"
    return "未获取到可用剧情摘要。"


def _fallback_plot_structure(
    segments: list[SegmentReviewResult],
    findings: list[ReviewFinding],
) -> list[StoryPhaseAssessment]:
    if not segments:
        return []
    first = segments[0]
    middle = segments[len(segments) // 2]
    last = segments[-1]
    phase_segments = [("opening", first), ("middle", middle), ("ending", last)]
    output: list[StoryPhaseAssessment] = []
    for phase, segment in phase_segments:
        phase_findings = [
            finding
            for finding in findings
            if segment.start_time <= finding.start_time <= segment.end_time
        ]
        risk_points = [
            f"{finding.sub_category or finding.category}：{finding.reason}"
            for finding in phase_findings[:5]
        ]
        if risk_points:
            judgement = "该阶段存在价值导向或合规风险，需要按命中项处理。"
            advice = "按命中项删除、弱化或补充法治/道德后果，避免违规内容成为爽点。"
        else:
            judgement = "该阶段未发现明确价值观风险。"
            advice = "保持剧情因果清晰，避免新增低俗、暴力、违法或敏感表达。"
        output.append(
            StoryPhaseAssessment(
                phase=phase,
                phase_name=_phase_name(phase),
                time_range=f"{segment.start_time} - {segment.end_time}",
                plot_summary=segment.summary or "该阶段暂无明确剧情摘要。",
                value_judgement=judgement,
                risk_points=risk_points,
                correction_advice=advice,
            )
        )
    return output


def _fallback_value_advice(findings: list[ReviewFinding]) -> ValueCorrectionAdvice:
    if not findings:
        return ValueCorrectionAdvice(
            overall="未发现明确违规风险，保持正向结局和现实责任表达。",
        )
    high = [f for f in findings if f.severity in {"high", "critical"}]
    main_advice = "；".join(
        [
            finding.suggested_action
            for finding in findings[:5]
            if finding.suggested_action
        ]
    )
    if high:
        main = main_advice or "删除高风险片段，避免违规内容推动主线或形成爽点。"
        ending = "补充依法处理、承担后果、道德反思或受害者保护，完成价值回正。"
    else:
        main = main_advice or "弱化争议表达，控制篇幅频次。"
        ending = "结尾补充正向态度，避免误导性价值背书。"
    return ValueCorrectionAdvice(
        opening="开头避免使用低俗、暴力、违法、民族历史或未成年成人化内容作为钩子。",
        main=main,
        ending=ending,
        overall="优先处理高风险/不予过审项，其次处理中风险项；所有低风险争议点都要控制篇幅并回正。",
    )


def _fallback_final_verdict(
    findings: list[ReviewFinding],
    decision: Decision,
    risk_score: float,
) -> FinalVerdict:
    high_categories = sorted(
        {
            finding.sub_category or finding.category
            for finding in findings
            if finding.severity in {"high", "critical"}
        }
    )
    medium_categories = sorted(
        {
            finding.sub_category or finding.category
            for finding in findings
            if finding.severity == "medium"
        }
    )
    if not findings:
        reason = "未发现明确违规证据。"
    else:
        top = max(findings, key=lambda f: SEVERITY_SCORE.get(f.severity, 0))
        reason = f"最高风险为{_severity_label(top.severity)}：{top.sub_category or top.category}。"
    return FinalVerdict(
        passed=decision == "pass",
        conclusion={
            "pass": "通过",
            "warn": "预警",
            "manual_review": "人工复核",
            "reject": "不予过审",
        }.get(decision, decision),
        reason=f"{reason} 综合风险分 {risk_score}。",
        high_risk_categories=high_categories,
        medium_risk_categories=medium_categories,
    )


def build_report(
    review_id: str,
    video_id: str,
    segments: list[SegmentReviewResult],
    narrative: ReportNarrative | None = None,
) -> VideoReviewReport:
    policy = load_policy()
    findings = _dedupe_findings([finding for segment in segments for finding in segment.findings])
    max_score = max([SEVERITY_SCORE.get(f.severity, 0) for f in findings] or [0])
    count_score = min(15, len(findings) * 2)
    repeated_score = min(10, max(0, len(findings) - len({f.category for f in findings})))
    risk_score = min(100, max_score + count_score + repeated_score)
    decision = decide(findings, risk_score)
    if findings:
        summary = f"发现 {len(findings)} 个审核风险，最高风险等级为 {max((f.severity for f in findings), key=lambda s: SEVERITY_SCORE.get(s, 0))}。"
    else:
        summary = "未发现明确违规风险。"
    main_plot = narrative.main_plot if narrative and narrative.main_plot else _fallback_main_plot(segments)
    plot_structure = (
        narrative.plot_structure
        if narrative and narrative.plot_structure
        else _fallback_plot_structure(segments, findings)
    )
    value_advice = (
        narrative.value_correction_advice
        if narrative and narrative.value_correction_advice
        else _fallback_value_advice(findings)
    )
    final_verdict = (
        narrative.final_verdict
        if narrative and narrative.final_verdict
        else _fallback_final_verdict(findings, decision, risk_score)
    )
    story_context = narrative.story_context if narrative and narrative.story_context else {}
    if narrative and narrative.overall_summary:
        summary = narrative.overall_summary
    return VideoReviewReport(
        review_id=review_id,
        video_id=video_id,
        policy_version=policy.version,
        decision=decision,
        risk_score=risk_score,
        summary=summary,
        main_plot=main_plot,
        story_context=story_context,
        plot_structure=plot_structure,
        value_correction_advice=value_advice,
        final_verdict=final_verdict,
        findings=findings,
        segments=segments,
    )
