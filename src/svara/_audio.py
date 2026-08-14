"""Client-side gain for the raw audio formats.

Volume has two possible owners, and only one of them may apply it. If the
server scales the samples *and* we scale them again, ``volume=1.4`` lands as
1.96x with clipping on the peaks — a bug that is inaudible on quiet text and
obvious on loud text, which is the worst way for a bug to behave. So the two
paths here are mutually exclusive by construction, selected by
``volume_mode``; see :data:`SERVER_APPLIES_VOLUME`.

Scaling is exact for all three raw formats:

* ``pcm``   — s16le. Multiply and clamp.
* ``ulaw``  — G.711 mu-law. One byte per sample, so a gain is a pure 256-entry
  byte mapping: decode, scale, re-encode once per distinct gain, then
  ``bytes.translate``. No arithmetic in the hot path at all.
* ``alaw``  — G.711 A-law. Same trick, different companding curve.

Container formats (mp3/opus/aac/flac/wav) would have to be decoded and
re-encoded to scale, which this SDK will not do silently — the caller is told
instead. ``wav`` is PCM in a RIFF wrapper, but scaling it here would also scale
the 44-byte header, so it is treated as a container.

The G.711 conversions are the CCITT reference implementation. ``audioop`` would
have supplied them, but it was removed in Python 3.13 and this package supports
3.9+.
"""

from __future__ import annotations

import array
import sys
import time
from typing import Dict, Optional, Tuple

#: Whether the Svara API applies ``volume`` server-side.
#:
#: **This is the only line to change the day the platform announces support.**
#: While it is False, ``volume_mode="auto"`` scales locally and does not send
#: the field; when it flips to True, auto sends the field and does not scale.
#: One of the two, never both — see the module docstring.
SERVER_APPLIES_VOLUME = False

#: Formats whose samples can be scaled without a decoder.
SCALABLE_FORMATS = ("pcm", "ulaw", "alaw")

VOLUME_MIN = 0.0
VOLUME_MAX = 2.0


def check_volume(volume: Optional[float]) -> None:
    """Reject out-of-range gain before it reaches the wire or the samples."""
    if volume is None:
        return
    if not isinstance(volume, (int, float)) or isinstance(volume, bool):
        raise ValueError(f"volume must be a number, got {type(volume).__name__}")
    if not VOLUME_MIN <= float(volume) <= VOLUME_MAX:
        raise ValueError(
            f"volume must be between {VOLUME_MIN} and {VOLUME_MAX} "
            f"(1.0 = unchanged), got {volume}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# G.711 (CCITT reference)
# ─────────────────────────────────────────────────────────────────────────────
_ULAW_BIAS = 0x84
_ULAW_CLIP = 8159
_ULAW_SEG_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _segment(value: int, table: Tuple[int, ...]) -> int:
    for i, end in enumerate(table):
        if value <= end:
            return i
    return len(table)


def ulaw_to_linear(u_val: int) -> int:
    """One mu-law byte -> signed 16-bit sample."""
    u_val = ~u_val & 0xFF
    t = ((u_val & 0x0F) << 3) + _ULAW_BIAS
    t <<= (u_val & 0x70) >> 4
    return (_ULAW_BIAS - t) if (u_val & 0x80) else (t - _ULAW_BIAS)


def linear_to_ulaw(pcm_val: int) -> int:
    """Signed 16-bit sample -> one mu-law byte."""
    pcm_val >>= 2                       # mu-law operates on 14 bits
    if pcm_val < 0:
        pcm_val, mask = -pcm_val, 0x7F
    else:
        mask = 0xFF
    if pcm_val > _ULAW_CLIP:
        pcm_val = _ULAW_CLIP
    pcm_val += _ULAW_BIAS >> 2
    seg = _segment(pcm_val, _ULAW_SEG_END)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm_val >> (seg + 1)) & 0x0F)) ^ mask


