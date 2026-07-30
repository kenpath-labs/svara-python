"""Minimal LiveKit voice agent using Svara as the TTS.

Install:  pip install "svara[livekit]" livekit-plugins-openai livekit-plugins-silero
Run:      SVARA_API_KEY=... OPENAI_API_KEY=... python examples/livekit_agent.py dev
"""

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess
from livekit.plugins import openai, silero

from svara.livekit import TTS as SvaraTTS


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer(setup_fnc=prewarm)


@server.rtc_session(agent_name="svara-agent")
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=openai.STT(model="gpt-4o-transcribe"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=SvaraTTS(voice="sv_enhdbrj5", mode="eager"),  # ← Svara
    )
    await session.start(Agent(instructions="You are a helpful voice assistant."), room=ctx.room)
    await session.generate_reply(instructions="Greet the caller warmly in one sentence.")


if __name__ == "__main__":
    agents.cli.run_app(server)
