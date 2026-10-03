---
name: svara-voices
description: Find, compare and preview Svara TTS voices (320 voices, 82 languages) and choose the right voice id for a task, filtered by language, gender, age, style (calm, confident, professional, storytelling...) and quality. Use when the user asks which voice to use, wants a Hindi, Tamil, Arabic, British, Indian-English or other accent, wants a male or female voice, wants to hear voice samples, or code needs a Svara voice id (sv_...). Works without an API key.
license: Apache-2.0
compatibility: Needs network access to api.kenpathlabs.com. The script uses only the Python 3 standard library; no API key is required.
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com/voices
---

# Finding Svara voices

Svara TTS Turbo has 320 voices. Each voice has a home accent, and every voice
speaks all 82 languages: pick a voice for how it sounds, not for the language
of the text. Voice ids look like `sv_enhdbrj5`. They are stable, so put them in
code. Display names are not ids.

The catalogue and the previews are **public**. No key is needed to browse,
filter or listen.

## Quickest path: the bundled script

`scripts/find_voices.py` needs only the Python standard library. It fetches the
live catalogue and ranks quality band A first.

```bash
python scripts/find_voices.py --language hi --gender female       # Hindi-native women
python scripts/find_voices.py -l tamil -q A                       # band A Tamil voices only
python scripts/find_voices.py -l en --tag professional            # English, professional style
python scripts/find_voices.py warm storytelling                   # words from descriptions
python scripts/find_voices.py british male                        # accent words work too
python scripts/find_voices.py --preview sv_enhdbrj5               # writes sv_enhdbrj5.mp3
python scripts/find_voices.py --languages                         # all 82 language codes
python scripts/find_voices.py -l hi --json -n 3                   # full records
```

The output columns are voice id, name, native language, gender, age, style
tag, quality band and description. Use the `voice_id` as `voice=`.

The filters are:
- `--language` takes `hi`, `hin`, `Hindi` or `hi-IN`.
- `--gender` takes `female`, `male` or `neutral`.
- `--age` takes `young`, `middle_aged` or `old`.
- `--tag` takes a style: `calm`, `confident`, `professional`, `casual`, `pleasant`, `deep`, `excited`, `meditative`, `upbeat`, `serious`, `gentle`, `crisp` and others.
- `--region` takes `india`, `world`, `africa`, `english_accents` or `central_asia`.
- `-q A` keeps band A only.

Free-text words must all appear somewhere in the record.

## Offline: the snapshot

[references/voice-catalogue.md](references/voice-catalogue.md) lists every
voice, grouped by native language, with gender, age, style, band and accent.
Use it when there is no network. The live API is the authority, so check an id
there before it ships.

## With the SDK or CLI (key required to construct the client)

```python
from svara import Svara
client = Svara()
for v in client.voices.list(language="hi", gender="female"):          # client-side filter
    print(v.voice_id, v.name, v.labels.get("tags"), v.quality_band, v.description)
client.voices.search("tamil male calm")      # every word must match; cached, so repeat searches are free
client.voices.retrieve("sv_enhdbrj5")
client.voices.preview("sv_enhdbrj5").save("aanya.mp3")
```

```bash
svara voices --language hi --gender female
svara voices --json
```

## With plain HTTP

```bash
curl -s https://api.kenpathlabs.com/v1/voices | jq -r '.voices[]
  | select(.labels.native_language_code=="hi" and .gender=="female")
  | [.voice_id, .name, .labels.tags, .labels.quality_band] | @tsv'
curl -s https://api.kenpathlabs.com/v1/voices/sv_enhdbrj5/preview -o aanya.mp3
```

Each record carries:
- `voice_id`, `name`, `gender`
- `accent_family`, `description`
- `labels.native_language` and `labels.native_language_code`
- `labels.accent`, `labels.region`, `labels.age`
- `labels.tags` (the style)
- `labels.quality_band` (`A` is best, then `B`, then `C`)
- `labels.preview_text`
- `preview_url`

`/v1/voices` has no server-side filters. Download the list once (282 KB) and
filter it locally.

## Choosing well

1. **Prefer band A** for anything customers will hear. There are 104 band-A voices.
2. **Match the accent to the audience.** For example, use a Hindi-native voice for a
   Hindi IVR and an Indian-English voice for Indian customers who are addressed
   in English. The text language doesn't restrict the voice: a Bengali-native
   voice reads English and Hindi too.
3. **Match the style to the use:**
   - `professional`, `confident` or `crisp` for support lines and announcements
   - `calm` or `pleasant` for assistants
   - `meditative` or `gentle` for wellness content
   - a storyteller description for audiobooks
   - `excited` or `upbeat` for ads
4. **Listen before you commit.** Download two or three previews, or synthesise the
   user's own sentence with each candidate (svara-tts skill), and let the user
   choose.
5. **No native voice for the language?** Japanese, Chinese and some other
   languages have no voice native to them. Pick by gender and style, then pass
   `language="ja"` (or the right code) on the speech call.

Starting points from the snapshot (check them against the live catalogue):

| Need | Voice |
|---|---|
| Documented default, warm female | `sv_enhdbrj5` Aanya (Bengali-native) |
| Hindi female, warm | `sv_fhn6tfve` Anjali |
| Hindi male, composed | `sv_84sb2v3w` Kabir |
| Telugu male, used in the telephony docs | `sv_r22w7pwe` Aarav |
| Indian-English female, serious | `sv_92xqyy9r` Alia |
| British male, crisp | `sv_kq5snfd4` Oliver |
| American female, calm | `sv_a5thzx8v` Grace |
| Tamil male, calm | `sv_rum9hcg9` Karthik |

You can also try voices in the console playground at
https://platform.kenpathlabs.com/dashboard/playground.
