#!/usr/bin/env python3
"""Narrate a long text with Svara TTS Turbo: one WAV plus optional SRT subtitles.

A single request takes at most 5,000 characters, so this splits the text at
sentence boundaries (Latin, Devanagari `।`, CJK `。！？`), synthesises each
part as 24 kHz PCM with word timings, and joins the parts into one WAV. PCM is
used so the join is exact; convert afterwards if you need mp3
(`ffmpeg -i out.wav -b:a 128k out.mp3`).

    pip install svara-voice
    export SVARA_API_KEY=sk_live_...
    python narrate.py chapter1.txt --voice sv_enhdbrj5 --out chapter1.wav --srt chapter1.srt
    python narrate.py script.txt --voice sv_84sb2v3w --language hi --speed 0.95 --out vo.wav

Find a voice id first: python ../../svara-voices/scripts/find_voices.py --language hi
"""

from __future__ import annotations

import argparse
import re
import sys
import wave
from typing import List, Optional, Tuple

from svara import Svara, SvaraError

SAMPLE_RATE = 24000
MAX_CHARS = 5000
# Sentence ends: Latin . ! ? (followed by space), Devanagari danda, CJK full stops.
_SENTENCE = re.compile(r"(?<=[.!?])\s+|(?<=[।॥。！？])")
_NO_SPACE = "。！？，、"  # Chinese and Japanese join without a space


def split_text(text: str, limit: int) -> List[str]:
    """Split into parts of at most `limit` characters, at sentence ends where possible."""
    parts: List[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        for sentence in _SENTENCE.split(paragraph.strip()):
            sentence = " ".join(sentence.split())
            if not sentence:
                continue
            while len(sentence) > limit:  # one very long sentence: cut at a space
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                if current:
                    parts.append(current)
                    current = ""
                parts.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            sep = "" if current[-1:] in _NO_SPACE else " "
            joined = f"{current}{sep}{sentence}".strip()
            if len(joined) > limit:
                parts.append(current)
                current = sentence
            else:
                current = joined
        if current:  # keep paragraph breaks as part boundaries
            parts.append(current)
            current = ""
    return [p for p in parts if p]


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_cues(words: List[Tuple[str, float, float]], max_chars: int = 42) -> List[Tuple[float, float, str]]:
    """Group timed words into subtitle cues of at most `max_chars`, breaking after sentence ends."""
    cues: List[Tuple[float, float, str]] = []
    text, start, end = "", 0.0, 0.0
    for word, w_start, w_end in words:
        if text and len(text) + 1 + len(word) > max_chars:
            cues.append((start, end, text))
            text = ""
        if not text:
            start = w_start
        text = f"{text} {word}".strip()
        end = w_end
        if word[-1:] in ".!?।。！？":
            cues.append((start, end, text))
            text = ""
    if text:
        cues.append((start, end, text))
    return cues


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Narrate long text with Svara TTS Turbo.")
    p.add_argument("textfile", help="UTF-8 text file, or - for stdin")
    p.add_argument("-v", "--voice", required=True, help="voice id, e.g. sv_enhdbrj5")
    p.add_argument("-o", "--out", default="narration.wav")
    p.add_argument("--srt", help="also write subtitles here")
    p.add_argument("-l", "--language", help="force a language (enables number/date normalisation)")
    p.add_argument("-s", "--speed", type=float, help="0.7-1.5; 1.0 is the voice's natural pace")
    p.add_argument("--pause", type=float, default=0.35, help="seconds of silence between parts")
    p.add_argument("--max-chars", type=int, default=2000,
                   help="characters per request (<= 5000; smaller parts render sooner)")
    args = p.parse_args(argv)

    text = sys.stdin.read() if args.textfile == "-" else open(args.textfile, encoding="utf-8").read()
    parts = split_text(text, min(args.max_chars, MAX_CHARS))
    if not parts:
        print("error: no text", file=sys.stderr)
        return 1

    silence = b"\x00\x00" * int(SAMPLE_RATE * args.pause)
    words: List[Tuple[str, float, float]] = []
    offset = 0.0
    with Svara() as client, wave.open(args.out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for i, part in enumerate(parts, 1):
            print(f"[{i}/{len(parts)}] {len(part)} chars", file=sys.stderr)
            try:
                r = client.speech.create_with_timestamps(
                    input=part, voice=args.voice, response_format="pcm",
                    sample_rate=SAMPLE_RATE, language=args.language, speed=args.speed,
                )
            except SvaraError as e:
                print(f"error on part {i}: {e}", file=sys.stderr)
                return 1
            words += [(w, s + offset, e + offset) for w, s, e in r.alignment.words()]
            wav.writeframes(r.audio)
            offset += len(r.audio) / (2 * SAMPLE_RATE)
            if i < len(parts):
                wav.writeframes(silence)
                offset += args.pause

    print(f"wrote {args.out} ({offset:.1f} s)", file=sys.stderr)
    if args.srt:
        with open(args.srt, "w", encoding="utf-8") as f:
            for n, (start, end, line) in enumerate(build_cues(words), 1):
                f.write(f"{n}\n{_srt_time(start)} --> {_srt_time(end)}\n{line}\n\n")
        print(f"wrote {args.srt}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
