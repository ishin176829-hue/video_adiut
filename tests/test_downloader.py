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


def test_stream_response_enforces_total_timeout_and_removes_partial_file(tmp_path):
    async def scenario():
        target = tmp_path / "video.mp4"

        with pytest.raises(TimeoutError):
            await _stream_response_to_file(
                FakeResponse([b"abc", b"def"], delay=0.03),
                target,
                total_timeout_seconds=0.04,
            )

        assert not target.exists()

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
