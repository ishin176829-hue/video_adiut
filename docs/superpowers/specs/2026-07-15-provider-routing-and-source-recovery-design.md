# Provider Routing And Source Recovery Design

## Goal

Make model failures recoverable without treating multiple keys on one proxy as independent providers. A review must route by provider channel, switch away from incompatible contracts, use a non-Gemini fallback for Gemini core-policy blocks, and preserve partial source downloads across retries.

## Constraints

- A channel identity is the tuple `provider + normalized base_url + contract_id`.
- All current `GOOGLE_API_KEY(S)` values behind `jzapi.duanju.com` belong to one JZ channel.
- API keys remain secret. Redis, events, and logs store only channel IDs and key fingerprints.
- Contract errors never contribute to the aggregate model circuit.
- `PROHIBITED_CONTENT` and equivalent Gemini core-policy blocks exclude the entire Gemini family for the remainder of the logical review attempt.
- A blocked Gemini request is never retried against another Gemini key or Gemini endpoint.
- Grok is optional and disabled until `XAI_API_KEY` is configured. A missing non-Gemini fallback is reported explicitly instead of pretending that JZ is redundant.
- Existing public review APIs and result schemas remain unchanged.
- The default frame-sheet pipeline is the primary failover target. Grok handles text and JPEG/PNG inputs; direct Gemini Files API video fallback remains Gemini-only.

## Provider Model

`ModelChannel` describes one independently routed channel:

- `provider_id`: `jz`, `google`, or `xai`.
- `family`: `gemini` or `grok`.
- `base_url`: normalized API origin.
- `contract_id`: `gemini-native-proxy-v1beta`, `gemini-native-v1beta`, or `openai-chat-json-schema`.
- `model`: model name for the channel.
- `api_keys`: one or more keys sharing the same provider contract and failure domain.
- `channel_id`: a stable, non-secret hash of provider, base URL, and contract ID.

Configured channels are ordered as follows:

1. Existing JZ Gemini proxy.
2. Optional direct Google Gemini Developer API channel.
3. Optional xAI Grok channel.

Google direct is a proxy-failure fallback but remains in the Gemini family. Grok is the required fallback for Gemini core-policy blocks.

## Routing Rules

For each logical model operation, the router keeps an exclusion set:

1. A successful channel ends routing and resets that channel's failure count.
2. `parse` or `validation` marks only the current channel contract unhealthy, applies a channel cooldown, and tries the next channel.
3. `auth`, `rate_limit`, and `transient` update only that channel/key health and try another available channel.
4. `provider_block` adds the entire provider family to the operation exclusion set. For Gemini this skips JZ and direct Google and routes directly to Grok.
5. If every eligible channel fails, `ModelProviderExhaustedError` carries the attempted channel IDs, failure kinds, and excluded families to the task-level retry workflow.

The analyzer owns one routing context for the whole review stage. Once any batch receives a Gemini core-policy block, later batches in that stage also skip Gemini. If task-level retry is needed, `model_excluded_families` is copied into request metadata so the delayed retry does not call Gemini again.

## Channel Health And Circuits

Key concurrency and rate-limit cooldown remain inside the existing key pool, but its Redis keys are namespaced by `channel_id`. A new lightweight channel state records consecutive failures and cooldown deadlines using the same Redis prefix.

The existing aggregate circuit observes only the final outcome of a logical model operation. Internal channel failures that are recovered by another channel are not aggregate failures. `parse`, `validation`, `auth`, and `provider_block` never open the aggregate circuit.

## Provider Adapters

### Gemini Native

The JZ and direct Google channels use the Google GenAI SDK. JZ supplies its configured base URL; direct Google leaves the SDK base URL unset. Both use only these adjustable categories:

- `HARM_CATEGORY_HARASSMENT`
- `HARM_CATEGORY_HATE_SPEECH`
- `HARM_CATEGORY_SEXUALLY_EXPLICIT`
- `HARM_CATEGORY_DANGEROUS_CONTENT`

`HARM_CATEGORY_CIVIC_INTEGRITY` and all `HARM_CATEGORY_IMAGE_*` values are removed. The configured threshold remains `BLOCK_NONE` unless explicitly changed.

Before reading response text, the adapter inspects prompt feedback and candidate finish reasons. `PROHIBITED_CONTENT`, `SAFETY`, and equivalent empty blocked responses become `ModelProviderBlockedError`, not parse errors.

### Grok OpenAI-Compatible

The xAI adapter calls `/v1/chat/completions` with `grok-4.5` by default. Text prompts are sent as text content, and frame/sheet images are sent as base64 data URLs. Pydantic schemas are passed through `response_format.type=json_schema`, and response text is read from `choices[0].message.content` before the existing normalization layer validates it.

The adapter does not accept Gemini `safetySettings`. It is selected because it is an independent non-Gemini family, not because it emulates Gemini.

## Source Download Recovery

HTTP source downloads use a `.part` file and preserve it after retryable failures. The next request sends `Range: bytes=<existing_size>-`:

- `206` with a matching `Content-Range` appends to the partial file.
- `200` after a range request means the source ignored range; the file is safely restarted.
- A successful download atomically renames `.part` to the final video path and computes SHA-256 over the completed file.
- The partial path is stored in delayed-retry request metadata, so another worker can resume the same task-level download.
- Terminal source failure deletes the partial artifact through the existing cleanup path.

When OSS source caching is enabled, a deterministic object key derived from the source URL is checked before contacting Qiniu. A cache hit downloads through the OSS internal endpoint. A completed Qiniu download is uploaded to that key for later submissions and retries. Cache failures are recorded but never make an otherwise valid review fail.

## Configuration

New optional environment variables:

- `GOOGLE_OFFICIAL_API_KEY`, `GOOGLE_OFFICIAL_API_KEYS`
- `GOOGLE_OFFICIAL_MODEL` (default `gemini-2.5-flash`)
- `XAI_API_KEY`, `XAI_API_KEYS`
- `XAI_API_BASE_URL` (default `https://api.x.ai/v1`)
- `XAI_MODEL` (default `grok-4.5`)
- `VIDEO_REVIEW_PROVIDER_CONTRACT_COOLDOWN_SECONDS` (default `300`)
- `VIDEO_REVIEW_SOURCE_OSS_CACHE_ENABLED` (default `1` when OSS is configured)
- `VIDEO_REVIEW_SOURCE_OSS_CACHE_PREFIX` (default `<ALIYUN_OSS_PREFIX>/source-cache`)

## Observability

Every provider attempt event records `provider_id`, `channel_id`, `contract_id`, model, key fingerprint, error kind, and whether failover occurred. Raw keys and response bodies are never logged. Provider-block events additionally record the normalized block reason and excluded family.

## Acceptance Tests

- Three JZ keys produce one channel ID and distinct key fingerprints.
- A direct Google key produces a different channel ID even with the same Gemini contract family.
- Contract failure cools only its channel and the next channel is attempted.
- Contract failure does not call aggregate circuit telemetry as a failure.
- `PROHIBITED_CONTENT` skips every Gemini channel and calls Grok once.
- A delayed task retry preserves the Gemini-family exclusion.
- Gemini safety settings contain exactly four categories.
- Interrupted downloads retain `.part`, issue a valid Range request, and finish with correct bytes and hash.
- A source-cache hit bypasses the Qiniu HTTP client.
- A source-cache upload failure does not fail the completed source download.

