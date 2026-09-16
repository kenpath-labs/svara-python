# API reference

`svara-voice` 0.2. Everything public is importable from `svara`.

## Clients

### `Svara(api_key=None, *, base_url=None, timeout=None, max_retries=2, http_client=None)`

Synchronous client. `api_key` falls back to `$SVARA_API_KEY`; `base_url` to
`$SVARA_BASE_URL`, then `https://api.kenpathlabs.com`.

- `timeout` — a float (read, write and pool timeout; connect stays 5 s) or an
  `httpx.Timeout`. Default `Timeout(120.0, connect=5.0)`: a non-streaming
  `create()` of the 5,000-character maximum takes ~50 s to render.
- `max_retries` — retries for connection errors, 429 and 5xx, with jittered
  exponential backoff (0.5 s → 8 s cap) and `Retry-After` honoured. Streams
  retry only until the first byte. `insufficient_quota` is never retried.
- `http_client` — your own `httpx.Client`. The SDK then neither closes it nor
  writes headers onto it; auth goes per request. Without one, the SDK builds a
  client whose pool keeps idle connections for 120 s (httpx's default of 5 s
  cost +140 ms per voice-agent turn, measured).

Resources: `.speech`, `.voices`, `.languages`, `.usage`. Methods: `warm_up()`
(opens the connection now; ~100 ms off the first call), `close()`. Usable as a
context manager. A `Svara` may be shared across threads (httpx's pool is
thread-safe).

### `AsyncSvara(..., ssl_context=None)`

Same arguments; `http_client` is an `httpx.AsyncClient`. Close with
`await client.aclose()` or `async with`. Belongs to one event loop. `.speech`
adds `prepare()`. `ssl_context` overrides the process-wide TLS context used
for the WebSocket path (built once, shared: 9–17 ms per connect saved).

A `SpeechStream`, `AsyncSpeechStream` or `PreparedStream` has one consumer.

## `client.speech`

### `create(*, input, voice, response_format="mp3", model="svara-1", sample_rate=None, speed=None, language=None, normalize=None, bitrate_kbps=None, temperature=None, top_p=None, top_k=None, repetition_penalty=None, presence_penalty=None, pronunciation_dictionary_id=None, extra_body=None, timeout=None) -> SpeechResponse`

Synthesize `input` and return the whole clip. Async variant is awaitable.

`SpeechResponse` is `bytes` plus `headers`, `content_type`, `sample_rate`,
`request_id`, `rate_limit` (`RateLimitInfo`) and `save(path)`.

### `stream(*, …same as create…, chunk_size=None) -> SpeechStream`

Same parameters; `response_format` defaults to `"pcm"`. The request is sent on
the first iteration. Yields audio blocks as they arrive; `chunk_size=None`
means no client-side re-buffering. Pass a number only for fixed-size frames
(telephony: 160 B = 20 ms of 8 kHz µ-law). Async variant returns
`AsyncSpeechStream`; iterate with `async for` (no `await` on the call).

`SpeechStream` / `AsyncSpeechStream`: `headers`, `content_type`,
`sample_rate`, `request_id`, `rate_limit`, `time_to_first_audio` (seconds,
measured at this client), `bytes_received`, `read()` (drain to bytes),
`close()` / `aclose()`; context manager.

### `stream_input(text, *, voice, response_format="pcm", mode="eager", chunk_words=4, peek_words=2, max_chunk_words=20, sample_rate=None, speed=None, language=None, <sampling>, pronunciation_dictionary_id=None, on_event=None)`

