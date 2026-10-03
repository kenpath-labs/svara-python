#!/usr/bin/env python3
"""Find Svara TTS voices. Standard library only; no API key needed.

The voice catalogue (GET /v1/voices) and voice previews are public, so an agent
can pick a voice before anyone has a key.

    python find_voices.py --language hi --gender female
    python find_voices.py --language tamil --tag calm --quality A
    python find_voices.py warm storytelling            # words from description/labels
    python find_voices.py --language en --json --limit 3
    python find_voices.py --preview sv_enhdbrj5        # writes sv_enhdbrj5.mp3
    python find_voices.py --languages                  # the 82 language codes
    python find_voices.py --markdown > ../references/voice-catalogue.md

Ranking: quality band A first, then B, then C. Prefer A for anything a
customer will hear.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = os.environ.get("SVARA_BASE_URL", "https://api.kenpathlabs.com").rstrip("/")
USER_AGENT = "svara-skills/find_voices"


def _get(path: str) -> bytes:
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def load_languages() -> List[Dict[str, Any]]:
    return json.loads(_get("/v1/languages"))["languages"]


def load_voices() -> List[Dict[str, Any]]:
    return json.loads(_get("/v1/voices"))["voices"]


def _language_codes(query: str, languages: List[Dict[str, Any]]) -> set:
    """Every spelling of the language `query` names: iso1, iso3, name, aliases."""
    q = query.lower().split("-")[0]  # hi-IN -> hi
    for lang in languages:
        names = {lang.get("iso1"), lang.get("iso3"), (lang.get("name") or "").lower()}
        names |= {a.lower() for a in lang.get("aliases") or []}
        names.discard(None)
        if q in names:
            return {n for n in names if n} | {(lang.get("name") or "").lower()}
    return {q}


def _haystack(v: Dict[str, Any]) -> str:
    labels = v.get("labels") or {}
    parts = [v.get("voice_id"), v.get("name"), v.get("gender"), v.get("accent_family"),
             v.get("description")]
    parts += [str(x) for k, x in labels.items() if k != "preview_text"]
    return " ".join(str(p) for p in parts if p).lower()


def filter_voices(voices: List[Dict[str, Any]], args: argparse.Namespace,
                  languages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    codes = _language_codes(args.language, languages) if args.language else None
    out = []
    for v in voices:
        labels = v.get("labels") or {}
        if codes is not None:
            spoken = {str(labels.get("native_language_code", "")).lower(),
                      str(labels.get("native_language", "")).lower()}
            if not spoken & codes:
                continue
        if args.gender and (v.get("gender") or "").lower() != args.gender.lower():
            continue
        if args.age and (labels.get("age") or "").lower() != args.age.lower():
            continue
        if args.tag and (labels.get("tags") or "").lower() != args.tag.lower():
            continue
        if args.region and (labels.get("region") or "").lower() != args.region.lower():
            continue
        if args.quality and (labels.get("quality_band") or "Z") > args.quality.upper():
            continue
        hay = _haystack(v)
        if any(w.lower() not in hay for w in args.words):
            continue
        out.append(v)
    out.sort(key=lambda v: ((v.get("labels") or {}).get("quality_band") or "Z", v.get("name") or ""))
    return out


def _row(v: Dict[str, Any]) -> str:
    labels = v.get("labels") or {}
    desc = (v.get("description") or "").replace("\n", " ")
    if len(desc) > 70:
        desc = desc[:67] + "..."
    return (f"{v['voice_id']:<12} {v.get('name') or '':<14} "
            f"{labels.get('native_language_code') or '':<5} {v.get('gender') or '':<7} "
            f"{labels.get('age') or '':<11} {labels.get('tags') or '':<12} "
            f"{labels.get('quality_band') or '-'}  {desc}")


def catalogue_markdown(voices: List[Dict[str, Any]], languages: List[Dict[str, Any]]) -> str:
    import datetime

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for v in voices:
        labels = v.get("labels") or {}
        key = labels.get("native_language") or labels.get("native_language_code") or "Other"
        groups.setdefault(key, []).append(v)
    lines = [
        "# Svara voice catalogue",
        "",
        f"Snapshot of `GET /v1/voices` taken {datetime.date.today().isoformat()}: "
        f"{len(voices)} voices, {len(languages)} languages. The live catalogue wins; "
        "regenerate with `python scripts/find_voices.py --markdown`.",
        "",
        "Grouped by the voice's native language (its home accent). Every voice speaks "
        "every language. Band A is the best quality; prefer it for anything customers hear.",
        "",
    ]
    for lang in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        lines += [f"## {lang}", "", "| voice_id | name | gender | age | style | band | accent |",
                  "|---|---|---|---|---|---|---|"]
        rows = sorted(groups[lang], key=lambda v: ((v.get("labels") or {}).get("quality_band") or "Z",
                                                   v.get("name") or ""))
        for v in rows:
            labels = v.get("labels") or {}
            lines.append(f"| `{v['voice_id']}` | {v.get('name') or ''} | {v.get('gender') or ''} | "
                         f"{labels.get('age') or ''} | {labels.get('tags') or ''} | "
                         f"{labels.get('quality_band') or ''} | {labels.get('accent') or ''} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Find a Svara TTS voice (no API key needed).")
    p.add_argument("words", nargs="*", help="free-text words that must all appear "
                   "(name, accent, description, labels)")
    p.add_argument("-l", "--language", help="hi, hin, Hindi, hi-IN, tamil, ...")
    p.add_argument("-g", "--gender", help="female | male | neutral")
    p.add_argument("--age", help="young | middle_aged | old")
    p.add_argument("-t", "--tag", help="style tag: calm, confident, professional, casual, "
                   "pleasant, deep, excited, meditative, upbeat, ...")
    p.add_argument("--region", help="india | world | africa | english_accents | central_asia")
    p.add_argument("-q", "--quality", help="best band to allow down to: A (best only), B, C")
    p.add_argument("-n", "--limit", type=int, default=15)
    p.add_argument("--json", action="store_true", help="print raw voice records")
    p.add_argument("--preview", metavar="VOICE_ID", help="download that voice's sample mp3")
    p.add_argument("--languages", action="store_true", help="list supported languages")
    p.add_argument("--markdown", action="store_true",
                   help="print the whole catalogue as Markdown, grouped by native language")
    args = p.parse_args(argv)

    try:
        if args.preview:
            path = f"{args.preview}.mp3"
            with open(path, "wb") as f:
                f.write(_get(f"/v1/voices/{args.preview}/preview"))
            print(f"wrote {path}")
            return 0
        languages = load_languages()
        voices = [] if args.languages else load_voices()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        print(f"error: could not reach {BASE_URL}: {e}", file=sys.stderr)
        return 1

    if args.languages:
        for lang in languages:
            print(f"{lang.get('iso1') or '-':<4} {lang['iso3']:<5} {lang['name']:<24} "
                  f"{lang.get('region') or ''}")
        return 0
    if args.markdown:
        print(catalogue_markdown(voices, languages))
        return 0
    matches = filter_voices(voices, args, languages)
    shown = matches[: args.limit] if args.limit > 0 else matches
    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
    else:
        for v in shown:
            print(_row(v))
    print(f"\n{len(matches)} matching voices (showing {len(shown)}). "
          f"Use the voice_id (sv_...) as `voice=`.", file=sys.stderr)
    if not matches and args.language:
        print("Try loosening the filters. Note --language only matches a voice's native "
              "language, and every voice speaks all 82 languages: dropping --language "
              "and passing language=<code> on the speech call also works.", file=sys.stderr)
    return 0 if matches else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # piped into `head`
        sys.exit(0)
