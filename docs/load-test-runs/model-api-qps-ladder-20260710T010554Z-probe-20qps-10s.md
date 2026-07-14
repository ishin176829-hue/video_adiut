# model-api-qps-ladder-20260710T010554Z-probe-20qps-10s

```json
{
  "run_id": "model-api-qps-ladder-20260710T010554Z-probe-20qps-10s",
  "generated_at": "2026-07-10T01:07:16.561858+00:00",
  "base_url": "https://jzapi.duanju.com",
  "model": "gemini-3.1-pro-preview",
  "target_qps": 20.0,
  "duration_seconds": 10.0,
  "request_timeout_seconds": 60.0,
  "total": 200,
  "elapsed_seconds": 65.081423329073,
  "actual_started_qps": 20.0,
  "actual_finished_qps_over_wall": 3.0730735403363023,
  "sliding_1s_started_peak": 21,
  "sliding_1s_finished_peak": 30,
  "sliding_60s_started_peak": 200,
  "sliding_60s_finished_peak": 199,
  "status_counts": {
    "ok": 196,
    "error": 4
  },
  "error_counts": {
    "JSONDecodeError": 3,
    "TimeoutError": 1
  },
  "success_ratio": 0.98,
  "ok_latency_seconds": {
    "avg": 4.054896753670396,
    "p50": 3.888158597459551,
    "p90": 4.932528955978341,
    "p95": 5.220147059240844,
    "p99": 5.7599278728827015
  },
  "all_latency_seconds": {
    "avg": 4.338673687182018,
    "p50": 3.906797051022295,
    "p90": 4.938124317442998,
    "p95": 5.263097623252542,
    "p99": 6.677234776807003
  },
  "sample_errors": [
    {
      "error": "JSONDecodeError",
      "error_text": "Extra data: line 2 column 1 (char 38)",
      "latency_seconds": 4.264795440016314
    },
    {
      "error": "TimeoutError",
      "error_text": "",
      "latency_seconds": 60.03059656301048
    },
    {
      "error": "JSONDecodeError",
      "error_text": "Extra data: line 2 column 1 (char 39)",
      "latency_seconds": 4.146015583071858
    },
    {
      "error": "JSONDecodeError",
      "error_text": "Extra data: line 2 column 1 (char 39)",
      "latency_seconds": 4.533566130907275
    }
  ]
}
```