def alaw_to_linear(a_val: int) -> int:
    """One A-law byte -> signed 16-bit sample."""
    a_val ^= 0x55
    t = (a_val & 0x0F) << 4
    seg = (a_val & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if (a_val & 0x80) else -t


def linear_to_alaw(pcm_val: int) -> int:
    """Signed 16-bit sample -> one A-law byte."""
    pcm_val >>= 3                       # A-law operates on 13 bits
    if pcm_val >= 0:
        mask = 0xD5
    else:
        mask, pcm_val = 0x55, -pcm_val - 1
    seg = _segment(pcm_val, _ALAW_SEG_END)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= (pcm_val >> 1) & 0x0F if seg < 2 else (pcm_val >> seg) & 0x0F
    return aval ^ mask


_DECODE = {"ulaw": ulaw_to_linear, "alaw": alaw_to_linear}
_ENCODE = {"ulaw": linear_to_ulaw, "alaw": linear_to_alaw}

# (format, gain) -> (translate table, clip-marker table). Built once per
# distinct gain; a caller sweeping volume over a slider will build a handful.
_TABLES: Dict[Tuple[str, float], Tuple[bytes, bytes]] = {}


def _companded_tables(fmt: str, gain: float) -> Tuple[bytes, bytes]:
    """Byte->byte gain map for a companded format, plus a 0/1 clip map.

    The clip map lets clipping be counted with a second ``translate`` and a
    ``sum`` — both C-speed — instead of a Python loop over every sample.
    """
    key = (fmt, gain)
    cached = _TABLES.get(key)
    if cached is not None:
        return cached
    decode, encode = _DECODE[fmt], _ENCODE[fmt]
    out = bytearray(256)
    clip = bytearray(256)
    for code in range(256):
        original = decode(code)
        scaled = int(original * gain)
        if scaled > 32767:
            scaled, clip[code] = 32767, 1
        elif scaled < -32768:
            scaled, clip[code] = -32768, 1
        # Leave a code alone when the gain didn't move its sample. Without
        # this, mu-law's negative zero (0x7F) re-encodes to positive zero
        # (0xFF) — the same silence, but a byte we had no reason to rewrite,
        # and a unity-gain table that isn't the identity.
        out[code] = code if scaled == original else encode(scaled)
    tables = (bytes(out), bytes(clip))
    _TABLES[key] = tables
    return tables


def _scale_pcm16(data: bytes, gain: float) -> Tuple[bytes, int]:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":       # the wire format is little-endian
        samples.byteswap()
    clipped = 0
    for i, s in enumerate(samples):
        v = int(s * gain)
        if v > 32767:
            v, clipped = 32767, clipped + 1
        elif v < -32768:
            v, clipped = -32768, clipped + 1
        samples[i] = v
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes(), clipped


class Gain:
    """Applies a fixed gain to a stream of audio bytes.

    Stateful for one reason: ``pcm`` is two bytes per sample and a chunk
    boundary can land between them. A stray byte is held back and prepended to
    the next chunk rather than being scaled as if it were a whole sample, which
    would produce a click on every chunk boundary.
    """

    __slots__ = ("gain", "response_format", "clipped_samples", "seconds", "_carry")

    def __init__(self, gain: float, response_format: str) -> None:
        if response_format not in SCALABLE_FORMATS:
            raise ValueError(
                f"volume cannot be applied client-side to {response_format!r}: it "
                f"would need a decoder. Use one of {', '.join(SCALABLE_FORMATS)}, "
                f'or pass volume_mode="server" to let the API handle it.'
            )
        self.gain = float(gain)
        self.response_format = response_format
        self.clipped_samples = 0
        #: Wall time spent scaling, so the timeline can bill it honestly.
        self.seconds = 0.0
        self._carry = b""

    @property
    def is_identity(self) -> bool:
        return self.gain == 1.0

    def apply(self, data: bytes) -> bytes:
        if not data or self.is_identity:
            return data
        t0 = time.perf_counter()
        try:
            if self.response_format == "pcm":
                buf = self._carry + data
                if len(buf) % 2:
                    buf, self._carry = buf[:-1], buf[-1:]
                else:
                    self._carry = b""
                if not buf:
                    return b""
                out, clipped = _scale_pcm16(buf, self.gain)
                self.clipped_samples += clipped
                return out
            table, clip = _companded_tables(self.response_format, self.gain)
            self.clipped_samples += sum(data.translate(clip))
            return data.translate(table)
        finally:
            self.seconds += time.perf_counter() - t0

    def flush(self) -> bytes:
        """Emit a held-back odd byte at end of stream.

        An odd byte count means the stream was truncated mid-sample, so there
        is nothing sensible to scale it by — it is returned untouched rather
        than dropped, leaving the caller's byte count intact.
        """
        out, self._carry = self._carry, b""
        return out
