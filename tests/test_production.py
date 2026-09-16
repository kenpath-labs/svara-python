"""Tests for the 0.2.0 production-readiness pass.

Each test names the behaviour it pins. Live measurements behind the defaults
are in MEASUREMENTS.md; these tests check the SDK does what those numbers say
it should, without a network.
"""

from __future__ import annotations

import asyncio
import json
import threading
import warnings

import httpx
import pytest

import svara._client as core
from svara import (
    FLUSH,
    AsyncSvara,
    InternalServerError,
    InvalidRequestError,
    QuotaExceededError,
    RateLimitError,
    SpeechResponse,
    SpeechStream,
    StreamInterruptedError,
    Svara,
    output_format,
)
from svara.exceptions import parse_error_body, raise_for_status


def _client(handler, **kw) -> Svara:
    http = httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://api.kenpathlabs.com")
    return Svara(api_key="sk_test", http_client=http, **kw)


def _async_client(handler, **kw) -> AsyncSvara:
    return AsyncSvara(api_key="k", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), **kw)


# ── keep-alive: the +140 ms per turn ─────────────────────────────────────────

def test_owned_transport_keeps_connections_longer_than_httpx_default():
    """httpx drops idle connections after 5 s; voice-agent turns are further
    apart than that, so every synthesis re-did TCP+TLS: +140 ms measured."""
    c = Svara(api_key="sk_test")
    limits = c._http._transport._pool  # httpcore pool built from our Limits
    assert limits._keepalive_expiry >= 60, "idle connections are still dropped between turns"
    c.close()


def test_async_owned_transport_keeps_connections_too():
    c = AsyncSvara(api_key="sk_test")
    assert c._http._transport._pool._keepalive_expiry >= 60
    asyncio.run(c.aclose())


def test_default_read_timeout_covers_a_maximum_length_create():
    """5,000 characters render in ~50 s non-streaming; 30 s would have fired."""
    assert core.DEFAULT_TIMEOUT.read >= 100
    assert core.DEFAULT_TIMEOUT.connect <= 10


# ── client-side validation: fail before the round trip ───────────────────────

@pytest.mark.parametrize("kw,fragment", [
    (dict(input=""), "empty"),
    (dict(input="x" * 5001), "5000"),
    (dict(input="x", speed=2.0), "speed"),
    (dict(input="x", speed=0.5), "speed"),
    (dict(input="x", sample_rate=11025), "sample_rate"),
    (dict(input="x", response_format="mulaw"), "response_format"),
])
def test_invalid_requests_fail_locally_with_the_argument_named(kw, fragment):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b"OK")

    with pytest.raises(InvalidRequestError, match=fragment):
        _client(handler).speech.create(voice="sv_x", **kw)
    assert calls["n"] == 0, "the request should not have been sent"


def test_invalid_request_is_also_a_value_error():
    with pytest.raises(ValueError):
        _client(lambda r: httpx.Response(200)).speech.create(input="", voice="sv_x")


def test_boundary_values_are_accepted():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="x" * 5000, voice="sv_x", speed=1.5, sample_rate=8000,
                                   response_format="ulaw")
    assert seen["speed"] == 1.5 and seen["sample_rate"] == 8000


# ── error bodies: three shapes, one exception ────────────────────────────────

def test_gateway_error_shape_yields_code_and_message():
    assert parse_error_body('{"detail": {"status": "invalid_api_key", "message": "Revoked."}}') == (
        "invalid_api_key", "Revoked.")


def test_pydantic_validation_shape_names_the_field():
    body = '{"detail":[{"type":"less_than_equal","loc":["body","speed"],"msg":"Input should be <= 1.5"}]}'
    code, msg = parse_error_body(body)
    assert code == "validation_error" and msg == "speed: Input should be <= 1.5"


def test_string_detail_shape():
    assert parse_error_body('{"detail":"voice x not found"}') == (None, "voice x not found")


def test_unparseable_body_falls_back_to_raw_text():
    assert parse_error_body("<html>502</html>") == (None, None)
    with pytest.raises(InternalServerError, match="<html>502</html>"):
        raise_for_status(502, "<html>502</html>")


