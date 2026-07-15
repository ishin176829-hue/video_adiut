import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from video_review.analyzer import MultimodalAnalyzer
from video_review.models import SegmentReviewResult
from video_review.provider_adapters import GrokProviderAdapter, ProviderImage
from video_review.providers import ModelChannel


def test_gemini_safety_settings_only_contain_four_adjustable_categories():
    analyzer = MultimodalAnalyzer()

    categories = {setting.category.name for setting in analyzer._safety_settings()}

    assert categories == {
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    }


def test_grok_adapter_sends_images_and_strict_json_schema(monkeypatch, tmp_path):
    async def scenario():
        image_path = tmp_path / "frame.jpg"
        image_path.write_bytes(b"jpeg-data")
        captured = {}
        qpm_entries = []

        @asynccontextmanager
        async def qpm_slot():
            qpm_entries.append("entered")
            yield

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": '{"risk_score":0,"findings":[]}'}}]}

        class Client:
            async def post(self, url, **kwargs):
                captured["url"] = url
                captured.update(kwargs)
                return Response()

            async def aclose(self):
                return None

        lease = SimpleNamespace(api_key="xai-key", key_id="xai-id", scheduler_index=0, lease_id=None)

        class Pool:
            async def acquire(self):
                return lease

            async def release(self, lease_arg, *, success, error_kind=None):
                assert lease_arg is lease

        channel = ModelChannel(
            provider_id="xai",
            family="grok",
            base_url="https://api.x.ai/v1",
            contract_id="openai-chat-json-schema",
            model="grok-4.5",
            api_keys=("xai-key",),
        )
        adapter = GrokProviderAdapter(
            channel,
            Pool(),
            client=Client(),
            qpm_slot_factory=qpm_slot,
        )
        text = await adapter.generate(
            prompt="审核画面",
            images=[ProviderImage(path=Path(image_path), label="00:01")],
            response_schema=SegmentReviewResult,
        )

        assert text.startswith("{")
        assert captured["url"] == "https://api.x.ai/v1/chat/completions"
        payload = captured["json"]
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        content = payload["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": "审核画面"}
        assert content[1]["type"] == "text"
        assert content[2]["image_url"]["url"] == (
            "data:image/jpeg;base64," + base64.b64encode(b"jpeg-data").decode("ascii")
        )
        assert qpm_entries == ["entered"]

    asyncio.run(scenario())
