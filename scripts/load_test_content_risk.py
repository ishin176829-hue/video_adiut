#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / "docs" / "load-test-runs"
DEFAULT_BASE_URL = "https://video-audit.duanju.com"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


class DiskGuardExceeded(RuntimeError):
    def __init__(self, path: str, used_percent: float, threshold_percent: float) -> None:
        self.path = path
        self.used_percent = used_percent
        self.threshold_percent = threshold_percent
        super().__init__(
            f"disk guard exceeded: {path} used {used_percent:.2f}% >= {threshold_percent:.2f}%"
        )


def required_concurrency(*, total: int, hours: float, review_seconds: float, utilization: float = 0.7) -> int:
    target_qps = total / (hours * 3600)
    return math.ceil((target_qps * review_seconds) / utilization)


def disk_usage_snapshot(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    usage = shutil.disk_usage(path)
    used_percent = usage.used / usage.total * 100 if usage.total else 0
    return {
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": used_percent,
    }


def check_disk_guard(path: str | None, threshold_percent: float | None) -> dict[str, Any] | None:
    if not path or threshold_percent is None:
        return None
    snapshot = disk_usage_snapshot(path)
    assert snapshot is not None
    if snapshot["used_percent"] >= threshold_percent:
        raise DiskGuardExceeded(path, float(snapshot["used_percent"]), float(threshold_percent))
    return snapshot


def classify_model_error(text: str | None) -> set[str]:
    raw = (text or "").lower()
    labels: set[str] = set()
    if "429" in raw or "resource_exhausted" in raw or "rate limit" in raw or "too many requests" in raw:
        labels.add("429")
    if "504" in raw:
        labels.add("504")
    if "gateway time-out" in raw or "gateway timeout" in raw:
        labels.add("gateway_timeout")
    if "timeout" in raw or "timed out" in raw or "超时" in raw:
        labels.add("timeout")
    if "模型调用异常" in raw:
        labels.add("model_exception")
    return labels


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def sample_urls_from_rows(
    rows: list[Any],
    *,
    min_duration_seconds: float | None = None,
    max_duration_seconds: float | None = None,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows:
        url = str(_row_value(row, "video_url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        duration = _row_value(row, "duration_seconds")
        if duration is None and (min_duration_seconds is not None or max_duration_seconds is not None):
            continue
        if duration is not None:
            seconds = float(duration)
            if min_duration_seconds is not None and seconds < min_duration_seconds:
                continue
            if max_duration_seconds is not None and seconds > max_duration_seconds:
                continue
        if url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


async def fetch_sample_urls_from_db(
    *,
    limit: int,
    min_duration_seconds: float | None = None,
    max_duration_seconds: float | None = None,
    database_url: str | None = None,
) -> list[str]:
    dsn = database_url or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required to sample historical videos from PostgreSQL.")
    try:
        import asyncpg
    except Exception as exc:
        raise RuntimeError("asyncpg is required to sample historical videos from PostgreSQL.") from exc

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2, timeout=5)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(
                        NULLIF(j.request->>'video_url', ''),
                        NULLIF(j.source_url, ''),
                        NULLIF(va.source_url, '')
                    ) AS video_url,
                    va.duration_seconds
                FROM review_jobs j
                LEFT JOIN video_assets va ON va.video_id = j.video_id
                WHERE j.status = 'completed'
                  AND COALESCE(
                        NULLIF(j.request->>'video_url', ''),
                        NULLIF(j.source_url, ''),
                        NULLIF(va.source_url, '')
                      ) LIKE 'http%'
                ORDER BY j.completed_at DESC NULLS LAST, j.created_at DESC
                LIMIT $1
                """,
                max(limit * 20, limit),
            )
    finally:
        await pool.close()
    return sample_urls_from_rows(
        rows,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
    )[:limit]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_video_urls(path: str | None, single_url: str | None) -> list[str]:
    urls: list[str] = []
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                urls.append(stripped)
    if single_url:
        urls.append(single_url.strip())
    unique = []
    seen = set()
    for url in urls:
        if url and url not in seen:
            unique.append(url)
            seen.add(url)
    return unique


def estimate_rows(
    *,
    total: int,
    hours: list[float],
    utilization: float,
    review_seconds: dict[str, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hour in hours:
        target_qps = total / (hour * 3600)
        for label, seconds in review_seconds.items():
            rows.append(
                {
                    "window_hours": hour,
                    "percentile": label,
                    "review_seconds": seconds,
                    "target_qps": target_qps,
                    "required_concurrency": required_concurrency(
                        total=total,
                        hours=hour,
                        review_seconds=seconds,
                        utilization=utilization,
                    ),
                }
            )
    return rows


def write_report(report_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path = report_dir / f"{name}.md"
    md_path.write_text(render_markdown_report(payload), encoding="utf-8")
    return path


def render_markdown_report(payload: dict[str, Any]) -> str:
    lines = [f"# {payload.get('name', 'content-risk load test')}", ""]
    if "capacity_estimate" in payload:
        lines.extend(["## Capacity Estimate", "", "| window_hours | percentile | review_seconds | target_qps | required_concurrency |", "| ---: | --- | ---: | ---: | ---: |"])
        for row in payload["capacity_estimate"]:
            lines.append(
                "| {window_hours:g} | {percentile} | {review_seconds:.2f} | {target_qps:.3f} | {required_concurrency} |".format(
                    **row
                )
            )
        lines.append("")
    if "run" in payload:
        run = payload["run"]
        lines.extend(["## Run", "", "```json", json.dumps(run, ensure_ascii=False, indent=2, default=str), "```", ""])
    if "db_report" in payload and payload["db_report"]:
        db_report = payload["db_report"]
        lines.extend(["## DB Report", "", "```json", json.dumps(db_report, ensure_ascii=False, indent=2, default=str), "```", ""])
    return "\n".join(lines)


async def submit_one(
    client: httpx.AsyncClient,
    *,
    data_id: str,
    video_url: str,
    app_id: str,
    title: str,
    fps: int,
) -> dict[str, Any]:
    response = await client.post(
        "/api/compat/content-risk/video/tasks",
        headers={"X-App-Id": app_id},
        json={
            "data_id": data_id,
            "parameters": {
                "video_url": video_url,
                "title": title,
                "interval": fps,
            },
        },
    )
    return {"status_code": response.status_code, "body": response.json()}


async def submit_tasks(
    *,
    base_url: str,
    app_id: str,
    prefix: str,
    video_urls: list[str],
    total: int,
    fps: int,
    submit_rps: float,
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    interval = 1 / max(0.1, submit_rps)
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        for index in range(total):
            data_id = f"{prefix}-{index + 1:05d}"
            video_url = video_urls[index % len(video_urls)]
            started = time.monotonic()
            try:
                response = await submit_one(
                    client,
                    data_id=data_id,
                    video_url=video_url,
                    app_id=app_id,
                    title=f"load-test-{data_id}",
                    fps=fps,
                )
                submit_error = None
            except Exception as exc:
                response = {"status_code": 0, "body": {}}
                submit_error = str(exc)
            states[data_id] = {
                "data_id": data_id,
                "video_url": video_url,
                "submitted_at": time.time(),
                "submit_elapsed_seconds": time.monotonic() - started,
                "submit_status_code": response["status_code"],
                "submit_response": response["body"],
                "submit_error": submit_error,
                "status": "submit_failed" if submit_error or response["status_code"] >= 400 else "submitted",
            }
            await asyncio.sleep(interval)
    return states


async def poll_results(
    *,
    base_url: str,
    app_id: str,
    states: dict[str, dict[str, Any]],
    poll_interval: float,
    timeout_seconds: float,
    batch_size: int,
    disk_guard_path: str | None = None,
    max_disk_used_percent: float | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        while time.monotonic() < deadline:
            check_disk_guard(disk_guard_path, max_disk_used_percent)
            pending = [
                data_id
                for data_id, state in states.items()
                if state.get("status") not in TERMINAL_STATUSES and state.get("status") != "submit_failed"
            ]
            if not pending:
                return
            for batch in chunked(pending, max(1, min(batch_size, 500))):
                response = await client.post(
                    "/api/compat/content-risk/video/results/batch",
                    headers={"X-App-Id": app_id},
                    json={"data_ids": batch},
                )
                response.raise_for_status()
                for item in response.json().get("items", []):
                    data_id = item.get("data_id")
                    if not data_id or data_id not in states:
                        continue
                    previous = states[data_id].get("status")
                    status = item.get("status") or previous
                    states[data_id]["status"] = status
                    states[data_id]["last_result"] = item
                    if status in TERMINAL_STATUSES and previous not in TERMINAL_STATUSES:
                        states[data_id]["completed_at"] = time.time()
            await asyncio.sleep(max(1, poll_interval))


def _safe_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_size(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() or item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _remove_path(path: Path) -> int:
    size = _path_size(path)
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    return size


async def cleanup_load_test_artifacts(prefix: str, *, cancel_unfinished: bool = False) -> dict[str, Any] | None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return None
    try:
        import asyncpg
    except Exception:
        return None

    data_dir = Path(os.getenv("VIDEO_REVIEW_DATA_DIR", str(ROOT / "data"))).resolve()
    raw_dir = data_dir / "raw"
    derived_dir = data_dir / "derived"
    reports_dir = data_dir / "reports"
    jobs_dir = data_dir / "jobs"
    events_dir = data_dir / "events"
    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=2, timeout=5)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT review_id, video_id, local_path, report_path
                FROM review_jobs
                WHERE request->>'platform_task_id' LIKE $1
                """,
                f"{prefix}%",
            )
            if cancel_unfinished:
                await conn.execute(
                    """
                    UPDATE review_jobs
                    SET status = CASE WHEN status IN ('pending','processing') THEN 'cancelled' ELSE status END,
                        phase = CASE WHEN status IN ('pending','processing') THEN 'cancelled' ELSE phase END,
                        message = CASE WHEN status IN ('pending','processing') THEN '压测因磁盘护栏中止' ELSE message END,
                        error = CASE WHEN status IN ('pending','processing') THEN 'LOAD_TEST_ABORTED_DISK_GUARD' ELSE error END,
                        completed_at = CASE WHEN status IN ('pending','processing') THEN COALESCE(completed_at, now()) ELSE completed_at END,
                        updated_at = now()
                    WHERE request->>'platform_task_id' LIKE $1
                    """,
                    f"{prefix}%",
                )
    finally:
        await pool.close()

    redis_report = None
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            import redis.asyncio as redis_async

            review_ids = {str(row["review_id"]) for row in rows if row["review_id"]}
            stream = os.getenv("REDIS_REVIEW_STREAM", "sn2s:video_review:jobs")
            group = os.getenv("REDIS_REVIEW_GROUP", "video-review-workers")
            active_key = os.getenv("REDIS_GLOBAL_ACTIVE_KEY", "sn2s:video_review:active_reviews")
            client = redis_async.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5)
            scanned = 0
            xack_count = 0
            xdel_count = 0
            last = "-"
            try:
                while True:
                    entries = await client.xrange(stream, min=last, max="+", count=500)
                    if not entries:
                        break
                    for stream_id, fields in entries:
                        if stream_id == last:
                            continue
                        scanned += 1
                        raw_payload = fields.get("payload") or "{}"
                        try:
                            payload = json.loads(raw_payload)
                        except json.JSONDecodeError:
                            payload = {}
                        review_id = fields.get("review_id") or payload.get("review_id")
                        platform_task_id = payload.get("platform_task_id") or payload.get("task_id")
                        if (platform_task_id and str(platform_task_id).startswith(prefix)) or review_id in review_ids:
                            try:
                                xack_count += int(await client.xack(stream, group, stream_id) or 0)
                            except Exception:
                                pass
                            xdel_count += int(await client.xdel(stream, stream_id) or 0)
                        last = stream_id
                    if len(entries) < 500:
                        break
                active_members = await client.zrange(active_key, 0, -1)
                zrem_members = [member for member in active_members if str(member).split(":")[-1] in review_ids]
                zrem_count = int(await client.zrem(active_key, *zrem_members) or 0) if zrem_members else 0
                redis_report = {
                    "stream_scanned": scanned,
                    "xack": xack_count,
                    "xdel": xdel_count,
                    "active_zrem": zrem_count,
                }
            finally:
                await client.aclose()
        except Exception as exc:
            redis_report = {"error": str(exc) or exc.__class__.__name__}

    targets: set[Path] = set()
    for row in rows:
        local_path = row["local_path"]
        if local_path:
            path = Path(local_path).resolve()
            if _safe_inside(path, raw_dir):
                if path.parent.name.startswith("video_") and _safe_inside(path.parent, raw_dir):
                    targets.add(path.parent)
                else:
                    targets.add(path)
        report_path = row["report_path"]
        if report_path:
            path = Path(report_path).resolve()
            if _safe_inside(path, data_dir):
                targets.add(path)
        review_id = row["review_id"]
        if review_id:
            targets.add((reports_dir / f"{review_id}.json").resolve())
            targets.add((jobs_dir / f"{review_id}.json").resolve())
            targets.add((events_dir / f"{review_id}.jsonl").resolve())
        video_id = row["video_id"]
        if video_id:
            targets.add((derived_dir / video_id).resolve())

    deleted_bytes = 0
    deleted_paths = 0
    skipped_paths = 0
    for target in sorted(targets):
        if not _safe_inside(target, data_dir):
            skipped_paths += 1
            continue
        if target.exists() or target.is_symlink():
            deleted_bytes += _remove_path(target)
            deleted_paths += 1
    return {
        "matched_jobs": len(rows),
        "deleted_paths": deleted_paths,
        "deleted_bytes": deleted_bytes,
        "deleted_gib": deleted_bytes / 1024 / 1024 / 1024,
        "skipped_paths": skipped_paths,
        "cancel_unfinished": cancel_unfinished,
        "redis": redis_report,
    }


async def query_db_report(prefix: str) -> dict[str, Any] | None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return None
    try:
        import asyncpg
    except Exception:
        return None

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=2, timeout=5)
    try:
        async with pool.acquire() as conn:
            job_rows = await conn.fetch(
                """
                SELECT
                    review_id,
                    status,
                    error,
                    EXTRACT(EPOCH FROM (completed_at - COALESCE(started_at, created_at))) AS review_seconds,
                    EXTRACT(EPOCH FROM (completed_at - created_at)) AS total_seconds
                FROM review_jobs
                WHERE request->>'platform_task_id' LIKE $1
                """,
                f"{prefix}%",
            )
            event_rows = await conn.fetch(
                """
                SELECT e.review_id, e.payload::text AS text
                FROM review_events e
                JOIN review_jobs j ON j.review_id = e.review_id
                WHERE j.request->>'platform_task_id' LIKE $1
                """,
                f"{prefix}%",
            )
            finding_rows = await conn.fetch(
                """
                SELECT f.review_id, concat_ws(' ', f.category, f.sub_category, f.evidence, f.reason, f.payload::text) AS text
                FROM review_findings f
                JOIN review_jobs j ON j.review_id = f.review_id
                WHERE j.request->>'platform_task_id' LIKE $1
                """,
                f"{prefix}%",
            )
    finally:
        await pool.close()

    status_counts: dict[str, int] = {}
    job_error_labels: dict[str, set[str]] = {}
    review_seconds = []
    total_seconds = []
    for row in job_rows:
        status = row["status"] or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        for label in classify_model_error(row["error"]):
            job_error_labels.setdefault(row["review_id"], set()).add(label)
        if row["review_seconds"] is not None:
            review_seconds.append(float(row["review_seconds"]))
        if row["total_seconds"] is not None:
            total_seconds.append(float(row["total_seconds"]))

    for row in list(event_rows) + list(finding_rows):
        labels = classify_model_error(row["text"])
        if labels:
            job_error_labels.setdefault(row["review_id"], set()).update(labels)

    error_counts: dict[str, int] = {}
    for labels in job_error_labels.values():
        for label in labels:
            error_counts[label] = error_counts.get(label, 0) + 1

    total_jobs = len(job_rows)
    return {
        "matched_jobs": total_jobs,
        "status_counts": status_counts,
        "review_seconds": {
            "p50": percentile(review_seconds, 0.5),
            "p90": percentile(review_seconds, 0.9),
            "p95": percentile(review_seconds, 0.95),
            "avg": statistics.mean(review_seconds) if review_seconds else None,
        },
        "total_seconds": {
            "p50": percentile(total_seconds, 0.5),
            "p90": percentile(total_seconds, 0.9),
            "p95": percentile(total_seconds, 0.95),
            "avg": statistics.mean(total_seconds) if total_seconds else None,
        },
        "model_error_job_counts": error_counts,
        "model_error_job_ratio": {
            label: count / total_jobs if total_jobs else 0 for label, count in sorted(error_counts.items())
        },
    }


