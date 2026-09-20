"""LiveKit plugin: the eager path must carry every knob, and must prewarm.

Skipped when livekit-agents is not installed.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

pytest.importorskip("livekit.agents")

from svara.livekit import TTS  # noqa: E402


def _tts(**kw):
    return TTS(api_key="sk_test", base_url="https://example.invalid", **kw)


def _eager_source() -> str:
    import svara.livekit.tts as mod

    src = inspect.getsource(mod)
    return src.split("async def _run_eager")[1].split("async def _run_http")[0]


def test_eager_path_forwards_speed():
    """`speed` was absent from the eager call, so update_options(speed=...)
    silently did nothing for the default mode — which is what agents use."""
    assert "speed=self._opts.speed" in _eager_source(), "eager path drops speed"


@pytest.mark.parametrize("knob", ["voice", "language", "speed", "sample_rate",
                                  "pronunciation_dictionary_id"])
def test_eager_path_forwards_every_opt_the_http_path_does(knob):
    assert knob in _eager_source(), f"eager path drops {knob}"


def test_update_options_reaches_the_opts():
    t = _tts()
    t.update_options(speed=1.25, voice="sv_other")
    assert t._opts.speed == 1.25
    assert t._opts.voice == "sv_other"


def test_prewarm_is_a_noop_without_a_running_loop():
    """Called from sync setup code it must not explode, just do nothing."""
    t = _tts()
    t.prewarm()
    assert t._prepared is None


def test_prewarm_disabled_does_not_arm():
    async def go():
        t = _tts(prewarm=False)
        t.prewarm()
        assert t._prepare_task is None
    asyncio.run(go())


def test_prewarm_schedules_a_connect_when_enabled():
    async def go():
        t = _tts(prewarm=True)
        t.prewarm()
        assert t._prepare_task is not None
        # The host is unroutable, so the connect fails — and that must be
        # swallowed, because a failed prewarm is only a missed optimisation.
        await asyncio.gather(t._prepare_task, return_exceptions=True)
        assert t._prepared is None
        assert t._take_prepared() is None
    asyncio.run(go())


def test_http_mode_never_prewarms():
    async def go():
        t = _tts(mode="http")
        t.prewarm()
        assert t._prepare_task is None
    asyncio.run(go())


def test_plugin_identifies_itself_to_livekit():
    """LiveKit stamps these on traces and per-turn metrics; the base class
    answers 'unknown' unless a plugin overrides."""
    t = _tts()
    assert t.label == "svara.TTS"
    assert t.provider == "svara"
    assert t.model == "svara-tts-turbo"


def test_error_mapping_does_not_retry_bad_requests_or_spent_quota():
    from livekit.agents import APIConnectionError as LKConn
    from livekit.agents import APIStatusError as LKStatus

    from svara import InvalidRequestError, QuotaExceededError, RateLimitError, StreamInterruptedError
    from svara.livekit.tts import _to_lk_error

    e = _to_lk_error(InvalidRequestError("too long"))
    assert isinstance(e, LKStatus) and e.retryable is False and e.status_code == 400
    e = _to_lk_error(QuotaExceededError("spent", status_code=429, code="insufficient_quota"))
    assert isinstance(e, LKStatus) and e.retryable is False
    e = _to_lk_error(RateLimitError("busy", status_code=429))
    assert e.retryable is True
    assert isinstance(_to_lk_error(StreamInterruptedError("cut", frames=3)), LKConn)


def test_update_options_after_prewarm_discards_the_stale_socket():
    """The socket's URL fixed the voice at open time; a later update_options
    must not be served by it."""
    class FakePrepared:
        expired = False
        closed = False

        async def aclose(self):
            self.closed = True

    async def go():
        t = _tts()
        p = FakePrepared()
        t._prepared = p
        t._prepared_kwargs = {"voice": "sv_old"}
        assert t._take_prepared({"voice": "sv_new"}) is None
        await asyncio.sleep(0)
        assert p.closed and t._prepared is None
        p2 = FakePrepared()
        t._prepared = p2
        t._prepared_kwargs = {"voice": "sv_same"}
        assert t._take_prepared({"voice": "sv_same"}) is p2
    asyncio.run(go())
