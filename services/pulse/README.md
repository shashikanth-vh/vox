# PRISM PULSE — the news / adverse-media radar

PULSE watches the outside world for the companies in the Register. It fetches news from
configured sources, matches each item against the tenant's entities, classifies it
RED / AMBER / GREEN with auditable keyword rules, and files one
`external-intelligence` row per (item, entity) into the Register — idempotently, so a
re-run never duplicates an alert. `GET /v1/digest` is the payload behind the 7 AM
portfolio digest.

```
sources (RSS / JSON / sample) ──▶ fetch ──▶ match against entities ──▶ signal ──▶
        Register /v1/external-intelligence  (Idempotency-Key = pulse:{tenant}:{entity}:{hash})
```

## Why it is built this way

- **Stateless, no own database.** The Register is the source of truth; the
  Idempotency-Key doubles as dedup. One less schema to migrate, nothing to back up,
  scale by adding replicas.
- **Multi-tenant per request.** Every endpoint accepts `X-Tenant`; PULSE keeps one
  Register client per tenant. One deployment serves all tenants.
- **Explainable matching.** Name substring match + keyword signal rules
  (`PULSE_RED_WORDS` / `PULSE_GREEN_WORDS`). A human can always answer *"why did this
  alert fire?"* — which matters when the alert can stop a disbursement. Swap
  `app/matching.py` for something smarter later; it is the seam.
- **Scheduling is external.** Point a Kubernetes CronJob (the Helm chart ships one),
  Temporal schedule, or plain cron at `POST /v1/scan`. PULSE itself has no clock — that
  keeps replicas identical and restarts boring.

## API

| Endpoint | What it does |
| --- | --- |
| `POST /v1/scan` | Fetch every source, match, file intel. Returns per-provider stats + what was filed. |
| `POST /v1/items` | Push one item (scraper / webhook / human). Explicit `entity_id` or auto-match. |
| `GET /v1/digest?hours=24` | Recent intel grouped RED/AMBER/GREEN (the 7 AM digest payload). |
| `GET /healthz`, `GET /readyz` | Liveness / readiness. |

Headers: `X-Tenant` (defaults to `PULSE_REGISTER_TENANT`), `X-API-Key` (required only
when `PULSE_API_KEYS` is set — always set it in production).

## Configuration (env, prefix `PULSE_`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `PULSE_REGISTER_BASE_URL` | `http://register:8000` | The Register (or the gateway) |
| `PULSE_REGISTER_API_KEY` | `dev-local-key` | Must be in the Register's `REGISTER_API_KEYS` |
| `PULSE_REGISTER_TENANT` | `EVAM` | Default tenant when `X-Tenant` absent |
| `PULSE_API_KEYS` | *(empty = open)* | PULSE's own front door, comma-separated |
| `PULSE_SOURCES` | sample feed | JSON list: `[{"name":..,"kind":"rss"\|"json"\|"sample","url":..}]` |
| `PULSE_RED_WORDS` / `PULSE_GREEN_WORDS` | sensible defaults | Signal keyword lists |
| `PULSE_WATCHLIST_MAX_ENTITIES` | `2000` | Scan safety cap |

## Run it

```bash
# Local (needs a running Register):
cd services/pulse && pip install -e ".[dev]" && uvicorn app.main:app --port 8004

# Docker (build context = repo root):
docker build -f services/pulse/Dockerfile -t prism-pulse:0.1.0 .
docker run -p 8004:8000 \
  -e PULSE_REGISTER_BASE_URL=http://host.docker.internal:8000 \
  -e PULSE_REGISTER_API_KEY=my-key prism-pulse:0.1.0

# Compose subset (shared Postgres + Register + PULSE):
docker compose -f deploy/compose/docker-compose.yml up --build postgres register pulse

# Kubernetes, standalone (the chart is vendored, no registry needed):
helm upgrade --install pulse deploy/helm/prism/charts/pulse \
  --set register.baseUrl=http://prism-register --set register.apiKey.value=<key>

# Try it:
curl -X POST -H "X-Tenant: EVAM" http://localhost:8004/v1/scan
curl -H "X-Tenant: EVAM" "http://localhost:8004/v1/digest?hours=24"
```

## Tests

`make -C ../.. test-pulse` (or `pytest` here) — unit tests for the matching rules plus
an end-to-end scan → intel → digest loop against a real Register with real Postgres.

## Extending it (start here if you are new)

- **New source kind** → subclass `Provider` in `app/providers.py`, implement `fetch()`,
  register it in `_KINDS`. One class, one dict entry.
- **Smarter matching / scoring** → replace functions in `app/matching.py`. Nothing else
  imports its internals.
- **New signal rules** → usually just env config (`PULSE_RED_WORDS`), no code at all.
