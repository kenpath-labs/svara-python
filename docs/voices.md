# Voices

List everything available to your key:

```python
for v in client.voices.list():
    print(v.voice_id, v.name, v.gender, v.language, v.labels.get("quality_band"))
```

Each `Voice` has `voice_id`, `name`, `gender`, `accent_family`, `description`,
`category`, `curated`, `is_default`, `preview_url`, and a `labels` dict
(`native_language_code`, `region`, `age`, `quality_band`, `tags`, …). The
`.language` property returns the best-effort ISO code.

## Picking a voice

- Filter by language / quality band:

  ```python
  hindi_a = [v for v in client.voices.list()
             if v.language == "hi" and v.labels.get("quality_band") == "A"]
  ```

- `preview_url` (when present) is a ready-made audio sample.
- Voice ids look like `sv_enhdbrj5`. Pass one to `voice=` on any speech call.

## Language & code-switching

You don't select a language per voice — Svara reads the script of your `input`
and switches automatically (e.g. Devanagari + Latin in one sentence). A voice
has a "home" accent/language but will speak other languages too. Force one with
`language="hi"` only if you need to override detection.

