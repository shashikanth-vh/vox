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


## The voice pipeline (`app/vocx`)

Production-grade port of the field PoC, backend-only. Through the edge:

    POST /vocx/v1/capture         transcript (or audio_b64) → PREVIEW: Claude-Haiku
                                       extraction (offline stub without ANTHROPIC_API_KEY),
                                       entity resolution against the LIVE Register, the
                                       per-critical-field confidence gate, the write plan.
                                       Mints _meta.capture_id. NEVER writes.
    POST /vocx/v1/capture_audio  raw audio (?rm=) → the recording is archived to
                                       MinIO (s3://prism-vocx-captures/captures/YYYY/MM/…;
                                       volume fallback — a failed PUT never discards) →
                                       STT (faster-whisper, baked into the image; model
                                       downloads once to the volume) → the same preview.
                                       The committed interaction carries the Recording:
                                       reference in its notes. 25 MB / 40k-char caps.
                                       Retention: VOCX_AUDIO_RETENTION_DAYS becomes a
                                       bucket lifecycle rule (0 = keep forever).
    POST /vocx/v1/commit         approved capture → REAL writes, IDEMPOTENT by
                                       capture_id (Idempotency-Key vocx:<id>:<op> — a retry
                                       replays, never duplicates): interaction / new lead in
                                       the Register as svc_vox; a follow-up event on the
                                       speaking RM's own Google Calendar when connected.
    GET  /vocx/v1/capabilities · interactions · facets · entity?code= · calendar/test
    GET  /vocx/v1/auth/start?rm=X&go=1   per-RM Google connect (browser). PKCE state
                                       and tokens persist on the vocx volume, so restarts
                                       and multiple replicas don't break the round-trip.

Reliability posture: Register reads retry with backoff; keyed writes retry safely; 4xx
refusals never retry; one failed op never sinks the rest of a commit; the resolution
corpus (cheap) and the search log (heavier) are cached separately so capture latency
stays flat as the book grows. Everything degrades honestly — no key → stub extraction,
no Google → register-only commits with calendar ops `skipped` — and `capabilities`
reports the current truth.

Configuration (compose defaults in deploy/compose/docker-compose.yml, Helm in
charts/vocx/values.yaml → `pipeline:`): `ANTHROPIC_API_KEY`, `VOCX_TOKENS_DIR` (volume),
`VOCX_GOOGLE_CLIENT_SECRET_FILE` (mounted from deploy/vocx-secrets/, git-ignored),
`VOCX_OAUTH_REDIRECT_URI` (the edge URL — add it in Google Cloud Console),
`VOCX_STT_BACKEND` (faster_whisper | api | stub; model size/cache in app/vocx/config.json).

Prod posture: the gateway exempts exactly `/vocx/v1/auth/callback` from
require_auth (Google's redirect carries no bearer). Completing the exchange still needs
the PKCE verifier persisted by an authenticated /auth/start, so the exemption cannot be
used to plant a token in someone else's slot.


### Package layout (`app/vocx`)

    mount.py       FastAPI adapter — /api/vocx/* (the only PRISM entry point)
    loader.py      packaged config.json + env overrides (secrets never in-repo)
    core/          pipeline engine: pipeline · extract · resolve · gate · server ·
                   atlas/store (corpus model) · search        (PoC lineage, relaxed lint)
    speech/        stt.py (faster-whisper/API/stub) · audio_store.py (MinIO→volume)
    registry/      Register adapters: store.py (live corpus) · writer.py (idempotent writes)
    google/        oauth.py (PKCE on volume) · workspace.py · notes.py · drive_writer.py

Rule of thumb: `core/`+`google/`+`speech/stt.py` are the vendored engine (edit sparingly,
lint relaxed); everything else is PRISM-owned and fully linted/typed.


### Server-side reports, playback, Log-To (v1)

    GET  /vocx/v1/reports?rm=          the RM's report list — every preview auto-saves a
                                       DRAFT (a dead phone loses nothing); save → ready;
                                       commit → committed (then read-only, 409 on save)
    GET  /vocx/v1/reports/get?rm=&id=  one report document
    POST /vocx/v1/reports/save|delete  keep edits / remove
    GET  /vocx/v1/audio?ref=           playback: MinIO refs → {url: presigned} signed
                                       against VOCX_S3_PUBLIC_ENDPOINT_URL; volume refs
                                       stream bytes; anything outside the captures
                                       bucket/prefix is refused
    commit body log_to                 {"subject_type": Lending|Syndication|
                                       AssetMonetisation|Deal|Lead|Entity,
                                       "subject_id": "<uuid>"} — the interaction lands on
                                       the explicitly chosen line instead of auto-routing

Report documents live in MinIO under reports/<rm>/<capture_id>.json (same bucket as the
audio), volume fallback, last-write-wins (single-writer by nature).
