"""Measure this SDK's time-to-first-audio against the live API.

Run: SVARA_API_KEY=sk_live_... python examples/latency_probe.py

Reproduces the numbers in MEASUREMENTS.md.


Records the arrival time of every raw network chunk, then replays httpx's
ByteChunker over those timestamps. Both the buffered and unbuffered answers
come from the SAME request, so network jitter cancels out completely instead
of swamping the effect.
"""
import os
import statistics
import time

import httpx

KEY = os.environ["SVARA_API_KEY"]
VOICE = "sv_enhdbrj5"
TEXT = "नमस्ते, मैं स्वरा हूँ। आपकी कैसे मदद कर सकती हूँ आज?"
REGIONS = ["api.kenpathlabs.com", "api.in.idr.kenpathlabs.com"]
BYTES_PER_SEC = {"pcm": 24000 * 2, "ulaw": 8000, "mp3": None}


def emit_time(arrivals, chunk_size):
    """When would iter_bytes(chunk_size) have released its first chunk?"""
    if not chunk_size:
        return arrivals[0][0]
    total = 0
    for t, n in arrivals:
        total += n
        if total >= chunk_size:
            return t
    return arrivals[-1][0]  # stream ended before filling one chunk


def probe(host, fmt, reps=5):
    payload = {"model": "svara-tts-turbo", "voice": VOICE, "input": TEXT,
               "response_format": fmt, "stream": True}
    rows = []
    with httpx.Client(base_url=f"https://{host}",
                      headers={"xi-api-key": KEY}, timeout=60.0) as c:
        for _ in range(reps):
            t0 = time.perf_counter()
            arrivals = []
            with c.stream("POST", "/v1/audio/speech", json=payload) as r:
                if r.status_code != 200:
                    return None, f"HTTP {r.status_code}"
                for b in r.iter_raw():
                    if b:
                        arrivals.append(((time.perf_counter() - t0) * 1000, len(b)))
            if arrivals:
                rows.append(arrivals)
    return rows, None


def main():
    print("Server frame sizes and the delay iter_bytes(4096) adds to first audio\n")
    for host in REGIONS:
        for fmt in ("pcm", "ulaw", "mp3"):
            rows, err = probe(host, fmt)
            if err:
                print(f"{host:<28} {fmt:<5} {err}")
                continue
            frame = statistics.median(n for r in rows for _, n in r[:5])
            unbuf = statistics.median(emit_time(r, None) for r in rows)
            buf4k = statistics.median(emit_time(r, 4096) for r in rows)
            nframes = statistics.median(len(r) for r in rows)
            bps = BYTES_PER_SEC[fmt]
            audio = f"{4096 / bps * 1000:.0f}ms" if bps else "n/a"
            print(f"{host:<28} {fmt:<5} server frame {frame:>6.0f}B  "
                  f"frames/utt {nframes:>3.0f}  |  unbuffered {unbuf:6.1f}ms  "
                  f"chunk_size=4096 {buf4k:6.1f}ms  ADDED {buf4k - unbuf:+6.1f}ms "
                  f"(4096B = {audio} audio)")
        print()


main()
