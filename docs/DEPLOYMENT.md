# Deploying PRISM — need-basis, portable, production-grade

Every PRISM module is an **independently deployable service**: its own Docker image,
its own vendored Helm chart, no shared code at runtime (only HTTP + the platform SDK).
You deploy exactly the set you need, on any container platform, and add the rest later
without touching what is already running.

| Service | Image (build from repo root) | Chart | Needs | Port (compose) |
| --- | --- | --- | --- | --- |
| **Register** (source of truth) | `services/register/Dockerfile` | `charts/register` | PostgreSQL, S3/MinIO (documents) | 8000 |
| **Access** (users/roles/matrix) | `services/access/Dockerfile` | `charts/access` | PostgreSQL (own DB) | 8002 |
| **Gateway** (REST-API door, RBAC gate) | `services/gateway/Dockerfile` | `charts/gateway` | Register + Access | 8001 |
| **VocX** (voice touchpoints) | `services/vocx/Dockerfile` | `charts/vocx` | Register | 8003 |
| **PULSE** (news radar) | `services/pulse/Dockerfile` | `charts/pulse` | Register | 8004 |
| **ATLAS** (dashboard BFF) | `services/atlas/Dockerfile` | `charts/atlas` | Register (+ Access for RBAC) | 8005 |
| Workflows (Temporal worker) | `services/workflows/Dockerfile` | `charts/workflows` | Temporal + Register | — |

**The minimum viable install is the Register alone.** Everything else is optional and
additive: VocX/PULSE/ATLAS only need the Register's URL and an API key. The gateway +
access pair adds user-level RBAC in front of everything.

---

## 1. Local / demo — one command

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
```

Brings up all ten containers (edge, gateway, register, access, vocx, pulse, atlas,
postgres, minio, temporal + worker + UI). Don't need everything? Name the services:

```bash
docker compose -f deploy/compose/docker-compose.yml up --build postgres register pulse
```

## 2. Kubernetes — the umbrella chart (any cloud)

The umbrella deploys the platform with **one `enabled:` flag per module** — this is the
"need basis" switchboard:

```bash
helm upgrade --install prism deploy/helm/prism \
  -f deploy/helm/prism/values-local.yaml --namespace prism --create-namespace

# Register-only install:
helm upgrade --install prism deploy/helm/prism --namespace prism --create-namespace \
  --set gateway.enabled=false --set access.enabled=false --set vocx.enabled=false \
  --set pulse.enabled=false --set atlas.enabled=false \
  --set temporal.enabled=false --set workflows.enabled=false
```

Or install **one chart standalone** (all subcharts are vendored — no registry, no
`helm dependency build`):

```bash
helm upgrade --install pulse deploy/helm/prism/charts/pulse \
  --set register.baseUrl=http://prism-register --set register.apiKey.value=<key>
```

Works identically on EKS, GKE, AKS, or any on-prem cluster — the charts use only core
resources (Deployment/Service/Secret/ServiceAccount/CronJob).

## 3. Public cloud, production posture

The one decision that matters: **use managed backing services, keep PRISM stateless.**
Every PRISM container is disposable; all state lives in PostgreSQL and S3.

1. **Database** — a managed PostgreSQL (RDS / Cloud SQL / Azure Database; pick an
   India region for the data-residency posture). Set `postgresql.enabled=false` and
   point each module's `database.host` at it. One server, one database per service
   (`register`, `access`, Temporal's two) — the same layout the bundled DB uses.
2. **Object storage** — real S3 (or GCS/Azure via an S3-compatible endpoint). Set
   `minio.enabled=false` and configure `register.storage.s3.*`.
3. **Secrets** — never in values files: every chart takes `existingSecret` references;
   populate them from your cloud secret manager (External Secrets Operator works).
4. **Edge** — your cloud LB / Ingress in front of the **gateway** (TLS terminates
   there). Only the gateway (and, if used directly, ATLAS/VocX/PULSE) needs to be
   reachable; Register and Access stay cluster-internal.
5. **Rotate every default**: API keys (`REGISTER_API_KEYS`, `ACCESS_API_KEYS`,
   `PULSE_API_KEYS`), the gateway shared secret, DB and MinIO passwords.

### Multi-tenancy

One deployment serves many tenants. Every row in every database carries `tenant_id`;
every request carries `X-Tenant`; the Register resolves it and scopes every query.
Onboard a tenant with `POST /v1/tenants` (Register) + a tenant seed in Access — no new
infrastructure per tenant. Per-tenant RBAC (users, roles, the admin-editable access
matrix) lives in the Access service; the gateway enforces it at the door and the
Register re-checks scoped writes next to the data.

### Scaling to thousands of transactions

Everything except PostgreSQL is stateless — scale horizontally:

- **Replicas**: raise `replicaCount` on gateway/register/atlas (or `kubectl autoscale
  deployment prism-gateway --min 2 --max 10 --cpu-percent 70`). Rolling updates are
  zero-downtime (maxUnavailable 0 is set in the charts).
- **Workers per pod**: `REGISTER_WEB_CONCURRENCY` (gunicorn workers, default 4).
- **DB connections**: `pool_size × workers × replicas` must stay under Postgres
  `max_connections`. Defaults: pool 20, worker 4 → budget ~1 register replica per
  100 connections; use PgBouncer if you scale wide.
- **Where load lands**: reads are keyset-paginated (no OFFSET cliffs), writes are
  short transactions with optimistic locking, the gateway answers RBAC from cache
  (zero per-request calls to Access), NGINX rate-limits the edge. The concurrency
  test suite (`services/register/tests/test_concurrency.py`) proves no lost updates
  and no deadlocks under parallel load.
- **PULSE scans** are batch work — they run on a schedule (chart CronJob), not on the
  request path; a slow feed never blocks user traffic.

### Observability / debugging (see also CONTRIBUTING)

- Every request gets/propagates a **correlation id** (`X-Request-Id`) — NGINX →
  gateway → register → SDK calls; it is in every structured JSON log line. To trace a
  transaction across services: grep the id in your log aggregator.
- `GET /healthz` (liveness) and `GET /readyz` (readiness) on every service; probes are
  wired in the charts.
- The Register's `/v1/audit` records who changed what, when, with the request id.

## 4. Upgrades & data

- Migrations run automatically at Register/Access startup (`migrate-*-serve`
  entrypoints) or as Helm hooks (`migrations.asHook=true`) — pick one per environment.
- Backups: standard Postgres backups + S3 versioning cover the entire platform state.
- `GET /v1/export/json` is a tenant-level logical backup; `POST /v1/import/atlas-xlsx`
  the corresponding restore/import door.
