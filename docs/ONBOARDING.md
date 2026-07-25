# PRISM onboarding — the tour for a brand-new engineer

You just joined and you have never seen this codebase (maybe never seen a production
codebase). This page is the map. Setup commands live in
[`CONTRIBUTING.md`](../CONTRIBUTING.md); this is *how the system thinks*, so the code
reads like something you already understand.

## The 60-second mental model

PRISM is a set of small services around one source of truth:

```
                 you / the front-end / curl
                            │
              NGINX (edge) ─▶ GATEWAY  ── "may this user do this?" ──▶ ACCESS
                            │  (:8001)      (cached answer)             (:8002)
                            ▼
                        REGISTER (:8000)  ◀── the only place data lives
                       ▲    ▲    ▲    ▲
        VocX (:8003) ──┘    │    │    └── Workflows (Temporal worker)
        voice notes         │    └── ATLAS (:8005) dashboards (reads only)
                            └── PULSE (:8004) news radar (writes intel)
```

- **The Register** is a REST API over PostgreSQL. Every table row belongs to a tenant
  (`tenant_id`), every change is audited, every update is optimistic-locked.
- **Access** stores users, roles, and an access matrix admins can edit at runtime.
- **The Gateway** is the front door: it asks Access (cached) whether the caller may
  perform an operation, then forwards to the Register with verified identity headers.
- **VocX / PULSE / ATLAS** never touch the database. They talk to the Register over
  HTTP using `packages/evam-register-client`. That is why they are small.

If you understand one request end-to-end, you understand the platform. Trace one:
`curl` the gateway with `X-User-Email`, watch the logs of gateway → register (they
share the same `X-Request-Id` in every JSON log line).

## Where things are (and why)

| Path | What lives there | Rule of thumb |
| --- | --- | --- |
| `services/register/` | The source of truth. Models, migrations, CRUD, RBAC-next-to-data. | Data logic goes here. |
| `services/access/`, `services/gateway/` | Who may do what, and the door that enforces it. | Permission logic goes here. |
| `services/vocx|pulse|atlas/` | Small satellites: capture, radar, dashboards. | Each is ~4 files. Read one whole. |
| `packages/evam-backend-core/` | Logging, errors, DB helpers, CRUD engine, app factory. | Shared concerns. Never copy — extend. |
| `packages/evam-register-client/` | The typed SDK every satellite uses. | New Register endpoint? Add a method here. |
| `deploy/` | One compose file (whole platform) + one Helm umbrella (per-module flags). | |
| `docs/` | Schema, deployment guide, decision records (`adr/` — the "why"). | |

**Start by reading `services/vocx/` top to bottom** (~200 lines including tests). It
shows the whole pattern: config from env → FastAPI app factory → SDK call with an
idempotency key → e2e test against a real Register. PULSE and ATLAS are the same
pattern with more moving parts.

## The five platform habits

Every service follows the same five habits. Recognise them once, see them everywhere:

1. **Config is env vars** — `app/config.py`, pydantic-settings, a prefix per service
   (`REGISTER_`, `PULSE_`, …). No config files to hunt down.
2. **Logs are structured JSON with a request id** — `get_logger(...)`, and the
   `X-Request-Id` header flows through every hop. Debugging = grep one id.
3. **Errors are RFC-style problem JSON** — raise `NotFoundError` / `ConflictError` /
   `ValidationAppError` from `evam_backend_core.errors`; the handler does the rest.
4. **Writes are exactly-once** — send an `Idempotency-Key`; retrying the same key
   replays the stored response instead of duplicating (VocX uses the capture id,
   PULSE hashes the news URL).
5. **Concurrent updates are safe** — reads return a `version`; send `If-Match:
   <version>` on update and a stale write comes back `409` instead of silently losing.

## Your first change, step by step (worked example)

Say you want ATLAS to show *"deals with no interaction in 30 days"*:

1. **Write the math as a pure function** in `services/atlas/app/aggregations.py` —
   rows in, dict out. No I/O.
2. **Unit-test it** in `services/atlas/tests/test_atlas.py` (copy an existing test).
3. **Wire it into an endpoint** in `app/main.py` — fetch rows with `_rows(client,
   "deals")`, call your function, add the result to the response.
4. **Run the gate**: `make ci` (or just `cd services/atlas && pytest`).
5. Commit with a message that says *why*, push, open a PR. CI runs the same gate.

New endpoint in the Register instead? The pattern is `app/api/resources.py` (generic
CRUD via `ResourceSpec`) or `app/api/custom.py` (bespoke routes) + a migration under
`migrations/versions/` + a test. Copy the nearest neighbour; the code comments in each
module explain the contract.

A whole new service? `make new-service NAME=cipher` scaffolds one on
`evam-backend-core`, and `services/vocx/` is the reference for wiring Docker,
Helm and CI.

## Debugging checklist

- **A request fails** → take the `request_id` from the error response / response
  headers, grep every service's logs for it. The failing hop is the last line.
- **A permission surprise** → `GET /v1/resolve?email=…` on Access shows the user's
  effective matrix; `GET /v1/authz/check?...` on the Register answers "may I, on this
  line?"; the gateway logs the operation it mapped the route to.
- **Data looks wrong** → `GET /v1/audit?resource_type=…&resource_id=…` shows every
  change with actor + before/after.
- **Tests fail locally with "Connection refused"** → your test Postgres isn't up
  (see CONTRIBUTING one-time setup).
- **Something works locally, fails in a container** → check env vars first
  (`docker compose config` shows what each service actually receives).

## Glossary (the domain in ten lines)

- **Entity** — a company we track. Everything hangs off entities.
- **Lead → Deal** — BD pipeline; a deal can carry three product lines at once:
  **Lending** (we lend), **Syndication** (we place debt with other lenders — each
  approached lender is a row), **Asset Monetisation** (we help sell operating assets).
- **Financials** — versioned statements (a restatement is a new version, not an edit).
- **Interactions** — the append-only touchpoint timeline (meetings, calls, site visits).
- **Documents / Data Register** — the KYC-style checklist + files per company.
- **External intelligence** — news/bureau signals, RED / AMBER / GREEN.
- **Line assignment** — "this analyst works this deal" — what SCOPED access checks.
- **Tenant** — one customer organisation; every row and request is tenant-scoped.
