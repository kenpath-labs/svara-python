"""Types for the Svara SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from typing import Literal
except ImportError:  # pragma: no cover - py<3.8
    from typing_extensions import Literal  # type: ignore

# Exactly the values the /v1/audio/speech endpoint accepts for response_format
# (verified against the live API). ``ulaw``/``alaw`` are 8 kHz G.711 for telephony.
ResponseFormat = Literal["mp3", "opus", "aac", "flac", "wav", "pcm", "ulaw", "alaw"]

# Container/rate facts per format, handy for wiring downstream sinks. ``None``
# rate = caller-selectable via ``sample_rate`` (server default in parens).
FORMAT_INFO: Dict[str, Dict[str, Any]] = {
    "mp3": {"content_type": "audio/mpeg", "container": True, "default_rate": 24000},
    "opus": {"content_type": "audio/ogg", "container": True, "default_rate": 24000},
    "aac": {"content_type": "audio/aac", "container": True, "default_rate": 24000},
    "flac": {"content_type": "audio/flac", "container": True, "default_rate": 24000},
    "wav": {"content_type": "audio/wav", "container": True, "default_rate": 24000},
    "pcm": {"content_type": "audio/pcm", "container": False, "default_rate": 24000},   # s16le
    "ulaw": {"content_type": "audio/basic", "container": False, "default_rate": 8000},  # G.711 µ-law
    "alaw": {"content_type": "audio/basic", "container": False, "default_rate": 8000},  # G.711 A-law
}


@dataclass
class Voice:
    """A voice from ``GET /v1/voices``."""

    voice_id: str
    name: Optional[str] = None
    gender: Optional[str] = None
    accent_family: Optional[str] = None
    description: Optional[str] = None
    model_id: Optional[str] = None
    category: Optional[str] = None
    curated: bool = False
    is_default: bool = False
    preview_url: Optional[str] = None
    labels: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def language(self) -> Optional[str]:
        """Best-effort ISO code from labels (``native_language_code``)."""
        return self.labels.get("native_language_code") or self.labels.get("native_language")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Voice":
        return cls(
            voice_id=d.get("voice_id") or d.get("id"),
            name=d.get("name"),
            gender=d.get("gender"),
            accent_family=d.get("accent_family"),
            description=d.get("description"),
            model_id=d.get("model_id"),
            category=d.get("category"),
            curated=bool(d.get("curated", False)),
            is_default=bool(d.get("is_default", False)),
            preview_url=d.get("preview_url"),
            labels=d.get("labels") or {},
            raw=d,
        )


@dataclass
class ChunkEvent:
    """A text/lookahead event from the eager input-streaming WebSocket."""

    text: str
    peek: Optional[str] = None
