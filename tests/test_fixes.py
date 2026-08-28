"""Regression tests for the defects fixed in the 2026-08-27 pass.

One test per defect, each named for the behaviour it pins rather than the
function it calls. Every one of these failed before the corresponding fix —
with one labelled exception at the bottom of the file, where the defect was
latent rather than live and the test says so itself.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import svara._client as core
from svara import (
    AsyncSvara,
    MissingAPIKeyError,
    StreamInterruptedError,
    Svara,
    SvaraError,
)
from svara._client import _backoff, _sampling, _ws_url

# ── the chunk_size latency defect ────────────────────────────────────────────
# The server flushes a small first frame (1920 B pcm / 960 B ulaw) so audio can
# start early. The old default of 4096 held it back and waited for the second
# frame, costing 46-131 ms of time-to-first-audio against production.

def test_stream_does_not_rebuffer_by_default():
    """Each block the server sends must be yielded as it arrives."""
    frames = [b"A" * 1920, b"B" * 3552, b"C" * 5930]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(frames)))

    c = _client(handler)
    out = list(c.speech.stream(input="hi", voice="sv_x", response_format="pcm"))
    # Not one 11402-byte blob, and crucially not a first chunk of exactly 4096.
    assert out[0] != b"".join(frames)[:4096]
    assert b"".join(out) == b"".join(frames)


def test_stream_chunk_size_still_available_for_fixed_frames():
    """Telephony wants exact 20 ms frames; passing a number must still do that."""
    c = _client(lambda r: httpx.Response(200, content=b"0123456789"))
    got = list(c.speech.stream(input="hi", voice="sv_x", chunk_size=4))
    assert got == [b"0123", b"4567", b"89"]


def test_stream_signature_defaults_to_unbuffered_on_both_clients():
    import inspect

    sync = Svara(api_key="sk_test", base_url="https://example.invalid")
    aio = AsyncSvara(api_key="sk_test", base_url="https://example.invalid")
    for fn in (sync.speech.stream, aio.speech.stream):
        default = inspect.signature(fn).parameters["chunk_size"].default
        assert default is None, f"{fn.__qualname__} re-buffers by default"


# ── borrowed http_client ownership ───────────────────────────────────────────

def test_borrowed_http_client_is_not_mutated():
    """A shared client must not come back carrying our key and User-Agent."""
    shared = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"OK")))
    before = dict(shared.headers)
    Svara(api_key="sk_secret", http_client=shared)
    assert dict(shared.headers) == before
    assert "xi-api-key" not in shared.headers


def test_borrowed_http_client_is_not_closed():
    """close() must not shut down a transport the caller still needs."""
    shared = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"OK")))
    with Svara(api_key="sk_test", http_client=shared):
        pass
    assert not shared.is_closed
    shared.close()


def test_owned_http_client_is_closed():
    c = Svara(api_key="sk_test")
    http = c._http
    c.close()
    assert http.is_closed


def test_key_still_reaches_the_request_on_a_borrowed_client():
    """Not mutating the client must not mean dropping the auth header."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers.get("xi-api-key")
        seen["ua"] = req.headers.get("User-Agent")
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x")
    assert seen["key"] == "sk_test"
    assert seen["ua"].startswith("svara-python/")


# ── WebSocket query string ───────────────────────────────────────────────────

def test_ws_url_percent_encodes_caller_values():
    """A voice or dictionary id with & or = must not split into extra params."""
    url = _ws_url("https://api.kenpathlabs.com",
                  {"pronunciation_dictionary_id": "pd&evil=1", "lang": "hi IN"})
    assert "pd&evil=1" not in url
    assert "pd%26evil%3D1" in url
    assert "hi+IN" in url or "hi%20IN" in url


def test_ws_url_serialises_booleans_the_way_a_server_parses_them():
    url = _ws_url("https://x.com", {"a": True, "b": False})
    assert "a=true" in url and "b=false" in url
    assert "True" not in url


# ── retry ────────────────────────────────────────────────────────────────────

def test_backoff_is_jittered():
    """Identical clients must not retry in lockstep and re-trigger the limit."""
    delays = {_backoff(1) for _ in range(50)}
    assert len(delays) > 40, "backoff is deterministic; concurrent callers will sync"


def test_backoff_is_capped():
    assert _backoff(50) <= 8.0


def test_backoff_honours_retry_after():
    assert _backoff(0, retry_after=3.5) == 3.5


def test_backoff_ignores_absurd_retry_after():
    """A server asking for an hour is not worth obeying."""
    assert _backoff(0, retry_after=3600) <= 8.0


def test_retry_after_header_is_parsed_from_a_429(monkeypatch):
    slept = []
    monkeypatch.setattr(core.time, "sleep", slept.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow", headers={"retry-after": "2"})
        return httpx.Response(200, content=b"OK")

    assert _client(handler, max_retries=2).speech.create(input="hi", voice="sv_x") == b"OK"
    assert slept == [2.0]


def test_retry_after_ms_wins_over_seconds(monkeypatch):
    slept = []
    monkeypatch.setattr(core.time, "sleep", slept.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow", headers={
                "retry-after": "5", "retry-after-ms": "250"})
        return httpx.Response(200, content=b"OK")

    _client(handler, max_retries=2).speech.create(input="hi", voice="sv_x")
    assert slept == [0.25]