def test_error_message_is_readable_not_a_json_dump():
    c = _client(lambda r: httpx.Response(401, json={"detail": {
        "status": "invalid_api_key", "message": "API key invalid or revoked."}}))
    with pytest.raises(Exception) as ei:
        c.speech.create(input="hi", voice="sv_x")
    assert str(ei.value) == "Svara API error 401 (invalid_api_key): API key invalid or revoked."
    assert ei.value.code == "invalid_api_key"
    assert "detail" in ei.value.body  # raw body still there for logs


# ── quota: a 429 that must not be retried ────────────────────────────────────

def test_insufficient_quota_is_not_retried(monkeypatch):
    monkeypatch.setattr(core, "_backoff", lambda attempt, retry_after=None: 0.0)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, json={"detail": {"status": "insufficient_quota",
                                                    "message": "Monthly quota spent."}})

    with pytest.raises(QuotaExceededError) as ei:
        _client(handler, max_retries=3).speech.create(input="hi", voice="sv_x")
    assert calls["n"] == 1, "retrying a spent quota is a retry storm"
    assert isinstance(ei.value, RateLimitError), "existing 429 handlers must still catch it"


def test_other_429s_are_still_retried(monkeypatch):
    monkeypatch.setattr(core, "_backoff", lambda attempt, retry_after=None: 0.0)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"detail": {
                "status": "too_many_concurrent_requests", "message": "busy"}})
        return httpx.Response(200, content=b"OK")

    assert _client(handler, max_retries=2).speech.create(input="hi", voice="sv_x") == b"OK"
    assert calls["n"] == 2


def test_5xx_maps_to_internal_server_error():
    with pytest.raises(InternalServerError):
        _client(lambda r: httpx.Response(503, text="down"), max_retries=0).speech.create(
            input="hi", voice="sv_x")


# ── response metadata ────────────────────────────────────────────────────────

def test_create_returns_bytes_with_headers():
    c = _client(lambda r: httpx.Response(200, content=b"AUDIO", headers={
        "content-type": "audio/mpeg", "x-sample-rate": "24000",
        "x-ratelimit-remaining-characters": "1234", "x-ratelimit-remaining-requests": "-1"}))
    out = c.speech.create(input="hi", voice="sv_x")
    assert isinstance(out, bytes) and out == b"AUDIO"           # still just bytes
    assert isinstance(out, SpeechResponse)
    assert out.content_type == "audio/mpeg" and out.sample_rate == 24000
    assert out.rate_limit.characters == 1234 and out.rate_limit.requests == -1
    assert out[:2] == b"AU"                                     # slicing works
    assert "AUDIO" not in repr(out)                             # no payload dumps


def test_create_save_writes_the_file(tmp_path):
    c = _client(lambda r: httpx.Response(200, content=b"AUDIO"))
    p = c.speech.create(input="hi", voice="sv_x").save(str(tmp_path / "a.mp3"))
    assert open(p, "rb").read() == b"AUDIO"


def test_stream_exposes_headers_from_inside_the_loop():
    frames = [b"A" * 1920, b"B" * 3552]
    c = _client(lambda r: httpx.Response(200, stream=httpx.ByteStream(b"".join(frames)),
                                         headers={"content-type": "audio/pcm", "x-sample-rate": "8000"}))
    s = c.speech.stream(input="hi", voice="sv_x")
    assert isinstance(s, SpeechStream)
    assert s.headers == {} and s.time_to_first_audio is None   # lazy: nothing sent yet
    got = []
    for chunk in s:
        assert s.sample_rate == 8000                            # known once audio flows
        got.append(chunk)
    assert b"".join(got) == b"".join(frames)
    assert s.bytes_received == sum(map(len, frames))
    assert s.time_to_first_audio is not None and s.time_to_first_audio >= 0


def test_stream_is_still_a_plain_iterator_for_old_callers():
    c = _client(lambda r: httpx.Response(200, content=b"0123456789"))
    assert b"".join(c.speech.stream(input="hi", voice="sv_x")) == b"0123456789"
    assert list(c.speech.stream(input="hi", voice="sv_x", chunk_size=4)) == [b"0123", b"4567", b"89"]
    assert next(c.speech.stream(input="hi", voice="sv_x", chunk_size=2)) == b"01"


