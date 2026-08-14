# Timing

Measuring "how fast is the TTS" sounds like one number. It is not, and the
reason it isn't is the reason this module exists.

## Why one number doesn't work

A server-side latency figure covers generation only. What a caller actually
waits through is a chain of six things, owned by three different parties:

```
dns - tcp - tls - auth  |  feed ... trigger  |  trigger -> first audio byte
|________________|         |____________|      |_________________________|
      network                  your LLM         the server, then the model
```

Quote a single number and you have silently picked one of these and thrown away
the rest. That is how a team ends up optimising a model that was never the
problem.

Three things make this genuinely hard rather than merely tedious:

**1. Nothing on the wire announces itself.** The API key check is not a message
you can time — a `wss://` connection is born as an HTTP `GET` carrying the key,
answered with `101 Switching Protocols`. The check happens before any WebSocket
frame exists. Separating it from the TLS handshake means stamping inside
asyncio's protocol factory, which is why
[`staged_connect`](../src/svara/_timing.py) exists at all. When the connection
can't be staged (a proxy, `ws://`, an older `websockets`), `auth_ms` reports
`None` rather than a number that would be TLS and the upgrade added together
and labelled wrong.

**2. Most spans contain more than one party's time.** `auth_ms` is one network
round-trip *plus* the server's key check. At a 96 ms RTT, a 129 ms `auth_ms` is
about 33 ms of actual key checking — so comparing raw `auth_ms` against a
server-side target reads as a regression that isn't one. That is what
`auth_server_ms` is for. Likewise `feed_to_chunk_ms` is your LLM's token rate
*and* the server's batching window, and no client-side measurement can split
them without an estimate.

**3. The obvious number is the misleading one.** `ttfa_from_trigger_ms` — "how
long after the Nth word do we hear something" — is the span people ask for by
name, and it is *not* the model's time. It still carries uplink transit and the
server continuing to wait for a full lookahead chunk. At the default settings
it runs several times `generate_ms`. Read it as the model's speed and you will
file the wrong ticket.

## The one clean measurement

`generate_ms` — from the server announcing a chunk to the first audio byte —
is the honest one, because **transit cancels out of it**. Both messages travel
the same path in the same direction, so the gap between their arrival times
equals the gap between their send times. Network latency shifts both equally.

That gives an identity with no estimate in it:

```
feed_to_chunk_ms  +  generate_ms  =  ttfa_from_first_text_ms
   (shared)            (svara)              (what you feel)
```

This is the split the whole module is built to report, and the only
decomposition here that needs no assumption about where the server's threshold
sits.

## What the trigger actually is

In eager mode the server starts speaking once **`2 × chunk_words` whole words**
are buffered — it wants a full next chunk in hand before committing to the
current one. Note *words*, not messages: LLM deltas are sub-word fragments
(`"Hel"`, `"lo"`, `" there"`), so counting messages gives a number unrelated to
what the server waits for.

That formula is an expectation, not a contract, so nothing depends on it.
`words_at_first_chunk` records where the server *actually* began, and the report
prints it. A gap between expected and measured is news, and it is louder than
any percentile in the table.

## Using it

Set `SVARA_TIMING=1` for a one-line summary per utterance, with no code change:

```
svara connect=427.5ms ttfa=1527.5ms feed=1407.5ms model=120.0ms audio=8.0s rtf=2.3x
```

`SVARA_TIMING=json` emits the flat record instead. For aggregate work, collect
timelines and report:

```python
from svara import AsyncSvara, TimingStats

stats = TimingStats()
async for audio in client.speech.stream_input(tokens, voice="sv_x",
                                              on_timing=stats.add):
    ...

print(stats.report())    # grouped table, owners, and a diagnosis
print(stats.legend())    # what each span means
print(stats.timelines[-1].explain())   # one utterance, in prose
```

`report()` groups spans by when they happen, tags each with **who owns it**,
and ends with a diagnosis naming the dominant cost — because "how slow" is
rarely the question anyone actually arrived with. `legend()` explains only the
spans that printed. `explain()` narrates a single run.

Percentiles a sample size can't support are withheld rather than faked: with
nearest-rank on 10 samples, p90 and p99 both resolve to the maximum, and
printing them side by side reads as three measurements agreeing when it is one
number three times. p90 needs 10 samples; p99 needs 100.

## Reading the output

| If the report says | It means | Do |
|---|---|---|
| `DOMINATED BY THE WAIT` | Most of TTFA is buffering text, not synthesis | Speed up your LLM's first tokens. `chunk_words` won't help — 4 is the floor and raising it makes this worse |
| `DOMINATED BY GENERATION` | The model is the cost | Worth a ticket, with the table attached |
| `realtime_factor` p50 < 1.0 | Synthesis is slower than playback | Outranks every latency above it — the caller hears gaps regardless of TTFA |
| a large `max_frame_gap_ms` | An audible stall mid-utterance | Sounds worse than a slow start |
| `the trigger moved` | The server began speaking somewhere unexpected | Re-check every span above it; the premise changed |
| `auth_ms` high, `auth_server_ms` low | Distance, not the server | Move closer or reuse connections |

## Volume and timing

When `volume` is applied client-side (the default today — see
[Volume](api-reference.md#volume)), scaling costs CPU in *your* process. That
shows up as `gain_ms`, attributed to you rather than folded into the latency
spans around it, and `clipped_samples` / `clipping_ratio` flag a gain that is
too high for the material. Audio frames are measured as they arrive, before
scaling: gain doesn't change what the network delivered, so billing the
transport for it would be wrong.
