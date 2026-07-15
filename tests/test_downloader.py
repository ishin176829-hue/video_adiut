import asyncio
import hashlib

import httpx
import pytest

from video_review import downloader
from video_review.downloader import SourceDownloadError, _stream_response_to_file
from video_review.config import settings


class FakeResponse:
    def __init__(self, chunks, *, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.headers = {}

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk


def test_stream_response_hashes_while_writing(tmp_path):
    async def scenario():
        target = tmp_path / "video.mp4"
        chunks = [b"abc", b"def"]

        written, digest = await _stream_response_to_file(
            FakeResponse(chunks),
            target,
            total_timeout_seconds=1,
        )

        assert written == 6
        assert target.read_bytes() == b"abcdef"
        assert digest == hashlib.sha256(b"abcdef").hexdigest()

    asyncio.run(scenario())


def test_stream_response_enforces_total_timeout_and_preserves_partial_file(tmp_path):
    async def scenario():
        target = tmp_path / "video.mp4"

        with pytest.raises(TimeoutError):
            await _stream_response_to_file(
                FakeResponse([b"abc", b"def"], delay=0.03),
                target,
                total_timeout_seconds=0.04,
            )

        assert target.read_bytes() == b"abc"

    asyncio.run(scenario())


def test_download_turns_total_stream_timeout_into_resumable_source_error(monkeypatch, tmp_path):
    class StreamContext:
        async def __aenter__(self):
            return FakeResponse([b"abc", b"def"], delay=0.03)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):
            return StreamContext()

    async def scenario():
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "download_retry_attempts", 1)
        monkeypatch.setattr(settings, "download_total_timeout_seconds", 0.04)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0)
        monkeypatch.setattr(settings, "source_oss_cache_enabled", False)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_timeout")
        monkeypatch.setattr(downloader.httpx, "AsyncClient", Client)

        with pytest.raises(SourceDownloadError) as exc_info:
            await downloader.download_video("https://example.com/video.mp4", "demo")

        assert exc_info.value.code == "source_timeout"
        assert exc_info.value.retryable is True
        assert exc_info.value.partial_path
        assert Path(exc_info.value.partial_path).read_bytes() == b"abc"

    from pathlib import Path

    asyncio.run(scenario())


def test_download_resumes_partial_file_with_range(monkeypatch, tmp_path):
    class InterruptedResponse(FakeResponse):
        status_code = 200

        async def aiter_bytes(self, chunk_size):
            yield b"abc"
            raise httpx.ReadTimeout("read timed out")

    class ResumeResponse(FakeResponse):
        status_code = 206

        def __init__(self):
            super().__init__([b"def"])
            self.headers = {"content-range": "bytes 3-5/6", "content-length": "3"}

    class StreamContext:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        calls = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):
            type(self).calls.append(kwargs)
            response = InterruptedResponse([]) if len(type(self).calls) == 1 else ResumeResponse()
            return StreamContext(response)

    async def scenario():
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "download_retry_attempts", 2)
        monkeypatch.setattr(settings, "download_retry_delay_seconds", 0)
        monkeypatch.setattr(settings, "download_retry_jitter_seconds", 0)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0)
        monkeypatch.setattr(settings, "source_oss_cache_enabled", False, raising=False)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_resume")
        monkeypatch.setattr(downloader.httpx, "AsyncClient", FakeClient)

        asset = await downloader.download_video("https://example.com/video.mp4", "demo")

        assert Path(asset.local_path).read_bytes() == b"abcdef"
        assert asset.sha256 == hashlib.sha256(b"abcdef").hexdigest()
        assert FakeClient.calls == [{}, {"headers": {"Range": "bytes=3-"}}]
        assert not Path(f"{asset.local_path}.part").exists()

    from pathlib import Path

    asyncio.run(scenario())


def test_download_restarts_when_origin_ignores_range(monkeypatch, tmp_path):
    class FullResponse(FakeResponse):
        status_code = 200

        def __init__(self):
            super().__init__([b"fresh"])
            self.headers = {"content-length": "5"}

    class StreamContext:
        async def __aenter__(self):
            return FullResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        headers = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):
            type(self).headers = kwargs["headers"]
            return StreamContext()

    async def scenario():
        resume_dir = tmp_path / "raw" / "video_restart"
        resume_dir.mkdir(parents=True)
        part = resume_dir / "demo.mp4.part"
        part.write_bytes(b"stale")
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "download_retry_attempts", 1)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0)
        monkeypatch.setattr(settings, "source_oss_cache_enabled", False, raising=False)
        monkeypatch.setattr(downloader.httpx, "AsyncClient", FakeClient)

        asset = await downloader.download_video(
            "https://example.com/video.mp4",
            "demo",
            resume_path=str(part),
        )

        assert Path(asset.local_path).read_bytes() == b"fresh"
        assert FakeClient.headers == {"Range": "bytes=5-"}

    from pathlib import Path

    asyncio.run(scenario())


def test_source_cache_key_is_deterministic_and_hides_url():
    first = downloader.source_cache_object_key("https://qiniu.duanju.com/private/video.mp4?token=secret")
    second = downloader.source_cache_object_key("https://qiniu.duanju.com/private/video.mp4?token=secret")

    assert first == second
    assert "secret" not in first
    assert first.endswith(".mp4")


