"""LiveKit Agents integration for Svara.

Install the extra::

    pip install "svara[livekit]"

Then drop Svara in as the TTS of any LiveKit ``AgentSession``::

    from svara.livekit import TTS
    session = AgentSession(tts=TTS(voice="sv_enhdbrj5", mode="eager"), stt=..., llm=...)
"""

try:
    from livekit.agents import tts as _lk_tts  # noqa: F401
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "svara.livekit requires livekit-agents. Install it with:\n"
        '    pip install "svara[livekit]"'
    ) from e

from .tts import DEFAULT_VOICE, SAMPLE_RATE, TTS

__all__ = ["TTS", "DEFAULT_VOICE", "SAMPLE_RATE"]
