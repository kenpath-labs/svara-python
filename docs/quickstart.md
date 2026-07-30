# Quickstart

```bash
pip install git+https://github.com/kenpath-labs/svara-python.git
export SVARA_API_KEY="sk_live_..."
```

## Synthesize a file

```python
from svara import Svara

client = Svara()
audio = client.speech.create(
    input="नमस्ते! This is Svara, speaking Hindi and English in one breath.",
    voice="sv_enhdbrj5",
    response_format="mp3",
)
open("hello.mp3", "wb").write(audio)
```

No language flag needed — Svara detects language from the script and
code-switches automatically. `lang=` is available if you want to force one.

## Stream while it generates

```python
for chunk in client.speech.stream(input="…", voice="sv_enhdbrj5", response_format="pcm"):
    speaker.write(chunk)     # 24 kHz, 16-bit, mono — first bytes in ~0.3–0.5 s
```

## Feed an LLM token stream (async, eager)

```python
import asyncio
from svara import AsyncSvara

async def main():
    client = AsyncSvara()
    async for audio in client.speech.stream_input(
        my_llm_token_stream(),        # any (async) iterable of text
        voice="sv_enhdbrj5",
        on_event=lambda e: print("spoke:", e.text),
    ):
        speaker.write(audio)
    await client.aclose()

asyncio.run(main())
```

Svara starts speaking a few words in (holding back only a small lookahead), so
time-to-first-audio stays low even before the LLM finishes.

## List voices

```python
for v in client.voices.list():
    print(v.voice_id, v.name, v.language, v.gender)
```

## Errors

```python
from svara import RateLimitError, AuthenticationError, SvaraError

try:
    client.speech.create(input="…", voice="sv_enhdbrj5")
except RateLimitError:
    ...            # 429 — back off and retry
except AuthenticationError:
    ...            # 401 — bad key
except SvaraError as e:
    print(e.status_code, e.body)
```

Next: [streaming & latency](streaming.md) · [deployment](deployment/overview.md).
