from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from .config import settings
from .model_retry import (
    ModelProviderExhaustedError,
    classify_model_error,
)


T = TypeVar("T")


def normalize_base_url(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def channel_fingerprint(provider_id: str, base_url: str, contract_id: str) -> str:
    identity = "|".join(
        (provider_id.strip().lower(), normalize_base_url(base_url), contract_id.strip().lower())
    )
    return f"channel_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class ModelChannel:
    provider_id: str
    family: str
    base_url: str
    contract_id: str
    model: str
    api_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        object.__setattr__(
            self,
            "api_keys",
            tuple(dict.fromkeys(key.strip() for key in self.api_keys if key and key.strip())),
        )

    @property
    def channel_id(self) -> str:
        return channel_fingerprint(self.provider_id, self.base_url, self.contract_id)


def build_model_channels(
    *,
    jz_keys: list[str],
    jz_base_url: str,
    jz_model: str,
    google_keys: list[str],
    google_model: str,
    xai_keys: list[str],
    xai_base_url: str,
    xai_model: str,
) -> list[ModelChannel]:
    channels: list[ModelChannel] = []
    if jz_keys:
        channels.append(
            ModelChannel(
                provider_id="jz",
                family="gemini",
                base_url=jz_base_url,
                contract_id="gemini-native-proxy-v1beta",
                model=jz_model,
                api_keys=tuple(jz_keys),
            )
        )
    if google_keys:
        channels.append(
            ModelChannel(
                provider_id="google",
                family="gemini",
                base_url="https://generativelanguage.googleapis.com",
                contract_id="gemini-native-v1beta",
                model=google_model,
                api_keys=tuple(google_keys),
            )
        )
    if xai_keys:
        channels.append(
            ModelChannel(
                provider_id="xai",
                family="grok",
                base_url=xai_base_url,
                contract_id="openai-chat-json-schema",
                model=xai_model,
                api_keys=tuple(xai_keys),
            )
        )
    return channels


def configured_model_channels() -> list[ModelChannel]:
    legacy_keys = settings.google_api_key_pool
    if settings.google_api_base_url:
        jz_keys = legacy_keys
        google_keys = settings.google_official_api_key_pool
    else:
        jz_keys = []
        google_keys = list(dict.fromkeys(legacy_keys + settings.google_official_api_key_pool))
    return build_model_channels(
        jz_keys=jz_keys,
        jz_base_url=settings.google_api_base_url or "https://generativelanguage.googleapis.com",
        jz_model=settings.video_review_model,
        google_keys=google_keys,
        google_model=settings.google_official_model,
        xai_keys=settings.xai_api_key_pool,
        xai_base_url=settings.xai_api_base_url,
        xai_model=settings.xai_model,
    )


class ProviderChannelHealth:
    def __init__(self, *, redis_getter=None, cooldown_seconds: int | None = None) -> None:
        self._redis_getter = redis_getter
        self._cooldown_seconds = max(
            1,
            cooldown_seconds
            if cooldown_seconds is not None
            else settings.provider_contract_cooldown_seconds,
        )
        self._local_cooldown_until: dict[str, float] = {}

    async def _get_redis(self):
        if self._redis_getter is not None:
            return await self._redis_getter()
        from .queue import get_redis

        return await get_redis()

    def _key(self, channel: ModelChannel) -> str:
        return f"{settings.redis_model_key_pool_prefix}:channel:{channel.channel_id}:cooldown"

    async def is_available(self, channel: ModelChannel) -> bool:
        if self._local_cooldown_until.get(channel.channel_id, 0) > time.monotonic():
            return False
        try:
            client = await self._get_redis()
            if client is not None and await client.exists(self._key(channel)):
                return False
        except Exception:
            pass
        return True

    async def cooldown(self, channel: ModelChannel, *, reason: str) -> None:
        self._local_cooldown_until[channel.channel_id] = time.monotonic() + self._cooldown_seconds
        try:
            client = await self._get_redis()
            if client is not None:
                await client.set(self._key(channel), reason, ex=self._cooldown_seconds)
        except Exception:
            pass

    async def success(self, channel: ModelChannel) -> None:
        self._local_cooldown_until.pop(channel.channel_id, None)


class ProviderRouter:
    def __init__(
        self,
        channels: list[ModelChannel],
        *,
        health: ProviderChannelHealth | Any | None = None,
        excluded_families: set[str] | None = None,
    ) -> None:
        self.channels = list(channels)
        self.health = health or ProviderChannelHealth()
        self.excluded_families = set(excluded_families or set())

    async def execute(self, operation: Callable[[ModelChannel], Awaitable[T]]) -> T:
        attempts: list[dict[str, str]] = []
        last_kind = "transient"
        for channel in self.channels:
            if channel.family in self.excluded_families:
                continue
            if not await self.health.is_available(channel):
                continue
            try:
                result = await operation(channel)
            except Exception as exc:
                kind = classify_model_error(exc)
                last_kind = kind
                attempts.append(
                    {
                        "provider_id": channel.provider_id,
                        "channel_id": channel.channel_id,
                        "contract_id": channel.contract_id,
                        "error_kind": kind,
                    }
                )
                if kind == "provider_block":
                    family = getattr(exc, "family", channel.family)
                    self.excluded_families.add(family)
                    continue
                if kind in {"parse", "validation", "auth"}:
                    await self.health.cooldown(channel, reason=kind)
                continue
            await self.health.success(channel)
            return result

        if "gemini" in self.excluded_families and not any(
            channel.family not in self.excluded_families for channel in self.channels
        ):
            message = "Gemini 内容保护已拦截，但未配置可用的独立模型渠道"
            last_kind = "provider_block"
        else:
            message = "所有可用模型渠道均调用失败"
        raise ModelProviderExhaustedError(
            attempts=attempts,
            excluded_families=self.excluded_families,
            error_kind=last_kind,
            message=message,
        )
