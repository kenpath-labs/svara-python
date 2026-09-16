"""Types for the Svara SDK."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

# Exactly the values the /v1/audio/speech endpoint accepts for response_format
# (verified against the live OpenAPI document). ``ulaw``/``alaw`` are G.711.
ResponseFormat = Literal["mp3", "opus", "aac", "flac", "wav", "pcm", "ulaw", "alaw"]

#: The sample rates the server will render. Anything else is a 422. The codec is
#: 24 kHz native; every other rate is resampled server-side.
SampleRate = Literal[8000, 16000, 22050, 24000, 32000, 44100, 48000]
SAMPLE_RATES: Tuple[int, ...] = (8000, 16000, 22050, 24000, 32000, 44100, 48000)

#: Hard limits the server enforces on a request. Checked client-side too, so a
#: mistake fails before a network round trip rather than after one.
MAX_INPUT_CHARS = 5000
SPEED_RANGE = (0.7, 1.5)

# Container/rate facts per format, handy for wiring downstream sinks.
#
# ``default_rate`` is what the server actually returns when the request omits
# ``sample_rate`` — verified against production, see MEASUREMENTS.md.
#
# It is 24000 for **every** format, including the G.711 ones. That is worth
# stating loudly because it is the opposite of what the format names imply:
# G.711 is a telephony codec and is 8 kHz essentially everywhere else, so
# `ulaw` bytes handed straight to a SIP leg without asking for 8 kHz play at
# three times speed. This table used to claim 8000 for ulaw/alaw, and the
# telephony example relied on that claim.
#
# ``telephony_rate`` is the rate a phone leg wants; pass it as ``sample_rate``.
FORMAT_INFO: Dict[str, Dict[str, Any]] = {
    "mp3": {"content_type": "audio/mpeg", "container": True, "default_rate": 24000,
            "default_bitrate_kbps": 128},
    "opus": {"content_type": "audio/ogg", "container": True, "default_rate": 24000,
             "default_bitrate_kbps": 64},
    "aac": {"content_type": "audio/aac", "container": True, "default_rate": 24000,
            "default_bitrate_kbps": 96},
    "flac": {"content_type": "audio/flac", "container": True, "default_rate": 24000},
    "wav": {"content_type": "audio/wav", "container": True, "default_rate": 24000},
    "pcm": {"content_type": "audio/pcm", "container": False, "default_rate": 24000,
            "bytes_per_sample": 2},   # s16le
    "ulaw": {"content_type": "audio/basic", "container": False, "default_rate": 24000,
             "telephony_rate": 8000, "bytes_per_sample": 1},   # G.711 µ-law
    "alaw": {"content_type": "audio/basic", "container": False, "default_rate": 24000,
             "telephony_rate": 8000, "bytes_per_sample": 1},   # G.711 A-law
}

#: Formats that a phone network expects at 8 kHz. Requesting one of these
#: without an explicit ``sample_rate`` gets you 24 kHz — correct audio, wrong
#: clock for a SIP or PSTN leg.
TELEPHONY_FORMATS = ("ulaw", "alaw")

#: Formats whose ``bitrate_kbps`` the server honours.
LOSSY_FORMATS = ("mp3", "opus", "aac")


_OUTPUT_FORMAT_RE = re.compile(r"^(mp3|opus|aac|flac|wav|pcm|ulaw|alaw)(?:_(\d{4,5}))?(?:_(\d{1,3}))?$")


def output_format(name: str) -> Dict[str, Any]:
    """Translate an ElevenLabs-style output format into Svara request fields.

    ElevenLabs spells a format, its rate and (for lossy codecs) its bitrate as
    one string — ``pcm_24000``, ``ulaw_8000``, ``mp3_44100_128``. Svara takes
    them as three fields. This returns the three fields, ready to splat::

        client.speech.create(input=..., voice=..., **output_format("ulaw_8000"))

    Only the format is required: ``output_format("pcm")`` sets nothing but the
    format, and the server's defaults apply. Raises :class:`ValueError` on a
    string that names a rate the server cannot render.
    """
    m = _OUTPUT_FORMAT_RE.match(name.strip().lower())
    if m is None:
        raise ValueError(
            f"Unrecognised output format {name!r}. Expected <format>[_<rate>[_<kbps>]], "
            f"e.g. 'pcm_24000', 'ulaw_8000', 'mp3_44100_128'. Formats: {', '.join(FORMAT_INFO)}."
        )
    fmt, rate, kbps = m.group(1), m.group(2), m.group(3)
    out: Dict[str, Any] = {"response_format": fmt}
    if rate is not None:
        r = int(rate)
        if r not in SAMPLE_RATES:
            raise ValueError(f"sample rate {r} is not one of {SAMPLE_RATES}")
        out["sample_rate"] = r
    if kbps is not None:
        if fmt not in LOSSY_FORMATS:
            raise ValueError(f"{fmt!r} is not a lossy format; a bitrate does not apply")
        out["bitrate_kbps"] = int(kbps)
    return out


class _Flush:
    """Sentinel: yield it from a ``stream_input`` text source to flush.

    Everything the server has buffered is synthesised now rather than when the
    next word boundary or the end of the stream arrives. Useful at a paragraph
    or turn boundary in an LLM stream. ``svara.FLUSH`` is the only instance.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "svara.FLUSH"


