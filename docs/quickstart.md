# Quickstart

```bash
pip install svara-voice
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
audio.save("hello.mp3")           # audio is bytes; .sample_rate, .content_type, .rate_limit ride along
```

No language flag needed — Svara reads the script and code-switches on its
own. Pass `language="hi"` to force one (it also turns on number, date and unit
normalisation for that language).

## Stream while it generates

```python
stream = client.speech.stream(input="…", voice="sv_enhdbrj5", response_format="pcm")
for chunk in stream:
    player.write(chunk)           # 24 kHz, 16-bit, mono
print(stream.time_to_first_audio) # measured at this client, in seconds
```

Chunks are yielded the instant they arrive. Pass `chunk_size=` only when a
consumer needs fixed-size frames (telephony wants 20 ms packets); it can only
hold audio back, never make it arrive sooner.

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
        player.write(audio)
    await client.aclose()

asyncio.run(main())
```

Svara starts speaking eight words in by default (`chunk_words=4`,
`peek_words=2`), so audio begins before the LLM finishes its first sentence. To shave a further
~300 ms off the first reply, open the socket before the text exists:

```python
async def turn(client: AsyncSvara, get_llm_tokens):
    prepared = await client.speech.prepare(voice="sv_enhdbrj5")   # call while the user is still talking
    tokens = await get_llm_tokens()                               # the LLM request goes out here
    async for audio in prepared.stream(tokens):
        player.write(audio)
```

Yield `svara.FLUSH` from the token stream to have everything buffered spoken
immediately, for instance at the end of a paragraph.

Synchronous code gets the same thing from `Svara().speech.stream_input(...)`,
which feeds the text from a helper thread.

## Telephony

```python
ulaw = client.speech.create(input="…", voice="sv_enhdbrj5", response_format="ulaw", sample_rate=8000)
```

Always pass `sample_rate=8000` with `ulaw`/`alaw`: the API renders 24 kHz
unless told otherwise, and 24 kHz µ-law on an 8 kHz phone leg plays at three
times speed. The SDK warns when the rate is missing.

## Voices

```python
for v in client.voices.list(language="hi"):
    print(v.voice_id, v.name, v.gender, v.quality_band)
```

## Errors

```python
from svara import SvaraError, RateLimitError, QuotaExceededError, AuthenticationError

try:
    client.speech.create(input="…", voice="sv_enhdbrj5")
except QuotaExceededError:
    ...            # 429 insufficient_quota — terminal until the month resets; not retried
except RateLimitError as e:
    ...            # 429 — retried already; e.retry_after says how long the server asked for
except AuthenticationError:
    ...            # 401 — bad key
except SvaraError as e:
    print(e.status_code, e.code, e.message)
```

Next: [streaming & latency](streaming.md) · [API reference](api-reference.md) ·
[deployment](deployment/overview.md).
