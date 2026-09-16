"""Live integration tests — hit the real API. Skipped unless SVARA_API_KEY is set.

    SVARA_API_KEY=sk_live_... pytest tests/test_integration.py
"""

from __future__ import annotations

import os

import pytest

from svara import FLUSH, AsyncSvara, AuthenticationError, Svara

pytestmark = pytest.mark.skipif(
    not os.environ.get("SVARA_API_KEY"), reason="set SVARA_API_KEY to run live tests"
)

VOICE = os.environ.get("SVARA_TEST_VOICE", "sv_enhdbrj5")


def test_live_voices_and_create():
    with Svara() as c:
        voices = c.voices.list()
        assert len(voices) > 0
        mp3 = c.speech.create(input="Integration test.", voice=VOICE, response_format="mp3")
        assert mp3[:2] == b"\xff\xfb" or mp3[:3] == b"\xff\xf3" or mp3[:3] == b"ID3" or len(mp3) > 1000
        assert mp3.content_type == "audio/mpeg"
        assert mp3.rate_limit.characters is None or isinstance(mp3.rate_limit.characters, int)
        ulaw = c.speech.create(input="Telephony.", voice=VOICE, response_format="ulaw", sample_rate=8000)
        assert len(ulaw) > 1000


def test_live_stream_pcm_with_metadata():
    with Svara() as c:
        s = c.speech.stream(input="one two three", voice=VOICE, response_format="pcm")
        n = sum(len(ch) for ch in s)
        assert n > 0 and n % 2 == 0  # s16le
        assert s.content_type == "audio/pcm" and s.sample_rate == 24000
        assert s.time_to_first_audio is not None and s.time_to_first_audio < 5
        assert s.bytes_received == n


def test_live_catalogue_endpoints():
    with Svara() as c:
        langs = c.languages.list()
        assert any(lang.iso1 == "hi" for lang in langs)
        u = c.usage.get()
        assert u.plan_id
        p = c.voices.preview(VOICE)
        assert p.content_type == "audio/mpeg" and len(p) > 1000
        assert c.voices.list(language="hi")


def test_live_bad_key_is_readable():
    with pytest.raises(AuthenticationError) as ei:
        Svara(api_key="sk_live_not_a_real_key").speech.create(input="x", voice=VOICE)
    assert ei.value.code == "invalid_api_key"


def test_live_sync_stream_input():
    with Svara() as c:
        events = []
        total = sum(len(a) for a in c.speech.stream_input(
            ["Hello ", "there, ", FLUSH, "friend."], voice=VOICE, on_event=events.append))
        assert total > 0 and events


async def test_live_eager_stream_input_and_prepare():
    async def toks():
        for t in ["Hello ", "there."]:
            yield t

    c = AsyncSvara()
    total = 0
    async for audio in c.speech.stream_input(toks(), voice=VOICE, response_format="pcm"):
        total += len(audio)
    assert total > 0
    prepared = await c.speech.prepare(voice=VOICE)
    assert not prepared.expired
    total = 0
    async for audio in prepared.stream(toks()):
        total += len(audio)
    assert total > 0
    await c.aclose()
