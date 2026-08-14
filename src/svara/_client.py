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
import logging
import os
import time
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

from ._audio import SERVER_APPLIES_VOLUME, Gain, check_volume
from ._timing import Timeline, WordCounter, staged_connect
from ._timing import emit as timing_emit
from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
    SvaraError,
    raise_for_status,
)
from .types import FORMAT_INFO, ChunkEvent, ResponseFormat, Voice

log = logging.getLogger("svara")

DEFAULT_BASE_URL = "https://api.kenpathlabs.com"
DEFAULT_MODEL = "svara-1"
DEFAULT_MAX_RETRIES = 2
_USER_AGENT = f"svara-python/{__version__}"


def _should_retry(exc: SvaraError) -> bool:
    """Transient errors worth retrying: connection blips, 429, and 5xx."""
    if isinstance(exc, (APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code and exc.status_code >= 500:
        return True
    return False


def _backoff(attempt: int) -> float:
    return 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s, …


def _retry_sync(fn, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except SvaraError as e:
            if attempt < max_retries and _should_retry(e):
                time.sleep(_backoff(attempt))
                continue
            raise


async def _retry_async(fn, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except SvaraError as e:
            if attempt < max_retries and _should_retry(e):
                await asyncio.sleep(_backoff(attempt))
                continue
            raise

# Optional sampling knobs. Omitted from the request unless the caller sets them,
# so the SDK inherits the server's certified defaults rather than pinning its own.
_SAMPLING_KEYS = ("temperature", "top_p", "top_k", "repetition_penalty", "presence_penalty")


def _resolve_key(api_key: Optional[str]) -> str:
    key = api_key or os.environ.get("SVARA_API_KEY")
    if not key:
        raise ValueError(
            "No API key. Pass api_key=... or set the SVARA_API_KEY environment variable."
        )
    return key


def _headers(api_key: str) -> Dict[str, str]:
    # The API accepts either `xi-api-key` (ElevenLabs-style) or `Authorization:
    # Bearer`. We use xi-api-key; both are equivalent.
    return {"xi-api-key": api_key, "User-Agent": _USER_AGENT}


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
    volume: Optional[float] = None,
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
    if volume is not None:
        payload["volume"] = volume
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


#: The server refuses to chunk smaller than this and clamps quietly, so a
#: caller asking for 2 gets 4. Mirrored here only to predict the eager trigger.
_CHUNK_WORDS_FLOOR = 4

_VOLUME_MODES = ("auto", "server", "client")


def _resolve_volume(
    volume: Optional[float],
    volume_mode: str,
    response_format: str,
) -> "tuple[Optional[float], Optional[Gain]]":
    """Decide which single party applies the gain.

    Returns ``(value_to_send, local_gain)`` with **at most one of them set**.
    That exclusivity is the whole point: if the server scales the samples and
    the SDK scales them again, ``volume=1.4`` arrives as 1.96x with clipped
    peaks — a defect that is silent on quiet text and obvious on loud text.

    ``auto`` follows :data:`~svara._audio.SERVER_APPLIES_VOLUME`, so the day the
    API ships volume the SDK stops touching the bytes and starts forwarding the
    field, with no change at any call site.
    """
    if volume_mode not in _VOLUME_MODES:
        raise ValueError(
            f"volume_mode must be one of {_VOLUME_MODES}, got {volume_mode!r}"
        )
    check_volume(volume)
    if volume is None:
        return None, None
    if volume_mode == "server" or (volume_mode == "auto" and SERVER_APPLIES_VOLUME):
        return volume, None
    # Client-side. Gain() rejects container formats loudly rather than
    # accepting a volume it would silently drop.
    return None, Gain(volume, response_format)


def _ws_url(base_url: str, params: Dict[str, Any]) -> str:
    base = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    q = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
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
        volume: Optional[float] = None,
        volume_mode: str = "auto",
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
        send_volume, gain = _resolve_volume(volume, volume_mode, response_format)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body, volume=send_volume,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )

        def _once() -> bytes:
            try:
                r = self._c._http.post("/v1/audio/speech", json=payload)
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"))
            return r.content

        data = _retry_sync(_once, self._c._max_retries)
        return data if gain is None else gain.apply(data) + gain.flush()

    def stream(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "pcm",
        model: str = DEFAULT_MODEL,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        volume: Optional[float] = None,
        volume_mode: str = "auto",
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        chunk_size: int = 4096,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        """Stream synthesized audio as it is generated (first bytes in ~0.3–0.5 s)."""
        send_volume, gain = _resolve_volume(volume, volume_mode, response_format)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body, volume=send_volume,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        try:
            with self._c._http.stream("POST", "/v1/audio/speech", json=payload) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", "replace")
                    raise_for_status(r.status_code, body, r.headers.get("x-request-id"))
                for chunk in r.iter_bytes(chunk_size):
                    if chunk:
                        yield chunk if gain is None else gain.apply(chunk)
                if gain is not None:
                    # A held-back odd byte, if the stream ended mid-sample.
                    tail = gain.flush()
                    if tail:
                        yield tail
        except httpx.TimeoutException as e:
            raise APITimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e

    def save(self, path: str, **kwargs: Any) -> str:
        """Convenience: synth and write to ``path`` (format inferred from kwargs)."""
        data = self.create(**kwargs)
        with open(path, "wb") as f:
            f.write(data)
        return path


class _SyncVoices:
    def __init__(self, client: Svara) -> None:
        self._c = client

    def list(self) -> List[Voice]:
        def _once() -> List[Voice]:
            try:
                r = self._c._http.get("/v1/voices")
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"))
            data = r.json()
            items = data.get("voices", data) if isinstance(data, dict) else data
            return [Voice.from_dict(v) for v in items]

        return _retry_sync(_once, self._c._max_retries)

    def retrieve(self, voice_id: str) -> Voice:
        """Fetch a single voice by id (falls back to scanning ``list()``)."""
        def _once() -> Voice:
            try:
                r = self._c._http.get(f"/v1/voices/{voice_id}")
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code == 200:
                return Voice.from_dict(r.json())
            # The by-id endpoint resolves only custom voices (404 for library
            # ids) and may not exist at all — fall back to the catalog.
            for v in self.list():
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
        timeout: float = 30.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = _resolve_key(api_key)
        self.base_url = (base_url or os.environ.get("SVARA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._max_retries = max_retries
        self._http = http_client or httpx.Client(
            base_url=self.base_url, headers=_headers(self.api_key), timeout=timeout
        )
        self._http.headers.update(_headers(self.api_key))  # also cover a passed-in client
        self.speech = _SyncSpeech(self)
        self.voices = _SyncVoices(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Svara:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Asynchronous client
# ─────────────────────────────────────────────────────────────────────────────
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
        volume: Optional[float] = None,
        volume_mode: str = "auto",
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        send_volume, gain = _resolve_volume(volume, volume_mode, response_format)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body, volume=send_volume,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )

        async def _once() -> bytes:
            try:
                r = await self._c._http.post("/v1/audio/speech", json=payload)
            except httpx.TimeoutException as e:
                raise APITimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"))
            return r.content

        data = await _retry_async(_once, self._c._max_retries)
        return data if gain is None else gain.apply(data) + gain.flush()

    async def stream(
        self,
        *,
        input: str,
        voice: str,
        response_format: ResponseFormat = "pcm",
        model: str = DEFAULT_MODEL,
        sample_rate: Optional[int] = None,
        speed: Optional[float] = None,
        volume: Optional[float] = None,
        volume_mode: str = "auto",
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        chunk_size: int = 4096,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[bytes]:
        send_volume, gain = _resolve_volume(volume, volume_mode, response_format)
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body, volume=send_volume,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        try:
            async with self._c._http.stream("POST", "/v1/audio/speech", json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise_for_status(r.status_code, body, r.headers.get("x-request-id"))
                async for chunk in r.aiter_bytes(chunk_size):
                    if chunk:
                        yield chunk if gain is None else gain.apply(chunk)
                if gain is not None:
                    # A held-back odd byte, if the stream ended mid-sample.
                    tail = gain.flush()
                    if tail:
                        yield tail
        except httpx.TimeoutException as e:
            raise APITimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise APIConnectionError(str(e)) from e

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
        volume: Optional[float] = None,
        volume_mode: str = "auto",
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        on_event: Optional[Callable[[ChunkEvent], None]] = None,
        on_timing: Optional[Callable[[Timeline], None]] = None,
    ) -> AsyncIterator[bytes]:
        """Eager input-streaming: feed a (sync or async) iterable of text — e.g. an
        LLM token stream — and yield audio bytes as the model speaks, holding back
        only ``peek_words``. Lowest time-to-first-audio for conversational use.

        ``on_event`` receives :class:`ChunkEvent` (spoken text + lookahead peek)
        as the server reports each chunk.

        ``on_timing`` receives a :class:`~svara._timing.Timeline` once the
        stream ends — connect breakdown (including API-key check time), time to
        first audio split by whether the caller's LLM or the model was the one
        being waited on, and audio throughput. Setting ``SVARA_TIMING=1`` logs
        the same record to the ``svara.timing`` logger without any code change.

        ``volume`` scales the audio (1.0 = unchanged). ``volume_mode`` picks who
        applies it — see :func:`_resolve_volume`. When it lands client-side the
        cost shows up as ``gain_ms`` on the timeline rather than disappearing
        into the caller's process.
        """
        send_volume, gain = _resolve_volume(volume, volume_mode, response_format)
        params = {
            "voice": voice,
            "mode": mode,
            "response_format": response_format,
            "chunk_words": chunk_words,
            "peek_words": peek_words,
            "max_chunk_words": max_chunk_words,
            "sample_rate": sample_rate,
            "speed": speed,
            "volume": send_volume,
            "lang": language,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repetition_penalty": repetition_penalty,
            "presence_penalty": presence_penalty,
            "pronunciation_dictionary_id": pronunciation_dictionary_id,
        }
        url = _ws_url(self._c.base_url, params)
        headers = {"xi-api-key": self._c.api_key}

        # In eager mode the server starts speaking once 2 × chunk_words WHOLE
        # WORDS are buffered — it wants a full chunk plus a full next chunk in
        # hand before committing to the first one. Measured, not documented: an
        # A/B over the query params showed the trigger tracking chunk_words
        # exactly (4→8, 8→16, 10→20), peek_words having no effect on it at all,
        # and values under 4 clamping silently to 4. Everything before that
        # point is the caller's LLM being slow, not us — the timeline keeps the
        # two apart. :attr:`Timeline.words_at_first_chunk` reports where the
        # server *actually* started, so a server-side change shows up as a
        # divergence rather than as a silently wrong label.
        tl = Timeline(
            trigger_words=(2 * max(chunk_words, _CHUNK_WORDS_FLOOR)) if mode == "eager" else 0,
            voice=voice,
            mode=mode,
            response_format=response_format,
            sample_rate=sample_rate or FORMAT_INFO.get(response_format, {}).get("default_rate", 24000),
            volume=volume,
            volume_applied_by=("client" if gain is not None
                               else "server" if send_volume is not None else None),
        )
        counter = WordCounter()

        async def _aiter(src: Union[Iterable[str], AsyncIterable[str]]) -> AsyncIterator[str]:
            if hasattr(src, "__aiter__"):
                async for x in src:  # type: ignore[union-attr]
                    yield x
            else:
                for x in src:  # type: ignore[union-attr]
                    yield x

        try:
            ws = await staged_connect(url, timeline=tl, additional_headers=headers)
        except OSError as e:
            tl.error = f"connect: {e}"
            tl.t_end = time.perf_counter()
            timing_emit(tl)
            if on_timing is not None:
                on_timing(tl)
            raise APIConnectionError(str(e)) from e
        except BaseException as e:  # 401/403 surface here, as an upgrade rejection
            tl.error = f"{type(e).__name__}: {e}"
            tl.t_end = time.perf_counter()
            timing_emit(tl)
            if on_timing is not None:
                on_timing(tl)
            raise

        try:
            async def _feed() -> None:
                async for piece in _aiter(text):
                    if piece:
                        await ws.send(json.dumps({"text": piece}))
                        now = time.perf_counter()
                        tl.messages_sent += 1
                        tl.chars_sent += len(piece)
                        if tl.t_first_text is None:
                            tl.t_first_text = now
                        tl.words_sent = counter.add(piece)
                        if tl.trigger_words:
                            if tl.t_trigger_word is None and tl.words_sent >= tl.trigger_words:
                                tl.t_trigger_word = now
                            if (tl.t_trigger_message is None
                                    and tl.messages_sent >= tl.trigger_words):
                                tl.t_trigger_message = now
                await ws.send(json.dumps({"text": ""}))  # EOS

            feeder = asyncio.ensure_future(_feed())
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        # Measured as it arrived. Gain is applied after, and
                        # scaling doesn't change what the network delivered —
                        # billing the transport for it would be wrong.
                        tl.note_audio(len(msg))
                        yield bytes(msg) if gain is None else gain.apply(bytes(msg))
                    else:
                        try:
                            ev = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        etype = ev.get("type")
                        if etype:
                            tl.events.append(etype)
                        # `flushed` answers a {"flush": true}; `done` is only sent
                        # at end-of-stream. Recorded, not acted on — changing which
                        # one ends the loop is a behaviour change, not instrumentation.
                        if etype == "flushed" and tl.t_flushed is None:
                            tl.t_flushed = time.perf_counter()
                        if etype == "done":
                            tl.t_done = time.perf_counter()
                            break
                        if etype == "chunk":
                            if tl.t_first_chunk is None:
                                # Announced immediately before that chunk's
                                # audio, so it separates the server's input-side
                                # wait from actual generation.
                                tl.t_first_chunk = time.perf_counter()
                                tl.first_chunk_words = len(ev.get("text", "").split())
                                tl.words_at_first_chunk = tl.words_sent
                            if on_event is not None:
                                on_event(ChunkEvent(text=ev.get("text", ""),
                                                    peek=ev.get("peek")))
            finally:
                feeder.cancel()
            if gain is not None:
                # Outside the finally on purpose: yielding while unwinding an
                # exception (or an aclose()) is how async generators deadlock.
                tail = gain.flush()
                if tail:
                    yield tail
        except OSError as e:
            tl.error = f"stream: {e}"
            raise APIConnectionError(str(e)) from e
        finally:
            if gain is not None:
                tl.gain_seconds = gain.seconds
                tl.clipped_samples = gain.clipped_samples
            try:
                await ws.close()
            except Exception:
                pass
            tl.t_end = time.perf_counter()
            timing_emit(tl)
            if on_timing is not None:
                try:
                    on_timing(tl)
                except Exception:
                    log.warning("on_timing callback raised", exc_info=True)

    async def save(self, path: str, **kwargs: Any) -> str:
        data = await self.create(**kwargs)
        with open(path, "wb") as f:
            f.write(data)
        return path


class _AsyncVoices:
    def __init__(self, client: AsyncSvara) -> None:
        self._c = client

    async def list(self) -> List[Voice]:
        async def _once() -> List[Voice]:
            try:
                r = await self._c._http.get("/v1/voices")
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code != 200:
                raise_for_status(r.status_code, r.text, r.headers.get("x-request-id"))
            data = r.json()
            items = data.get("voices", data) if isinstance(data, dict) else data
            return [Voice.from_dict(v) for v in items]

        return await _retry_async(_once, self._c._max_retries)

    async def retrieve(self, voice_id: str) -> Voice:
        """Fetch a single voice by id (falls back to scanning ``list()``)."""
        async def _once() -> Voice:
            try:
                r = await self._c._http.get(f"/v1/voices/{voice_id}")
            except httpx.HTTPError as e:
                raise APIConnectionError(str(e)) from e
            if r.status_code == 200:
                return Voice.from_dict(r.json())
            # The by-id endpoint resolves only custom voices (404 for library
            # ids) and may not exist at all — fall back to the catalog.
            for v in await self.list():
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
        timeout: float = 30.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = _resolve_key(api_key)
        self.base_url = (base_url or os.environ.get("SVARA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._max_retries = max_retries
        self._http = http_client or httpx.AsyncClient(
            base_url=self.base_url, headers=_headers(self.api_key), timeout=timeout
        )
        self._http.headers.update(_headers(self.api_key))  # also cover a passed-in client
        self.speech = _AsyncSpeech(self)
        self.voices = _AsyncVoices(self)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncSvara:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
