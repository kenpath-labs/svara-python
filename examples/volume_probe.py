"""Does the API apply ``volume`` server-side? Ask it.

    python examples/volume_probe.py                     # key from .env
    python examples/volume_probe.py --runs 3

Sends the same text at several volumes with ``volume_mode="server"`` -- so the
SDK forwards the field and touches nothing -- and compares the loudness of what
comes back. Covers both transports, because the HTTP body and the WebSocket
query string are separate code paths on the server and one can support a field
the other drops.

**The confound this controls for:** synthesis is sampled, so the same text does
not produce the same audio twice, and RMS moves between identical calls for
reasons that have nothing to do with volume. So the baseline is run several
times first to measure that natural spread. A volume effect only counts if it
is far outside it -- 0.5x to 2.0x should be a 4x separation, which no amount of
sampling noise reaches.

Costs a handful of short synthesis calls against your quota.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import struct

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from svara import AsyncSvara
from svara.exceptions import SvaraError

TEXT = "Thanks for calling the front desk. How can I help you today?"

#: Compared against each other rather than against 1.0: if the server clamps or
#: rescales, the *ratio* between these two is still the tell.
QUIET, LOUD = 0.5, 2.0


def rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    s = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(float(v) * v for v in s) / len(s))


def spread(vals) -> str:
    if not vals:
        return "n/a"
    lo, hi = min(vals), max(vals)
    mean = sum(vals) / len(vals)
    return f"{mean:8.1f}  (spread {100 * (hi - lo) / mean:.1f}%)" if mean else "0"


async def http_run(client, volume, runs):
    out = []
    for _ in range(runs):
        audio = await client.speech.create(
            input=TEXT, voice=VOICE, response_format="pcm",
            volume=volume, volume_mode="server",
        )
        out.append(rms(audio))
    return out


async def ws_run(client, volume, runs):
    out = []
    for _ in range(runs):
        buf = b""
        async for chunk in client.speech.stream_input(
            [w + " " for w in TEXT.split()], voice=VOICE, response_format="pcm",
            volume=volume, volume_mode="server",
        ):
            buf += chunk
        out.append(rms(buf))
    return out


async def probe(name, fn, client, runs):
    print(f"\n  {name}")
    try:
        base = await fn(client, None, runs)
        quiet = await fn(client, QUIET, runs)
        loud = await fn(client, LOUD, runs)
    except SvaraError as e:
        # A rejection is a conclusive answer too, and a friendlier one than
        # silence: it means the field reached something that knew to refuse it.
        print(f"    REJECTED: {e}")
        return
    except Exception as e:  # a dropped socket is how `speed` failed once
        print(f"    FAILED: {type(e).__name__}: {e}")
        return

    print(f"    no volume   rms {spread(base)}")
    print(f"    {QUIET}x        rms {spread(quiet)}")
    print(f"    {LOUD}x        rms {spread(loud)}")

    mq = sum(quiet) / len(quiet)
    ml = sum(loud) / len(loud)
    mb = sum(base) / len(base)
    noise = (max(base) - min(base)) / mb if mb else 0.0
    sep = ml / mq if mq else 0.0
    print(f"\n    {LOUD}x / {QUIET}x = {sep:.2f}x   (expected {LOUD / QUIET:.0f}x "
          f"if applied; sampling noise alone is {100 * noise:.1f}%)")
    if sep > 2.5:
        print("    -> the server APPLIES volume. Set SERVER_APPLIES_VOLUME = True.")
    elif sep < 1.3:
        print("    -> the server IGNORES volume. Leave the flag False; the field")
        print("       would be accepted and silently dropped.")
    else:
        print("    -> INCONCLUSIVE. Re-run with --runs 5, or the server is doing")
        print("       something partial (clamping, normalising).")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="calls per condition")
    ap.add_argument("--voice", default="sv_enhdbrj5")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    global VOICE
    VOICE = args.voice

    if not (args.api_key or os.environ.get("SVARA_API_KEY")):
        raise SystemExit("No API key. Pass --api-key, set SVARA_API_KEY, or use .env")

    client = AsyncSvara(api_key=args.api_key)
    print(f"probing volume support -- {args.runs} runs per condition, "
          f"{3 * 2 * args.runs} calls total")
    try:
        await probe("HTTP  POST /v1/audio/speech", http_run, client, args.runs)
        await probe("WS    /v1/audio/speech/stream-input", ws_run, client, args.runs)
    finally:
        await client.aclose()
    print()


asyncio.run(main())
