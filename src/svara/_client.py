"""Svara API clients — synchronous (:class:`Svara`) and async (:class:`AsyncSvara`).

Thin, well-typed wrapper over the public speech API
(https://docs.kenpathlabs.com). Mirrors the real endpoints exactly:

* ``POST /v1/audio/speech``                — synth to bytes, or stream chunks
* ``wss …/v1/audio/speech/stream-input``   — eager input-streaming (async only)
* ``GET  /v1/voices``                      — list voices
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import ssl
import threading
import time
import urllib.parse
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Union,
)

import httpx

from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    MissingAPIKeyError,
    RateLimitError,
    StreamInterruptedError,
    SvaraError,
    raise_for_status,
)
from .types import FORMAT_INFO, TELEPHONY_FORMATS, ChunkEvent, ResponseFormat, Voice

DEFAULT_BASE_URL = "https://api.kenpathlabs.com"
DEFAULT_MODEL = "svara-1"
DEFAULT_MAX_RETRIES = 2
_USER_AGENT = f"svara-python/{__version__}"

#: Read timeout, and a much shorter connect timeout. One flat number for both
#: means a host that is simply unreachable ties the caller up for the whole
#: read budget before failing — a connect either happens quickly or is not
#: going to happen. Same split the OpenAI SDK uses.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

#: Backoff curve. Capped, because an unbounded 0.5·2ⁿ reaches minutes by the
#: time a caller has raised max_retries a couple of notches.
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 8.0


def _should_retry(exc: SvaraError) -> bool:
    """Transient errors worth retrying: connection blips, 429, and 5xx."""
    if isinstance(exc, (APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code and exc.status_code >= 500:
        return True
    return False


def _backoff(attempt: int, retry_after: Optional[float] = None) -> float:
    """Seconds to wait before attempt ``attempt`` (0-based).

    A ``Retry-After`` from the server wins: it knows when capacity frees up and
    the curve here does not.

    Otherwise exponential with full jitter down to 75%. The jitter is the point
    — without it, every client that got rate-limited by the same burst retries
    at the same instant and rate-limits itself again. With concurrent streams
    from one process, that self-synchronisation is easy to hit.
    """
    if retry_after is not None and 0 < retry_after <= 120.0:
        return retry_after
    delay = min(_INITIAL_BACKOFF * (2 ** attempt), _MAX_BACKOFF)
    return delay * (1 - 0.25 * random.random())


def _retry_sync(fn, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except SvaraError as e:
            if attempt < max_retries and _should_retry(e):
                time.sleep(_backoff(attempt, getattr(e, "retry_after", None)))
                continue
            raise
    raise AssertionError("unreachable: max_retries must be >= 0")


async def _retry_async(fn, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except SvaraError as e:
            if attempt < max_retries and _should_retry(e):
                await asyncio.sleep(_backoff(attempt, getattr(e, "retry_after", None)))
                continue
            raise
    raise AssertionError("unreachable: max_retries must be >= 0")


# ─────────────────────────────────────────────────────────────────────────────
# Shared TLS context for the WebSocket path
# ─────────────────────────────────────────────────────────────────────────────
_ssl_context: Optional[ssl.SSLContext] = None
_ssl_context_lock = threading.Lock()


def _build_ssl_context() -> ssl.SSLContext:
    """Trust certifi where it is available, matching what httpx does.

    The HTTP path goes through httpx (certifi) and the WebSocket path through
    ``websockets`` (OS trust store). On a host whose OS store carries an expired
    root, that split shows up as HTTP synthesis working while the WebSocket
    fails with an opaque CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def default_ssl_context() -> ssl.SSLContext:
    """The process-wide client TLS context, built once.

    ``websockets`` passes ``ssl=True``, and asyncio then calls
    :func:`ssl.create_default_context` per connection — synchronously, on the
    event loop, so concurrent connects queue behind each other's context
    builds. Measured against production this is worth **9–17 ms per connect**
    (see ``MEASUREMENTS.md``); the build itself is 2.5 ms warm, 5–7 ms cold.

    It does *not* enable TLS session resumption — OpenSSL's client-side session
    cache is off by default and asyncio never passes ``session=``. Measured:
    ``session_reused`` is False either way. Sharing the context is worth the
    9–17 ms and nothing more; claims beyond that do not survive a check.
    """
    global _ssl_context
    ctx = _ssl_context
    if ctx is None:
        with _ssl_context_lock:
            if _ssl_context is None:
                _ssl_context = _build_ssl_context()
            ctx = _ssl_context
    return ctx

