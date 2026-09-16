"""``svara`` command-line interface.

    svara say "नमस्ते!" --voice sv_enhdbrj5 --out hello.mp3
    svara say "Hello" --voice sv_enhdbrj5 --format ulaw --sample-rate 8000 --out ivr.ulaw
    svara voices --language hi
    svara voices --json
    svara languages
    svara usage
    svara doctor          # connectivity + key check with timings, for support tickets
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from ._client import Svara
from ._version import __version__
from .exceptions import SvaraError
from .types import FORMAT_INFO, SAMPLE_RATES

# format -> default file extension. Only opus differs from its format name
# (it ships in an Ogg container); the rest are derived so that adding a format
# to FORMAT_INFO is the only edit needed.
_EXT = {fmt: ("ogg" if fmt == "opus" else fmt) for fmt in FORMAT_INFO}
_FORMATS = sorted(FORMAT_INFO)


def _cmd_say(args: argparse.Namespace) -> int:
    client = Svara(api_key=args.api_key, base_url=args.base_url)
    out = args.out or f"speech.{_EXT.get(args.format, 'bin')}"
    try:
        data = client.speech.create(
            input=args.text, voice=args.voice, response_format=args.format,
            sample_rate=args.sample_rate, speed=args.speed, language=args.language,
        )
    except SvaraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    with open(out, "wb") as f:
        f.write(data)
    rate = f" @ {data.sample_rate} Hz" if data.sample_rate else ""
    print(f"wrote {out} ({len(data)} bytes, {data.content_type or args.format}{rate})")
    return 0


def _cmd_voices(args: argparse.Namespace) -> int:
    client = Svara(api_key=args.api_key, base_url=args.base_url)
    try:
        voices = client.voices.list(language=args.language, gender=args.gender)
    except SvaraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    if args.json:
        print(json.dumps([v.raw for v in voices], ensure_ascii=False, indent=2))
        return 0
    for v in voices:
        print(f"{v.voice_id:<14} {v.name or '':<18} {v.language or '':<6} {v.gender or '':<8} "
              f"{v.quality_band or ''}")
    sys.stdout.flush()  # keep the count after the rows when stdout is a pipe
    print(f"\n{len(voices)} voices", file=sys.stderr)
    return 0


def _cmd_languages(args: argparse.Namespace) -> int:
    client = Svara(api_key=args.api_key, base_url=args.base_url)
    try:
        langs = client.languages.list()
    except SvaraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    if args.json:
        print(json.dumps([lang.raw for lang in langs], ensure_ascii=False, indent=2))
        return 0
    for lang in langs:
        print(f"{lang.iso1 or '':<4} {lang.iso3:<5} {lang.name:<28} {lang.region or ''}")
    sys.stdout.flush()
    print(f"\n{len(langs)} languages", file=sys.stderr)
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    client = Svara(api_key=args.api_key, base_url=args.base_url)
    try:
        u = client.usage.get()
    except SvaraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    if args.json:
        print(json.dumps(u.raw, ensure_ascii=False, indent=2))
        return 0

    def fmt(n: Optional[int]) -> str:
        return "unlimited" if n == -1 else ("-" if n is None else f"{n:,}")

    print(f"plan                   {u.plan_id or '-'}")
    print(f"characters used        {fmt(u.characters_used)}  (this month)")
    print(f"characters remaining   {fmt(u.characters_remaining)}")
    print(f"requests / minute      {fmt(u.requests_per_minute)}")
    print(f"concurrent streams     {fmt(u.max_concurrent_streams)}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Check DNS, TLS, the HTTP path, the key, and the WebSocket path, with timings.

    The output is what a support ticket needs: which hop fails, and how long
    each one took from this machine. Costs one short synthesis (about ten
    characters) against the quota.
    """
    import asyncio
    import os
    import platform
    import socket
    import time
    import urllib.parse

    import httpx

    from . import __version__ as ver
    from ._client import DEFAULT_BASE_URL, AsyncSvara
    from .exceptions import AuthenticationError, MissingAPIKeyError
    base = (args.base_url or os.environ.get("SVARA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    host = urllib.parse.urlparse(base).hostname or base
    ok = True

    def row(label: str, status: str, detail: str = "") -> None:
        print(f"  {label:<22} {status:<6} {detail}")

    print(f"svara-voice {ver} · Python {platform.python_version()} · httpx {httpx.__version__}")
    print(f"base URL {base}\n")

    t0 = time.perf_counter()
    try:
        ip = socket.gethostbyname(host)
        row("DNS", "ok", f"{host} -> {ip} in {(time.perf_counter() - t0) * 1000:.0f} ms")
    except OSError as e:
        row("DNS", "FAIL", f"{host}: {e}")
        print("\nThe API host does not resolve from here. Check the network or SVARA_BASE_URL.")
        return 1

    # TLS + HTTP: /v1/models needs no key and is 89 bytes.
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=10.0) as hc:
            r = hc.get(f"{base}/v1/models")
        row("TLS + HTTP", "ok" if r.status_code == 200 else "FAIL",
            f"GET /v1/models -> {r.status_code} in {(time.perf_counter() - t0) * 1000:.0f} ms"
            f" (server: {r.headers.get('server', '?')}, via: {r.headers.get('via', '-')})")
        ok &= r.status_code == 200
    except httpx.HTTPError as e:
        row("TLS + HTTP", "FAIL", f"{type(e).__name__}: {e}")
        print("\nThe API host resolves but cannot be reached over HTTPS. A proxy or firewall "
              "between this machine and the API is the usual cause.")
        return 1

    try:
        client = Svara(api_key=args.api_key, base_url=base)
    except MissingAPIKeyError:
        row("API key", "FAIL", "not set: pass --api-key or export SVARA_API_KEY")
        return 1
    key = client.api_key
    row("API key", "ok", f"{key[:8]}…{key[-4:]} ({len(key)} chars)")

    t0 = time.perf_counter()
    try:
        u = client.usage.get()
        row("Auth (/v1/usage)", "ok", f"plan {u.plan_id or '?'}, {u.characters_remaining} characters left, "
            f"{(time.perf_counter() - t0) * 1000:.0f} ms")
    except AuthenticationError as e:
        row("Auth (/v1/usage)", "FAIL", e.message)
        client.close()
        return 1
    except SvaraError as e:
        row("Auth (/v1/usage)", "WARN", f"{type(e).__name__}: {e.message}")

    t0 = time.perf_counter()
    try:
        s = client.speech.stream(input="Svara doctor.", voice=args.voice, response_format="pcm")
        first = next(s)
        n = len(first) + len(s.read())
        row("HTTP synthesis", "ok", f"first audio {s.time_to_first_audio * 1000:.0f} ms, "
            f"{n / 48000:.2f} s of audio, {(time.perf_counter() - t0) * 1000:.0f} ms total")
    except SvaraError as e:
        row("HTTP synthesis", "FAIL", f"{type(e).__name__}: {e.message}")
        ok = False
    client.close()

    async def ws_check() -> None:
        ac = AsyncSvara(api_key=key, base_url=base)
        t0 = time.perf_counter()
        try:
            prepared = await ac.speech.prepare(voice=args.voice)
            t_open = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter()
            first = None
            n = 0
            async for a in prepared.stream(["Svara ", "doctor, ", "websocket ", "path ", "check ",
                                            "one ", "two ", "three ", "four."]):
                if first is None:
                    first = (time.perf_counter() - t1) * 1000
                n += len(a)
            row("WebSocket synthesis", "ok", f"connect {t_open:.0f} ms, first audio {first:.0f} ms "
                f"after text, {n / 48000:.2f} s of audio")
        except SvaraError as e:
            row("WebSocket synthesis", "FAIL", f"{type(e).__name__}: {e.message}")
            print("\nHTTP works but the WebSocket does not: a proxy that blocks Upgrade requests "
                  "is the usual cause. The LiveKit plugin's eager mode and stream_input() need it.")
            raise
        finally:
            await ac.aclose()

    try:
        asyncio.run(ws_check())
    except SvaraError:
        ok = False

    print()
    print("All checks passed." if ok else "Some checks failed; see above.")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="svara", description="Svara TTS command-line interface.")
    p.add_argument("--version", action="version", version=f"svara {__version__}")
    p.add_argument("--api-key", default=None, help="defaults to $SVARA_API_KEY")
    p.add_argument("--base-url", default=None, help="defaults to $SVARA_BASE_URL")
    sub = p.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="synthesize text to an audio file")
    say.add_argument("text")
    say.add_argument("--voice", "-v", required=True)
    say.add_argument("--format", "-f", default="mp3", choices=_FORMATS)
    say.add_argument("--sample-rate", "-r", type=int, default=None, choices=SAMPLE_RATES,
                     help="output rate in Hz (telephony ulaw/alaw: 8000)")
    say.add_argument("--out", "-o", default=None)
    say.add_argument("--speed", type=float, default=None,
                     help="speaking speed, 0.7-1.5 (pitch is preserved)")
    say.add_argument("--language", "-l", default=None)
    say.set_defaults(func=_cmd_say)

    voices = sub.add_parser("voices", help="list available voices")
    voices.add_argument("--language", "-l", default=None, help="filter by ISO code")
    voices.add_argument("--gender", "-g", default=None, choices=["female", "male"])
    voices.add_argument("--json", action="store_true", help="print raw JSON")
    voices.set_defaults(func=_cmd_voices)

    langs = sub.add_parser("languages", help="list supported languages")
    langs.add_argument("--json", action="store_true", help="print raw JSON")
    langs.set_defaults(func=_cmd_languages)

    usage = sub.add_parser("usage", help="show plan limits and remaining balance")
    usage.add_argument("--json", action="store_true", help="print raw JSON")
    usage.set_defaults(func=_cmd_usage)

    doctor = sub.add_parser("doctor", help="check connectivity, key and both synthesis paths")
    doctor.add_argument("--voice", "-v", default="sv_enhdbrj5")
    doctor.set_defaults(func=_cmd_doctor)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:  # e.g. missing API key, invalid argument
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
