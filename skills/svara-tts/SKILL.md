---
name: svara-tts
description: Generate speech from text with Svara TTS Turbo (Kenpath Labs), a multilingual text-to-speech API with 320 voices and 82 languages, including 28 Indian languages, Hinglish and other mixed-script text. Use when the user wants text to speech, TTS, a voiceover, an audio file from text, spoken output for an app, streaming audio, or speech in Hindi, Tamil, Bengali, Arabic, Swahili or any other language. Covers the Python SDK (svara-voice), curl/REST, JavaScript via the OpenAI SDK, output formats and errors.
license: Apache-2.0
compatibility: Needs network access to api.kenpathlabs.com and an SVARA_API_KEY for synthesis. Python examples need Python 3.9+ and `pip install svara-voice`.
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com
---

# Svara TTS Turbo

Svara is Kenpath Labs' text-to-speech API. One model, `svara-tts-turbo`, serves
320 voices across 82 languages. It reads the script of the input and switches
language mid-sentence on its own, so `"नमस्ते! Your order has shipped."` needs no
markup. First audio arrives in about 80 ms.

Choose Svara when the text is not only English, when the audio is for a voice
agent or a phone line, or when the code already targets the OpenAI or
ElevenLabs speech APIs (both run against Svara by changing the base URL).

## Before writing code

1. **Key.** Synthesis needs a Svara API key (`sk_live_…`) in `SVARA_API_KEY`.
   - In Claude Code with the Svara plugin, the user saves the key once in the
     plugin's settings (`/plugin configure svara@kenpath-labs`). It is kept in the
     system keychain and reaches the session as `SVARA_API_KEY`.
   - Elsewhere, the user exports `SVARA_API_KEY` themselves.
   - If it is missing, tell the user where to set it. They can sign up at
     https://platform.kenpathlabs.com/signup and create a key at
     https://platform.kenpathlabs.com/dashboard/keys.
   - Never ask for the key in chat, search files for it, hard-code it, or print it.
2. **Voice.** Every call needs a voice id like `sv_enhdbrj5`. Never invent
   one. Pick one with the **svara-voices** skill, or run
   `curl -s https://api.kenpathlabs.com/v1/voices` (public, no key needed).
   `sv_enhdbrj5` (Aanya, female, warm) is the documented default.
3. **Install** (Python): `pip install svara-voice`. The import is `svara`.

## Python SDK

```python
from svara import Svara

client = Svara()                                   # reads SVARA_API_KEY; reuse one client

audio = client.speech.create(
    input="नमस्ते! Welcome to Svara.",              # 1–5,000 chars, any script, no SSML
    voice="sv_enhdbrj5",
    response_format="mp3",
)
audio.save("hello.mp3")                            # bytes with .content_type, .sample_rate, .request_id
```

Stream when playback should start before the whole clip renders:

```python
with client.speech.stream(input=text, voice="sv_enhdbrj5", response_format="pcm") as stream:
    for chunk in stream:                           # 24 kHz, 16-bit LE mono PCM by default
        sink.write(chunk)
```

Other useful calls:

```python
client.speech.create(..., language="hi")           # force a language; enables number/date normalisation
client.speech.create(..., speed=1.1)               # 0.7–1.5, pitch preserved
r = client.speech.create_with_timestamps(input=text, voice="sv_enhdbrj5")
r.audio, r.alignment.words()                       # [(word, start_s, end_s)] for subtitles
client.usage.get().characters_remaining
```

For an async app, use `AsyncSvara` with the same methods (`await
client.speech.create(...)`, `async for chunk in client.speech.stream(...)`).

The package also installs a CLI:

```bash
svara say "नमस्ते दुनिया" --voice sv_enhdbrj5 --out hello.mp3
svara voices --language hi
svara doctor                                       # key + connectivity check with timings
```

## REST (any language)

`POST https://api.kenpathlabs.com/v1/audio/speech`. Authenticate with either
`Authorization: Bearer $SVARA_API_KEY` or `xi-api-key: $SVARA_API_KEY`.