# Optional sampling knobs. Omitted from the request unless the caller sets them,
# so the SDK inherits the server's certified defaults rather than pinning its own.
_SAMPLING_KEYS = ("temperature", "top_p", "top_k", "repetition_penalty", "presence_penalty")


def _sampling(
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    repetition_penalty: Optional[float],
    presence_penalty: Optional[float],
) -> Dict[str, Any]:
    """Collect the sampling knobs a caller actually set.

    Spelled out rather than harvested from ``locals()``. The old version swept
    up every local in scope — ``self``, the text, the voice — and relied on
    ``_SAMPLING_KEYS`` to filter them back out, so renaming a parameter
    anywhere silently dropped it from the request instead of failing.
    """
    return {
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "repetition_penalty": repetition_penalty,
        "presence_penalty": presence_penalty,
    }


def _resolve_key(api_key: Optional[str]) -> str:
    key = api_key or os.environ.get("SVARA_API_KEY")
    if not key:
        raise MissingAPIKeyError(
            "No API key. Pass api_key=... or set the SVARA_API_KEY environment variable."
        )
    return key


def _headers(api_key: str) -> Dict[str, str]:
    # The API accepts either `xi-api-key` (ElevenLabs-style) or `Authorization:
    # Bearer`. We use xi-api-key; both are equivalent.
    return {"xi-api-key": api_key, "User-Agent": _USER_AGENT}


def _warn_telephony_rate(response_format: str, sample_rate: Optional[int]) -> None:
    """Warn when a G.711 format is requested without naming a rate.

    The server's default is 24 kHz for every format, µ-law and A-law included.
    That is defensible — it is the model's native rate — but it is the opposite
    of what "ulaw" implies to anyone wiring up telephony, where G.711 means
    8 kHz. Bytes fed to a SIP leg at the wrong clock play at three times speed,
    and the symptom (a chipmunk voice) points at the voice, not at the rate.

    Warn rather than silently substituting 8000: quietly rewriting a caller's
    request is how an SDK becomes impossible to reason about.
    """
    if response_format in TELEPHONY_FORMATS and sample_rate is None:
        import warnings

        warnings.warn(
            f"response_format={response_format!r} without sample_rate returns "
            f"{FORMAT_INFO[response_format]['default_rate']} Hz, not 8000 Hz. "
            f"Telephony (SIP/PSTN) expects 8000 — pass sample_rate=8000 unless "
            f"you specifically want {FORMAT_INFO[response_format]['default_rate']} Hz.",
            stacklevel=3,
        )


def _speech_payload(
    *,
    input: str,
    voice: str,
    model: str,
    response_format: ResponseFormat,
    stream: bool,
    sample_rate: Optional[int],
    speed: Optional[float],
    language: Optional[str],
    sampling: Dict[str, Any],
    extra: Optional[Dict[str, Any]],
    pronunciation_dictionary_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": input,
        "response_format": response_format,
        "stream": stream,
    }
    if sample_rate is not None:
        payload["sample_rate"] = sample_rate
    if speed is not None:
        payload["speed"] = speed
    if language is not None:
        payload["lang"] = language  # the API field is `lang`
    if pronunciation_dictionary_id is not None:
        payload["pronunciation_dictionary_id"] = pronunciation_dictionary_id
    for k in _SAMPLING_KEYS:
        if sampling.get(k) is not None:
            payload[k] = sampling[k]
    if extra:
        payload.update(extra)
    return payload


