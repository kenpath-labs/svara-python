"""``svara`` command-line interface.

    svara say "नमस्ते!" --voice sv_enhdbrj5 --out hello.mp3
    svara say "Hello" --voice sv_enhdbrj5 --format ulaw --sample-rate 8000 --out ivr.ulaw
    svara voices --language hi
    svara voices --json
    svara languages
    svara usage
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
