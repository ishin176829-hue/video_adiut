from video_review.system_monitor import collect_system_metric, system_metrics


def test_system_metrics_response_contains_cpu_and_memory_points():
    collect_system_metric()

    response = system_metrics(window_seconds=60)

    assert response.hostname
    assert response.cpu_count >= 0
    assert response.interval_seconds > 0
    assert response.window_seconds == 60
    assert response.latest is not None
    assert response.points
    assert 0 <= response.latest.cpu_percent <= 100
    assert 0 <= response.latest.memory_percent <= 100
    assert response.latest.memory_total_bytes >= response.latest.memory_used_bytes
