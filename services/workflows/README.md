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

## Reference workflow

`IngestInteractionWorkflow` (`app/workflows.py`): record a field interaction against an
entity, then read back its 360° dossier. It is the platform pattern — orchestration in the
workflow, all I/O in activities (`app/activities.py`) via the SDK, with a stable
workflow-derived idempotency key.

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
