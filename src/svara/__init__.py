"""Svara — Python SDK for Kenpath Labs' multilingual text-to-speech API.

    from svara import Svara
    client = Svara(api_key="sk_live_...")
    audio = client.speech.create(input="नमस्ते! Welcome to Svara.", voice="sv_enhdbrj5")
    open("hello.mp3", "wb").write(audio)

For LiveKit voice agents, install the extra (``pip install "svara[livekit]"``) and
use ``from svara.livekit import TTS``.
"""

from ._client import AsyncSvara, Svara
from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    RateLimitError,
    SvaraError,
)
from .types import FORMAT_INFO, ChunkEvent, ResponseFormat, Voice

__all__ = [
    "Svara",
    "AsyncSvara",
    "Voice",
    "ChunkEvent",
    "ResponseFormat",
    "FORMAT_INFO",
    "SvaraError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "AuthenticationError",
    "BadRequestError",
    "NotFoundError",
    "RateLimitError",
    "__version__",
]