def test_stream_read_drains():
    c = _client(lambda r: httpx.Response(200, content=b"0123456789"))
    s = c.speech.stream(input="hi", voice="sv_x", chunk_size=4)
    assert next(s) == b"0123"
    assert s.read() == b"456789"


def test_stream_context_manager_closes_early():
    c = _client(lambda r: httpx.Response(200, content=b"0123456789"))
    with c.speech.stream(input="hi", voice="sv_x", chunk_size=2) as s:
        assert next(s) == b"01"
    with pytest.raises(StopIteration):
        next(s)


def test_async_stream_exposes_metadata():
    async def go():
        c = _async_client(lambda r: httpx.Response(200, content=b"0123456789",
                                                   headers={"x-sample-rate": "24000"}))
        s = c.speech.stream(input="hi", voice="sv_x", chunk_size=5)
        out = [ch async for ch in s]
        assert out == [b"01234", b"56789"] and s.sample_rate == 24000 and s.bytes_received == 10
        s2 = c.speech.stream(input="hi", voice="sv_x")
        assert await s2.read() == b"0123456789"
    asyncio.run(go())


# ── new request fields ───────────────────────────────────────────────────────

def test_bitrate_and_normalize_reach_the_payload():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x", response_format="mp3",
                                   bitrate_kbps=192, normalize=False, language="hi")
    assert seen["bitrate_kbps"] == 192 and seen["normalize"] is False and seen["lang"] == "hi"


def test_unset_new_fields_stay_absent():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x")
    assert "bitrate_kbps" not in seen and "normalize" not in seen


def test_per_call_timeout_is_forwarded():
    seen = {}

    def handler(req):
        seen["ext"] = dict(req.extensions.get("timeout", {}))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x", timeout=7.0)
    assert seen["ext"]["read"] == 7.0


# ── ElevenLabs-style output formats ──────────────────────────────────────────

@pytest.mark.parametrize("name,expect", [
    ("pcm_24000", {"response_format": "pcm", "sample_rate": 24000}),
    ("ulaw_8000", {"response_format": "ulaw", "sample_rate": 8000}),
    ("mp3_44100_128", {"response_format": "mp3", "sample_rate": 44100, "bitrate_kbps": 128}),
    ("PCM_16000", {"response_format": "pcm", "sample_rate": 16000}),
    ("wav", {"response_format": "wav"}),
])
def test_output_format_translates(name, expect):
    assert output_format(name) == expect


@pytest.mark.parametrize("bad", ["pcm_11025", "wav_24000_128", "foo", "mp3_44100_128_x"])
def test_output_format_rejects_what_the_server_would(bad):
    with pytest.raises(ValueError):
        output_format(bad)


def test_output_format_splats_into_a_call():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x", **output_format("ulaw_8000"))
    assert seen["response_format"] == "ulaw" and seen["sample_rate"] == 8000


# ── catalogue endpoints ──────────────────────────────────────────────────────

def test_voices_list_filters_client_side():
    c = _client(lambda r: httpx.Response(200, json={"voices": [
        {"voice_id": "a", "gender": "female", "curated": True, "labels": {"native_language_code": "hi"}},
        {"voice_id": "b", "gender": "male", "curated": True, "labels": {"native_language_code": "hi"}},
        {"voice_id": "c", "gender": "female", "curated": False, "labels": {"native_language_code": "ta"}},
    ]}))
    assert [v.voice_id for v in c.voices.list(language="hi")] == ["a", "b"]
    assert [v.voice_id for v in c.voices.list(language="HI", gender="female")] == ["a"]
    assert [v.voice_id for v in c.voices.list(curated=False, use_cache=True)] == ["c"]


def test_voice_carries_quality_fields():
    c = _client(lambda r: httpx.Response(200, json={"voices": [
        {"voice_id": "a", "quality_warning": ["noisy"], "hours": 1.5, "labels": {"quality_band": "B"}}]}))
    v = c.voices.list()[0]
    assert v.quality_warning == ["noisy"] and v.hours == 1.5 and v.quality_band == "B"


def test_languages_list_parses():
    c = _client(lambda r: httpx.Response(200, json={"languages": [
        {"iso3": "hin", "iso1": "hi", "name": "Hindi", "region": "indic", "aliases": ["hindi"]}]}))
    ls = c.languages.list()
    assert ls[0].iso1 == "hi" and ls[0].name == "Hindi" and ls[0].aliases == ["hindi"]


