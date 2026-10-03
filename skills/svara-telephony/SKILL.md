---
name: svara-telephony
description: Generate phone-quality speech with Svara TTS Turbo for calls, IVR menus and voice bots, as native 8 kHz G.711 mu-law/A-law with no transcoding. Use when the user is building a phone agent, IVR prompts, call-center or outbound calling bot, or is integrating TTS with Twilio Media Streams, Plivo, Vobiz, Telnyx, Exotel, Asterisk/FreeSWITCH or LiveKit SIP, including Hindi and other Indian-language phone lines.
license: Apache-2.0
compatibility: Needs SVARA_API_KEY and network access to api.kenpathlabs.com. Python 3.9+ with `pip install svara-voice`.
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com/text-to-speech
---

# Telephony with Svara TTS Turbo

Phone networks carry 8 kHz G.711 (µ-law in North America and Japan, A-law in
most other places, including India's PSTN). Svara renders that format directly.
The bytes it returns are the bytes the media stream wants.

## The one rule

**Always send `sample_rate=8000` together with `response_format="ulaw"` or `"alaw"`.**
The API renders every format at 24 kHz unless told otherwise, G.711 included.
24 kHz µ-law played on an 8 kHz leg sounds three times too fast, which is easy
to mistake for a broken voice. The Python SDK warns when the rate is missing.

```python
from svara import Svara

client = Svara()                                    # SVARA_API_KEY from the environment
PHONE = dict(response_format="ulaw", sample_rate=8000)

prompt = client.speech.create(input="Thank you for calling. आपकी कॉल के लिए धन्यवाद।",
                              voice="sv_r22w7pwe", **PHONE)
open("welcome.ulaw", "wb").write(prompt)            # 8000 bytes = 1 second
```

curl equivalent: `{"voice":"sv_r22w7pwe","input":"…","response_format":"ulaw","sample_rate":8000}`
POSTed to `/v1/audio/speech` (see **svara-tts**).

## Which path?

| You have | Use |
|---|---|
| Fixed IVR prompts and menus | Pre-render them once with `create()` (below) |
| A provider that streams call audio to your WebSocket (Twilio Media Streams, Plivo/Vobiz Audio Streams, Telnyx) | `stream()` or `stream_input()` with µ-law, framed into the provider's JSON |
| A SIP trunk and you want the least code | LiveKit Agents + LiveKit SIP + `svara.livekit.TTS` |
| Pipecat with a telephony transport | `SvaraTTSService`. Do **not** request µ-law; Pipecat's serializer handles it |

## Pre-rendered IVR prompts

```python
prompts = {
    "welcome": "Welcome to Acme Bank. आपकी कॉल हमारे लिए महत्वपूर्ण है।",
    "menu": "For balance, press 1. बैलेंस के लिए 1 दबाएँ।",
    "goodbye": "Thank you. Goodbye.",
}
for name, text in prompts.items():
    client.speech.save(f"{name}.ulaw", input=text, voice="sv_r22w7pwe", **PHONE)
```

- Asterisk and FreeSWITCH play raw `.ulaw`/`.alaw` at 8 kHz directly.
- For Twilio `<Play>` or Plivo `<Play>`, a file URL is easier: render
  `response_format="mp3"` (or `wav` with `sample_rate=8000`) and host it.
- Use `alaw` for A-law trunks. `svara.play(audio, response_format="alaw")` lets
  you listen to an A-law file locally.

## Live replies over a media-stream WebSocket

Send the reply in fixed 20 ms frames: `chunk_size=160` gives 160 bytes, which is
20 ms of 8 kHz µ-law. This is the one place a fixed `chunk_size` is right.

### Twilio Media Streams (FastAPI)

```python
import base64, json
from fastapi import FastAPI, WebSocket
from svara import AsyncSvara

app, svara = FastAPI(), AsyncSvara()

@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    stream_sid = None
    async for raw in ws.iter_text():
        msg = json.loads(raw)
        if msg["event"] == "start":
            stream_sid = msg["start"]["streamSid"]
            await say(ws, stream_sid, "Hello! How can I help you today?")
        elif msg["event"] == "media":
            ...                                    # base64 µ-law from the caller → your STT

async def say(ws, stream_sid, text):
    async for frame in svara.speech.stream(input=text, voice="sv_r22w7pwe",
                                           response_format="ulaw", sample_rate=8000, chunk_size=160):
        await ws.send_text(json.dumps({"event": "media", "streamSid": stream_sid,
                                       "media": {"payload": base64.b64encode(frame).decode()}}))
```

When the caller interrupts, stop the loop and send
`{"event": "clear", "streamSid": stream_sid}` to drop the audio Twilio has
already queued.

### Plivo / Vobiz Audio Streams

The bytes are the same; only the envelope differs:

```python
await ws.send_text(json.dumps({"event": "playAudio", "media": {
    "contentType": "audio/x-mulaw", "sampleRate": 8000,
    "payload": base64.b64encode(frame).decode()}}))
```

### Speaking an LLM reply as it streams

Replace `stream(input=text, …)` with eager input streaming. It takes the LLM's
deltas and gives the lowest latency (see **svara-voice-agent**):

```python
async for frame in svara.speech.stream_input(llm_deltas, voice="sv_r22w7pwe",
                                             response_format="ulaw", sample_rate=8000):
    ...   # frames arrive as generated; re-slice into 160-byte packets if your provider needs exact 20 ms
```

## LiveKit SIP (least code)

The telephony provider (Twilio, Plivo, Telnyx, Exotel, Vobiz or your PBX)
forwards calls over SIP to LiveKit. LiveKit runs the agent, and Svara is its TTS:

```python
from svara.livekit import TTS as SvaraTTS
session = AgentSession(vad=..., stt=..., llm=..., tts=SvaraTTS(voice="sv_r22w7pwe"))
```

Leave the plugin at 24 kHz; LiveKit resamples for the phone leg. The trunk,
dispatch-rule and outbound-call setup is in
https://github.com/kenpath-labs/svara-python/blob/main/docs/deployment/livekit-sip.md

## Phone-specific tips

- **Choose a voice for the line.** A clear `professional`, `confident` or `crisp`
  band-A voice carries best over 8 kHz. Audition voices by synthesising at
  8 kHz, not by listening to the 24 kHz preview.
- **Read numbers correctly.** Pass `language="hi"` (or the caller's language) so
  amounts, dates and units are normalised. Listen to how phone and account
  numbers come out, and spell them in the input the way they should be read.
- **Use a pronunciation dictionary** for brand names and acronyms (see **svara-multilingual**).
- **Keep frames between 20 and 60 ms** so barge-in stays responsive.
- **Reuse one client.** Call `client.warm_up()` at process start so the first call doesn't pay the connection setup.
