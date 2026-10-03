---
name: svara-voice-agent
description: Build low-latency conversational voice agents and voice bots with Svara TTS Turbo, streaming LLM tokens straight into speech (first audio ~130 ms on a prepared socket). Use when building a voice assistant, AI receptionist, realtime voice bot, speech output for an LLM or chatbot, or when wiring TTS into LiveKit Agents, Pipecat, or a custom WebSocket pipeline, especially for Indian or multilingual users. Covers Python (svara-voice) and raw WebSocket from JavaScript.
license: Apache-2.0
compatibility: Needs SVARA_API_KEY and outbound WebSocket (wss) access to api.kenpathlabs.com. Python 3.9+ with `pip install svara-voice` (extras [livekit] or [pipecat] for those frameworks).
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com/input-streaming
---

# Voice agents with Svara TTS Turbo

In a voice agent, TTS is the last hop: caller → STT → LLM → **Svara** → caller.
Svara's input-streaming WebSocket takes the LLM's tokens as they arrive. It
starts speaking eight words in, and it keeps prosody continuous across the
reply because the whole reply is one generation.

Measured time to first audio after the trigger words:

| Path | First audio |
|---|---|
| `prepared.stream()` on a socket opened before the text exists | **132 ms** |
| `stream_input()` on a fresh socket | 427 ms |
| HTTP `stream()` per sentence | ~200 ms for each sentence, after waiting for that sentence to finish |
| ElevenLabs realtime protocol against Svara (buffers 120 characters) | 1047 ms |

**Rule: for a live agent, use `stream_input`/`prepare`. Do not split the LLM
output into sentences and call `stream()` per sentence.** That splitting adds
the wait for each sentence to finish, and it flattens the pauses between
sentences.

Setup: `pip install svara-voice`, export `SVARA_API_KEY`, and pick a voice id
with the **svara-voices** skill. `sv_enhdbrj5` is the default.

## Python: LLM tokens → speech

```python
import asyncio
from svara import AsyncSvara, FLUSH

async def speak_reply(client: AsyncSvara, llm_deltas, player):
    # llm_deltas: any (async) iterable of text fragments
    async for pcm in client.speech.stream_input(llm_deltas, voice="sv_enhdbrj5"):
        player.write(pcm)               # 24 kHz, 16-bit LE mono PCM
```

With the OpenAI Python SDK as the LLM:

```python
async def deltas(openai_client, messages):
    stream = await openai_client.chat.completions.create(model="gpt-4.1-mini", messages=messages, stream=True)
    async for event in stream:
        if event.choices and event.choices[0].delta.content:
            yield event.choices[0].delta.content
    yield FLUSH                         # turn over: speak whatever is buffered
```

### Lowest latency: prepare the socket during the user's turn

```python
async def handle_turn(client: AsyncSvara, user_text, player):
    prepared = await client.speech.prepare(voice="sv_enhdbrj5")     # while the user is still talking
    async for pcm in prepared.stream(deltas(openai_client, history + [user_text])):
        player.write(pcm)
```

- A prepared socket carries one utterance. Prepare a new one for each turn.
- A prepared socket stays usable for minutes. `prepared.expired` reports when one has gone stale.
- Open `prepare()` with `async with` so an unused socket is closed.

### Barge-in

When the user interrupts, stop iterating and close the stream. Abandoning it
costs less than 1 ms. Then flush your player's buffer. The next turn uses a new
socket.

### Tuning knobs

The defaults are measured; change them only for a reason.

- `chunk_words`: default 4, which is also the minimum. Larger values delay the first audio.
- `peek_words`: lookahead, 1–5, default 2.
- `max_chunk_words`: default 20.
- `sample_rate`: for example 16000 if your transport runs at 16 kHz. The server renders at that rate, so no resampling is needed.
- `language`: for example `"hi"`, to force the language and normalise numbers and dates.
- `speed`: 0.7–1.5.
- `pronunciation_dictionary_id`: applies brand terms (see **svara-multilingual**).
- `on_event`: a callback that receives each spoken chunk's text. Use it for captions.

Blocking code without an event loop can call
`Svara().speech.stream_input(iterable, voice=...)`.

## LiveKit Agents

`pip install "svara-voice[livekit]"`

```python
from livekit.agents import AgentSession
from svara.livekit import TTS as SvaraTTS

session = AgentSession(
    vad=..., stt=..., llm=...,
    tts=SvaraTTS(voice="sv_enhdbrj5"),        # eager WebSocket by default; prewarms the next socket
)
```

Options:
- `language="hi"`, `speed=1.05` and `pronunciation_dictionary_id=...` are accepted.
- `tts.update_options(voice=..., language=...)` changes them live.
- `mode="http"` buffers sentences over HTTP. Use it only if a proxy blocks WebSockets.

Driven end to end against production, the plugin reached first audio in
359–435 ms in eager mode, against 672–679 ms in sentence mode. For phone numbers,
put LiveKit SIP in front (see **svara-telephony**). Full example:
https://github.com/kenpath-labs/svara-python/blob/main/examples/livekit_agent.py

## Pipecat

`pip install "svara-voice[pipecat]"` (pipecat-ai ≥ 0.0.105)

```python
from svara.pipecat import SvaraTTSService

tts = SvaraTTSService(voice="sv_enhdbrj5", language="hi")
pipeline = Pipeline([transport.input(), stt, context_aggregator.user(), llm, tts,
                     transport.output(), context_aggregator.assistant()])
```

The service renders PCM at the transport's `audio_out_sample_rate`. Never
request `ulaw` here: Pipecat's telephony serializers do the G.711 encoding.

## JavaScript / any language: the raw WebSocket

```javascript
import WebSocket from "ws";

const ws = new WebSocket(
  "wss://api.kenpathlabs.com/v1/audio/speech/stream-input?voice=sv_enhdbrj5&mode=eager",
  { headers: { Authorization: `Bearer ${process.env.SVARA_API_KEY}` } },
);
ws.on("open", async () => {
  for await (const delta of llmTokenStream()) ws.send(JSON.stringify({ text: delta }));
  ws.send(JSON.stringify({ text: "" }));                 // end of input
});
ws.on("message", (data, isBinary) => {
  if (isBinary) player.feed(data);                       // PCM16 LE mono, 24 kHz
  else {
    const msg = JSON.parse(data);
    if (msg.type === "done") ws.close();
    if (msg.type === "error") console.error(msg.message);
  }
});
```

- Always set `mode=eager`, because the server's default is `sentence`.
- Send `{"flush": true}` to speak buffered text immediately.
- One socket carries one utterance. To cut first-audio latency, open the next
  socket while the user is talking.
- If the socket closes without `{"type":"done"}`, the audio is truncated.
- Close code 1013 means no engine was ready. Retry that utterance.

Full protocol: the `svara-tts` skill's `references/rest-api.md`.

## Where the time goes

On a real phone call:

| Stage | Time |
|---|---|
| End-of-turn detection + STT | ~1.1–1.3 s |
| LLM first token | ~0.7–1.3 s |
| Svara first audio | ~0.13–0.4 s |
| PSTN hop | ~0.1–0.3 s |

Optimise turn detection, STT and the LLM first. Then add `prepare()`, and reuse
one `AsyncSvara` per process. A new client for each turn costs about 140 ms.
