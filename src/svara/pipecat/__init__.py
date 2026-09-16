"""Pipecat integration for Svara.

Install the extra::

    pip install "svara-voice[pipecat]"

Use Svara as the TTS service in a Pipecat pipeline::

    from svara.pipecat import SvaraTTSService
    tts = SvaraTTSService(voice="sv_enhdbrj5")          # reads SVARA_API_KEY
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])

Targets pipecat-ai's ``TTSService`` contract as of 0.0.105+ (the ``Settings``
API and ``run_tts(text, context_id)``): Pipecat aggregates the LLM output into
sentences and calls :meth:`run_tts` per sentence; the service streams PCM (or
G.711 for telephony transports) back as ``TTSAudioRawFrame``. Pipecat pushes
the started/stopped frames and runs the TTFB clock itself.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from pipecat.services.tts_service import TTSService
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "svara.pipecat requires pipecat-ai >= 0.0.105. Install it with:\n"
        '    pip install "svara-voice[pipecat]"'
    ) from e

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame  # noqa: E402
from pipecat.services.settings import TTSSettings  # noqa: E402
from pipecat.transcriptions.language import Language  # noqa: E402
from pipecat.utils.types import NOT_GIVEN, NotGiven, is_given  # noqa: E402

from .._client import DEFAULT_MODEL, AsyncSvara  # noqa: E402
from ..exceptions import SvaraError  # noqa: E402

DEFAULT_VOICE = "sv_enhdbrj5"

#: Same platform-certified sampling the LiveKit plugin pins, so the service
#: sounds like the API it fronts.
_SAMPLING = dict(temperature=1.2, top_p=0.9, top_k=40, repetition_penalty=1.1, presence_penalty=0)


@dataclass
class SvaraTTSSettings(TTSSettings):
    """Runtime-updatable settings for :class:`SvaraTTSService`.

    ``voice``, ``model`` and ``language`` come from Pipecat's ``TTSSettings``;
    change any of them mid-call with ``TTSUpdateSettingsFrame``.

    Parameters:
        speed: Speaking speed, 0.7–1.5, pitch preserved.
        pronunciation_dictionary_id: Respelling rules created in the console.
    """

    speed: float | None | NotGiven = field(default_factory=lambda: NOT_GIVEN)
    pronunciation_dictionary_id: str | None | NotGiven = field(default_factory=lambda: NOT_GIVEN)


class SvaraTTSService(TTSService):
    """Svara as a Pipecat ``TTSService`` (HTTP streaming, one call per sentence).

    Always streams 16-bit mono PCM, at the transport's output rate: Pipecat's
    audio frames assume 16-bit samples for their duration bookkeeping, and the
    telephony serializers (Twilio, Plivo, Telnyx …) do the G.711 companding
    themselves. So a phone transport with ``audio_out_sample_rate=8000`` gets
    8 kHz PCM from the server — resampled there, not in Python — and the
    serializer turns it into µ-law on the way out. Asking Svara for ``ulaw``
    here would be companded twice.
    """

    Settings = SvaraTTSSettings
    _settings: SvaraTTSSettings

    def __init__(
        self,
        *,
        voice: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        language: Optional[str] = None,
        speed: Optional[float] = None,
        pronunciation_dictionary_id: Optional[str] = None,
        sample_rate: Optional[int] = None,
        client: Optional[AsyncSvara] = None,
        settings: Optional[SvaraTTSSettings] = None,
        **kwargs: Any,
    ) -> None:
        """
        Args:
            voice: Voice id from ``GET /v1/voices``. Defaults to Aanya.
            api_key: Svara API key; falls back to ``SVARA_API_KEY``.
            base_url: Gateway origin; falls back to ``SVARA_BASE_URL``.
            model: Accepted and ignored by the API today; kept for parity.
            language: Force a language (``"hi"``, ``"hi-IN"``, ``"hindi"``).
                Omit to auto-detect from the script.
            speed: Speaking speed, 0.7–1.5.
            pronunciation_dictionary_id: Respelling rules from the console.
            sample_rate: Output rate. ``None`` (recommended) follows the
                transport's ``audio_out_sample_rate`` — 8000 on a phone
                transport, 24000 for WebRTC — and the server renders at that
                rate so nothing resamples in the pipeline.
            client: An :class:`AsyncSvara` to reuse (shares its connection pool
                and keeps it warm across services). One is built otherwise.
            settings: Pipecat-style settings delta; wins over the direct
                arguments where both are given.
            **kwargs: Passed to ``TTSService``.
        """
        defaults = self.Settings(
            model=model or DEFAULT_MODEL,
            voice=voice or DEFAULT_VOICE,
            language=language,
            speed=speed,
            pronunciation_dictionary_id=pronunciation_dictionary_id,
        )
        if settings is not None:
            defaults.apply_update(settings)
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=defaults,
            **kwargs,
        )
        self._requested_rate = sample_rate
        self._owns_client = client is None
        self._client = client or AsyncSvara(api_key=api_key, base_url=base_url)

    def can_generate_metrics(self) -> bool:
        return True

    def language_to_service_language(self, language: Language) -> str | None:
        # Svara accepts BCP-47 tags and bare ISO codes alike.
        return language.value

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        voice = self._settings.voice if is_given(self._settings.voice) else DEFAULT_VOICE
        language = self._settings.language if is_given(self._settings.language) else None
        speed = self._settings.speed if is_given(self._settings.speed) else None
        pron = (self._settings.pronunciation_dictionary_id
                if is_given(self._settings.pronunciation_dictionary_id) else None)
        model = self._settings.model if is_given(self._settings.model) else DEFAULT_MODEL
        # Pipecat resolves the rate at StartFrame (ours, else the transport's
        # audio_out_sample_rate). Before that it reads 0; fall back to what the
        # constructor asked for, and otherwise let the server pick (24 kHz).
        rate = self.sample_rate or self._requested_rate or None
        frame_rate = rate or 24000
        try:
            await self.start_tts_usage_metrics(text)
            # chunk_size is left unset: the SDK yields each block as it arrives,
            # which is what a realtime transport wants. Fixing a size here would
            # only hold audio back until enough had accumulated.
            async for chunk in self._client.speech.stream(
                input=text, voice=voice, model=model or DEFAULT_MODEL,
                response_format="pcm", sample_rate=rate,
                language=language, speed=speed,
                pronunciation_dictionary_id=pron, **_SAMPLING,
            ):
                await self.stop_ttfb_metrics()
                yield TTSAudioRawFrame(chunk, frame_rate, 1, context_id=context_id)
        except SvaraError as e:
            yield ErrorFrame(error=f"svara tts error: {e}")

    async def stop(self, frame) -> None:  # pragma: no cover - lifecycle
        await super().stop(frame)
        if self._owns_client:
            await self._client.aclose()

    async def cancel(self, frame) -> None:  # pragma: no cover - lifecycle
        await super().cancel(frame)
        if self._owns_client:
            await self._client.aclose()


__all__ = ["SvaraTTSService", "SvaraTTSSettings"]
