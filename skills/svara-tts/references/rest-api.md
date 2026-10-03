# Svara REST and WebSocket reference

Base URL `https://api.kenpathlabs.com`. Auth on every call, the WebSocket
upgrade included: `Authorization: Bearer <key>` or `xi-api-key: <key>`. Every
response carries `x-request-id`; quote it in a support ticket. The live OpenAPI
document is at https://api.kenpathlabs.com/openapi.json.

## POST /v1/audio/speech

The body follows the OpenAI speech request.

| Field | Default | Notes |
|---|---|---|
| `input` | required | 1–5,000 characters. Raw text in any script, code-switching allowed, no SSML. |
| `voice` | required | A voice id such as `sv_enhdbrj5`. |
| `model` | `svara-tts-turbo` | Optional. Any value is accepted and ignored. |
| `response_format` | `wav` | `mp3` `opus` `aac` `flac` `wav` `pcm` `ulaw` `alaw` |
| `sample_rate` | 24000 | 8000 16000 22050 24000 32000 44100 48000. `opus` accepts only 8000/16000/24000/48000. The default applies to `ulaw`/`alaw` too. |
| `speed` | 1.0 | 0.7–1.5, pitch preserved. |
| `stream` | false | `true` returns a chunked response, so playback starts at the first chunk. |
| `lang` | auto | `hi`, `hin`, `hindi`, `hi-IN`, `ja`, `zh-CN`, … Also turns on number, date and unit normalisation. |
| `normalize` | true | Applies only when `lang` is set. |
| `bitrate_kbps` | mp3 128 · opus 64 · aac 96 | 8–320. |
| `pronunciation_dictionary_id` | — | A UUID. An unknown id is not an error: the response header `x-svara-dictionary: miss` reports it. |

The response is raw audio bytes. `pcm` is headerless 16-bit little-endian mono
at `sample_rate`. When you stream PCM, keep an odd trailing byte for the next
chunk so samples stay 2-byte aligned.

```bash
# Stream PCM and play it with sox
curl -N https://api.kenpathlabs.com/v1/audio/speech \
  -H "Authorization: Bearer $SVARA_API_KEY" -H "Content-Type: application/json" \
  -d '{"voice":"sv_enhdbrj5","stream":true,"response_format":"pcm","input":"This starts playing before it finishes."}' \
  | play -t raw -r 24000 -e signed -b 16 -c 1 -
```

## ElevenLabs-shaped routes

- `POST /v1/text-to-speech/{voice_id}` and `/stream`. The body is `{"text", "language_code", "voice_settings": {"speed"}}`, and the query is `?output_format=mp3_44100_128`, `pcm_24000`, `ulaw_8000` and so on.
- `POST /v1/text-to-speech/{voice_id}/with-timestamps` and `/stream/with-timestamps` return audio (base64) plus a character `alignment`. The streaming variant returns one JSON object per line. The timings are accurate to the word and approximate to the character.

## WebSocket: /v1/audio/speech/stream-input (native input streaming)

`wss://api.kenpathlabs.com/v1/audio/speech/stream-input?voice=sv_enhdbrj5&mode=eager`

Query parameters:
- `voice`
- `lang`
- `mode`: `eager` (the right choice for agents) or `sentence` (the server default)
- `speed`
- `sample_rate`
- `pronunciation_dictionary_id`
- `chunk_words` (default 4, which is also the minimum)
- `peek_words` (1–5, default 2)
- `max_chunk_words` (default 20)

Client → server:
- `{"text": "fragment "}` for each LLM delta.
- `{"flush": true}` to speak whatever is buffered now.
- `{"text": ""}` to end the input.

Server → client:
- Binary frames: PCM16 LE mono at `sample_rate`.
- `{"type":"chunk","text":…,"peek":…}` before each chunk's audio.
- `{"type":"flushed"}`.
- `{"type":"done"}`, then the server closes with code 1000. If the socket closes without `done`, the audio is truncated.
- `{"type":"error","message":…}`, then close 1008. This covers a bad voice, sample rate or speed.
- Close 1013: no engine was ready. Retry.
- Close 1011: internal error.

Each socket carries one utterance. In eager mode, speech starts after 8 words
with the default settings. Chinese and Japanese count one word per two
characters. Open the socket before the text exists (the SDK's `prepare()`)
so connecting costs nothing when the reply starts.

## Discovery (public, no key)

- `GET /v1/voices` returns `{"voices": [...]}`, all 320 voices (282 KB). This route has no server-side filters, so filter on the client.
- `GET /v1/voices/{id}` returns one voice. `GET /v1/voices/{id}/preview` returns an `audio/mpeg` sample.
- `GET /v1/languages` returns `{"languages": [{iso3, iso1, name, region, aliases}]}`.
- `GET /v1/models` returns `svara-tts-turbo`.
- `GET /v2/voices?search=…&page_size=…` is a server-side search, and it needs a key.

## Account (key required)

- `GET /v1/usage` returns the plan, `month.characters_used` and `balance.characters_remaining`. Poll it at most once a minute.
- `GET /v1/pronunciation-dictionaries` and `/{id}` read dictionaries. `POST /v1/pronunciation-dictionaries/add-from-rules` takes `{"name": "...", "rules": [{"text":"SQL","pronunciation":"sequel"}]}`.

## Errors

Every error has the shape `{"detail": {"status": "<code>", "message": "..."}}`.

| HTTP | `status` | Meaning |
|---|---|---|
| 401 | `missing_api_key`, `invalid_api_key` | No key, or a revoked key |
| 404 | — | Unknown voice |
| 422 | validation error | The message names the field |
| 429 | `rate_limit_exceeded` | Over the per-minute limit; honour `Retry-After` |
| 429 | `too_many_concurrent_requests` | Every stream slot is busy; retry in about 1 s |
| 429 | `insufficient_quota` | Monthly characters are spent; do not retry |

Successful responses carry `x-ratelimit-remaining-requests`,
`x-ratelimit-remaining-streams` and `x-ratelimit-remaining-characters`. Limits
apply per organisation, not per key.