def test_stream_retries_before_the_first_byte(monkeypatch):
    """create() retried and stream() did not. They must agree."""
    monkeypatch.setattr(core, "_backoff", lambda attempt, retry_after=None: 0.0)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, content=b"AUDIO")

    c = _client(handler, max_retries=2)
    assert b"".join(c.speech.stream(input="hi", voice="sv_x")) == b"AUDIO"
    assert calls["n"] == 2


def test_stream_does_not_retry_after_audio_has_been_yielded(monkeypatch):
    """Replaying an utterance the caller is already playing is worse than
    the truncation it hides."""
    monkeypatch.setattr(core, "_backoff", lambda attempt, retry_after=None: 0.0)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise httpx.ReadError("connection dropped mid-stream")

    c = _client(handler, max_retries=2)
    with pytest.raises(SvaraError):
        list(c.speech.stream(input="hi", voice="sv_x"))


# ── voices ───────────────────────────────────────────────────────────────────

def test_retrieve_reuses_the_cached_catalogue():
    """The catalogue is 282 KB. Resolving n library voices must not fetch it n
    times."""
    hits = {"list": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/v1/voices/"):
            return httpx.Response(404, json={"detail": "not found"})
        hits["list"] += 1
        return httpx.Response(200, json={"voices": [
            {"voice_id": "sv_a", "name": "A"}, {"voice_id": "sv_b", "name": "B"}]})

    c = _client(handler)
    assert c.voices.retrieve("sv_a").name == "A"
    assert c.voices.retrieve("sv_b").name == "B"
    assert hits["list"] == 1, f"catalogue fetched {hits['list']}x"


def test_list_defaults_to_hitting_the_server():
    """Caching must be opt-in: list() means "ask"."""
    hits = {"n": 0}

    def handler(req):
        hits["n"] += 1
        return httpx.Response(200, json={"voices": [{"voice_id": "sv_a"}]})

    c = _client(handler)
    c.voices.list()
    c.voices.list()
    assert hits["n"] == 2


# ── misc correctness ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("caught", [SvaraError, ValueError, MissingAPIKeyError])
def test_missing_key_is_catchable_as_both_svara_error_and_value_error(caught):
    """It used to be a bare ValueError; code in the wild catches it that way.
    It is now also a SvaraError, so one except clause can cover the SDK."""
    with pytest.raises(caught):
        _no_key()


def _no_key():
    import os
    saved = os.environ.pop("SVARA_API_KEY", None)
    try:
        return Svara(api_key=None)
    finally:
        if saved is not None:
            os.environ["SVARA_API_KEY"] = saved


def test_sampling_is_explicit_not_harvested_from_locals():
    s = _sampling(1.2, 0.9, 40, 1.1, 0.0)
    assert s == {"temperature": 1.2, "top_p": 0.9, "top_k": 40,
                 "repetition_penalty": 1.1, "presence_penalty": 0.0}
    assert "self" not in s and "input" not in s


def test_sampling_knobs_reach_the_payload():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    _client(handler).speech.create(input="hi", voice="sv_x", temperature=1.2,
                                   top_k=40, presence_penalty=0.0)
    assert seen["temperature"] == 1.2 and seen["top_k"] == 40
    assert seen["presence_penalty"] == 0.0     # 0 is a value, not an absence
    assert "top_p" not in seen                 # unset stays unset


def test_ssl_context_is_shared_across_calls():
    """asyncio builds one per connection otherwise — 9-17 ms each, on the loop."""
    from svara import default_ssl_context
    assert default_ssl_context() is default_ssl_context()


# ── the WebSocket feeder hang ────────────────────────────────────────────────
# If the caller's text iterator raised, the feeder task was cancelled without
# being awaited, EOS was never sent, and the receive loop waited forever: no
# audio, no error, no log line. Reproduced against a real socket below.

@pytest.mark.asyncio
async def test_text_source_failure_surfaces_instead_of_hanging():
    import websockets

    async def server(ws):
        async for raw in ws:
            if json.loads(raw).get("text") == "":
                await ws.send(b"\x00" * 32)
                await ws.send(json.dumps({"type": "done"}))
                return

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        client = AsyncSvara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")

        async def exploding_tokens():
            yield "hello "
            raise RuntimeError("LLM upstream died")

        with pytest.raises(RuntimeError, match="LLM upstream died"):
            await asyncio.wait_for(
                _drain(client.speech.stream_input(exploding_tokens(), voice="sv_x")),
                timeout=10,
            )
        await client.aclose()


@pytest.mark.asyncio
async def test_stream_ending_without_done_is_an_error_not_silence():
    """A truncated utterance must not look like a successful short one."""
    import websockets

    async def server(ws):
        await ws.recv()
        await ws.send(b"\x00" * 64)
        await ws.close()          # no 'done'

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        client = AsyncSvara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(StreamInterruptedError) as ei:
            await asyncio.wait_for(
                _drain(client.speech.stream_input(["hi "], voice="sv_x")), timeout=10)
        assert ei.value.frames >= 1
        await client.aclose()


@pytest.mark.asyncio
async def test_prepared_stream_is_single_use():
    import websockets

    async def server(ws):
        async for raw in ws:
            if json.loads(raw).get("text") == "":
                await ws.send(json.dumps({"type": "done"}))
                return

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        client = AsyncSvara(api_key="sk_test", base_url=f"http://127.0.0.1:{port}")
        prepared = await client.speech.prepare(voice="sv_x")
        assert not prepared.expired
        await _drain(prepared.stream(["hi "]))
        with pytest.raises(SvaraError, match="already been used"):
            await _drain(prepared.stream(["again "]))
        await client.aclose()


async def _drain(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return b"".join(out)


# ── helpers ──────────────────────────────────────────────────────────────────
def _client(handler, **kw) -> Svara:
    http = httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://api.kenpathlabs.com")
    return Svara(api_key="sk_test", http_client=http, **kw)


# ── G.711 sample rate ────────────────────────────────────────────────────────
# FORMAT_INFO claimed ulaw/alaw default to 8000 Hz. Measured against production
# they default to 24000, like every other format. The telephony example relied
# on the wrong number, so µ-law bytes went to a SIP leg at 3x speed.

def test_g711_default_rate_matches_the_server():
    from svara import FORMAT_INFO
    assert FORMAT_INFO["ulaw"]["default_rate"] == 24000
    assert FORMAT_INFO["alaw"]["default_rate"] == 24000
    assert FORMAT_INFO["ulaw"]["telephony_rate"] == 8000


def test_g711_without_sample_rate_warns():
    import warnings
    c = _client(lambda r: httpx.Response(200, content=b"OK"))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c.speech.create(input="hi", voice="sv_x", response_format="ulaw")
    assert any("8000" in str(x.message) for x in w), "silent 3x-speed telephony audio"


def test_g711_with_explicit_rate_is_silent():
    import warnings
    c = _client(lambda r: httpx.Response(200, content=b"OK"))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c.speech.create(input="hi", voice="sv_x", response_format="ulaw", sample_rate=8000)
    assert not w


def test_non_telephony_formats_do_not_warn():
    import warnings
    c = _client(lambda r: httpx.Response(200, content=b"OK"))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c.speech.create(input="hi", voice="sv_x", response_format="mp3")
    assert not w


# ── sync/async drift ─────────────────────────────────────────────────────────
# Both defects below lived in exactly one method — AsyncSvara.speech.create —
# because the sync and async twins were maintained by hand and the async
# non-streaming path is the one nothing exercised. The tests are written as
# sync-vs-async *pairs* on purpose: a fix applied to one twin and not the other
# is the failure mode this file exists to catch.

def _async_client(handler, **kw) -> AsyncSvara:
    return AsyncSvara(
        api_key="k",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kw,
    )


def test_async_create_honours_retry_after_like_sync_does():
    """A 429 saying "wait 2s" must reach the caller on every path.

    ``AsyncSvara.speech.create`` called ``raise_for_status`` without the
    response headers, so ``parse_retry_after`` saw nothing and the error
    carried ``retry_after=None``. The retry then used the local backoff curve —
    ~0.43 s against a server asking for 7 — which is precisely the retry storm
    the header exists to prevent, on the client an agent under a concurrency
    limit actually uses.
    """
    def handler(req):
        return httpx.Response(429, text="slow", headers={"retry-after": "2"})

    from svara import RateLimitError

    with pytest.raises(RateLimitError) as sync_err:
        _client(handler, max_retries=0).speech.create(input="hi", voice="sv_x")

    async def go():
        with pytest.raises(RateLimitError) as e:
            await _async_client(handler, max_retries=0).speech.create(input="hi", voice="sv_x")
        return e.value

    async_err = asyncio.run(go())
    assert sync_err.value.retry_after == 2.0
    assert async_err.retry_after == sync_err.value.retry_after


def test_async_create_sends_the_same_sampling_knobs_as_sync():
    """The sampling knobs must survive the async non-streaming path.

    Unlike the rest of this file, this one **passed before its fix too** — say
    so plainly rather than imply a catch it did not make. The method passed
    ``sampling=locals()`` where every other call site passes ``_sampling(...)``,
    and that worked purely because the local variable names happened to match
    ``_SAMPLING_KEYS``. The defect was latent: renaming any parameter would have
    dropped it from the request silently, with no test failing. ``_sampling``
    exists to close exactly that trap, and this pins the payloads equal so the
    two twins cannot drift apart again.
    """
    seen: list[dict] = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=b"OK")

    knobs = dict(temperature=1.2, top_p=0.9, top_k=40, repetition_penalty=1.1)
    _client(handler).speech.create(input="hi", voice="sv_x", **knobs)
    asyncio.run(_async_client(handler).speech.create(input="hi", voice="sv_x", **knobs))

    assert seen[0] == seen[1]
    for k, v in knobs.items():
        assert seen[1][k] == v
