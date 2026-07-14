# content-risk-load-capacity-limit-120-v3-20260709174706

## Run

```json
{
  "total": 120,
  "elapsed_seconds": 1521.2592812760267,
  "status_counts": {
    "completed": 120
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 724.4037039279938,
    "p90": 977.3150945425034,
    "p95": 1099.3558895349502,
    "avg": 726.869359344244
  },
  "model_error_result_counts": {
    "timeout": 1,
    "model_exception": 1
  },
  "model_error_result_ratio": {
    "model_exception": 0.008333333333333333,
    "timeout": 0.008333333333333333
  },
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 120,
  "status_counts": {
    "completed": 116,
    "processing": 4
  },
  "review_seconds": {
    "p50": 686.868602,
    "p90": 962.6448255,
    "p95": 1091.951641,
    "avg": 696.3243319655172
  },
  "total_seconds": {
    "p50": 720.1050405000001,
    "p90": 970.395497,
    "p95": 1099.700715,
    "avg": 718.6029945172414
  },
  "model_error_job_counts": {
    "timeout": 12,
    "model_exception": 1,
    "429": 32,
    "504": 3
  },
  "model_error_job_ratio": {
    "429": 0.26666666666666666,
    "504": 0.025,
    "model_exception": 0.008333333333333333,
    "timeout": 0.1
  }
}
```
