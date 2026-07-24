# ADR 0006 — Transparent retry of transient DB failures

- **Status:** Accepted
- **Date:** 2026-07-25

## Context

Even with [ADR-0003](0003-optimistic-concurrency.md), some database errors are transient and
*not* the caller's fault: a deadlock (`40P01`) or serialization failure (`40001`) — which
PostgreSQL has already rolled back — or a dropped connection after a failover. Surfacing
these as `500`s pushes retry logic into every caller, and event-driven verticals (VOX/PULSE)
retry anyway.

## Decision

Retry transparently at two layers:

- **Server** (`evam_backend_core.retry.RetryableRoute`, bound to every route via
  `api_router`): re-run the whole request on a fresh transaction for rollback-safe errors
  (deadlock/serialization) always, and for connection errors on **read** methods only (a
  write may have committed before the socket died). Exponential backoff + jitter.
- **Client** (`evam-register-client`): retry network/timeout/`429`/`5xx` with backoff,
  honouring `Retry-After`; retry writes only when they carry an `Idempotency-Key` or
  `If-Match`, so a replay can't duplicate or clobber.

## Consequences

- Rare, transient hiccups self-heal instead of failing the caller.
- Retries are safe because rollback-safe errors committed nothing, and writes are only
  retried when idempotent — this is a deliberate, conservative policy.
- Not covered yet (roadmap): metrics to observe retry rates. See `BACKEND_STANDARDS.md`.
