# PRISM STT

Dedicated speech-to-text service: **faster-whisper behind an OpenAI-compatible HTTP
endpoint**. VocX (and any future consumer) sends audio here instead of running Whisper
in-process.

## Why it exists

| in-process (old) | this service |
| --- | --- |
| one model copy **per VocX worker** (~700 MB each) | **one** shared model instance |
| a long decode competes with the capture API | resource-isolated, capped independently |
| model downloads at first request (needs huggingface.co at runtime) | model **baked at build**; runtime is `HF_HUB_OFFLINE=1` |
| scales with VocX | scales alone (replicas / GPU pool later) |

## API

```
POST /v1/audio/transcriptions       multipart: file (required), model (ignored,
                                    OpenAI-compat), language (optional)
                                    → {text, language, duration, segments, backend}
GET  /healthz | /readyz             readyz reports model_loaded
```

Auth: `Authorization: Bearer <key>` (or `X-API-Key`) checked against `STT_API_KEYS`
(comma-separated; empty = open, dev only). Internal-only service: no gateway route, no
host port in compose.

## Configuration (env, prefix `STT_`)

| var | default | notes |
| --- | --- | --- |
| `STT_API_KEYS` | `` | front-door keys; VocX sends `VOCX_STT_API_KEY` |
| `STT_MODEL_SIZE` | `small` | must match the baked model (`--build-arg STT_MODEL_SIZE`) |
| `STT_DEVICE` / `STT_COMPUTE_TYPE` | `cpu` / `int8` | |
| `STT_BEAM_SIZE` / `STT_VAD_FILTER` | `5` / `true` | |
| `STT_MODEL_DIR` | `/opt/models` | baked into the image |
| `STT_PRELOAD` | `true` | load at startup; readiness reflects it |
| `STT_MAX_AUDIO_BYTES` | 25 MB | matches VocX's inbound cap |
| `STT_STUB_TEXT` | `` | tests/CI: fixed answer, no model |

## Changing the model

```bash
docker build -f services/stt/Dockerfile --build-arg STT_MODEL_SIZE=medium -t prism-stt:0.1.0 .
```

`small` is the fast CPU default; `medium`/`large-v3` trade speed for accuracy. The
build arg flows into the runtime `STT_MODEL_SIZE` env automatically so they can't drift.

## VocX side

Pure configuration — `services/vocx` already has an `api` STT backend:

```
VOCX_STT_BACKEND=api
VOCX_STT_API_URL=http://stt:8000/v1/audio/transcriptions
VOCX_STT_API_KEY=<one of STT_API_KEYS>
```

The in-process `faster_whisper` backend remains available as a fallback (install the
vocx `[stt]` extra and set `VOCX_STT_BACKEND=faster_whisper`).

## Tests

`python -m pytest` — runs against the stub engine (no model download): multipart
round-trip, bearer auth, size caps, health/readiness.
