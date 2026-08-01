# LiveKit + SIP (phone calls)

Put a Svara voice agent on a real phone number. This is the LiveKit-native path:
a telephony provider carries the PSTN call and hands it to LiveKit over SIP;
LiveKit runs your agent; Svara is the TTS.

```
 📞 caller ⇄ Telephony provider (PSTN + SIP trunk) ⇄ LiveKit (SIP + media) ⇄ agent worker
                                                                              │
                                             VAD → STT → LLM → svara.livekit.TTS
```

Works with any SIP provider — **Vobiz, Twilio, Plivo, Telnyx, Exotel**, or your
own PBX. The concrete example below uses Vobiz; the shapes are identical
elsewhere (only trunk-creation UI and field names differ).

## Prerequisites

- A **LiveKit Cloud** project with SIP enabled (or self-hosted LiveKit + the SIP
  service). Note your project's **inbound SIP URI**:
  `sip:<subdomain>.sip.livekit.cloud`.
- A telephony account with a **phone number (DID)** and a **SIP trunk** (username
  / password / SIP domain).
- `pip install "svara-voice[livekit]" livekit-plugins-openai livekit-plugins-silero livekit-api`

## 1. The agent

Svara is one line in the session:

```python
from svara.livekit import TTS as SvaraTTS

session = AgentSession(
    vad=ctx.proc.userdata["vad"],
    stt=...,                                   # a streaming STT keeps latency low
    llm=...,
    tts=SvaraTTS(voice="sv_enhdbrj5", mode="eager"),   # ← Svara, eager streaming
)
```

Register the worker under a name your dispatch rule will target
(`@server.rtc_session(agent_name="svara-agent")`). See `examples/livekit_agent.py`
and the full `kenpath-labs/svara-vobiz-agent` reference.

## 2. LiveKit SIP setup (once)

Using `livekit-api` (or the `lk` CLI):

```python
from livekit import api
from livekit.protocol import sip as sippb
from livekit.protocol import room as roompb
from livekit.protocol import agent_dispatch as adpb

lk = api.LiveKitAPI()  # reads LIVEKIT_URL / _API_KEY / _API_SECRET

# Outbound trunk — for the agent to dial out through the provider.
await lk.sip.create_outbound_trunk(sippb.CreateSIPOutboundTrunkRequest(
    trunk=sippb.SIPOutboundTrunkInfo(
        name="provider-outbound",
        address="<your-unique>.sip.<provider>",   # provider SIP domain
        transport=sippb.SIPTransport.SIP_TRANSPORT_UDP,
        numbers=["+91..."],                         # caller ID (your DID)
        auth_username="...", auth_password="...",
    )))

# Inbound trunk — accepts calls the provider forwards to your LiveKit SIP URI.
inb = await lk.sip.create_inbound_trunk(sippb.CreateSIPInboundTrunkRequest(
    trunk=sippb.SIPInboundTrunkInfo(name="provider-inbound", numbers=["+91..."])))

# Dispatch rule — every inbound call → its own room with the agent auto-dispatched.
await lk.sip.create_dispatch_rule(sippb.CreateSIPDispatchRuleRequest(
    name="provider-inbound-dispatch",
    trunk_ids=[inb.sip_trunk_id],
    rule=sippb.SIPDispatchRule(dispatch_rule_individual=sippb.SIPDispatchRuleIndividual(room_prefix="call-")),
    room_config=roompb.RoomConfiguration(agents=[adpb.RoomAgentDispatch(agent_name="svara-agent")]),
))
```

## 3a. Outbound — the agent calls a number

```python
room = "call-out-abc123"
await lk.agent_dispatch.create_dispatch(
    adpb.CreateAgentDispatchRequest(agent_name="svara-agent", room=room))
await lk.sip.create_sip_participant(sippb.CreateSIPParticipantRequest(
    sip_trunk_id="<outbound-trunk-id>",
    sip_call_to="+91XXXXXXXXXX",
    room_name=room,
    wait_until_answered=True,
))
```

The callee's phone rings; when they answer, Svara greets them.

## 3b. Inbound — someone calls your number

On the **provider** side, route the DID's inbound calls to your LiveKit SIP URI
(`<subdomain>.sip.livekit.cloud`, no `sip:` prefix). Then any call to the number
lands in a `call-*` room with the agent dispatched. Nothing else to run.

## Concrete example — Vobiz

Real values from the `svara-vobiz-agent` reference deployment:

| Piece | Value |
|---|---|
| Provider outbound trunk (Vobiz console) | domain `5580604c.sip.vobiz.ai`, user `livekitsvara` |
| DID | `+91 11 7136 6938` |
| LiveKit inbound SIP URI | `sip:testing-2q5h2bl1.sip.livekit.cloud` |

- **Outbound:** create the Vobiz *Outbound SIP Trunk* (gives you SIP
  domain/user/pass), plug those into the LiveKit outbound trunk above. Verified
  end-to-end — the agent dials an Indian mobile and Svara converses in Hindi.
- **Inbound:** in Vobiz, route the DID to `testing-2q5h2bl1.sip.livekit.cloud`
  (Inbound Trunk / origination), and link the number to the trunk.

Twilio/Plivo/Telnyx: same three LiveKit objects; you create an "elastic SIP
trunk" / "origination URI" on their side instead.

## Hosting the agent

- **Local / Docker / Cloud** — see [local](local.md), [docker](docker.md),
  [cloud](cloud.md). The worker dials out to LiveKit, so NAT is fine.
- **LiveKit Cloud agents** — let LiveKit host the worker: push your agent with
  the LiveKit CLI (`lk agent create` / deploy) so there's no box to babysit. The
  code is identical; you just don't run `agent.py` yourself.

## Latency tips

Use `mode="eager"` (done above), a **streaming** STT, and a fast LLM. Svara's
first audio is ~0.35 s — the budget is STT + turn-taking + LLM + the PSTN hop.
See [streaming.md](../streaming.md#where-latency-actually-goes-voice-agent-measured).
