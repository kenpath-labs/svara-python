"""Svara API clients — synchronous (:class:`Svara`) and async (:class:`AsyncSvara`).

Thin, well-typed wrapper over the public speech API
(https://docs.kenpathlabs.com). Mirrors the real endpoints exactly:

* ``POST /v1/audio/speech``                — synth to bytes, or stream chunks
* ``wss …/v1/audio/speech/stream-input``   — eager input-streaming
* ``GET  /v1/voices``, ``/v1/voices/{id}``, ``/v1/voices/{id}/preview``
* ``GET  /v1/languages``, ``GET /v1/usage``, ``GET /v1/models``
* ``GET/POST /v1/pronunciation-dictionaries``
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NoReturn,
    Optional,
    Union,
)

import httpx

from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InvalidRequestError,
    MissingAPIKeyError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    StreamInterruptedError,
    SvaraError,
    raise_for_status,
)
from .types import (
    FORMAT_INFO,
    MAX_INPUT_CHARS,
    OPUS_SAMPLE_RATES,
    SAMPLE_RATES,
    SPEED_RANGE,
    TELEPHONY_FORMATS,
    Alignment,
    ChunkEvent,
    Language,
    Model,
    PronunciationDictionary,
    PronunciationRule,
    RateLimitInfo,
    ResponseFormat,
    SpeechResponse,
    TimestampedAudio,
    Usage,
    Voice,
    _Flush,
)

DEFAULT_BASE_URL = "https://api.kenpathlabs.com"
#: The one model the API serves (``GET /v1/models``). The field is optional and
#: the server ignores it; it is sent so request logs name the real model.
DEFAULT_MODEL = "svara-tts-turbo"
DEFAULT_MAX_RETRIES = 2
_USER_AGENT = f"svara-python/{__version__}"

#: Read timeout, and a much shorter connect timeout. One flat number for both
#: means a host that is simply unreachable ties the caller up for the whole
#: read budget before failing — a connect either happens quickly or is not
#: going to happen. Same split the OpenAI SDK uses.
#:
#: The read budget is sized for the *non-streaming* worst case: ``create()``
#: receives nothing until the whole clip is rendered, and a 5,000-character
#: request (the server's maximum) renders in roughly 50 s at the measured
#: 6-7× realtime. The old 30 s would have timed out on legitimately long
#: text. On streaming calls the same number bounds the gap between chunks,
#: where it is generous but harmless.
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=5.0)

#: Connection-pool policy. The one number that matters is ``keepalive_expiry``.
#:
#: httpx drops an idle pooled connection after **5 s** by default. A voice
#: agent's turns are further apart than that, so with the default every
#: synthesis re-did TCP + TLS: measured against production, a request 6 s
#: after the previous one cost 354 ms to first audio versus 213 ms with the
#: connection kept — **+140 ms per turn, paid by the SDK, for nothing**. The
#: gateway keeps idle connections open for minutes (verified at 65 s and
#: beyond; see MEASUREMENTS.md), so keeping ours for two is safe. A stale
#: socket, should one ever be reused, surfaces as a connection error before
#: the first byte and is retried.
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20,
                              keepalive_expiry=120.0)

#: Backoff curve. Capped, because an unbounded 0.5·2ⁿ reaches minutes by the
#: time a caller has raised max_retries a couple of notches.
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 8.0


def _should_retry(exc: SvaraError) -> bool:
    """Transient errors worth retrying: connection blips, 429, and 5xx.

    Not ``insufficient_quota``: it is a 429, but nothing about it changes on
    the next attempt, and retrying it is how a client turns one over-budget
    request into a burst of them.
    """
    if isinstance(exc, QuotaExceededError):
        return False
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


def _request_id() -> str:
    """A client-chosen id for one request, sent as ``x-request-id``.

    The server adopts a caller-supplied id and echoes it on every response
    (and mints one when there is none), so client logs and server logs share
    a handle. It comes back on ``SpeechResponse.request_id`` /
    ``SpeechStream.request_id`` / ``SvaraError.request_id``.
    """
    return uuid.uuid4().hex


def _warn_telephony_rate(response_format: str, sample_rate: Optional[int], stacklevel: int = 3) -> None:
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
            stacklevel=stacklevel,
        )


def _warn_dictionary_miss(headers: Any, pronunciation_dictionary_id: Optional[str]) -> None:
    """Warn when a dictionary id was sent but the server did not find it.

    A dictionary id the server cannot resolve is not an error: the request
    proceeds with the organisation's global rules and the response says so in
    ``x-svara-dictionary: miss``. Nobody reads that header, so a typo'd or
    stale id turns into "the rules stopped applying" with no other signal.
    """
    if pronunciation_dictionary_id and headers is not None:
        try:
            status = headers.get("x-svara-dictionary")
        except Exception:
            return
        if status == "miss":
            import warnings

            warnings.warn(
                f"pronunciation_dictionary_id={pronunciation_dictionary_id!r} was not found; "
                f"the server applied the global dictionary instead (x-svara-dictionary: miss). "
                f"Dictionary ids are the UUIDs shown in the console.",
                stacklevel=2,
            )


def _validate(
    *,
    input: Optional[str],
    response_format: str,
    sample_rate: Optional[int],
    speed: Optional[float],
    websocket: bool = False,
) -> None:
    """Reject what the server would reject, before the round trip.

    Only the limits the OpenAPI document states outright — text length, the
    sample-rate set, the speed range, the format enum. Anything the server
    might relax later is left to the server.
    """
    if input is not None:
        if not input:
            raise InvalidRequestError("input is empty; there is nothing to synthesise.")
        if len(input) > MAX_INPUT_CHARS:
            raise InvalidRequestError(
                f"input is {len(input)} characters; the API accepts at most "
                f"{MAX_INPUT_CHARS} per request. Split the text and call once per part."
            )
    if response_format not in FORMAT_INFO:
        raise InvalidRequestError(
            f"response_format={response_format!r} is not one of {', '.join(FORMAT_INFO)}."
        )
    # One list for both paths; a refusal from the socket surfaces as the
    # BadRequestError its error event carries.
    if sample_rate is not None and sample_rate not in SAMPLE_RATES:
        raise InvalidRequestError(
            f"sample_rate={sample_rate} is not one of {SAMPLE_RATES}."
        )
    if response_format == "opus" and sample_rate is not None and sample_rate not in OPUS_SAMPLE_RATES:
        raise InvalidRequestError(
            f"opus is only rendered at {OPUS_SAMPLE_RATES}; sample_rate={sample_rate} is not one of them."
        )
    if speed is not None and not (SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]):
        raise InvalidRequestError(
            f"speed={speed} is outside {SPEED_RANGE[0]}–{SPEED_RANGE[1]}."
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
    bitrate_kbps: Optional[int] = None,
    normalize: Optional[bool] = None,
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
    if normalize is not None:
        payload["normalize"] = normalize
    if bitrate_kbps is not None:
        payload["bitrate_kbps"] = bitrate_kbps
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


def _ws_params(**params: Any) -> Dict[str, Any]:
    """Query parameters for the stream-input socket, in the server's names."""
    params["lang"] = params.pop("language", None)
    return params


