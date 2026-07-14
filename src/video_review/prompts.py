from __future__ import annotations

import json

from .audit_guidance import DETAILED_AUDIT_GUIDANCE, FINE_GRAINED_OUTPUT_GUIDANCE
from .models import PolicyRules, SegmentPlan


def _format_policy_rule(category) -> str:
    risk_levels = category.risk_levels or {}
    risk_text = ""
    if risk_levels:
        risk_text = "；".join(f"{key}风险：{value}" for key, value in risk_levels.items() if value)
    if not risk_text:
        risk_text = f"默认风险：{category.severity}"
    return (
        f"- {category.id}/{category.name}：{category.rule} "
        f"机器默认等级：{category.severity}；{risk_text}；默认处理：{category.default_action}"
    )


SYSTEM_PROMPT = """你是 SN2S 的短剧视频审核专家。
你的任务是审查视频画面、字幕、OCR、人物关系、主线剧情、公共标识、品牌、暴力伤害、未成年保护、低俗擦边、历史民族、社会舆论和价值观导向。
必须输出 JSON，且每个问题必须包含明确时间码、证据、违规原因、建议动作和置信度。
所有字符串字段只能输出 JSON 字符串，不能输出对象或数组；confidence 必须是 0.0-1.0 的 JSON 数字。
审核颗粒度必须细到“规则子类”，不能只输出大方向分类。
涉及亲属、继亲、同父异母、同母异父、公媳等关系时，必须先判断人物关系链；乱伦/近亲伦理归入“不良价值观”，不得归入低俗擦边。
没有明确证据时不要强行判拒，标记为 manual_review 候选。
"""


def build_review_prompt(policy: PolicyRules, segment: SegmentPlan | None, video_title: str | None) -> str:
    rules = "\n".join(_format_policy_rule(c) for c in policy.categories)
    segment_text = ""
    if segment:
        segment_text = f"本次只审核视频时间段 {segment.start_time} 到 {segment.end_time}。"
    return f"""请审核短剧视频。

视频标题：{video_title or "未命名视频"}
{segment_text}

审核规则：
{rules}

SN2S 细粒度规则：
{DETAILED_AUDIT_GUIDANCE}

{FINE_GRAINED_OUTPUT_GUIDANCE}

输出要求：
1. 输出 SegmentReviewResult JSON。
2. start_time/end_time 必须是视频中的实际时间码。
3. evidence 必须描述画面、字幕或配音中的具体证据。
4. summary 必须包含该分段剧情事件摘要和风险摘要，不要只写“发现风险”。
5. finding 必须填写 category、sub_category、risk_level、rule_tag、context_note、plot_impact。
6. 如果未发现问题，findings 为空，risk_score 为 0-20。
7. 不要输出 Markdown，不要输出解释性前后缀，只输出 JSON。
"""


def build_frames_review_prompt(
    policy: PolicyRules,
    segment: SegmentPlan,
    video_title: str | None,
    frame_count: int,
) -> str:
    rules = "\n".join(_format_policy_rule(c) for c in policy.categories)
    return f"""请审核一组按时间顺序抽取的视频帧。

视频标题：{video_title or "未命名视频"}
审核时间段：{segment.start_time} 到 {segment.end_time}
本批帧数量：{frame_count}

每张图片前都有对应时间戳。请结合连续帧判断画面风险；如果只凭单帧无法确定，降低置信度或标记为 manual_review 候选。

审核规则：
{rules}

SN2S 细粒度规则：
{DETAILED_AUDIT_GUIDANCE}

{FINE_GRAINED_OUTPUT_GUIDANCE}

输出要求：
1. 输出 SegmentReviewResult JSON。
2. finding 的 start_time/end_time 必须使用图片前标注的时间戳范围。
3. evidence 必须描述图片中的具体画面、文字、标识或人物动作。
4. 当前抽帧模式没有音频，不能基于未提供的配音做判断。
5. summary 必须概括本批帧呈现的剧情事件、人物关系、价值导向和风险点。
6. finding 必须填写 category、sub_category、risk_level、rule_tag、context_note、plot_impact。
7. 如果未发现问题，findings 为空，risk_score 为 0-20。
8. 不要输出 Markdown，不要输出解释性前后缀，只输出 JSON。
"""


