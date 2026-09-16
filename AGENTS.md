# Working in svara-python

Notes for anyone — person or coding agent — changing this repository. The
package is `svara-voice` on PyPI (`import svara`); it is the official client
for the Svara TTS API and the reference for how the API should be used.

## What this package is, and is not

- A thin client over `https://api.kenpathlabs.com`. It mirrors the real
  endpoints and the real field names (`lang`, not `language`, on the wire;
  the SDK argument is `language`). It does not add features the API lacks.
- Latency is the product. Every transport default is backed by a measurement
  in `MEASUREMENTS.md`. Do not change `DEFAULT_TIMEOUT`, `DEFAULT_LIMITS`,
  `chunk_size`, or `PreparedStream.IDLE_BUDGET_SECONDS` without a new
  measurement, and write the measurement down there.
- Audio processing (volume, resampling, format conversion) does not belong
  in the SDK. The server renders every format and rate; bytes pass through
  untouched. A benchmark harness is an `examples/` script, not a module.
- No dependencies beyond `httpx` and `websockets` in the core. Framework
  integrations live behind extras and import their framework lazily with a
  helpful `ImportError`.

## Conventions

- Python 3.9+. `from __future__ import annotations` everywhere; keep
  `typing.Optional`/`Dict` spellings (ruff has `UP` disabled on purpose).
- The sync and async clients are maintained by hand as twins. A change to
  one must land in the other, and `tests/test_fixes.py` pins several of them
  as sync-vs-async pairs. Add a pair when you add a method.
- Errors: every failure a caller can see is a `SvaraError` subclass. HTTP
  status → class mapping lives in `exceptions.raise_for_status`; error bodies
  are parsed by `parse_error_body` (three server shapes). Never let an
  `httpx` or `websockets` exception escape.
- Retries happen only before the first audio byte. Replaying audio the
  caller is already playing is worse than the truncation it would hide.
- Warn, don't rewrite: the SDK warns about `ulaw` without `sample_rate`
  rather than substituting 8000. Silent rewriting makes the client impossible
  to reason about.
- Docs are part of the change. `docs/api-reference.md`, `README.md`,
  `CHANGELOG.md` and `docs/llms.txt` must describe the code as it is.

## Testing

```bash
uv venv --python 3.12 .venv && uv pip install -e ".[dev,livekit,pipecat]"
ruff check src tests examples
pytest tests -q                                   # no network needed
SVARA_API_KEY=sk_live_... pytest tests/test_integration.py -q   # live
```

`tests/test_unit.py` and `tests/test_production.py` use `httpx.MockTransport`
and a local `websockets` server; nothing there touches the network. The
LiveKit and Pipecat tests skip themselves when the framework is absent. Run
on 3.9 too (`uv venv --python 3.9`) before a release: that is the floor.

Secrets for live runs live in `~/Github/svara/secrets/` (outside any git
repo); never paste one into code, docs, tests or commit messages.

## Releasing

1. Bump `src/svara/_version.py`, add the `CHANGELOG.md` entry, update the
   docs that mention the version.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`. The release workflow refuses a
   tag that disagrees with `_version.py`, runs lint and tests, builds, and
   uploads with the `PYPI_API_TOKEN` repository secret.
3. Check https://pypi.org/project/svara-voice/ and `pip install svara-voice==X.Y.Z`
   in a clean venv.

## Measuring

`examples/latency_probe.py` reproduces the first-frame / `chunk_size` tables.
For anything else, write a throwaway script, run it against production with
`a production API key`, and record the numbers with the date in
`MEASUREMENTS.md`. Interleave arms when comparing two things; the API's
audio length is stochastic, so compare time-to-first-audio, never bytes.
