# API reference

## Clients

### `Svara(api_key=None, *, base_url=None, timeout=30.0, http_client=None)`
Synchronous client. `api_key` falls back to `$SVARA_API_KEY`; `base_url` to
`$SVARA_BASE_URL` or `https://api.kenpathlabs.com`. Use as a context manager or
call `.close()`. Resources: `.speech`, `.voices`.

### `AsyncSvara(...)`
Same constructor, async. `await client.aclose()` or `async with`. Resources:
`.speech` (adds `stream_input`), `.voices`.

## `client.speech`

### `create(*, input, voice, response_format="mp3", model="svara-1", sample_rate=None, speed=None, language=None, temperature=None, top_p=None, top_k=None, repetition_penalty=None, presence_penalty=None, extra_body=None) -> bytes`
Synthesize `input` and return the full audio. Async variant is awaitable.

### `stream(*, ..., chunk_size=4096) -> Iterator[bytes]`
Same params; streams audio chunks as generated. `response_format` defaults to
`"pcm"`. Async variant yields via `async for`.

### `stream_input(text, *, voice, response_format="pcm", mode="eager", chunk_words=4, peek_words=2, max_chunk_words=20, sample_rate=None, language=None, <sampling>, on_event=None) -> AsyncIterator[bytes]`
**Async only.** `text` is a sync or async iterable of strings (e.g. an LLM token
stream). Opens the input-streaming WebSocket and yields audio as Svara speaks,
holding back only `peek_words`. `on_event(ChunkEvent)` fires per spoken chunk.

### `save(path, **create_kwargs) -> str`
Convenience: `create(...)` then write to `path`.

### Parameters

| Param | Meaning |
|---|---|
| `input` | Text. Any language/script; code-switching automatic. No SSML/markup. |
| `voice` | Voice id, e.g. `sv_enhdbrj5` (see `voices.list()`). |
| `response_format` | `mp3`·`opus`·`aac`·`flac`·`wav`·`pcm`·`ulaw`·`alaw`. |
| `sample_rate` | Override rate (Hz). PCM default 24000; `ulaw`/`alaw` are 8000. |
| `speed` | Speaking-rate multiplier. |
| `language` | Force a language (`lang`), e.g. `"hi"`. Usually leave unset. |
| `temperature`,`top_p`,`top_k`,`repetition_penalty`,`presence_penalty` | Sampling; omit to use server-certified defaults. |
| `extra_body` | Escape hatch: extra JSON fields merged into the request. |

## `client.voices`

### `list() -> list[Voice]`
All voices available to your key.

### `Voice`
`voice_id, name, gender, accent_family, description, model_id, category,
curated, is_default, preview_url, labels: dict, raw: dict`. Property
`.language` → best-effort ISO code from `labels`.

## Formats (`svara.FORMAT_INFO`)

| Format | Content-type | Container | Default rate |
|---|---|---|---|
| `mp3` | audio/mpeg | yes | 24 kHz |
| `opus` | audio/ogg | yes | 24 kHz |
| `aac` | audio/aac | yes | 24 kHz |
| `flac` | audio/flac | yes | 24 kHz |
| `wav` | audio/wav | yes | 24 kHz |
| `pcm` | audio/pcm | no (s16le) | 24 kHz |
| `ulaw` | audio/basic | no (G.711 µ-law) | 8 kHz |
| `alaw` | audio/basic | no (G.711 A-law) | 8 kHz |

## Exceptions

`SvaraError` (base) → `APIConnectionError` → `APITimeoutError`;
`APIStatusError` → `AuthenticationError` (401), `PermissionError_` (403),
`NotFoundError` (404), `BadRequestError` (400/422), `RateLimitError` (429).
Each carries `.status_code`, `.body`, `.request_id`.

## HTTP endpoints (under the hood)

- `POST /v1/audio/speech` — synth (`stream: true` for chunked)
- `wss …/v1/audio/speech/stream-input` — eager input-streaming
- `GET /v1/voices` — list voices

Auth header: `xi-api-key: <key>` (or `Authorization: Bearer <key>`).
