# svara

Python SDK for **Svara** — [Kenpath Labs'](https://kenpathlabs.com) multilingual
text-to-speech API. 80 languages with automatic code-switching, streaming, voice
cloning, and telephony-ready audio.

- **Docs:** https://docs.kenpathlabs.com
- **API base:** `https://api.kenpathlabs.com`

## Install

```bash
pip install git+https://github.com/kenpath-labs/svara-python.git          # core SDK
pip install "svara[livekit] @ git+https://github.com/kenpath-labs/svara-python.git"  # + LiveKit plugin
```

(Published to PyPI later — until then, install from git.)

## Quickstart

```python
from svara import Svara

client = Svara(api_key="sk_live_...")          # or set SVARA_API_KEY
audio = client.speech.create(
    input="नमस्ते! Welcome to Svara.",
    voice="sv_enhdbrj5",                       # any id from client.voices.list()
    response_format="mp3",
)
open("hello.mp3", "wb").write(audio)
```

### Stream (low latency)

```python
for chunk in client.speech.stream(input="...", voice="sv_enhdbrj5", response_format="pcm"):
    play(chunk)   # 24 kHz s16le, first bytes in ~0.3–0.5 s
```

### Async + eager input-streaming (feed an LLM token stream)

```python
from svara import AsyncSvara

client = AsyncSvara()
async for audio in client.speech.stream_input(llm_token_stream, voice="sv_enhdbrj5"):
    play(audio)   # starts speaking a few words in — ideal for voice agents
```

### Telephony (8 kHz µ-law)

```python
ulaw = client.speech.create(input="...", voice="sv_enhdbrj5", response_format="ulaw")
# drop straight into a SIP / media-stream WebSocket — no resampling
```

### List voices

```python
for v in client.voices.list():
    print(v.voice_id, v.name, v.language, v.gender)
```

## LiveKit voice agents

```python
from svara.livekit import TTS
session = AgentSession(tts=TTS(voice="sv_enhdbrj5", mode="eager"), stt=..., llm=...)
```

## Formats

`mp3`, `opus`, `aac`, `flac`, `wav` (containered) · `pcm` (24 kHz s16le) ·
`ulaw` / `alaw` (8 kHz G.711, telephony). Pass `sample_rate=` to override PCM rate.

## Deployment

See [`docs/deployment/`](docs/deployment/) for local, Docker, cloud, LiveKit+SIP
(inbound & outbound), and raw WebSocket-media (Pipecat / other telephony) guides.

## License

Proprietary © Kenpath Labs.
