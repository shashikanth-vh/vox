# PRISM

**Platform for Real-asset Intelligence, Screening & Monitoring** — Evam Finance's
internal operating system for climate finance (Lend · Mobilise · Recycle).

PRISM is built as four concentric rings — **Doors → Register → Workflows → Intelligence
Layer** — around one non-negotiable centre: the **Register**, the single source of truth
that every module reads from and writes to. The build sequence starts there, because a
bad Register design is the single most expensive architectural mistake to make.

This repository is being built module by module. **The Register is the first module, and
it is live here.**

## Modules (build sequence)

| Module | What it is | Status |
| --- | --- | --- |
| **Register** | The data foundation — 7 master tables, entity-centric, tenant-aware, versioned. Source of truth. | ✅ **in this repo** (`services/register/`) |
| CIPHER | Underwriting brain — automated CAM + Internal Risk Grade (IRG). | planned |
| PULSE | Continuous news / adverse-media intelligence; 7 AM portfolio digest. | planned |
| VOX | Voice-based field touchpoint capture → structured into the Register. | planned |
| ATLAS | Live management dashboard across Lending / Syndication / Asset Monetisation. | planned |
| SCRIBE | Standardised documents & operations engine. | Phase 2 |

## The Register

The [`services/register/`](register/) service is a production-grade REST API over PostgreSQL:

- Entity-centric, tenant-aware, product-aware, versioned (the four PRISM commitments).
- Concurrency-safe by construction — optimistic locking, idempotent creates, bounded
  pool, hard timeouts. No lost updates, no deadlocks under parallel load.
- Full CRUD for all 18 tables + versioned financials, nested syndication lenders, an
  entity 360° dossier, reference vocabularies and an audit trail.
- Seeded with the real ATLAS mock dataset (132 entities, 111 deals, 74 syndications, …).
- Deployable via Docker Compose and Helm.

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
curl -H "X-API-Key: dev-local-key" http://localhost:8000/v1/entities?with_total=true
```

See **[`CHANGELOG.md`](CHANGELOG.md)** for what changed, **[`QUICKSTART.md`](QUICKSTART.md)** for step-by-step run/test/build/deploy commands,
**[`register/README.md`](services/register/README.md)** for the full guide, and
**[`docs/SCHEMA.md`](docs/SCHEMA.md)** for the data model.

## Repository layout

A monorepo: **`services/*`** are deployable services, **`packages/*`** are shared libraries
every service builds on. A new engineer knows exactly where things go.

```
services/
  register/            the Register service (FastAPI) — the source of truth; enforces
                       SCOPED access next to the data (assignments, approvals)
  gateway/             the REST-API service — cached binary RBAC gate + routing; the
                       future home of client-specific logic (stateless)
  access/              user management & access facts — users, roles, and the
                       admin-editable access matrix (guardrails; /v1/resolve)
  workflows/           Temporal worker — durable orchestration; activities write the
                       Register via the client SDK (the Workflows ring, realized)
packages/
  evam-backend-core/   shared platform: build a service on it (logging, errors, DB,
                       CRUD, retry, app factory) — the Register is its reference impl
  evam-register-client/ typed client: call the Register from any vertical
deploy/
  compose/             docker-compose.yml — the whole platform: edge + gateway + register
                       + access + DB + MinIO + Temporal + worker (one command)
  nginx/               NGINX edge config (TLS-ready, routing, rate-limit, correlation id)
  helm/prism/          the PRISM umbrella chart (postgresql · register · access · gateway
                       · minio · temporal · workflows subcharts)
docs/                  SCHEMA.md (data model + ERD), openapi.json, adr/ (decision records)
scripts/               repo tooling (new_service.py — scaffold a vertical)
postman/               Postman collection + environment (CRUD for every table)
```

**Building a new vertical** (CIPHER/PULSE/VOX/gateway): `make new-service NAME=cipher` —
it scaffolds on `evam-backend-core`; use `evam-register-client` to talk to the Register.
See **[`BACKEND_STANDARDS.md`](BACKEND_STANDARDS.md)** and **[`CONTRIBUTING.md`](CONTRIBUTING.md)**.

## Quality & onboarding

- **New here?** [`CONTRIBUTING.md`](CONTRIBUTING.md) — zero-to-productive setup + how-to.
- **Why it's built this way?** [`docs/adr/`](docs/adr/) — architecture decision records.
- **Gate:** `make ci` runs `ruff` (lint) + `mypy` (types, green) + `pytest` (63 tests, real
  Postgres). CI (`.github/workflows/ci.yml`) enforces it on every PR; `pre-commit` catches
  issues before that.

**One PRISM Helm chart containing the modules as subcharts**, and one architectural
decision inside it: **PostgreSQL is a shared platform service, not owned by any module.**
The `postgresql` subchart runs a single database (`prism-postgresql`) that the Register —
and CIPHER, PULSE, VOX, ATLAS as they arrive — all connect to. Managed vs local is a
per-environment choice: use the bundled DB, or point modules at a managed India-resident
Postgres.

Two deployment paths, both under `deploy/`:
- **Docker Compose** — `docker compose -f deploy/compose/docker-compose.yml up --build`
  (a shared `postgres` service + the Register).
- **Helm** — the umbrella brings up the shared DB + modules together (subcharts are
  vendored, no `helm dependency build` needed):
  ```bash
  helm upgrade --install prism deploy/helm/prism -f deploy/helm/prism/values-local.yaml \
    --namespace prism --create-namespace
  ```
  See [`deploy/helm/prism/README.md`](deploy/helm/prism/README.md) for managed-DB and
  single-module variants.

## Testing it independently

The Register is designed to be exercised on its own:

1. `docker compose -f deploy/compose/docker-compose.yml up --build` (or `make migrate && make seed && make run`).
2. Import [`postman/Register.postman_collection.json`](postman/Register.postman_collection.json)
   and the environment; run CRUD against every table.
3. `make test` runs the CRUD + concurrency suite.

---

*Confidential — Evam Finance Pvt Ltd. All infrastructure is intended to be India-resident
per PRISM's localise-everything data posture.*
