"""Live integration tests — hit the real API. Skipped unless SVARA_API_KEY is set.

    SVARA_API_KEY=sk_live_... pytest tests/test_integration.py
"""

from __future__ import annotations

import os

import pytest

from svara import AsyncSvara, Svara

pytestmark = pytest.mark.skipif(
    not os.environ.get("SVARA_API_KEY"), reason="set SVARA_API_KEY to run live tests"
)

VOICE = os.environ.get("SVARA_TEST_VOICE", "sv_enhdbrj5")


def test_live_voices_and_create():
    c = Svara()
    voices = c.voices.list()
    assert len(voices) > 0
    mp3 = c.speech.create(input="Integration test.", voice=VOICE, response_format="mp3")
    assert mp3[:2] == b"\xff\xfb" or mp3[:3] == b"\xff\xf3" or mp3[:3] == b"ID3" or len(mp3) > 1000
    ulaw = c.speech.create(input="Telephony.", voice=VOICE, response_format="ulaw")
    assert len(ulaw) > 1000
    c.close()


def test_live_stream_pcm():
    c = Svara()
    n = sum(len(ch) for ch in c.speech.stream(input="one two three", voice=VOICE, response_format="pcm"))
    assert n > 0 and n % 2 == 0  # s16le
    c.close()


async def test_live_eager_stream_input():
    async def toks():
        for t in ["Hello ", "there."]:
            yield t

    c = AsyncSvara()
    total = 0
    async for audio in c.speech.stream_input(toks(), voice=VOICE, response_format="pcm"):
        total += len(audio)
    await c.aclose()
    assert total > 0