def _timestamps_request(
    *, input: str, voice: str, response_format: str, sample_rate: Optional[int],
    bitrate_kbps: Optional[int], speed: Optional[float], language: Optional[str],
    normalize: Optional[bool], pronunciation_dictionary_id: Optional[str], stream: bool,
) -> Dict[str, Any]:
    """Path, query and body for the with-timestamps endpoints.

    Timestamps are served on the ElevenLabs-shaped routes, so the request is
    spelled their way: the format is one ``output_format`` string, speed
    rides in ``voice_settings``. Callers never see that; they pass the same
    arguments as ``create()``.
    """
    _validate(input=input, response_format=response_format, sample_rate=sample_rate, speed=speed)
    fmt = response_format
    fmt += f"_{sample_rate or FORMAT_INFO[response_format]['default_rate']}"
    if bitrate_kbps is not None:
        fmt += f"_{bitrate_kbps}"
    body: Dict[str, Any] = {"text": input}
    if language is not None:
        body["language_code"] = language
    if speed is not None:
        body["voice_settings"] = {"speed": speed}
    if normalize is not None:
        body["apply_text_normalization"] = "on" if normalize else "off"
    if pronunciation_dictionary_id is not None:
        body["pronunciation_dictionary_locators"] = [
            {"pronunciation_dictionary_id": pronunciation_dictionary_id}]
    path = f"/v1/text-to-speech/{urllib.parse.quote(voice, safe='')}"
    path += "/stream/with-timestamps" if stream else "/with-timestamps"
    return {"path": path, "params": {"output_format": fmt}, "json": body}


def _parse_timestamped(obj: Dict[str, Any]) -> TimestampedAudio:
    audio = base64.b64decode(obj.get("audio_base64") or obj.get("audio_base_64") or "")
    return TimestampedAudio(audio=audio, alignment=Alignment.from_dict(obj.get("alignment")), raw=obj)


# ─────────────────────────────────────────────────────────────────────────────
# Streaming response wrappers
# ─────────────────────────────────────────────────────────────────────────────
class _StreamMeta:
    """What a streaming response said about itself. Shared by both wrappers.

    Populated when the response headers arrive — before the first chunk — so
    it is readable from inside the ``for`` loop, and afterwards.
    """

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}
        self.status_code: Optional[int] = None
        self.bytes_received = 0
        self._requested_at: Optional[float] = None
        self._first_audio_at: Optional[float] = None
        self._sent_request_id: Optional[str] = None

    def _begin_attempt(self) -> None:
        """Forget the previous attempt's response before retrying: headers from
        an attempt that died before its first byte must not describe this one."""
        self.headers = {}
        self.status_code = None
        self._requested_at = time.monotonic()

    def _on_response(self, r: httpx.Response) -> None:
        self.headers = dict(r.headers)
        self.status_code = r.status_code

    @property
    def content_type(self) -> Optional[str]:
        return self.headers.get("content-type")

    @property
    def sample_rate(self) -> Optional[int]:
        """The rate the server rendered at (``x-sample-rate``)."""
        v = self.headers.get("x-sample-rate")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def request_id(self) -> Optional[str]:
        """The server's ``x-request-id`` if it sent one, else the id this
        client sent — either way, the handle to quote in a support ticket."""
        return self.headers.get("x-request-id") or self._sent_request_id

    @property
    def rate_limit(self) -> RateLimitInfo:
        """Remaining budget after this request (``x-ratelimit-remaining-*``)."""
        return RateLimitInfo.from_headers(self.headers)

    @property
    def time_to_first_audio(self) -> Optional[float]:
        """Seconds from sending the request to the first audio byte, or ``None``
        until that byte has arrived. Measured on the wire, at this client."""
        if self._requested_at is None or self._first_audio_at is None:
            return None
        return self._first_audio_at - self._requested_at

    def _note_chunk(self, chunk: bytes) -> None:
        if self._first_audio_at is None:
            self._first_audio_at = time.monotonic()
        self.bytes_received += len(chunk)


class SpeechStream(_StreamMeta, Iterator[bytes]):
    """Audio chunks from :meth:`Svara.speech.stream`, as they arrive.

    Iterate it like any generator; it also exposes ``headers``,
    ``content_type``, ``sample_rate``, ``request_id``, ``rate_limit`` and
    ``time_to_first_audio``. Use it as a context manager, or call
    :meth:`close`, to abandon the stream early — the connection is dropped and
    the server stops rendering.
    """

    def __init__(self, gen: Iterator[bytes]) -> None:
        super().__init__()
        self._gen = gen

    def __iter__(self) -> SpeechStream:
        return self

    def __next__(self) -> bytes:
        chunk = next(self._gen)
        self._note_chunk(chunk)
        return chunk

    def read(self) -> bytes:
        """Drain the rest of the stream into one ``bytes``."""
        return b"".join(self)

    def iter_bytes(self, chunk_size: Optional[int] = None) -> SpeechStream:
        """The stream itself. Present so code written against the OpenAI SDK's
        ``with_streaming_response`` keeps working; to get fixed-size frames pass
        ``chunk_size=`` to :meth:`speech.stream` instead."""
        return self

    def stream_to_file(self, path: str) -> str:
        """Write the audio to ``path`` as it arrives; returns the path."""
        with open(path, "wb") as f:
            for chunk in self:
                f.write(chunk)
        return path

    def close(self) -> None:
        self._gen.close()

    def __enter__(self) -> SpeechStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncSpeechStream(_StreamMeta, AsyncIterator[bytes]):
    """Async twin of :class:`SpeechStream`."""

    def __init__(self, gen: AsyncIterator[bytes]) -> None:
        super().__init__()
        self._gen = gen

    def __aiter__(self) -> AsyncSpeechStream:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._gen.__anext__()
        self._note_chunk(chunk)
        return chunk

    async def read(self) -> bytes:
        return b"".join([c async for c in self])

    def iter_bytes(self, chunk_size: Optional[int] = None) -> AsyncSpeechStream:
        """The stream itself (``async for``). See :meth:`SpeechStream.iter_bytes`."""
        return self

    async def stream_to_file(self, path: str) -> str:
        with open(path, "wb") as f:
            async for chunk in self:
                f.write(chunk)
        return path

    async def aclose(self) -> None:
        await self._gen.aclose()

    async def __aenter__(self) -> AsyncSpeechStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def _rid(sent: Dict[str, str], r: Any) -> Optional[str]:
    """The request id to report: the server's if it sent one, else ours."""
    try:
        return r.headers.get("x-request-id") or sent.get("x-request-id")
    except Exception:
        return sent.get("x-request-id")


class _StreamingShim:
    """``speech.with_streaming_response.create(...)`` → ``speech.stream(...)``."""

    def __init__(self, stream_fn: Callable[..., Any]) -> None:
        self._stream = stream_fn

    def create(self, **kwargs: Any) -> Any:
        return self._stream(**kwargs)


class _AudioNamespace:
    """``client.audio.speech`` is ``client.speech`` — the OpenAI SDK's path."""

    def __init__(self, speech: Any) -> None:
        self.speech = speech