def _ws_url(base_url: str, params: Dict[str, Any]) -> str:
    """Build the stream-input URL, percent-encoding the query.

    Encoding matters more than it looks: ``language`` and
    ``pronunciation_dictionary_id`` are caller-supplied, and a value containing
    ``&``, ``=``, ``#`` or a space used to either split into a bogus extra
    parameter or produce a URL the server rejects at the upgrade — surfacing as
    a dropped socket rather than as "your argument was invalid".

    Booleans are lowercased to ``true``/``false``; Python's ``str(True)`` is
    ``"True"``, which most JSON-backed servers do not parse as a boolean.
    """
    base = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    pairs = []
    for k, v in params.items():
        if v is None:
            continue
        pairs.append((k, "true" if v is True else "false" if v is False else str(v)))
    q = urllib.parse.urlencode(pairs)
    return f"{base}/v1/audio/speech/stream-input?{q}"


# ─────────────────────────────────────────────────────────────────────────────
# Synchronous client
# ─────────────────────────────────────────────────────────────────────────────
class _SyncSpeech:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def create(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "mp3",
        model: str = DEFAULT_MODEL,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Synthesize ``input`` and return the full audio as bytes."""
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )

        def _once() -> bytes:
            try:
                r = self._c._http.post(self._c._url("/v1/audio/speech"), json=payload,
                                       headers=self._c._request_headers())
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return r.content

        return _retry_sync(_once, self._c._max_retries)

    def stream(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "pcm",
        model: str = DEFAULT_MODEL,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        chunk_size: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        """Stream synthesized audio as it is generated.

        Yields each block of audio the moment it arrives. ``chunk_size=None``
        (the default) means exactly that: no client-side re-buffering.

        Pass a number only when the consumer needs *fixed-size* frames — a
        telephony media stream wanting 20 ms per packet, say. It does not make
        the stream faster and cannot make it arrive sooner; it can only hold
        bytes back until enough have accumulated.

        That distinction used to cost real latency. The default was 4096, while
        the server deliberately flushes a small first frame — 1920 B for
        ``pcm``, 960 B for ``ulaw`` — so audio can start early. Asking for 4096
        held that first frame back and waited for the second, throwing the head
        start away: **+46 to +131 ms** to first audio depending on region and
        format, measured on production (``MEASUREMENTS.md``).
        """
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        # Retried only up to the first byte. Once audio has been handed to the
        # caller, a retry would replay part of an utterance they are already
        # playing, which is worse than the truncation it tries to hide.
        for attempt in range(self._c._max_retries + 1):
            started = False
            try:
                with self._c._http.stream("POST", self._c._url("/v1/audio/speech"),
                                          json=payload,
                                          headers=self._c._request_headers()) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", "replace")
                        raise_for_status(r.status_code, body,
                                         r.headers.get("x-request-id"), r.headers)
                    for chunk in r.iter_bytes(chunk_size):
                        if chunk:
                            started = True
                            yield chunk
                return
            except httpx.TimeoutException as e:
                err: SvaraError = APITimeoutError(str(e))
            except httpx.HTTPError as e:
                err = APIConnectionError(str(e))
            except SvaraError as e:
                err = e
            if started or attempt >= self._c._max_retries or not _should_retry(err):
                raise err
            time.sleep(_backoff(attempt, err.retry_after))

    def save(self, path: str, **kwargs: Any) -> str:
        """Convenience: synth and write to ``path`` (format inferred from kwargs)."""
        data = self.create(**kwargs)
        with open(path, "wb") as f:
            f.write(data)
        return path


class _SyncVoices:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def list(self, *, use_cache: bool = False) -> List[Voice]:
        """The voice catalogue.

        ``use_cache=True`` returns a previously fetched copy if there is one.
        Off by default so ``list()`` keeps meaning "ask the server", but worth
        turning on in a process that resolves voices repeatedly: the catalogue
        is 320 voices and **282 KB** on the wire.
        """
        if use_cache and self._c._voice_cache is not None:
            return self._c._voice_cache

        def _once() -> List[Voice]:
            try:
                r = self._c._http.get(self._c._url("/v1/voices"),
                                      headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            data = r.json()
            items = data.get("voices", data) if isinstance(data, dict) else data
            return [Voice.from_dict(v) for v in items]

        voices = _retry_sync(_once, self._c._max_retries)
        self._c._voice_cache = voices
        return voices

    def retrieve(self, voice_id: str) -> Voice:
        """Fetch a single voice by id.

        The by-id endpoint resolves only custom voices — library ids 404 — so
        this falls back to scanning the catalogue. The fallback reuses a cached
        catalogue when one is available, because otherwise resolving *n*
        library voices downloads 282 KB *n* times.
        """
        def _once() -> Voice:
            try:
                r = self._c._http.get(
                    self._c._url(f"/v1/voices/{urllib.parse.quote(voice_id, safe='')}"),
                    headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code == 200:
                return Voice.from_dict(r.json())
            for v in self.list(use_cache=True):
                if v.voice_id == voice_id:
                    return v
            raise_for_status(404, f"voice {voice_id} not found", None)

        return _retry_sync(_once, self._c._max_retries)


class Svara:
    """Synchronous Svara client.

    >>> from svara import Svara
    >>> client = Svara(api_key="sk_live_...")
    >>> audio = client.speech.create(input="नमस्ते!", voice="sv_enhdbrj5", response_format="mp3")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = _resolve_key(api_key)
        self.base_url = (base_url or os.environ.get("SVARA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._max_retries = max_retries
        self._voice_cache: Optional[List[Voice]] = None
        # Who owns the transport decides who may close it, and whether we are
        # allowed to write on it. A client we built is ours; one handed to us
        # belongs to the caller, who may well be sharing it with other services.
        self._owns_http = http_client is None
        if http_client is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                headers=_headers(self.api_key),
                timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
            )
        else:
            self._http = http_client
        self.speech = _SyncSpeech(self)
        self.voices = _SyncVoices(self)

    def _url(self, path: str) -> str:
        """Absolute URL for an endpoint.

        Built from *our* ``base_url`` rather than relying on the transport's, so
        a caller-supplied ``http_client`` does not have to be pre-pointed at
        Svara — and so ``base_url=`` on this client always wins for our calls.
        """
        return f"{self.base_url}{path}"

    def _request_headers(self) -> Dict[str, str]:
        """Auth headers for one request.

        Attached per request rather than merged into the transport's defaults.
        A caller-supplied ``http_client`` is frequently shared with other APIs,
        and stamping our key and User-Agent onto it used to leak both onto every
        unrelated request that client made. Same approach the OpenAI SDK takes.
        """
        return _headers(self.api_key)

    def close(self) -> None:
        """Close the transport, if this client owns it.

        A caller-supplied ``http_client`` is left open: closing something we did
        not create breaks whatever else the caller was using it for, and
        ``with Svara(http_client=shared) as c:`` is an easy way to trip over it.
        """
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Svara:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Asynchronous client
# ─────────────────────────────────────────────────────────────────────────────
async def _aiter_text(
    src: Union[Iterable[str], AsyncIterable[str]],
) -> AsyncIterator[str]:
    if hasattr(src, "__aiter__"):
        async for x in src:  # type: ignore[union-attr]
            yield x
    else:
        for x in src:  # type: ignore[union-attr]
            yield x


async def _run_stream(
    ws: Any,
    text: Union[Iterable[str], AsyncIterable[str]],
    *,
    on_event: Optional[Callable[[ChunkEvent], None]] = None,
) -> AsyncIterator[bytes]:
    """Feed ``text`` down an open socket and yield the audio.

    Shared by :meth:`_AsyncSpeech.stream_input` and :meth:`PreparedStream.stream`
    so a pre-opened socket runs through exactly this code rather than a parallel
    copy of it.
    """
    feed_error: List[BaseException] = []

    async def _feed() -> None:
        try:
            async for piece in _aiter_text(text):
                if piece:
                    await ws.send(json.dumps({"text": piece}))
            await ws.send(json.dumps({"text": ""}))  # EOS
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - re-raised to the caller below
            feed_error.append(e)
            # Close the socket so the receive loop below stops waiting. Without
            # this, a text source that raises leaves us blocked forever on audio
            # the server will never produce, because it never saw the end of the
            # text: no exception, no audio, no log line. In a voice agent that
            # is dead air, which is the worst failure mode available.
            try:
                await ws.close(code=1000, reason="client text source failed")
            except Exception:
                pass

    feeder = asyncio.ensure_future(_feed())
    frames = 0
    done = False
    try:
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    frames += 1
                    yield bytes(msg)
                else:
                    try:
                        ev = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type")
                    if etype == "done":
                        done = True
                        break
                    if etype == "chunk" and on_event is not None:
                        on_event(ChunkEvent(text=ev.get("text", ""), peek=ev.get("peek")))
        except _connection_closed_errors() as e:
            # An abnormal close (1006 = connection vanished with no close frame,
            # 1011 = the server reporting its own error) arrives as the
            # websockets library's own exception rather than ending iteration.
            # Without this, half of the same failure leaves as a SvaraError and
            # the other half as a third-party exception the caller never agreed
            # to catch.
            if not feed_error:
                raise StreamInterruptedError(
                    f"The server closed the stream after {frames} audio frame(s) "
                    f"without finishing it ({type(e).__name__}: {e}).",
                    frames=frames,
                    close_code=getattr(ws, "close_code", None),
                ) from e
    finally:
        if not feeder.done():
            feeder.cancel()
        # Awaited, not just cancelled. A bare cancel() leaves the task's
        # exception unretrieved — Python then reports it via an asyncio warning
        # at garbage-collection time, detached from the code that caused it.
        await asyncio.gather(feeder, return_exceptions=True)
        try:
            await ws.close()
        except Exception:
            pass

    # The caller's text source blew up. Surface that as itself: it is their bug,
    # not a Svara API failure, and mislabelling it sends them hunting in the
    # wrong place.
    if feed_error:
        raise feed_error[0]

    if not done:
        code = getattr(ws, "close_code", None)
        raise StreamInterruptedError(
            f"The server closed the stream after {frames} audio frame(s) without "
            f"sending 'done' (close code {code}). The audio is very likely "
            f"truncated.",
            frames=frames,
            close_code=code,
        )


def _connection_closed_errors() -> tuple:
    """The websockets exception(s) meaning "the peer closed the connection".

    Resolved at call time: the SDK imports websockets lazily everywhere else, so
    a missing or renamed symbol should degrade to "catch nothing" rather than
    break importing ``svara``.
    """
    try:
        from websockets.exceptions import ConnectionClosed

        return (ConnectionClosed,)
    except Exception:
        return ()


class PreparedStream:
    """A stream-input socket opened before the text exists.

    Returned by :meth:`AsyncSvara.speech.prepare`. See that method for why.
    """

    #: How long a socket survives with no text on it before the server reaps it.
    #: Treat this as the window in which a pre-opened connection is still worth
    #: having, not as a guarantee — prepare close to when the text is expected.
    IDLE_BUDGET_SECONDS = 20.0

    __slots__ = ("_ws", "_opened_at", "_used")

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._opened_at = time.monotonic()
        self._used = False

    @property
    def idle_seconds(self) -> float:
        """How long this socket has been open with nothing sent on it."""
        return time.monotonic() - self._opened_at

    @property
    def expired(self) -> bool:
        """Whether the socket is past its idle budget, or already closed.

        The observed close code is checked first: a server that reaps early is
        the real authority, and the budget is only a guess about one that has
        not spoken yet.
        """
        if getattr(self._ws, "close_code", None) is not None:
            return True
        return self.idle_seconds > self.IDLE_BUDGET_SECONDS

    async def stream(
        self,
        text: Union[Iterable[str], AsyncIterable[str]],
        *,
        on_event: Optional[Callable[[ChunkEvent], None]] = None,
    ) -> AsyncIterator[bytes]:
        """Feed the text and yield audio, exactly as ``stream_input`` does."""
        if self._used:
            raise SvaraError(
                "This PreparedStream has already been used. The server closes "
                "the socket after an utterance, so prepare() one per utterance."
            )
        self._used = True
        code = getattr(self._ws, "close_code", None)
        if code is not None:
            raise APIConnectionError(
                f"The prepared socket was closed by the server after "
                f"{self.idle_seconds:.1f}s idle (close {code}). Prepare closer to "
                f"when the text arrives — the budget is about "
                f"{self.IDLE_BUDGET_SECONDS:.0f}s."
            )
        async for audio in _run_stream(self._ws, text, on_event=on_event):
            yield audio

    async def aclose(self) -> None:
        """Close an unused prepared socket. Safe to call twice."""
        try:
            await self._ws.close()
        except Exception:
            pass

    async def __aenter__(self) -> PreparedStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if not self._used:
            await self.aclose()


class _AsyncSpeech:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def create(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "mp3",
        model: str = DEFAULT_MODEL,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )

        async def _once() -> bytes:
            try:
                r = await self._c._http.post(self._c._url("/v1/audio/speech"), json=payload,
                                             headers=self._c._request_headers())
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return r.content

        return await _retry_async(_once, self._c._max_retries)

    async def stream(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "pcm",
        model: str = DEFAULT_MODEL,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        chunk_size: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[bytes]:
        """Stream synthesized audio as it is generated.

        ``chunk_size=None`` (the default) yields each block as it arrives. Pass
        a number only for fixed-size frames; see :meth:`Svara.speech.stream` for
        why the old 4096 default cost 46–131 ms of time-to-first-audio.
        """
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        # Retried only up to the first byte — see the sync twin.
        for attempt in range(self._c._max_retries + 1):
            started = False
            try:
                async with self._c._http.stream(
                    "POST", self._c._url("/v1/audio/speech"), json=payload,
                    headers=self._c._request_headers(),
                ) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise_for_status(r.status_code, body,
                                         r.headers.get("x-request-id"), r.headers)
                    async for chunk in r.aiter_bytes(chunk_size):
                        if chunk:
                            started = True
                            yield chunk
                return
            except httpx.TimeoutException as e:
                err: SvaraError = APITimeoutError(str(e))
            except httpx.HTTPError as e:
                err = APIConnectionError(str(e))
            except SvaraError as e:
                err = e
            if started or attempt >= self._c._max_retries or not _should_retry(err):
                raise err
            await asyncio.sleep(_backoff(attempt, err.retry_after))

    async def stream_input(
        self,
        text: Union[Iterable[str], AsyncIterable[str]],
        *,
        voice: str,
        response_format: ResponseFormat = "pcm",
        mode: str = "eager",
        chunk_words: int = 4,
        peek_words: int = 2,
        max_chunk_words: int = 20,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        on_event: Optional[Callable[[ChunkEvent], None]] = None,
    ) -> AsyncIterator[bytes]:
        """Eager input-streaming: feed a (sync or async) iterable of text — e.g. an
        LLM token stream — and yield audio bytes as the model speaks, holding back
        only ``peek_words``. Lowest time-to-first-audio for conversational use.

        ``on_event`` receives :class:`ChunkEvent` (spoken text + lookahead peek)
        as the server reports each chunk.

        For the lowest possible time-to-first-audio, open the socket before the
        text exists with :meth:`prepare` — the handshake is 124–143 ms warm and
        none of it depends on the words.
        """
        ws = await self._connect(
            voice=voice, response_format=response_format, mode=mode,
            chunk_words=chunk_words, peek_words=peek_words,
            max_chunk_words=max_chunk_words, sample_rate=sample_rate, speed=speed,
            language=language, temperature=temperature, top_p=top_p, top_k=top_k,
            repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        async for audio in _run_stream(ws, text, on_event=on_event):
            yield audio

    async def _connect(self, **params: Any) -> Any:
        """Open the stream-input socket. Shared by :meth:`stream_input` and
        :meth:`prepare` so there is exactly one connect path."""
        import websockets

        params["lang"] = params.pop("language", None)
        url = _ws_url(self._c.base_url, params)
        headers = {"xi-api-key": self._c.api_key, "User-Agent": _USER_AGENT}

        kw: Dict[str, Any] = {}
        # An ssl context is only legal on wss://; websockets rejects it on ws://.
        if url.startswith("wss://"):
            kw["ssl"] = self._c._ssl_context or default_ssl_context()
        try:
            # websockets renamed extra_headers -> additional_headers in v14.
            try:
                return await websockets.connect(url, additional_headers=headers, **kw)
            except TypeError:
                return await websockets.connect(url, extra_headers=headers, **kw)
        except OSError as e:
            raise APIConnectionError(str(e)) from e

    async def prepare(
        self,
        *,
        voice: str,
        response_format: ResponseFormat = "pcm",
        mode: str = "eager",
        chunk_words: int = 4,
        peek_words: int = 2,
        max_chunk_words: int = 20,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
    ) -> PreparedStream:
        """Open the streaming socket now, to feed text into later.

        Same arguments as :meth:`stream_input` minus the text — which is exactly
        the thing you do not have yet.

        In a voice agent the handshake is otherwise paid at the worst possible
        moment: the instant the user stops speaking and the LLM starts
        producing. None of it depends on the text, so none of it has to happen
        then. Measured against production it is **124–143 ms** warm.

        Open it while the user is still talking, or as the LLM request goes out::

            prepared = await client.speech.prepare(voice="sv_enhdbrj5")
            ...                                   # LLM produces its first token
            async for audio in prepared.stream(tokens):
                play(audio)

        The socket serves one utterance — the server closes it after ``done`` —
        so this moves the connect earlier rather than avoiding it per utterance.
        It also has a shelf life; see :data:`PreparedStream.IDLE_BUDGET_SECONDS`.
        """
        ws = await self._connect(
            voice=voice, response_format=response_format, mode=mode,
            chunk_words=chunk_words, peek_words=peek_words,
            max_chunk_words=max_chunk_words, sample_rate=sample_rate, speed=speed,
            language=language, temperature=temperature, top_p=top_p, top_k=top_k,
            repetition_penalty=repetition_penalty, presence_penalty=presence_penalty,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        return PreparedStream(ws)

    async def save(self, path: str, **kwargs: Any) -> str:
        data = await self.create(**kwargs)
        with open(path, "wb") as f:
            f.write(data)
        return path


class _AsyncVoices:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def list(self, *, use_cache: bool = False) -> List[Voice]:
        """The voice catalogue. See the sync twin for ``use_cache``."""
        if use_cache and self._c._voice_cache is not None:
            return self._c._voice_cache

        async def _once() -> List[Voice]:
            try:
                r = await self._c._http.get(self._c._url("/v1/voices"),
                                            headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            data = r.json()
            items = data.get("voices", data) if isinstance(data, dict) else data
            return [Voice.from_dict(v) for v in items]

        voices = await _retry_async(_once, self._c._max_retries)
        self._c._voice_cache = voices
        return voices

    async def retrieve(self, voice_id: str) -> Voice:
        """Fetch a single voice by id. See the sync twin."""
        async def _once() -> Voice:
            try:
                r = await self._c._http.get(
                    self._c._url(f"/v1/voices/{urllib.parse.quote(voice_id, safe='')}"),
                    headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code == 200:
                return Voice.from_dict(r.json())
            for v in await self.list(use_cache=True):
                if v.voice_id == voice_id:
                    return v
            raise_for_status(404, f"voice {voice_id} not found", None)

        return await _retry_async(_once, self._c._max_retries)


class AsyncSvara:
    """Asynchronous Svara client.

    >>> from svara import AsyncSvara
    >>> client = AsyncSvara(api_key="sk_live_...")
    >>> async for chunk in client.speech.stream(input="नमस्ते!", voice="sv_enhdbrj5"):
    ...     ...  # PCM bytes
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.AsyncClient] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self.api_key = _resolve_key(api_key)
        self.base_url = (base_url or os.environ.get("SVARA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._max_retries = max_retries
        self._voice_cache: Optional[List[Voice]] = None
        #: TLS context for the WebSocket path. ``None`` means the shared
        #: process-wide one; pass your own to override.
        self._ssl_context = ssl_context
        # See the sync client: a transport we built is ours to configure and to
        # close; one handed to us belongs to the caller.
        self._owns_http = http_client is None
        if http_client is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=_headers(self.api_key),
                timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
            )
        else:
            self._http = http_client
        self.speech = _AsyncSpeech(self)
        self.voices = _AsyncVoices(self)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request_headers(self) -> Dict[str, str]:
        return _headers(self.api_key)

    async def aclose(self) -> None:
        """Close the transport, if this client owns it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncSvara:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
