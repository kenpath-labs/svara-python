"""Timing instrumentation tests — no network, no API key, no quota.

The synthesis tests run against a local fake stream-input server, so the whole
protocol shape (text in, binary audio out, chunk/flushed/done events) is
exercised without touching the live API.
"""

from __future__ import annotations

import asyncio
import json
import struct

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
    tl = Timeline(trigger_words=8, response_format="pcm", sample_rate=24000)
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


def test_auth_server_ms_subtracts_the_round_trip():
    """A server-side auth target can't be compared against auth_ms directly.

    auth_ms is one RTT of transit plus the server's work, so from far away the
    transit is most of it — 129ms of auth_ms over a 96ms RTT is ~33ms of key
    checking, not a blown 50ms budget.
    """
    tl = Timeline()
    tl.t_start, tl.t_dns, tl.t_tcp = 0.0, 0.0, 0.096      # 96ms RTT
    tl.t_tls, tl.t_open = 0.400, 0.529                    # 129ms upgrade
    tl.split_connect = True
    assert tl.auth_ms == 129.0
    assert tl.auth_server_ms == 33.0


def test_auth_server_ms_never_reports_negative_time():
    """A faster upgrade than the SYN exchange means the RTT estimate is off,
    not that the server finished before it started."""
    tl = Timeline()
    tl.t_start, tl.t_dns, tl.t_tcp = 0.0, 0.0, 0.200
    tl.t_tls, tl.t_open = 0.400, 0.450
    tl.split_connect = True
    assert tl.auth_server_ms == 0.0


def test_auth_server_ms_is_none_without_both_halves():
    tl = Timeline()
    tl.t_start, tl.t_tls, tl.t_open = 0.0, 0.1, 0.2
    tl.split_connect = False
    assert tl.auth_server_ms is None                 # no auth_ms to adjust


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


def test_report_note_names_only_the_withheld_percentiles():
    """At n=10 p90 prints, so naming its threshold in the note makes the caveat
    look like it applies to a column that has a number in it."""
    # Located by content, not by position: the report grew a diagnosis section
    # after this note, and the assertion is about what the note says.
    note = next(ln for ln in _stats_of(range(100, 1100, 100)).report().splitlines()
                if "not enough samples" in ln)
    assert "p99 needs 100" in note
    assert "p90" not in note

    # And with enough samples for every percentile, no caveat at all.
    assert "not enough samples" not in _stats_of(range(1, 101)).report()


def test_report_covers_every_span_that_was_asked_for():
    """The three requested numbers must appear in the pasted table, not just on
    the object: auth, first-text-to-audio, and trigger-word-to-audio."""
    stats = TimingStats()
    tl = Timeline(trigger_words=8)
    tl.t_start, tl.t_dns, tl.t_tcp = 0.0, 0.0, 0.1
    tl.t_tls, tl.t_open, tl.split_connect = 0.4, 0.5, True
    tl.t_first_text, tl.t_trigger_word = 1.0, 2.3
    tl.t_first_chunk, tl.t_first_audio = 2.4, 2.5
    tl.words_at_first_chunk = 8
    stats.add(tl)
    report = stats.report()
    for span in ("auth_ms", "auth_server_ms",
                 "ttfa_from_first_text_ms", "ttfa_from_trigger_ms"):
        assert span in report


def test_report_states_where_the_server_actually_started():
    """The word count is the premise of every latency above it, and a table of
    milliseconds has nowhere to put it."""
    stats = TimingStats()
    for words in (8, 8):
        tl = Timeline(trigger_words=8)
        tl.t_first_text, tl.t_first_audio = 0.0, 1.5
        tl.words_at_first_chunk = words
        stats.add(tl)
    assert "began speaking after 8 words (expected 8)" in stats.report()