def summarize_run(states: dict[str, dict[str, Any]], started_at: float, finished_at: float) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    latencies = []
    submit_errors = 0
    result_error_labels: dict[str, set[str]] = {}
    for data_id, state in states.items():
        status = state.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        if state.get("submit_error"):
            submit_errors += 1
        if state.get("completed_at") and state.get("submitted_at"):
            latencies.append(float(state["completed_at"] - state["submitted_at"]))
        labels = classify_model_error(json.dumps(state.get("last_result") or {}, ensure_ascii=False))
        if labels:
            result_error_labels[data_id] = labels
    model_error_counts: dict[str, int] = {}
    for labels in result_error_labels.values():
        for label in labels:
            model_error_counts[label] = model_error_counts.get(label, 0) + 1
    total = len(states)
    return {
        "total": total,
        "elapsed_seconds": finished_at - started_at,
        "status_counts": status_counts,
        "submit_errors": submit_errors,
        "latency_seconds": {
            "p50": percentile(latencies, 0.5),
            "p90": percentile(latencies, 0.9),
            "p95": percentile(latencies, 0.95),
            "avg": statistics.mean(latencies) if latencies else None,
        },
        "model_error_result_counts": model_error_counts,
        "model_error_result_ratio": {
            label: count / total if total else 0 for label, count in sorted(model_error_counts.items())
        },
    }


