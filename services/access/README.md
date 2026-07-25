# PRISM Access — user management & the admin-editable access matrix

Access is the platform's identity-and-permissions service. It owns three things, all as
plain database tables an Admin can manage through the API — no deploy needed to change
who may do what:

1. **Users** — the Employees governance table (`@evamfinance.com` addresses; SSO
   integrity is enforced at the API).
2. **Roles** — stacked: a user may hold several (Admin, Management, BD Head, Credit
   Head, Syn Head, AM Head, BDRM, Deal Analyst, Syn RM, AM RM); effective access is
   the **maximum** across held roles.
3. **The access matrix** — view + operation grants per role
   (NONE / READ / SCOPED / FULL / APPROVE), seeded from the ATLAS RBAC v3.1 spec
   (`evam_backend_core.rbac`) and editable cell-by-cell at runtime. **Guardrail
   cells** (`delete_row`, `backup_restore`, the `audit` and `activity_log` views)
   refuse edits even from Admin. Every change bumps a matrix version, which is how
   gateway caches know to refresh.

Who calls it: the **Gateway** fills its permission cache from `/v1/resolve` (on cache
miss / version change — never per request); **ATLAS** gates dashboard views the same
way; humans/UIs use the governance endpoints. It can equally serve as a standalone
user-management service for any other system that understands roles and grants.

## API

| Endpoint | What it does |
| --- | --- |
| `POST /v1/users` | Add a user, optionally with initial roles. **Admin-only.** |
| `GET /v1/users`, `GET /v1/users/{id}` | The team directory (readable by every role). |
| `PATCH /v1/users/{id}` | Edit a user. **Admin-only.** |
| `POST /v1/users/{id}/roles` · `DELETE /v1/users/{id}/roles/{role}` | Grant / revoke a role (stacking). **Admin-only.** |
| `GET /v1/access` | The live matrix (views + operations) and its version. |
| `PATCH /v1/access` | Edit one cell `{kind, item, role, access}`. **Admin-only**; guardrails refuse. |
| `GET /v1/resolve?email=` | User → roles + effective matrices + version — what gateways cache. |
| `GET /v1/access/version` | Just the version (cheap cache-validity poll). |
| `GET /v1/me` | The calling user's own resolution. |
| `GET /healthz` · `GET /readyz` | Liveness / readiness. |

Headers: `X-API-Key` (always), `X-Tenant` (tenant scoping), `X-User-Email` (the acting
user — Admin-only writes are checked against this identity).

## Configuration (env, prefix `ACCESS_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ACCESS_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` | `access` db | Its own PostgreSQL database (a DB on the shared server, or any Postgres) |
| `ACCESS_API_KEYS` | `dev-local-key` | Comma-separated machine keys — rotate in production |
| `ACCESS_DEFAULT_TENANT_CODE` | `EVAM` | Tenant when `X-Tenant` absent |
| `ACCESS_USER_EMAIL_DOMAIN` | `evamfinance.com` | Users must belong to this domain |
| `ACCESS_ENFORCE_RBAC` | `false` | When on, governance writes REQUIRE an Admin user context |
| `ACCESS_LOG_LEVEL` / `ACCESS_LOG_JSON` | `INFO` / `true` | Structured logging |

## Run it standalone

Access is fully usable on its own — all it needs is a PostgreSQL database.

```bash
# 1. A throwaway Postgres:
docker run -d --name access-db -p 5432:5432 \
  -e POSTGRES_USER=access -e POSTGRES_PASSWORD=access -e POSTGRES_DB=access postgres:16

# 2. Build & run (build context = repo root). migrate-seed-serve migrates, seeds the
#    tenant + spec matrix + admin@evamfinance.com, then serves:
docker build -f services/access/Dockerfile -t prism-access:0.1.0 .
docker run -p 8002:8000 -e ACCESS_DB_HOST=host.docker.internal \
  -e ACCESS_DB_USER=access -e ACCESS_DB_PASSWORD=access -e ACCESS_DB_NAME=access \
  -e ACCESS_API_KEYS=my-key prism-access:0.1.0 migrate-seed-serve

# 3. Use it:
curl -H "X-API-Key: my-key" -H "X-Tenant: EVAM" \
  "http://localhost:8002/v1/resolve?email=admin@evamfinance.com"
```

Entrypoint subcommands: `serve` (default) · `migrate` · `seed` · `migrate-serve` ·
`migrate-seed-serve`.

Other paths to the same thing:

```bash
# Local dev (no Docker):
cd services/access && pip install -e ../../packages/evam-backend-core -e ".[dev]"
alembic upgrade head && python -m app.seed && uvicorn app.main:app --port 8002

# Compose subset (shared Postgres + Access only):
docker compose -f deploy/compose/docker-compose.yml up --build postgres access

# Kubernetes, standalone (vendored chart — no registry, no dependency build):
helm upgrade --install access deploy/helm/prism/charts/access \
  --set database.host=<postgres-host> --set apiKeys.value=<key>
```

## Tests

Create an `access_test` database, then `pytest` here (5 tests: governance, guardrails,
stacking, resolve). CI runs them on every PR.

## Extending it (start here if you are new)

- **New role or operation** → edit the spec artifact
  `packages/evam-backend-core/evam_backend_core/rbac.py` (one table, heavily
  commented); the seed and `/v1/resolve` pick it up. Runtime overrides always go
  through `PATCH /v1/access`.
- **New guardrail** → `IMMUTABLE_ITEMS` in `app/matrix.py`.
- The whole permission model is documented on the RBAC design page in
  `docs/architecture/`.
