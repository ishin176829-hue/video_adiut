from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import settings
from .models import VideoAsset
from .oss import download_oss_object, head_oss_object, upload_oss_object
from .utils import new_id, safe_filename, sha256_file


_download_semaphore = asyncio.Semaphore(max(1, settings.download_concurrency_per_process))
_source_cache_tasks: set[asyncio.Task] = set()
logger = logging.getLogger(__name__)


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
        partial_path: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.host = host
        self.attempts = attempts
        self.status_code = status_code
        self.cause = cause
        self.partial_path = partial_path
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
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
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
    append: bool = False,
) -> tuple[int, str]:
    hasher = hashlib.sha256()
    written = 0
    if append and target.exists():
        with target.open("rb") as existing:
            while chunk := existing.read(1024 * 1024):
                hasher.update(chunk)
                written += len(chunk)
    async with asyncio.timeout(max(0.001, total_timeout_seconds)):
        with target.open("ab" if append else "wb") as file:
            if not append:
                written = 0
            async for chunk in response.aiter_bytes(1024 * 1024):
                file.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
    return written, hasher.hexdigest()


def source_cache_object_key(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}:
        suffix = ".mp4"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    prefix = settings.source_oss_cache_prefix.strip("/")
    return f"{prefix}/{digest[:2]}/{digest}{suffix}"


def _source_cache_enabled(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(settings.source_oss_cache_enabled and settings.oss_bucket and "qiniu" in host)


def _safe_resume_path(value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value).expanduser().resolve()
    raw_dir = settings.raw_dir.expanduser().resolve()
    if candidate.suffix != ".part" or not candidate.is_relative_to(raw_dir):
        raise ValueError("download_resume_path 必须是 raw 目录下的 .part 文件")
    return candidate


async def _try_source_cache(url: str, target: Path) -> tuple[str, dict] | None:
    if not _source_cache_enabled(url):
        return None
    object_key = source_cache_object_key(url)
    try:
        await head_oss_object(settings.oss_bucket, object_key)
        metadata = await download_oss_object(settings.oss_bucket, object_key, target)
        return object_key, metadata
    except Exception:
        target.unlink(missing_ok=True)
        return None


async def _upload_source_cache(url: str, source: Path) -> None:
    try:
        await upload_oss_object(settings.oss_bucket, source_cache_object_key(url), source)
    except Exception as exc:
        logger.warning("OSS source cache upload failed: %s", exc.__class__.__name__)


def _schedule_source_cache_upload(url: str, source: Path) -> None:
    if not _source_cache_enabled(url):
        return
    task = asyncio.create_task(_upload_source_cache(url, source))
    _source_cache_tasks.add(task)
    task.add_done_callback(_source_cache_tasks.discard)


def _content_range_total(value: str | None) -> int | None:
    if not value or "/" not in value:
        return None
    try:
        total = value.rsplit("/", 1)[1]
        return int(total) if total != "*" else None
    except ValueError:
        return None


async def download_video(
    url: str,
    title: str | None = None,
    *,
    resume_path: str | None = None,
) -> VideoAsset:
    async with _download_semaphore:
        settings.ensure_dirs()
        parsed = urlparse(url)
        part = _safe_resume_path(resume_path)
        if part is None:
            video_id = new_id("video")
            suffix = Path(parsed.path).suffix or ".mp4"
            name = safe_filename(title or Path(parsed.path).stem or video_id)
            target_dir = settings.raw_dir / video_id
            target = target_dir / f"{name}{suffix}"
            part = Path(f"{target}.part")
        else:
            target_dir = part.parent
            target = Path(str(part)[: -len(".part")])
            video_id = target_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)

        cached = await _try_source_cache(url, target)
        if cached is not None:
            object_key, metadata = cached
            part.unlink(missing_ok=True)
            return VideoAsset(
                video_id=video_id,
                source_url=url,
                local_path=str(target),
                sha256=sha256_file(target),
                content_length=metadata.get("content_length") or target.stat().st_size,
                etag=metadata.get("etag"),
                oss_bucket=settings.oss_bucket,
                oss_key=object_key,
                oss_endpoint=settings.oss_internal_endpoint or settings.oss_endpoint,
            )

        host = parsed.hostname or "unknown"
        from .queue import download_host_slot

        async with download_host_slot(host):
            attempts = max(1, settings.download_retry_attempts)
            for attempt in range(1, attempts + 1):
                try:
                    offset = part.stat().st_size if part.exists() else 0
                    request_kwargs = {"headers": {"Range": f"bytes={offset}-"}} if offset else {}
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            connect=settings.download_connect_timeout_seconds,
                            read=120.0,
                            write=30.0,
                            pool=30.0,
                        ),
                        follow_redirects=True,
                    ) as client:
                        async with client.stream("GET", url, **request_kwargs) as response:
                            response.raise_for_status()
                            status_code = int(getattr(response, "status_code", 200) or 200)
                            content_range = response.headers.get("content-range")
                            append = False
                            if offset and status_code == 206:
                                if not (content_range or "").lower().startswith(f"bytes {offset}-"):
                                    raise httpx.RemoteProtocolError("源站返回的 Content-Range 与本地断点不一致")
                                append = True
                            elif offset and status_code == 200:
                                append = False
                            declared_length = _content_range_total(content_range)
                            if declared_length is None:
                                response_length = int(response.headers.get("content-length") or 0) or None
                                declared_length = (offset + response_length) if append and response_length else response_length
                            etag = response.headers.get("etag")
                            content_length, digest = await _stream_response_to_file(
                                response,
                                part,
                                total_timeout_seconds=settings.download_total_timeout_seconds,
                                append=append,
                            )
                    part.replace(target)
                    _schedule_source_cache_upload(url, target)
                    break
                except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError, TimeoutError) as exc:
                    code, retryable, status_code = _download_error_details(exc, host)
                    if not retryable or attempt == attempts:
                        raise SourceDownloadError(
                            code=code,
                            retryable=retryable,
                            host=host,
                            attempts=attempt,
                            status_code=status_code,
                            cause=exc,
                            partial_path=str(part) if part.exists() else None,
                        ) from exc
                    await asyncio.sleep(_retry_delay_seconds(attempt))
                except BaseException:
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
