---
name: svara-migrate
description: Switch existing text-to-speech code from OpenAI TTS (audio.speech) or ElevenLabs to Svara TTS Turbo, usually by changing only the base URL and API key, or port it to the native svara-voice SDK for lower latency. Use when the user wants an OpenAI TTS or ElevenLabs alternative, cheaper or more multilingual TTS, better Hindi or Indian-language voices, or asks to replace, compare or A/B test TTS providers in Python or JavaScript.
license: Apache-2.0
compatibility: Needs SVARA_API_KEY and network access to api.kenpathlabs.com. Works with the openai (Python/JS), elevenlabs (Python) and @elevenlabs/elevenlabs-js SDKs, or with svara-voice.
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com/sdks
---

# Migrating to Svara TTS Turbo

Svara's HTTP API accepts OpenAI and ElevenLabs speech requests and uses their
error codes (`invalid_api_key`, `rate_limit_exceeded`, `insufficient_quota`), so
their SDKs' retry logic keeps working. Migration takes three steps:
1. Point the base URL at Svara.
2. Swap the key for `SVARA_API_KEY`.
3. Replace the voice ids with Svara ids.

OpenAI voice names such as `alloy` and ElevenLabs voice ids won't work. Pick
`sv_…` ids with the **svara-voices** skill. Match on gender, accent and style.

## OpenAI SDK → Svara (keep the SDK)

**The base URL includes `/v1`.**

```python
import os
from openai import OpenAI
client = OpenAI(base_url="https://api.kenpathlabs.com/v1", api_key=os.environ["SVARA_API_KEY"])

audio = client.audio.speech.create(model="svara-tts-turbo", voice="sv_enhdbrj5",
                                   input="Namaste!", response_format="mp3",
                                   extra_body={"lang": "hi"})          # Svara-only fields go in extra_body
audio.write_to_file("out.mp3")

with client.audio.speech.with_streaming_response.create(
        model="svara-tts-turbo", voice="sv_enhdbrj5", input=text, response_format="pcm",
        extra_body={"stream": True}) as r:                             # needed, or nothing arrives until the whole clip renders
    for chunk in r.iter_bytes():
        player.write(chunk)
```

Without `extra_body={"stream": True}`, first byte arrives at 700 ms instead of
192 ms. `instructions` and `stream_format="sse"` have no Svara equivalent.
Remove them.

JavaScript:

```javascript
const client = new OpenAI({ baseURL: "https://api.kenpathlabs.com/v1", apiKey: process.env.SVARA_API_KEY });
const res = await client.audio.speech.create({
  model: "svara-tts-turbo", voice: "sv_enhdbrj5", input: "Namaste!", response_format: "mp3",
  // @ts-expect-error svara extension
  lang: "hi",
});
```

## ElevenLabs SDK → Svara (keep the SDK)

**The base URL has no `/v1`**, because the SDK appends it.

```python
import os
from elevenlabs.client import ElevenLabs
client = ElevenLabs(base_url="https://api.kenpathlabs.com", api_key=os.environ["SVARA_API_KEY"])

audio = b"".join(client.text_to_speech.convert(voice_id="sv_enhdbrj5", text="Namaste!",
                                               output_format="mp3_44100_128"))
for chunk in client.text_to_speech.stream(voice_id="sv_enhdbrj5", text=text, output_format="pcm_24000"):
    player.write(chunk)
```

```javascript
const client = new ElevenLabsClient({ baseUrl: "https://api.kenpathlabs.com", apiKey: process.env.SVARA_API_KEY });
```

Verified working:
- `convert` and `stream`, with every `output_format`, including `ulaw_8000` and `alaw_8000`
- `convert_with_timestamps` and `stream_with_timestamps`
- `convert_realtime`
- `voices.get_all`, `voices.get` and `voices.search`
- `models.list`
- `user.subscription.get`
- `pronunciation_dictionaries.list`

Accepted but ignored:
- `model_id`
- `voice_settings.stability`, `similarity_boost` and `style` (only `voice_settings.speed` is honoured)
- `seed`, `previous_text` and `next_text`

## Port to the native SDK (recommended for voice agents)

Only the native `svara-voice` SDK exposes Svara's own input-streaming socket.

| | First audio |
|---|---|
| ElevenLabs `convert_realtime` against Svara | 1047 ms |
| `svara` `stream_input()`, fresh socket | 427 ms |
| `svara` `prepared.stream()`, socket opened earlier | **132 ms** |

HTTP streaming takes about the same time from all three clients (182–203 ms).
If you have no live agent, keeping your current SDK is fine.

`pip install svara-voice`. Code written for the OpenAI SDK then runs on a `Svara`
client with only the constructor changed:

```python
from svara import Svara
client = Svara()                                    # instead of OpenAI(...)
client.audio.speech.create(model="svara-tts-turbo", voice="sv_enhdbrj5", input="Hi", response_format="mp3").write_to_file("hi.mp3")
```

| ElevenLabs | svara-voice |
|---|---|
| `text_to_speech.convert(voice_id, text=…, output_format="mp3_44100_128")` | `speech.create(voice=…, input=…, **svara.output_format("mp3_44100_128"))` |
| `text_to_speech.stream(...)` | `speech.stream(voice=…, input=…)` |
| `text_to_speech.convert_realtime(voice_id, text=iterator)` | `speech.stream_input(iterator, voice=…)` |
| `voice_settings.speed` | `speed=` (0.7–1.5) |
| `language_code="hi"` | `language="hi"` |
| `pronunciation_dictionary_locators=[…]` | `pronunciation_dictionary_id="…"` |
| `voices.get_all().voices` / `voices.search(search=…)` | `voices.list()` / `voices.search("…")` |
| `ApiError` | `svara.SvaraError` (`.status_code`, `.code`, `.body`) |

For LiveKit, replace the ElevenLabs or OpenAI TTS plugin with
`svara.livekit.TTS(voice=...)`. For Pipecat, use `svara.pipecat.SvaraTTSService`.
See **svara-voice-agent**.

## Comparing providers fairly

- **Compare time to first audio, not total bytes or duration.** Generation is
  stochastic, so audio length varies from run to run.
  `stream.time_to_first_audio` measures it on a Svara stream.
- **Interleave the providers** (A, B, A, B…) rather than running all of A and then all of B.
- **Reuse one client for each provider.** A new connection adds about 140 ms.
- **Listen to non-English and mixed-language samples.** That is where the differences show.

Pricing is per character and the same on every plan. See
https://kenpathlabs.com/pricing for current rates and the free tier.