FLUSH = _Flush()


@dataclass
class RateLimitInfo:
    """Remaining budget, from the ``x-ratelimit-remaining-*`` response headers.

    Attach to every successful response. ``None`` means the header was absent;
    ``-1`` is the server's way of saying "unlimited on this plan".
    """

    requests: Optional[int] = None
    streams: Optional[int] = None
    characters: Optional[int] = None

    @classmethod
    def from_headers(cls, headers: Optional[Mapping[str, str]]) -> RateLimitInfo:
        if not headers:
            return cls()

        def _int(k: str) -> Optional[int]:
            v = headers.get(k)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            requests=_int("x-ratelimit-remaining-requests"),
            streams=_int("x-ratelimit-remaining-streams"),
            characters=_int("x-ratelimit-remaining-characters"),
        )


class SpeechResponse(bytes):
    """The audio from :meth:`speech.create`, as bytes, plus the response metadata.

    It *is* ``bytes`` — write it to a file, hand it to a player, slice it —
    and it also carries what the server said about it: ``content_type``,
    ``sample_rate``, ``request_id`` and the remaining ``rate_limit`` budget.
    """

    headers: Mapping[str, str]

    def __new__(cls, data: bytes, headers: Optional[Mapping[str, str]] = None) -> SpeechResponse:
        obj = super().__new__(cls, data)
        obj.headers = headers or {}
        return obj

    @property
    def content_type(self) -> Optional[str]:
        return self.headers.get("content-type")

    @property
    def sample_rate(self) -> Optional[int]:
        """The rate the server rendered at (``x-sample-rate``)."""
        v = self.headers.get("x-sample-rate")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def request_id(self) -> Optional[str]:
        return self.headers.get("x-request-id")

    @property
    def rate_limit(self) -> RateLimitInfo:
        return RateLimitInfo.from_headers(self.headers)

    def save(self, path: str) -> str:
        """Write the audio to ``path`` and return the path."""
        with open(path, "wb") as f:
            f.write(self)
        return path

    # bytes.__repr__ would print the whole payload; summarise instead.
    def __repr__(self) -> str:
        return f"<SpeechResponse {len(self)} bytes, {self.content_type or 'unknown type'}>"

    __str__ = __repr__


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
    #: Server-side caveats about this voice's training data, if any.
    quality_warning: List[str] = field(default_factory=list)
    #: Hours of source audio behind the voice, when the server reports it.
    hours: Optional[float] = None
    labels: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def language(self) -> Optional[str]:
        """Best-effort ISO code from labels (``native_language_code``)."""
        return self.labels.get("native_language_code") or self.labels.get("native_language")

    @property
    def quality_band(self) -> Optional[str]:
        """The curation band (``A`` best) from labels, when present."""
        return self.labels.get("quality_band")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Voice:
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
            quality_warning=list(d.get("quality_warning") or []),
            hours=d.get("hours"),
            labels=d.get("labels") or {},
            raw=d,
        )


