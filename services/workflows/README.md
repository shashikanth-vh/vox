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

Config is `WORKFLOWS_`-prefixed (`TEMPORAL_ADDRESS`, `TASK_QUEUE`, `REGISTER_BASE_URL`,
`REGISTER_API_KEY`, `REGISTER_TENANT`). See [`BACKEND_STANDARDS.md`](../../BACKEND_STANDARDS.md).
