from pathlib import Path

from video_review.db import SCHEMA_SQL, schema_table_names


def test_postgres_schema_contains_review_tables():
    assert schema_table_names() == {
        "video_assets",
        "review_jobs",
        "review_events",
        "review_segments",
        "review_findings",
        "review_reports",
        "frame_batch_cache_index",
    }


def test_postgres_schema_tracks_oss_video_assets():
    assert "oss_bucket TEXT" in SCHEMA_SQL
    assert "oss_key TEXT" in SCHEMA_SQL
    assert "oss_endpoint TEXT" in SCHEMA_SQL


def test_postgres_schema_adds_oss_columns_before_indexing_them():
    alter_position = SCHEMA_SQL.index("ALTER TABLE video_assets")
    index_position = SCHEMA_SQL.index("idx_video_assets_oss_key")

    assert alter_position < index_position


def test_restart_all_sources_env_before_worker_defaults():
    script = Path("scripts/restart_all.sh").read_text(encoding="utf-8")

    source_position = script.index("source .env")
    worker_default_position = script.index('worker_concurrency="${WORKER_CONCURRENCY')

    assert source_position < worker_default_position


def test_runtime_scripts_preserve_load_test_env_overrides():
    for path in ["scripts/restart_all.sh", "scripts/run_worker_nohup.sh", "scripts/run_api_nohup.sh"]:
        script = Path(path).read_text(encoding="utf-8")

        assert "preserved_names=(" in script
        assert "VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT" in script
        assert "VIDEO_REVIEW_REDIS_CACHE_ENABLED" in script
        assert "VIDEO_REVIEW_FRAME_BATCH_CONCURRENCY" in script
        assert "VIDEO_REVIEW_FRAME_BATCH_MAX_SPLIT_DEPTH" in script
        assert "VIDEO_REVIEW_MODEL_CALL_TIMEOUT_SECONDS" in script
        assert "VIDEO_REVIEW_DOWNLOAD_CONCURRENCY_PER_PROCESS" in script
        assert "VIDEO_REVIEW_DOWNLOAD_TOTAL_TIMEOUT_SECONDS" in script
        assert 'export "${value}"' in script


def test_worker_scripts_use_a_new_consumer_identity_after_restart():
    restart_script = Path("scripts/restart_all.sh").read_text(encoding="utf-8")
    worker_script = Path("scripts/run_worker_nohup.sh").read_text(encoding="utf-8")

    assert 'worker_instance_id="${WORKER_INSTANCE_ID:-$(hostname)-$(date +%s%N)}"' in restart_script
    assert 'WORKER_INSTANCE_ID="${worker_instance_id}"' in restart_script
    assert 'worker_instance_id="${WORKER_INSTANCE_ID:-$(hostname)-$(date +%s%N)-$$}"' in worker_script
    assert '--consumer "${CONSUMER:-video-review-${worker_id}-${worker_instance_id}}"' in worker_script


def test_postgres_schema_has_stale_processing_timeout_marker():
    from video_review.db import mark_stale_processing_jobs_failed

    assert mark_stale_processing_jobs_failed.__name__ == "mark_stale_processing_jobs_failed"
    assert "STALE_PROCESSING_TIMEOUT" in Path("src/video_review/db.py").read_text(encoding="utf-8")
