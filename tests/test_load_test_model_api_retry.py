import asyncio
import importlib.util
from pathlib import Path


def _load_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_test_model_api.py"
    spec = importlib.util.spec_from_file_location("load_test_model_api", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeModels:
    def __init__(self):
        self.calls = 0

    async def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return type("Response", (), {"text": ""})()
        return type("Response", (), {"text": '{"ok": true}'})()


class FakeClient:
    def __init__(self):
        self.aio = type("Aio", (), {"models": FakeModels()})()


def test_call_once_can_retry_json_decode_error(monkeypatch):
    async def scenario():
        module = _load_script_module()

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr("video_review.model_retry.asyncio.sleep", fake_sleep)
        client = FakeClient()

        result = await module.call_once(
            client,
            model="fake-model",
            index=1,
            timeout_seconds=1,
            retry_attempts=2,
            retry_backoff_seconds=0.01,
        )

        assert result["status"] == "ok"
        assert result["attempts"] == 2
        assert result["retry_counts"] == {"parse": 1}
        assert client.aio.models.calls == 2

    asyncio.run(scenario())