async def run_load_test(args: argparse.Namespace) -> dict[str, Any]:
    if args.total > 0 and not args.yes_real_cost:
        raise SystemExit("Refusing real model load test without --yes-real-cost.")
    if args.sample_from_db:
        video_urls = await fetch_sample_urls_from_db(
            limit=max(args.total, args.sample_limit),
            min_duration_seconds=args.min_duration_seconds,
            max_duration_seconds=args.max_duration_seconds,
        )
    else:
        video_urls = read_video_urls(args.video_url_file, args.video_url)
    if args.total > 0 and not video_urls:
        raise SystemExit("Provide --video-url-file or --video-url.")
    if args.total > 0 and len(video_urls) < args.total and not args.allow_repeat:
        raise SystemExit(
            f"Need at least {args.total} distinct video URLs, got {len(video_urls)}. "
            "Use --allow-repeat only for smoke tests; repeated videos can hit frame cache and skew model QPS."
        )

    prefix = args.prefix or f"capacity-{utc_stamp()}"
    disk_before = disk_usage_snapshot(args.disk_guard_path)
    check_disk_guard(args.disk_guard_path, args.max_disk_used_percent)
    started_at = time.monotonic()
    states: dict[str, dict[str, Any]] = {}
    abort_reason: str | None = None
    states = await submit_tasks(
        base_url=args.base_url,
        app_id=args.app_id,
        prefix=prefix,
        video_urls=video_urls,
        total=args.total,
        fps=args.fps,
        submit_rps=args.submit_rps,
    )
    try:
        await poll_results(
            base_url=args.base_url,
            app_id=args.app_id,
            states=states,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
            batch_size=args.batch_size,
            disk_guard_path=args.disk_guard_path,
            max_disk_used_percent=args.max_disk_used_percent,
        )
    except DiskGuardExceeded as exc:
        abort_reason = str(exc)
    finished_at = time.monotonic()
    run_summary = summarize_run(states, started_at, finished_at)
    if abort_reason:
        run_summary["aborted"] = True
        run_summary["abort_reason"] = abort_reason
    else:
        run_summary["aborted"] = False
    db_report = await query_db_report(prefix) if args.with_db_report else None
    cleanup_report = None
    if args.cleanup_artifacts:
        cleanup_report = await cleanup_load_test_artifacts(prefix, cancel_unfinished=bool(abort_reason))
    disk_after = disk_usage_snapshot(args.disk_guard_path)
    return {
        "name": f"content-risk-load-{prefix}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prefix": prefix,
        "base_url": args.base_url,
        "app_id": args.app_id,
        "sample": {
            "source": "postgres" if args.sample_from_db else "file_or_cli",
            "distinct_video_urls": len(video_urls),
            "min_duration_seconds": args.min_duration_seconds,
            "max_duration_seconds": args.max_duration_seconds,
        },
        "run": run_summary,
        "db_report": db_report,
        "cleanup": cleanup_report,
        "disk": {
            "before": disk_before,
            "after": disk_after,
            "guard_path": args.disk_guard_path,
            "max_used_percent": args.max_disk_used_percent,
        },
        "sample_states": list(states.values())[: min(10, len(states))],
    }


