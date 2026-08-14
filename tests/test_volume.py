"""Volume tests — gain maths, mode routing, and the wire format.

No network and no API key: the G.711 tables are pure functions, and the mode
routing is decided before a request is built.

The property under test throughout is that **exactly one party scales the
samples**. A gain applied twice is the failure mode that matters here: it is
inaudible on quiet text, obvious on loud text, and looks like a model
regression rather than a client bug.
"""

from __future__ import annotations

import struct

import pytest

from svara import Gain
from svara._audio import (
    SCALABLE_FORMATS,
    _companded_tables,
    alaw_to_linear,
    check_volume,
    linear_to_alaw,
    linear_to_ulaw,
    ulaw_to_linear,
)
from svara._client import _resolve_volume, _speech_payload, _ws_url


# ── G.711 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fmt", ["ulaw", "alaw"])
def test_g711_round_trips_every_code(fmt):
    """decode -> encode must return the byte it started from, for all 256.

    A single wrong entry is a burst of noise on whichever amplitude it maps,
    which is very hard to hear in a unit test and very easy to hear on a call.
    """
    decode = ulaw_to_linear if fmt == "ulaw" else alaw_to_linear
    encode = linear_to_ulaw if fmt == "ulaw" else linear_to_alaw
    # mu-law is the one codec with two zeros: 0x7F is negative zero and 0xFF is
    # positive zero. Both decode to amplitude 0 and re-encode to 0xFF, so the
    # round-trip is exact everywhere except there. That is G.711, not a bug --
    # and the gain tables sidestep it by leaving unmoved codes alone.
    exceptions = {0x7F: 0xFF} if fmt == "ulaw" else {}
    for code in range(256):
        assert encode(decode(code)) == exceptions.get(code, code), f"{fmt} code {code}"


def test_g711_silence_stays_silence():
    assert ulaw_to_linear(0xFF) == 0
    assert alaw_to_linear(0xD5) == 8      # A-law's smallest positive step


@pytest.mark.parametrize("fmt", ["ulaw", "alaw"])
def test_unity_gain_table_is_the_identity(fmt):
    """1.0x must not rewrite a single byte.

    mu-law has two zeros (0x7F negative, 0xFF positive) and a naive
    decode/re-encode collapses one onto the other — same silence, but a
    needless difference that makes byte-comparing two runs useless.
    """
    table, clip = _companded_tables(fmt, 1.0)
    assert table == bytes(range(256))
    assert sum(clip) == 0


@pytest.mark.parametrize("fmt", ["ulaw", "alaw"])
def test_gain_scales_amplitude(fmt):
    encode = linear_to_ulaw if fmt == "ulaw" else linear_to_alaw
    decode = ulaw_to_linear if fmt == "ulaw" else alaw_to_linear
    out = Gain(2.0, fmt).apply(bytes([encode(1000)]))
    # Companding is lossy, so this lands near 2000 rather than on it.
    assert 1800 <= decode(out[0]) <= 2200


def test_pcm_gain_scales_and_clamps():
    g = Gain(2.0, "pcm")
    out = g.apply(struct.pack("<3h", 1000, -1000, 20000))
    assert struct.unpack("<3h", out) == (2000, -2000, 32767)
    assert g.clipped_samples == 1          # the third one hit the rail


def test_pcm_gain_carries_a_split_sample_across_chunks():
    """A chunk boundary can land between a sample's two bytes. Scaling the odd
    byte as if it were a whole sample puts a click on every boundary."""
    data = struct.pack("<3h", 1000, -1000, 3000)
    whole = Gain(2.0, "pcm").apply(data)

    split = Gain(2.0, "pcm")
    piecewise = split.apply(data[:3]) + split.apply(data[3:]) + split.flush()
    assert piecewise == whole


def test_flush_returns_a_truncated_trailing_byte_rather_than_dropping_it():
    g = Gain(2.0, "pcm")
    assert g.apply(b"\x01") == b""         # held: half a sample
    assert g.flush() == b"\x01"            # returned unscaled, not swallowed
    assert g.flush() == b""


def test_identity_gain_short_circuits():
    g = Gain(1.0, "pcm")
    data = struct.pack("<2h", 1234, -1234)
    assert g.apply(data) is data           # untouched, not merely equal
    assert g.seconds == 0.0


def test_gain_refuses_container_formats_with_an_actionable_message():
    """Silently ignoring volume on mp3 is the bug this SDK just finished
    removing from `speed`. Fail loudly and name the way out."""
    with pytest.raises(ValueError, match="volume_mode"):
        Gain(1.5, "mp3")
    for fmt in SCALABLE_FORMATS:
        Gain(1.5, fmt)                     # these are all fine


