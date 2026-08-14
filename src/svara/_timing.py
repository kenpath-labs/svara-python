"""Client-side latency instrumentation for the input-streaming WebSocket.

Server-side timings only cover generation. This module measures what the
*caller* actually experiences, and splits it into spans that each point at a
different owner:

    dns ─ tcp ─ tls ─ auth │ feed…trigger │ trigger → first audio byte
    └──── network ────┘ └─ us ─┘ └─ their LLM ─┘ └──── the model ────┘

The interesting one is ``auth``. A ``wss://`` connection is born as an HTTP/1.1
``GET`` carrying the API key, answered with ``101 Switching Protocols`` (or
401/403) — so the key check happens *before* any WebSocket frame exists, and
nothing else is on the wire during it. asyncio calls the protocol factory
before connecting and ``connection_made()`` after the TLS handshake completes,
so stamping there separates TLS from the upgrade round-trip.

``trigger`` is the point where the server has enough buffered to start
speaking: ``2 × chunk_words`` **whole words**, with ``chunk_words`` clamped up
to 4. Note *words*, not messages — LLM deltas are sub-word fragments
(``"Hel"``, ``"lo"``, ``" there"``), so counting messages gives a number
unrelated to what the model waits for. Both are recorded; the word one is the
one to quote.

That formula is an expectation, not a contract, so nothing here depends on it:
:attr:`Timeline.words_at_first_chunk` records where the server actually began.
Quote the measured one and treat a gap between the two as news.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("svara.timing")

# Bytes per sample for the raw formats. Container formats (mp3/opus/…) carry no
# fixed frame size, so audio duration — and therefore realtime factor — is not
# derivable from a byte count.
_BYTES_PER_SAMPLE = {"pcm": 2, "ulaw": 1, "alaw": 1}


def _ms(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Milliseconds between two perf_counter stamps, or None if either is unset."""
    if a is None or b is None:
        return None
    return round((b - a) * 1000, 1)


class WordCounter:
    """Counts *complete* words across a stream of arbitrary fragments.

    A word is complete only once whitespace follows it, which is what the
    server's buffer does too — so feeding ``"Hel"``, ``"lo"``, ``" there "``
    yields 0, 0, 1.
    """

    __slots__ = ("count", "_tail")

    def __init__(self) -> None:
        self.count = 0
        self._tail = ""

    def add(self, piece: str) -> int:
        buf = self._tail + piece
        parts = buf.split()
        if buf and not buf[-1].isspace():
            self._tail = parts[-1] if parts else ""
            self.count += max(0, len(parts) - 1)
        else:
            self._tail = ""
            self.count += len(parts)
        return self.count