def parse_hours(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def add_estimate_payload(args: argparse.Namespace) -> dict[str, Any]:
    review_seconds = {
        "p50": args.p50,
        "p90": args.p90,
        "p95": args.p95,
    }
    return {
        "name": "capacity-estimate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capacity_estimate": estimate_rows(
            total=args.total,
            hours=parse_hours(args.hours),
            utilization=args.utilization,
            review_seconds=review_seconds,
        ),
    }


async def sample_payload(args: argparse.Namespace) -> dict[str, Any]:
    urls = await fetch_sample_urls_from_db(
        limit=args.limit,
        min_duration_seconds=args.min_duration_seconds,
        max_duration_seconds=args.max_duration_seconds,
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    return {
        "name": "postgres-video-sample",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(urls),
        "limit": args.limit,
        "min_duration_seconds": args.min_duration_seconds,
        "max_duration_seconds": args.max_duration_seconds,
        "output": args.output,
        "urls": urls[: min(20, len(urls))],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SN2S content-risk compatibility load test runner.")
    sub = parser.add_subparsers(dest="command", required=True)

    estimate = sub.add_parser("estimate", help="Estimate active concurrency for a target completion window.")
    estimate.add_argument("--total", type=int, default=10000)
    estimate.add_argument("--hours", default="4,5,6")
    estimate.add_argument("--utilization", type=float, default=0.7)
    estimate.add_argument("--p50", type=float, default=112.92)
    estimate.add_argument("--p90", type=float, default=278.55)
    estimate.add_argument("--p95", type=float, default=357.35)
    estimate.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))

    sample = sub.add_parser("sample", help="Fetch historical HTTP video URLs from PostgreSQL.")
    sample.add_argument("--limit", type=int, default=200)
    sample.add_argument("--min-duration-seconds", type=float, default=10)
    sample.add_argument("--max-duration-seconds", type=float, default=600)
    sample.add_argument("--output")
    sample.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))

    run = sub.add_parser("run", help="Submit real compatibility tasks and poll batch results.")
    run.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run.add_argument("--app-id", default="capacity-test")
    run.add_argument("--prefix")
    run.add_argument("--video-url-file")
    run.add_argument("--video-url")
    run.add_argument("--sample-from-db", action="store_true")
    run.add_argument("--sample-limit", type=int, default=200)
    run.add_argument("--min-duration-seconds", type=float, default=10)
    run.add_argument("--max-duration-seconds", type=float, default=600)
    run.add_argument("--total", type=int, required=True)
    run.add_argument("--fps", type=int, default=1)
    run.add_argument("--submit-rps", type=float, default=5)
    run.add_argument("--poll-interval", type=float, default=20)
    run.add_argument("--timeout-seconds", type=float, default=3600)
    run.add_argument("--batch-size", type=int, default=200)
    run.add_argument("--allow-repeat", action="store_true")
    run.add_argument("--with-db-report", action="store_true")
    run.add_argument("--disk-guard-path")
    run.add_argument("--max-disk-used-percent", type=float)
    run.add_argument("--cleanup-artifacts", action="store_true")
    run.add_argument("--yes-real-cost", action="store_true")
    run.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    report_dir = Path(args.report_dir)
    if args.command == "estimate":
        payload = add_estimate_payload(args)
        path = write_report(report_dir, f"capacity-estimate-{utc_stamp()}", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"report={path}")
        return
    if args.command == "sample":
        payload = asyncio.run(sample_payload(args))
        path = write_report(report_dir, f"postgres-video-sample-{utc_stamp()}", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"report={path}")
        return
    if args.command == "run":
        payload = asyncio.run(run_load_test(args))
        path = write_report(report_dir, payload["name"], payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        print(f"report={path}")
        return
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
