# Installation

Requires Python 3.9 or newer. The core package depends on `httpx` and
`websockets` only — no audio libraries, no compiled extensions.

```bash
pip install svara-voice
```

The distribution is `svara-voice`; the import is `import svara`. (The bare
`svara` name on PyPI belongs to an unrelated placeholder package.)

## Extras

| Install | Adds | For |
|---|---|---|
| `svara-voice` | `httpx`, `websockets` | the SDK: synth, stream, input streaming, voices, CLI |
| `svara-voice[livekit]` | `livekit-agents` | `svara.livekit.TTS`, the LiveKit Agents plugin |
| `svara-voice[pipecat]` | `pipecat-ai` | `svara.pipecat.SvaraTTSService`, the Pipecat TTS service |

```bash
pip install "svara-voice[livekit]"
```

## From source

```bash
pip install git+https://github.com/kenpath-labs/svara-python.git            # latest main
pip install "svara-voice @ git+https://github.com/kenpath-labs/svara-python.git@v0.2.0"
```

## Authentication

Create a key in the [Kenpath Labs console](https://platform.kenpathlabs.com)
and set it once in the environment:

```bash
export SVARA_API_KEY="sk_live_..."
```

or pass it explicitly: `Svara(api_key="sk_live_...")`. The SDK also honours
`SVARA_BASE_URL` (default `https://api.kenpathlabs.com`) for regional
gateways and self-hosted deployments.

## Checking the install

```bash
svara --version
svara voices --language hi
svara usage
```
