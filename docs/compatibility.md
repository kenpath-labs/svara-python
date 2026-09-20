# Compatibility with the OpenAI and ElevenLabs SDKs

Svara's HTTP API is request-compatible with both vendors' speech APIs, and
the error vocabulary (`invalid_api_key`, `rate_limit_exceeded`,
`too_many_concurrent_requests`, `insufficient_quota`) matches theirs, so their
SDKs' retry logic works unchanged. Everything below was run against production
between 2026-09-17 and 2026-09-21 with `openai` 2.54.0 and 3.16.2, and `elevenlabs` 2.68.0.

## OpenAI SDK

Include `/v1` in the base URL.

```python
from openai import OpenAI

client = OpenAI(base_url="https://api.kenpathlabs.com/v1", api_key=SVARA_API_KEY)

audio = client.audio.speech.create(
    model="svara-tts-turbo", voice="sv_enhdbrj5", input="Namaste!", response_format="mp3",
    speed=1.1,
    extra_body={"lang": "hi"},                     # Svara-only fields ride in extra_body
)
audio.write_to_file("out.mp3")

# Streaming: OpenAI's client has no `stream` field, so pass Svara's in extra_body.
with client.audio.speech.with_streaming_response.create(
    model="svara-tts-turbo", voice="sv_enhdbrj5", input="…", response_format="pcm",
    extra_body={"stream": True},
) as r:
    for chunk in r.iter_bytes():
        player.write(chunk)
```

Without `extra_body={"stream": True}` the server renders the whole clip before
answering: measured 700 ms to the first byte instead of 192 ms.

Verified: `audio.speech.create` (every `response_format`), `with_streaming_response`,
`models.list()` (returns `svara-tts-turbo`), `speed`, `extra_body` fields.
Not applicable: `instructions`, `stream_format="sse"`.

## ElevenLabs SDK

Do **not** include `/v1`; the ElevenLabs client appends it.

```python
from elevenlabs.client import ElevenLabs

client = ElevenLabs(base_url="https://api.kenpathlabs.com", api_key=SVARA_API_KEY)

audio = b"".join(client.text_to_speech.convert(
    voice_id="sv_enhdbrj5", text="Namaste!",
    model_id="eleven_multilingual_v2",             # accepted, ignored
    output_format="mp3_44100_128",
))

for chunk in client.text_to_speech.stream(voice_id="sv_enhdbrj5", text="…", output_format="pcm_24000"):
    player.write(chunk)
```

Verified against production:

| ElevenLabs call | Works | Notes |
|---|---|---|
| `text_to_speech.convert` / `.stream` | yes | all `output_format` names incl. `ulaw_8000`, `alaw_8000` |
| `text_to_speech.convert_with_timestamps` / `.stream_with_timestamps` | yes | character alignment, chunk-relative and approximate |
| `text_to_speech.convert_realtime` (WebSocket) | yes | first audio ≈ 1.0 s — see below |
| `voices.get_all` / `.get` | yes | 320 voices; `labels` carry language, accent, quality band |
| `voices.search` (`GET /v2/voices`) | yes | same 320-voice roster as `get_all`, with search and paging |
| `models.list` | yes | one model, `svara-tts-turbo` |
| `user.subscription.get` | yes | character counts in EL's shape |
| `pronunciation_dictionaries.list` | yes | manage rules in the Svara console |
| `voice_settings` (stability, similarity, style) | accepted, ignored | `voice_settings.speed` maps onto `speed` |
| `seed`, `previous_text`/`next_text`, request stitching | accepted, ignored | no equivalent in this model |

### Why the native SDK for realtime

The ElevenLabs realtime protocol buffers text to its `chunk_length_schedule`
(120 characters before the first generation). Svara's native input-streaming
socket generates after eight words. Measured time to first audio, same
sentence, same laptop:

| | First audio |
|---|---|
| `elevenlabs` `convert_realtime` (EL WebSocket protocol) | 1047 ms |
| `svara` `stream_input` on a fresh socket | 427 ms |
| `svara` `prepared.stream()` on a socket opened earlier | **132 ms** |

HTTP streaming is equivalent across all three clients (182–203 ms). The
WebSocket is the reason to use this package for a voice agent.

## Migrating to `svara-voice`

| From ElevenLabs | To Svara |
|---|---|
| `ElevenLabs(api_key=…)` | `Svara(api_key=…)` |
| `text_to_speech.convert(voice_id, text=…, output_format="mp3_44100_128")` | `speech.create(voice=…, input=…, **output_format("mp3_44100_128"))` |
| `text_to_speech.stream(voice_id, text=…, output_format="pcm_24000")` | `speech.stream(voice=…, input=…, **output_format("pcm_24000"))` |
| `text_to_speech.convert_realtime(voice_id, text=iterator)` | `speech.stream_input(iterator, voice=…)` |
| `voice_settings.speed` | `speed=` (0.7–1.5) |
| `language_code="hi"` | `language="hi"` |
| `pronunciation_dictionary_locators=[…]` | `pronunciation_dictionary_id="…"` |
| `voices.get_all().voices` | `voices.list()` |
| `voices.search(search=…)` | `voices.search("…")` (client-side over the cached catalogue) |
| `models.list()` | `models.list()` |
| `play(audio)` / `stream(audio_stream)` | `svara.play(audio_or_stream)` |
| `pronunciation_dictionaries.create_from_rules(...)` | `pronunciation_dictionaries.create_from_rules(name=, rules=[PronunciationRule(...)])` |
| `ApiError` (`.status_code`, `.body`) | `SvaraError` (`.status_code`, `.code`, `.body`) |

| From OpenAI | To Svara |
|---|---|
| `OpenAI(base_url=…, api_key=…)` | `Svara(api_key=…)` — after which the rows below run as written: |
| `client.audio.speech.create(model, voice, input, response_format, speed)` | same (`client.audio.speech` is `client.speech`); `model` optional |
| `audio.write_to_file(p)`, `.stream_to_file(p)`, `.content`, `.read()`, `.iter_bytes()` | same, on the returned `SpeechResponse` |
| `with_streaming_response.create(...)` + `extra_body={"stream": True}` | same, and it streams without the `extra_body`; or `speech.stream(...)` |
| `extra_headers=`, `extra_query=`, `client.with_options(timeout=, max_retries=)` | same names, same meaning |
| `PermissionDeniedError`, `UnprocessableEntityError`, `ConflictError` | same names |
| `extra_body={"lang": …}` | `language=` |
| `extra_body={…}` | `extra_body={…}` (same escape hatch) |
| `APIStatusError` (`.status_code`, `.request_id`, `.body`) | `SvaraError` (`.status_code`, `.code`, `.body`) |

The OpenAI SDK's `max_retries`, `timeout` and `http_client` constructor
arguments exist here with the same meaning. ElevenLabs' `timeout` maps
directly; its `httpx_client` is `http_client` here.
