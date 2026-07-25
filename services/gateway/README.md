# PRISM Gateway — the REST-API service (the platform's front door)

The Gateway is the single entry point every client goes through. It does two jobs and
deliberately nothing else:

1. **The binary RBAC gate.** Each request's route is mapped to an operation
   (`app/routes_map.py`); the caller's effective grant for it comes from a local cache
   of Access-service facts. `NONE` → **403 right here** — the request never reaches the
   data plane. `FULL` / `APPROVE` / `SCOPED` → forwarded to the Register with verified
   identity headers (`X-User-Email`, `X-User-Id`, `X-User-Roles`), an
   `X-Authz-Decision` header, and a shared-secret stamp (`X-Gateway-Auth`) so the
   Register knows the identity was verified — spoofed headers on direct calls are
   rejected. The *scoped* half ("is this user assigned to this line?") is decided by
   the Register next to the data.
2. **The seam for client-specific logic.** Response shaping, composition, API
   versioning per client — all future per-client behaviour lives here so the core
   services never fork. `GET /v1/me` (identity facts + active assignments in one
   response) is the composition pattern in miniature.

It is stateless and holds no database: facts are fetched from the Access service **on
cache miss / TTL / matrix-version change — never per request** — and the last known
good answer is served if Access is briefly down. That is why the gate adds ~zero
latency and scales by adding replicas.

## API

| Endpoint | What it does |
| --- | --- |
| `ANY /v1/...` | The proxy: RBAC gate → forward to the Register (all Register endpoints work through it). |
| `GET /v1/me` | Composed identity: roles + effective matrices + active line assignments. |
| `POST /gateway/cache/invalidate` | Drop the facts cache (rarely needed — the matrix version handles refresh). |
| `GET /healthz` · `GET /readyz` | Liveness / readiness. |

Headers in: whatever the client sends (`X-API-Key`, `X-Tenant`, `X-User-Email`, ...).
A request **without** `X-User-Email` is a machine caller — it is forwarded unchanged
and the Register applies its own machine-caller policy.

## Configuration (env, prefix `GATEWAY_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `GATEWAY_REGISTER_URL` | `http://register:8000` | The data plane it fronts |
| `GATEWAY_ACCESS_URL` | `http://access:8000` | Where identity facts come from |
| `GATEWAY_ACCESS_API_KEY` | `dev-local-key` | The gateway's own key for calling Access |
| `GATEWAY_GATEWAY_SHARED_SECRET` | *(empty = dev)* | Stamped on forwarded identity; must equal the Register's `REGISTER_GATEWAY_SHARED_SECRET` |
| `GATEWAY_CACHE_TTL_S` | `30` | How long resolved facts are reused |
| `GATEWAY_UPSTREAM_TIMEOUT_S` | `30` | Per-request upstream timeout |
| `GATEWAY_LOG_LEVEL` / `GATEWAY_LOG_JSON` | `INFO` / `true` | Structured logging |

## Run it standalone

The gateway needs its two upstreams reachable (a Register, an Access) — nothing else.

```bash
# Build & run (build context = repo root):
docker build -f services/gateway/Dockerfile -t prism-gateway:0.1.0 .
docker run -p 8001:8000 \
  -e GATEWAY_REGISTER_URL=http://host.docker.internal:8000 \
  -e GATEWAY_ACCESS_URL=http://host.docker.internal:8002 \
  -e GATEWAY_ACCESS_API_KEY=my-key \
  -e GATEWAY_GATEWAY_SHARED_SECRET=change-me prism-gateway:0.1.0

# Use it — same paths as the Register, plus RBAC:
curl -H "X-API-Key: my-key" -H "X-Tenant: EVAM" \
  -H "X-User-Email: admin@evamfinance.com" http://localhost:8001/v1/entities
```

Other paths to the same thing:

```bash
# Local dev (no Docker):
cd services/gateway && pip install -e ../../packages/evam-backend-core -e ".[dev]"
uvicorn app.main:app --port 8001

# Compose subset (DB + register + access + gateway):
docker compose -f deploy/compose/docker-compose.yml up --build postgres register access gateway

# Kubernetes, standalone (vendored chart):
helm upgrade --install gateway deploy/helm/prism/charts/gateway \
  --set upstreams.registerUrl=http://prism-register \
  --set upstreams.accessUrl=http://prism-access \
  --set gatewaySecret.value=<secret> --set accessApiKey.value=<key>
```

In production put your load balancer / Ingress (or the bundled NGINX) in front of the
gateway and keep Register + Access cluster-internal.

## Tests

`pytest` here runs **7 end-to-end tests on a real three-service stack** — the fixtures
boot an actual Register and Access (uvicorn subprocesses, real migrations and seeds)
and prove the full ladder: NONE at the gate, FULL/SCOPED pass-through, live matrix
edits taking effect, and the spoofed-identity wall.

## Extending it (start here if you are new)

- **New route → operation mapping** → one line in `app/routes_map.py`.
- **Client-specific logic** → this service is the sanctioned home; add composed
  endpoints like `/v1/me` in `app/main.py` and keep core services untouched.
- **Cache behaviour** → `app/resolver.py` (TTL, version bump, last-known-good).
