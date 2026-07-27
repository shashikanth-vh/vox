# PRISM workflows (Temporal)

The **Workflows ring** of PRISM, made concrete. Durable, multi-step business processes run
as **Temporal** workflows; their side-effecting steps (activities) write the Register
through the shared **evam-register-client**, so they inherit auth, idempotency, optimistic
concurrency, retry and correlation.

## Why Temporal here

Long-running, resumable processes — an underwriting flow, a syndication mandate chase, a
covenant/monitoring schedule, the field-touchpoint ingest — need durable state and
guaranteed completion. Temporal persists every step; a crash resumes exactly where it left
off. Combined with the Register's idempotency keys, **Temporal's automatic retries give an
exactly-once *effect*** on the source of truth (retries run to completion but never
duplicate).

## The workflows

| Workflow | Business id | What it does |
| --- | --- | --- |
| `VoxTouchpointWorkflow` | `vox-{capture_id}` | The genuine end-to-end VOX capture: resolve the company by **canonical name** → create the entity + lead when missing / link & update the active lead when present → log the **full-fidelity** interaction (transcript, audio ref, GPS, attendees, both RMs, follow-up dates; Temporal workflow id stored in `source_ref`) → record the calendar hand-off. A retried upload with the same capture id replays the same workflow and the same writes — exactly-once end to end. |
| `LeadConversionWorkflow` | `leadconv-{lead_id}` | **Human-in-the-loop**: waits durably (days if needed) for an `approve`/`reject` SIGNAL, answers a `status` QUERY for dashboards, auto-times-out; approval applies the conversion — deal + requested product lines + lead marked Converted. |
| `IngestInteractionWorkflow` | caller-chosen | The original minimal reference, kept for teaching. |

## The Orchestrator API — how workflows start operationally

`python -m app.api` (same image as the worker, second deployment/container — compose
runs it as **orchestrator :8006**; the Helm chart as **prism-workflows-api**). It is the
workflow plane's HTTP front door: **stable business workflow ids, idempotent starts**
(starting an id that already ran attaches to it), signals, status.

```bash
# Start (or attach to) a VOX capture; ?wait=true blocks for the result:
curl -X POST "http://localhost:8006/v1/workflows/vox-touchpoints?wait=true" \
  -H "Content-Type: application/json" -d '{
    "capture_id": "cap-2026-07-25-001",
    "company_name": "Verdant Hydro Pvt Ltd",
    "transcript": "Plant running at 92% availability...",
    "audio_ref": "s3://vox-audio/cap-1.ogg",
    "performed_by": "Chetan", "assigned_rm": "Shubh",
    "next_action": "Collect FY26 financials", "next_meeting_date": "2026-08-04"}'

# Request a lead→deal conversion, then decide it:
curl -X POST http://localhost:8006/v1/workflows/lead-conversions \
  -H "Content-Type: application/json" \
  -d '{"lead_id": "<uuid>", "requested_by": "chetan@evamfinance.com",
       "is_lending": true, "product_type": "Term Loan", "amount_cr": 25}'
curl -X POST http://localhost:8006/v1/workflows/leadconv-<uuid>/approve \
  -H "Content-Type: application/json" \
  -d '{"by": "credit.head@evamfinance.com", "note": "Proceed"}'
curl http://localhost:8006/v1/workflows/leadconv-<uuid>     # status / stage / result
```

VocX is wired in: a capture that carries `company_name` (instead of a resolved subject
id) is forwarded to the orchestrator automatically when `VOCX_ORCHESTRATOR_URL` is set —
the "new company" and "unresolved company" VOX scenarios run as durable workflows, while
resolved-subject captures keep the direct fast path.

## Run it (local)

```bash
# The single compose file brings up the whole platform — Register stack AND the workflow
# plane (Temporal + its datastore + UI + this worker) — in one command:
docker compose -f deploy/compose/docker-compose.yml up --build
# Temporal Web UI → http://localhost:8088
```

Start a workflow (from any Temporal client / the CLI):

```python
from temporalio.client import Client
from app.workflows import IngestInteractionWorkflow
from app.types import InteractionInput

client = await Client.connect("localhost:7233")
result = await client.execute_workflow(
    IngestInteractionWorkflow.run,
    InteractionInput(entity_id="<entity-uuid>", interaction_type="Site Visit", source="VOX"),
    id="visit-123", task_queue="prism-workflows",
)
```

## Develop / test

```bash
pip install -e packages/evam-backend-core -e packages/evam-register-client
pip install -e "services/workflows[dev]"
cd services/workflows && pytest      # activity tests use a mock Register; the workflow
                                     # test runs on Temporal's in-memory server (skips offline)
```

## Run it standalone

The worker needs two reachable upstreams: a Temporal server and a Register.

```bash
# Build & run (build context = repo root):
docker build -f services/workflows/Dockerfile -t prism-workflows:0.1.0 .
docker run \
  -e WORKFLOWS_TEMPORAL_ADDRESS=host.docker.internal:7233 \
  -e WORKFLOWS_REGISTER_BASE_URL=http://host.docker.internal:8000 \
  -e WORKFLOWS_REGISTER_API_KEY=my-key -e WORKFLOWS_REGISTER_TENANT=EVAM \
  prism-workflows:0.1.0

# Kubernetes, standalone (vendored chart):
helm upgrade --install workflows deploy/helm/prism/charts/workflows \
  --set temporal.address=<temporal-host>:7233 \
  --set register.baseUrl=http://prism-register --set register.apiKey=<key>
```

A managed Temporal (Temporal Cloud) works the same way — point
`WORKFLOWS_TEMPORAL_ADDRESS` at it and skip the bundled server entirely.

## Configuration (env, prefix `WORKFLOWS_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `WORKFLOWS_TEMPORAL_ADDRESS` | `temporal:7233` | The Temporal frontend |
| `WORKFLOWS_TASK_QUEUE` | `prism-workflows` | Queue this worker polls |
| `WORKFLOWS_REGISTER_BASE_URL` | `http://register:8000` | The Register the activities write |
| `WORKFLOWS_REGISTER_API_KEY` | `dev-local-key` | Must be in `REGISTER_API_KEYS` |
| `WORKFLOWS_REGISTER_TENANT` | `EVAM` | Tenant the workflows act on |
| `WORKFLOWS_LOG_LEVEL` / `WORKFLOWS_LOG_JSON` | `INFO` / `true` | Structured logging |

See [`BACKEND_STANDARDS.md`](../../BACKEND_STANDARDS.md) for the shared conventions.
