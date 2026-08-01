# Svara Python SDK

Svara is [Kenpath Labs'](https://kenpathlabs.com) multilingual text-to-speech
engine: **80 languages** with automatic code-switching, natural prosody,
streaming, and telephony-ready audio. This SDK is a
thin, typed wrapper over the public API at `https://api.kenpathlabs.com`.

## Contents

- [Installation](installation.md)
- [Quickstart](quickstart.md)
- [API reference](api-reference.md)
- [Streaming & latency](streaming.md)
- [Voices](voices.md)
- **Deployment**
  - [Overview — pick a topology](deployment/overview.md)
  - [Local](deployment/local.md)
  - [Docker](deployment/docker.md)
  - [Cloud host (Railway / RunPod / Fly)](deployment/cloud.md)
  - [LiveKit + SIP (phone calls)](deployment/livekit-sip.md)
  - [Raw WebSocket media (Pipecat / other telephony)](deployment/websocket-media.md)

## The 30-second version

```python
from svara import Svara
client = Svara(api_key="sk_live_...")
open("hi.mp3", "wb").write(
    client.speech.create(input="नमस्ते! Welcome to Svara.", voice="sv_enhdbrj5")
)
```

## Two ways to use it

| You want… | Use |
|---|---|
| A file / buffer of audio | `client.speech.create(...)` → bytes |
| Play as it generates | `client.speech.stream(...)` → chunks (~0.3–0.5 s to first byte) |
| Feed an LLM token stream, speak live | `AsyncSvara().speech.stream_input(...)` (eager WS) |
| A phone / voice agent | `svara-voice[livekit]` → [LiveKit + SIP](deployment/livekit-sip.md) |
| Raw telephony media | `response_format="ulaw"` → [WebSocket media](deployment/websocket-media.md) |
