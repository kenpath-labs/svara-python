---
name: svara-narration
description: Turn long text into narrated audio with Svara TTS Turbo, such as audiobooks, article and blog read-alouds, course and e-learning narration, YouTube or explainer video voiceovers, podcasts and news briefings, with optional SRT subtitles from word timestamps. Use when the text is longer than one request (5,000 characters), when audio must be produced in batch, when the user needs captions or karaoke highlighting synced to speech, or for narration in Indian and other languages.
license: Apache-2.0
compatibility: Needs SVARA_API_KEY and network access to api.kenpathlabs.com. The script needs Python 3.9+ and `pip install svara-voice`; converting WAV to mp3 needs ffmpeg.
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com/text-to-speech
---

# Long-form narration with Svara TTS Turbo

A single Svara request takes up to 5,000 characters, which is about five
minutes of audio. Anything longer has to be split. Split at sentence ends,
never mid-sentence, and join the parts without gaps or clicks.

## The bundled script

`scripts/narrate.py` does the whole job:
1. It splits the text at sentence ends. It understands `. ! ?`, the Devanagari `।` and the CJK `。！？`.
2. It renders each part as 24 kHz PCM with word timestamps.
3. It joins the parts into one WAV with a short pause between them.
4. It can also write SRT subtitles.

```bash
pip install svara-voice
export SVARA_API_KEY=sk_live_...
python scripts/narrate.py chapter1.txt --voice sv_enhdbrj5 --out chapter1.wav --srt chapter1.srt
python scripts/narrate.py script_hi.txt -v sv_84sb2v3w --language hi --speed 0.95 -o vo.wav
cat article.md | python scripts/narrate.py - -v sv_kq5snfd4 -o article.wav
ffmpeg -i chapter1.wav -b:a 128k chapter1.mp3        # if you need mp3
```

Options:
- `--max-chars`: characters per request. The default is 2000, so each part renders sooner; the maximum is 5000.
- `--pause`: seconds of silence between parts. The default is 0.35. Paragraph breaks always start a new part.
- `--language`: forces a language and turns on number and date normalisation.
- `--speed`: 0.7–1.5.

Rendering runs at about 7× realtime: 2,400 characters became 162 s of audio in
24 s.

## Picking the voice

Use the **svara-voices** skill. For narration, search the descriptions:

```bash
python ../svara-voices/scripts/find_voices.py storytelling -q A
python ../svara-voices/scripts/find_voices.py --language en --tag calm -q A
```

| Content | Style to look for |
|---|---|
| Fiction, audiobooks | storytelling descriptions, `soft`, `pleasant`, `deep` |
| Courses, explainers | `calm`, `crisp`, `professional` |
| News, briefings | `confident`, `serious`, `professional` |
| Ads, promos | `upbeat`, `excited` |
| Meditation, wellness | `meditative`, `gentle` |

Keep one voice for the whole piece. For a dialogue or a podcast with two hosts,
render each speaker's lines with their own voice in script order. Every part is
a separate request, so this works without extra setup.

## Doing it in your own code

```python
from svara import Svara

client = Svara()
r = client.speech.create_with_timestamps(input=part, voice="sv_enhdbrj5", response_format="pcm")
r.audio                      # bytes; 24 kHz 16-bit mono PCM here
r.alignment.words()          # [(word, start_s, end_s), ...] relative to this part
```

- **Joining parts.** Join raw PCM (or WAV payloads). Don't concatenate mp3
  files, because each one carries its own encoder padding.
- **Timing offsets.** Add each part's duration to the next part's timestamps.
  Compute the duration from the PCM length: `len(pcm) / (2 * 24000)` seconds.
- **Timestamp accuracy.** Timestamps are accurate to the word and approximate to the
  character. That is right for subtitles and karaoke, not for lip-sync at the
  phoneme level.
- **Short single files.** `client.speech.save("intro.mp3", input=text,
  voice=..., response_format="mp3")` is enough for anything under 5,000
  characters. Use `create()`, not `stream()`, for `wav` files, because a
  streamed WAV has a placeholder length header.
- **Batch jobs.** Run parts concurrently with `AsyncSvara`, within your plan's
  concurrent-stream limit. A 429 `too_many_concurrent_requests` means lower the
  concurrency; the SDK already retries. Check the remaining quota first with
  `client.usage.get().characters_remaining`. Roughly 1,000 characters make one
  minute of audio.

## Preparing the text

- Expand or remove anything that should not be read aloud: URLs, Markdown
  symbols, footnote markers and table pipes. There is no SSML, so the text
  itself is the script.
- Leave numbers, dates and currency as written and pass `language=`; Svara
  normalises them.
- Put recurring brand names and acronyms in a pronunciation dictionary (see
  **svara-multilingual**) instead of misspelling them in the source.
- Mixed-language text (Hinglish, English terms in Tamil prose) needs no
  special handling.
