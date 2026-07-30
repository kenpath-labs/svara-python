# Installation

Requires Python 3.9+.

## From git (current)

```bash
pip install git+https://github.com/kenpath-labs/svara-python.git
```

With the LiveKit integration:

```bash
pip install "svara[livekit] @ git+https://github.com/kenpath-labs/svara-python.git"
```

Over SSH (private repo):

```bash
pip install git+ssh://git@github.com/kenpath-labs/svara-python.git
```

Pin a commit/tag for reproducible builds:

```bash
pip install "git+https://github.com/kenpath-labs/svara-python.git@v0.1.0"
```

## Extras

| Extra | Adds | For |
|---|---|---|
| _(none)_ | `httpx`, `websockets` | the core SDK — synth, stream, voices |
| `svara[livekit]` | `livekit-agents` | the `svara.livekit.TTS` plugin |
| `svara[pipecat]` | `pipecat-ai` | Pipecat frame processor (reserved) |

## Authentication

Set your key once in the environment:

```bash
export SVARA_API_KEY="sk_live_..."
```

or pass it explicitly: `Svara(api_key="sk_live_...")`. Get a key from your
Kenpath Labs dashboard. The SDK also honors `SVARA_BASE_URL` (defaults to
`https://api.kenpathlabs.com`) for self-hosted / regional gateways.

## PyPI

Not yet published. When it is: `pip install svara`.
