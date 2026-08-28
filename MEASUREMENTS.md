# Measured behaviour of the live Svara API

Everything here was measured against production on 2026-08-27, from a laptop in
India, with `a production API key`. Numbers are medians unless stated. These
are the facts the SDK's defaults should be chosen against — several current
defaults were chosen against guesses instead.

Re-run with `examples/latency_probe.py`.

## Regions

| host | `/v1/voices` | `/v1/audio/speech` | notes |
|---|---|---|---|
| `api.kenpathlabs.com` | 200 | 200 | global entry — Mumbai, Akamai/Linode `203.0.113.10` |
| `api.in.idr.kenpathlabs.com` | 200 | 200 | Indore, NeevCloud `203.0.113.11` |

`api.in.kenpathlabs.com` was measured on 08-27 and 401'd on `/v1/audio/speech`
for every key in `secrets/` — it was the GCP Mumbai residency gateway
(`gw-warden`, `203.0.113.12`) with its own key database. It was **decommissioned
on 08-27** (`an internal teardown record`) and now returns NXDOMAIN, along
with `admin.in`, `api.res.in`, `dokploy.in`, `in.platform` and `prom.in`. Do not
use it as a test target.

`/v1/voices` answers **200 with no API key at all**, on every host tested, and
returns the full 320-voice / 282 KB catalogue including `quality_warning`,
`hours` and `is_default`. That is a server-side decision, not an SDK one, but
the SDK should not assume the endpoint is authenticated.

## HTTP streaming — the server front-loads a small first frame

The server flushes a deliberately small first frame so audio can start early,
then switches to large ones:

| format | 1st frame | later frames | 1st frame = audio |
|---|---|---|---|
| `pcm` (24 kHz s16le) | **1920 B** | 3552 / 5930 / 15110 / 16384 B | 40 ms |
| `ulaw` (G.711, 24 kHz by default — see below) | **960 B** | 3552 / 5510 B | 40 ms |
| `mp3` | ~2460 B | — | — |

`speech.stream()` defaulted to `chunk_size=4096`, which is larger than that
first frame in every format. httpx's `iter_bytes(4096)` therefore holds the
first frame back and waits for the second — discarding the head start the
server went out of its way to provide.

Cost, measured by replaying httpx's chunker over per-frame arrival timestamps
from a **single** request (so network jitter cancels rather than dominates):

| region | format | unbuffered | `chunk_size=4096` | added |
|---|---|---|---|---|
| `api` | pcm | 111.4 ms | 157.6 ms | **+46.2 ms** |
| `api` | ulaw | 138.8 ms | 204.2 ms | **+65.4 ms** |
| `api` | mp3 | 114.2 ms | 167.3 ms | **+53.1 ms** |
| `api.in.idr` | pcm | 113.9 ms | 245.4 ms | **+131.5 ms** |
| `api.in.idr` | ulaw | 118.3 ms | 163.5 ms | **+45.2 ms** |
| `api.in.idr` | mp3 | 121.1 ms | 170.9 ms | **+49.8 ms** |

Sweeping `chunk_size` on one `pcm` request shows it is a cliff, not a gradient
— anything above the first frame pays the full inter-frame gap:

```
chunk_size   ttfa      vs unbuffered
       512   133.6ms         +0.0ms
      1024   133.6ms         +0.0ms
      2048   241.4ms       +107.7ms     <- crosses the 1920 B first frame
      4096   241.4ms       +107.7ms     <- the old default
     16384   241.4ms       +107.8ms
```

**Therefore: `chunk_size` now defaults to `None` (unbuffered).** Callers who
need fixed frames — telephony wants 20 ms — still pass a number and get exact
frames.

## WebSocket (`stream-input`)

Handshake, 5 connects per cell:

| region | shared SSL context | fresh context per connect | delta |
|---|---|---|---|
| `api` | **123.9 ms** | 141.1 ms | 17.2 ms |
| `api.in.idr` | **143.1 ms** | 152.3 ms | 9.2 ms |

Two separate facts live in that table:

1. The handshake costs **124–143 ms warm**. That is what opening the socket
   before the text exists saves. It is real and worth taking, but it is not the
   300–600 ms it has been described as.
2. Reusing one `ssl.SSLContext` saves **9–17 ms** per connect. `websockets`
   passes `ssl=True`, and asyncio's `SSLProtocol` then calls
   `ssl.create_default_context()` per connection — synchronously, on the event
   loop, so concurrent connects serialise behind it. Building the context costs
   2.5 ms warm / 5–7 ms cold in isolation.

   It does **not** enable TLS session resumption, contrary to a common
   assumption. Six connects to `api.kenpathlabs.com`, three with a shared
   context and three with fresh ones, all reported `session_reused=False`.
   OpenSSL's client-side session cache is off by default and asyncio never
   passes `session=`. Sharing the context is worth doing for the 9–17 ms; it
   buys nothing beyond that.