def test_report_flags_a_trigger_that_moved():
    """If the server starts somewhere other than predicted, that is the
    headline — louder than any percentile in the table."""
    stats = TimingStats()
    for words in (8, 12):
        tl = Timeline(trigger_words=8)
        tl.t_first_text, tl.t_first_audio = 0.0, 1.5
        tl.words_at_first_chunk = words
        stats.add(tl)
    report = stats.report()
    assert "after 8-12 words" in report
    assert "the trigger moved" in report


# ── ownership, narrative, diagnosis ──────────────────────────────────────────
def _ttfa_split(feed_ms, gen_ms, **kw):
    tl = Timeline(**kw)
    tl.t_first_text = 0.0
    tl.t_first_chunk = feed_ms / 1000.0
    tl.t_first_audio = (feed_ms + gen_ms) / 1000.0
    return tl


def test_every_reported_span_declares_an_owner():
    """The table's whole job is answering 'whose time is this'. A span with no
    owner is a row the reader has to already understand."""
    from svara._timing import OWNERS, REPORT_FIELDS, SPANS

    assert {s.field for s in SPANS} == set(REPORT_FIELDS)
    for span in SPANS:
        assert span.owner in OWNERS, span.field
        assert span.detail, span.field


def test_breakdown_sums_to_time_to_first_audio():
    """feed_to_chunk + generate == ttfa is the one split here that needs no
    estimate, which is why it is the one reported as shares."""
    tl = _ttfa_split(1400, 100)
    parts = tl.breakdown()
    assert [p[0] for p in parts] == ["waiting for text", "generating audio"]
    assert sum(p[2] for p in parts) == tl.ttfa_from_first_text_ms
    assert abs(sum(p[3] for p in parts) - 1.0) < 0.01


def test_breakdown_does_not_blame_the_caller_for_a_shared_span():
    """feed_to_chunk contains the server's eager threshold as well as the
    caller's LLM, so labelling it 'you' would contradict the table."""
    tl = _ttfa_split(1400, 100)
    owners = {label: owner for label, owner, _, _ in tl.breakdown()}
    assert owners["waiting for text"] == "shared"
    assert owners["generating audio"] == "svara"


def test_breakdown_is_empty_without_a_measured_ttfa():
    assert Timeline().breakdown() == []


def test_diagnosis_names_the_wait_when_the_wait_dominates():
    stats = TimingStats()
    for _ in range(3):
        stats.add(_ttfa_split(1400, 100))
    diag = stats.diagnosis()
    assert "DOMINATED BY THE WAIT" in diag
    assert "93% waiting for text" in diag         # 1400 of 1500 ms


def test_diagnosis_names_generation_when_generation_dominates():
    """The case worth a ticket, and it must not be described as the caller's
    LLM being slow."""
    stats = TimingStats()
    for _ in range(3):
        stats.add(_ttfa_split(100, 1400))
    diag = stats.diagnosis()
    assert "DOMINATED BY GENERATION" in diag
    assert "DOMINATED BY THE WAIT" not in diag


def test_diagnosis_warns_when_synthesis_is_slower_than_playback():
    """rtf < 1 outranks every latency in the table: no time-to-first-audio is
    good enough if the audio then stalls."""
    stats = TimingStats()
    # Two runs, not one: a p50 needs two samples to be supported at all, and
    # the diagnosis reads percentiles rather than raw values on purpose.
    for _ in range(2):
        tl = Timeline(response_format="pcm", sample_rate=24000)
        tl.t_first_text, tl.t_first_chunk, tl.t_first_audio = 0.0, 0.3, 0.4
        tl.note_audio(24000 * 2, now=0.0)
        tl.note_audio(24000 * 2, now=4.0)      # 2s of audio in 4s of wall
        stats.add(tl)
    assert "slower than playback" in stats.diagnosis()


def test_diagnosis_warns_about_clipping_and_names_the_volume():
    stats = TimingStats()
    tl = _ttfa_split(300, 100, volume=1.8, volume_applied_by="client")
    tl.clipped_samples = 412
    stats.add(tl)
    diag = stats.diagnosis()
    assert "412 samples clipped" in diag and "1.8" in diag


