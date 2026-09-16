"""Pipecat service: the contract pipecat-ai 0.0.105+ actually calls.

Skipped when pipecat-ai is not installed.
"""
from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame  # noqa: E402

from svara import AsyncSvara  # noqa: E402
from svara.pipecat import SvaraTTSService, SvaraTTSSettings  # noqa: E402


def _svc(handler, **kw) -> SvaraTTSService:
    client = AsyncSvara(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return SvaraTTSService(client=client, **kw)


def test_run_tts_has_the_context_id_signature():
    """pipecat calls run_tts(text, context_id); the old two-arg form breaks."""
    params = list(inspect.signature(SvaraTTSService.run_tts).parameters)
    assert params == ["self", "text", "context_id"]


def test_run_tts_yields_audio_frames_tagged_with_the_context():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"\x00" * 3840, headers={"content-type": "audio/pcm"})

    async def go():
        svc = _svc(handler, voice="sv_x", sample_rate=24000, language="hi", speed=1.2,
                   pronunciation_dictionary_id="pd_1")
        frames = [f async for f in svc.run_tts("hello", "ctx-1")]
        assert frames and all(isinstance(f, TTSAudioRawFrame) for f in frames)
        assert frames[0].context_id == "ctx-1"
        assert frames[0].sample_rate == 24000 and frames[0].num_channels == 1
        assert b"".join(f.audio for f in frames) == b"\x00" * 3840
    asyncio.run(go())
    assert seen["voice"] == "sv_x" and seen["lang"] == "hi" and seen["speed"] == 1.2
    assert seen["pronunciation_dictionary_id"] == "pd_1" and seen["stream"] is True
    assert seen["temperature"] == 1.2  # certified sampling pinned


def test_errors_become_error_frames_not_exceptions():
    async def go():
        svc = _svc(lambda r: httpx.Response(401, json={"detail": {
            "status": "invalid_api_key", "message": "bad"}}), sample_rate=24000)
        frames = [f async for f in svc.run_tts("hello", "ctx")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert "invalid_api_key" in frames[0].error
    asyncio.run(go())


def test_settings_drive_the_request():
    """TTSUpdateSettingsFrame(voice=...) lands in _update_settings; run_tts must read
    the settings, not constructor-time copies."""
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"\x00" * 2)

    async def go():
        svc = _svc(handler, voice="sv_old", sample_rate=8000)
        await svc._update_settings(SvaraTTSSettings(voice="sv_new", speed=0.9))
        [f async for f in svc.run_tts("x", "c")]
    asyncio.run(go())
    assert seen["voice"] == "sv_new" and seen["speed"] == 0.9 and seen["sample_rate"] == 8000


def test_telephony_construction_passes_ulaw_and_rate():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=b"\xff" * 160)

    async def go():
        svc = _svc(handler, response_format="ulaw", sample_rate=8000)
        frames = [f async for f in svc.run_tts("x", "c")]
        assert frames[0].sample_rate == 8000
    asyncio.run(go())
    assert seen["response_format"] == "ulaw" and seen["sample_rate"] == 8000


def test_pipecat_language_enum_maps_to_a_tag():
    from pipecat.transcriptions.language import Language

    svc = _svc(lambda r: httpx.Response(200), sample_rate=24000)
    assert svc.language_to_service_language(Language.HI) == Language.HI.value