@dataclass
class Timeline:
    """One utterance's client-side latency record.

    All ``t_*`` fields are raw :func:`time.perf_counter` stamps (seconds,
    monotonic, not wall-clock). The ``*_ms`` properties are what you report.
    """

    # ── connect ──────────────────────────────────────────────────────────
    t_start: Optional[float] = None
    t_dns: Optional[float] = None
    t_tcp: Optional[float] = None
    t_tls: Optional[float] = None
    t_open: Optional[float] = None
    #: False when the transport couldn't be staged (proxy, older websockets),
    #: in which case tls/auth are folded together into :attr:`handshake_ms`.
    split_connect: bool = False

    # ── text fed in ──────────────────────────────────────────────────────
    t_first_text: Optional[float] = None
    t_trigger_message: Optional[float] = None
    t_trigger_word: Optional[float] = None
    trigger_words: int = 6
    messages_sent: int = 0
    words_sent: int = 0
    chars_sent: int = 0

    # ── audio out ────────────────────────────────────────────────────────
    #: When the server announced its first chunk. It sends `chunk` immediately
    #: before that chunk's audio, so this is the closest observable proxy for
    #: "synthesis started" and splits waiting from generating.
    t_first_chunk: Optional[float] = None
    first_chunk_words: int = 0
    #: Words sent by the time the server announced that chunk — the *observed*
    #: eager threshold, measured rather than assumed. It runs at ``2 ×
    #: chunk_words``: the server wants a whole next chunk in hand before it
    #: commits to the current one, so the 4/2 defaults start it at 8 words.
    #: Compare against :attr:`trigger_words` to notice the day that changes.
    words_at_first_chunk: int = 0
    t_first_audio: Optional[float] = None
    t_last_audio: Optional[float] = None
    audio_bytes: int = 0
    audio_frames: int = 0
    max_frame_gap: float = 0.0

    # ── volume ───────────────────────────────────────────────────────────
    #: Requested gain, or None if the caller didn't ask for one.
    volume: Optional[float] = None
    #: ``"client"``, ``"server"``, or None. Exactly one party may scale the
    #: samples; recording which one is what makes a doubled gain diagnosable
    #: instead of merely audible.
    volume_applied_by: Optional[str] = None
    #: Wall time this process spent scaling audio. It is real latency the
    #: caller pays, and it belongs to the caller, not to Svara — so it is
    #: measured rather than quietly folded into the spans around it.
    gain_seconds: float = 0.0
    #: Samples that hit the rails. Non-zero means the gain is too high for this
    #: material and the peaks are being flattened.
    clipped_samples: int = 0

    # ── control ──────────────────────────────────────────────────────────
    t_flushed: Optional[float] = None
    t_done: Optional[float] = None
    t_end: Optional[float] = None

    # ── context ──────────────────────────────────────────────────────────
    request_id: Optional[str] = None
    voice: Optional[str] = None
    mode: Optional[str] = None
    response_format: str = "pcm"
    sample_rate: int = 24000
    error: Optional[str] = None
    events: List[str] = field(default_factory=list)

    # ── connect spans ────────────────────────────────────────────────────
    @property
    def dns_ms(self) -> Optional[float]:
        return _ms(self.t_start, self.t_dns)

    @property
    def tcp_ms(self) -> Optional[float]:
        return _ms(self.t_dns, self.t_tcp)

    @property
    def tls_ms(self) -> Optional[float]:
        return _ms(self.t_tcp, self.t_tls)

    @property
    def auth_ms(self) -> Optional[float]:
        """HTTP upgrade round-trip: API key validation + admission control.

        ``None`` when the connection couldn't be staged — see
        :attr:`split_connect`.
        """
        return _ms(self.t_tls, self.t_open) if self.split_connect else None

    @property
    def auth_server_ms(self) -> Optional[float]:
        """:attr:`auth_ms` with one network round-trip subtracted out.

        The upgrade is a request and a response over an already-open socket, so
        ``auth_ms`` is *one RTT of transit plus whatever the server did*. From
        far away the transit dominates: at a 96 ms RTT a 129 ms ``auth_ms`` is
        about 33 ms of actual key checking. Comparing a raw ``auth_ms`` against
        a server-side target therefore reads as a regression that isn't one.

        The RTT estimate is :attr:`tcp_ms` — the SYN/SYN-ACK exchange, which is
        the one clean round-trip measurable here. It is an estimate: it can be
        inflated by a slow local network stack, and it says nothing about
        asymmetric routing. Treat this as an upper bound on server time that is
        much closer to the truth than ``auth_ms``, and quote ``auth_ms`` when
        the question is what the *caller* waits for.
        """
        if self.auth_ms is None or self.tcp_ms is None:
            return None
        return round(max(0.0, self.auth_ms - self.tcp_ms), 1)

    @property
    def handshake_ms(self) -> Optional[float]:
        """Everything from first syscall to a usable socket."""
        return _ms(self.t_start, self.t_open)

    # ── synthesis spans ──────────────────────────────────────────────────
    @property
    def feed_to_trigger_ms(self) -> Optional[float]:
        """First text out → the word the eager trigger is expected to fire on.

        Purely local: it is the caller's own feed clocked against the expected
        threshold, so it says nothing about the server. Its use is as a control
        — it should come out near ``trigger_words ÷ feed rate``, and a run where
        it doesn't is a run where the measuring machine was busy, which
        disqualifies every other span in that timeline.

        For the real input-side wait use :attr:`feed_to_chunk_ms`, which needs
        no assumption about where the threshold sits.
        """
        return _ms(self.t_first_text, self.t_trigger_word)

    @property
    def feed_to_chunk_ms(self) -> Optional[float]:
        """First text out → server announces its first chunk.

        The whole input-side wait: the caller's LLM producing enough words,
        plus transit and the server's batching window. Pair with
        :attr:`generate_ms` and the two account for time-to-first-audio.
        """
        return _ms(self.t_first_text, self.t_first_chunk)

    @property
    def ttfa_from_trigger_ms(self) -> Optional[float]:
        """Trigger word sent → first audio byte.

        The span people usually ask for by name — "how long after the Nth word
        do we hear something". It is *not* the model's time. It carries uplink
        transit for that word, the server continuing to wait for a full
        lookahead chunk beyond the trigger, and the downlink of the first audio
        frame. In practice the waiting dominates: at the 4/2 defaults this runs
        several times :attr:`generate_ms`.

        Use it to answer the question as asked; use :attr:`generate_ms` before
        concluding anything about generation.
        """
        return _ms(self.t_trigger_word, self.t_first_audio)

    @property
    def trigger_to_chunk_ms(self) -> Optional[float]:
        """Trigger word sent → server announces its first chunk.

        Transit plus however long the server waited before deciding it had
        enough to speak. Everything here is ahead of the model.
        """
        return _ms(self.t_trigger_word, self.t_first_chunk)

    @property
    def generate_ms(self) -> Optional[float]:
        """First chunk announced → first audio byte. **The model's number.**

        Transit cancels out of this one. Both messages travel the same path in
        the same direction, so the gap between their arrival times equals the
        gap between their send times — network latency shifts both equally.
        That makes this the cleanest span here, not a padded one.

        Two caveats. It can read low when generation is fast enough that the
        announcement and the first audio frame coalesce into one TCP segment.
        And it measures the *first* chunk only; later chunks are covered by
        :attr:`max_frame_gap_ms` and :attr:`realtime_factor`.
        """
        return _ms(self.t_first_chunk, self.t_first_audio)

    @property
    def ttfa_from_first_text_ms(self) -> Optional[float]:
        """First text out → first audio byte. What the caller perceives."""
        return _ms(self.t_first_text, self.t_first_audio)

    @property
    def ttfa_from_trigger_message_ms(self) -> Optional[float]:
        """As above but counting messages rather than words — for comparison
        only; a delta is not a word."""
        return _ms(self.t_trigger_message, self.t_first_audio)

    @property
    def max_frame_gap_ms(self) -> Optional[float]:
        """Longest silence between audio frames. Large values are audible
        mid-utterance stalls, which sound worse than a slow start."""
        return round(self.max_frame_gap * 1000, 1) if self.audio_frames > 1 else None

    @property
    def audio_seconds(self) -> Optional[float]:
        """Playable duration. ``None`` for container formats."""
        bps = _BYTES_PER_SAMPLE.get(self.response_format)
        if not bps or not self.sample_rate:
            return None
        return round(self.audio_bytes / (self.sample_rate * bps), 3)

    @property
    def realtime_factor(self) -> Optional[float]:
        """Audio seconds produced per wall second, measured from first byte.

        Below 1.0 means synthesis can't keep ahead of playback and the caller
        will hear gaps no matter how good time-to-first-audio was.
        """
        secs = self.audio_seconds
        if secs is None or self.t_first_audio is None or self.t_last_audio is None:
            return None
        wall = self.t_last_audio - self.t_first_audio
        return round(secs / wall, 2) if wall > 0 else None

    @property
    def gain_ms(self) -> Optional[float]:
        """Client-side volume cost. ``None`` when nothing was scaled locally."""
        if self.volume_applied_by != "client":
            return None
        return round(self.gain_seconds * 1000, 1)

    @property
    def clipping_ratio(self) -> Optional[float]:
        """Fraction of samples driven into the rails, 0.0-1.0.

        A handful of clipped samples on a loud consonant is inaudible; a few
        percent is distortion. Reported as a ratio so the number means the same
        thing on a one-second prompt and a one-minute one.
        """
        if self.volume_applied_by != "client":
            return None
        bps = _BYTES_PER_SAMPLE.get(self.response_format)
        if not bps or not self.audio_bytes:
            return None
        return round(self.clipped_samples / (self.audio_bytes / bps), 4)

    @property
    def total_ms(self) -> Optional[float]:
        return _ms(self.t_start, self.t_end)

    # ── narrative ────────────────────────────────────────────────────────
    def breakdown(self) -> List[tuple]:
        """Time-to-first-audio split into parts that sum to it.

        Returns ``(label, owner, ms, share)`` with ``share`` a fraction of
        ttfa. The identity ``feed_to_chunk + generate == ttfa`` is the one
        decomposition here that needs no estimate, which is why it is the one
        reported.
        """
        ttfa = self.ttfa_from_first_text_ms
        if not ttfa:
            return []
        parts = [
            # "shared", not "you": this span is the caller's token rate *and*
            # the server's eager threshold, and the two can't be separated
            # without an estimate. Matching the owner in SPANS keeps the
            # narrative from contradicting the table above it.
            ("waiting for text", "shared", self.feed_to_chunk_ms),
            ("generating audio", "svara", self.generate_ms),
        ]
        return [(label, owner, ms, round(ms / ttfa, 3))
                for label, owner, ms in parts if ms is not None]

    def explain(self) -> str:
        """This one utterance in plain English, ASCII only.

        The table says what the numbers are; this says what they mean. Kept in
        the library rather than in an example so every caller gets it, not just
        the people who read ``examples/timing.py``.
        """
        out: List[str] = []
        if self.handshake_ms is not None:
            line = f"connect      {self.handshake_ms:>8.1f} ms  (once per call)"
            if self.split_connect and self.auth_server_ms is not None:
                line += f" - of which ~{self.auth_server_ms:.1f} ms was the key check"
            out.append(line)

        ttfa = self.ttfa_from_first_text_ms
        if ttfa is not None:
            out.append(f"first audio  {ttfa:>8.1f} ms  after your first word went out")
            for label, owner, ms, share in self.breakdown():
                out.append(f"  {label:<18} {ms:>8.1f} ms  {share * 100:>5.1f}%  "
                           f"[{owner}]")
            if self.words_at_first_chunk:
                note = (f"  the server began speaking after "
                        f"{self.words_at_first_chunk} words")
                if self.trigger_words and self.words_at_first_chunk != self.trigger_words:
                    note += f" (expected {self.trigger_words})"
                out.append(note)

        if self.realtime_factor is not None:
            verdict = "keeps ahead of playback" if self.realtime_factor >= 1.0 \
                else "SLOWER THAN PLAYBACK - the caller will hear gaps"
            out.append(f"throughput   {self.realtime_factor:>8.2f} x   {verdict}")
        if self.max_frame_gap_ms is not None and self.max_frame_gap_ms > AUDIBLE_GAP_MS:
            out.append(f"stall        {self.max_frame_gap_ms:>8.1f} ms  "
                       f"longest mid-utterance silence")
        if self.gain_ms is not None:
            line = f"volume       {self.gain_ms:>8.1f} ms  applied client-side"
            if self.volume is not None:
                line += f" at {self.volume}x"
            out.append(line)
            ratio = self.clipping_ratio
            if ratio:
                out.append(f"  WARNING: {ratio * 100:.2f}% of samples clipped - "
                           f"lower volume")
        if self.error:
            out.append(f"error        {self.error}")
        return "\n".join(out)

    def as_dict(self) -> Dict[str, Any]:
        """Flat, JSON-safe record — for logs, telemetry, or a support ticket."""
        d: Dict[str, Any] = {
            "request_id": self.request_id,
            "voice": self.voice,
            "mode": self.mode,
            "response_format": self.response_format,
            "sample_rate": self.sample_rate,
            "dns_ms": self.dns_ms,
            "tcp_ms": self.tcp_ms,
            "tls_ms": self.tls_ms,
            "auth_ms": self.auth_ms,
            "handshake_ms": self.handshake_ms,
            "feed_to_chunk_ms": self.feed_to_chunk_ms,
            "generate_ms": self.generate_ms,
            "ttfa_from_first_text_ms": self.ttfa_from_first_text_ms,
            "feed_to_trigger_ms": self.feed_to_trigger_ms,
            "trigger_to_chunk_ms": self.trigger_to_chunk_ms,
            "ttfa_from_trigger_ms": self.ttfa_from_trigger_ms,
            "trigger_words": self.trigger_words,
            "words_at_first_chunk": self.words_at_first_chunk,
            "first_chunk_words": self.first_chunk_words,
            "messages_sent": self.messages_sent,
            "words_sent": self.words_sent,
            "chars_sent": self.chars_sent,
            "audio_bytes": self.audio_bytes,
            "audio_frames": self.audio_frames,
            "audio_seconds": self.audio_seconds,
            "realtime_factor": self.realtime_factor,
            "max_frame_gap_ms": self.max_frame_gap_ms,
            "total_ms": self.total_ms,
        }
        # Only present when a gain was actually asked for, so an untouched
        # stream's record doesn't grow four null columns.
        if self.volume is not None:
            d.update({
                "volume": self.volume,
                "volume_applied_by": self.volume_applied_by,
                "gain_ms": self.gain_ms,
                "clipped_samples": self.clipped_samples,
                "clipping_ratio": self.clipping_ratio,
            })
        if self.error:
            d["error"] = self.error
        return d

    def summary(self) -> str:
        """One line, for ``SVARA_TIMING=1``. Omits spans that never happened."""
        bits = []
        if self.handshake_ms is not None:
            conn = f"connect={self.handshake_ms}ms"
            if self.split_connect:
                conn += (f" (dns={self.dns_ms} tcp={self.tcp_ms} "
                         f"tls={self.tls_ms} auth={self.auth_ms})")
            bits.append(conn)
        if self.ttfa_from_first_text_ms is not None:
            bits.append(f"ttfa={self.ttfa_from_first_text_ms}ms")
        if self.feed_to_chunk_ms is not None:
            bits.append(f"feed={self.feed_to_chunk_ms}ms")
        if self.generate_ms is not None:
            bits.append(f"model={self.generate_ms}ms")
        if self.audio_seconds is not None:
            bits.append(f"audio={self.audio_seconds}s")
        if self.realtime_factor is not None:
            bits.append(f"rtf={self.realtime_factor}x")
        if self.max_frame_gap_ms is not None:
            bits.append(f"maxgap={self.max_frame_gap_ms}ms")
        if self.volume is not None:
            # Which side scaled matters more than the number: a doubled gain is
            # only diagnosable if the log says who applied it.
            bits.append(f"vol={self.volume}x@{self.volume_applied_by or 'none'}")
            if self.clipped_samples:
                bits.append(f"clipped={self.clipped_samples}")
        if self.error:
            bits.append(f"error={self.error}")
        return "svara " + " ".join(bits)

    # ── recording helpers (used by the client) ───────────────────────────
    def note_audio(self, nbytes: int, now: Optional[float] = None) -> None:
        now = time.perf_counter() if now is None else now
        if self.t_first_audio is None:
            self.t_first_audio = now
        elif self.t_last_audio is not None:
            self.max_frame_gap = max(self.max_frame_gap, now - self.t_last_audio)
        self.t_last_audio = now
        self.audio_bytes += nbytes
        self.audio_frames += 1


