"""Eager input-streaming: feed text as it's produced (e.g. LLM tokens), get audio
as the model speaks. Run: SVARA_API_KEY=sk_live_... python examples/stream_eager.py"""

import asyncio

from svara import AsyncSvara


async def fake_llm_tokens():
    for piece in ["नमस्ते, ", "मैं स्वरा हूँ। ", "आपकी कैसे ", "मदद कर सकती हूँ?"]:
        await asyncio.sleep(0.15)  # pretend tokens arrive over time
        yield piece


async def main():
    client = AsyncSvara()
    total = 0
    with open("eager.pcm", "wb") as f:
        async for audio in client.speech.stream_input(
            fake_llm_tokens(),
            voice="sv_enhdbrj5",
            response_format="pcm",           # 24 kHz s16le
            on_event=lambda e: print("spoke:", e.text, "| peek:", e.peek),
        ):
            f.write(audio)
            total += len(audio)
    await client.aclose()
    print(f"wrote eager.pcm ({total} bytes ≈ {total/2/24000:.2f}s @24kHz)")


asyncio.run(main())
