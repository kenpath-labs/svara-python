# Svara Python SDK

Svara is [Kenpath Labs'](https://kenpathlabs.com) text-to-speech engine:
**80 languages** with automatic code-switching, 320 voices, streaming over HTTP
and WebSocket, and telephony formats out of the box. This SDK is a thin, typed
client for the public API at `https://api.kenpathlabs.com`.

```bash
pip install svara-voice
```

## Contents

- [Installation](installation.md)
- [Quickstart](quickstart.md)
- [Streaming & latency](streaming.md) — the three paths, and the measured numbers behind the defaults
- [Voices & languages](voices.md)
- [API reference](api-reference.md)
- [Compatibility](compatibility.md) — using the OpenAI or ElevenLabs SDKs against Svara, and migrating from them
- [Troubleshooting](debugging.md) — `svara doctor`, symptom → cause, what the numbers mean
- **Deployment**
  - [Overview — pick a topology](deployment/overview.md)
  - [Local](deployment/local.md) · [Docker](deployment/docker.md) · [Cloud host](deployment/cloud.md)
  - [LiveKit + SIP (phone calls)](deployment/livekit-sip.md)
  - [Raw WebSocket media (Pipecat / other telephony)](deployment/websocket-media.md)
- [llms.txt](llms.txt) — this documentation condensed for coding assistants

## The 30-second version

```python
from svara import Svara

client = Svara()                                   # reads SVARA_API_KEY
client.speech.create(input="नमस्ते! Welcome to Svara.", voice="sv_enhdbrj5").save("hi.mp3")
```

## Which call do I want?

| You want… | Use | First audio |
|---|---|---|
| A file or buffer | `client.speech.create(...)` → bytes | after the whole clip renders (~7× realtime) |
| Play as it generates | `client.speech.stream(...)` → chunks | ≈ 200 ms |
| Speak an LLM's tokens as they arrive | `AsyncSvara().speech.stream_input(...)` | ≈ 130 ms after the trigger words, on a prepared socket |
| A phone or browser voice agent | `svara-voice[livekit]` or `svara-voice[pipecat]` | the plugins use the paths above |
| Raw telephony media | `response_format="ulaw", sample_rate=8000` | see [WebSocket media](deployment/websocket-media.md) |
