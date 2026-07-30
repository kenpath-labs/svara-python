# Cloud host (Railway / RunPod / Fly / VM)

Once you have a [Docker image](docker.md), an always-on deployment is "run that
container somewhere with the env vars set." A voice-agent worker connects
**outbound** to LiveKit Cloud, so it needs **no public ingress** — it's just a
long-running process. That makes it easy to host almost anywhere.

## Railway

Point Railway at the repo (it detects the `Dockerfile`) or deploy the image.
Set variables in the service:

```
SVARA_API_KEY, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, OPENAI_API_KEY, ...
```

Since the worker has no HTTP server, either mark it a **worker/no-port** service,
or run a trivial health endpoint if the platform insists on a port. Set the start
command to `python agent.py start`.

## RunPod

Best when the pipeline needs a GPU (e.g. self-hosted STT). Use a Pod (not
Serverless) for a persistent worker: base image → install the package → run
`python agent.py start`. Put keys in the pod's environment/secrets.

## Fly.io

```bash
fly launch --no-deploy          # generates fly.toml from the Dockerfile
fly secrets set SVARA_API_KEY=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... OPENAI_API_KEY=...
fly deploy
```

A worker needs no `[http_service]`; a plain `app` process works. Scale replicas
with `fly scale count N` to handle more concurrent calls.

## Plain VM (systemd)

```ini
# /etc/systemd/system/svara-agent.service
[Service]
EnvironmentFile=/etc/svara-agent.env
ExecStart=/opt/svara/venv/bin/python /opt/svara/agent.py start
Restart=always
[Install]
WantedBy=multi-user.target
```

`systemctl enable --now svara-agent`.

## Scaling & concurrency

- One worker process handles multiple concurrent sessions, but STT/LLM/TTS calls
  are per-session — size CPU/RAM to your peak concurrency and run multiple
  replicas behind LiveKit's job dispatch for more headroom.
- Svara enforces per-key concurrency/rate limits; a `RateLimitError` (429) is
  retryable — back off. Ask Kenpath Labs to raise limits for production volume.
- Put the worker in a **region near your users and near LiveKit's SIP region**
  to shave the media round-trip.

## Just synthesizing (no agent)

If you're deploying a TTS microservice or batch worker rather than a voice agent,
the same hosts apply and it's simpler — no LiveKit, no models to download, no
telephony. It's an ordinary stateless service that returns audio bytes.
