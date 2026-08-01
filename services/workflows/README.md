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

## Production foundation (Release 1)

Every human-in-the-loop workflow shares one operational contract:

* **Run control** — `POST /v1/workflows/{id}/control` with `{"action": "cancel" | "return"
  | "resubmit", "by": ..., "note": ...}`. The action is persisted as an immutable control
  record in the Register **before** the run is signalled; the workflow verifies that record
  (fail-closed) before acting, exactly like decisions — a raw Temporal signal can neither
  cancel nor resume anything. `return` parks the run as *ReturnedForInformation*; `resubmit`
  restores it and **restarts its SLA clock**; `cancel` ends it as *Cancelled*.
* **Business vs technical status** — the `state` query (also embedded in
  `GET /v1/workflows/{id}`) answers `business_status` (AwaitingDecision /
  ReturnedForInformation / Cancelled / Sanctioned / …) separately from the technical
  `technical_stage` ("Verifying committee decision", …). Dashboards never infer one from
  the other.
* **SLA timers** — while a run waits on a human it emits `sla_reminder` every
  `sla_reminder_hours` (default 24) and a single `sla_escalation` after
  `sla_escalation_hours` (default 72); `0` disables either. Events always land in the
  structured log; set `WORKFLOWS_OPS_WEBHOOK_URL` to also POST them as JSON (Slack / Teams /
  any receiver) — best-effort with bounded retry, never load-bearing.
* **Payload encryption** — set `WORKFLOWS_PAYLOAD_ENCRYPTION_KEY` (base64url, 32 bytes:
  `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`)
  and every workflow input/argument/result is AES-256-GCM ciphertext at rest in Temporal.
  Set it on the worker AND the orchestrator. Rotation: see `app/codec.py`.
* **Metrics** — set `WORKFLOWS_METRICS_BIND_ADDRESS=0.0.0.0:9464` on the worker for a
  Prometheus scrape endpoint (task latencies, failures, slot usage).
* **Search attributes** — set `WORKFLOWS_SEARCH_ATTRIBUTES_ENABLED=true` to stamp each run
  with `PrismBusinessStatus` + `PrismSubject` for Temporal UI/CLI filtering. Register them
  first (once per cluster):

      temporal operator search-attribute create --name PrismBusinessStatus --type Keyword
      temporal operator search-attribute create --name PrismSubject --type Keyword

* **Long waits** — decision windows survive history limits: the run continues-as-new
  carrying its elapsed window (`resumed_elapsed_hours`), so a 14-day committee wait never
  hits Temporal's history cap.

## VOX + lead lifecycle (Release 1, increment 2)

* **Ambiguous-company confirmation** (`WORKFLOWS_VOX_CONFIRM_AMBIGUOUS_COMPANY=true`) —
  a capture whose company has CLOSE candidates but no exact canonical match parks instead
  of silently creating a near-duplicate. `GET /v1/workflows/{id}` (query
  `pending_confirmation`) lists the candidates; answer with
  `POST /v1/workflows/{id}/confirm-company` `{"entity_id": "<candidate>" | "", "by": ...}`
  ("" = genuinely new → create). The choice is WHITELISTED to the run's own candidates.
* **Multi-active-lead selection** — several active leads rank deterministically (owning RM
  > lens > sector > recency); with `WORKFLOWS_VOX_CONFIRM_LEAD_SELECTION=true` a genuine
  tie at the top parks the run for `POST /v1/workflows/{id}/select-lead`.
* **Configurable qualification checklist** — define once per deployment:
  `WORKFLOWS_QUALIFICATION_CHECKLIST='[{"key":"kyc","label":"KYC complete","required":true}, …]'`.
  A qualification request then answers every item
  (`"checklist": [{"key":"kyc","passed":true,"note":…}, …]`); the workflow COMPUTES the
  outcome (all required items must pass) and files the evaluation in the qualification
  evidence. Unknown or missing keys are refused at the door.
* **Repeat conversion** — after a rejection/withdrawal/timeout, a fresh conversion request
  for the same lead starts a NEW run under a `-r2`/`-r3` id (covered in the approval and
  persist test suites); each run gets its own single-winner decision record.

