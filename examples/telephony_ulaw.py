"""Telephony: request 8 kHz G.711 µ-law, ready to drop into a SIP/WebSocket media
stream (e.g. Vobiz ``playAudio`` with contentType audio/x-mulaw, sampleRate 8000).

Run: SVARA_API_KEY=sk_live_... python examples/telephony_ulaw.py
"""

from svara import Svara

client = Svara()

# ulaw is inherently 8 kHz, 1 byte/sample — no resampling needed for phone audio.
ulaw = client.speech.create(
    input="Thank you for calling. आपकी कॉल के लिए धन्यवाद।",
    voice="sv_enhdbrj5",
    response_format="ulaw",
)
with open("prompt.ulaw", "wb") as f:
    f.write(ulaw)
print(f"wrote prompt.ulaw ({len(ulaw)} bytes ≈ {len(ulaw)/8000:.2f}s @8kHz µ-law)")

# Streaming µ-law, base64-framed the way most media-stream WebSockets expect:
import base64

with open("prompt_frames.txt", "w") as f:
    for chunk in client.speech.stream(
        input="This chunk streams as it is generated.",
        voice="sv_enhdbrj5",
        response_format="ulaw",
        chunk_size=320,  # 40 ms frames @ 8 kHz µ-law
    ):
        f.write(base64.b64encode(chunk).decode() + "\n")
print("wrote base64 µ-law frames to prompt_frames.txt")