@dataclass
class Language:
    """A language from ``GET /v1/languages``.

    Any of ``iso1``, ``iso3``, ``name`` or an alias is accepted as the
    ``language`` argument on speech calls (BCP-47 tags like ``hi-IN`` too).
    """

    iso3: str
    name: str
    iso1: Optional[str] = None
    region: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Language:
        return cls(
            iso3=d.get("iso3", ""), name=d.get("name", ""), iso1=d.get("iso1"),
            region=d.get("region"), aliases=list(d.get("aliases") or []), raw=d,
        )


@dataclass
class Usage:
    """Plan, month-to-date usage and balance, from ``GET /v1/usage``.

    The nested dictionaries are the server's own shapes (``plan``, ``month``,
    ``balance``, ``subscription``); the properties pull out the numbers most
    callers want. See https://docs.kenpathlabs.com/usage.
    """

    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def plan(self) -> Dict[str, Any]:
        return self.raw.get("plan") or {}

    @property
    def month(self) -> Dict[str, Any]:
        return self.raw.get("month") or {}

    @property
    def balance(self) -> Dict[str, Any]:
        return self.raw.get("balance") or {}

    @property
    def subscription(self) -> Optional[Dict[str, Any]]:
        return self.raw.get("subscription")

    @property
    def plan_id(self) -> Optional[str]:
        return self.plan.get("id")

    @property
    def characters_used(self) -> Optional[int]:
        return self.month.get("characters_used")

    @property
    def characters_remaining(self) -> Optional[int]:
        return self.balance.get("characters_remaining")

    @property
    def max_concurrent_streams(self) -> Optional[int]:
        return self.plan.get("max_concurrent_streams")

    @property
    def requests_per_minute(self) -> Optional[int]:
        return self.plan.get("requests_per_minute")


@dataclass
class Alignment:
    """Character timings for a clip, from the with-timestamps endpoints.

    Parallel lists: ``characters[i]`` is spoken from ``start_times[i]`` to
    ``end_times[i]`` seconds. The server times each synthesis chunk exactly
    and spreads its characters uniformly inside it, so treat these as
    word-level accurate and character-level approximate — right for karaoke
    highlighting and subtitles, not for phoneme work.
    """

    characters: List[str] = field(default_factory=list)
    start_times: List[float] = field(default_factory=list)
    end_times: List[float] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Alignment:
        if not d:
            return cls()
        return cls(
            characters=list(d.get("characters") or []),
            start_times=list(d.get("character_start_times_seconds") or []),
            end_times=list(d.get("character_end_times_seconds") or []),
        )

    @property
    def text(self) -> str:
        return "".join(self.characters)

    @property
    def duration(self) -> float:
        return self.end_times[-1] if self.end_times else 0.0

    def words(self) -> List[Tuple[str, float, float]]:
        """``(word, start, end)`` triples, split on whitespace."""
        out: List[Tuple[str, float, float]] = []
        word, start, end = "", 0.0, 0.0
        for ch, s, e in zip(self.characters, self.start_times, self.end_times):
            if ch.isspace():
                if word:
                    out.append((word, start, end))
                word = ""
                continue
            if not word:
                start = s
            word += ch
            end = e
        if word:
            out.append((word, start, end))
        return out

    def extend(self, other: Alignment) -> None:
        self.characters.extend(other.characters)
        self.start_times.extend(other.start_times)
        self.end_times.extend(other.end_times)


@dataclass
class TimestampedAudio:
    """One chunk (streaming) or the whole clip (non-streaming) with its timings."""

    audio: bytes
    alignment: Alignment
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkEvent:
    """A text/lookahead event from the eager input-streaming WebSocket."""

    text: str
    peek: Optional[str] = None
