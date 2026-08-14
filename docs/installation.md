# Installation

Requires Python 3.9+.

## From git (latest main)

The package is not on PyPI yet, so install from the repository:

```bash
pip install git+https://github.com/kenpath-labs/svara-python.git
```

With extras:

```bash
pip install "svara-voice[livekit] @ git+https://github.com/kenpath-labs/svara-python.git"
```

Pin a commit for reproducible builds:

```bash
pip install "svara-voice @ git+https://github.com/kenpath-labs/svara-python.git@<commit-sha>"
```

## From PyPI (once the first release is published)

```bash
pip install svara-voice
pip install "svara-voice[livekit]"   # with the LiveKit integration
```

The distribution is `svara-voice`; the import is `import svara`.

## Extras

| Extra | Adds | For |
|---|---|---|
| _(none)_ | `httpx`, `websockets` | the core SDK — synth, stream, voices |
| `svara-voice[livekit]` | `livekit-agents` | the `svara.livekit.TTS` plugin |
| `svara-voice[pipecat]` | `pipecat-ai` | Pipecat frame processor (reserved) |

## Authentication

Set your key once in the environment:

```bash
export SVARA_API_KEY="sk_live_..."
```

or pass it explicitly: `Svara(api_key="sk_live_...")`. Get a key from your
Kenpath Labs dashboard. The SDK also honors `SVARA_BASE_URL` (defaults to
`https://api.kenpathlabs.com`) for self-hosted / regional gateways.
