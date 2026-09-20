"""Parity with the OpenAI and ElevenLabs SDKs: code written for either should
port by changing the client, and nothing the API serves should be unreachable."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import svara._client as core
from svara import (
    AsyncSvara,
    BadRequestError,
    ConflictError,
    PermissionDeniedError,
    PermissionError_,
    Svara,
    UnprocessableEntityError,
)


def _client(handler, **kw) -> Svara:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Svara(api_key="sk_test", http_client=http, **kw)


def test_default_model_is_the_one_the_api_serves():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x")
    assert seen["model"] == "svara-tts-turbo" == core.DEFAULT_MODEL


def test_openai_shaped_code_ports_unchanged():
    """client.audio.speech.create(model, voice, input) + write_to_file, and
    with_streaming_response.create(...).iter_bytes()."""
    c = _client(lambda r: httpx.Response(200, content=b"0123456789"))
    audio = c.audio.speech.create(model="svara-tts-turbo", voice="sv_x", input="hi", response_format="mp3")
    assert audio.content == b"0123456789" and audio.read() == b"0123456789"
    assert list(audio.iter_bytes(4)) == [b"0123", b"4567", b"89"]
    with c.audio.speech.with_streaming_response.create(
        model="svara-tts-turbo", voice="sv_x", input="hi", response_format="pcm",
    ) as response:
        assert b"".join(response.iter_bytes()) == b"0123456789"


def test_openai_write_to_file_names(tmp_path):
    c = _client(lambda r: httpx.Response(200, content=b"AUDIO"))
    p = str(tmp_path / "a.mp3")
    c.speech.create(input="hi", voice="sv_x").write_to_file(p)
    assert open(p, "rb").read() == b"AUDIO"
    q = str(tmp_path / "b.pcm")
    c.speech.stream(input="hi", voice="sv_x").stream_to_file(q)
    assert open(q, "rb").read() == b"AUDIO"


def test_extra_headers_and_query_reach_the_wire():
    seen = {}

    def handler(req):
        seen["h"] = req.headers.get("x-trace"), req.headers.get("x-org")
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, content=b"OK")

    c = _client(handler, default_headers={"x-org": "acme"})
    c.speech.create(input="hi", voice="sv_x", extra_headers={"x-trace": "t1"}, extra_query={"debug": "1"})
    assert seen["h"] == ("t1", "acme") and seen["q"] == {"debug": "1"}
    c.speech.stream(input="hi", voice="sv_x", extra_headers={"x-trace": "t2"}).read()
    assert seen["h"] == ("t2", "acme")


def test_with_options_shares_the_pool_and_overrides_per_call(monkeypatch):
    monkeypatch.setattr(core, "_backoff", lambda attempt, retry_after=None: 0.0)
    calls = {"n": 0, "timeout": None}

    def handler(req):
        calls["n"] += 1
        calls["timeout"] = dict(req.extensions.get("timeout", {})).get("read")
        return httpx.Response(503, text="down")

    c = _client(handler, max_retries=2)
    fast = c.with_options(max_retries=0, timeout=3.0)
    assert fast._http is c._http
    with pytest.raises(Exception):
        fast.speech.create(input="hi", voice="sv_x")
    assert calls["n"] == 1 and calls["timeout"] == 3.0
    fast.close()
    assert not c._http.is_closed, "closing a with_options() copy must not close the shared pool"


def test_models_list_handles_both_server_shapes():
    openai_shape = {"object": "list", "data": [{"id": "svara-tts-turbo", "object": "model"}]}
    el_shape = [{"model_id": "svara-tts-turbo", "name": "Svara TTS Turbo",
                 "maximum_text_length_per_request": 5000}]
    assert _client(lambda r: httpx.Response(200, json=openai_shape)).models.list()[0].id == "svara-tts-turbo"
    m = _client(lambda r: httpx.Response(200, json=el_shape)).models.list()[0]
    assert m.id == "svara-tts-turbo" and m.name == "Svara TTS Turbo" and m.max_characters == 5000


def test_voice_search_is_client_side_over_v1():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return httpx.Response(200, json={"voices": [
            {"voice_id": "sv_a", "name": "Aanya", "gender": "female", "accent_family": "bengali",
             "labels": {"native_language": "Bengali", "native_language_code": "bn", "tags": "soft"}},
            {"voice_id": "sv_b", "name": "Kabir", "gender": "male", "accent_family": "hindi",
             "labels": {"native_language": "Hindi", "native_language_code": "hi"}}]})

    c = _client(handler)
    assert [v.voice_id for v in c.voices.search("bengali soft")] == ["sv_a"]
    assert [v.voice_id for v in c.voices.search("KABIR")] == ["sv_b"]
    assert [v.voice_id for v in c.voices.search("", gender="male")] == ["sv_b"]
    assert set(paths) == {"/v1/voices"} and len(paths) == 1, "must not touch /v2/voices; must cache"


def test_error_class_names_match_the_other_sdks():
    from svara.exceptions import raise_for_status

    assert PermissionDeniedError is PermissionError_
    with pytest.raises(UnprocessableEntityError) as ei:
        raise_for_status(422, '{"detail":[{"loc":["body","speed"],"msg":"bad"}]}')
    assert isinstance(ei.value, BadRequestError)          # 0.1 handlers still catch it
    with pytest.raises(ConflictError):
        raise_for_status(409, '{"detail":"a dictionary named x already exists"}')


def test_async_parity():
    async def go():
        c = AsyncSvara(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"0123", json=None) if r.url.path != "/v1/models"
            else httpx.Response(200, json=[{"model_id": "svara-tts-turbo"}]))))
        a = await c.audio.speech.create(model="svara-tts-turbo", voice="v", input="hi")
        assert a.content == b"0123"
        async with c.audio.speech.with_streaming_response.create(voice="v", input="hi") as r:
            assert b"".join([ch async for ch in r.iter_bytes()]) == b"0123"
        assert (await c.models.list())[0].id == "svara-tts-turbo"
        assert c.with_options(max_retries=0)._http is c._http
    asyncio.run(go())


def test_play_explains_itself_without_ffplay(monkeypatch):
    import svara._play as p
    monkeypatch.setattr(p.shutil, "which", lambda name: None)
    with pytest.raises(Exception, match="ffplay"):
        p.play(b"abc")
