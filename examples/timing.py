"""Measure client-side latency — where the time actually goes.

    python examples/timing.py                        # key from .env
    python examples/timing.py --runs 20 --wps 8
    python examples/timing.py --api-key sk_live_...

Feeds text at a configurable words-per-second to imitate a real LLM, then
reports the distribution. The point is the split: a slow first word is either
the caller's LLM taking its time to produce enough words to start on, or the
model taking its time to speak them, and those have very different fixes.

Lower --wps and feed_to_chunk_ms grows while generate_ms holds steady, which
is how you can tell the two apart.

For a one-line record from code you already have, change nothing and set
``SVARA_TIMING=1`` — every stream_input call logs its own summary.
"""

import argparse
import asyncio
import logging
import os

# Examples are scripts, so picking up a local .env is a convenience. The SDK
# itself deliberately does not: a library that silently reads files from the
# working directory is an unpleasant surprise in production.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from svara import AsyncSvara, TimingStats

TEXT = (
    "Thanks for calling the front desk. We have a deluxe room available for "
    "those dates at five thousand five hundred rupees a night, and breakfast "
    "for two is included in that rate."
)


async def fake_llm(text: str, words_per_second: float):
    """Emit one word at a time at a fixed rate. 0 = as fast as possible."""
    for word in text.split():
        yield word + " "
        if words_per_second:
            await asyncio.sleep(1.0 / words_per_second)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--wps", type=float, default=6.0,
                    help="words/sec fed in; 0 sends everything at once")
    ap.add_argument("--voice", default="sv_enhdbrj5")
    ap.add_argument("--format", default="pcm", help="pcm | ulaw (telephony)")
    ap.add_argument("--sample-rate", type=int, default=None)
    ap.add_argument("--chunk-words", type=int, default=4,
                    help="eager chunk size; the server starts at 2x this, and "
                         "clamps anything under 4")
    ap.add_argument("--volume", type=float, default=None,
                    help="loudness multiplier 0.0-2.0; applied client-side, so its "
                         "CPU cost shows up as gain_ms in the table")
    ap.add_argument("--api-key", default=None, help="defaults to $SVARA_API_KEY or .env")
    ap.add_argument("--verbose", action="store_true", help="log each run")
    args = ap.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (args.api_key or os.environ.get("SVARA_API_KEY")):
        raise SystemExit(
            "No API key. Pass --api-key, set SVARA_API_KEY, or put it in a .env "
            f"file next to where you run this (looked in {os.getcwd()})."
        )

    client = AsyncSvara(api_key=args.api_key)
    stats = TimingStats()
    try:
        for i in range(args.runs):
            nbytes = 0
            async for audio in client.speech.stream_input(
                fake_llm(TEXT, args.wps),
                voice=args.voice,
                response_format=args.format,
                sample_rate=args.sample_rate,
                chunk_words=args.chunk_words,
                volume=args.volume,
                on_timing=stats.add,
            ):
                nbytes += len(audio)
            tl = stats.timelines[-1]
            print(f"  run {i + 1}/{args.runs}: {nbytes} bytes  "
                  f"ttfa={tl.ttfa_from_first_text_ms}ms "
                  f"(waiting={tl.feed_to_chunk_ms}ms model={tl.generate_ms}ms)")
    finally:
        await client.aclose()

    print(f"\nfed at {args.wps or 'max'} words/s\n")
    print(stats.report())

    # Plain ASCII: this gets pasted into tickets and read on Windows consoles,
    # where cp1252 turns an em-dash into a replacement character.
    tl = stats.timelines[-1]
    print("\nhow to read this:")
    print("  handshake_ms          opening the connection - paid per stream_input call")
    print("  auth_ms               API key check as the caller experiences it, one RTT included")
    print("  auth_server_ms        the same minus that RTT - compare server-side targets to THIS")
    print("  ttfa_from_trigger_ms  from the trigger word to audio; still mostly the server")
    print("                        waiting for lookahead, not generating")
    print(f"  feed_to_chunk_ms      waiting for enough text to start - your LLM "
          f"produced {tl.words_at_first_chunk} words before")
    print("                        the server began speaking, plus transit")
    print("  generate_ms           the model's own time to first audio")
    print("  realtime_factor       audio seconds per wall second; below 1.0 means "
          "playback will stall")
    print(f"\n  feed_to_chunk + generate = ttfa. At {args.wps or 'max'} words/s the "
          f"first two account for")
    print("  where the time went, and feed_to_chunk dominates.")
    # The threshold is worth naming: it looks like the caller's LLM being slow,
    # and it is partly the server deciding it wants a chunk of lookahead.
    print(f"\n  That wait is not all your LLM. Eager mode starts at 2x chunk_words "
          f"= {tl.trigger_words} words,")
    if args.wps:
        print(f"  so ~{tl.trigger_words / args.wps:.1f}s of it is the threshold, not the feed.")
    print("  Raising --chunk-words only makes that worse and 4 is a hard floor, so the")
    print("  default is already the fastest setting available. From here only")
    print("  generate_ms is Svara's to improve.")


asyncio.run(main())
