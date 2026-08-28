"""Telephony: 8 kHz G.711 µ-law, ready for a SIP/WebSocket media stream
(e.g. Vobiz ``playAudio`` with contentType audio/x-mulaw, sampleRate 8000).

Run: SVARA_API_KEY=sk_live_... python examples/telephony_ulaw.py

NOTE — ``sample_rate=8000`` is not optional here. The API returns **24 kHz**
µ-law when the request omits it, for every format including the G.711 ones.
24 kHz µ-law fed to an 8 kHz phone leg plays at three times speed, and the
symptom sounds like a broken voice rather than a wrong clock. The SDK warns if
you leave it out. Verified against production; see MEASUREMENTS.md.
"""

import base64

from svara import Svara

client = Svara()

TELEPHONY = dict(response_format="ulaw", sample_rate=8000)  # 1 byte/sample @ 8 kHz

ulaw = client.speech.create(
    input="Thank you for calling. आपकी कॉल के लिए धन्यवाद।",
    voice="sv_enhdbrj5",
    **TELEPHONY,
)
with open("prompt.ulaw", "wb") as f:
    f.write(ulaw)
print(f"wrote prompt.ulaw ({len(ulaw)} bytes ≈ {len(ulaw) / 8000:.2f}s @8kHz µ-law)")

# Streaming µ-law, base64-framed the way most media-stream WebSockets expect.
# chunk_size is the one place a fixed size is right: a phone leg wants exact
# 20 ms packets, and 160 bytes is 20 ms at 8 kHz µ-law. Leave it unset anywhere
# you do not need fixed framing — the SDK then yields audio as it arrives.
with open("prompt_frames.txt", "w") as f:
    for chunk in client.speech.stream(
        input="This chunk streams as it is generated.",
        voice="sv_enhdbrj5",
        chunk_size=160,  # 20 ms frames @ 8 kHz µ-law
        **TELEPHONY,
    ):
        f.write(base64.b64encode(chunk).decode() + "\n")
print("wrote base64 µ-law frames to prompt_frames.txt")
