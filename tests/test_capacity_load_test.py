import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    path = Path("scripts/load_test_content_risk.py")
    spec = importlib.util.spec_from_file_location("load_test_content_risk", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_required_concurrency_uses_target_window_and_utilization():
    load_test = _load_module()

    assert load_test.required_concurrency(total=10000, hours=6, review_seconds=278.55, utilization=0.7) == 185
    assert load_test.required_concurrency(total=10000, hours=4, review_seconds=278.55, utilization=0.7) == 277
    assert load_test.required_concurrency(total=10000, hours=6, review_seconds=357.35, utilization=0.7) == 237


def test_classify_model_error_extracts_gateway_and_rate_limit_tokens():
    load_test = _load_module()

    assert load_test.classify_model_error("429 RESOURCE_EXHAUSTED") == {"429"}
    assert load_test.classify_model_error("504 Gateway Time-out") == {"504", "gateway_timeout"}
    assert load_test.classify_model_error("model call timed out") == {"timeout"}


def test_sample_video_rows_are_deduped_and_duration_filtered():
    load_test = _load_module()

    rows = [
        {"video_url": "https://qiniu.duanju.com/a.mp4", "duration_seconds": 60},
        {"video_url": "https://qiniu.duanju.com/a.mp4", "duration_seconds": 60},
        {"video_url": "https://qiniu.duanju.com/too-short.mp4", "duration_seconds": 3},
        {"video_url": "oss://bucket/key.mp4", "duration_seconds": 60},
        {"video_url": "https://qiniu.duanju.com/b.mp4", "duration_seconds": 180},
    ]

    urls = load_test.sample_urls_from_rows(rows, min_duration_seconds=10, max_duration_seconds=240)

    assert urls == ["https://qiniu.duanju.com/a.mp4", "https://qiniu.duanju.com/b.mp4"]


def test_disk_guard_raises_when_usage_crosses_threshold(monkeypatch):
    load_test = _load_module()
    usage = SimpleNamespace(total=100, used=86, free=14)
    monkeypatch.setattr(load_test.shutil, "disk_usage", lambda path: usage)

    try:
        load_test.check_disk_guard("/home", 85)
    except load_test.DiskGuardExceeded as exc:
        assert exc.used_percent == 86
        assert exc.threshold_percent == 85
    else:
        raise AssertionError("expected DiskGuardExceeded")
