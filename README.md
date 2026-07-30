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
| **PULSE** | Continuous news / adverse-media intelligence; 7 AM portfolio digest. | ✅ **in this repo** (`services/pulse/`) |
| **VocX** | Voice-based field touchpoint capture → structured into the Register (formerly "VOX"). | ✅ **in this repo** (`services/vocx/`) |
| **ATLAS** | Live management dashboard across Lending / Syndication / Asset Monetisation. | ✅ **service in this repo** (`services/atlas/`, UI planned) |
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
  vocx/                VocX — voice touchpoint capture → Register interactions via the
                       SDK (capture-id idempotency = exactly-once); stateless
  pulse/               PULSE — news/adverse-media radar → matches Register entities,
                       files RED/AMBER/GREEN intel idempotently; stateless
  atlas/               ATLAS — management dashboard service (read-side BFF):
                       /v1/dashboard, /v1/today, /v1/pipeline; view-level RBAC
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
                       · vocx · pulse · atlas · minio · temporal · workflows subcharts —
                       every module toggles with an enabled: flag; see docs/DEPLOYMENT.md)
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
The `postgresql` subchart runs a single database (`prism-postgresql`) that the Register
and the Access service connect to (VocX, PULSE and ATLAS are stateless — they only
talk to the Register over HTTP). Managed vs local is a
per-environment choice: use the bundled DB, or point modules at a managed India-resident
Postgres.

## Deploying PRISM

Two deployment paths (Docker Compose and Helm), each with a choice of identity provider.
The IdP is **pure configuration** — `*_OIDC_ISSUER` / `*_OIDC_ISSUERS` /
`*_OIDC_ALLOWED_DOMAINS` — so the image you test against Dex is byte-for-byte the image
you ship against Google.

| | **Dex** (bundled dev IdP) | **Google** (Workspace) |
| --- | --- | --- |
| best for | local, CI, unattended tests (password grant) | production (no passwords to hold) |
| membership check | Dex only holds your own accounts | **`ALLOWED_DOMAINS` is mandatory** — a valid Google token proves the account is *real*, not that it is Evam's |
| Postman sign-in | folder 00b (password grant) | folder 00c (refresh-token grant, one-time consent) |

**Once, before any compose path:** generate the edge's TLS cert — NGINX terminates HTTPS
on `:8443` and refuses to start without one.

```bash
scripts/gen_dev_certs.sh
# reaching the stack from another machine (Postman on the host, PRISM in a VM)?
#   EXTRA_SANS="IP:<vm-ip>" scripts/gen_dev_certs.sh --force
```

### 1 · Docker Compose — dev default (no IdP at all)

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
```

No issuer configured ⇒ identity is header trust (`X-User-Email`), enforcement flags off.
This is the posture the E2E Postman journey runs in out of the box. The front door is
`https://localhost:8443` (self-signed in dev — in Postman turn SSL verification off, or
trust `deploy/nginx/certs/tls.crt`); `:8080` only 301-redirects to it.

### 2 · Docker Compose + Dex (the production POSTURE, locally)

```bash
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod-posture.yml --profile sso up -d --build
```

Turns on `REQUIRE_AUTH` + OIDC (issuer = the bundled `dex` container), `ENFORCE_RBAC`,
`ENFORCE_RLS`. **`--profile sso` is required** — it is what starts Dex; an override file
cannot un-gate a profiled service. Sign in with the fixed identities from
`deploy/compose/dex/config.yaml` (`e2e.rm@` / `e2e.maker@` / `e2e.checker@evamfinance.com`,
password `prism`), or in Postman set `dexUrl=http://localhost:5556` so folder 00b does it.

### 3 · Docker Compose + Google

Same overlay, but point the gateway/orchestrator at Google instead of Dex — edit the
overlay (or a copy of it) to:

```yaml
GATEWAY_OIDC_ISSUER: "https://accounts.google.com"
GATEWAY_OIDC_AUDIENCE: "<client-id>.apps.googleusercontent.com"
GATEWAY_OIDC_ALLOWED_DOMAINS: "evamfinance.com"     # NEVER empty with Google
# and the WORKFLOWS_* trio on the orchestrator, same values
```

then bring it up **without** `--profile sso` (Dex is not needed). Prerequisite: an OAuth
client in Google Cloud Console. To keep Dex available *alongside* Google (e.g. staging —
humans on Google, CI on Dex), use the registry form and keep the profile:

```yaml
GATEWAY_OIDC_ISSUERS: "https://accounts.google.com|<client-id>.apps.googleusercontent.com,http://dex:5556/dex|prism"
```

A token is verified only by the issuer matching its own `iss` claim, so adding Dex never
weakens Google.

### 4 · Helm — local/dev with Dex

```bash
helm upgrade --install prism deploy/helm/prism -f deploy/helm/prism/values-local.yaml \
  --namespace prism --create-namespace
```

Subcharts are vendored (no `helm dependency build`). `values-local.yaml` is the open dev
posture; to exercise auth in-cluster, deploy a Dex (or any OIDC IdP) and set
`gateway.oidc.issuer` / `workflows.api.oidcIssuer` to it the same way as below.

### 5 · Helm — production with Google

```bash
helm upgrade --install prism deploy/helm/prism \
  -f deploy/helm/prism/values.yaml -f deploy/helm/prism/values-prod.yaml \
  --set gateway.oidc.issuer=https://accounts.google.com \
  --set gateway.oidc.audience=<client-id>.apps.googleusercontent.com \
  --set atlas.oidc.issuer=https://accounts.google.com \
  --set atlas.oidc.audience=<client-id>.apps.googleusercontent.com \
  --set workflows.api.oidcIssuer=https://accounts.google.com \
  --set workflows.api.oidcAudience=<client-id>.apps.googleusercontent.com \
  --namespace prism --create-namespace
  # + your real secrets via --set / existingSecret (the overlay REFUSES to render
  #   its REPLACE-* placeholders — that guard is on by default)
```

`values-prod.yaml` already sets `allowedDomains: "evamfinance.com"` for all three
services and turns on every enforcement (`requireAuth`, `enforceRbac`, `enforceRls`).
Two guards fail the render rather than deploy a hole: placeholder credentials, and a
public issuer (Google/Microsoft) with an empty domain allowlist. Multi-issuer works here
too: set `gateway.oidc.issuers` (and the workflows/atlas equivalents) instead of the
single pair.

Details: [`deploy/helm/prism/README.md`](deploy/helm/prism/README.md) (managed-DB and
single-module variants), [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md),
[`docs/POSTMAN.md`](docs/POSTMAN.md) §11–11b (running the E2E journey in either posture,
and the one-time Google consent flow for Postman).

## Testing it independently

The Register is designed to be exercised on its own:

1. `docker compose -f deploy/compose/docker-compose.yml up --build` (or `make migrate && make seed && make run`).
2. Import [`postman/Register.postman_collection.json`](postman/Register.postman_collection.json)
   and the environment; run CRUD against every table.
3. `make test` runs the CRUD + concurrency suite.

---

*Confidential — Evam Finance Pvt Ltd. All infrastructure is intended to be India-resident
per PRISM's localise-everything data posture.*
