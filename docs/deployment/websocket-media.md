# Raw WebSocket media (Pipecat / custom telephony)

When you don't want LiveKit in the path — you're running your own media loop, or
the telephony provider streams audio to a WebSocket you control — Svara drops in
directly, because it emits **8 kHz G.711 µ-law** (`response_format="ulaw",
sample_rate=8000` — the rate is required, the API defaults to 24 kHz), the
exact wire format phone networks use. No transcoding.

```
 📞 caller ⇄ provider media WebSocket ⇄ your server ⇄ [ STT → LLM → Svara ulaw ]
```

Providers with this model: **Vobiz Audio Streams**, Twilio Media Streams, Plivo
AudioStream, Telnyx — all send base64 µ-law frames and accept the same back.

## Svara → telephony

Request µ-law and stream it out in small frames:

```python
import base64
from svara import AsyncSvara

client = AsyncSvara()

async for frame in client.speech.stream(
    input=reply_text,
    voice="sv_enhdbrj5",
    response_format="ulaw",     # G.711 µ-law, 1 byte/sample
    sample_rate=8000,           # required: the API renders 24 kHz otherwise
    chunk_size=320,             # 40 ms frames (8000 * 0.04)
):
    await ws.send_json({
        "event": "playAudio",
        "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000,
                  "payload": base64.b64encode(frame).decode()},
    })
```

That `playAudio` shape is Vobiz's; Twilio uses `{"event":"media","media":{"payload":...}}`.
Same bytes, different envelope. Keep frames 20–60 ms so barge-in/interruption
stays responsive.

## For a live agent: eager streaming

Feed your LLM's tokens straight into Svara and forward µ-law frames as they come
— lowest latency, natural prosody:

```python
async for frame in client.speech.stream_input(
    llm_token_stream,
    voice="sv_enhdbrj5",
    response_format="ulaw",
    sample_rate=8000,
):
    await ws.send_json({"event": "playAudio",
                        "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000,
                                  "payload": base64.b64encode(frame).decode()}})
```

## Inbound audio (caller → you)

The provider sends the caller's audio as base64 µ-law frames (e.g. Vobiz
`{"event":"media","media":{"payload": "..."}}`). Decode and feed your STT; when
the LLM produces a reply, synthesize with Svara as above. Svara handles the TTS
half — STT/LLM are yours.

## Pipecat

Pipecat orchestrates this loop (transports for Twilio/Telnyx/etc., STT, LLM).
`pip install "svara-voice[pipecat]"` and drop the service in:

```python
from svara.pipecat import SvaraTTSService

tts = SvaraTTSService(voice="sv_enhdbrj5")     # follows the transport's audio_out_sample_rate
```

Pipecat aggregates the LLM output into sentences and the service streams each
one back over HTTP as PCM at the transport's rate (8 kHz on a phone
transport, rendered at that rate by the server). Pipecat's telephony
serializers do the G.711 companding themselves — do not ask Svara for `ulaw`
inside a Pipecat pipeline.

## Starting the stream (Vobiz specifics)

Vobiz opens the bidirectional stream either via an **XML `<Stream bidirectional="true">`**
returned from your Answer URL, or the **Audio Stream REST API** on an active
call. On connect it sends a `start` event (with `mediaFormat`), then inbound
`media` events. Match your decode to `start.mediaFormat`; send `playAudio` back.
See the Vobiz "WebSockets & Streaming" docs for the exact envelope.

## When to choose this over LiveKit + SIP

| | WebSocket media | [LiveKit + SIP](livekit-sip.md) |
|---|---|---|
| Transcoding | none (Svara `ulaw` = wire format) | LiveKit resamples 24 k ↔ 8 k |
| Control | you own every frame | framework handles media |
| Setup | a WebSocket server + your STT/LLM loop | trunks + dispatch rule |
| Best for | maximum control, existing media stack, Pipecat | fastest robust path, browser + phone |
