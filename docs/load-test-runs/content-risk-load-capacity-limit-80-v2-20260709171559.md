# content-risk-load-capacity-limit-80-v2-20260709171559

## Run

```json
{
  "total": 80,
  "elapsed_seconds": 1490.1842838829616,
  "status_counts": {
    "completed": 77,
    "failed": 3
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 601.6319096088409,
    "p90": 746.6415485143668,
    "p95": 858.7590324163436,
    "avg": 606.0145945131778
  },
  "model_error_result_counts": {},
  "model_error_result_ratio": {},
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 80,
  "status_counts": {
    "completed": 77,
    "failed": 3
  },
  "review_seconds": {
    "p50": 573.2329110000001,
    "p90": 726.4534855,
    "p95": 840.4489059499999,
    "avg": 572.2776373625
  },
  "total_seconds": {
    "p50": 592.818675,
    "p90": 739.3129811000006,
    "p95": 845.2909687499999,
    "avg": 597.6438792
  },
  "model_error_job_counts": {
    "timeout": 13,
    "504": 2
  },
  "model_error_job_ratio": {
    "504": 0.025,
    "timeout": 0.1625
  }
}
```
