ALTER TABLE review_jobs
    ADD COLUMN IF NOT EXISTS upload_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS upload_completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_review_jobs_upload_started ON review_jobs (upload_started_at DESC);