def test_source_cache_hit_bypasses_origin_http(monkeypatch, tmp_path):
    async def scenario():
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "oss_bucket", "cache-bucket")
        monkeypatch.setattr(settings, "source_oss_cache_enabled", True, raising=False)
        monkeypatch.setattr(settings, "source_oss_cache_prefix", "cache/source", raising=False)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_cache")

        async def fake_head(bucket, key):
            return {"content_length": 6, "etag": "cache-etag"}

        async def fake_download(bucket, key, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"cached")
            return {"content_length": 6, "etag": "cache-etag"}

        class ForbiddenClient:
            def __init__(self, **kwargs):
                raise AssertionError("origin HTTP must not be used on an OSS cache hit")

        monkeypatch.setattr(downloader, "head_oss_object", fake_head)
        monkeypatch.setattr(downloader, "download_oss_object", fake_download)
        monkeypatch.setattr(downloader.httpx, "AsyncClient", ForbiddenClient)

        asset = await downloader.download_video("https://qiniu.duanju.com/video.mp4", "demo")

        assert Path(asset.local_path).read_bytes() == b"cached"
        assert asset.oss_bucket == "cache-bucket"
        assert asset.etag == "cache-etag"

    from pathlib import Path

    asyncio.run(scenario())


def test_source_cache_upload_failure_does_not_fail_origin_download(monkeypatch, tmp_path):
    class Response(FakeResponse):
        status_code = 200

        def __init__(self):
            super().__init__([b"origin"])
            self.headers = {"content-length": "6"}

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):
            return StreamContext()

    async def scenario():
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "oss_bucket", "cache-bucket")
        monkeypatch.setattr(settings, "source_oss_cache_enabled", True)
        monkeypatch.setattr(settings, "download_retry_attempts", 1)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_cache_upload")

        async def cache_miss(*args, **kwargs):
            raise RuntimeError("not found")

        async def upload_failure(*args, **kwargs):
            raise RuntimeError("OSS unavailable")

        monkeypatch.setattr(downloader, "head_oss_object", cache_miss)
        monkeypatch.setattr(downloader, "upload_oss_object", upload_failure)
        monkeypatch.setattr(downloader.httpx, "AsyncClient", Client)

        asset = await downloader.download_video("https://qiniu.duanju.com/video.mp4", "demo")
        await asyncio.sleep(0)

        assert Path(asset.local_path).read_bytes() == b"origin"

    from pathlib import Path

    asyncio.run(scenario())


def test_download_retries_connect_timeout_before_failing(monkeypatch, tmp_path):
    class StreamContext:
        def __init__(self, attempt):
            self.attempt = attempt

        async def __aenter__(self):
            if self.attempt == 1:
                raise httpx.ConnectTimeout("connect timed out")
            return FakeResponse([b"video"])

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        attempts = 0

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            type(self).attempts += 1
            return StreamContext(type(self).attempts)

    async def scenario():
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "download_retry_attempts", 2, raising=False)
        monkeypatch.setattr(settings, "download_retry_delay_seconds", 0.5, raising=False)
        monkeypatch.setattr(settings, "download_retry_jitter_seconds", 0, raising=False)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0, raising=False)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_test")
        monkeypatch.setattr(downloader.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(downloader.asyncio, "sleep", fake_sleep)

        asset = await downloader.download_video("https://example.com/video.mp4", "demo")

        assert asset.content_length == 5
        assert FakeClient.attempts == 2
        assert sleeps == [0.5]

    asyncio.run(scenario())


def test_download_retries_retryable_http_status_with_jitter(monkeypatch, tmp_path):
    class Response(FakeResponse):
        def __init__(self, status_code):
            super().__init__([b"video"])
            self.status_code = status_code
            self.request = httpx.Request("GET", "https://example.com/video.mp4")

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("unavailable", request=self.request, response=self)

    class StreamContext:
        def __init__(self, attempt):
            self.attempt = attempt

        async def __aenter__(self):
            return Response(503 if self.attempt == 1 else 200)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        attempts = 0

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            type(self).attempts += 1
            return StreamContext(type(self).attempts)

    async def scenario():
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "download_retry_attempts", 2)
        monkeypatch.setattr(settings, "download_retry_delay_seconds", 1)
        monkeypatch.setattr(settings, "download_retry_jitter_seconds", 0, raising=False)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0, raising=False)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_503")
        monkeypatch.setattr(downloader.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(downloader.asyncio, "sleep", fake_sleep)

        asset = await downloader.download_video("https://example.com/video.mp4", "demo")

        assert asset.content_length == 5
        assert FakeClient.attempts == 2
        assert sleeps == [1]

    asyncio.run(scenario())


def test_download_marks_forbidden_url_as_non_retryable_source_error(monkeypatch, tmp_path):
    class Response(FakeResponse):
        status_code = 403
        request = httpx.Request("GET", "https://example.com/video.mp4")

        def raise_for_status(self):
            raise httpx.HTTPStatusError("forbidden", request=self.request, response=self)

    class StreamContext:
        async def __aenter__(self):
            return Response([b""])

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        attempts = 0

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            type(self).attempts += 1
            return StreamContext()

    async def scenario():
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "download_retry_attempts", 3)
        monkeypatch.setattr(settings, "download_host_concurrency_limit", 0, raising=False)
        monkeypatch.setattr(downloader, "new_id", lambda prefix: "video_403")
        monkeypatch.setattr(downloader.httpx, "AsyncClient", FakeClient)

        with pytest.raises(SourceDownloadError) as exc_info:
            await downloader.download_video("https://example.com/video.mp4", "demo")

        assert exc_info.value.code == "source_forbidden"
        assert exc_info.value.retryable is False
        assert FakeClient.attempts == 1

    asyncio.run(scenario())