## Lending depth (Release 1, increment 3)

* **Committee rework, end to end** — `return` (run-control) parks the run, then
  `POST /v1/workflows/{id}/revise-credit-note` `{"reference", "sha256"?, "by"}` circulates
  the revision: each one is filed as the NEXT immutable `credit_note` evidence version on
  every lending line (the full circulation history stays on the record), the `state` query
  reports `credit_note_version`, and `resubmit` restores the decision window. The result
  records which version the committee decided on.
* **Conditional approval** — a committee submission (grouped or per facility) may carry
  `conditions` and `valid_days`. Conditions are recorded on the per-facility decision AND
  filed as governance evidence (`sanction_conditions`, verified against that same decision)
  beside the sanction letter.
* **Sanction validity window** — `valid_days` starts an abandoned
  `SanctionExpiryMonitorWorkflow` per sanctioned facility: an ops reminder before the
  deadline (default 7 days), and if the facility still sits at 'Sanctioned' when the window
  lapses, the monitor files `sanction_expired` evidence and raises the ops event. It only
  observes and records — what happens to a lapsed sanction is a committee/RM call.
* **Sanction versioning** — a fresh structuring attempt after rejection runs under a new
  `-r2` id with its own decision records; version = (run, credit-note version), all
  evidence-backed.

## Syndication lifecycle (Release 1, increment 5)

`POST /v1/workflows/syndications` starts a **SyndicationMandateWorkflow** for a
syndication_tracker mandate row (`syndication_id` + `deal_id`), inheriting the full
run-control + SLA foundation:

* **IM circulation is versioned evidence** — the start request's `im_reference` (or a later
  `POST /v1/workflows/{id}/circulate-im`) files `im_document` v1, v2, … on the mandate;
  the first circulation walks the mandate to 'IM Circulated'.
* **Lender-level activity** — `POST /v1/workflows/{id}/lender-update` moves ONE of the
  deal's lender rows (whitelisted to the rows the run discovered); every move goes through
  the Register's policy API, so an illegal transition is refused and surfaced as an ops
  event, never a crashed run.
* **The Syn Head's decision** — `POST /v1/workflows/{id}/syndication-decision` (approve /
  reject + sanction reference + conditions), persist-before-signal as a `kind="syndication"`
  decision (subject-bound, Syn Head/Management/Admin authority); the run verifies it
  fail-closed. Approval files the VERIFIED `syndication_sanction` evidence — the mandate
  reaches 'Sanctioned' only because the Register's evidence gate now passes.
* **Allocation** — `POST /v1/workflows/{id}/allocate` records the post-sanction lender
  split (validated: only the run's lender rows, sum ≤ the mandate amount), applied as
  amount updates plus a `syndication_allocation` evidence record. A bounded window
  (default 7 days) completes the run without one rather than blocking the sanction.

## Asset Monetisation lifecycle (Release 1, increment 6)

`POST /v1/workflows/asset-monetisations` starts an **AssetMonetisationWorkflow** for an
asset_monetisation mandate row, mirroring the syndication pattern:

* **Teaser circulation is versioned evidence** (`teaser_document` v1, v2, … via
  `/circulate-teaser`; the first walks the mandate to 'Teaser Shared').
* **Buyer-level tracking** — `/buyer-update` moves ONE of the deal's buyer rows
  (whitelisted; policy-enforced; illegal moves surfaced as ops events).
* **NDA / data-room control** — `/record-nda` files an immutable `am_nda` record per
  buyer (with the data-room grant flagged).
* **Offers** — `/record-offer` (nbo | binding) files immutable `am_offer` evidence; the
  `offer_comparison` query returns the full arrival-ordered set; any offer walks the
  mandate to 'NBO Received', a binding one to 'BO Received'.
* **Closure** — `/am-decision` records the AM Head's call as a `kind="asset_monetisation"`
  decision (subject-bound, AM Head/Management/Admin); approval files the VERIFIED
  `am_closure_approval` evidence and the mandate reaches 'Closed' only because the
  Register's evidence gate passes; rejection is a LOST mandate ('Dropped', reason on
  record).

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