Eager input-streaming over the WebSocket. `text` is any iterable of strings
— an LLM token stream — and audio is yielded as the model speaks. Speech
starts after `2 × chunk_words` words; `peek_words` (1–5) is the lookahead held
back; `max_chunk_words` caps a chunk once text has queued. `mode="sentence"`
(the server's own default) waits for sentence boundaries instead. `sample_rate`
on this path is 8000, 16000, 22050, 24000, 44100 or 48000 — the socket does
not serve 32000. Yield `svara.FLUSH` from `text` to have
everything buffered spoken now. `on_event(ChunkEvent)` fires per spoken chunk
with `.text` and `.peek`.

- On `AsyncSvara`: `text` may be sync or async iterable; returns an async
  iterator of `bytes`.
- On `Svara`: `text` is a sync iterable; returns an iterator of `bytes`. The
  text is fed from a helper thread; the socket opens on the first `next()`.

The stream ends with the server's `done`; if the socket closes before that,
`StreamInterruptedError` (with `.frames` delivered and `.close_code`) is
raised rather than silently returning truncated audio.

### `prepare(*, …same as stream_input minus text…) -> PreparedStream` *(async only)*

Open the socket now; feed text later with
`async for audio in prepared.stream(text, on_event=None)`. Voice, format and
sampling were fixed at `prepare()` time. One utterance per prepared socket — the server closes it after `done`. Measured: first audio 427 ms after
`stream_input()` on a fresh socket, 132 ms on a prepared one.

`PreparedStream`: `idle_seconds`, `closed`, `expired` (closed, or idle past
`IDLE_BUDGET_SECONDS` = 240; measured usable at 300 s), `aclose()`; async
context manager that closes an unused socket.

### `create_with_timestamps(*, input, voice, response_format="mp3", sample_rate=None, bitrate_kbps=None, speed=None, language=None, normalize=None, pronunciation_dictionary_id=None, timeout=None) -> TimestampedAudio`

The clip plus per-character timings: `.audio` (bytes in `response_format`)
and `.alignment` (`Alignment`: `characters`, `start_times`, `end_times` in
seconds; `.text`, `.duration`, `.words()` → `(word, start, end)`). Chunk
boundaries are sample-exact and characters are spread uniformly inside each
chunk, so timings are word-accurate and character-approximate — right for
subtitles and karaoke highlighting.

### `stream_with_timestamps(*, …same…) -> Iterator[TimestampedAudio]`

One `TimestampedAudio` per synthesis chunk, offsets relative to the start of
the clip; `response_format` defaults to `"pcm"`. Async variant is an async
iterator.

### `save(path, **create_kwargs) -> str`

`create(...)` then write to `path`.

### Parameters

| Param | Meaning |
|---|---|
| `input` | Text, 1–5,000 characters. Any language or script; code-switching is automatic. No SSML. |
| `voice` | Voice id, e.g. `sv_enhdbrj5` (see `voices.list()`). |
| `response_format` | `mp3` · `opus` · `aac` · `flac` · `wav` · `pcm` · `ulaw` · `alaw`. |
| `sample_rate` | 8000 · 16000 · 22050 · 24000 (default, native) · 32000 · 44100 · 48000. Applies to every format. **`ulaw`/`alaw` are also 24 kHz unless you say 8000** — the SDK warns. |
| `speed` | 0.7–1.5, pitch preserved; 1.0 is the voice's natural pace. |
| `language` | Force a language: ISO-1 (`hi`), ISO-3 (`hin`), name, alias, or BCP-47 (`hi-IN`). Sent as `lang`. Also enables number/date/unit normalisation for that language. |
| `normalize` | `False` to skip text normalisation (default on when `language` is set). |
| `bitrate_kbps` | 8–320 per the OpenAPI document, for `mp3` (default 128), `opus` (64), `aac` (96). |
| `temperature`, `top_p`, `top_k`, `repetition_penalty`, `presence_penalty` | Sampling. Omit to use the server's certified defaults. |
| `pronunciation_dictionary_id` | Respelling rules created in the console. |
| `extra_body` | Extra JSON fields merged into the request: `min_p`, `max_tokens`, `buffer_ms`, `chunk_codes`, and the server's `chunk_size` (characters per synthesis chunk, unrelated to the SDK's byte `chunk_size`). |
| `timeout` | Per-call override, float or `httpx.Timeout`. |
| `model` | Accepted by the API and ignored today (`/v1/models` reports `svara-tts-turbo`). |

Client-side validation raises `InvalidRequestError` (also a `ValueError`) for
an empty or over-long `input`, `speed` outside the range, and unsupported
`sample_rate` or `response_format`, before any network call.

### `svara.output_format(name) -> dict`

Translate an ElevenLabs-style name into request fields, ready to splat:
`output_format("mp3_44100_128")` → `{"response_format": "mp3", "sample_rate": 44100, "bitrate_kbps": 128}`.
Raises `ValueError` for a rate the server cannot render.

## `client.voices`

- `list(*, language=None, gender=None, curated=None, use_cache=False) -> list[Voice]` — the catalogue (320 voices, 282 KB), filtered client-side. The endpoint itself is public, but the client still needs a key to construct. `use_cache=True` reuses the last download.
- `retrieve(voice_id) -> Voice` — falls back to the catalogue for library ids.
- `preview(voice_id) -> SpeechResponse` — a sample clip, `audio/mpeg`.

`Voice`: `voice_id, name, gender, accent_family, description, model_id,
category, curated, is_default, preview_url, quality_warning: list[str],
hours, labels: dict, raw: dict`; properties `.language` (ISO code from
labels) and `.quality_band` (`A` best).

## `client.languages`

- `list() -> list[Language]` — `iso3, iso1, name, region, aliases, raw`. Any of the codes is valid as `language=`.

## `client.usage`

- `get() -> Usage` — `plan_id, characters_used, characters_remaining, requests_per_minute, max_concurrent_streams` plus the raw `plan`, `month`, `balance`, `subscription` dicts. Counts as a request; poll at most once a minute.

