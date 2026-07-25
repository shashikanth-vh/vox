# PRISM ATLAS — the live management dashboard service

ATLAS is the read-side of the platform: a stateless BFF (backend-for-frontend) that
composes the numbers a management dashboard needs from the Register and serves them as
small JSON payloads. It owns no data — a bug in ATLAS can never corrupt the book — and
it scales by adding replicas.

```
front-end ──▶ ATLAS ──▶ Register  (reads via the platform SDK, tenant-scoped)
                 └────▶ Access    (view-level RBAC: may this user open this view?)
```

## API

| Endpoint | What it does |
| --- | --- |
| `GET /v1/dashboard` | The whole book summarised: every vertical's counts by stage/status, amounts (₹ Cr), open intel by signal. |
| `GET /v1/today?horizon_days=7` | What needs a human today: due/overdue lead actions, lender chases awaiting response, covenants due. |
| `GET /v1/pipeline/{vertical}` | Slim rows for one board: `leads` · `deals` · `lending` · `syndication` · `asset-monetisation`. |
| `GET /v1/entities/{id}/summary` | One company composed — the Register's 360° dossier. |
| `POST /atlas/cache/invalidate` | Drop the cached permissions (after admin matrix edits). |

Headers: `X-Tenant` (defaults to `ATLAS_REGISTER_TENANT`) and `X-User-Email` — the
caller's identity, checked against the Access service's **view matrix** (`dashboard`,
`today`, `leads`, …), the same matrix admins edit live at `PATCH /v1/access`.

## RBAC — what ATLAS checks, and what it doesn't

- **View level (ATLAS's job):** can this user open this view at all? Answered by the
  Access service (`/v1/resolve`), cached for `ATLAS_PERMISSION_CACHE_TTL_S` seconds,
  stale-cache fallback if Access is briefly down — same policy as the gateway.
- **Row level (the Register's job):** which rows a SCOPED user may write is enforced
  next to the data (line assignments). ATLAS only reads, and reads with its own
  service key; it does not re-implement data security.
- Gating is off when `ATLAS_ACCESS_URL` is empty (dev). In production set it, and set
  `ATLAS_REQUIRE_USER=true` so every dashboard call is attributable to a person.

## Configuration (env, prefix `ATLAS_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ATLAS_REGISTER_BASE_URL` | `http://register:8000` | The Register |
| `ATLAS_REGISTER_API_KEY` | `dev-local-key` | Must be in `REGISTER_API_KEYS` |
| `ATLAS_REGISTER_TENANT` | `EVAM` | Default tenant when `X-Tenant` absent |
| `ATLAS_ACCESS_URL` | *(empty = gating off)* | The Access service |
| `ATLAS_REQUIRE_USER` | `false` | Refuse anonymous view calls (set `true` in prod) |
| `ATLAS_PERMISSION_CACHE_TTL_S` | `30` | Permission cache TTL |
| `ATLAS_MAX_PAGES_PER_RESOURCE` | `10` | Read cap (×200 rows) per vertical per request |

## Run it

```bash
# Local (needs a running Register):
cd services/atlas && pip install -e ".[dev]" && uvicorn app.main:app --port 8005

# Docker (build context = repo root):
docker build -f services/atlas/Dockerfile -t prism-atlas:0.1.0 .
docker run -p 8005:8000 \
  -e ATLAS_REGISTER_BASE_URL=http://host.docker.internal:8000 \
  -e ATLAS_REGISTER_API_KEY=my-key prism-atlas:0.1.0

# Compose subset (shared Postgres + Register + Access + ATLAS):
docker compose -f deploy/compose/docker-compose.yml up --build postgres register access atlas

# Kubernetes, standalone (vendored chart, no registry needed):
helm upgrade --install atlas deploy/helm/prism/charts/atlas \
  --set register.baseUrl=http://prism-register --set register.apiKey.value=<key>

# Try it:
curl -H "X-Tenant: EVAM" -H "X-User-Email: admin@evamfinance.com" \
  http://localhost:8005/v1/dashboard
```

## Code map (start here if you are new)

- `app/aggregations.py` — **pure functions**: lists of Register rows in, JSON summaries
  out. No I/O. Add your new widget's math here and unit-test it in isolation.
- `app/permissions.py` — the view gate (resolve + TTL cache + degradation policy).
- `app/main.py` — the endpoints: gate → fetch bounded pages → aggregate → return.
- `VERTICALS` in `app/main.py` — the one map to extend when a new vertical arrives.

Tests: `pytest` here runs the aggregation unit tests plus end-to-end composed views
against a real Register (uvicorn subprocess, real Postgres, real migrations).
