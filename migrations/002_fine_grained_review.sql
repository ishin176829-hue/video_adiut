ALTER TABLE review_findings
    ADD COLUMN IF NOT EXISTS sub_category TEXT,
    ADD COLUMN IF NOT EXISTS risk_level TEXT,
    ADD COLUMN IF NOT EXISTS rule_tag TEXT,
    ADD COLUMN IF NOT EXISTS original_text TEXT,
    ADD COLUMN IF NOT EXISTS context_note TEXT,
    ADD COLUMN IF NOT EXISTS plot_impact TEXT,
    ADD COLUMN IF NOT EXISTS value_correction_advice JSONB;

CREATE INDEX IF NOT EXISTS idx_review_findings_sub_category ON review_findings (sub_category);
CREATE INDEX IF NOT EXISTS idx_review_findings_risk_level ON review_findings (risk_level);
