# Troubleshooting

Start with the doctor. It checks each hop from your machine in order and
prints the timings a support ticket needs:

```bash
svara doctor            # or: SVARA_API_KEY=... svara doctor --voice sv_enhdbrj5
```

```
svara-voice 0.2.0 · Python 3.12.12 · httpx 0.28.1
base URL https://api.kenpathlabs.com

  DNS                    ok     api.kenpathlabs.com -> 203.0.113.10 in 5 ms
  TLS + HTTP             ok     GET /v1/models -> 200 in 180 ms (server: uvicorn, via: 1.1 Caddy)
  API key                ok     sk_live_…a1B2 (49 chars)
  Auth (/v1/usage)       ok     plan growth, 958770 characters left, 120 ms
  HTTP synthesis         ok     first audio 210 ms, 1.10 s of audio, 640 ms total
  WebSocket synthesis    ok     connect 290 ms, first audio 130 ms after text, 2.60 s of audio
```

The first failing row is the problem. It costs one ten-character synthesis.

## Symptom → cause

| Symptom | Cause | Fix |
|---|---|---|
| `MissingAPIKeyError` at construction | no key passed and `SVARA_API_KEY` unset | export the key, or `Svara(api_key=...)` |
| `AuthenticationError` … `invalid_api_key` | key revoked, or a key from a different region/gateway | create a new key in the console; check `SVARA_BASE_URL` |
| `NotFoundError` … `voice 'x' not found` | not a library voice id | use an `sv_…` id from `client.voices.list()`; display names are not ids |
| `InvalidRequestError` | raised before sending: empty/over-long `input`, `speed` outside 0.7–1.5, unsupported `sample_rate`/format | the message names the argument |
| `RateLimitError` … `too_many_concurrent_requests` | every concurrency slot in the workspace is busy | the SDK already retried with backoff; lower concurrency or ask for more slots |
| `QuotaExceededError` | monthly characters spent | terminal until the month resets or the plan changes; not retried |
| Audio plays at 3× speed on a phone | `ulaw`/`alaw` requested without `sample_rate=8000` (the server renders 24 kHz) | pass `sample_rate=8000`; the SDK warns when it is missing |
| A warning about `x-svara-dictionary: miss` | the `pronunciation_dictionary_id` does not exist; global rules applied | copy the UUID from the console |
| `StreamInterruptedError` with close code 1013 | the server had no engine ready | transient; retry the utterance |
| `StreamInterruptedError` with close code 1006 | the connection vanished mid-utterance | network; the message says how many frames arrived |
| `APITimeoutError` on a long `create()` | read timeout | default is 120 s; pass `timeout=` for longer text, or use `stream()` |
| First audio is 350 ms instead of 200 ms | a new connection per request | reuse one client (it keeps connections 120 s); call `warm_up()` at start-up |
| HTTP works, WebSocket fails to connect | a proxy that blocks `Upgrade` | eager streaming and the LiveKit plugin's default mode need WebSockets; `mode="http"` is the fallback |
| `CERTIFICATE_VERIFY_FAILED` on the WebSocket only | the OS trust store lacks a root that `certifi` has | `pip install certifi`; the SDK uses it for the socket when present |

## Reading the numbers

- `stream.time_to_first_audio` — seconds from sending the request to the first
  audio byte, at this client. Compare against ~0.2 s warm from India; add your
  round-trip time to the gateway from elsewhere.
- `stream.rate_limit` / `response.rate_limit` — remaining requests, streams and
  characters after this call. `-1` means unlimited on your plan.
- `error.code` — the server's machine-readable status; `error.body` the raw
  response for a ticket.

## Logging

The SDK logs nothing on its own. Turn on httpx's logger to see every request
and response line:

```python
import logging
logging.getLogger("httpx").setLevel(logging.DEBUG)
```

The LiveKit plugin logs under `svara.livekit` (a failed prewarm is logged at
DEBUG and falls back to connecting inline).

## Still stuck

Send hello@kenpathlabs.com the `svara doctor` output, the SDK version, the
voice id, and — for streaming problems — the `StreamInterruptedError` message
with its close code.
