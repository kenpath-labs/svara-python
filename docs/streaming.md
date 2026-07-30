# Streaming & latency

Three ways to get audio, from simplest to lowest-latency:

## 1. `create()` — one shot
Returns the whole clip. Simplest; highest latency (you wait for all of it).
Best for fixed prompts, batch, or when you need a file.

## 2. `stream()` — chunked HTTP
Yields audio as it's generated; first bytes in ~0.3–0.5 s. Good when you have
the full text up front but want playback to start immediately.

```python
for chunk in client.speech.stream(input=text, voice=v, response_format="pcm"):
    speaker.write(chunk)
```

## 3. `stream_input()` — eager WebSocket (for live agents)
You don't have the full text yet — it's arriving token-by-token from an LLM.
Feed those tokens in; Svara starts speaking after a few words, holding back only
`peek_words` of lookahead. Lowest perceived latency and the most natural
cross-sentence prosody, because the whole reply is one continuous generation.

```python
async for audio in client.speech.stream_input(llm_tokens, voice=v):
    speaker.write(audio)
```

### `eager` vs sentence-buffered
If you synthesize an LLM reply by waiting for each full sentence and calling
`stream()` per sentence, the sentences are independent generations stitched
back-to-back — which flattens the pauses between them and adds a
wait-for-sentence delay. `stream_input(mode="eager")` avoids both. **For voice
agents, prefer eager.** (This is exactly the difference we measured on live
phone calls: eager gave natural sentence breaks; per-sentence HTTP ran them
together.)

Tuning knobs: `chunk_words` (words buffered before the first chunk; smaller =
faster first audio), `peek_words` (trained lookahead, 1–5; 2 is default),
`max_chunk_words` (cap on later chunks).

## Where latency actually goes (voice agent, measured)

On a real phone call the perceived "I stopped talking → it starts talking" gap
breaks down roughly as:

| Stage | Typical | Notes |
|---|---|---|
| End-of-turn detection + STT | ~1.1–1.3 s | dominated by turn-taking + a non-streaming STT |
| LLM first token | ~0.7–1.3 s | model-dependent |
| **Svara first audio** | **~0.35 s** | the fastest link — not your bottleneck |
| Telephony (PSTN/SIP) hop | ~0.1–0.3 s | only on phone, not browser/WebRTC |

Takeaways: use `eager`; pick a fast STT (a streaming STT beats a non-streaming
one by ~0.5–0.9 s); a lighter LLM trims first-token time. TTS is already optimal.
