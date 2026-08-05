"""Timing instrumentation tests — no network, no API key, no quota.

The synthesis tests run against a local fake stream-input server, so the whole
protocol shape (text in, binary audio out, chunk/flushed/done events) is
exercised without touching the live API.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from svara import AsyncSvara, Timeline, TimingStats
from svara._timing import WordCounter, percentile


# ── word counting ────────────────────────────────────────────────────────────
def test_word_counter_only_counts_completed_words():
    """A word isn't complete until whitespace follows — the same rule the
    server's buffer uses, so the client's count tracks the eager trigger."""
    c = WordCounter()
    assert c.add("Hel") == 0          # mid-word
    assert c.add("lo") == 0           # still mid-word
    assert c.add(" ") == 1            # now terminated
    assert c.add("there world ") == 3


def test_word_counter_matches_naive_split_on_whitespace_terminated_text():
    text = "the quick brown fox jumps over the lazy dog "
    c = WordCounter()
    for ch in text:                   # worst case: one character per delta
        c.add(ch)
    assert c.count == len(text.split())


def test_word_counter_handles_multiword_and_empty_pieces():
    c = WordCounter()
    c.add("")
    assert c.count == 0
    assert c.add("one two three") == 2   # "three" not yet terminated
    assert c.add("!") == 2
    assert c.add(" ") == 3


# ── derived spans ────────────────────────────────────────────────────────────
def test_timeline_spans_and_ownership_split():
    tl = Timeline(trigger_words=6, response_format="pcm", sample_rate=24000)
    tl.t_start, tl.t_dns, tl.t_tcp, tl.t_tls, tl.t_open = 0.0, 0.010, 0.050, 0.150, 0.400
    tl.split_connect = True
    tl.t_first_text = 1.000
    tl.t_trigger_word = 1.900
    tl.t_first_audio = 2.300

    assert tl.dns_ms == 10.0
    assert tl.tcp_ms == 40.0
    assert tl.tls_ms == 100.0
    assert tl.auth_ms == 250.0             # the upgrade round-trip
    assert tl.handshake_ms == 400.0

    # The split that decides whose problem a slow reply is. The server
    # announces its first chunk at 2.2s, having waited past the configured
    # trigger for more words.
    tl.t_first_chunk = 2.200
    assert tl.feed_to_trigger_ms == 900.0        # to the *configured* threshold
    assert tl.trigger_to_chunk_ms == 300.0       # still waiting, past it
    assert tl.feed_to_chunk_ms == 1200.0         # whole input-side wait
    assert tl.generate_ms == 100.0               # the model alone
    assert tl.ttfa_from_first_text_ms == 1300.0  # what the caller hears
    assert tl.feed_to_chunk_ms + tl.generate_ms == tl.ttfa_from_first_text_ms


def test_generate_ms_excludes_the_input_side_wait():
    """The headline number must not blame the model for the caller's LLM.

    Two streams with identical generation but very different feed rates should
    report the same generate_ms and different feed_to_chunk_ms.
    """
    fast, slow = Timeline(), Timeline()
    fast.t_first_text, fast.t_first_chunk, fast.t_first_audio = 0.0, 0.30, 0.40
    slow.t_first_text, slow.t_first_chunk, slow.t_first_audio = 0.0, 1.50, 1.60
    assert fast.generate_ms == slow.generate_ms == 100.0
    assert fast.feed_to_chunk_ms == 300.0 and slow.feed_to_chunk_ms == 1500.0


def test_auth_ms_is_none_when_connect_could_not_be_staged():
    """Better to report nothing than to report TLS+upgrade mislabelled as auth."""
    tl = Timeline()
    tl.t_start, tl.t_tls, tl.t_open = 0.0, 0.1, 0.4
    tl.split_connect = False
    assert tl.auth_ms is None
    assert tl.handshake_ms == 400.0


def test_audio_duration_respects_format_width():
    pcm = Timeline(response_format="pcm", sample_rate=24000)
    pcm.audio_bytes = 24000 * 2                      # 1s of 16-bit
    assert pcm.audio_seconds == 1.0

    ulaw = Timeline(response_format="ulaw", sample_rate=8000)
    ulaw.audio_bytes = 8000                          # 1s of 8-bit G.711
    assert ulaw.audio_seconds == 1.0

    mp3 = Timeline(response_format="mp3", sample_rate=24000)
    mp3.audio_bytes = 50_000
    assert mp3.audio_seconds is None                 # container: not derivable
    assert mp3.realtime_factor is None


def test_realtime_factor_and_frame_gap():
    tl = Timeline(response_format="pcm", sample_rate=24000)
    tl.note_audio(24000 * 2, now=10.0)               # 1s of audio, first frame
    tl.note_audio(24000 * 2, now=10.5)               # another 1s, 0.5s later
    assert tl.audio_seconds == 2.0
    assert tl.realtime_factor == 4.0                 # 2s of audio in 0.5s wall
    assert tl.max_frame_gap_ms == 500.0


def test_realtime_factor_below_one_means_audible_gaps():
    tl = Timeline(response_format="pcm", sample_rate=24000)
    tl.note_audio(24000 * 2, now=0.0)
    tl.note_audio(24000 * 2, now=4.0)                # 2s of audio took 4s
    assert tl.realtime_factor == 0.5


def test_summary_omits_spans_that_never_happened():
    tl = Timeline()
    tl.t_first_text, tl.t_first_audio = 1.0, 1.4
    out = tl.summary()
    assert "ttfa=400.0ms" in out
    assert "connect=" not in out                     # never connected
    assert "rtf=" not in out


# ── aggregation ──────────────────────────────────────────────────────────────
def _stats_of(values_ms):
    stats = TimingStats()
    for v in values_ms:
        tl = Timeline()
        tl.t_first_text, tl.t_first_audio = 0.0, v / 1000.0
        stats.add(tl)
    return stats


def test_percentiles_and_report():
    stats = _stats_of((100, 200, 300, 400, 5000))    # one bad tail
    assert len(stats) == 5
    s = stats.summarize("ttfa_from_first_text_ms")
    assert s["n"] == 5 and s["p50"] == 300.0 and s["max"] == 5000.0
    assert "ttfa_from_first_text_ms" in stats.report()


def test_unsupported_percentiles_are_withheld_not_faked():
    """At n=10 nearest-rank puts p90, p99 and max on the same sample. Printing
    all three reads as agreement between measurements when it is one number
    three times."""
    s10 = _stats_of(range(100, 1100, 100))           # exactly 10 samples
    assert s10.summarize("ttfa_from_first_text_ms")["p90"] is not None
    assert s10.summarize("ttfa_from_first_text_ms")["p99"] is None

    s5 = _stats_of((100, 200, 300, 400, 500))
    assert s5.summarize("ttfa_from_first_text_ms")["p50"] is not None
    assert s5.summarize("ttfa_from_first_text_ms")["p90"] is None

    s100 = _stats_of(range(1, 101))
    assert s100.summarize("ttfa_from_first_text_ms")["p99"] is not None


def test_report_marks_withheld_percentiles_and_explains():
    report = _stats_of((100, 200, 300)).report()
    assert "-" in report
    assert "p99 needs 100" in report                  # says why, not just blank


def test_percentile_ignores_missing_values():
    assert percentile([], 50) is None
    assert percentile([1.0, None, 3.0], 50) == 3.0   # type: ignore[list-item]


# ── against a fake stream-input server ───────────────────────────────────────
@pytest.fixture
async def fake_server():
    """Minimal stand-in for /v1/audio/speech/stream-input.

    Replies with a `chunk` event, two binary PCM frames, then `flushed` and
    `done` — enough to drive every stamp in the timeline.
    """
    received: list = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            if msg.get("text") == "":                # EOS
                await ws.send(json.dumps({"type": "chunk", "text": "hello", "peek": "there"}))
                await ws.send(b"\x00\x01" * 1200)
                await asyncio.sleep(0.02)            # a measurable frame gap
                await ws.send(b"\x00\x01" * 1200)
                await ws.send(json.dumps({"type": "flushed"}))
                await ws.send(json.dumps({"type": "done"}))
                return

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", received
    server.close()
    await server.wait_closed()


async def test_stream_input_populates_timeline(fake_server):
    base, received = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)
    seen: list = []
    chunks: list = []

    audio = b""
    async for buf in client.speech.stream_input(
        ["Hello ", "there ", "how ", "are ", "you ", "doing ", "today "],
        voice="sv_test", on_timing=seen.append, on_event=chunks.append,
    ):
        audio += buf

    assert audio                                     # audio actually flowed
    assert len(seen) == 1
    tl = seen[0]

    # feed accounting
    assert tl.messages_sent == 7
    assert tl.words_sent == 7
    assert tl.trigger_words == 6                     # chunk_words 4 + peek_words 2
    assert tl.t_trigger_word is not None

    # audio accounting
    assert tl.audio_frames == 2
    assert tl.audio_bytes == len(audio)
    assert tl.ttfa_from_first_text_ms is not None
    assert tl.ttfa_from_trigger_ms is not None
    assert tl.max_frame_gap_ms and tl.max_frame_gap_ms > 10

    # the server's chunk announcement is what separates waiting from generating
    assert tl.t_first_chunk is not None
    assert tl.first_chunk_words == 1                  # fake server says "hello"
    assert tl.words_at_first_chunk == 7               # all words were out by then
    assert tl.generate_ms is not None
    assert tl.feed_to_chunk_ms is not None

    # both control events observed, in order
    assert tl.events == ["chunk", "flushed", "done"]
    assert tl.t_flushed is not None and tl.t_done is not None
    assert chunks and chunks[0].peek == "there"
    assert tl.error is None
    assert received[-1] == {"text": ""}              # EOS still sent


async def test_timeline_reports_no_auth_split_over_plaintext(fake_server):
    """ws:// can't be staged, so auth_ms is None rather than a wrong number."""
    base, _ = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)
    seen: list = []
    async for _ in client.speech.stream_input(["hi there "], voice="sv_test",
                                              on_timing=seen.append):
        pass
    tl = seen[0]
    assert tl.split_connect is False
    assert tl.auth_ms is None
    assert tl.handshake_ms is not None               # still measured end-to-end


async def test_on_timing_fires_even_when_connect_fails():
    """A timeline for a failed connect is the most useful one to have."""
    client = AsyncSvara(api_key="sk_test", base_url="http://127.0.0.1:1")
    seen: list = []
    with pytest.raises(Exception):
        async for _ in client.speech.stream_input(["hi "], voice="sv_test",
                                                  on_timing=seen.append):
            pass
    assert len(seen) == 1 and seen[0].error


async def test_on_timing_exception_does_not_break_synthesis(fake_server):
    base, _ = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)

    def boom(_tl):
        raise RuntimeError("callback blew up")

    audio = b""
    async for buf in client.speech.stream_input(["hi there "], voice="sv_test",
                                                on_timing=boom):
        audio += buf
    assert audio                                     # synthesis unaffected


async def test_svara_timing_env_logs_one_line(fake_server, monkeypatch, caplog):
    base, _ = fake_server
    monkeypatch.setenv("SVARA_TIMING", "1")
    client = AsyncSvara(api_key="sk_test", base_url=base)
    with caplog.at_level("INFO", logger="svara.timing"):
        async for _ in client.speech.stream_input(["hi there "], voice="sv_test"):
            pass
    assert any(r.message.startswith("svara ") for r in caplog.records)
