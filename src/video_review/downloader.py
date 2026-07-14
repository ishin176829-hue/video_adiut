from __future__ import annotations

import asyncio
import hashlib
import os
import random
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import settings
from .models import VideoAsset
from .oss import download_oss_object
from .utils import new_id, safe_filename, sha256_file


_download_semaphore = asyncio.Semaphore(max(1, settings.download_concurrency_per_process))


class SourceDownloadError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        retryable: bool,
        host: str,
        attempts: int,
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.host = host
        self.attempts = attempts
        self.status_code = status_code
        self.cause = cause
        details = f"code={code}, host={host}, attempts={attempts}"
        if status_code is not None:
            details += f", http_status={status_code}"
        super().__init__(details)


def _download_error_details(exc: BaseException, host: str) -> tuple[str, bool, int | None]:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 403:
            return "source_forbidden", False, status_code
        if status_code == 404:
            return "source_not_found", False, status_code
        if status_code in {408, 429, 500, 502, 503, 504}:
            return f"source_http_{status_code}", True, status_code
        return f"source_http_{status_code}", False, status_code
    if isinstance(exc, httpx.ConnectTimeout):
        return "source_connect_timeout", True, None
    if isinstance(exc, httpx.TimeoutException):
        return "source_timeout", True, None
    if isinstance(exc, httpx.NetworkError):
        return "source_network_error", True, None
    return "source_download_error", False, None


def _retry_delay_seconds(attempt: int) -> float:
    base = max(0.0, settings.download_retry_delay_seconds) * (2 ** max(0, attempt - 1))
    jitter = max(0.0, settings.download_retry_jitter_seconds)
    return base + (random.uniform(0, jitter) if jitter else 0.0)


async def _stream_response_to_file(
    response,
    target: Path,
    *,
    total_timeout_seconds: float,
) -> tuple[int, str]:
    hasher = hashlib.sha256()
    written = 0
    try:
        async with asyncio.timeout(max(0.001, total_timeout_seconds)):
            with target.open("wb") as file:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    file.write(chunk)
                    hasher.update(chunk)
                    written += len(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return written, hasher.hexdigest()


async def download_video(url: str, title: str | None = None) -> VideoAsset:
    async with _download_semaphore:
        settings.ensure_dirs()
        video_id = new_id("video")
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix or ".mp4"
        name = safe_filename(title or Path(parsed.path).stem or video_id)
        target_dir = settings.raw_dir / video_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}{suffix}"

        host = parsed.hostname or "unknown"
        from .queue import download_host_slot

        async with download_host_slot(host):
            attempts = max(1, settings.download_retry_attempts)
            for attempt in range(1, attempts + 1):
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            connect=settings.download_connect_timeout_seconds,
                            read=120.0,
                            write=30.0,
                            pool=30.0,
                        ),
                        follow_redirects=True,
                    ) as client:
                        async with client.stream("GET", url) as response:
                            response.raise_for_status()
                            declared_length = int(response.headers.get("content-length") or 0) or None
                            etag = response.headers.get("etag")
                            content_length, digest = await _stream_response_to_file(
                                response,
                                target,
                                total_timeout_seconds=settings.download_total_timeout_seconds,
                            )
                    break
                except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as exc:
                    target.unlink(missing_ok=True)
                    code, retryable, status_code = _download_error_details(exc, host)
                    if not retryable or attempt == attempts:
                        try:
                            target_dir.rmdir()
                        except OSError:
                            pass
                        raise SourceDownloadError(
                            code=code,
                            retryable=retryable,
                            host=host,
                            attempts=attempt,
                            status_code=status_code,
                            cause=exc,
                        ) from exc
                    await asyncio.sleep(_retry_delay_seconds(attempt))
                except BaseException:
                    target.unlink(missing_ok=True)
                    try:
                        target_dir.rmdir()
                    except OSError:
                        pass
                    raise

    return VideoAsset(
        video_id=video_id,
        source_url=url,
        local_path=str(target),
        sha256=digest,
        content_length=declared_length or content_length,
        etag=etag,
    )


def register_local_video(path: str, title: str | None = None) -> VideoAsset:
    local = Path(path)
    if not local.exists():
        raise FileNotFoundError(path)
    return VideoAsset(
        video_id=new_id("video"),
        local_path=str(local),
        sha256=sha256_file(local),
        content_length=local.stat().st_size,
    )


async def download_oss_video(
    bucket: str,
    object_key: str,
    *,
    title: str | None = None,
    endpoint: str | None = None,
    etag: str | None = None,
    content_length: int | None = None,
) -> VideoAsset:
    settings.ensure_dirs()
    video_id = new_id("video")
    suffix = Path(object_key).suffix or ".mp4"
    name = safe_filename(title or Path(object_key).stem or video_id)
    target_dir = settings.raw_dir / video_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}{suffix}"
    metadata = await download_oss_object(bucket, object_key, target)
    return VideoAsset(
        video_id=video_id,
        source_url=f"oss://{bucket}/{object_key}",
        local_path=str(target),
        sha256=sha256_file(target),
        content_length=content_length or metadata.get("content_length") or os.path.getsize(target),
        etag=etag or metadata.get("etag"),
        oss_bucket=bucket,
        oss_key=object_key,
        oss_endpoint=endpoint or settings.oss_endpoint,
    )