def test_diagnosis_says_when_the_spans_only_describe_the_survivors():
    stats = TimingStats()
    stats.add(_ttfa_split(300, 100))
    bad = Timeline()
    bad.error = "connect: refused"
    stats.add(bad)
    assert "describe only the ones that succeeded" in stats.diagnosis()


def test_diagnosis_is_empty_without_data():
    assert TimingStats().diagnosis() == ""


def test_report_groups_spans_and_shows_owners():
    stats = TimingStats()
    tl = _ttfa_split(1400, 100)
    tl.t_start, tl.t_dns, tl.t_tcp, tl.t_tls, tl.t_open = 0.0, 0.01, 0.1, 0.3, 0.4
    tl.split_connect = True
    stats.add(tl)
    report = stats.report()
    assert "[connect]" in report and "[first audio]" in report
    assert "owner" in report.splitlines()[0]
    assert "network" in report and "svara" in report


def test_explain_is_ascii_only():
    """This gets pasted into tickets and read on Windows consoles, where cp1252
    turns a stray em-dash into a replacement character."""
    tl = _ttfa_split(1400, 100, response_format="pcm", sample_rate=24000,
                     trigger_words=8, volume=1.5, volume_applied_by="client")
    tl.t_start, tl.t_open = 0.0, 0.4
    tl.words_at_first_chunk = 8
    tl.note_audio(24000 * 2, now=1.5)
    tl.note_audio(24000 * 2, now=2.0)
    tl.gain_seconds, tl.clipped_samples = 0.004, 3
    text = tl.explain()
    text.encode("ascii")                       # raises if anything crept in
    assert "first audio" in text and "93.3%" in text   # 1400 of 1500 ms
    assert "clipped" in text                   # the warning made it through


def test_explain_and_report_survive_an_empty_timeline():
    assert Timeline().explain() == ""
    assert TimingStats().report()


def test_gain_ms_is_only_billed_when_we_did_the_work():
    """Server-side volume costs this process nothing, and reporting a number
    there would invent latency that nobody paid."""
    client = Timeline(volume=1.5, volume_applied_by="client", gain_seconds=0.002)
    server = Timeline(volume=1.5, volume_applied_by="server", gain_seconds=0.002)
    assert client.gain_ms == 2.0
    assert server.gain_ms is None


def test_clipping_ratio_is_scale_free():
    """Ratio, not a count, so the number means the same thing on a one-second
    prompt and a one-minute one."""
    tl = Timeline(response_format="pcm", sample_rate=24000,
                  volume=1.9, volume_applied_by="client")
    tl.audio_bytes = 24000 * 2                 # 24000 samples
    tl.clipped_samples = 240
    assert tl.clipping_ratio == 0.01


def test_as_dict_stays_clean_when_no_volume_was_asked_for():
    d = Timeline().as_dict()
    assert "volume" not in d and "clipped_samples" not in d
    d = Timeline(volume=1.4, volume_applied_by="client").as_dict()
    assert d["volume"] == 1.4 and d["volume_applied_by"] == "client"


def test_summary_records_which_side_applied_the_gain():
    """A doubled gain is only diagnosable from a log line if the line says who
    applied it."""
    tl = Timeline(volume=1.4, volume_applied_by="client")
    tl.t_first_text, tl.t_first_audio = 0.0, 0.4
    assert "vol=1.4x@client" in tl.summary()


