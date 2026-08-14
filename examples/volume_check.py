"""Adjust volume on streamed audio -- no API key, no network, no latency.

    python examples/volume_check.py                    # measure, print a table
    python examples/volume_check.py --write out        # also write .wav to listen
    python examples/volume_check.py --format ulaw

Synthesises a speech-like signal locally, chops it into 20 ms frames the way a
real stream arrives, and pushes each frame through the same ``Gain`` the SDK
uses. Answers two questions without touching the live API:

  1. **Does the gain do what it says?**  Measured RMS ratio is compared against
     the requested multiplier. For pcm it should land on it; for ulaw/alaw it
     lands near it, because G.711 is companded and lossy by construction.

  2. **Does it cost anything?**  Each frame carries 20 ms of audio, so the
     budget per frame is 20 ms. The table reports what scaling actually spends
     against that budget. Anything under ~1% is free in the sense that matters:
     it cannot move time-to-first-audio or open a gap between frames.

The point of frames is that this is the streaming case, not a whole-buffer
case. A gain that is cheap on one 10-second blob but expensive per 20 ms frame
would stall playback, and only the second measurement would show it.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import struct
import time
import wave

from svara._audio import (
    Gain,
    alaw_to_linear,
    linear_to_alaw,
    linear_to_ulaw,
    ulaw_to_linear,
)

FRAME_MS = 20          # what a telephony/WebRTC stream actually delivers
DURATION_S = 3.0


#: Peak of the generated signal, as a fraction of full scale. Chosen so that
#: 1.4x still fits and 2.0x does not: clipping is the behaviour most worth
#: seeing, and a test signal that never reaches the rails never shows it.
PEAK = 0.62


def speechlike_pcm(rate: int, seconds: float) -> bytes:
    """A signal with speech's rough shape: a low fundamental, a few harmonics,
    and a syllable envelope. Not speech -- but unlike a pure sine its peaks sit
    well above its average, which is what makes gain interesting.

    Normalised to :data:`PEAK` rather than to a fixed amplitude: the harmonics
    are out of phase, so the true peak is nowhere near the sum of their
    amplitudes and guessing it leaves the signal too quiet to ever clip.
    """
    n = int(rate * seconds)
    raw = []
    for i in range(n):
        t = i / rate
        # syllable-rate envelope, ~3.5 Hz, never fully silent
        env = 0.35 + 0.45 * abs(math.sin(2 * math.pi * 3.5 * t))
        raw.append(env * (1.00 * math.sin(2 * math.pi * 145 * t)
                          + 0.50 * math.sin(2 * math.pi * 290 * t)
                          + 0.25 * math.sin(2 * math.pi * 580 * t)
                          + 0.12 * math.sin(2 * math.pi * 1160 * t)))
    scale = PEAK * 32767 / max(abs(v) for v in raw)
    return b"".join(struct.pack("<h", int(v * scale)) for v in raw)


def to_format(pcm: bytes, fmt: str) -> bytes:
    if fmt == "pcm":
        return pcm
    encode = linear_to_ulaw if fmt == "ulaw" else linear_to_alaw
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return bytes(encode(s) for s in samples)


def to_linear(data: bytes, fmt: str):
    if fmt == "pcm":
        return struct.unpack(f"<{len(data) // 2}h", data)
    decode = ulaw_to_linear if fmt == "ulaw" else alaw_to_linear
    return [decode(b) for b in data]


def rms(samples) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples))


def frames_of(data: bytes, fmt: str, rate: int):
    """Split into 20 ms frames. Deliberately produces an odd-sized frame for
    pcm so the sample-splitting carry is exercised, not just described."""
    bps = 2 if fmt == "pcm" else 1
    size = int(rate * FRAME_MS / 1000) * bps
    out = [data[i:i + size] for i in range(0, len(data), size)]
    if fmt == "pcm" and len(out) > 2:
        # Re-cut one boundary so a 16-bit sample straddles two frames.
        joined = out[0] + out[1]
        out[0], out[1] = joined[:size - 1], joined[size - 1:]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", default="pcm", choices=["pcm", "ulaw", "alaw"])
    ap.add_argument("--rate", type=int, default=None,
                    help="default 24000 for pcm, 8000 for ulaw/alaw")
    ap.add_argument("--gains", default="0.5,0.8,1.0,1.4,2.0")
    ap.add_argument("--write", default=None, metavar="DIR",
                    help="also write a .wav per gain, so you can hear it")
    args = ap.parse_args()

    fmt = args.format
    rate = args.rate or (24000 if fmt == "pcm" else 8000)
    gains = [float(g) for g in args.gains.split(",")]

    source_pcm = speechlike_pcm(rate, DURATION_S)
    source = to_format(source_pcm, fmt)
    base_rms = rms(to_linear(source, fmt))
    frames = frames_of(source, fmt, rate)

    print(f"\n{fmt} @ {rate} Hz, {DURATION_S}s, {len(frames)} frames of "
          f"{FRAME_MS} ms  (budget {FRAME_MS:.1f} ms/frame)\n")
    head = (f"{'gain':>6} {'RMS x':>8} {'clipped':>9} {'p50/frame':>11} "
            f"{'worst frame':>13} {'% of budget':>12}")
    print(head)
    print("-" * len(head))

    for g in gains:
        # Warm up on a throwaway Gain: the companded byte table is built on
        # first use, and the first timed frame would otherwise be charged for
        # constructing it plus whatever the interpreter faults in.
        warm = Gain(g, fmt)
        for f in frames[:5]:
            warm.apply(f)

        gain = Gain(g, fmt)
        per_frame = []
        out = bytearray()
        for f in frames:
            t0 = time.perf_counter()
            out += gain.apply(f)
            per_frame.append((time.perf_counter() - t0) * 1000)
        out += gain.flush()

        got = rms(to_linear(bytes(out), fmt))
        ratio = got / base_rms if base_rms else 0.0
        total = len(out) if fmt != "pcm" else len(out) // 2
        clipped = 100.0 * gain.clipped_samples / total if total else 0.0
        p50 = statistics.median(per_frame)
        worst = max(per_frame)
        print(f"{g:>6.2f} {ratio:>8.3f} {clipped:>8.2f}% {p50:>9.3f}ms "
              f"{worst:>11.3f}ms {100 * worst / FRAME_MS:>11.2f}%")

        # Byte count must survive scaling, or the stream length changed.
        assert len(out) == len(source), f"{len(out)} != {len(source)}"

        if args.write:
            os.makedirs(args.write, exist_ok=True)
            path = os.path.join(args.write, f"{fmt}_gain{g:g}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                lin = to_linear(bytes(out), fmt)
                w.writeframes(struct.pack(f"<{len(lin)}h", *lin))

    print("\n  RMS x   should track the gain column. pcm lands on it; ulaw/alaw")
    print( "          land near it -- G.711 is companded, so it quantises.")
    print( "  clipped  samples pushed into the rails. Non-zero above 1.0 is")
    print( "          expected on peaks; a few percent is audible distortion.")
    print(f"  budget   worst frame as a share of the {FRAME_MS} ms it carries.")
    print( "          Well under 1% means scaling cannot stall a stream.")
    if args.write:
        print(f"\n  wrote .wav files to {args.write}/ -- play them back to compare.")
    print()


if __name__ == "__main__":
    main()
