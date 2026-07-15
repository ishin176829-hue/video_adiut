from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from google import genai
from google.genai import types

from .api_key_pool import GoogleApiKeyPool
from .config import settings
from .model_retry import ModelContractError, ModelProviderBlockedError, classify_model_error
from .prompts import SYSTEM_PROMPT
from .providers import ModelChannel


@dataclass(frozen=True)
class ProviderImage:
    path: Path
    label: str = ""
    mime_type: str = "image/jpeg"


def gemini_safety_settings() -> list[types.SafetySetting] | None:
    threshold_name = (settings.gemini_safety_threshold or "").strip().upper()
    if not threshold_name:
        return None
    threshold = getattr(types.HarmBlockThreshold, threshold_name, None)
    if threshold is None:
        threshold = types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    categories = (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
    return [types.SafetySetting(category=category, threshold=threshold) for category in categories]


def _enum_text(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "name", value) or "").upper()


def gemini_block_reason(response: Any) -> str | None:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    reason = _enum_text(getattr(prompt_feedback, "block_reason", None))
    if reason and reason not in {"BLOCK_REASON_UNSPECIFIED", "0"}:
        return reason
    for candidate in getattr(response, "candidates", None) or []:
        finish_reason = _enum_text(getattr(candidate, "finish_reason", None))
        if any(token in finish_reason for token in ("PROHIBITED_CONTENT", "SAFETY", "BLOCKLIST")):
            return finish_reason
    return None


def response_text(response: Any) -> str:
    block_reason = gemini_block_reason(response)
    if block_reason:
        raise ModelProviderBlockedError(block_reason, family="gemini")
    try:
        text = response.text
    except Exception as exc:
        raise ModelContractError("模型响应文本不可读取", kind="parse") from exc
    if not isinstance(text, str) or not text.strip():
        raise ModelContractError("模型返回空内容", kind="parse")
    return text.strip()


class GeminiProviderAdapter:
    def __init__(
        self,
        channel: ModelChannel,
        pool: GoogleApiKeyPool,
        *,
        qpm_slot_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.channel = channel
        self.pool = pool
        self._qpm_slot_factory = qpm_slot_factory
        self._clients: dict[str, Any] = {}

    def _client_for_key(self, api_key: str):
        client = self._clients.get(api_key)
        if client is None:
            http_options = None
            if self.channel.provider_id != "google":
                http_options = {"base_url": self.channel.base_url}
            client = genai.Client(api_key=api_key, http_options=http_options)
            self._clients[api_key] = client
        return client

    async def generate(self, *, prompt: str, images: list[ProviderImage], response_schema) -> str:
        lease = await self.pool.acquire()
        client = self._client_for_key(lease.api_key)
        parts = [types.Part.from_text(text=prompt)]
        for image in images:
            if image.label:
                parts.append(types.Part.from_text(text=image.label))
            parts.append(types.Part.from_bytes(data=image.path.read_bytes(), mime_type=image.mime_type))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=response_schema,
            temperature=0.2,
            safety_settings=gemini_safety_settings(),
        )
        try:
            if self._qpm_slot_factory is not None:
                async with self._qpm_slot_factory():
                    response = await client.aio.models.generate_content(
                        model=self.channel.model,
                        contents=parts,
                        config=config,
                    )
            else:
                response = await client.aio.models.generate_content(
                    model=self.channel.model,
                    contents=parts,
                    config=config,
                )
            text = response_text(response)
        except Exception as exc:
            await self.pool.release(lease, success=False, error_kind=classify_model_error(exc))
            raise
        await self.pool.release(lease, success=True)
        return text

    async def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if close:
                close()


class GrokProviderAdapter:
    def __init__(
        self,
        channel: ModelChannel,
        pool: GoogleApiKeyPool,
        *,
        client: httpx.AsyncClient | Any | None = None,
        qpm_slot_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.channel = channel
        self.pool = pool
        self._qpm_slot_factory = qpm_slot_factory
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))

    async def generate(self, *, prompt: str, images: list[ProviderImage], response_schema) -> str:
        lease = await self.pool.acquire()
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            if image.label:
                content.append({"type": "text", "text": image.label})
            encoded = base64.b64encode(image.path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"},
                }
            )
        payload = {
            "model": self.channel.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.2,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "strict": True,
                    "schema": response_schema.model_json_schema(),
                },
            },
        }
        try:
            if self._qpm_slot_factory is not None:
                async with self._qpm_slot_factory():
                    response = await self._client.post(
                        f"{self.channel.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {lease.api_key}"},
                        json=payload,
                    )
            else:
                response = await self._client.post(
                    f"{self.channel.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {lease.api_key}"},
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ModelContractError("Grok 返回空内容", kind="parse")
        except Exception as exc:
            await self.pool.release(lease, success=False, error_kind=classify_model_error(exc))
            raise
        await self.pool.release(lease, success=True)
        return text.strip()

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close:
            await close()
