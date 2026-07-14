ALTER TABLE video_assets
    ADD COLUMN IF NOT EXISTS oss_bucket TEXT,
    ADD COLUMN IF NOT EXISTS oss_key TEXT,
    ADD COLUMN IF NOT EXISTS oss_endpoint TEXT;

CREATE INDEX IF NOT EXISTS idx_video_assets_oss_key ON video_assets (oss_bucket, oss_key);
