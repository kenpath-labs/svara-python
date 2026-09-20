"""Svara — Python SDK for Kenpath Labs' multilingual text-to-speech API.

    from svara import Svara
    client = Svara(api_key="sk_live_...")
    audio = client.speech.create(input="नमस्ते! Welcome to Svara.", voice="sv_enhdbrj5")
    open("hello.mp3", "wb").write(audio)

For LiveKit voice agents, install the extra (``pip install "svara-voice[livekit]"``) and
use ``from svara.livekit import TTS``.
"""

from ._client import AsyncSpeechStream, AsyncSvara, PreparedStream, SpeechStream, Svara, default_ssl_context
from ._play import play
from ._version import __version__
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    InvalidRequestError,
    MissingAPIKeyError,
    NotFoundError,
    PermissionDeniedError,
    PermissionError_,
    QuotaExceededError,
    RateLimitError,
    StreamInterruptedError,
    SvaraError,
    UnprocessableEntityError,
)
from .types import (
    FLUSH,
    FORMAT_INFO,
    Alignment,
    ChunkEvent,
    Language,
    Model,
    PronunciationDictionary,
    PronunciationRule,
    RateLimitInfo,
    ResponseFormat,
    SampleRate,
    SpeechResponse,
    TimestampedAudio,
    Usage,
    Voice,
    output_format,
)

__all__ = [
    "Svara",
    "AsyncSvara",
    "PreparedStream",
    "SpeechStream",
    "AsyncSpeechStream",
    "SpeechResponse",
    "default_ssl_context",
    "output_format",
    "play",
    "FLUSH",
    "Voice",
    "Alignment",
    "TimestampedAudio",
    "Language",
    "Model",
    "PronunciationDictionary",
    "PronunciationRule",
    "Usage",
    "RateLimitInfo",
    "ChunkEvent",
    "ResponseFormat",
    "SampleRate",
    "FORMAT_INFO",
    "SvaraError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "UnprocessableEntityError",
    "PermissionDeniedError",
    "InternalServerError",
    "InvalidRequestError",
    "MissingAPIKeyError",
    "QuotaExceededError",
    "NotFoundError",
    "PermissionError_",
    "RateLimitError",
    "StreamInterruptedError",
    "__version__",
]
