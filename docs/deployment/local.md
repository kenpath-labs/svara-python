# Local

The baseline: run your Svara code on a laptop or box.

```bash
python -m venv venv && source venv/bin/activate
pip install svara-voice
export SVARA_API_KEY="sk_live_..."
python your_script.py
```

That's all the core SDK needs — it's pure Python (`httpx` + `websockets`), no
system audio libraries required. You get bytes back; what you do with them
(write a file, pipe to a player, push to a socket) is up to you.

## Running a local voice-agent worker

For a LiveKit agent (see [LiveKit + SIP](livekit-sip.md)):

```bash
pip install "svara-voice[livekit]" \
            livekit-plugins-openai livekit-plugins-silero
python agent.py download-files   # one-time: VAD + turn-detector models
python agent.py dev              # registers the worker, waits for calls
```

The worker dials out to LiveKit Cloud, so a laptop behind NAT works fine for
**both inbound and outbound** calls — no inbound ports to open. This is exactly
how the `svara-vobiz-agent` reference demo runs during development.

Keep it alive across a session:

```bash
nohup python agent.py dev > agent.log 2>&1 &
```

**Limits of local:** it stops when your machine sleeps or the terminal closes,
and only you can reach it. For an always-on shareable demo, containerize it
([Docker](docker.md)) and put it on a [cloud host](cloud.md).