def build_frame_sheet_review_prompt(
    policy: PolicyRules,
    segment: SegmentPlan,
    video_title: str | None,
    sheet_count: int,
    frame_count: int,
) -> str:
    rules = "\n".join(_format_policy_rule(c) for c in policy.categories)
    return f"""请审核一组视频帧拼图。

视频标题：{video_title or "未命名视频"}
审核时间段：{segment.start_time} 到 {segment.end_time}
拼图数量：{sheet_count}
总帧数：{frame_count}

每张拼图是 4x4 或配置的网格，每个格子上方标有序号和时间戳。请按格子的时间戳定位风险。
拼图来自感知哈希去重后的关键帧，连续动作可能只保留关键变化点；不要因为中间帧缺失而臆测未看到的内容。

审核规则：
{rules}

SN2S 细粒度规则：
{DETAILED_AUDIT_GUIDANCE}

{FINE_GRAINED_OUTPUT_GUIDANCE}

输出要求：
1. 输出 SegmentReviewResult JSON。
2. finding 的 start_time/end_time 必须使用拼图格子上的时间戳或相邻格子时间范围。
3. evidence 必须描述拼图中具体格子的画面、字幕、标识或人物动作。
4. 当前视觉拼图通道不包含音频；不能基于未提供的配音做判断。
5. summary 必须概括本批拼图呈现的剧情事件、人物关系、价值导向和风险点。
6. finding 必须填写 category、sub_category、risk_level、rule_tag、context_note、plot_impact。
7. 如果未发现问题，findings 为空，risk_score 为 0-20。
8. 不要输出 Markdown，不要输出解释性前后缀，只输出 JSON。
"""


def build_subtitle_review_prompt(
    policy: PolicyRules,
    video_title: str | None,
    subtitle_text: str,
    source: str,
) -> str:
    rules = "\n".join(_format_policy_rule(c) for c in policy.categories)
    return f"""请审核短剧字幕/台词文本。

视频标题：{video_title or "未命名视频"}
字幕来源：{source}

字幕文本：
{subtitle_text[:20000]}

审核规则：
{rules}

SN2S 细粒度规则：
{DETAILED_AUDIT_GUIDANCE}

{FINE_GRAINED_OUTPUT_GUIDANCE}

输出要求：
1. 输出 SegmentReviewResult JSON，segment_index 使用 0，start_time/end_time 使用字幕中可定位的时间范围；如果无时间码，使用 00:00。
2. 只依据字幕/台词文本判断，不要臆测未提供的画面。
3. finding 的 evidence 必须引用具体字幕文本，original_text 必须填写命中的原文。
4. category、sub_category、risk_level、rule_tag、context_note、plot_impact 必须填写。
5. 如果仅凭字幕无法定性，建议动作为“结合画面人工复核”。
6. 如果未发现问题，findings 为空，risk_score 为 0-20。
7. 不要输出 Markdown，不要输出解释性前后缀，只输出 JSON。
"""


def build_narrative_prompt(
    policy: PolicyRules,
    segments: list,
    video_title: str | None,
) -> str:
    rules = "\n".join(_format_policy_rule(c) for c in policy.categories)
    segment_json = json.dumps(
        [
            segment.model_dump(mode="json") if hasattr(segment, "model_dump") else segment
            for segment in segments
        ],
        ensure_ascii=False,
    )
    return f"""请根据视频分段审核结果，生成整片级别的剧情与价值观审核结论。

视频标题：{video_title or "未命名视频"}

平台审核规则：
{rules}

SN2S 细粒度规则：
{DETAILED_AUDIT_GUIDANCE}

分段结果 JSON：
{segment_json}

输出 ReportNarrative JSON，要求：
1. main_plot：按“开头-中间-结尾”写完整主线剧情，不少于 80 字。不要只罗列风险分类。
2. story_context：识别背景和题材，例如 background=古代/现代/民国/近代（年代文）/末日架空/未知，genres 可包含玄幻、都市、历史等，signals 写判断依据。
3. plot_structure：必须包含 opening、middle、ending 三项。每项要有 time_range、plot_summary、value_judgement、risk_points、correction_advice。
4. value_correction_advice：必须分别给 opening、main、ending、overall 的回正建议。建议要可执行，例如“删除某段”“改称谓”“补充依法处理后果”“弱化血腥镜头”。
5. final_verdict：结合证据和回正情况给 passed、conclusion、reason、high_risk_categories、medium_risk_categories。
6. overall_summary：用审核人员口吻总结整片是否可过审、主要问题和修改优先级。
7. 不要输出 Markdown，不要输出解释性前后缀，只输出 JSON。
8. risk_points 必须是纯字符串数组；correction_advice、conclusion、reason 必须是字符串。
9. final_verdict.passed 必须是 JSON 布尔值 true/false；high_risk_categories 和 medium_risk_categories 必须是纯字符串数组。
"""


def build_judge_prompt(policy: PolicyRules, scanner_json: str) -> str:
    rules = "\n".join(_format_policy_rule(c) for c in policy.categories)
    return f"""根据审核规则和多模态扫描结果，生成最终 VideoReviewReport。

审核规则：
{rules}

扫描结果：
{scanner_json}

裁决要求：
- critical 默认 reject。
- high 默认 reject 或 manual_review。
- medium 默认 warn 或 manual_review。
- low 默认 warn。
- 没有明确时间码或证据的问题不得直接 reject。
"""
