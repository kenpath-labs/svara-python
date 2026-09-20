# Changelog

All notable changes to `svara-voice`. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/) — until 1.0, minor versions may change behaviour
and this file says exactly where.

## [0.2.0] — 2026-09-21

Production-readiness pass. The latency and timeout defaults below were set from
measurements on the live API; the numbers are in `MEASUREMENTS.md`.

### Changed
- **The default `model` is `svara-tts-turbo`** (was `svara-1`). It is the one
  id `GET /v1/models` reports; the server ignores the field either way.
- 422 raises `UnprocessableEntityError` (a `BadRequestError`, so existing
  handlers still catch it); 409 raises `ConflictError`.
- **Requires `websockets >= 14`** (was 12). The modern client is where
  `additional_headers` and handshake-refusal responses live; Pipecat itself
  needs 13.1+. On 12/13 the sync path could not connect over `wss://`.
- Abandoning a WebSocket stream now closes the socket within 1 s
  (`close_timeout`), not the library's 10 s — during which the server still
  counted the stream against your concurrency limit.
- **Connections stay open between turns.** The owned `httpx` transport now
  keeps idle connections for 120 s (httpx's default is 5 s). A voice agent's
  turns are further apart than 5 s, so every synthesis was re-doing TCP + TLS:
  +140 ms to first audio, measured. Pass your own `http_client` to override.
- **Read timeout 30 s → 120 s.** A non-streaming `create()` of the 5,000-character
  maximum renders in ~50 s and used to time out.
- `speech.create()` returns `SpeechResponse` — still `bytes`, plus `headers`,
  `content_type`, `sample_rate`, `request_id`, `rate_limit` and `.save(path)`.
- `speech.stream()` returns `SpeechStream` / `AsyncSpeechStream` — still an
  iterator of `bytes`, plus the response headers, `rate_limit`,
  `time_to_first_audio`, `bytes_received`, `.read()` and context-manager close.
- Error messages are readable: `Svara API error 401 (invalid_api_key): API key
  invalid or revoked.` instead of a JSON dump. All three server body shapes
  (gateway status/message, plain string, pydantic list) are parsed; the raw body
  stays on `.body`, the machine code on the new `.code`.
- `PreparedStream.IDLE_BUDGET_SECONDS` 20 → 240. A prepared socket measured
  usable after 300 s idle; at 20 s the LiveKit plugin discarded most of the
  sockets it had prewarmed.
- The LiveKit plugin reports `label="svara.TTS"`, `provider="svara"`,
  `model="svara-tts-turbo"` instead of `unknown`.
- The Pipecat service targets pipecat-ai ≥ 0.0.105's `run_tts(text, context_id)`
  contract and `TTSUpdateSettingsFrame`; the extra now pins that floor. It is
  PCM-only: Pipecat's frames assume 16-bit samples and its telephony
  serializers do the G.711 companding, so the old `response_format="ulaw"`
  option produced double-companded audio and is gone.

- A `{"type": "error"}` event on the input-streaming socket (unknown voice)
  now raises `NotFoundError` with the server's message instead of a
  `StreamInterruptedError` claiming truncation; interrupted-stream messages
  explain the close code. `normalize` is accepted on the WebSocket paths.
- WebSocket handshake refusals (401, 429 …) raise the matching `SvaraError`
  subclass instead of a `websockets` exception; connects honour the connect
  timeout.

### Added
- OpenAI-SDK spellings, so code ports by changing the client only:
  `client.audio.speech`, `speech.with_streaming_response.create()`,
  `SpeechResponse.write_to_file/.stream_to_file/.content/.read()/.iter_bytes()`,
  per-call `extra_headers` / `extra_query`, `client.with_options()` (shares the
  connection pool), `default_headers`, `PermissionDeniedError`.
- `client.models.list()`; `client.voices.search(query)`, client-side over the
  cached catalogue.
- `svara.play(audio_or_stream)` via `ffplay`, for quickstarts.
- `speech.create_with_timestamps()` / `speech.stream_with_timestamps()` —
  audio plus per-character `Alignment` (word-accurate), on both clients.
- `Svara().speech.stream_input(...)` — a blocking twin of the async eager
  WebSocket path, on `websockets.sync`, for code without an event loop.
- `svara.FLUSH` — yield it from a `stream_input` text source to have everything
  buffered spoken now (paragraph and turn boundaries).
- `client.warm_up()` / `await client.warm_up()` — open the HTTP connection at
  start-up instead of on the first utterance (~100 ms).
- `client.pronunciation_dictionaries.list()` / `.retrieve()` / `.create_from_rules()`
  with `PronunciationRule`; opus sample rates validated locally (8/16/24/48 kHz).
- `client.languages.list()`, `client.usage.get()`, `client.voices.preview(id)`;
  `voices.list(language=, gender=, curated=)` filters; `Voice.quality_warning`,
  `Voice.hours`, `Voice.quality_band`.
- Request fields `bitrate_kbps`, `normalize`, and a per-call `timeout`.
- `svara.output_format("mp3_44100_128")` translates ElevenLabs-style format
  names into `response_format` / `sample_rate` / `bitrate_kbps`.
- `InvalidRequestError` (also a `ValueError`) raised before the round trip for
  empty or over-long input, `speed` outside 0.7–1.5, an unsupported
  `sample_rate` or `response_format`.
- `QuotaExceededError` (a `RateLimitError`) for 429 `insufficient_quota`; it is
  never retried. `InternalServerError` for 5xx.
- Every request carries a client-chosen `x-request-id`; it is reported on
  `SpeechResponse.request_id`, `SpeechStream.request_id` and
  `SvaraError.request_id` (the server's own id wins when it sends one).
- A warning when `pronunciation_dictionary_id` is not found server-side
  (`x-svara-dictionary: miss`), instead of the global rules applying silently.
- CLI: `svara doctor` (DNS, TLS, key, HTTP and WebSocket synthesis with
  timings), `svara usage`, `svara languages`, `say --sample-rate`, `voices --gender`.
- `AGENTS.md` and `docs/llms.txt` for coding assistants; `docs/compatibility.md`;
  `docs/debugging.md`.

### Fixed
- `svara.pipecat` failed at import on pipecat-ai 0.0.105, the oldest release the
  extra admits. Both plugins are now tested end to end on their oldest and
  newest supported versions (livekit-agents 1.6.0 and 1.8.2, pipecat-ai 0.0.105
  and 1.10.0).
- The LiveKit plugin no longer retries client-side validation errors or a
  spent quota through LiveKit's connection retries; a prewarmed socket opened
  for one voice/speed/language is discarded when `update_options()` changes
  them, instead of speaking the next turn with the old settings.
- `voices.retrieve()` falls back to the catalogue only on 404; a 401/429/5xx
  from the by-id endpoint is raised as itself.
- A WebSocket connect timeout is an `APITimeoutError` on every Python
  version (it was an `APIConnectionError` on 3.11+).
- The sdist lists what ships (a local build once included a 200 MB virtualenv).

## [0.1.0] — 2026-09-10

First PyPI release. Sync and async clients, HTTP streaming, the eager
input-streaming WebSocket with `prepare()`, retries with `Retry-After`,
pronunciation dictionaries, LiveKit and Pipecat integrations, the `svara` CLI.
