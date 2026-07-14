from video_review.judge import build_report
from video_review.models import ReportNarrative, ReviewFinding, SegmentReviewResult, ValueCorrectionAdvice


def test_critical_finding_rejects():
    segment = SegmentReviewResult(
        segment_index=1,
        start_time="00:00",
        end_time="01:00",
        findings=[
            ReviewFinding(
                category="minor_protection",
                sub_category="未成年人成人化",
                risk_level="不予过审",
                rule_tag="禁止",
                severity="critical",
                start_time="00:10",
                end_time="00:13",
                evidence="未成年与低俗内容绑定",
                reason="违反未成年保护规则",
                suggested_action="删除",
                plot_impact="以未成年擦边作为冲突钩子",
                value_correction_advice={"main": "删除未成年擦边片段"},
                confidence=0.9,
            )
        ],
    )
    report = build_report("review_1", "video_1", [segment])
    assert report.decision == "reject"
    assert report.risk_score >= 95
    assert report.main_plot
    assert report.value_correction_advice.main
    assert report.final_verdict.high_risk_categories == ["未成年人成人化"]


def test_build_report_uses_narrative_value_structure():
    segment = SegmentReviewResult(
        segment_index=1,
        start_time="00:00",
        end_time="01:00",
        summary="主角遭遇冲突后选择依法解决。",
        findings=[],
    )
    narrative = ReportNarrative(
        main_plot="开头主角遭遇家庭冲突，中间通过调查厘清误会，结尾以依法处理和家庭和解完成回正。",
        value_correction_advice=ValueCorrectionAdvice(
            opening="开头保留正向动机",
            main="主线强调依法解决",
            ending="结尾补足责任承担",
            overall="避免以暴制暴",
        ),
        overall_summary="整体价值观回正，可通过。",
    )

    report = build_report("review_1", "video_1", [segment], narrative=narrative)

    assert report.main_plot.startswith("开头主角")
    assert report.summary == "整体价值观回正，可通过。"
    assert report.value_correction_advice.main == "主线强调依法解决"


def test_keyword_findings_use_fine_grained_fields():
    from video_review.policies import keyword_findings, load_policy

    findings = keyword_findings("儿童出现成人化擦边冲突", load_policy())

    assert findings
    first = findings[0]
    assert first.sub_category
    assert first.risk_level in {"低风险", "中风险", "高风险", "不予过审"}
    assert first.rule_tag
    assert first.context_note
    assert first.value_correction_advice.overall
