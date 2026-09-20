"""Svara TTS plugin for LiveKit Agents, built on the ``svara`` SDK client.

Two synthesis paths, chosen by ``mode``:

* ``mode="eager"`` — forwards the LLM token stream into Svara's input-streaming
  WebSocket; the model speaks ~a chunk in (holds back only ``peek_words``), so
  first audio lands fast and cross-sentence prosody stays natural.
* ``mode="http"`` — buffers whole sentences and synthesizes each over the
  streaming HTTP endpoint. Used for fixed-text ``say()``.

Both emit 24 kHz mono 16-bit PCM to LiveKit's ``AudioEmitter``; LiveKit resamples
to the 8 kHz telephony leg on its own.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Optional

logger = logging.getLogger("svara.livekit")

from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given

from .._client import AsyncSvara
from ..exceptions import InvalidRequestError, QuotaExceededError, SvaraError

SAMPLE_RATE = 24000  # Svara streams 24 kHz mono s16le PCM.
NUM_CHANNELS = 1
DEFAULT_VOICE = "sv_enhdbrj5"  # Aanya (Bengali-native, female; speaks every language). Override per session.

# Platform-certified sampling — keep in sync with the server so the plugin sounds
# like the API it fronts.
_SAMPLING = dict(temperature=1.2, top_p=0.9, top_k=40, repetition_penalty=1.1, presence_penalty=0)

_SENT_END = re.compile(r'[.!?।॥]["\')\]]*\s')


def _pron_extra(pron_dict_id: Optional[str]):
    """extra_body carrying the pronunciation dictionary id onto HTTP speech calls."""
    return {"pronunciation_dictionary_id": pron_dict_id} if pron_dict_id else None


@dataclass
class _Opts:
    voice: str
    language: Optional[str]
    speed: Optional[float]
    mode: str
    chunk_words: int
    peek_words: int
    pron_dict_id: Optional[str] = None


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        language: Optional[str] = None,
        speed: Optional[float] = None,
        mode: str = "eager",
        chunk_words: int = 4,
        peek_words: int = 2,
        api_key: NotGivenOr[str] = NOT_GIVEN,
        base_url: NotGivenOr[str] = NOT_GIVEN,
        sample_rate: int = SAMPLE_RATE,
        pronunciation_dictionary_id: Optional[str] = None,
        prewarm: bool = True,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )
        self._sample_rate = sample_rate
        self._prewarm = prewarm
        self._prepared = None
        self._prepared_kwargs: Optional[dict] = None
        self._prepare_task: Optional[asyncio.Task] = None
        self._closing: set = set()
        self._client = AsyncSvara(
            api_key=api_key if is_given(api_key) else None,
            base_url=base_url if is_given(base_url) else None,
        )
        self._opts = _Opts(
            voice=voice, language=language, speed=speed,
            mode=(mode or "eager").lower(), chunk_words=chunk_words, peek_words=peek_words,
            pron_dict_id=pronunciation_dictionary_id,
        )

    @property
    def mode(self) -> str:
        return self._opts.mode

    # LiveKit reads these for tracing and the metrics it emits per turn; the
    # base class answers "unknown" for all three unless a plugin overrides.
    @property
    def label(self) -> str:
        return "svara.TTS"

    @property
    def model(self) -> str:
        return "svara-tts-turbo"

    @property
    def provider(self) -> str:
        return "svara"

    # ── connection prewarming ────────────────────────────────────────────────
    # The eager WebSocket handshake costs 124-143 ms warm against production,
    # and in an agent it otherwise lands exactly when the user has stopped
    # talking and is waiting to hear something. None of it depends on the text,
    # so it is opened between turns instead. Best-effort throughout: if the
    # prewarm fails or has gone stale, synthesis falls back to connecting
    # inline and the caller sees nothing but the usual latency.

    def prewarm(self, **overrides: Any) -> None:
        """Open a stream-input socket now, for the next utterance to use.

        LiveKit calls this itself when an agent activity starts, and the plugin
        re-arms after every utterance, so most agents never need to call it.
        Safe to call repeatedly — a socket is only opened if there is not
        already a live one. Measured against production: first audio lands
        ~300 ms sooner on a prepared socket than on a fresh connection.
        """
        kwargs = dict(
            voice=self._opts.voice, response_format="pcm", mode="eager",
            chunk_words=self._opts.chunk_words, peek_words=self._opts.peek_words,
            sample_rate=self._sample_rate, language=self._opts.language,
            speed=self._opts.speed,
            pronunciation_dictionary_id=self._opts.pron_dict_id, **_SAMPLING,
        )
        kwargs.update(overrides)
        self._arm_prepared(kwargs)

    def _arm_prepared(self, kwargs: dict) -> None:
        if self._opts.mode != "eager" or not self._prewarm:
            return
        if self._prepare_task is not None and not self._prepare_task.done():
            return
        if self._prepared is not None and not self._prepared.expired:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._discard_prepared()
        self._prepare_task = loop.create_task(self._open_prepared(kwargs))

    async def _open_prepared(self, kwargs: dict) -> None:
        try:
            prepared = await self._client.speech.prepare(**kwargs)
        except Exception:
            # Never fatal: this is an optimisation, and the synthesis path
            # connects for itself when there is nothing prepared.
            logger.debug("svara: prewarm failed, will connect inline", exc_info=True)
            self._prepared = None
            return
        # The socket's URL fixed voice, speed, language and the rest at open
        # time. Remember them so a later update_options() does not get served
        # by a socket that will speak in the old voice.
        self._prepared = prepared
        self._prepared_kwargs = kwargs

    def _discard_prepared(self) -> None:
        """Close whatever is prepared, without waiting on it."""
        prepared, self._prepared = self._prepared, None
        if prepared is None:
            return
        try:
            task = asyncio.ensure_future(prepared.aclose())
        except RuntimeError:
            return
        # Hold the reference: a pending task nobody references may be
        # garbage-collected before it runs.
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _take_prepared(self, kwargs: Optional[dict] = None):
        """The prepared socket, if one is ready, still usable, and opened with
        the options the caller wants now."""
        prepared = self._prepared
        if prepared is None:
            return None
        if prepared.expired or (kwargs is not None and self._prepared_kwargs != kwargs):
            # Reaped while idle, or opened for a different voice/speed/language
            # than update_options() has since set. Close it rather than feed it.
            self._discard_prepared()
            return None
        self._prepared = None
        return prepared

    def update_options(
        self,
        *,
        voice: NotGivenOr[str] = NOT_GIVEN,
        language: NotGivenOr[Optional[str]] = NOT_GIVEN,
        speed: NotGivenOr[Optional[float]] = NOT_GIVEN,
        mode: NotGivenOr[str] = NOT_GIVEN,
        pronunciation_dictionary_id: NotGivenOr[Optional[str]] = NOT_GIVEN,
    ) -> None:
        if is_given(voice):
            self._opts.voice = voice
        if is_given(language):
            self._opts.language = language
        if is_given(speed):
            self._opts.speed = speed
        if is_given(mode) and mode in ("eager", "http"):
            self._opts.mode = mode
        if is_given(pronunciation_dictionary_id):
            self._opts.pron_dict_id = pronunciation_dictionary_id

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        return SynthesizeStream(tts=self, conn_options=conn_options)

    async def aclose(self) -> None:
        if self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()
            await asyncio.gather(self._prepare_task, return_exceptions=True)
        if self._prepared is not None:
            await self._prepared.aclose()
            self._prepared = None
        if self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)
        await self._client.aclose()


class ChunkedStream(tts.ChunkedStream):
    """Non-streaming: one HTTP synth for fixed text (``say()``)."""

    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts._sample_rate,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        try:
            async for chunk in self._tts._client.speech.stream(
                input=self.input_text, voice=self._opts.voice, response_format="pcm",
                sample_rate=self._tts._sample_rate, language=self._opts.language,
                speed=self._opts.speed, extra_body=_pron_extra(self._opts.pron_dict_id),
                **_SAMPLING,
            ):
                output_emitter.push(chunk)
            output_emitter.flush()
        except SvaraError as e:
            raise _to_lk_error(e) from e


class SynthesizeStream(tts.SynthesizeStream):
    """Streaming: forward the LLM token stream into Svara (eager WS or http-buffered)."""

    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = utils.shortuuid()
        output_emitter.initialize(
            request_id=request_id, sample_rate=self._tts._sample_rate,
            num_channels=NUM_CHANNELS, mime_type="audio/pcm", stream=True,
        )
        output_emitter.start_segment(segment_id=request_id)
        try:
            if self._opts.mode == "eager":
                await self._run_eager(output_emitter)
            else:
                await self._run_http(output_emitter)
        except SvaraError as e:
            raise _to_lk_error(e) from e
        output_emitter.end_segment()

    async def _text_stream(self):
        """Adapt LiveKit's input channel to an async iterator of text pieces."""
        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                continue
            if data:
                self._mark_started()
                yield data

    async def _run_eager(self, output_emitter: tts.AudioEmitter) -> None:
        kwargs = dict(
            voice=self._opts.voice, response_format="pcm", mode="eager",
            chunk_words=self._opts.chunk_words, peek_words=self._opts.peek_words,
            sample_rate=self._tts._sample_rate, language=self._opts.language,
            # `speed` was missing here. Eager is the default mode, so a knob
            # absent from this call is a knob that silently does nothing for
            # almost every LiveKit agent — update_options(speed=...) appeared to
            # work and changed nothing.
            speed=self._opts.speed,
            pronunciation_dictionary_id=self._opts.pron_dict_id, **_SAMPLING,
        )

        # A socket the TTS opened ahead of time, while the user was still
        # speaking. The handshake is 124-143 ms warm and none of it depends on
        # the text, so paying it here — the instant the LLM starts producing —
        # is paying it at the one moment the caller is waiting.
        prepared = self._tts._take_prepared(kwargs)
        if prepared is not None:
            stream = prepared.stream(self._text_stream())
        else:
            stream = self._tts._client.speech.stream_input(self._text_stream(), **kwargs)

        try:
            async for audio in stream:
                output_emitter.push(audio)
        finally:
            # Open the next one now, so it is warm before the next turn begins.
            self._tts._arm_prepared(kwargs)

    async def _run_http(self, output_emitter: tts.AudioEmitter) -> None:
        buf = ""

        async def flush(final: bool) -> None:
            nonlocal buf
            while (m := _SENT_END.search(buf)) is not None:
                cut = m.end()
                sent = buf[:cut].strip()
                buf = buf[cut:]
                if sent:
                    await self._synth_one(sent, output_emitter)
            if final and buf.strip():
                await self._synth_one(buf.strip(), output_emitter)
                buf = ""

        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                await flush(final=True)
                continue
            if data:
                self._mark_started()
                buf += data
                await flush(final=False)
        await flush(final=True)

    async def _synth_one(self, text: str, output_emitter: tts.AudioEmitter) -> None:
        async for chunk in self._tts._client.speech.stream(
            input=text, voice=self._opts.voice, response_format="pcm",
            sample_rate=self._tts._sample_rate, language=self._opts.language,
            speed=self._opts.speed, extra_body=_pron_extra(self._opts.pron_dict_id),
            **_SAMPLING,
        ):
            output_emitter.push(chunk)


def _to_lk_error(e: SvaraError):
    """Svara's error → LiveKit's, with retryability LiveKit will act on.

    LiveKit retries ``APIConnectionError`` and retryable ``APIStatusError``
    through its ``conn_options``. A request the SDK rejected locally (too
    long, speed out of range) is not going to pass on the third try, and a
    quota that is spent stays spent.
    """
    if isinstance(e, InvalidRequestError):
        return APIStatusError(message=e.message, status_code=400, body=None, retryable=False)
    if e.status_code:
        retryable = bool(e.status_code == 429 or e.status_code >= 500) and not isinstance(
            e, QuotaExceededError)
        return APIStatusError(
            message=e.message, status_code=e.status_code, body=e.body, retryable=retryable
        )
    return APIConnectionError(e.message)
