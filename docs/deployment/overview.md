# Deployment overview

Svara is just an API — "deploying Svara" means deploying **whatever calls it**.
This page maps the common shapes and points you at the right guide.

## What are you building?

| You're building… | Runtime | Guide |
|---|---|---|
| A script / batch job / backend that generates audio | anything that runs Python | [Local](local.md) → [Docker](docker.md) |
| An always-on service (API, worker) | a container on a host | [Docker](docker.md) → [Cloud](cloud.md) |
| A **voice agent on a real phone number** | LiveKit agent + a SIP/telephony provider | [LiveKit + SIP](livekit-sip.md) |
| A voice bot on your own media transport | your WebSocket + Svara `ulaw` | [WebSocket media](websocket-media.md) |
| A browser voice bot | LiveKit (WebRTC) agent | [LiveKit + SIP](livekit-sip.md) (skip the SIP part) |

## The voice-agent picture

A live conversational agent is a loop; Svara is the last hop (TTS):

```
 caller ⇄ transport ⇄ [ VAD → STT → LLM → Svara TTS ] ⇄ back to caller
          (phone/PSTN, WebRTC, or your own WebSocket)
```

Two ways to connect a **phone** to that loop:

1. **SIP trunking → LiveKit** ([livekit-sip.md](livekit-sip.md)) — the telephony
   provider (Vobiz, Twilio, Plivo, Telnyx…) hands the call to LiveKit over SIP;
   LiveKit runs your agent and Svara plugs in as the TTS. Most robust; least code.
2. **Raw WebSocket media** ([websocket-media.md](websocket-media.md)) — the
   provider streams μ-law 8 kHz frames to *your* WebSocket; you run STT/LLM and
   push Svara `ulaw` back. Lowest-level; tightest control; matches Svara's native
   telephony output with zero transcoding.

## Where the agent process runs

Independent of the above, the agent worker itself runs somewhere:

- **[Local](local.md)** — your laptop/box. Fine for dev and demos.
- **[Docker](docker.md)** — the portable unit; run it anywhere.
- **[Cloud host](cloud.md)** — Railway / RunPod / Fly / a VM, for an always-on
  demo that doesn't depend on your machine.
- **LiveKit Cloud agents** — LiveKit hosts the worker for you ([livekit-sip.md](livekit-sip.md#hosting-the-agent)).

## A complete working reference

`kenpath-labs/svara-vobiz-agent` is a full, tested example: a phone voice agent
over **Vobiz (SIP) → LiveKit → Svara**, with inbound and outbound scripts. The
guides here generalize what it does.