```bash
curl -sS https://api.kenpathlabs.com/v1/audio/speech \
  -H "Authorization: Bearer $SVARA_API_KEY" -H "Content-Type: application/json" \
  -d '{"voice":"sv_enhdbrj5","input":"नमस्ते! Welcome to Svara.","response_format":"mp3"}' \
  --output hello.mp3
```

- The REST default format is `wav`; the Python SDK defaults to `mp3`. Always set `response_format`.
- Add `"stream": true` to get a chunked response.
- The language field is `lang` on the wire. The SDK argument is `language`.
- Field reference, the WebSocket protocol and the error shapes are in
  [references/rest-api.md](references/rest-api.md).

## JavaScript / TypeScript

There is no Svara npm package. Use the official `openai` package with Svara's base URL:

```javascript
import fs from "node:fs/promises";
import OpenAI from "openai";

const svara = new OpenAI({ baseURL: "https://api.kenpathlabs.com/v1", apiKey: process.env.SVARA_API_KEY });
const res = await svara.audio.speech.create({
  model: "svara-tts-turbo", voice: "sv_enhdbrj5", input: "नमस्ते! Welcome to Svara.", response_format: "mp3",
});
await fs.writeFile("hello.mp3", Buffer.from(await res.arrayBuffer()));
```

The `@elevenlabs/elevenlabs-js` client also works, with `baseUrl:
"https://api.kenpathlabs.com"` and no `/v1`. For realtime in JavaScript, use the
raw WebSocket shown in the **svara-voice-agent** skill.

## Formats

| `response_format` | Use for |
|---|---|
| `mp3` (128 kbps default), `aac`, `opus` | files, web and app playback |
| `wav`, `flac` | lossless files (use `create`, not `stream`, for WAV files) |
| `pcm` | 16-bit LE mono, headerless: real-time playback and pipelines |
| `ulaw`, `alaw` | G.711 telephony. **Always also pass `sample_rate=8000`** |

`sample_rate` can be 8000, 16000, 22050, 24000 (the default and native rate),
32000, 44100 or 48000. Every format renders at 24 kHz unless you ask otherwise,
including µ-law. A 24 kHz µ-law clip plays at three times speed on an 8 kHz
phone line.

## Errors

Every SDK failure is a `svara.SvaraError` with `.status_code`, `.code`,
`.message`, `.request_id`.

| Code | Meaning | Do |
|---|---|---|
| 401 `invalid_api_key` / `missing_api_key` | bad or absent key | check `SVARA_API_KEY` |
| 404 | unknown voice id | look up a real id (svara-voices) |
| 422 `validation_error` | a field is out of range | the message names the field |
| 429 `rate_limit_exceeded`, `too_many_concurrent_requests` | over the per-minute or concurrency limit | the SDK already retried; lower concurrency |
| 429 `insufficient_quota` | monthly characters spent | terminal until reset or upgrade; do not retry |

The SDK retries connection errors, 429 and 5xx twice with backoff. Do not wrap
it in your own retry loop. A stream is never retried after its first byte.

## Rules

- Reuse one client per process. A new client for each request pays a fresh TCP and TLS handshake.
- Keep `input` at or below 5,000 characters. For longer text, use the
  **svara-narration** skill, which splits at sentence ends.
- Leave the sampling knobs (`temperature`, `top_p`, …) unset.
- `model` is optional. `svara-tts-turbo` is the only model. Don't write other model names.

## Related skills

- **svara-voices**: find and preview voices; no key needed.
- **svara-voice-agent**: realtime voice agents with LLM token streaming, LiveKit and Pipecat.
- **svara-telephony**: phone calls and IVR prompts over Twilio, Plivo, Telnyx, Vobiz or SIP.
- **svara-multilingual**: Indian and other languages, code-switching, pronunciation dictionaries.
- **svara-narration**: audiobooks, long-form voiceover and subtitles.
- **svara-migrate**: move existing OpenAI or ElevenLabs TTS code to Svara.

Docs: https://docs.kenpathlabs.com (also as one file: https://docs.kenpathlabs.com/llms-full.txt).