def test_legend_explains_only_the_spans_that_printed():
    stats = TimingStats()
    stats.add(_ttfa_split(1400, 100))
    legend = stats.legend()
    assert "generate_ms" in legend
    assert "dns_ms" not in legend              # never measured, never explained
    assert "owners:" in legend


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
        # Eight words: exactly the eager trigger, so t_trigger_word lands on
        # the last one rather than never being stamped.
        ["Hello ", "there ", "how ", "are ", "you ", "doing ", "today ", "friend "],
        voice="sv_test", on_timing=seen.append, on_event=chunks.append,
    ):
        audio += buf

    assert audio                                     # audio actually flowed
    assert len(seen) == 1
    tl = seen[0]

    # feed accounting
    assert tl.messages_sent == 8
    assert tl.words_sent == 8
    assert tl.trigger_words == 8                     # 2x chunk_words, defaults
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
    assert tl.words_at_first_chunk == 8               # all words were out by then
    assert tl.generate_ms is not None
    assert tl.feed_to_chunk_ms is not None

    # both control events observed, in order
    assert tl.events == ["chunk", "flushed", "done"]
    assert tl.t_flushed is not None and tl.t_done is not None
    assert chunks and chunks[0].peek == "there"
    assert tl.error is None
    assert received[-1] == {"text": ""}              # EOS still sent


@pytest.mark.parametrize(
    "chunk_words, peek_words, expected",
    [
        (4, 2, 8),     # defaults
        (8, 1, 16),    # tracks chunk_words...
        (10, 5, 20),   # ...and ignores peek_words entirely
        (2, 1, 8),     # below the floor: the server clamps to 4, so predict 8
    ],
)
async def test_trigger_words_predicts_the_measured_eager_threshold(
    fake_server, chunk_words, peek_words, expected
):
    """Pins the formula against live A/B numbers.

    Measured against the real server at 6 words/s: the trigger sits at
    ``2 × chunk_words`` — a whole next chunk buffered before it commits to the
    current one — and ``peek_words`` does not move it. Requests under the
    documented floor of 4 are clamped silently, so 2/1 behaves as 4/2.
    """
    base, _ = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)
    seen: list = []
    async for _ in client.speech.stream_input(
        ["hi "], voice="sv_test", chunk_words=chunk_words, peek_words=peek_words,
        on_timing=seen.append,
    ):
        pass
    assert seen[0].trigger_words == expected


async def test_stream_input_applies_volume_and_bills_it_to_the_caller(fake_server):
    """End to end: the gain reaches the audio, and the cost of applying it is
    recorded as the caller's rather than vanishing into the process."""
    base, _ = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)
    seen: list = []

    audio = b""
    async for buf in client.speech.stream_input(
        ["hi there "], voice="sv_test", response_format="pcm",
        volume=2.0, on_timing=seen.append,
    ):
        audio += buf

    # The fake server sends samples of 0x0100 (256); doubled they are 512.
    assert audio
    assert struct.unpack("<h", audio[:2])[0] == 512

    tl = seen[0]
    assert tl.volume == 2.0
    assert tl.volume_applied_by == "client"      # no server support yet
    assert tl.gain_ms is not None                # measured, not hidden
    assert tl.clipped_samples == 0               # 512 is nowhere near the rail
    assert "vol=2.0x@client" in tl.summary()


async def test_stream_input_without_volume_leaves_audio_untouched(fake_server):
    base, _ = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)
    seen: list = []
    audio = b""
    async for buf in client.speech.stream_input(["hi there "], voice="sv_test",
                                                on_timing=seen.append):
        audio += buf
    assert struct.unpack("<h", audio[:2])[0] == 256    # as the server sent it
    assert seen[0].volume is None and seen[0].gain_ms is None


async def test_non_eager_mode_has_no_trigger(fake_server):
    """Only eager mode starts before end-of-input, so a threshold is meaningless
    elsewhere — 0 rather than a number that would read as real."""
    base, _ = fake_server
    client = AsyncSvara(api_key="sk_test", base_url=base)
    seen: list = []
    async for _ in client.speech.stream_input(["hi "], voice="sv_test", mode="sentence",
                                              on_timing=seen.append):
        pass
    assert seen[0].trigger_words == 0
    assert seen[0].feed_to_trigger_ms is None


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
