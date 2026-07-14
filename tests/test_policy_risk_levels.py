from video_review.policies import load_policy


def test_policy_exposes_contextual_risk_levels():
    policy = load_policy()
    categories = {item.id: item for item in policy.categories}

    assert policy.version == "sn2s-video-review-v0.3-taxonomy"
    assert categories["minor_protection"].risk_levels["现代"] == "不予通过"
    assert categories["bad_values_ethics"].name == "不良价值观"
    assert categories["bad_values_ethics"].severity == "high"
    assert "同父异母" in categories["bad_values_ethics"].keywords
    assert "乱伦" not in categories["sexual_lowbrow"].keywords
    assert categories["public_politics_symbols"].risk_levels["古代（含近代民国）"] == "低风险"
    assert categories["brand_ip_logo"].severity == "high"
    assert "medical_superstition" in categories
    assert "dialogue_subtitle" in categories
