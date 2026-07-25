# PRISM VocX — voice-based field touchpoint capture

VocX turns a field visit into structured Register data. An RM records a voice note
after a client meeting; the capture app transcribes it (speech-to-text is upstream, on
device or edge) and posts the transcript + structured intel to VocX, which writes it to
the Register as an interaction with `source: "VocX"` through the platform SDK.

The one design idea that matters: **the capture id is the idempotency key**
(`vocx:{capture_id}`). Field uplinks are flaky and retry; with the key, a re-uploaded
capture *replays* the original write instead of duplicating the touchpoint —
exactly-once effect with no queue and no workflow.

Stateless: no database, no files — the Register is the only state. Scale by adding
replicas; kill and restart at will.

## API

| Endpoint | What it does |
| --- | --- |
| `POST /v1/touchpoints` | One captured touchpoint → an interaction in the Register. Returns the interaction id. |
| `GET /healthz` · `GET /readyz` | Liveness / readiness. |

Payload (all optional except subject): `subject_type` + `subject_id` (Lead / Deal /
Entity / Counterparty / Lending / Syndication / AssetMonetisation), `interaction_type`,
`transcript`, `summary`, `notes`, `performed_by`, `contact_name`, `gps_lat`/`gps_lng`,
`location`, `attendees`, `key_intel`, `next_steps`, `next_action(_date)`, `language`,
and `capture_id` (the stable id of the recording — send it and retries become safe).

```bash
curl -X POST http://localhost:8003/v1/touchpoints -H "Content-Type: application/json" -d '{
  "subject_type": "Deal", "subject_id": "<deal-uuid>",
  "interaction_type": "Site Visit / Due Diligence",
  "summary": "Voice note after site visit",
  "transcript": "All rooftops generating above P50 ...",
  "performed_by": "Chetan", "capture_id": "capture-2026-07-25-001"
}'
```

## Configuration (env, prefix `VOCX_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOCX_REGISTER_BASE_URL` | `http://register:8000` | The Register (or the gateway) |
| `VOCX_REGISTER_API_KEY` | `dev-local-key` | Must be in the Register's `REGISTER_API_KEYS` |
| `VOCX_REGISTER_TENANT` | `EVAM` | The tenant touchpoints are written to |
| `VOCX_LOG_LEVEL` / `VOCX_LOG_JSON` | `INFO` / `true` | Structured logging |

## Run it standalone

VocX needs exactly one thing: a reachable Register (any deployment of it).

```bash
# Build & run (build context = repo root):
docker build -f services/vocx/Dockerfile -t prism-vocx:0.1.0 .
docker run -p 8003:8000 \
  -e VOCX_REGISTER_BASE_URL=http://host.docker.internal:8000 \
  -e VOCX_REGISTER_API_KEY=my-key -e VOCX_REGISTER_TENANT=EVAM prism-vocx:0.1.0
```

Other paths to the same thing:

```bash
# Local dev (no Docker):
cd services/vocx && pip install -e ../../packages/evam-backend-core \
  -e ../../packages/evam-register-client -e ".[dev]"
uvicorn app.main:app --port 8003

# Compose subset (DB + Register + VocX):
docker compose -f deploy/compose/docker-compose.yml up --build postgres register vocx

# Kubernetes, standalone (vendored chart):
helm upgrade --install vocx deploy/helm/prism/charts/vocx \
  --set register.baseUrl=http://prism-register --set register.apiKey.value=<key>
```

## Tests

`pytest` here runs 3 end-to-end tests against a **real Register** (uvicorn subprocess,
real Postgres, real migrations): capture → interaction, replayed capture → same
interaction (no duplicate), and validation errors surfacing cleanly.

## Extending it (start here if you are new)

VocX is the smallest service in the platform (~200 lines including tests) and the
reference for the satellite pattern: config from env → app factory → SDK call with an
idempotency key → e2e test. To add a field to the capture contract, extend
`TouchpointIn` in `app/main.py` — anything the Register's interaction schema accepts
passes straight through.
