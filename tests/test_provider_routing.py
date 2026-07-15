import asyncio

import pytest

from video_review.model_retry import ModelContractError, ModelProviderBlockedError
from video_review.providers import (
    ModelChannel,
    ProviderRouter,
    build_model_channels,
    channel_fingerprint,
)


def test_jz_keys_share_one_channel_but_independent_origins_do_not():
    channels = build_model_channels(
        jz_keys=["jz-a", "jz-b", "jz-c"],
        jz_base_url="https://jzapi.duanju.com/",
        jz_model="gemini-2.5-flash",
        google_keys=["google-a"],
        google_model="gemini-2.5-flash",
        xai_keys=["xai-a"],
        xai_base_url="https://api.x.ai/v1/",
        xai_model="grok-4.5",
    )

    assert [channel.provider_id for channel in channels] == ["jz", "google", "xai"]
    assert channels[0].api_keys == ("jz-a", "jz-b", "jz-c")
    assert len({channel.channel_id for channel in channels}) == 3
    assert channels[0].channel_id == channel_fingerprint(
        "jz",
        "https://jzapi.duanju.com",
        "gemini-native-proxy-v1beta",
    )


def test_channel_identity_ignores_key_rotation():
    first = ModelChannel(
        provider_id="jz",
        family="gemini",
        base_url="https://jzapi.duanju.com",
        contract_id="gemini-native-proxy-v1beta",
        model="gemini-2.5-flash",
        api_keys=("old-key",),
    )
    second = ModelChannel(
        provider_id="jz",
        family="gemini",
        base_url="https://jzapi.duanju.com/",
        contract_id="gemini-native-proxy-v1beta",
        model="gemini-2.5-flash",
        api_keys=("new-key",),
    )

    assert first.channel_id == second.channel_id


def test_contract_error_cools_only_current_channel_and_fails_over():
    async def scenario():
        channels = build_model_channels(
            jz_keys=["jz-a"],
            jz_base_url="https://jzapi.duanju.com",
            jz_model="gemini-2.5-flash",
            google_keys=["google-a"],
            google_model="gemini-2.5-flash",
            xai_keys=[],
            xai_base_url="https://api.x.ai/v1",
            xai_model="grok-4.5",
        )
        cooled = []

        class Health:
            async def is_available(self, channel):
                return True

            async def cooldown(self, channel, *, reason):
                cooled.append((channel.channel_id, reason))

            async def success(self, channel):
                return None

        calls = []

        async def operation(channel):
            calls.append(channel.provider_id)
            if channel.provider_id == "jz":
                raise ModelContractError("代理返回非 JSON", kind="parse")
            return "ok"

        router = ProviderRouter(channels, health=Health())
        assert await router.execute(operation) == "ok"
        assert calls == ["jz", "google"]
        assert cooled == [(channels[0].channel_id, "parse")]

    asyncio.run(scenario())


def test_prohibited_content_excludes_all_gemini_channels_and_calls_grok_once():
    async def scenario():
        channels = build_model_channels(
            jz_keys=["jz-a"],
            jz_base_url="https://jzapi.duanju.com",
            jz_model="gemini-2.5-flash",
            google_keys=["google-a"],
            google_model="gemini-2.5-flash",
            xai_keys=["xai-a"],
            xai_base_url="https://api.x.ai/v1",
            xai_model="grok-4.5",
        )
        calls = []

        async def operation(channel):
            calls.append(channel.provider_id)
            if channel.family == "gemini":
                raise ModelProviderBlockedError("PROHIBITED_CONTENT")
            return "grok-ok"

        router = ProviderRouter(channels)
        assert await router.execute(operation) == "grok-ok"
        assert calls == ["jz", "xai"]
        assert router.excluded_families == {"gemini"}

    asyncio.run(scenario())


def test_provider_router_reports_missing_independent_fallback():
    async def scenario():
        channels = build_model_channels(
            jz_keys=["jz-a"],
            jz_base_url="https://jzapi.duanju.com",
            jz_model="gemini-2.5-flash",
            google_keys=[],
            google_model="gemini-2.5-flash",
            xai_keys=[],
            xai_base_url="https://api.x.ai/v1",
            xai_model="grok-4.5",
        )
        router = ProviderRouter(channels)

        async def operation(channel):
            raise ModelProviderBlockedError("PROHIBITED_CONTENT")

        with pytest.raises(RuntimeError, match="独立模型渠道") as exc_info:
            await router.execute(operation)
        assert exc_info.value.excluded_families == {"gemini"}

    asyncio.run(scenario())
