"""Prepared socket: open the WebSocket before the text exists.

Run: SVARA_API_KEY=sk_live_... python examples/prepare_stream.py

In a voice agent the connect otherwise lands at the worst moment — the instant
the user stops talking. Measured: first audio 427 ms after the call on a fresh
socket, 132 ms on a prepared one. See MEASUREMENTS.md.
"""

import asyncio
import time

from svara import FLUSH, AsyncSvara


async def llm_tokens():
    reply = "Sure. The quickest route is the metro, then a ten minute walk. Shall I send directions?"
    for word in reply.split(" "):
        await asyncio.sleep(0.04)  # a realistic token cadence
        yield word + " "
    yield FLUSH  # speak whatever is buffered now; the turn is over


async def main():
    client = AsyncSvara()

    # 1. The user is still speaking: open the socket now.
    prepared = await client.speech.prepare(voice="sv_enhdbrj5")

    # 2. The LLM starts producing. Feed it straight in.
    t0 = time.perf_counter()
    first = None
    total = 0
    async for audio in prepared.stream(llm_tokens()):
        if first is None:
            first = time.perf_counter()
        total += len(audio)
    # The eight-word trigger alone takes ~320 ms to feed at this cadence; the
    # rest is Svara. On a fresh socket the same figure is ~300 ms higher.
    print(f"first audio {(first - t0) * 1000:.0f} ms after the first token; "
          f"{total / 2 / 24000:.2f}s of 24 kHz PCM")
    await client.aclose()


asyncio.run(main())
