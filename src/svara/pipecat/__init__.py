"""Pipecat integration for Svara.

Install the extra::

    pip install "svara-voice[pipecat]"

Use Svara as the TTS service in a Pipecat pipeline::

    from svara.pipecat import SvaraTTSService
    tts = SvaraTTSService(voice="sv_enhdbrj5")          # reads SVARA_API_KEY
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])

Targets pipecat-ai's ``TTSService`` contract: Pipecat's base aggregates the LLM
output into sentences and calls :meth:`run_tts` per sentence; we stream PCM (or
µ-law for telephony transports) back as ``TTSAudioRawFrame``.

.. note:: Beta — validate against your pinned pipecat-ai version.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Optional

try:  # base class moved between pipecat versions
    from pipecat.services.tts_service import TTSService
except ImportError:  # pragma: no cover
    try:
        from pipecat.services.ai_services import TTSService  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "svara.pipecat requires pipecat-ai. Install it with:\n"
            '    pip install "svara-voice[pipecat]"'
        ) from e

from pipecat.frames.frames import (  # noqa: E402
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)

from .._client import AsyncSvara  # noqa: E402
from ..exceptions import SvaraError  # noqa: E402
from ..types import ResponseFormat  # noqa: E402

DEFAULT_VOICE = "sv_enhdbrj5"


class SvaraTTSService(TTSService):
    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "svara-1",
        language: Optional[str] = None,
        speed: Optional[float] = None,
        response_format: ResponseFormat = "pcm",
        sample_rate: int = 24000,
        **kwargs,
    ) -> None:
        # Telephony transports use 8 kHz µ-law; pass response_format="ulaw",
        # sample_rate=8000 to match without resampling.
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._client = AsyncSvara(api_key=api_key, base_url=base_url)
        self._voice = voice
        self._model = model
        self._language = language
        self._speed = speed
        self._format: ResponseFormat = response_format
        self._rate = sample_rate

    def can_generate_metrics(self) -> bool:
        return True

    async def set_voice(self, voice: str) -> None:
        self._voice = voice

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        yield TTSStartedFrame()
        try:
            if hasattr(self, "start_ttfb_metrics"):
                await self.start_ttfb_metrics()
            first = True
            async for chunk in self._client.speech.stream(
                input=text, voice=self._voice, model=self._model,
                response_format=self._format, sample_rate=self._rate,
                language=self._language, speed=self._speed,
            ):
                if first and hasattr(self, "stop_ttfb_metrics"):
                    await self.stop_ttfb_metrics()
                    first = False
                yield TTSAudioRawFrame(audio=chunk, sample_rate=self._rate, num_channels=1)
        except SvaraError as e:
            yield ErrorFrame(f"svara tts error: {e}")
        finally:
            yield TTSStoppedFrame()

    async def stop(self, frame) -> None:  # pragma: no cover - lifecycle
        await super().stop(frame)
        await self._client.aclose()


__all__ = ["SvaraTTSService"]
