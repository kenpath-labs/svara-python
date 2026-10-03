"""Offline tests for the scripts shipped with the agent skills in skills/."""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "skills"


def _load(rel: str):
    spec = importlib.util.spec_from_file_location(pathlib.Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


find_voices = _load("svara-voices/scripts/find_voices.py")
narrate = _load("svara-narration/scripts/narrate.py")

LANGS = [
    {"iso3": "hin", "iso1": "hi", "name": "Hindi", "region": "indic", "aliases": []},
    {"iso3": "zho", "iso1": "zh", "name": "Chinese", "region": "asia", "aliases": ["mandarin"]},
]
VOICES = [
    {"voice_id": "sv_b", "name": "Bee", "gender": "female", "accent_family": "hindi",
     "description": "warm storyteller", "labels": {"native_language": "Hindi",
     "native_language_code": "hi", "tags": "calm", "quality_band": "B", "age": "young"}},
    {"voice_id": "sv_a", "name": "Ay", "gender": "female", "accent_family": "hindi",
     "description": "crisp", "labels": {"native_language": "Hindi",
     "native_language_code": "hi", "tags": "crisp", "quality_band": "A", "age": "old"}},
    {"voice_id": "sv_m", "name": "Em", "gender": "male", "accent_family": "japanese",
     "description": "calm", "labels": {"native_language": "Odia",
     "native_language_code": "or", "tags": "calm", "quality_band": "A"}},
]


def _args(*words, **kw):
    base = dict(language=None, gender=None, age=None, tag=None, region=None, quality=None)
    base.update(kw)
    return argparse.Namespace(words=list(words), **base)


def test_find_voices_language_forms_and_ranking():
    for lang in ("hi", "hin", "Hindi", "hi-IN"):
        got = find_voices.filter_voices(VOICES, _args(language=lang), LANGS)
        assert [v["voice_id"] for v in got] == ["sv_a", "sv_b"]  # band A first


def test_find_voices_language_ignores_accent_family():
    # An Odia voice with a "japanese" accent is not a Japanese-native voice.
    assert find_voices.filter_voices(VOICES, _args(language="ja"), LANGS) == []


def test_find_voices_filters_and_words():
    assert [v["voice_id"] for v in find_voices.filter_voices(VOICES, _args(gender="male"), LANGS)] == ["sv_m"]
    assert [v["voice_id"] for v in find_voices.filter_voices(VOICES, _args(quality="A", tag="calm"), LANGS)] == ["sv_m"]
    assert [v["voice_id"] for v in find_voices.filter_voices(VOICES, _args("warm", "story"), LANGS)] == ["sv_b"]


def test_catalogue_markdown_groups_by_language():
    md = find_voices.catalogue_markdown(VOICES, LANGS)
    assert "## Hindi" in md and "## Odia" in md and "`sv_a`" in md


@pytest.mark.parametrize("limit", [20, 60, 5000])
def test_split_text_is_lossless_and_bounded(limit):
    text = "Hello there. This is a test! नमस्ते दुनिया। यह परीक्षा है। 你好。世界！\n\n" + "word " * 40 + "end."
    parts = narrate.split_text(text, limit)
    assert all(len(p) <= limit for p in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "").replace("\n", "")


def test_split_text_joins_cjk_without_space():
    assert narrate.split_text("你好。世界！", 100) == ["你好。世界！"]


def test_build_cues_breaks_at_sentence_end_and_length():
    words = [("Hi.", 0.0, 0.3), ("This", 0.4, 0.6), ("is", 0.6, 0.7), ("long", 0.7, 0.9)]
    assert narrate.build_cues(words, max_chars=8) == [(0.0, 0.3, "Hi."), (0.4, 0.7, "This is"), (0.7, 0.9, "long")]


def test_narrate_end_to_end_offline(tmp_path, monkeypatch):
    from svara._client import _SyncSpeech
    from svara.types import Alignment, TimestampedAudio

    calls = []

    def fake(self, *, input, voice, **kw):
        calls.append((input, kw))
        n = len(input)
        align = Alignment(list(input), [i * 0.05 for i in range(n)], [(i + 1) * 0.05 for i in range(n)])
        return TimestampedAudio(b"\x01\x00" * int(24000 * n * 0.05), align)

    monkeypatch.setattr(_SyncSpeech, "create_with_timestamps", fake)
    monkeypatch.setenv("SVARA_API_KEY", "sk_test")
    src = tmp_path / "in.txt"
    src.write_text("First sentence here. Second one!\n\nNew paragraph.", encoding="utf-8")
    out, srt = tmp_path / "o.wav", tmp_path / "o.srt"
    assert narrate.main([str(src), "-v", "sv_x", "-o", str(out), "--srt", str(srt), "-l", "hi"]) == 0
    assert len(calls) == 2 and calls[0][1]["response_format"] == "pcm" and calls[0][1]["language"] == "hi"
    with wave.open(str(out)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
    cues = srt.read_text(encoding="utf-8")
    assert "Second one!" in cues and "New paragraph." in cues
