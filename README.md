# svara-voice

Python SDK for **Svara**, [Kenpath Labs'](https://kenpathlabs.com) text-to-speech
API: 80 languages with automatic code-switching, 320 voices, streaming over HTTP
and WebSocket, and telephony formats out of the box.

- Docs: https://docs.kenpathlabs.com · API reference for this package: [`docs/api-reference.md`](docs/api-reference.md)
- API base: `https://api.kenpathlabs.com`
- Requires Python 3.9+. Depends on `httpx` and `websockets` only.

```bash
pip install svara-voice
```

The distribution is `svara-voice`; the import is `svara`.

## Quickstart

```python
from svara import Svara

client = Svara(api_key="sk_live_...")            # or set SVARA_API_KEY

audio = client.speech.create(
    input="नमस्ते! Welcome to Svara.",            # any language, mixed scripts are fine
    voice="sv_enhdbrj5",                         # any id from client.voices.list()
    response_format="mp3",
)
audio.save("hello.mp3")                          # it is bytes, with headers attached
```

### Stream while it generates

```python
for chunk in client.speech.stream(input="...", voice="sv_enhdbrj5", response_format="pcm"):
    player.write(chunk)                          # 24 kHz, 16-bit, mono; first audio ≈ 200 ms
```

### Speak an LLM's tokens as they arrive

The lowest-latency path, and the one voice agents should be on. Feed the token
stream in; Svara starts speaking a few words into the sentence and keeps
prosody continuous across the whole reply.

```python
from svara import AsyncSvara

client = AsyncSvara()

async for audio in client.speech.stream_input(llm_token_stream, voice="sv_enhdbrj5"):
    player.write(audio)
```

Open the socket before the text exists and first audio lands ~300 ms sooner:

```python
prepared = await client.speech.prepare(voice="sv_enhdbrj5")   # while the user is still talking
...
async for audio in prepared.stream(llm_token_stream):
    player.write(audio)
```

There is a blocking twin, `Svara().speech.stream_input(...)`, for code without an
event loop.

### Telephony

```python
ulaw = client.speech.create(input="...", voice="sv_enhdbrj5", response_format="ulaw", sample_rate=8000)
```

`sample_rate=8000` is not optional: the API renders every format at 24 kHz
unless told otherwise, G.711 included, and 24 kHz µ-law on an 8 kHz phone leg
plays at three times speed. The SDK warns if you leave it out.

### Timestamps

```python
r = client.speech.create_with_timestamps(input="...", voice="sv_enhdbrj5")
r.audio, r.alignment.words()                 # [(word, start_s, end_s), ...] for subtitles or karaoke
```

### Voices, languages, usage

```python
client.voices.list(language="hi", gender="female")   # filtered client-side
client.voices.preview("sv_enhdbrj5")                 # a sample clip, audio/mpeg
client.languages.list()                              # 80 languages and the codes `language=` accepts
client.usage.get().characters_remaining              # plan, month-to-date, balance
```

### Errors

```python
from svara import SvaraError, RateLimitError, QuotaExceededError

try:
    client.speech.create(input="...", voice="sv_enhdbrj5")
except QuotaExceededError:      # 429 insufficient_quota — terminal until the month resets
    ...
except RateLimitError as e:     # 429 — already retried with backoff; e.retry_after in seconds
    ...
except SvaraError as e:
    print(e.status_code, e.code, e.message)
```

Connection errors, 429s and 5xx are retried twice with jittered backoff and
`Retry-After` honoured. Streams retry only until the first byte arrives, so a
retry never replays audio the caller is already playing.

## Voice-agent frameworks

**LiveKit Agents** — `pip install "svara-voice[livekit]"`

```python
from svara.livekit import TTS
session = AgentSession(tts=TTS(voice="sv_enhdbrj5"), stt=..., llm=...)
```

**Pipecat** — `pip install "svara-voice[pipecat]"`

```python
from svara.pipecat import SvaraTTSService
pipeline = Pipeline([transport.input(), stt, llm, SvaraTTSService(voice="sv_enhdbrj5"), transport.output()])
```

Both stream 24 kHz PCM straight into the framework's audio path with no
resampling. The LiveKit plugin rides the input-streaming WebSocket and keeps a
socket prewarmed between turns.

## Using the OpenAI or ElevenLabs SDKs instead

Svara is request-compatible with both. Point `base_url` at Svara and they work
unmodified, including ElevenLabs' realtime WebSocket client. See
[`docs/compatibility.md`](docs/compatibility.md) for the exact base URLs and a
field-by-field mapping, and for what only this SDK can do (the native
input-streaming socket reaches first audio ~0.9 s before the ElevenLabs protocol).

## CLI

```bash
svara say "नमस्ते दुनिया" --voice sv_enhdbrj5 --out hello.mp3
svara say "Your call is important" -v sv_enhdbrj5 -f ulaw -r 8000 -o prompt.ulaw
svara voices --language hi
svara languages
svara usage
```

## Formats

`mp3` · `opus` · `aac` · `flac` · `wav` · `pcm` (16-bit LE mono) · `ulaw` / `alaw` (G.711).
`sample_rate` ∈ 8000, 16000, 22050, 24000 (default), 32000, 44100, 48000.
`bitrate_kbps` for the lossy three. ElevenLabs-style names translate with
`output_format("mp3_44100_128")`.

## Documentation

[`docs/`](docs/) covers installation, streaming and latency (with the measured
numbers behind every default), voices, the full API reference, compatibility
with other SDKs, and deployment guides for local, Docker, cloud, LiveKit + SIP
and raw WebSocket telephony. [`MEASUREMENTS.md`](MEASUREMENTS.md) is the lab
notebook. [`docs/llms.txt`](docs/llms.txt) is the same material condensed for
coding assistants.

## License

Proprietary © Kenpath Labs. See [LICENSE](LICENSE).
