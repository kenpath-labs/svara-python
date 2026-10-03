---
name: svara-multilingual
description: Speak Indian, African, Asian and other non-English text naturally with Svara TTS Turbo, which covers 82 languages and switches automatically inside mixed-script text such as Hinglish (Hindi plus English). Use for Hindi, Bengali, Tamil, Telugu, Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia, Urdu, Bhojpuri, Arabic, Swahili, Yoruba, Japanese and other language TTS; for code-mixed text; for forcing a language or normalising numbers and dates; and for fixing the pronunciation of brand names and acronyms with pronunciation dictionaries.
license: Apache-2.0
compatibility: Needs SVARA_API_KEY and network access to api.kenpathlabs.com. Python 3.9+ with `pip install svara-voice`, or any HTTP client.
metadata:
  author: Kenpath Labs
  version: "1.0"
  homepage: https://docs.kenpathlabs.com/pronunciation
---

# Multilingual speech with Svara TTS Turbo

Svara speaks 82 languages:
- 28 from the Indian subcontinent, including Bhojpuri, Maithili, Santali, Konkani, Dogri and Manipuri
- 30 African
- 10 European
- 9 Asian, including Chinese, Japanese and Korean
- 5 from the Middle East

The full table of codes is in [references/languages.md](references/languages.md).

## Write text the way people write it

Svara reads the **script** of the input and switches language on its own,
mid-sentence if needed. Do not split mixed text into per-language requests. Do
not transliterate it, and do not add markup (there is no SSML):

```python
from svara import Svara
client = Svara()

client.speech.save("order.mp3", voice="sv_fhn6tfve",
    input="आपका order ship हो गया है, और कल तक deliver हो जाएगा।")      # Hinglish, one call
client.speech.save("ta.mp3", voice="sv_rum9hcg9",
    input="வணக்கம்! Your appointment is confirmed.")                  # Tamil + English
```

Write each language in its own script: detection works from the script, so
Hindi typed in Latin letters ("aapka order") gives the model no signal that it
is Hindi. If the source is romanised, have the LLM that writes the reply output
Devanagari (or the language's own script) instead.

## When to pass `language=`

Language detection is automatic, so `language` is optional. Pass it when you
want:

1. **Number, date, currency and unit normalisation in that language.** For example,
   `"₹2,500 on 15/08"` is read as Hindi words when `language="hi"` is set.
   Normalisation is on only when a language is given.
2. **To settle an ambiguous script.** Urdu and Arabic share a script, so do
   Hindi, Marathi and Nepali, and Chinese and Japanese share characters.
3. **A low-resource language** whose name you know.

The argument accepts an ISO-639-1 code (`hi`), an ISO-639-3 code (`hin`), a
name (`hindi`), an alias (`mandarin`, `naija`) or a BCP-47 tag (`hi-IN`,
`zh-CN`). On the wire the field is `lang`:

```python
client.speech.create(input="कुल राशि ₹2,500 है।", voice="sv_84sb2v3w", language="hi")
```

```bash
curl -s https://api.kenpathlabs.com/v1/languages        # live list, no key needed
```

`normalize=False` turns normalisation off when the text is already written
the way it should be spoken.

## Choosing a voice for a language

Every voice speaks every language. A voice native to the language gives the
most natural accent. Find one with the **svara-voices** skill:

```bash
python ../svara-voices/scripts/find_voices.py --language bn -q A
```

There are native voices for English, Hindi, Arabic, Portuguese, German,
Gujarati, Tamil, Indonesian, Bengali, Telugu, Punjabi, Turkish, Spanish,
Russian, French, Italian, Korean, Urdu, Kannada, Malayalam, Marathi,
Vietnamese and about 40 more. Japanese and Chinese have no native voice: pick a
voice by style and pass `language="ja"` or `"zh"`.

## Pronunciation dictionaries: brand names, acronyms, jargon

Rules are **respellings**, not IPA. Write each one the way the word should
sound, in whatever script gets that sound across:

```python
from svara import PronunciationRule

d = client.pronunciation_dictionaries.create_from_rules(name="acme-brand", rules=[
    PronunciationRule("SQL", "sequel"),
    PronunciationRule("Kenpath", "Ken path"),
    PronunciationRule("HDFC", "एच डी एफ सी", only_languages=["hi"]),   # only on Hindi requests
])
client.speech.create(input="HDFC का SQL dashboard", voice="sv_84sb2v3w",
                     language="hi", pronunciation_dictionary_id=d.id)
```

- `pronunciation_dictionary_id` works on `create`, `stream`, `stream_input`,
  `prepare`, the timestamp calls, and the LiveKit and Pipecat integrations.
- Rule options are `case_sensitive`, `word_boundaries` (default on),
  `only_languages` and `except_languages`.
- An unknown id is not an error: the global rules apply instead, and the
  response header `x-svara-dictionary: miss` reports it (the SDK warns). Copy
  ids from the console at https://platform.kenpathlabs.com.
- Creation is all-or-nothing. A duplicate name returns 409. A full plan returns 403.
- You can list dictionaries in code, but deletion is done in the console.

REST: `POST /v1/pronunciation-dictionaries/add-from-rules` with
`{"name":"acme-brand","rules":[{"text":"SQL","pronunciation":"sequel"}]}`.

## Tips

- The Devanagari danda `।` and the CJK `。` end a sentence the same way `.` does.
- Use `speed` (0.7–1.5) to slow dense content such as instructions. Do not
  insert extra punctuation to slow it down.
- For a voice agent that answers in the user's language, keep a single voice
  and let the LLM reply in that language. Svara follows the script, so the
  voice stays the same.
