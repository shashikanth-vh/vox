# PRISM Register

The **Register** is PRISM's single source of truth — *every entity, deal, financial,
contract, touchpoint, signal and behavioural record*. Everything else in the platform
(CIPHER, VOX, PULSE, ATLAS) reads from and writes to it. It is deliberately the first
thing built and the most stable: get the Register wrong and the cost surfaces six to
nine months later. So this service is built for **stability, concurrency-safety and data
integrity above all**.

It is a production-grade REST API over PostgreSQL:

- **Entity-centric, tenant-aware, product-aware, versioned** — the four PRISM commitments.
- **Concurrency-safe**: optimistic locking (no lost updates), idempotent creates,
  bounded connection pool, hard statement/lock timeouts, advisory-locked financial
  versioning. Proven by a parallel-request test suite.
- **Full CRUD** for all 18 tables, plus versioned financials, nested syndication lenders,
  an entity 360° dossier, reference vocabularies and an audit trail.
- **Deployable** via Docker Compose and Helm.
- Handles **lakhs of rows**: keyset pagination, targeted indexes, trigram search.

> Stack: Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · asyncpg · Alembic · Pydantic v2 · PostgreSQL 16.

---

## Quickstart

### Option A — Docker Compose (nothing to install but Docker)

```bash
# from the repo root:
docker compose -f deploy/compose/docker-compose.yml up --build
# Postgres starts, the API migrates, provisions the default tenant + reference dropdowns
# (no business data), then serves on :8000. The API is usable immediately.
curl -H "X-API-Key: dev-local-key" http://localhost:8000/v1/entities?with_total=true
open http://localhost:8000/docs        # interactive API docs

# Load data on demand, only if you want it:
#   real data — upload your own spreadsheet (nothing real is shipped in the image):
curl -H "X-API-Key: dev-local-key" -F "file=@<your-mis>.xlsx" \
  "http://localhost:8000/v1/import/atlas-xlsx?mode=replace"
#   or the synthetic prototype mock (shipped, for smoke tests only):
docker compose -f deploy/compose/docker-compose.yml exec register python -m app.seed
```

### Option B — Local (Python + a Postgres you point at)

```bash
make install                      # pip install -e ".[dev]"
cp .env.example .env              # edit REGISTER_DB_* to your Postgres
make migrate                      # alembic upgrade head
make seed                         # load ref data + the ATLAS mock dataset
make run                          # uvicorn with autoreload on :8000
```

---

## Using the API

