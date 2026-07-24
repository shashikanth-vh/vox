# evam-register-client

Typed **async + sync** client for the PRISM **Register** (the platform's source of truth).
Every vertical — **VOX**, **CIPHER**, **PULSE**, portal/gateway APIs — integrates through
this one client, so they all speak the Register's contract identically and correctly.

Handles, on every call, the things each vertical would otherwise re-implement (and get
subtly wrong):

- **Auth headers** — `X-API-Key`, `X-Tenant`, `X-Actor` from config.
- **Idempotency** — a fresh `Idempotency-Key` on every create, so an at-least-once retry
  never duplicates a row (VOX/PULSE are event-driven and *will* retry).
- **Optimistic concurrency** — `expected_version` → `If-Match`; a lost race raises
  `VersionConflictError` (never a silent lost update).
- **Transient retry** — network errors, timeouts and `429/502/503/504` retried with
  exponential backoff + jitter (honouring `Retry-After`); writes retried only when they
  carry an idempotency key or `If-Match`.
- **Correlation** — forwards your `request_id` (or mints one) as `X-Request-ID`, so a VOX
  call → Register write is traceable end-to-end.
- **Typed errors** — the Register's RFC-9457 body becomes `NotFoundError`,
  `VersionConflictError`, `ValidationError`, `RateLimitedError`, … each carrying the
  Register's `request_id`.
- **Keyset pagination** — `list(...)` returns a `Page`; `iterate(...)` streams every row.

## Install

```bash
pip install -e packages/evam-register-client
```

## Async (the default for FastAPI verticals)

```python
from evam_register_client import AsyncRegisterClient
from evam_register_client.errors import VersionConflictError

async with AsyncRegisterClient(base_url="http://register:8000", api_key="vox-key",
                               tenant="EVAM", actor="vox") as reg:
    # VOX — log an interaction (auto idempotency-key, retried safely)
    await reg.log_interaction("Deal", deal_id, "Phone Call", source="VOX",
                              summary="Promoter call", transcript="…",
                              idempotency_key=vox_recording_id)   # dedupe on your own id

    # CIPHER — append a new financial version
    await reg.create_financial_version(entity_id, "Audited", "2026-03-31",
                                       revenue=120.0, is_consolidated=True, scale="Crore")

    # PULSE — raise a signal, then triage
    intel = await reg.create_intelligence(entity_id, "Court Case", signal="RED",
                                          title="Litigation filed")
    await reg.acknowledge_intelligence(intel["id"])

    # optimistic update
    ent = await reg.get("entities", entity_id)
    try:
        await reg.update("entities", entity_id, {"state": "Karnataka"},
                         expected_version=ent["version"])
    except VersionConflictError:
        ...  # re-read and retry

    # stream every entity in a sector
    async for e in reg.iterate("entities", sector="Solar - General"):
        ...
```

## Sync (scripts / cron / batch — e.g. a CIPHER nightly pull)

```python
from evam_register_client import RegisterClient

with RegisterClient(base_url="http://register:8000", api_key="cipher-key", actor="cipher") as reg:
    ent = reg.create("entities", {"code": "ACME", "legal_name": "Acme Ltd"})
    rows = reg.iterate("financials", entity_id=ent["id"])   # returns a list in sync mode
```

## Configuration

Constructor args override env (`REGISTER_CLIENT_` prefix): `BASE_URL`, `API_KEY`, `TENANT`,
`ACTOR`, `CONNECT_TIMEOUT_S`, `READ_TIMEOUT_S`, `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY_S`,
`RETRY_MAX_DELAY_S`, `MAX_CONNECTIONS`, `AUTO_IDEMPOTENCY`.

## Notes

- Reuse **one client per process** — it pools connections.
- Resource bodies are returned as plain `dict` (the Register's JSON) so the client stays
  forward-compatible as the Register's schemas evolve; only `Page` is typed.
- Give each vertical its **own API key** so writes are attributable/revocable per service.