def test_usage_get_exposes_the_numbers():
    c = _client(lambda r: httpx.Response(200, json={
        "plan": {"id": "growth", "requests_per_minute": 200, "max_concurrent_streams": 10},
        "month": {"characters_used": 41230},
        "balance": {"characters_remaining": 958770}}))
    u = c.usage.get()
    assert u.plan_id == "growth" and u.characters_used == 41230
    assert u.characters_remaining == 958770 and u.max_concurrent_streams == 10


def test_voice_preview_returns_audio_with_headers():
    c = _client(lambda r: httpx.Response(200, content=b"MP3", headers={"content-type": "audio/mpeg"}))
    p = c.voices.preview("sv_x")
    assert p == b"MP3" and p.content_type == "audio/mpeg"


def test_warm_up_hits_models_and_swallows_nothing():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"data": []})

    _client(handler).warm_up()
    assert seen == ["/v1/models"]


def test_async_catalogue_endpoints():
    async def go():
        c = _async_client(lambda r: httpx.Response(200, json={
            "languages": [{"iso3": "tam", "name": "Tamil"}],
            "plan": {"id": "free"}, "voices": [{"voice_id": "a"}]}))
        assert (await c.languages.list())[0].iso3 == "tam"
        assert (await c.usage.get()).plan_id == "free"
        assert (await c.voices.list())[0].voice_id == "a"
        await c.warm_up()
    asyncio.run(go())


# ── sync stream_input (websockets.sync) ──────────────────────────────────────

