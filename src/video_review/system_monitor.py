from __future__ import annotations

import asyncio
import fcntl
import os
import socket
from collections import deque
from datetime import datetime, timezone

import psutil

from .config import settings
from .models import SystemMetricPoint, SystemMetricsResponse


INTERVAL_SECONDS = 5
WINDOW_SECONDS = 60 * 60
MAX_POINTS = WINDOW_SECONDS // INTERVAL_SECONDS

_points: deque[SystemMetricPoint] = deque(maxlen=MAX_POINTS)
_sampler_task: asyncio.Task | None = None
_lock_file = None


def _metrics_path() -> str:
    settings.derived_dir.mkdir(parents=True, exist_ok=True)
    return str(settings.derived_dir / "system_metrics.jsonl")


def _lock_path() -> str:
    settings.derived_dir.mkdir(parents=True, exist_ok=True)
    return str(settings.derived_dir / "system_metrics.lock")


def _load_average() -> tuple[float | None, float | None, float | None]:
    if not hasattr(os, "getloadavg"):
        return None, None, None
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return None, None, None
    return round(one, 2), round(five, 2), round(fifteen, 2)


def _append_recorded_point(point: SystemMetricPoint) -> None:
    with open(_metrics_path(), "a", encoding="utf-8") as f:
        f.write(point.model_dump_json() + "\n")


def _read_recorded_points() -> list[SystemMetricPoint]:
    try:
        with open(_metrics_path(), encoding="utf-8") as f:
            lines = f.readlines()[-MAX_POINTS * 2 :]
    except FileNotFoundError:
        return list(_points)

    points: list[SystemMetricPoint] = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            points.append(SystemMetricPoint.model_validate_json(raw))
        except ValueError:
            continue
    return points[-MAX_POINTS:]


def collect_system_metric(record: bool = True) -> SystemMetricPoint:
    memory = psutil.virtual_memory()
    load_1m, load_5m, load_15m = _load_average()
    point = SystemMetricPoint(
        timestamp=datetime.now(timezone.utc).isoformat(),
        cpu_percent=round(float(psutil.cpu_percent(interval=None)), 2),
        memory_percent=round(float(memory.percent), 2),
        memory_used_bytes=int(memory.used),
        memory_total_bytes=int(memory.total),
        load_1m=load_1m,
        load_5m=load_5m,
        load_15m=load_15m,
    )
    _points.append(point)
    if record:
        _append_recorded_point(point)
    return point


async def _sample_loop() -> None:
    psutil.cpu_percent(interval=None)
    while True:
        collect_system_metric()
        await asyncio.sleep(INTERVAL_SECONDS)


def start_system_monitor() -> None:
    global _lock_file, _sampler_task
    if _sampler_task and not _sampler_task.done():
        return
    _lock_file = open(_lock_path(), "w", encoding="utf-8")
    try:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _lock_file.close()
        _lock_file = None
        return
    _sampler_task = asyncio.create_task(_sample_loop())


async def stop_system_monitor() -> None:
    global _lock_file, _sampler_task
    if _sampler_task:
        _sampler_task.cancel()
        try:
            await _sampler_task
        except asyncio.CancelledError:
            pass
    _sampler_task = None
    if _lock_file:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
        _lock_file.close()
        _lock_file = None


def system_metrics(window_seconds: int = 1800) -> SystemMetricsResponse:
    recorded_points = _read_recorded_points()
    if not recorded_points:
        collect_system_metric()
        recorded_points = _read_recorded_points()
    elif (
        datetime.now(timezone.utc).timestamp()
        - datetime.fromisoformat(recorded_points[-1].timestamp).timestamp()
        > INTERVAL_SECONDS * 2
    ):
        collect_system_metric()
        recorded_points = _read_recorded_points()
    bounded_window = max(INTERVAL_SECONDS, min(WINDOW_SECONDS, int(window_seconds or 1800)))
    latest = recorded_points[-1] if recorded_points else None
    cutoff = datetime.now(timezone.utc).timestamp() - bounded_window
    points = [
        point
        for point in recorded_points
        if datetime.fromisoformat(point.timestamp).timestamp() >= cutoff
    ]
    return SystemMetricsResponse(
        hostname=socket.gethostname(),
        cpu_count=psutil.cpu_count() or 0,
        interval_seconds=INTERVAL_SECONDS,
        window_seconds=bounded_window,
        latest=latest,
        points=points,
    )
