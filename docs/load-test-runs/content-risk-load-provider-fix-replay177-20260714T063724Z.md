# content-risk-load-provider-fix-replay177-20260714T063724Z

## Run

```json
{
  "total": 177,
  "elapsed_seconds": 819.9562996658497,
  "status_counts": {
    "completed": 174,
    "failed": 3
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 480.3844904899597,
    "p90": 714.5403705120086,
    "p95": 749.0845422267913,
    "avg": 478.4443141902234
  },
  "model_error_result_counts": {},
  "model_error_result_ratio": {},
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 175,
  "status_counts": {
    "completed": 172,
    "failed": 3
  },
  "review_seconds": {
    "p50": 201.507988,
    "p90": 259.23687259999997,
    "p95": 282.3004707999999,
    "avg": 202.36282177714287
  },
  "total_seconds": {
    "p50": 478.429895,
    "p90": 705.4394578,
    "p95": 744.9879101999999,
    "avg": 476.67462748
  },
  "model_error_job_counts": {
    "504": 6,
    "429": 1
  },
  "model_error_job_ratio": {
    "429": 0.005714285714285714,
    "504": 0.03428571428571429
  }
}
```