Auth is intentionally light (user management lives upstream in the platform's doors):

| Header | Meaning |
| --- | --- |
| `X-API-Key` | Required. One of `REGISTER_API_KEYS`. |
| `X-Tenant` | Tenant code (default `EVAM`). |
| `X-Actor` | Optional actor label recorded in the audit trail. |
| `Idempotency-Key` | Optional on `POST` — a retried create returns the original result. |
| `If-Match: "<version>"` | Optional on `PATCH`/`DELETE` — optimistic concurrency. |

**Tenancy / first run.** Every request is tenant-scoped; an unknown tenant is rejected
`403 "Unknown or inactive tenant"`. On a fresh database you must provision the tenant once —
the Compose/Helm defaults do this for you via `bootstrap` (tenant `EVAM` + reference
dropdowns, no business data). If you run a bare `migrate-serve`, provision it yourself:
`python -m app.seed.bootstrap` (or `--tenant-code X --tenant-name "…"` for another tenant).
Additional tenants are managed through the **Tenants API** below (or `bootstrap`, or a direct
row in `tenants`).

**Tenants are managed above tenancy.** Because a tenant is the boundary itself, the
`/v1/tenants` endpoints are gated by the `X-API-Key` **only** and do not take an `X-Tenant`
header (treat the key as an admin credential). `code` is the value other requests send in
`X-Tenant`; it is immutable, and a tenant is never hard-deleted (that would orphan its
business rows) — `DELETE` deactivates it (→ its requests start 403ing) and
`PATCH {"is_active": true}` brings it back. Every change is audited and takes effect at once.

Every resource exposes the same surface:

```
POST   /v1/<resource>            create        (201, returns the row + ETag)
GET    /v1/<resource>            list          (search q=, filters, keyset cursor)
GET    /v1/<resource>/{id}       read one
PATCH  /v1/<resource>/{id}       partial update (optimistic)
DELETE /v1/<resource>/{id}       soft delete    (optimistic)
POST   /v1/<resource>/{id}/restore
```

Resources: `entities`, `people`, `counterparties`, `leads`, `deals`, `lending`,
`syndication`, `syndication-lenders`, `asset-monetisation`, `financials`,
`contracts-assets`, `interactions`, `external-intelligence`, `monitoring`.

Custom endpoints:

```
POST   /v1/tenants                        create a tenant           (API-key only, no X-Tenant)
GET    /v1/tenants                        list tenants
GET    /v1/tenants/{code}                 read one (by code or id)
PATCH  /v1/tenants/{code}                 rename / (de)activate
DELETE /v1/tenants/{code}                 deactivate (soft — reactivate via PATCH)
POST /v1/interactions                     log an interaction (subject_type + subject_id)
GET/POST /v1/leads/{id}/interactions              timeline / log against a lead
GET/POST /v1/deals/{id}/interactions              timeline / log against a deal
GET/POST /v1/lending/{id}/interactions            timeline / log against a lending record
GET/POST /v1/syndication/{id}/interactions        timeline / log against a syndication
GET/POST /v1/asset-monetisation/{id}/interactions timeline / log against an asset-mon record
GET/POST /v1/counterparties/{id}/interactions     timeline / log against a counterparty
GET      /v1/entities/{id}/interactions           entity-level timeline (spans all its deals/trackers)
POST /v1/financials                       create a new financial VERSION (auto version_no)
GET  /v1/financials/history               full version history for a statement
GET  /v1/syndication/{id}/lenders         nested lenders on a syndication
POST /v1/syndication/{id}/lenders         add a lender
GET  /v1/entities/{id}/dossier            entity 360° — deals, financials, interactions, open intel
GET  /v1/entities/{id}/lender-matrix      lender engagement grid (derived from syndication lenders)
POST /v1/external-intelligence/{id}/acknowledge · /dismiss   shared RED/AMBER triage state
GET/PUT /v1/settings                      per-tenant config (alert thresholds; defaults merged in)
GET  /v1/ref  ·  /v1/ref/{category}       reference vocabularies (dropdowns)
GET  /v1/audit                            audit trail
POST /v1/import/atlas-xlsx                 load the ATLAS MIS spreadsheet (?mode=replace|merge)
GET  /v1/export/excel                     whole DB → .xlsx (one sheet per table) — backup / compare
GET  /v1/export/json                      whole DB → JSON (type-faithful backup)
GET  /v1/export/counts                    row counts per table (quick verification)
GET  /healthz  ·  /readyz                 liveness / readiness
```

**Load the real MIS** — the Register can be loaded from the authoritative ATLAS MIS
spreadsheet (the 6-sheet consolidated xlsx). **Real data is never committed to the repo or
baked into the image** — you supply the file at runtime:

```bash
# Recommended: upload it through the API (no need to get the file into the container):
curl -H "X-API-Key: dev-local-key" -F "file=@Evam_ATLAS_MIS_Consolidated_v4.xlsx" \
  "http://localhost:8000/v1/import/atlas-xlsx?mode=replace"      # or ?mode=merge

# Or copy it into the running container and use the CLI:
docker compose -f deploy/compose/docker-compose.yml cp \
  Evam_ATLAS_MIS_Consolidated_v4.xlsx register:/app/data/mis.xlsx
docker compose -f deploy/compose/docker-compose.yml exec register \
  python -m app.seed.xlsx_cli data/mis.xlsx                      # --no-truncate to merge
```

It maps Leads/Deals/Lending/Syndication(+lenders)/AssetMon to their tables, dedups every
company into `entities`, and folds Mandate Tracker onto `syndication_tracker.mandate_status`.
The Docker Compose stack starts **empty** (`migrate-serve`) — load on demand once it's up,
only if you want data. (For a mount-based auto-load on first boot, set the container command
to `migrate-import-serve` and point `REGISTER_MIS_XLSX` at a file you've mounted in; it loads
only when the DB is empty, and leaves the DB empty if the file is absent.)

**Export / backup** — `GET /v1/export/excel` streams a workbook with one sheet per table
(tenant-scoped; `?include_deleted=true` and `?tables=leads,deals` supported). Use it to
verify a load or as a point-in-time backup. `GET /v1/export/json` is the type-faithful
variant for re-import; `GET /v1/export/counts` is a fast row-count check.

**Interaction timeline** — log any touch (call/meeting/email/site visit/IC note) against
**any** record: a lead, deal, entity, counterparty, or a lending / syndication /
asset-monetisation tracker (polymorphic `subject_type` + `subject_id`, matching ATLAS's
`refType`/`refId`). Each subject exposes a chronological feed; the entity-level timeline
aggregates interactions across all of a company's deals and trackers. Three behaviours
match ATLAS: logging against a deal/tracker denormalises the entity; logging against a
lead rolls the latest onto the lead's `last_interaction_date`/`next_action`; and a
**syndication** interaction tied to a lender + direction updates that lender's response
date (inbound) or chased date (outbound). **VOX** writes here too — same endpoints with
`source:"VOX"` and the structured note (+ optional `transcript`).

### Example

```bash
BASE=http://localhost:8000; KEY="X-API-Key: dev-local-key"

# create
ID=$(curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST $BASE/v1/entities \
  -d '{"code":"ACME","legal_name":"Acme Solar Pvt Ltd","sector":"Solar - General","lens":"Mitigation"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# optimistic update (version 1 → 2)
curl -s -H "$KEY" -H 'If-Match: "1"' -H 'Content-Type: application/json' \
  -X PATCH $BASE/v1/entities/$ID -d '{"state":"Karnataka"}'

# 360° view
curl -s -H "$KEY" $BASE/v1/entities/$ID/dossier
```

---

## Why it is concurrency-safe

The brief demanded no deadlocks and no race conditions under parallel access. How that's
achieved (details in [`docs/SCHEMA.md`](../../docs/SCHEMA.md) and `app/db/session.py`):

- **Optimistic locking** (`version` column) → two racing writers can't both win; the
  loser gets `409 version_conflict`. No lost updates.
- **Idempotency keys** → a retried `POST` never creates a duplicate.
- **Bounded pool + timeouts** → a burst of parallel requests can't exhaust Postgres, and
  a stuck query self-terminates instead of holding locks into a deadlock.
- **Short, single-transaction requests** and consistent write ordering → deadlock-free.
- **Advisory-locked financial versioning** → concurrent restatements serialise on their
  own key and produce clean, sequential versions.

`tests/test_concurrency.py` fires dozens of overlapping requests and asserts exactly one
winner, no duplicates, no deadlocks.

---

## Testing

```bash
# needs a Postgres reachable at REGISTER_DB_* with a `register_test` database
createdb -h $REGISTER_DB_HOST -p $REGISTER_DB_PORT -U postgres register_test
make test          # runs migrations against register_test, then the full suite
```

---

## Deployment

### Docker

```bash
# build context is the repo root (installs packages/evam-backend-core too):
docker build -f register/Dockerfile -t prism-register:0.1.0 .
docker run -p 8000:8000 \
  -e REGISTER_DB_HOST=... -e REGISTER_DB_PASSWORD=... \
  -e REGISTER_API_KEYS=... prism-register:0.1.0 migrate-serve
```

Entrypoint subcommands: `serve` (default), `migrate`, `bootstrap` (tenant + reference
dropdowns, no business data), `seed` (adds the ATLAS mock), `import-mis` (load a mounted
MIS xlsx), and the combos `migrate-serve`, `migrate-bootstrap-serve` (**Compose default**),
`migrate-seed-serve`, `migrate-import-serve`.

Fully standalone in two commands (its own throwaway Postgres + the API):

```bash
docker run -d --name register-db -p 5432:5432 \
  -e POSTGRES_USER=register -e POSTGRES_PASSWORD=register -e POSTGRES_DB=register postgres:16
docker run -p 8000:8000 -e REGISTER_DB_HOST=host.docker.internal \
  -e REGISTER_DB_USER=register -e REGISTER_DB_PASSWORD=register -e REGISTER_DB_NAME=register \
  -e REGISTER_API_KEYS=my-key prism-register:0.1.0 migrate-bootstrap-serve
```

Or as a subset of the one-file compose (shared Postgres + the Register only):

```bash
docker compose -f ../../deploy/compose/docker-compose.yml up --build postgres register
```

### Helm

The Register **does not own a database** — it's a subchart of the PRISM umbrella
(`deploy/helm/prism`) and connects to the shared PRISM PostgreSQL (service
`prism-postgresql`) or a managed host. See
[`deploy/helm/prism/README.md`](../../deploy/helm/prism/README.md) for the full picture; in
short:

**Everything together (local)** — the umbrella brings up the shared DB + the Register
(subcharts are vendored, no `helm dependency build`):

```bash
helm upgrade --install prism ../deploy/helm/prism \
  -f ../deploy/helm/prism/values-local.yaml --namespace prism --create-namespace
```

**Register alone, against an existing shared/managed DB (production shape):**

```bash
helm upgrade --install register ../deploy/helm/prism/charts/register --namespace prism \
  --set image.tag=0.1.0 \
  --set database.host=<prism-postgresql-or-rds-host> \
  --set database.existingSecret=<db-secret> \
  --set apiKeys.existingSecret=register-api-keys \
  --set migrations.asHook=true
```

The chart runs migrations as a Job (a pre-install/upgrade hook when the DB already
exists, or in the main phase with a `wait-for-db` initContainer when the DB comes up
alongside it), and ships an HPA, PodDisruptionBudget, non-root/read-only-rootfs security
context, and health probes. Managed vs shared-in-cluster is **not baked in** — it's just
`database.*`. See [`values.yaml`](../../deploy/helm/prism/charts/register/values.yaml).

---

## Configuration

All settings use the `REGISTER_` prefix; see [`.env.example`](.env.example) for the full
list (DB, pool sizing, timeouts, API keys, tenancy, pagination, gunicorn workers).

## Layout

```
app/
  core/        config, logging, security, errors, middleware, pagination, enums
  db/          async engine + pool, declarative base + mixins
  models/      SQLAlchemy models (system, registry, deals, trackers, prism)
  schemas/     Pydantic request/response models
  repositories/generic CRUD (optimistic locking) + financial versioning
  api/         CRUD router factory, resource registry, custom + health routes
  seed/        ATLAS-mock + reference-data loader
migrations/    Alembic (0001 = authoritative initial schema)
tests/         CRUD + concurrency suite
data/          atlas_data.json (mock dataset extracted from ATLAS)
```
