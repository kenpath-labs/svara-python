"""Unit tests — no network. Uses httpx.MockTransport to fake the API."""

from __future__ import annotations

import json

import httpx
import pytest

import svara._client as core
from svara import (
    FORMAT_INFO,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    Svara,
    Voice,
)
from svara._client import _speech_payload, _ws_url


# ── pure helpers ──────────────────────────────────────────────────────────────
def test_payload_maps_language_to_lang_and_omits_none_sampling():
    p = _speech_payload(
        input="hi", voice="sv_x", model="svara-1", response_format="pcm",
        stream=True, sample_rate=8000, speed=1.1, language="hi",
        sampling={"temperature": None, "top_p": 0.9}, extra=None,
    )
    assert p["lang"] == "hi"
    assert p["sample_rate"] == 8000 and p["speed"] == 1.1
    assert p["top_p"] == 0.9
    assert "temperature" not in p          # None sampling omitted
    assert "language" not in p             # renamed to lang


def test_ws_url_scheme_and_params():
    url = _ws_url("https://api.kenpathlabs.com", {"voice": "sv_x", "mode": "eager", "skip": None})
    assert url.startswith("wss://api.kenpathlabs.com/v1/audio/speech/stream-input?")
    assert "voice=sv_x" in url and "mode=eager" in url and "skip" not in url


def test_format_info_ulaw_is_8k_telephony():
    assert FORMAT_INFO["ulaw"]["default_rate"] == 8000
    assert FORMAT_INFO["pcm"]["default_rate"] == 24000
    assert FORMAT_INFO["ulaw"]["container"] is False


def test_voice_from_dict_language():
    v = Voice.from_dict({"voice_id": "sv_x", "name": "A", "labels": {"native_language_code": "hi"}})
    assert v.voice_id == "sv_x" and v.name == "A" and v.language == "hi"


# ── client behavior via MockTransport ──────────────────────────────────────────
def _client(handler, **kw) -> Svara:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.kenpathlabs.com")
    return Svara(api_key="sk_test", http_client=http, **kw)


def test_create_returns_bytes_and_sends_key():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("xi-api-key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"AUDIODATA", headers={"content-type": "audio/mpeg"})

    c = _client(handler)
    out = c.speech.create(input="hi", voice="sv_x", response_format="mp3")
    assert out == b"AUDIODATA"
    assert seen["auth"] == "sk_test"
    assert seen["body"]["voice"] == "sv_x"


def test_voices_list_parses():
    def handler(req):
        return httpx.Response(200, json={"voices": [
            {"voice_id": "sv_a", "name": "A", "labels": {"native_language_code": "hi"}},
            {"voice_id": "sv_b", "name": "B", "gender": "male"},
        ]})

    c = _client(handler)
    vs = c.voices.list()
    assert [v.voice_id for v in vs] == ["sv_a", "sv_b"]
    assert vs[0].language == "hi"


def test_401_raises_authentication_error():
    c = _client(lambda req: httpx.Response(401, text="bad key"))
    with pytest.raises(AuthenticationError):
        c.speech.create(input="hi", voice="sv_x")


def test_422_raises_bad_request():
    c = _client(lambda req: httpx.Response(422, json={"detail": "nope"}))
    with pytest.raises(BadRequestError):
        c.speech.create(input="hi", voice="sv_x", response_format="mp3")


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(core, "_backoff", lambda attempt: 0.0)  # no real sleeping
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, content=b"OK")

    c = _client(handler, max_retries=2)
    assert c.speech.create(input="hi", voice="sv_x") == b"OK"
    assert calls["n"] == 2


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(core, "_backoff", lambda attempt: 0.0)
    c = _client(lambda req: httpx.Response(429, text="slow"), max_retries=1)
    with pytest.raises(RateLimitError):
        c.speech.create(input="hi", voice="sv_x")


def test_create_sends_pronunciation_dictionary_id():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"OK")

    c = _client(handler)
    c.speech.create(input="hi", voice="sv_x", pronunciation_dictionary_id="pd_123")
    assert seen["body"]["pronunciation_dictionary_id"] == "pd_123"


def test_retrieve_falls_back_to_catalog_on_404():
    # The live by-id endpoint resolves only custom voices; library ids 404.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/voices/sv_lib":
            return httpx.Response(404, json={"detail": "voice 'sv_lib' not found"})
        return httpx.Response(200, json={"voices": [{"voice_id": "sv_lib", "name": "Lib"}]})

    c = _client(handler)
    assert c.voices.retrieve("sv_lib").name == "Lib"


def test_stream_yields_chunks():
    c = _client(lambda req: httpx.Response(200, content=b"0123456789", headers={"content-type": "audio/pcm"}))
    got = b"".join(c.speech.stream(input="hi", voice="sv_x", response_format="pcm", chunk_size=4))
    assert got == b"0123456789"
