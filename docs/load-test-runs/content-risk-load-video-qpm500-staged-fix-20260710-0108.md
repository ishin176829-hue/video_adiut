# content-risk-load-video-qpm500-staged-fix-20260710-0108

## Run

```json
{
  "total": 158,
  "elapsed_seconds": 3614.6391505829524,
  "status_counts": {
    "completed": 94,
    "failed": 28,
    "processing": 36
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 585.7433429956436,
    "p90": 841.8467425584794,
    "p95": 914.7037903904915,
    "avg": 559.2038542735772
  },
  "model_error_result_counts": {
    "timeout": 27,
    "model_exception": 1
  },
  "model_error_result_ratio": {
    "model_exception": 0.006329113924050633,
    "timeout": 0.17088607594936708
  },
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 158,
  "status_counts": {
    "completed": 94,
    "failed": 28,
    "processing": 36
  },
  "review_seconds": {
    "p50": 236.3976745,
    "p90": 367.97810430000004,
    "p95": 425.8583361499999,
    "avg": 245.69272881967214
  },
  "total_seconds": {
    "p50": 580.427985,
    "p90": 836.7034418,
    "p95": 910.6353386,
    "avg": 554.1783326721311
  },
  "model_error_job_counts": {
    "timeout": 35,
    "model_exception": 1,
    "429": 4,
    "504": 2
  },
  "model_error_job_ratio": {
    "429": 0.02531645569620253,
    "504": 0.012658227848101266,
    "model_exception": 0.006329113924050633,
    "timeout": 0.22151898734177214
  }
}
```
