# content-risk-load-provider-fix-replay177-r2-20260714T0707Z

## Run

```json
{
  "total": 177,
  "elapsed_seconds": 891.6101409010589,
  "status_counts": {
    "completed": 177
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 475.37160754203796,
    "p90": 702.4136240959167,
    "p95": 722.0530618190766,
    "avg": 482.2453351007343
  },
  "model_error_result_counts": {
    "model_exception": 3
  },
  "model_error_result_ratio": {
    "model_exception": 0.01694915254237288
  },
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 177,
  "status_counts": {
    "completed": 177
  },
  "review_seconds": {
    "p50": 207.722292,
    "p90": 280.411072,
    "p95": 303.82918279999984,
    "avg": 211.26018937288134
  },
  "total_seconds": {
    "p50": 473.133768,
    "p90": 701.1312336,
    "p95": 719.9649919999999,
    "avg": 479.55669197175143
  },
  "model_error_job_counts": {
    "model_exception": 3,
    "timeout": 3,
    "429": 4,
    "504": 2
  },
  "model_error_job_ratio": {
    "429": 0.022598870056497175,
    "504": 0.011299435028248588,
    "model_exception": 0.01694915254237288,
    "timeout": 0.01694915254237288
  }
}
```