def _timeout_kw(timeout: Union[float, httpx.Timeout, None]) -> Dict[str, Any]:
    return {} if timeout is None else {"timeout": timeout}


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
        normalize: Optional[bool] = None,
        bitrate_kbps: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> SpeechResponse:
        """Synthesize ``input`` and return the full audio.

        The result is :class:`SpeechResponse` — a ``bytes`` you can write
        straight to a file, carrying ``content_type``, ``sample_rate`` and the
        remaining ``rate_limit`` budget from the response headers.
        """
        _validate(input=input, response_format=response_format, sample_rate=sample_rate, speed=speed)
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
            bitrate_kbps=bitrate_kbps, normalize=normalize,
        )

        def _once() -> SpeechResponse:
            hdrs = self._c._request_headers(extra_headers)
            try:
                r = self._c._http.post(self._c._url("/v1/audio/speech"), json=payload,
                                       headers=hdrs, params=extra_query, **self._c._tkw(timeout))
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e), request_id=hdrs["x-request-id"]) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e), request_id=hdrs["x-request-id"]) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, _rid(hdrs, r), r.headers)
            _warn_dictionary_miss(r.headers, pronunciation_dictionary_id)
            return SpeechResponse(r.content, dict(r.headers), request_id=_rid(hdrs, r))

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
        normalize: Optional[bool] = None,
        bitrate_kbps: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        chunk_size: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> SpeechStream:
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

        The request is sent on the first iteration, not when this returns.
        The returned :class:`SpeechStream` exposes the response headers,
        ``rate_limit`` and a measured ``time_to_first_audio``.
        """
        _validate(input=input, response_format=response_format, sample_rate=sample_rate, speed=speed)
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
            bitrate_kbps=bitrate_kbps, normalize=normalize,
        )
        wrapper = SpeechStream(iter(()))
        wrapper._gen = self._iter_stream(payload, chunk_size, timeout, wrapper,
                                         extra_headers, extra_query)
        return wrapper

    def _iter_stream(
        self, payload: Dict[str, Any], chunk_size: Optional[int],
        timeout: Union[float, httpx.Timeout, None], meta: _StreamMeta,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        # Retried only up to the first byte. Once audio has been handed to the
        # caller, a retry would replay part of an utterance they are already
        # playing, which is worse than the truncation it tries to hide.
        for attempt in range(self._c._max_retries + 1):
            started = False
            meta._begin_attempt()
            hdrs = self._c._request_headers(extra_headers)
            meta._sent_request_id = hdrs["x-request-id"]
            try:
                with self._c._http.stream("POST", self._c._url("/v1/audio/speech"),
                                          json=payload, headers=hdrs, params=extra_query,
                                          **self._c._tkw(timeout)) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", "replace")
                        raise_for_status(r.status_code, body, _rid(hdrs, r), r.headers)
                    meta._on_response(r)
                    _warn_dictionary_miss(r.headers, payload.get("pronunciation_dictionary_id"))
                    for chunk in r.iter_bytes(chunk_size):
                        if chunk:
                            started = True
                            yield chunk
                return
            except httpx.TimeoutException as e:
                err: SvaraError = APITimeoutError(str(e), request_id=hdrs["x-request-id"])
            except httpx.HTTPError as e:
                err = APIConnectionError(str(e), request_id=hdrs["x-request-id"])
            except SvaraError as e:
                err = e
            if started or attempt >= self._c._max_retries or not _should_retry(err):
                raise err
            time.sleep(_backoff(attempt, err.retry_after))

    def create_with_timestamps(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "mp3",
        sample_rate: Optional[int] = None,
        bitrate_kbps: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        normalize: Optional[bool] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> TimestampedAudio:
        """Synthesize and return the audio with per-character timings.

        ``result.audio`` is the clip in ``response_format``;
        ``result.alignment`` maps characters to seconds (word-accurate,
        character-approximate — see :class:`Alignment`). For subtitles,
        karaoke highlighting and click-to-seek transcripts.
        """
        req = _timestamps_request(
            input=input, voice=voice, response_format=response_format, sample_rate=sample_rate,
            bitrate_kbps=bitrate_kbps, speed=speed, language=language, normalize=normalize,
            pronunciation_dictionary_id=pronunciation_dictionary_id, stream=False)

        def _once() -> TimestampedAudio:
            try:
                r = self._c._http.post(self._c._url(req["path"]), params=req["params"],
                                       json=req["json"], headers=self._c._request_headers(),
                                       **self._c._tkw(timeout))
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
            return _parse_timestamped(r.json())

        return _retry_sync(_once, self._c._max_retries)

    def stream_with_timestamps(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "pcm",
        sample_rate: Optional[int] = None,
        bitrate_kbps: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        normalize: Optional[bool] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> Iterator[TimestampedAudio]:
        """Stream ``TimestampedAudio`` chunks — audio plus the timings of the
        text spoken in that chunk, offsets relative to the start of the clip.
        Retried only until the first chunk, like :meth:`stream`."""
        req = _timestamps_request(
            input=input, voice=voice, response_format=response_format, sample_rate=sample_rate,
            bitrate_kbps=bitrate_kbps, speed=speed, language=language, normalize=normalize,
            pronunciation_dictionary_id=pronunciation_dictionary_id, stream=True)
        for attempt in range(self._c._max_retries + 1):
            started = False
            hdrs = self._c._request_headers()
            try:
                with self._c._http.stream("POST", self._c._url(req["path"]), params=req["params"],
                                          json=req["json"], headers=hdrs,
                                          **self._c._tkw(timeout)) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", "replace")
                        raise_for_status(r.status_code, body, _rid(hdrs, r), r.headers)
                    for line in r.iter_lines():
                        if line.strip():
                            started = True
                            yield _parse_timestamped(json.loads(line))
                return
            except httpx.TimeoutException as e:
                err: SvaraError = APITimeoutError(str(e), request_id=hdrs["x-request-id"])
            except httpx.HTTPError as e:
                err = APIConnectionError(str(e), request_id=hdrs["x-request-id"])
            except SvaraError as e:
                err = e
            if started or attempt >= self._c._max_retries or not _should_retry(err):
                raise err
            time.sleep(_backoff(attempt, err.retry_after))

    def stream_input(
        self,
        text: Iterable[Union[str, _Flush]],
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
        normalize: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        on_event: Optional[Callable[[ChunkEvent], None]] = None,
    ) -> Iterator[bytes]:
        """Eager input-streaming over a blocking socket, for code without an
        event loop.

        Same contract as :meth:`AsyncSvara.speech.stream_input`: ``text`` is any
        iterable of strings (an LLM's token stream, a generator), and audio is
        yielded as the model speaks. The text is fed from a helper thread so a
        slow producer never blocks audio delivery. Prefer the async client in
        an async application; this exists so a Flask view or a script does not
        have to give up eager streaming.
        """
        _validate(input=None, response_format=response_format, sample_rate=sample_rate, speed=speed,
                  websocket=True)
        _warn_telephony_rate(response_format, sample_rate)
        url = _ws_url(self._c.base_url, _ws_params(
            voice=voice, response_format=response_format, mode=mode,
            chunk_words=chunk_words, peek_words=peek_words,
            max_chunk_words=max_chunk_words, sample_rate=sample_rate, speed=speed,
            language=language, normalize=normalize, temperature=temperature, top_p=top_p,
            top_k=top_k, repetition_penalty=repetition_penalty,
            presence_penalty=presence_penalty,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        ))
        return _run_stream_sync(url, self._c._request_headers(), self._c._connect_timeout,
                                text, on_event=on_event)

    @property
    def with_streaming_response(self) -> _StreamingShim:
        """OpenAI-SDK spelling of :meth:`stream`::

            with client.audio.speech.with_streaming_response.create(...) as r:
                for chunk in r.iter_bytes(): ...

        Unlike the OpenAI SDK against this API, no ``extra_body={"stream": True}``
        is needed — this always streams.
        """
        return _StreamingShim(self.stream)

    def save(self, path: str, **kwargs: Any) -> str:
        """Convenience: synth and write to ``path`` (format inferred from kwargs)."""
        data = self.create(**kwargs)
        with open(path, "wb") as f:
            f.write(data)
        return path


def _run_stream_sync(
    url: str,
    headers: Dict[str, str],
    connect_timeout: float,
    text: Iterable[Union[str, _Flush]],
    *,
    on_event: Optional[Callable[[ChunkEvent], None]] = None,
) -> Iterator[bytes]:
    """Blocking twin of :func:`_run_stream`, on ``websockets.sync``.

    A generator, so the socket opens on the first ``next()`` — the same lazy
    contract as :meth:`speech.stream` — and is closed however the loop ends.
    """
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect as ws_connect

    kw: Dict[str, Any] = {"open_timeout": connect_timeout, "close_timeout": _WS_CLOSE_TIMEOUT}
    if url.startswith("wss://"):
        kw["ssl"] = default_ssl_context()
    try:
        cm = ws_connect(url, additional_headers=headers, **kw)
    except (TimeoutError, socket.timeout) as e:
        # Before OSError: TimeoutError is an OSError subclass.
        raise APITimeoutError(f"WebSocket connect timed out after {connect_timeout}s",
                              request_id=headers.get("x-request-id")) from e
    except OSError as e:
        raise APIConnectionError(str(e), request_id=headers.get("x-request-id")) from e
    except Exception as e:
        _raise_handshake_error(e, headers.get("x-request-id"))

    with cm as ws:
        feed_error: List[BaseException] = []
        stop = threading.Event()

        def _feed() -> None:
            try:
                for piece in text:
                    if stop.is_set():
                        return
                    if isinstance(piece, _Flush):
                        ws.send(json.dumps({"flush": True}))
                    elif piece:
                        ws.send(json.dumps({"text": piece}))
                ws.send(json.dumps({"text": ""}))  # EOS
            except BaseException as e:  # noqa: BLE001 - re-raised to the caller below
                feed_error.append(e)
                try:
                    ws.close(code=1000, reason="client text source failed")
                except Exception:
                    pass

        feeder = threading.Thread(target=_feed, name="svara-stream-input-feeder", daemon=True)
        feeder.start()
        frames = 0
        done = False
        try:
            try:
                for msg in ws:
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
                        if etype == "error":
                            _raise_ws_error(ev, getattr(ws, "close_code", None))
                        if etype == "chunk" and on_event is not None:
                            on_event(ChunkEvent(text=ev.get("text", ""), peek=ev.get("peek")))
            except ConnectionClosed as e:
                if not feed_error:
                    code = getattr(ws, "close_code", None)
                    raise StreamInterruptedError(
                        f"The server closed the stream after {frames} audio frame(s) "
                        f"without finishing it (close code {code}: "
                        f"{_WS_CLOSE_MEANING.get(code, 'unknown')}; {type(e).__name__}: {e}).",
                        frames=frames,
                        close_code=code,
                    ) from e
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass
            # The feeder is a daemon thread and exits on its next send, which
            # now raises. Wait only briefly: a text source stalled inside
            # ``next()`` (an LLM that has gone quiet) must not stall the
            # caller's barge-in for the length of that stall. Note that the
            # feeder may pull one more item from the source before it notices.
            feeder.join(timeout=0.5)

        if feed_error:
            raise feed_error[0]
        if not done:
            code = getattr(ws, "close_code", None)
            raise StreamInterruptedError(
                f"The server closed the stream after {frames} audio frame(s) without "
                f"sending 'done' (close code {code}: {_WS_CLOSE_MEANING.get(code, 'unknown')}). "
                f"The audio is very likely truncated.",
                frames=frames,
                close_code=code,
            )


def _parse_voices(data: Any) -> List[Voice]:
    items = data.get("voices", data) if isinstance(data, dict) else data
    return [Voice.from_dict(v) for v in items]


def _filter_voices(
    voices: List[Voice], *, language: Optional[str], gender: Optional[str],
    curated: Optional[bool],
) -> List[Voice]:
    out = voices
    if language is not None:
        lang = language.lower()
        out = [v for v in out if (v.language or "").lower() == lang]
    if gender is not None:
        g = gender.lower()
        out = [v for v in out if (v.gender or "").lower() == g]
    if curated is not None:
        out = [v for v in out if v.curated == curated]
    return out


class _SyncVoices:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def list(
        self,
        *,
        language: Optional[str] = None,
        gender: Optional[str] = None,
        curated: Optional[bool] = None,
        use_cache: bool = False,
    ) -> List[Voice]:
        """The voice catalogue, optionally filtered client-side.

        ``language`` matches the voice's native ISO code (``"hi"``), ``gender``
        is ``"female"``/``"male"``, ``curated=True`` keeps the reviewed set.

        ``use_cache=True`` returns a previously fetched copy if there is one.
        Off by default so ``list()`` keeps meaning "ask the server", but worth
        turning on in a process that resolves voices repeatedly: the catalogue
        is 320 voices and **282 KB** on the wire.
        """
        if use_cache and self._c._voice_cache is not None:
            return _filter_voices(self._c._voice_cache, language=language, gender=gender,
                                  curated=curated)

        def _once() -> List[Voice]:
            try:
                r = self._c._http.get(self._c._url("/v1/voices"),
                                      headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return _parse_voices(r.json())

        voices = _retry_sync(_once, self._c._max_retries)
        self._c._voice_cache = voices
        return _filter_voices(voices, language=language, gender=gender, curated=curated)

    def search(self, query: str, *, language: Optional[str] = None,
               gender: Optional[str] = None, use_cache: bool = True) -> List[Voice]:
        """Voices whose id, name, accent, language, description or labels
        contain every word of ``query`` (case-insensitive).

        Searched client-side over the ``/v1/voices`` catalogue, which is cached
        after the first call — repeated searches cost no request. (The server's
        ``/v2/voices`` offers the same search with paging.)
        """
        return _search_voices(self.list(language=language, gender=gender, use_cache=use_cache),
                              query)

    def retrieve(self, voice_id: str) -> Voice:
        """Fetch a single voice by id.

        Falls back to the catalogue on a 404, reusing a cached copy when there
        is one so resolving *n* voices does not download 282 KB *n* times.
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
            if r.status_code != 404:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
            for v in self.list(use_cache=True):
                if v.voice_id == voice_id:
                    return v
            raise_for_status(404, f"voice {voice_id} not found", None)

        return _retry_sync(_once, self._c._max_retries)

    def preview(self, voice_id: str) -> SpeechResponse:
        """A ready-made sample clip of the voice (``audio/mpeg``)."""
        def _once() -> SpeechResponse:
            try:
                r = self._c._http.get(
                    self._c._url(f"/v1/voices/{urllib.parse.quote(voice_id, safe='')}/preview"),
                    headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return SpeechResponse(r.content, dict(r.headers))

        return _retry_sync(_once, self._c._max_retries)


def _parse_models(data: Any) -> List[Model]:
    # Two shapes, chosen by the server from the request's headers: OpenAI's
    # {"object": "list", "data": [...]}, and ElevenLabs' bare array (which is
    # what a request carrying xi-api-key gets).
    items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
    return [Model.from_dict(x) for x in items or []]


def _search_voices(voices: List[Voice], query: str) -> List[Voice]:
    q = query.strip().lower()
    if not q:
        return voices
    terms = q.split()

    def hay(v: Voice) -> str:
        parts = [v.voice_id, v.name, v.gender, v.accent_family, v.description, v.language,
                 *(str(x) for x in v.labels.values())]
        return " ".join(str(p).lower() for p in parts if p)

    return [v for v in voices if all(t in hay(v) for t in terms)]


class _SyncModels:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def list(self) -> List[Model]:
        """The models the API serves. Today that is one: ``svara-tts-turbo``."""
        def _once() -> List[Model]:
            try:
                r = self._c._http.get(self._c._url("/v1/models"),
                                      headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return _parse_models(r.json())

        return _retry_sync(_once, self._c._max_retries)


class _SyncLanguages:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def list(self) -> List[Language]:
        """Every language the model speaks, with the codes ``language=`` accepts."""
        def _once() -> List[Language]:
            try:
                r = self._c._http.get(self._c._url("/v1/languages"),
                                      headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            data = r.json()
            items = data.get("languages", data) if isinstance(data, dict) else data
            return [Language.from_dict(x) for x in items]

        return _retry_sync(_once, self._c._max_retries)


class _SyncUsage:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def get(self) -> Usage:
        """Plan limits, month-to-date characters and remaining balance.

        Counts against the requests-per-minute budget like any call; poll it
        once a minute at most.
        """
        def _once() -> Usage:
            try:
                r = self._c._http.get(self._c._url("/v1/usage"),
                                      headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return Usage(raw=r.json())

        return _retry_sync(_once, self._c._max_retries)


def _dictionary_body(name: str, rules: List[PronunciationRule], description: Optional[str]) -> Dict[str, Any]:
    if not rules:
        raise InvalidRequestError("a pronunciation dictionary needs at least one rule.")
    body: Dict[str, Any] = {"name": name, "rules": [r.to_dict() for r in rules]}
    if description is not None:
        body["description"] = description
    return body


def _parse_dictionaries(data: Any) -> List[PronunciationDictionary]:
    items = data.get("pronunciation_dictionaries", data) if isinstance(data, dict) else data
    return [PronunciationDictionary.from_dict(x) for x in items]


class _SyncPronunciationDictionaries:
    """Respelling rules applied before synthesis. Create in the console or
    here; pass the id as ``pronunciation_dictionary_id`` on speech calls."""

    def __init__(self, client: Svara) -> None:
        self._c = client

    def _get(self, path: str) -> Any:
        try:
            r = self._c._http.get(self._c._url(path), headers=self._c._request_headers())
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e
        if r.status_code != 200:
            raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
        return r.json()

    def list(self) -> List[PronunciationDictionary]:
        """Every dictionary in the workspace."""
        return _retry_sync(lambda: _parse_dictionaries(self._get("/v1/pronunciation-dictionaries")),
                           self._c._max_retries)

    def retrieve(self, dictionary_id: str) -> PronunciationDictionary:
        """One dictionary, with its rules in ``.raw``."""
        path = f"/v1/pronunciation-dictionaries/{urllib.parse.quote(dictionary_id, safe='')}"
        return _retry_sync(lambda: PronunciationDictionary.from_dict(self._get(path)),
                           self._c._max_retries)

    def create_from_rules(
        self, *, name: str, rules: List[PronunciationRule], description: Optional[str] = None,
    ) -> PronunciationDictionary:
        """Create a dictionary. All-or-nothing: a bad rule fails the whole call
        (422) and nothing is created. 403 when the plan has no room; 409 on a
        duplicate name. Not retried."""
        body = _dictionary_body(name, rules, description)
        try:
            r = self._c._http.post(self._c._url("/v1/pronunciation-dictionaries/add-from-rules"),
                                   json=body, headers=self._c._request_headers())
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e
        if r.status_code != 200:
            raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
        return PronunciationDictionary.from_dict(r.json())


class _ClientBase:
    """State and helpers common to both clients."""

    api_key: str
    base_url: str
    _max_retries: int
    _voice_cache: Optional[List[Voice]]
    _connect_timeout: Optional[float]
    _default_headers: Optional[Dict[str, str]] = None
    #: Set by with_options(timeout=...): a clone shares the parent's transport,
    #: whose own timeout it cannot change, so the override rides on each call.
    _call_timeout: Union[float, httpx.Timeout, None] = None

    def _tkw(self, timeout: Union[float, httpx.Timeout, None]) -> Dict[str, Any]:
        return _timeout_kw(self._call_timeout if timeout is None else timeout)

    def _init_common(
        self, api_key: Optional[str], base_url: Optional[str], max_retries: int,
        timeout: Union[float, httpx.Timeout, None],
    ) -> httpx.Timeout:
        self.api_key = _resolve_key(api_key)
        self.base_url = (base_url or os.environ.get("SVARA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._max_retries = max_retries
        self._voice_cache = None
        # A float keeps httpx's meaning (one number for every phase), as it did
        # in 0.1.0; the 5 s connect split applies to the default only.
        t = DEFAULT_TIMEOUT if timeout is None else (
            timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout))
        #: Connect budget for the WebSocket path. ``None`` means no limit.
        self._connect_timeout: Optional[float] = t.connect
        return t

    def _url(self, path: str) -> str:
        """Absolute URL for an endpoint.

        Built from *our* ``base_url`` rather than relying on the transport's, so
        a caller-supplied ``http_client`` does not have to be pre-pointed at
        Svara — and so ``base_url=`` on this client always wins for our calls.
        """
        return f"{self.base_url}{path}"

    def _request_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Auth headers for one request, plus a fresh ``x-request-id``.

        Attached per request rather than merged into the transport's defaults.
        A caller-supplied ``http_client`` is frequently shared with other APIs,
        and stamping our key and User-Agent onto it used to leak both onto every
        unrelated request that client made. Same approach the OpenAI SDK takes.
        """
        h = _headers(self.api_key)
        h["x-request-id"] = _request_id()
        if self._default_headers:
            h.update(self._default_headers)
        if extra:
            h.update(extra)
        return h


class Svara(_ClientBase):
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
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        t = self._init_common(api_key, base_url, max_retries, timeout)
        self._timeout = t
        self._default_headers = dict(default_headers) if default_headers else None
        # Who owns the transport decides who may close it, and whether we are
        # allowed to write on it. A client we built is ours; one handed to us
        # belongs to the caller, who may well be sharing it with other services.
        self._owns_http = http_client is None
        if http_client is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                headers=_headers(self.api_key),
                timeout=t,
                limits=DEFAULT_LIMITS,
            )
        else:
            self._http = http_client
        self.speech = _SyncSpeech(self)
        self.voices = _SyncVoices(self)
        self.languages = _SyncLanguages(self)
        self.usage = _SyncUsage(self)
        self.models = _SyncModels(self)
        self.pronunciation_dictionaries = _SyncPronunciationDictionaries(self)
        #: ``client.audio.speech`` — the path code written for the OpenAI SDK uses.
        self.audio = _AudioNamespace(self.speech)

    def with_options(
        self, *, timeout: Union[float, httpx.Timeout, None] = None,
        max_retries: Optional[int] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> Svara:
        """A client with different defaults that **shares this one's connection
        pool** — so ``client.with_options(max_retries=0).speech.create(...)``
        costs no new connection. Closing the copy does not close the pool."""
        clone = Svara(
            self.api_key, base_url=self.base_url,
            timeout=self._timeout if timeout is None else timeout,
            max_retries=self._max_retries if max_retries is None else max_retries,
            http_client=self._http,
            default_headers={**(self._default_headers or {}), **(default_headers or {})} or None,
        )
        clone._voice_cache = self._voice_cache
        if timeout is not None:
            clone._call_timeout = clone._timeout
        return clone

    def warm_up(self) -> None:
        """Open the HTTP connection now, so the first synthesis does not.

        A cold client pays DNS + TCP + TLS on its first request — about 100 ms
        against production from India, more from further away. Nothing about
        that depends on the text, so an agent can pay it at start-up instead of
        on the first thing it says. Fetches ``/v1/models`` (89 bytes, no
        authentication) and keeps the connection in the pool. Raises
        :class:`APIConnectionError` if the API is unreachable.
        """
        try:
            self._http.get(self._url("/v1/models"), headers=self._request_headers())
        except httpx.TimeoutException as e:
            raise APITimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e

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
    src: Union[Iterable[Any], AsyncIterable[Any]],
) -> AsyncIterator[Any]:
    if hasattr(src, "__aiter__"):
        async for x in src:  # type: ignore[union-attr]
            yield x
    else:
        for x in src:  # type: ignore[union-attr]
            yield x


async def _run_stream(
    ws: Any,
    text: Union[Iterable[Union[str, _Flush]], AsyncIterable[Union[str, _Flush]]],
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
                if isinstance(piece, _Flush):
                    await ws.send(json.dumps({"flush": True}))
                elif piece:
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
                    if etype == "error":
                        # The server names the problem (unknown voice, bad
                        # argument) and then closes. Raise that, not the
                        # "closed without done" the close would otherwise turn
                        # into — the message is the useful part.
                        _raise_ws_error(ev, getattr(ws, "close_code", None))
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
                code = getattr(ws, "close_code", None)
                raise StreamInterruptedError(
                    f"The server closed the stream after {frames} audio frame(s) "
                    f"without finishing it (close code {code}: "
                    f"{_WS_CLOSE_MEANING.get(code, 'unknown')}; {type(e).__name__}: {e}).",
                    frames=frames,
                    close_code=code,
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
            f"sending 'done' (close code {code}: {_WS_CLOSE_MEANING.get(code, 'unknown')}). "
            f"The audio is very likely truncated.",
            frames=frames,
            close_code=code,
        )


#: How long to wait for the peer's close frame when we abandon a socket. The
#: library default is 10 s; measured, a consumer that stops mid-utterance left
#: the server thinking the stream was live for that long — one concurrency
#: slot held for nothing on the next turn. One second is plenty for a close
#: handshake and short enough that barge-in stays barge-in.
_WS_CLOSE_TIMEOUT = 1.0


def _raise_handshake_error(e: BaseException, request_id: Optional[str]) -> NoReturn:
    """A refused WebSocket upgrade, as the SvaraError the same status gives over HTTP.

    websockets >= 14 raises ``InvalidStatus`` with a ``.response``; the legacy
    client (12/13) raised ``InvalidStatusCode`` with ``.status_code`` and
    ``.headers`` on the exception itself. Read both shapes: mislabelling a 401
    as a connection error makes a revoked key look like a network blip — and
    LiveKit retries connection errors.
    """
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None) or getattr(e, "status_code", None)
    if status:
        headers = getattr(resp, "headers", None) or getattr(e, "headers", None)
        body = ""
        try:
            body = bytes(getattr(resp, "body", b"") or b"").decode("utf-8", "replace")
        except Exception:
            pass
        raise_for_status(int(status), body or str(e), request_id, headers)
    raise APIConnectionError(f"WebSocket handshake failed: {e}", request_id=request_id) from e


def _raise_ws_error(ev: Dict[str, Any], close_code: Optional[int]) -> NoReturn:
    """Turn a ``{"type": "error", "message": …}`` control event into the
    SvaraError the same failure produces over HTTP."""
    msg = str(ev.get("message") or ev.get("error") or "unknown error")
    code = ev.get("code") or ev.get("status")
    low = msg.lower()
    if "not found" in low:
        raise NotFoundError(f"Svara API error 404: {msg}", status_code=404, body=json.dumps(ev),
                            code=code or "voice_not_found")
    raise BadRequestError(f"Svara API error (WebSocket): {msg}", status_code=400,
                          body=json.dumps(ev), code=code)


#: What the close codes the server uses mean, for the interrupted-stream message.
_WS_CLOSE_MEANING = {
    1000: "normal close",
    1006: "connection lost without a close frame",
    1008: "the server rejected the request",
    1011: "the server hit an internal error",
    1013: "the server is not ready to serve; try again shortly",
}


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

    #: How long a socket may sit idle before this SDK stops trusting it.
    #:
    #: Measured against production (2026-09-17): a prepared socket left idle
    #: for 60, 120, 180 and 300 s still accepted text and returned audio with
    #: the usual ~130 ms to first frame. The server does not reap idle native
    #: sockets on a short fuse, so this budget is a hedge against
    #: intermediaries and future server policy rather than a measured limit —
    #: and :attr:`expired` checks the observed close code first, which is the
    #: real authority. It used to be 20 s, which threw away most of the
    #: prewarmed sockets in a real conversation (turns are often further apart
    #: than that) and paid the connect inline after all.
    IDLE_BUDGET_SECONDS = 240.0

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
    def closed(self) -> bool:
        """Whether the peer (or we) already closed the socket."""
        return getattr(self._ws, "close_code", None) is not None

    @property
    def expired(self) -> bool:
        """Whether the socket is already closed, or past its idle budget.

        The observed close code is checked first: a server that reaps early is
        the real authority, and the budget is only a guess about one that has
        not spoken yet.
        """
        if self.closed:
            return True
        return self.idle_seconds > self.IDLE_BUDGET_SECONDS

    async def stream(
        self,
        text: Union[Iterable[Union[str, _Flush]], AsyncIterable[Union[str, _Flush]]],
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
        inner = _run_stream(self._ws, text, on_event=on_event)
        try:
            async for audio in inner:
                yield audio
        finally:
            await inner.aclose()

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
        normalize: Optional[bool] = None,
        bitrate_kbps: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> SpeechResponse:
        """Synthesize ``input`` and return the full audio. See the sync twin."""
        _validate(input=input, response_format=response_format, sample_rate=sample_rate, speed=speed)
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
            bitrate_kbps=bitrate_kbps, normalize=normalize,
        )

        async def _once() -> SpeechResponse:
            hdrs = self._c._request_headers(extra_headers)
            try:
                r = await self._c._http.post(self._c._url("/v1/audio/speech"), json=payload,
                                             headers=hdrs, params=extra_query,
                                             **self._c._tkw(timeout))
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e), request_id=hdrs["x-request-id"]) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e), request_id=hdrs["x-request-id"]) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, _rid(hdrs, r), r.headers)
            _warn_dictionary_miss(r.headers, pronunciation_dictionary_id)
            return SpeechResponse(r.content, dict(r.headers), request_id=_rid(hdrs, r))

        return await _retry_async(_once, self._c._max_retries)

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
        normalize: Optional[bool] = None,
        bitrate_kbps: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        chunk_size: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> AsyncSpeechStream:
        """Stream synthesized audio as it is generated.

        ``chunk_size=None`` (the default) yields each block as it arrives. Pass
        a number only for fixed-size frames; see :meth:`Svara.speech.stream` for
        why the old 4096 default cost 46–131 ms of time-to-first-audio.

        Not a coroutine: iterate the result with ``async for`` directly.
        """
        _validate(input=input, response_format=response_format, sample_rate=sample_rate, speed=speed)
        _warn_telephony_rate(response_format, sample_rate)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=_sampling(temperature, top_p, top_k, repetition_penalty,
                               presence_penalty),
            extra=extra_body,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
            bitrate_kbps=bitrate_kbps, normalize=normalize,
        )
        wrapper = AsyncSpeechStream(_empty_aiter())
        wrapper._gen = self._iter_stream(payload, chunk_size, timeout, wrapper,
                                         extra_headers, extra_query)
        return wrapper

    async def _iter_stream(
        self, payload: Dict[str, Any], chunk_size: Optional[int],
        timeout: Union[float, httpx.Timeout, None], meta: _StreamMeta,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[bytes]:
        # Retried only up to the first byte — see the sync twin.
        for attempt in range(self._c._max_retries + 1):
            started = False
            meta._begin_attempt()
            hdrs = self._c._request_headers(extra_headers)
            meta._sent_request_id = hdrs["x-request-id"]
            try:
                async with self._c._http.stream(
                    "POST", self._c._url("/v1/audio/speech"), json=payload,
                    headers=hdrs, params=extra_query, **self._c._tkw(timeout),
                ) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise_for_status(r.status_code, body, _rid(hdrs, r), r.headers)
                    meta._on_response(r)
                    _warn_dictionary_miss(r.headers, payload.get("pronunciation_dictionary_id"))
                    async for chunk in r.aiter_bytes(chunk_size):
                        if chunk:
                            started = True
                            yield chunk
                return
            except httpx.TimeoutException as e:
                err: SvaraError = APITimeoutError(str(e), request_id=hdrs["x-request-id"])
            except httpx.HTTPError as e:
                err = APIConnectionError(str(e), request_id=hdrs["x-request-id"])
            except SvaraError as e:
                err = e
            if started or attempt >= self._c._max_retries or not _should_retry(err):
                raise err
            await asyncio.sleep(_backoff(attempt, err.retry_after))

    async def create_with_timestamps(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "mp3",
        sample_rate: Optional[int] = None,
        bitrate_kbps: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        normalize: Optional[bool] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> TimestampedAudio:
        """Synthesize with per-character timings. See the sync twin."""
        req = _timestamps_request(
            input=input, voice=voice, response_format=response_format, sample_rate=sample_rate,
            bitrate_kbps=bitrate_kbps, speed=speed, language=language, normalize=normalize,
            pronunciation_dictionary_id=pronunciation_dictionary_id, stream=False)

        async def _once() -> TimestampedAudio:
            try:
                r = await self._c._http.post(self._c._url(req["path"]), params=req["params"],
                                             json=req["json"], headers=self._c._request_headers(),
                                             **self._c._tkw(timeout))
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
            return _parse_timestamped(r.json())

        return await _retry_async(_once, self._c._max_retries)

    async def stream_with_timestamps(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "pcm",
        sample_rate: Optional[int] = None,
        bitrate_kbps: Optional[int] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
        normalize: Optional[bool] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
    ) -> AsyncIterator[TimestampedAudio]:
        """Stream ``TimestampedAudio`` chunks. See the sync twin."""
        req = _timestamps_request(
            input=input, voice=voice, response_format=response_format, sample_rate=sample_rate,
            bitrate_kbps=bitrate_kbps, speed=speed, language=language, normalize=normalize,
            pronunciation_dictionary_id=pronunciation_dictionary_id, stream=True)
        for attempt in range(self._c._max_retries + 1):
            started = False
            hdrs = self._c._request_headers()
            try:
                async with self._c._http.stream(
                    "POST", self._c._url(req["path"]), params=req["params"], json=req["json"],
                    headers=hdrs, **self._c._tkw(timeout),
                ) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise_for_status(r.status_code, body, _rid(hdrs, r), r.headers)
                    async for line in r.aiter_lines():
                        if line.strip():
                            started = True
                            yield _parse_timestamped(json.loads(line))
                return
            except httpx.TimeoutException as e:
                err: SvaraError = APITimeoutError(str(e), request_id=hdrs["x-request-id"])
            except httpx.HTTPError as e:
                err = APIConnectionError(str(e), request_id=hdrs["x-request-id"])
            except SvaraError as e:
                err = e
            if started or attempt >= self._c._max_retries or not _should_retry(err):
                raise err
            await asyncio.sleep(_backoff(attempt, err.retry_after))

    async def stream_input(
        self,
        text: Union[Iterable[Union[str, _Flush]], AsyncIterable[Union[str, _Flush]]],
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
        normalize: Optional[bool] = None,
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

        Yield :data:`svara.FLUSH` from the text source to have everything
        buffered so far spoken immediately (a paragraph or turn boundary).

        ``on_event`` receives :class:`ChunkEvent` (spoken text + lookahead peek)
        as the server reports each chunk.

        For the lowest possible time-to-first-audio, open the socket before the
        text exists with :meth:`prepare` — the connect and admission are paid
        then instead of when the LLM's first token lands. Measured: first audio
        427 ms after the call with a fresh connection, 132 ms on a prepared one.
        """
        ws = await self._connect(
            voice=voice, response_format=response_format, mode=mode,
            chunk_words=chunk_words, peek_words=peek_words,
            max_chunk_words=max_chunk_words, sample_rate=sample_rate, speed=speed,
            language=language, normalize=normalize, temperature=temperature, top_p=top_p,
            top_k=top_k, repetition_penalty=repetition_penalty,
            presence_penalty=presence_penalty,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        inner = _run_stream(ws, text, on_event=on_event)
        try:
            async for audio in inner:
                yield audio
        finally:
            # aclose() on what we returned lands here; pass it on so the socket
            # closes now rather than whenever the event loop finalises the
            # orphaned inner generator.
            await inner.aclose()

    async def _connect(self, **params: Any) -> Any:
        """Open the stream-input socket. Shared by :meth:`stream_input` and
        :meth:`prepare` so there is exactly one connect path."""
        import websockets

        _validate(input=None, response_format=params.get("response_format", "pcm"),
                  sample_rate=params.get("sample_rate"), speed=params.get("speed"), websocket=True)
        _warn_telephony_rate(params.get("response_format", "pcm"), params.get("sample_rate"),
                             stacklevel=4)
        url = _ws_url(self._c.base_url, _ws_params(**params))
        headers = self._c._request_headers()

        kw: Dict[str, Any] = {"open_timeout": self._c._connect_timeout,
                              "close_timeout": _WS_CLOSE_TIMEOUT}
        # An ssl context is only legal on wss://; websockets rejects it on ws://.
        if url.startswith("wss://"):
            kw["ssl"] = self._c._ssl_context or default_ssl_context()
        rid = headers.get("x-request-id")
        try:
            # websockets renamed extra_headers -> additional_headers in v14.
            try:
                return await websockets.connect(url, additional_headers=headers, **kw)
            except TypeError:
                return await websockets.connect(url, extra_headers=headers, **kw)
        except (asyncio.TimeoutError, TimeoutError) as e:
            # Before OSError: on 3.11+ asyncio.TimeoutError *is* TimeoutError,
            # an OSError subclass, and would otherwise be caught below.
            raise APITimeoutError(
                f"WebSocket connect timed out after {kw['open_timeout']}s", request_id=rid) from e
        except OSError as e:
            raise APIConnectionError(str(e), request_id=rid) from e
        except Exception as e:
            # The upgrade was refused (401 on a bad key, 429 over the stream
            # limit …). The caller signed up for SvaraError.
            _raise_handshake_error(e, rid)

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
        normalize: Optional[bool] = None,
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

        In a voice agent the connect is otherwise paid at the worst possible
        moment: the instant the user stops speaking and the LLM starts
        producing. None of it depends on the text, so none of it has to happen
        then. Measured against production: first audio **427 ms** after
        ``stream_input`` is called on a fresh connection, **132 ms** on a
        prepared one.

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
            language=language, normalize=normalize, temperature=temperature, top_p=top_p,
            top_k=top_k, repetition_penalty=repetition_penalty,
            presence_penalty=presence_penalty,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        return PreparedStream(ws)

    @property
    def with_streaming_response(self) -> _StreamingShim:
        """OpenAI-SDK spelling of :meth:`stream` (``async with`` / ``async for``)."""
        return _StreamingShim(self.stream)

    async def save(self, path: str, **kwargs: Any) -> str:
        data = await self.create(**kwargs)
        with open(path, "wb") as f:
            f.write(data)
        return path


async def _empty_aiter() -> AsyncIterator[bytes]:
    return
    yield b""  # pragma: no cover - makes this an async generator


class _AsyncVoices:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def list(
        self,
        *,
        language: Optional[str] = None,
        gender: Optional[str] = None,
        curated: Optional[bool] = None,
        use_cache: bool = False,
    ) -> List[Voice]:
        """The voice catalogue. See the sync twin for the filters and ``use_cache``."""
        if use_cache and self._c._voice_cache is not None:
            return _filter_voices(self._c._voice_cache, language=language, gender=gender,
                                  curated=curated)

        async def _once() -> List[Voice]:
            try:
                r = await self._c._http.get(self._c._url("/v1/voices"),
                                            headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return _parse_voices(r.json())

        voices = await _retry_async(_once, self._c._max_retries)
        self._c._voice_cache = voices
        return _filter_voices(voices, language=language, gender=gender, curated=curated)

    async def search(self, query: str, *, language: Optional[str] = None,
                     gender: Optional[str] = None, use_cache: bool = True) -> List[Voice]:
        """Client-side search over the catalogue. See the sync twin."""
        return _search_voices(
            await self.list(language=language, gender=gender, use_cache=use_cache), query)

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
            if r.status_code != 404:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
            for v in await self.list(use_cache=True):
                if v.voice_id == voice_id:
                    return v
            raise_for_status(404, f"voice {voice_id} not found", None)

        return await _retry_async(_once, self._c._max_retries)

    async def preview(self, voice_id: str) -> SpeechResponse:
        """A ready-made sample clip of the voice (``audio/mpeg``)."""
        async def _once() -> SpeechResponse:
            try:
                r = await self._c._http.get(
                    self._c._url(f"/v1/voices/{urllib.parse.quote(voice_id, safe='')}/preview"),
                    headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return SpeechResponse(r.content, dict(r.headers))

        return await _retry_async(_once, self._c._max_retries)


class _AsyncModels:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def list(self) -> List[Model]:
        """The models the API serves. See the sync twin."""
        async def _once() -> List[Model]:
            try:
                r = await self._c._http.get(self._c._url("/v1/models"),
                                            headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return _parse_models(r.json())

        return await _retry_async(_once, self._c._max_retries)


class _AsyncLanguages:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def list(self) -> List[Language]:
        """Every language the model speaks. See the sync twin."""
        async def _once() -> List[Language]:
            try:
                r = await self._c._http.get(self._c._url("/v1/languages"),
                                            headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            data = r.json()
            items = data.get("languages", data) if isinstance(data, dict) else data
            return [Language.from_dict(x) for x in items]

        return await _retry_async(_once, self._c._max_retries)


class _AsyncUsage:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def get(self) -> Usage:
        """Plan limits, month-to-date characters and remaining balance."""
        async def _once() -> Usage:
            try:
                r = await self._c._http.get(self._c._url("/v1/usage"),
                                            headers=self._c._request_headers())
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text,
                                 r.headers.get("x-request-id"), r.headers)
            return Usage(raw=r.json())

        return await _retry_async(_once, self._c._max_retries)


class _AsyncPronunciationDictionaries:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def _get(self, path: str) -> Any:
        try:
            r = await self._c._http.get(self._c._url(path), headers=self._c._request_headers())
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e
        if r.status_code != 200:
            raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
        return r.json()

    async def list(self) -> List[PronunciationDictionary]:
        async def _once() -> List[PronunciationDictionary]:
            return _parse_dictionaries(await self._get("/v1/pronunciation-dictionaries"))
        return await _retry_async(_once, self._c._max_retries)

    async def retrieve(self, dictionary_id: str) -> PronunciationDictionary:
        path = f"/v1/pronunciation-dictionaries/{urllib.parse.quote(dictionary_id, safe='')}"

        async def _once() -> PronunciationDictionary:
            return PronunciationDictionary.from_dict(await self._get(path))
        return await _retry_async(_once, self._c._max_retries)

    async def create_from_rules(
        self, *, name: str, rules: List[PronunciationRule], description: Optional[str] = None,
    ) -> PronunciationDictionary:
        """See the sync twin."""
        body = _dictionary_body(name, rules, description)
        try:
            r = await self._c._http.post(
                self._c._url("/v1/pronunciation-dictionaries/add-from-rules"),
                json=body, headers=self._c._request_headers())
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e
        if r.status_code != 200:
            raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"), r.headers)
        return PronunciationDictionary.from_dict(r.json())


class AsyncSvara(_ClientBase):
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
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        t = self._init_common(api_key, base_url, max_retries, timeout)
        self._timeout = t
        self._default_headers = dict(default_headers) if default_headers else None
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
                timeout=t,
                limits=DEFAULT_LIMITS,
            )
        else:
            self._http = http_client
        self.speech = _AsyncSpeech(self)
        self.voices = _AsyncVoices(self)
        self.languages = _AsyncLanguages(self)
        self.usage = _AsyncUsage(self)
        self.models = _AsyncModels(self)
        self.pronunciation_dictionaries = _AsyncPronunciationDictionaries(self)
        self.audio = _AudioNamespace(self.speech)

    def with_options(
        self, *, timeout: Union[float, httpx.Timeout, None] = None,
        max_retries: Optional[int] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> AsyncSvara:
        """See :meth:`Svara.with_options`."""
        clone = AsyncSvara(
            self.api_key, base_url=self.base_url,
            timeout=self._timeout if timeout is None else timeout,
            max_retries=self._max_retries if max_retries is None else max_retries,
            http_client=self._http, ssl_context=self._ssl_context,
            default_headers={**(self._default_headers or {}), **(default_headers or {})} or None,
        )
        clone._voice_cache = self._voice_cache
        if timeout is not None:
            clone._call_timeout = clone._timeout
        return clone

    async def warm_up(self) -> None:
        """Open the HTTP connection now. See :meth:`Svara.warm_up`.

        For the eager WebSocket path use :meth:`speech.prepare` instead — that
        opens the socket the next utterance will actually use.
        """
        try:
            await self._http.get(self._url("/v1/models"), headers=self._request_headers())
        except httpx.TimeoutException as e:
            raise APITimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e

    async def aclose(self) -> None:
        """Close the transport, if this client owns it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncSvara:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
