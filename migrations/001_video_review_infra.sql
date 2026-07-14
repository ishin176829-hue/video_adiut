CREATE TABLE IF NOT EXISTS video_assets (
    video_id TEXT PRIMARY KEY,
    source_url TEXT,
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content_length BIGINT,
    etag TEXT,
    duration_seconds NUMERIC,
    width INTEGER,
    height INTEGER,
    bit_rate BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_video_assets_sha256 ON video_assets (sha256);
CREATE INDEX IF NOT EXISTS idx_video_assets_source_url ON video_assets (source_url);

CREATE TABLE IF NOT EXISTS review_jobs (
    review_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT NOT NULL,
    video_id TEXT REFERENCES video_assets(video_id) ON DELETE SET NULL,
    source_url TEXT,
    local_path TEXT,
    session_id TEXT,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    report_path TEXT,
    request JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_review_jobs_status_updated ON review_jobs (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_jobs_video_id ON review_jobs (video_id);
CREATE INDEX IF NOT EXISTS idx_review_jobs_session_id ON review_jobs (session_id);

CREATE TABLE IF NOT EXISTS review_events (
    id BIGSERIAL PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_events_review_id_id ON review_events (review_id, id);

CREATE TABLE IF NOT EXISTS review_segments (
    review_id TEXT NOT NULL REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_seconds INTEGER,
    end_seconds INTEGER,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    risk_score NUMERIC NOT NULL DEFAULT 0,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (review_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_review_segments_review_time ON review_segments (review_id, start_time, end_time);

CREATE TABLE IF NOT EXISTS review_findings (
    id BIGSERIAL PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    segment_index INTEGER,
    category TEXT NOT NULL,
    sub_category TEXT,
    risk_level TEXT,
    rule_tag TEXT,
    severity TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    evidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    original_text TEXT,
    context_note TEXT,
    plot_impact TEXT,
    value_correction_advice JSONB,
    confidence NUMERIC NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_findings_review_id ON review_findings (review_id);
CREATE INDEX IF NOT EXISTS idx_review_findings_category ON review_findings (category);
CREATE INDEX IF NOT EXISTS idx_review_findings_sub_category ON review_findings (sub_category);
CREATE INDEX IF NOT EXISTS idx_review_findings_risk_level ON review_findings (risk_level);
CREATE INDEX IF NOT EXISTS idx_review_findings_severity ON review_findings (severity);
CREATE INDEX IF NOT EXISTS idx_review_findings_time ON review_findings (review_id, start_time, end_time);

CREATE TABLE IF NOT EXISTS review_reports (
    review_id TEXT PRIMARY KEY REFERENCES review_jobs(review_id) ON DELETE CASCADE,
    video_id TEXT,
    policy_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    risk_score NUMERIC NOT NULL,
    summary TEXT NOT NULL,
    report JSONB NOT NULL,
    report_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS frame_batch_cache_index (
    cache_key TEXT PRIMARY KEY,
    video_id TEXT,
    video_sha256 TEXT,
    policy_version TEXT,
    model TEXT,
    fps INTEGER,
    start_time TEXT,
    end_time TEXT,
    frame_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_frame_batch_cache_video ON frame_batch_cache_index (video_sha256, policy_version, model);