def _serve_once(handler):
    """A real local WebSocket server on a free port, in a thread."""
    from websockets.sync.server import serve

    server = serve(handler, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_sync_stream_input_feeds_text_and_yields_audio():
    received = []

    def handler(ws):
        for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            if msg.get("text") == "":
                ws.send(b"\x01" * 32)
                ws.send(json.dumps({"type": "chunk", "text": "hello ", "peek": "world"}))
                ws.send(b"\x02" * 32)
                ws.send(json.dumps({"type": "done"}))
                return

    server, port = _serve_once(handler)
    try:
        c = Svara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        events = []
        out = list(c.speech.stream_input(["hello ", FLUSH, "world"], voice="sv_x", on_event=events.append))
        assert out == [b"\x01" * 32, b"\x02" * 32]
        assert received == [{"text": "hello "}, {"flush": True}, {"text": "world"}, {"text": ""}]
        assert events[0].text == "hello " and events[0].peek == "world"
        c.close()
    finally:
        server.shutdown()


def test_sync_stream_input_reports_truncation():
    def handler(ws):
        ws.recv()
        ws.send(b"\x00" * 64)
        ws.close()  # no 'done'

    server, port = _serve_once(handler)
    try:
        c = Svara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(StreamInterruptedError) as ei:
            list(c.speech.stream_input(["hi "], voice="sv_x"))
        assert ei.value.frames == 1
        c.close()
    finally:
        server.shutdown()


def test_sync_stream_input_surfaces_the_text_sources_own_error():
    def handler(ws):
        for raw in ws:
            if json.loads(raw).get("text") == "":
                ws.send(json.dumps({"type": "done"}))
                return

    server, port = _serve_once(handler)
    try:
        c = Svara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")

        def exploding():
            yield "hi "
            raise RuntimeError("LLM upstream died")

        with pytest.raises(RuntimeError, match="LLM upstream died"):
            list(c.speech.stream_input(exploding(), voice="sv_x"))
        c.close()
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_async_flush_sentinel_is_sent_as_a_flush_message():
    import websockets

    received = []

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            if msg.get("text") == "":
                await ws.send(json.dumps({"type": "done"}))
                return

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        client = AsyncSvara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        async for _ in client.speech.stream_input(["a ", FLUSH, "b"], voice="sv_x"):
            pass
        await client.aclose()
    assert received == [{"text": "a "}, {"flush": True}, {"text": "b"}, {"text": ""}]


# ── telephony warning points at the caller ───────────────────────────────────

def test_ulaw_warning_is_attributed_to_the_calling_line():
    c = _client(lambda r: httpx.Response(200, content=b"OK"))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c.speech.create(input="hi", voice="sv_x", response_format="ulaw")  # <- this line
    assert w and w[0].filename == __file__


# ── WebSocket error events ───────────────────────────────────────────────────
# The server answers an unknown voice with {"type": "error", "message": ...}
# and closes 1008. Without handling, that surfaced as "closed after 0 frames
# without done" — true, and useless.

@pytest.mark.asyncio
async def test_ws_error_event_raises_the_named_error_not_interrupted():
    import websockets

    from svara import NotFoundError

    async def server(ws):
        await ws.send(json.dumps({"type": "error", "message": "voice 'sv_nope' not found."}))
        await ws.close(code=1008)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        client = AsyncSvara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(NotFoundError, match="sv_nope"):
            async for _ in client.speech.stream_input(["hi "], voice="sv_nope"):
                pass
        await client.aclose()


def test_sync_ws_error_event_raises_the_named_error():
    from svara import NotFoundError

    def handler(ws):
        ws.send(json.dumps({"type": "error", "message": "voice 'sv_nope' not found."}))
        ws.close(code=1008)

    server, port = _serve_once(handler)
    try:
        c = Svara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(NotFoundError, match="sv_nope"):
            list(c.speech.stream_input(["hi "], voice="sv_nope"))
        c.close()
    finally:
        server.shutdown()


def test_interrupted_message_explains_the_close_code():
    def handler(ws):
        ws.recv()
        ws.close(code=1013)

    server, port = _serve_once(handler)
    try:
        c = Svara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(StreamInterruptedError, match="not ready"):
            list(c.speech.stream_input(["hi "], voice="sv_x"))
        c.close()
    finally:
        server.shutdown()


def test_normalize_reaches_the_ws_query():
    from svara._client import _ws_params, _ws_url
    url = _ws_url("https://x", _ws_params(voice="v", language="hi", normalize=False))
    assert "normalize=false" in url and "lang=hi" in url and "language" not in url


# ── timestamps ───────────────────────────────────────────────────────────────

def test_create_with_timestamps_speaks_the_el_dialect_and_parses():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["query"] = dict(req.url.params)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "audio_base64": "AAECAw==",
            "alignment": {"characters": ["h", "i", " ", "y", "o"],
                          "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4],
                          "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5]}})

    r = _client(handler).speech.create_with_timestamps(
        input="hi yo", voice="sv_x", response_format="mp3", sample_rate=44100, bitrate_kbps=128,
        speed=1.2, language="hi", pronunciation_dictionary_id="pd_1", normalize=False)
    assert seen["path"] == "/v1/text-to-speech/sv_x/with-timestamps"
    assert seen["query"] == {"output_format": "mp3_44100_128"}
    assert seen["body"]["text"] == "hi yo" and seen["body"]["language_code"] == "hi"
    assert seen["body"]["voice_settings"] == {"speed": 1.2}
    assert seen["body"]["apply_text_normalization"] == "off"
    assert seen["body"]["pronunciation_dictionary_locators"] == [{"pronunciation_dictionary_id": "pd_1"}]
    assert r.audio == b"\x00\x01\x02\x03"
    assert r.alignment.text == "hi yo" and r.alignment.duration == 0.5
    assert r.alignment.words() == [("hi", 0.0, 0.2), ("yo", 0.3, 0.5)]


def test_stream_with_timestamps_parses_ndjson():
    lines = [json.dumps({"audio_base64": "AAA=", "alignment": {
        "characters": ["a"], "character_start_times_seconds": [0.0], "character_end_times_seconds": [0.1]}}),
        json.dumps({"audio_base64": "AAA=", "alignment": None})]
    c = _client(lambda r: httpx.Response(200, content=("\n".join(lines) + "\n").encode()))
    out = list(c.speech.stream_with_timestamps(input="a", voice="sv_x"))
    assert len(out) == 2 and out[0].alignment.characters == ["a"] and out[1].alignment.characters == []
    assert out[0].audio == b"\x00\x00"


def test_timestamps_default_rate_is_spelled_out_for_the_el_route():
    seen = {}

    def handler(req):
        seen["query"] = dict(req.url.params)
        return httpx.Response(200, json={"audio_base64": "", "alignment": None})

    _client(handler).speech.create_with_timestamps(input="x", voice="sv_x", response_format="pcm")
    assert seen["query"] == {"output_format": "pcm_24000"}


def test_async_timestamps():
    async def go():
        c = _async_client(lambda r: httpx.Response(200, json={"audio_base64": "AQ==", "alignment": None}))
        r = await c.speech.create_with_timestamps(input="x", voice="sv_x")
        assert r.audio == b"\x01"
        lines = json.dumps({"audio_base64": "AQ==", "alignment": None}) + "\n"
        c2 = _async_client(lambda r: httpx.Response(200, content=lines.encode()))
        out = [t async for t in c2.speech.stream_with_timestamps(input="x", voice="sv_x")]
        assert len(out) == 1
    asyncio.run(go())


def test_ws_rejects_32000_which_the_socket_does_not_serve():
    """The socket's allow-list lacks 32000 (server.py); HTTP has it."""
    c = Svara(api_key="sk_test", base_url="http://127.0.0.1:1")
    with pytest.raises(InvalidRequestError, match="32000"):
        next(iter(c.speech.stream_input(["hi"], voice="v", sample_rate=32000)))

    async def go():
        a = AsyncSvara(api_key="sk_test", base_url="http://127.0.0.1:1")
        with pytest.raises(InvalidRequestError, match="32000"):
            await a.speech.prepare(voice="v", sample_rate=32000)
    asyncio.run(go())


def test_dictionary_miss_is_warned_not_silent():
    """A typo'd dictionary id is not an error server-side: the global rules
    apply and only the x-svara-dictionary header says so."""
    c = _client(lambda r: httpx.Response(200, content=b"OK", headers={"x-svara-dictionary": "miss"}))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c.speech.create(input="hi", voice="sv_x", pronunciation_dictionary_id="pd_typo")
        list(c.speech.stream(input="hi", voice="sv_x", pronunciation_dictionary_id="pd_typo"))
    assert len(w) == 2 and all("pd_typo" in str(x.message) for x in w)


def test_dictionary_hit_or_no_dictionary_does_not_warn():
    c = _client(lambda r: httpx.Response(200, content=b"OK", headers={"x-svara-dictionary": "miss"}))
    c2 = _client(lambda r: httpx.Response(200, content=b"OK", headers={"x-svara-dictionary": "fbdb2572"}))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c.speech.create(input="hi", voice="sv_x")                                   # none sent
        c2.speech.create(input="hi", voice="sv_x", pronunciation_dictionary_id="fbdb2572")
    assert not w


def test_cli_parser_knows_every_command():
    from svara._cli import build_parser
    p = build_parser()
    for argv in (["say", "x", "-v", "sv_x", "-r", "8000"], ["voices", "-l", "hi", "-g", "female"],
                 ["languages", "--json"], ["usage"], ["doctor", "-v", "sv_x"]):
        assert p.parse_args(argv).func


# ── request ids ──────────────────────────────────────────────────────────────
# The gateway mints no x-request-id on the speech path, so the client sends
# one. It is the handle a support ticket quotes.

def test_every_request_carries_a_fresh_request_id():
    seen = []

    def handler(req):
        seen.append(req.headers.get("x-request-id"))
        return httpx.Response(200, content=b"OK")

    c = _client(handler)
    a = c.speech.create(input="hi", voice="sv_x")
    s = c.speech.stream(input="hi", voice="sv_x")
    s.read()
    assert all(seen) and len(set(seen)) == 2
    assert a.request_id == seen[0] and s.request_id == seen[1]


def test_server_request_id_wins_when_present():
    c = _client(lambda r: httpx.Response(200, content=b"OK", headers={"x-request-id": "srv-1"}))
    assert c.speech.create(input="hi", voice="sv_x").request_id == "srv-1"


def test_errors_carry_the_request_id():
    from svara import AuthenticationError

    sent = {}

    def handler(req):
        sent["id"] = req.headers["x-request-id"]
        return httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "no"}})

    with pytest.raises(AuthenticationError) as ei:
        _client(handler).speech.create(input="hi", voice="sv_x")
    assert ei.value.request_id == sent["id"]
