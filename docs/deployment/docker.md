# Docker

Containerize whatever calls Svara — a batch job, an API service, or a voice-agent
worker. Below is a worker image (the most common case).

## Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps only if your STT/VAD plugins need them; the Svara SDK needs none.
RUN pip install --no-cache-dir \
    "svara-voice[livekit]" \
    livekit-plugins-openai livekit-plugins-silero

COPY agent.py .

# Pre-download VAD + turn-detector models into the image (avoids first-call stall).
RUN python agent.py download-files

CMD ["python", "agent.py", "start"]
```

> `start` (not `dev`) is the production worker mode.

## Build & run

```bash
docker build -t svara-agent .
docker run --rm \
  -e SVARA_API_KEY=sk_live_... \
  -e LIVEKIT_URL=wss://<project>.livekit.cloud \
  -e LIVEKIT_API_KEY=... -e LIVEKIT_API_SECRET=... \
  -e OPENAI_API_KEY=... \
  svara-agent
```

The worker connects **outbound** to LiveKit Cloud, so no inbound ports or
`-p` mapping are needed. Pass secrets as env vars (or Docker secrets); never bake
keys into the image.

## docker-compose

```yaml
services:
  agent:
    build: .
    restart: unless-stopped
    env_file: .env       # SVARA_API_KEY, LIVEKIT_*, OPENAI_API_KEY, ...
```

## Pure-SDK service (no LiveKit)

If you're only synthesizing (e.g. a TTS microservice), the image is tiny:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir svara-voice
COPY app.py .
CMD ["python", "app.py"]
```

This same image is what you push to any [cloud host](cloud.md).
