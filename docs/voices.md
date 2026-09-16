# Voices & languages

## Listing voices

```python
for v in client.voices.list(language="hi", gender="female"):
    print(v.voice_id, v.name, v.accent_family, v.quality_band, v.preview_url)
```

`list()` returns the whole catalogue (320 voices, 282 KB) and filters it
client-side; `use_cache=True` reuses the last download.

Each `Voice` has `voice_id`, `name`, `gender`, `accent_family`, `description`,
`category`, `curated`, `is_default`, `preview_url`, `quality_warning`,
`hours`, a `labels` dict (`native_language_code`, `region`, `age`,
`quality_band`, `tags`, …) and the untouched `raw` record. Properties
`.language` (ISO code) and `.quality_band` (`A` is best) read from the labels.

```python
client.voices.retrieve("sv_enhdbrj5")     # one voice
client.voices.preview("sv_enhdbrj5")      # a sample clip, audio/mpeg bytes
```

Voice ids look like `sv_enhdbrj5` and are stable across renames; use them in
code rather than display names.

## Languages

```python
for lang in client.languages.list():
    print(lang.iso1, lang.iso3, lang.name, lang.region, lang.aliases)
```

You do not pick a language per voice: Svara reads the script of `input` and
switches automatically, mid-sentence if need be (Devanagari + Latin in one
line is the normal case). Every voice has a home accent but speaks the other
languages too.

Pass `language=` — any of `iso1`, `iso3`, the name, an alias, or a BCP-47 tag
like `hi-IN` — to force a language. Doing so also enables number, date and
unit normalisation for that language. Leave normalisation on; `normalize=False`
exists for the rare case where the text is already in spoken form.

## Pronunciation dictionaries

Respelling rules created in the console apply to a request when you pass the
dictionary's id (a UUID): `pronunciation_dictionary_id="…"` on `create`,
`stream`, `stream_input`, `prepare` and the timestamps calls, and on the
LiveKit and Pipecat integrations. An id the server cannot find is not an
error — the global rules apply instead — so the SDK warns when the response
reports a miss. Rules are written the way the word should be read
(`SQL` → `sequel`, `NASA` → `नासा`), not in IPA.
See https://docs.kenpathlabs.com/pronunciation.