# ── range checking ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [-0.1, 2.1, 100.0])
def test_volume_out_of_range_is_rejected(bad):
    with pytest.raises(ValueError, match="between"):
        check_volume(bad)


@pytest.mark.parametrize("ok", [0.0, 0.5, 1.0, 1.4, 2.0])
def test_volume_in_range_is_accepted(ok):
    check_volume(ok)


def test_volume_none_is_accepted():
    check_volume(None)


def test_volume_rejects_bool_masquerading_as_a_number():
    """`volume=True` is 1.0 to Python and a mistake in every other sense."""
    with pytest.raises(ValueError, match="must be a number"):
        check_volume(True)


# ── mode routing: the exclusivity property ───────────────────────────────────
@pytest.mark.parametrize("mode", ["auto", "server", "client"])
def test_exactly_one_party_ever_applies_the_gain(mode):
    """The invariant the whole feature rests on.

    If both a wire value and a local Gain came back, volume=1.4 would land as
    1.96x with clipped peaks.
    """
    send, gain = _resolve_volume(1.4, mode, "pcm")
    assert (send is not None) + (gain is not None) == 1


def test_auto_applies_locally_while_the_server_lacks_support():
    send, gain = _resolve_volume(1.4, "auto", "pcm")
    assert send is None                    # nothing on the wire to double up
    assert gain is not None and gain.gain == 1.4


def test_auto_follows_the_server_support_flag(monkeypatch):
    """The day the API ships volume, one constant flips and every call site
    switches over — no signature and no caller changes."""
    monkeypatch.setattr("svara._client.SERVER_APPLIES_VOLUME", True)
    send, gain = _resolve_volume(1.4, "auto", "pcm")
    assert send == 1.4
    assert gain is None                    # and we stop touching the bytes


def test_server_mode_never_touches_the_samples():
    send, gain = _resolve_volume(1.4, "server", "mp3")
    assert send == 1.4 and gain is None    # and works for containers


def test_client_mode_never_puts_volume_on_the_wire():
    send, gain = _resolve_volume(1.4, "client", "ulaw")
    assert send is None and gain is not None


def test_no_volume_means_no_volume_anywhere():
    assert _resolve_volume(None, "auto", "pcm") == (None, None)


def test_unknown_volume_mode_is_rejected():
    with pytest.raises(ValueError, match="volume_mode"):
        _resolve_volume(1.4, "loud", "pcm")


def test_auto_on_a_container_format_fails_loudly():
    """auto can't scale mp3 locally and the server won't scale it either, so
    the only honest outcome is an error naming the two ways forward."""
    with pytest.raises(ValueError, match="decoder"):
        _resolve_volume(1.4, "auto", "mp3")


# ── wire format ──────────────────────────────────────────────────────────────
def test_volume_omitted_never_serialises_as_the_string_None():
    """The same trap `speed` fell into: the WebSocket puts query values in
    verbatim and the server calls float() on them."""
    assert "volume" not in _ws_url("https://x", {"voice": "v", "volume": None})
    p = _speech_payload(
        input="hi", voice="v", model="svara-1", response_format="pcm",
        stream=False, sample_rate=None, speed=None, language=None,
        sampling={}, extra=None, volume=None,
    )
    assert "volume" not in p


def test_volume_reaches_both_transports_as_a_number():
    assert "volume=1.4" in _ws_url("https://x", {"voice": "v", "volume": 1.4})
    p = _speech_payload(
        input="hi", voice="v", model="svara-1", response_format="pcm",
        stream=False, sample_rate=None, speed=None, language=None,
        sampling={}, extra=None, volume=1.4,
    )
    assert p["volume"] == 1.4


def test_volume_signature_is_float_only_on_every_path():
    """Mirrors the speed guard: a str here is what broke the WebSocket once."""
    import inspect
    import typing

    from svara import AsyncSvara, Svara

    sync = Svara(api_key="sk_test", base_url="https://example.invalid")
    aio = AsyncSvara(api_key="sk_test", base_url="https://example.invalid")
    for fn in (sync.speech.create, sync.speech.stream, aio.speech.create,
               aio.speech.stream, aio.speech.stream_input):
        ann = inspect.signature(fn).parameters["volume"].annotation
        args = typing.get_args(ann) or (ann,)
        assert str not in args, f"{fn.__qualname__} accepts str for volume"
