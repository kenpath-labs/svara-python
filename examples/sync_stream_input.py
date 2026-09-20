"""Eager input-streaming without an event loop.

Run: SVARA_API_KEY=sk_live_... python examples/sync_stream_input.py

The same WebSocket protocol as the async client, on a blocking socket. The
text source runs in a helper thread so a slow producer never stalls audio.
"""

from svara import Svara


def tokens():
    for word in "This is eager streaming from a plain generator, no asyncio required.".split(" "):
        yield word + " "


client = Svara()
with open("sync_eager.pcm", "wb") as f:
    for audio in client.speech.stream_input(
        tokens(), voice="sv_enhdbrj5",
        on_event=lambda e: print("spoke:", e.text, "| peek:", e.peek),
    ):
        f.write(audio)
client.close()
print("wrote sync_eager.pcm (24 kHz s16le mono)")
