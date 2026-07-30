"""``svara`` command-line interface.

    svara say "नमस्ते!" --voice sv_enhdbrj5 --out hello.mp3
    svara voices --language hi
    svara voices --json
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from ._client import Svara
from ._version import __version__
from .exceptions import SvaraError

# format -> default file extension
_EXT = {"mp3": "mp3", "opus": "ogg", "aac": "aac", "flac": "flac",
        "wav": "wav", "pcm": "pcm", "ulaw": "ulaw", "alaw": "alaw"}


def _cmd_say(args: argparse.Namespace) -> int:
    client = Svara(api_key=args.api_key, base_url=args.base_url)
    out = args.out or f"speech.{_EXT.get(args.format, 'bin')}"
    try:
        data = client.speech.create(
            input=args.text, voice=args.voice, response_format=args.format,
            speed=args.speed, language=args.language,
        )
    except SvaraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data)} bytes)")
    return 0


def _cmd_voices(args: argparse.Namespace) -> int:
    client = Svara(api_key=args.api_key, base_url=args.base_url)
    try:
        voices = client.voices.list()
    except SvaraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    if args.language:
        voices = [v for v in voices if (v.language or "").lower() == args.language.lower()]
    if args.json:
        import json
        print(json.dumps([v.raw for v in voices], ensure_ascii=False, indent=2))
        return 0
    for v in voices:
        band = v.labels.get("quality_band", "")
        print(f"{v.voice_id:<14} {v.name or '':<18} {v.language or '':<6} {v.gender or '':<8} {band}")
    print(f"\n{len(voices)} voices", file=sys.stderr)
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
    say.add_argument("--format", "-f", default="mp3",
                     choices=["mp3", "opus", "aac", "flac", "wav", "pcm", "ulaw", "alaw"])
    say.add_argument("--out", "-o", default=None)
    say.add_argument("--speed", type=float, default=None)
    say.add_argument("--language", "-l", default=None)
    say.set_defaults(func=_cmd_say)

    voices = sub.add_parser("voices", help="list available voices")
    voices.add_argument("--language", "-l", default=None, help="filter by ISO code")
    voices.add_argument("--json", action="store_true", help="print raw JSON")
    voices.set_defaults(func=_cmd_voices)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:  # e.g. missing API key
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
