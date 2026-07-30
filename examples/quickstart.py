"""Synthesize a line and save it. Run: SVARA_API_KEY=sk_live_... python examples/quickstart.py"""

from svara import Svara

client = Svara()  # reads SVARA_API_KEY from the environment

audio = client.speech.create(
    input="नमस्ते! Welcome to Svara — 80 languages, one voice.",
    voice="sv_enhdbrj5",        # any id from client.voices.list()
    response_format="mp3",
)
with open("hello.mp3", "wb") as f:
    f.write(audio)
print(f"wrote hello.mp3 ({len(audio)} bytes)")