def emit(timeline: Timeline) -> None:
    """Log the summary if ``SVARA_TIMING`` is set. Never raises."""
    flag = (os.environ.get("SVARA_TIMING") or "").strip().lower()
    if flag in ("", "0", "false", "no"):
        return
    try:
        if flag == "json":
            import json
            log.info(json.dumps(timeline.as_dict()))
        else:
            log.info(timeline.summary())
    except Exception:  # instrumentation must never break synthesis
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────
#: Who a span belongs to — the only question a latency table is usually asked.
#:
#: ``shared`` is not a hedge. Some spans genuinely contain two parties' time
#: (``auth_ms`` is transit plus key checking; ``feed_to_chunk_ms`` is the
#: caller's LLM plus the server's batching window) and splitting them requires
#: an estimate. Labelling those honestly is the difference between a table that
#: assigns blame and one that assigns it correctly.
OWNERS = {
    "network": "the path between you and the API",
    "you": "your text feed / LLM",
    "svara": "Svara's server and model",
    "shared": "more than one party - see the note",
    "total": "a roll-up of the rows above it",
}


@dataclass(frozen=True)
class Span:
    """One reportable span: where it comes from and what to do about it."""

    field: str
    owner: str
    group: str
    detail: str


#: Every span worth a distribution, in the order it occurs, grouped the way it
#: gets read: what you pay once, what you wait for, and what happens after
#: audio starts.
SPANS = (
    Span("dns_ms", "network", "connect",
         "resolving the API hostname; cache or reuse the connection to avoid it"),
    Span("tcp_ms", "network", "connect",
         "SYN/SYN-ACK - one clean round-trip, and the RTT estimate used below"),
    Span("tls_ms", "network", "connect",
         "TLS handshake; a session ticket makes this cheaper on reconnect"),
    Span("auth_ms", "shared", "connect",
         "API key check AS YOU EXPERIENCE IT - includes one RTT of transit"),
    Span("auth_server_ms", "svara", "connect",
         "the same minus that RTT - compare server-side targets against THIS"),
    Span("handshake_ms", "total", "connect",
         "first syscall to a usable socket; paid once per stream_input call"),

    # feed_to_chunk + generate = ttfa_from_first_text, with no assumption about
    # where the server's threshold sits.
    Span("feed_to_chunk_ms", "shared", "first audio",
         "waiting for enough text to start: your LLM, plus the server's window"),
    Span("generate_ms", "svara", "first audio",
         "the model's own time to first audio; transit cancels out of it"),
    Span("ttfa_from_first_text_ms", "total", "first audio",
         "first text out to first audio in - what the caller actually perceives"),
    # From the word the eager trigger fires on. Ahead of the model, not the
    # model: it still carries the server's remaining wait for a full lookahead
    # chunk. Kept because it is the span most people ask for by name.
    Span("ttfa_from_trigger_ms", "shared", "first audio",
         "from the trigger word; still mostly the server waiting, not generating"),

    Span("max_frame_gap_ms", "svara", "after audio starts",
         "longest silence mid-utterance; audible stalls sound worse than a slow start"),
    # Spread here is a quality signal, not a latency one: identical input
    # should produce near-identical duration, so a wide p50->max gap means the
    # model is pacing inconsistently or emitting trailing content.
    Span("audio_seconds", "svara", "after audio starts",
         "playable duration; identical input should vary very little"),
    Span("realtime_factor", "svara", "after audio starts",
         "audio seconds per wall second; below 1.0 and playback will stall"),
    Span("gain_ms", "you", "after audio starts",
         "CPU spent applying volume client-side, in your own process"),
)

