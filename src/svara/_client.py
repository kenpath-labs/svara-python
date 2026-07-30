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

from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
    SvaraError,
    raise_for_status,
)
from .types import ChunkEvent, ResponseFormat, Voice

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
    for k in _SAMPLING_KEYS:
        if sampling.get(k) is not None:
            payload[k] = sampling[k]
    if extra:
        payload.update(extra)
    return payload


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
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Synthesize ``input`` and return the full audio as bytes."""
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body,
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
        chunk_size: int = 4096,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Iterator[bytes]:
        """Stream synthesized audio as it is generated (first bytes in ~0.3–0.5 s)."""
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body,
        )
        try:
            with self._c._http.stream("POST", "/v1/audio/speech", json=payload) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", "replace")
                    raise_for_status(r.status_code, body, r.headers.get("x-request-id"))
                for chunk in r.iter_bytes(chunk_size):
                    if chunk:
                        yield chunk
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
            if r.status_code == 404:
                raise_for_status(404, r.text, r.headers.get("x-request-id"))
            # Endpoint may not exist on all deployments — fall back to the catalog.
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
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=False, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body,
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
        chunk_size: int = 4096,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[bytes]:
        payload = _speech_payload(
            input=input, voice=voice, model=model, response_format=response_format,
            stream=True, sample_rate=sample_rate, speed=speed, language=language,
            sampling=locals(), extra=extra_body,
        )
        try:
            async with self._c._http.stream("POST", "/v1/audio/speech", json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise_for_status(r.status_code, body, r.headers.get("x-request-id"))
                async for chunk in r.aiter_bytes(chunk_size):
                    if chunk:
                        yield chunk
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
        language: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        on_event: Optional[Callable[[ChunkEvent], None]] = None,
    ) -> AsyncIterator[bytes]:
        """Eager input-streaming: feed a (sync or async) iterable of text — e.g. an
        LLM token stream — and yield audio bytes as the model speaks, holding back
        only ``peek_words``. Lowest time-to-first-audio for conversational use.

        ``on_event`` receives :class:`ChunkEvent` (spoken text + lookahead peek)
        as the server reports each chunk.
        """
        import websockets

        params = {
            "voice": voice,
            "mode": mode,
            "response_format": response_format,
            "chunk_words": chunk_words,
            "peek_words": peek_words,
            "max_chunk_words": max_chunk_words,
            "sample_rate": sample_rate,
            "lang": language,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repetition_penalty": repetition_penalty,
            "presence_penalty": presence_penalty,
        }
        url = _ws_url(self._c.base_url, params)
        headers = {"xi-api-key": self._c.api_key}

        # websockets renamed extra_headers -> additional_headers in v14.
        try:
            ws_cm = websockets.connect(url, additional_headers=headers)
        except TypeError:
            ws_cm = websockets.connect(url, extra_headers=headers)

        async def _aiter(src: Union[Iterable[str], AsyncIterable[str]]) -> AsyncIterator[str]:
            if hasattr(src, "__aiter__"):
                async for x in src:  # type: ignore[union-attr]
                    yield x
            else:
                for x in src:  # type: ignore[union-attr]
                    yield x

        try:
            async with ws_cm as ws:
                async def _feed() -> None:
                    async for piece in _aiter(text):
                        if piece:
                            await ws.send(json.dumps({"text": piece}))
                    await ws.send(json.dumps({"text": ""}))  # EOS

                feeder = asyncio.ensure_future(_feed())
                try:
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            yield bytes(msg)
                        else:
                            try:
                                ev = json.loads(msg)
                            except json.JSONDecodeError:
                                continue
                            if ev.get("type") == "done":
                                break
                            if ev.get("type") == "chunk" and on_event is not None:
                                on_event(ChunkEvent(text=ev.get("text", ""), peek=ev.get("peek")))
                finally:
                    feeder.cancel()
        except OSError as e:
            raise APIConnectionError(str(e)) from e

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
            if r.status_code == 404:
                raise_for_status(404, r.text, r.headers.get("x-request-id"))
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
