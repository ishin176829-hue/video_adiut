# content-risk-load-peak-120-20260709225013

## Run

```json
{
  "total": 120,
  "elapsed_seconds": 1317.6508665409638,
  "status_counts": {
    "completed": 100,
    "failed": 20
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 497.92506301403046,
    "p90": 702.0037773132325,
    "p95": 772.7991777896881,
    "avg": 506.17988012830415
  },
  "model_error_result_counts": {
    "model_exception": 3,
    "timeout": 23
  },
  "model_error_result_ratio": {
    "model_exception": 0.025,
    "timeout": 0.19166666666666668
  },
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 119,
  "status_counts": {
    "completed": 99,
    "failed": 20
  },
  "review_seconds": {
    "p50": 482.986504,
    "p90": 696.578358,
    "p95": 756.9271125999998,
    "avg": 495.7671896806723
  },
  "total_seconds": {
    "p50": 484.062099,
    "p90": 696.8183253999999,
    "p95": 756.9363936999998,
    "avg": 497.3032538739496
  },
  "model_error_job_counts": {
    "timeout": 28,
    "model_exception": 3,
    "429": 1
  },
  "model_error_job_ratio": {
    "429": 0.008403361344537815,
    "model_exception": 0.025210084033613446,
    "timeout": 0.23529411764705882
  }
}
```