#: Field names only — the historical shape of this constant.
REPORT_FIELDS = tuple(s.field for s in SPANS)

_SPAN_BY_FIELD = {s.field: s for s in SPANS}

#: A frame gap past this is where a listener starts hearing a stall rather than
#: natural pacing. Rule of thumb, not a measurement.
AUDIBLE_GAP_MS = 250.0


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. No numpy dependency for ten numbers."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round((pct / 100.0) * len(vals) + 0.5)) - 1))
    return vals[k]


class TimingStats:
    """Collects :class:`Timeline` records and reports distributions.

    Percentiles matter more than averages here: a p50 of 400 ms with a p95 of
    3 s is a very different product from a flat 600 ms, and a mean hides it.
    """

    def __init__(self) -> None:
        self.timelines: List[Timeline] = []

    def add(self, timeline: Timeline) -> None:
        self.timelines.append(timeline)

    def __len__(self) -> int:
        return len(self.timelines)

    @property
    def errors(self) -> int:
        return sum(1 for t in self.timelines if t.error)

    def series(self, field_name: str) -> List[float]:
        out = []
        for t in self.timelines:
            v = getattr(t, field_name, None)
            if v is not None:
                out.append(v)
        return out

    @staticmethod
    def _supported(pct: float, n: int) -> bool:
        """Whether ``n`` samples can express a ``pct`` percentile at all.

        With nearest-rank on 10 samples, p90 and p99 both resolve to the
        maximum — printing them side by side reads as three measurements
        agreeing when it is one sample repeated. p90 needs 10 samples, p99
        needs 100.
        """
        return n >= int(round(1.0 / (1.0 - pct / 100.0)))

    def summarize(self, field_name: str) -> Dict[str, Optional[float]]:
        vals = self.series(field_name)
        if not vals:
            return {"n": 0, "p50": None, "p90": None, "p99": None, "min": None, "max": None}
        n = len(vals)
        return {
            "n": n,
            "p50": percentile(vals, 50) if self._supported(50, n) else None,
            "p90": percentile(vals, 90) if self._supported(90, n) else None,
            "p99": percentile(vals, 99) if self._supported(99, n) else None,
            "min": min(vals),
            "max": max(vals),
        }

    def report(self, fields: Sequence[str] = REPORT_FIELDS) -> str:
        """Fixed-width table — paste-able into a ticket or a status update.

        Grouped by when the time is spent and tagged with who owns it. A flat
        list of thirteen identifiers is readable only by someone who already
        knows the model, which defeats the purpose of writing it down.
        """
        rows = [(f, self.summarize(f)) for f in fields]
        rows = [(f, s) for f, s in rows if s["n"]]
        width = max((len(f) for f, _ in rows), default=20)
        owner_w = max((len(_SPAN_BY_FIELD[f].owner)
                       for f, _ in rows if f in _SPAN_BY_FIELD), default=5)
        head = (f"{'span'.ljust(width)}  {'owner'.ljust(owner_w)}  {'n':>4} "
                f"{'p50':>9} {'p90':>9} {'p99':>9} {'min':>9} {'max':>9}")
        lines = [head, "-" * len(head)]

        def cell(v: Optional[float]) -> str:
            # "-" means the sample size can't support this percentile, not that
            # the span was never measured.
            return f"{v:>9.1f}" if v is not None else f"{'-':>9}"

        seen_group: Optional[str] = None
        for name, s in rows:
            span = _SPAN_BY_FIELD.get(name)
            group = span.group if span else "other"
            if group != seen_group:
                lines.append(f"[{group}]")
                seen_group = group
            owner = span.owner if span else ""
            lines.append(
                f"{name.ljust(width)}  {owner.ljust(owner_w)}  {s['n']:>4} "
                f"{cell(s['p50'])} {cell(s['p90'])} {cell(s['p99'])} "
                f"{cell(s['min'])} {cell(s['max'])}"
            )
        lines.append(f"\n{len(self)} utterances, {self.errors} errors")

        # Where the server actually began speaking. A latency table can't show
        # this — it's a word count — but every span above it is only meaningful
        # if this held steady, and a threshold that moves between runs is the
        # first thing to know.
        starts = [tl.words_at_first_chunk for tl in self.timelines
                  if tl.words_at_first_chunk]
        if starts:
            expected = next((tl.trigger_words for tl in self.timelines
                             if tl.trigger_words), None)
            lo, hi = min(starts), max(starts)
            seen = f"{lo}" if lo == hi else f"{lo}-{hi}"
            note = f"server began speaking after {seen} words"
            if expected:
                note += (f" (expected {expected})" if lo == hi == expected
                         else f" -- expected {expected}, so the trigger moved")
            lines.append(note)

        # Name only the percentiles actually withheld. Listing p90's threshold
        # in a run where p90 printed makes the note look like it applies to a
        # column that is right there with a number in it.
        n = len(self)
        missing = [(f"p{p}", need) for p, need in ((50, 2), (90, 10), (99, 100))
                   if not self._supported(p, n)]
        if missing:
            want = ", ".join(f"{name} needs {need}" for name, need in missing)
            lines.append(f"'-' = not enough samples for that percentile "
                         f"({want}; have {n})")

        diag = self.diagnosis()
        if diag:
            lines.append("\n" + diag)
        return "\n".join(lines)

    # ── interpretation ───────────────────────────────────────────────────
    def diagnosis(self) -> str:
        """What the table means and what to do about it, in plain ASCII.

        A distribution answers "how slow"; almost every reader actually arrived
        wanting "whose fault, and what do I change". Working that out by eye
        from thirteen rows is exactly the step people get wrong — usually by
        reading a large ``ttfa_from_trigger_ms`` as the model being slow when
        it is mostly the server still waiting for lookahead.
        """
        if not self.timelines:
            return ""
        out: List[str] = ["what this means:"]

        feed = self.summarize("feed_to_chunk_ms")["p50"]
        gen = self.summarize("generate_ms")["p50"]
        ttfa = self.summarize("ttfa_from_first_text_ms")["p50"]

        if feed is not None and gen is not None and ttfa:
            fs, gs = feed / ttfa * 100, gen / ttfa * 100
            out.append(f"  time to first audio (p50 {ttfa:.0f} ms) splits "
                       f"{fs:.0f}% waiting for text / {gs:.0f}% generating.")
            if fs >= 60:
                # The common case, and the one most often misread.
                out.append("  DOMINATED BY THE WAIT, not by synthesis. Part of it is your")
                out.append("  LLM's token rate and part is the eager threshold; raising")
                out.append("  --chunk-words makes it worse and 4 is the floor, so the")
                out.append("  default is already the fastest setting. Speeding up the")
                out.append("  model would not move this number much.")
            elif gs >= 60:
                out.append("  DOMINATED BY GENERATION. This one is Svara's to improve -")
                out.append("  worth a ticket with this table attached.")
            else:
                out.append("  Split fairly evenly; no single owner to chase.")
        elif ttfa:
            out.append(f"  time to first audio (p50 {ttfa:.0f} ms), but the "
                       f"waiting/generating split was not captured.")

        warn: List[str] = []
        rtf = self.summarize("realtime_factor")["p50"]
        if rtf is not None and rtf < 1.0:
            warn.append(f"realtime_factor p50 is {rtf:.2f} - synthesis is slower than "
                        f"playback, so the caller hears gaps no matter how good "
                        f"time-to-first-audio is. This outranks every latency above.")
        gap = self.summarize("max_frame_gap_ms")
        worst = gap["max"]
        if worst is not None and worst > AUDIBLE_GAP_MS:
            warn.append(f"longest mid-utterance silence was {worst:.0f} ms - a stall "
                        f"partway through sounds worse than a slow start.")
        clipped = sum(t.clipped_samples for t in self.timelines)
        if clipped:
            vols = {t.volume for t in self.timelines if t.volume is not None}
            at = f" at volume={max(vols)}x" if vols else ""
            warn.append(f"{clipped} samples clipped{at} - the gain is too high for "
                        f"this material and peaks are being flattened.")
        if self.errors:
            warn.append(f"{self.errors} of {len(self)} utterances failed; the spans "
                        f"above describe only the ones that succeeded.")
        if warn:
            out.append("")
            out.extend(f"  WARNING: {w}" for w in warn)
        return "\n".join(out)

    def legend(self, fields: Sequence[str] = REPORT_FIELDS) -> str:
        """One line per span explaining what it is. For a first-time reader."""
        rows = [f for f in fields if self.summarize(f)["n"]]
        width = max((len(f) for f in rows), default=20)
        lines = ["what each span is:"]
        for f in rows:
            span = _SPAN_BY_FIELD.get(f)
            if span:
                lines.append(f"  {f.ljust(width)}  {span.detail}")
        lines.append("")
        lines.append("owners:")
        for owner, meaning in OWNERS.items():
            if any(_SPAN_BY_FIELD[f].owner == owner for f in rows
                   if f in _SPAN_BY_FIELD):
                lines.append(f"  {owner.ljust(8)}  {meaning}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Staged connect
# ─────────────────────────────────────────────────────────────────────────────
def default_ssl_context() -> ssl.SSLContext:
    """Trust certifi, matching httpx.

    The HTTP path goes through httpx (certifi) and the WebSocket path through
    ``websockets`` (OS trust store by default). On a machine whose OS store
    carries an expired root, that divergence shows up as HTTP synthesis working
    while the WebSocket fails with an opaque CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _proxy_configured() -> bool:
    return any(os.environ.get(v) for v in
               ("HTTPS_PROXY", "https_proxy", "WSS_PROXY", "wss_proxy", "ALL_PROXY", "all_proxy"))


async def staged_connect(
    url: str,
    *,
    timeline: Timeline,
    additional_headers: Dict[str, str],
    open_timeout: Optional[float] = None,
    **kwargs: Any,
):
    """Open the WebSocket in stages so each span is separately measurable.

    Falls back to a plain ``websockets.connect`` — leaving ``auth_ms`` as None
    and folding everything into ``handshake_ms`` — when the connection can't be
    staged: behind a proxy, on ``ws://``, or on a websockets version without
    the asyncio client. Correctness first; the measurement is a bonus.
    """
    import websockets

    timeline.t_start = time.perf_counter()

    def _hdr_kwargs() -> Dict[str, Any]:
        # websockets renamed extra_headers -> additional_headers in v14.
        try:
            import inspect
            params = inspect.signature(websockets.connect.__init__).parameters
            key = "additional_headers" if "additional_headers" in params else "extra_headers"
        except Exception:
            key = "additional_headers"
        return {key: additional_headers}

    async def _plain():
        kw = dict(kwargs)
        kw.update(_hdr_kwargs())
        if open_timeout is not None:
            kw["open_timeout"] = open_timeout
        ws = await websockets.connect(url, **kw)
        timeline.t_open = time.perf_counter()
        return ws

    if not url.startswith("wss://") or _proxy_configured():
        return await _plain()

    try:
        from websockets.asyncio.client import ClientConnection
        from websockets.asyncio.client import connect as aio_connect
    except ImportError:  # websockets < 13 — legacy client only
        return await _plain()

    import asyncio
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or 443
    if not host:
        return await _plain()

    loop = asyncio.get_event_loop()
    sock: Optional[socket.socket] = None
    try:
        infos: Sequence[Any] = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        timeline.t_dns = time.perf_counter()
        if not infos:
            return await _plain()

        af, stype, proto, _canon, sockaddr = infos[0]
        sock = socket.socket(af, stype, proto)
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        await loop.sock_connect(sock, sockaddr)
        timeline.t_tcp = time.perf_counter()

        # asyncio builds the protocol before connecting and calls
        # connection_made() once TLS is up — the only client-side seam between
        # the TLS handshake and the HTTP upgrade that carries the API key.
        class _Stamped(ClientConnection):
            def connection_made(self, transport: Any) -> None:
                timeline.t_tls = time.perf_counter()
                super().connection_made(transport)

        kw = dict(kwargs)
        ssl_ctx = kw.pop("ssl", None) or default_ssl_context()
        kw.update(_hdr_kwargs())
        if open_timeout is not None:
            kw["open_timeout"] = open_timeout
        ws = await aio_connect(
            url,
            sock=sock,
            ssl=ssl_ctx,
            server_hostname=host,
            create_connection=_Stamped,
            **kw,
        )
        timeline.t_open = time.perf_counter()
        timeline.split_connect = timeline.t_tls is not None
        return ws
    except (OSError, socket.gaierror):
        # Staging failed at the socket layer — let the plain path raise the
        # error the caller expects, rather than a mangled one from here.
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        timeline.t_dns = timeline.t_tcp = timeline.t_tls = None
        timeline.t_start = time.perf_counter()
        return await _plain()
