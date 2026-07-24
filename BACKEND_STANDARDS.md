# PRISM Backend Standards

Every PRISM backend service is built on **`evam-backend-core`** (in
[`packages/evam-backend-core`](packages/evam-backend-core)). The Register is the reference
implementation. This document is the contract: follow it and every service gets the same
production-grade behaviour — identical logging, error shape, concurrency safety and
operability — for free.

> Rule of thumb: **cross-cutting concerns come from the platform; a service supplies only
> its models, schemas, resources and business endpoints.** If you find yourself
> re-implementing logging, error handling, DB pooling, pagination or retry, stop — it's in
> the core.

**Two shared packages:**
- [`packages/evam-backend-core`](packages/evam-backend-core) — build a *service* on it.
- [`packages/evam-register-client`](packages/evam-register-client) — *call the Register*
  from any vertical (VOX/CIPHER/PULSE/gateway) with it. Never hand-roll HTTP against the
  Register: the client already does auth, idempotency, optimistic concurrency, transient
  retry, correlation and typed errors. See its README for usage.

---

## What the platform gives you

| Concern | Module | What you get |
| --- | --- | --- |
| **Logging** | `evam_backend_core.logging` | One JSON line/record in prod, pretty locally; every record carries `request_id` / `tenant` / `actor` via contextvars. Use `get_logger(__name__)`. |
| **Errors** | `evam_backend_core.errors` | Typed `AppError` hierarchy (`NotFoundError`, `ConflictError`, `VersionConflictError`, `ValidationAppError`, `Unauthorized/ForbiddenError`) rendered as RFC-9457 `problem+json` with the `request_id`. Never build ad-hoc error dicts. |
| **Correlation** | `evam_backend_core.middleware` | `RequestContextMiddleware` sets/propagates `X-Request-ID`, times every request, logs access lines. |
| **Config** | `evam_backend_core.config` | `BaseServiceSettings` — identity, HTTP, bounded DB pool, timeouts, retry, pagination, CORS, DSN helpers. Subclass it; set your own `env_prefix`. |
| **Database** | `evam_backend_core.db.session` | Bounded async pool + `pool_pre_ping` + `pool_recycle`; every connection gets `statement_timeout` / `lock_timeout` / `idle_in_transaction_session_timeout`. One request = one short transaction (`session_scope`). |
| **Models** | `evam_backend_core.db.base` | `RecordBase` (aka `RegisterBase`): tenant-aware, **version**-column optimistic locking, `created/updated_by/at`, soft-delete. `AuditLog`. Deterministic constraint naming. |
| **CRUD** | `evam_backend_core.crud` | `CRUDRepository` — create/read/list/update/soft-delete/restore with optimistic locking, keyset pagination, whitelisted filters, audit rows, and **auto-append of `stage`/`status` history**. |
| **Pagination** | `evam_backend_core.pagination` | `Page[T]` envelope + keyset cursor helpers. O(1) at any depth. |
| **Retry** | `evam_backend_core.retry` | `RetryableRoute` transparently retries deadlock/serialization (always) and connection errors (reads only) with exponential backoff + jitter. Use `api_router()` and it's automatic. |
| **Routers** | `evam_backend_core.router` | `api_router(**kwargs)` = `APIRouter` with retry bound to every route. **Always use this instead of `APIRouter`.** |
| **Health** | `evam_backend_core.health` | `/healthz` (liveness) + `/readyz` (real DB ping → 503). |
| **App factory** | `evam_backend_core.app` | `create_service_app(...)` wires all of the above in one call. |

---

## Conventions (do this)

1. **Settings** — subclass `BaseServiceSettings`, set `env_prefix="MYSVC_"`, add only
   service-specific fields. Expose a cached `get_settings()`.
2. **Models** — inherit `RecordBase`. Never invent your own PK/tenant/version/audit columns;
   they're inherited. Add tables to one declarative `Base.metadata`.
3. **Routers** — create with `api_router(...)`, never bare `APIRouter(...)`, so retry applies.
4. **CRUD** — build resources with `CRUDRepository(Model, searchable=[...], filterable=[...])`;
   don't hand-roll list/update/delete.
5. **Errors** — raise the typed errors; let the platform render them. Don't catch-and-format.
6. **Logging** — `log = get_logger(__name__)`; log events, not sentences (`log.info("thing_happened", extra={...})`).
7. **Transactions** — one unit of work per request; never hold a transaction across a
   network `await`. Writes use the `version` column (optimistic), not `SELECT ... FOR UPDATE`.
8. **Migrations** — hand-written Alembic against the shared `Base.metadata`; keep RLS
   policies and triggers explicit and reviewable.
9. **App** — assemble with `create_service_app(settings=..., routers=[...])`.

---

## Build a new service in ~40 lines

A complete, runnable reference lives at
[`packages/evam-backend-core/examples/widget_service.py`](packages/evam-backend-core/examples/widget_service.py):

```python
from evam_backend_core.app import create_service_app
from evam_backend_core.config import BaseServiceSettings
from evam_backend_core.crud import CRUDRepository
from evam_backend_core.db.base import Base, RecordBase
from evam_backend_core.pagination import Page
from evam_backend_core.router import api_router
from pydantic import ConfigDict
from pydantic_settings import SettingsConfigDict
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="WIDGETS_")
    app_name: str = "prism-widgets"


class Widget(RecordBase):                       # tenant/version/audit/soft-delete: inherited
    __tablename__ = "widgets"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str | None] = mapped_column(String(40))   # stage/status → auto history


class WidgetRead(...): ...                       # Pydantic schemas (ORM-mode)
class WidgetCreate(...): ...
class WidgetUpdate(...): ...

repo = CRUDRepository(Widget, searchable=["name"], filterable=["status"])
router = api_router(prefix="/v1/widgets", tags=["Widgets"])
# ... wire repo → router (see the Register's crud_router factory for the full pattern) ...

app = create_service_app(settings=Settings(), routers=[router], title="PRISM Widgets")
```

That service already has: JSON logging with correlation ids, the RFC-9457 error contract,
a bounded/timeout-guarded connection pool, optimistic-locking CRUD with audit + history,
keyset pagination, transient-retry, health probes, and CORS — none of which it wrote.

---

## Production guarantees (and how they're met)

- **No lost updates** — `version` optimistic locking → `409 version_conflict`.
- **No deadlock hangs** — `lock_timeout`/`statement_timeout` bound every statement; and
  deadlock/serialization failures are **transparently retried** (`RetryableRoute`).
- **No pool exhaustion** — bounded pool; a burst queues (`pool_timeout`) then errors, never
  wedges Postgres. Keep `pool_size × workers × replicas` under `max_connections`.
- **No silent data loss** — soft-delete + append-only `audit_log`; financials are versioned.
- **Traceable** — every log line and error carries the `request_id`; `X-Request-ID` in/out.

## Not yet in the platform (roadmap)

- **Metrics/tracing** (Prometheus `/metrics` + OpenTelemetry) — planned.
- **Rate limiting** (per-key/tenant `429`) — planned.
- **PgBouncer** guidance for scaling past the shared-DB connection ceiling.

When these land, they land in `evam-backend-core` and every service inherits them.