## `RateLimitInfo`

From `x-ratelimit-remaining-*` on every successful response: `requests`,
`streams`, `characters`. `None` = header absent, `-1` = unlimited.

## Formats (`svara.FORMAT_INFO`)

| Format | Content-type | Container | Default rate | Notes |
|---|---|---|---|---|
| `mp3` | audio/mpeg | yes | 24 kHz | `bitrate_kbps` default 128 |
| `opus` | audio/ogg | yes | 24 kHz | default 64 |
| `aac` | audio/aac | yes | 24 kHz | default 96 |
| `flac` | audio/flac | yes | 24 kHz | |
| `wav` | audio/wav | yes | 24 kHz | streamed WAV has a placeholder length header; use `create()` for files |
| `pcm` | audio/pcm | no | 24 kHz | 16-bit LE mono |
| `ulaw` | audio/basic | no | **24 kHz** | G.711 µ-law; pass `sample_rate=8000` for telephony |
| `alaw` | audio/basic | no | **24 kHz** | G.711 A-law; same |

## Exceptions

```
SvaraError                      .message .status_code .code .body .request_id .retry_after
├── MissingAPIKeyError          (also ValueError)
├── InvalidRequestError         (also ValueError) rejected before sending
├── APIConnectionError          DNS / TCP / TLS / dropped connection
│   ├── APITimeoutError
│   └── StreamInterruptedError  .frames .close_code — stream ended before `done`
└── APIStatusError              non-2xx
    ├── AuthenticationError     401  invalid_api_key / missing_api_key
    ├── PermissionError_        403
    ├── NotFoundError           404  unknown voice
    ├── BadRequestError         400 / 422  (.code == "validation_error", message names the field)
    ├── RateLimitError          429  rate_limit_exceeded / too_many_concurrent_requests — retried
    │   └── QuotaExceededError  429  insufficient_quota — not retried
    └── InternalServerError     5xx — retried
```

`code` is the server's machine-readable status; the vocabulary matches OpenAI's
and ElevenLabs'. `request_id` is `x-request-id` when the server sends one
(the timestamps routes do), else `None`. `PermissionError_` carries a trailing
underscore so it does not shadow Python's builtin `PermissionError`.

## Framework integrations

### `svara.livekit.TTS(*, voice="sv_enhdbrj5", language=None, speed=None, mode="eager", chunk_words=4, peek_words=2, api_key=…, base_url=…, sample_rate=24000, pronunciation_dictionary_id=None, prewarm=True)`

LiveKit Agents plugin. `mode="eager"` forwards the LLM token stream over the
input-streaming WebSocket and keeps the next socket prewarmed between turns;
`mode="http"` buffers sentences over HTTP. `update_options(voice=, language=,
speed=, mode=, pronunciation_dictionary_id=)` changes them live. Reports
`label="svara.TTS"`, `provider="svara"`, `model="svara-tts-turbo"`.

### `svara.pipecat.SvaraTTSService(*, voice=…, api_key=…, base_url=…, model=…, language=…, speed=…, pronunciation_dictionary_id=…, sample_rate=None, client=None, settings=None, **kwargs)`

Pipecat `TTSService` (pipecat-ai ≥ 0.0.105). One HTTP stream per sentence,
always 16-bit PCM. `sample_rate=None` follows the transport's
`audio_out_sample_rate` (8000 on a phone transport) and the server renders at
that rate; Pipecat's telephony serializers do the G.711 companding, so never
ask for `ulaw` here. Settings change
live via `TTSUpdateSettingsFrame` (`SvaraTTSSettings` adds `speed` and
`pronunciation_dictionary_id`). Pass `client=AsyncSvara(...)` to share one
connection pool across services.

## CLI

```
svara [--api-key KEY] [--base-url URL] [--version] <command>
svara say TEXT --voice ID [--format mp3] [--sample-rate 8000] [--speed 1.1] [--language hi] [--out FILE]
svara voices [--language hi] [--gender female] [--json]
svara languages [--json]
svara usage [--json]
```

## HTTP endpoints (under the hood)

- `POST /v1/audio/speech` — synth (`stream: true` for chunked)
- `WS /v1/audio/speech/stream-input` — eager input-streaming
- `POST /v1/text-to-speech/{voice}/with-timestamps` and `…/stream/with-timestamps` — timestamps
- `GET /v1/voices`, `/v1/voices/{id}`, `/v1/voices/{id}/preview`
- `GET /v1/languages`, `GET /v1/usage`, `GET /v1/models`

Auth header: `xi-api-key: <key>` (`Authorization: Bearer <key>` also works).
Live OpenAPI document: `https://api.kenpathlabs.com/openapi.json`.
