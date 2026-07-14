# content-risk-load-provider-fix-failed3-20260714T0701Z

## Run

```json
{
  "total": 3,
  "elapsed_seconds": 250.86417472106405,
  "status_counts": {
    "completed": 3
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 156.82785630226135,
    "p90": 229.31931004524233,
    "p95": 238.38074176311494,
    "avg": 177.2358593940735
  },
  "model_error_result_counts": {
    "model_exception": 3
  },
  "model_error_result_ratio": {
    "model_exception": 1.0
  },
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 3,
  "status_counts": {
    "completed": 3
  },
  "review_seconds": {
    "p50": 156.792293,
    "p90": 227.4709866,
    "p95": 236.3058233,
    "avg": 175.72096966666666
  },
  "total_seconds": {
    "p50": 156.818644,
    "p90": 227.497256,
    "p95": 236.3320825,
    "avg": 175.74921633333332
  },
  "model_error_job_counts": {
    "timeout": 3,
    "model_exception": 3
  },
  "model_error_job_ratio": {
    "model_exception": 1.0,
    "timeout": 1.0
  }
}
```
