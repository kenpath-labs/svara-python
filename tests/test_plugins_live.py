"""The framework plugins, driven by their frameworks, against the real API.

Skipped unless SVARA_API_KEY is set and the framework is installed. These are
the tests that caught what unit tests could not: a Pipecat import that only
fails on the oldest supported release, and sockets prewarmed for a voice the
agent had since changed.

    SVARA_API_KEY=sk_live_... pytest tests/test_plugins_live.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SVARA_API_KEY"), reason="set SVARA_API_KEY to run live tests"
)

VOICE = os.environ.get("SVARA_TEST_VOICE", "sv_enhdbrj5")
OTHER_VOICE = os.environ.get("SVARA_TEST_VOICE_2", "sv_84sb2v3w")


# ── LiveKit: a real AgentSession, a scripted LLM, audio captured at the output ─

def test_livekit_agent_session_speaks_through_svara():
    pytest.importorskip("livekit.agents")
    from livekit.agents import Agent, AgentSession, APIConnectOptions, llm
    from livekit.agents.voice import io as vio

    from svara.livekit import TTS

    class ScriptedLLM(llm.LLM):
        def chat(self, *, chat_ctx, tools=None, conn_options=None, **kw):
            return _Stream(self, chat_ctx=chat_ctx, tools=tools or [],
                           conn_options=conn_options or APIConnectOptions())

    class _Stream(llm.LLMStream):
        async def _run(self):
            reply = "Sure. The quickest route is the metro, then a ten minute walk."
            for i, w in enumerate(reply.split(" ")):
                await asyncio.sleep(0.02)
                self._event_ch.send_nowait(llm.ChatChunk(
                    id=f"c{i}", delta=llm.ChoiceDelta(role="assistant", content=w + " ")))

    class Capture(vio.AudioOutput):
        def __init__(self):
            kw = {}
            if hasattr(vio, "AudioOutputCapabilities"):   # required from livekit-agents 1.7
                kw["capabilities"] = vio.AudioOutputCapabilities(pause=False)
            super().__init__(label="capture", next_in_chain=None, sample_rate=24000, **kw)
            self.seconds = 0.0
            self.rates = set()

        async def capture_frame(self, frame):
            await super().capture_frame(frame)
            self.seconds += frame.duration
            self.rates.add(frame.sample_rate)

        def flush(self):
            super().flush()
            self.on_playback_finished(playback_position=self.seconds, interrupted=False)

        def clear_buffer(self):
            pass

    async def go():
        tts = TTS(voice=VOICE)
        session = AgentSession(llm=ScriptedLLM(), tts=tts)
        cap = Capture()
        session.output.audio = cap
        await session.start(Agent(instructions="test"))
        try:
            await session.generate_reply(user_input="how do I get there?")   # LLM tokens → eager WS
            eager = cap.seconds
            await session.say("Thank you for calling.")                       # fixed text → HTTP
            said = cap.seconds - eager
            tts.update_options(voice=OTHER_VOICE, speed=1.2)                  # must not reuse the old socket
            await session.generate_reply(user_input="once more")
            after = cap.seconds - eager - said
        finally:
            await session.aclose()
            await tts.aclose()
        assert eager > 1.0 and said > 0.5 and after > 1.0
        assert cap.rates == {24000}

    asyncio.run(go())


# ── Pipecat: a real Pipeline; LLM-style frames in, audio frames out ───────────

def test_pipecat_pipeline_speaks_through_svara():
    pytest.importorskip("pipecat")
    import pipecat.frames.frames as F
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.frame_processor import FrameProcessor

    from svara.pipecat import SvaraTTSService, SvaraTTSSettings

    class Sink(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.audio = 0
            self.rates = set()
            self.errors = []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, F.TTSAudioRawFrame):
                self.audio += len(frame.audio)
                self.rates.add(frame.sample_rate)
            elif isinstance(frame, F.ErrorFrame):
                self.errors.append(str(frame.error))
            await self.push_frame(frame, direction)

    async def run(tts, frames, rate):
        down, up = Sink(), Sink()   # ErrorFrames travel upstream
        task = PipelineTask(Pipeline([up, tts, down]),
                            params=PipelineParams(audio_out_sample_rate=rate))
        await task.queue_frames(frames + [F.EndFrame()])
        await PipelineRunner(handle_sigint=False).run(task)
        down.errors += up.errors
        return down

    async def go():
        turn = [F.LLMFullResponseStartFrame()]
        turn += [F.LLMTextFrame(w + " ") for w in "Sure, I can help. The metro is quickest.".split(" ")]
        turn += [F.LLMFullResponseEndFrame()]
        s = await run(SvaraTTSService(voice=VOICE), turn, 24000)
        assert s.audio / 2 / 24000 > 1.0 and s.rates == {24000} and not s.errors

        # A phone transport: PCM at 8 kHz, which the serializer compands. Never ulaw.
        s = await run(SvaraTTSService(voice=VOICE), [F.TTSSpeakFrame("Your appointment is confirmed.")], 8000)
        assert s.audio / 2 / 8000 > 0.8 and s.rates == {8000} and not s.errors

        tts = SvaraTTSService(voice=VOICE)
        try:
            upd = F.TTSUpdateSettingsFrame(delta=SvaraTTSSettings(voice=OTHER_VOICE, speed=1.3))
        except TypeError:
            upd = F.TTSUpdateSettingsFrame(settings={"voice": OTHER_VOICE, "speed": 1.3})
        s = await run(tts, [F.TTSSpeakFrame("Before."), upd, F.TTSSpeakFrame("After, in another voice.")], 24000)
        assert not s.errors and tts._settings.voice == OTHER_VOICE and tts._settings.speed == 1.3

        s = await run(SvaraTTSService(voice="sv_doesnotexist"), [F.TTSSpeakFrame("Cannot be spoken.")], 24000)
        assert any("not found" in e.lower() for e in s.errors), "a bad voice must surface as an ErrorFrame"

    asyncio.run(go())
