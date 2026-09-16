# Streaming & latency

Three ways to get audio, from simplest to lowest-latency. Every number on this
page was measured against production from a laptop in India; the method and
the full tables are in [`MEASUREMENTS.md`](../MEASUREMENTS.md).

## 1. `create()` — one shot

Returns the whole clip. Simplest; highest latency, because nothing arrives
until the whole clip has rendered (about 7× realtime: 2,400 characters →
162 s of audio in 24 s). Best for fixed prompts, batch jobs, files.

## 2. `stream()` — chunked HTTP

Yields audio as it renders; **first audio in ~200 ms** on a warm connection.
Right when you have the full text and want playback to start now.

```python
for chunk in client.speech.stream(input=text, voice=v, response_format="pcm"):
    speaker.write(chunk)
```

The server front-loads a small first frame (1920 B of PCM = 40 ms) so playback
can start early. The SDK yields it the moment it lands. Re-buffering it — the
old `chunk_size=4096` default did — cost 46–131 ms; pass `chunk_size` only when
a consumer needs exact frame sizes.

## 3. `stream_input()` — eager WebSocket, for live agents

The text is still being produced, token by token, by an LLM. Feed the tokens
in; Svara starts speaking after `2 × chunk_words` words (8 by default),
holding back only `peek_words` of lookahead, and keeps prosody continuous
across the whole reply because it is one generation rather than a sentence at
a time.

```python
async for audio in client.speech.stream_input(llm_tokens, voice=v):
    speaker.write(audio)
```

| | First audio after the trigger words |
|---|---|
| `stream_input()` on a fresh socket | 427 ms |
| `prepared.stream()` on a socket opened earlier | **132 ms** |

`prepare()` opens the socket before the text exists — while the user is still
talking, or as the LLM request goes out — so the connect and admission are
not paid at the one moment the caller is waiting. A prepared socket stays
usable for minutes (measured to 300 s idle); `PreparedStream.expired` tells
you if one has gone stale.

### Eager vs sentence-buffered

Synthesising an LLM reply one full sentence at a time over `stream()` means
independent generations stitched back to back: flatter pauses between
sentences, plus the wait for each sentence to complete. `stream_input` avoids
both. **For voice agents, use eager.** The LiveKit plugin does by default;
driven end to end against production it reached first audio in 359–435 ms on
a prepared socket versus 672–679 ms in its sentence-buffered mode.

Knobs: `chunk_words` (words per chunk; smaller = earlier first audio),
`peek_words` (lookahead, 1–5, default 2), `max_chunk_words` (cap once text has
queued up). Yield `svara.FLUSH` to force out whatever is buffered.

### Sync callers

`Svara().speech.stream_input(...)` is the same protocol on a blocking socket,
with the text fed from a helper thread. Prefer the async client inside an
async application.

## What the SDK does about its own latency

Measured against a bare `httpx` call with the identical payload, the SDK adds
nothing detectable (203 ms vs 204 ms to first audio). Its transport defaults,
though, are chosen for a conversation rather than for a batch script:

- **Connections are kept for 120 s**, not httpx's 5 s. Turns are further
  apart than 5 s; re-doing TCP + TLS on each one cost +140 ms.
- **`client.warm_up()`** opens the HTTP connection at start-up so the first
  utterance does not pay it (~100 ms).
- **Retries stop at the first byte.** Once audio has been handed to you, a
  retry would replay part of an utterance you are already playing.
- **Abandoning a stream is free** (<1 ms on either path). The next HTTP
  request reconnects, because HTTP/1.1 cannot reuse a half-read response;
  each eager utterance is its own socket anyway.

## Where the time goes in a voice agent

The perceived gap between the caller going quiet and hearing a reply, on a
real phone call:

| Stage | Typical | Notes |
|---|---|---|
| End-of-turn detection + STT | ~1.1–1.3 s | dominated by turn-taking and a non-streaming STT |
| LLM first token | ~0.7–1.3 s | model-dependent |
| **Svara first audio** | **~0.13–0.4 s** | prepared socket vs fresh; the fastest link |
| Telephony (PSTN/SIP) hop | ~0.1–0.3 s | phone only, not browser/WebRTC |

Use eager mode, a streaming STT, and a fast LLM. TTS is not the bottleneck.