### Eager trigger

Feed exactly *k* words, never send EOS, and see whether audio arrives. This has
no round-trip confound, unlike reading the threshold off a `chunk` event.

| `chunk_words` | audio first arrives at | `2 × chunk_words` |
|---|---|---|
| 4 | k = 8 | 8 |
| 8 | k = 16 | 16 |

`peek_words` does not affect it. Values below 4 clamp up to 4 server-side.

### Early close

The stream ending without a `done` frame was reported at "roughly 2 in 10
utterances". It did not reproduce:

| condition | clean |
|---|---|
| sequential, `api` | 10 / 10 |
| sequential, `api.in.idr` | 10 / 10 |
| concurrency 1 / 4 / 8, `api` | 2/2, 8/8, 16/16 |

**46 / 46 utterances completed with `done`.**

So the SDK *detects* an early close and raises `StreamInterruptedError` —
silent truncation is the genuinely bad outcome, and that fix stands on its own
regardless of how often it fires. It deliberately does **not** retry: replaying
an utterance the caller may already be playing is a real behaviour change, and
0/46 is not evidence for making one. If the 2-in-10 rate is real under
conditions not reproduced here, capture a `StreamInterruptedError` with its
`close_code` first, then decide.

## G.711 does **not** default to 8 kHz

The single most consequential finding, because it reaches the phone leg. Same
sentence, non-streaming, `api.kenpathlabs.com`:

| request | bytes | samples | @8 kHz | @24 kHz |
|---|---|---|---|---|
| `pcm`, no `sample_rate` | 177,600 | 88,800 | 11.10 s | **3.70 s** |
| `ulaw`, no `sample_rate` | 87,840 | 87,840 | 10.98 s | **3.66 s** |
| `ulaw`, `sample_rate=8000` | 30,720 | 30,720 | **3.84 s** | 1.28 s |
| `ulaw`, `sample_rate=24000` | 99,360 | 99,360 | 12.42 s | **4.14 s** |
| `alaw`, no `sample_rate` | 105,120 | 105,120 | 13.14 s | **4.38 s** |

Default `ulaw` produces the same duration as default `pcm` — 3.66 s vs 3.70 s —
which is only possible if both are 24 kHz. The server honours `sample_rate` for
G.711 (8000 and 24000 differ by ~3×); it simply does not default to 8000.

`FORMAT_INFO` claimed `default_rate: 8000` for `ulaw`/`alaw`, and
`examples/telephony_ulaw.py` wrote those bytes straight to a file for a SIP
media stream, stating "no resampling needed for phone audio". Following that
example put 24 kHz µ-law on an 8 kHz leg: **audio plays at 3× speed**, and it
sounds like a broken voice rather than a wrong clock.

Fixed: `FORMAT_INFO` now records the measured default, carries
`telephony_rate: 8000`, the example passes `sample_rate=8000`, and requesting a
G.711 format without a rate emits a warning.

## Streamed WAV carries an unknown-length header

Streamed `wav` arrives with `RIFF_size = 0xFFFFFFFF` and `data_size =
0xFFFFFFFF` — the standard streaming placeholder, since the length is not known
when the header goes out. Non-streamed `create()` returns an exact length.
Verified identical before and after this change set, so it is the server's
contract rather than anything the SDK does. Players that insist on a real
length will reject a streamed-to-disk `.wav`; use `create()` when writing files.

## Result of the fixes

Time to first audio, paired A/B against the pre-change SDK, 9 reps per cell,
calls interleaved so drift hits both arms equally:

| region | format | before | after | delta | paired wins |
|---|---|---|---|---|---|
| `api` | pcm | 298.1 ms | 210.2 ms | **−87.9 ms** | 7/9 |
| `api` | ulaw@8k | 281.5 ms | 240.7 ms | **−40.7 ms** | 8/9 |
| `api` | mp3 | 305.5 ms | 199.6 ms | **−105.9 ms** | 9/9 |
| `api.in.idr` | pcm | 293.5 ms | 213.6 ms | **−79.9 ms** | 9/9 |
| `api.in.idr` | ulaw@8k | 319.9 ms | 268.9 ms | **−51.1 ms** | 7/9 |
| `api.in.idr` | mp3 | 274.4 ms | 220.7 ms | **−53.7 ms** | 8/9 |

Every cell improved. On top of that, `prepare()` moves a further 151–163 ms off
the critical path for eager streaming, verified end to end.

### Not regressions

Two things look alarming in a naive A/B and are not:

- **Byte counts differ run to run.** The *unchanged* SDK, same request repeated
  six times, varies by 14–138% (one `pcm` run returned 10.00 s of audio for a
  sentence that usually takes 3.9 s). Generation is stochastic at the certified
  T=1.2. Any A/B on audio length is measuring the sampler, not the client.
- **Streamed WAV length mismatch** — see above, identical before and after.
