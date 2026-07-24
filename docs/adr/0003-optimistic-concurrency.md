# ADR 0003 — Optimistic concurrency + idempotency for safety under load

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

The Register must serve many verticals in parallel (ATLAS users, plus event-driven VOX /
CIPHER / PULSE) with **no lost updates and no deadlocks**. Two obvious approaches:
pessimistic row locks (`SELECT … FOR UPDATE`) or optimistic version checks.

## Decision

We will use **optimistic concurrency**: a `version` column via SQLAlchemy's `version_id_col`
so a racing overwrite fails (`StaleDataError` → `409 version_conflict`) instead of silently
winning. Combined with:

- **Idempotency keys** so a retried `POST` never duplicates,
- a **bounded connection pool** + hard `statement_timeout` / `lock_timeout` /
  `idle_in_transaction_session_timeout` so a burst can't exhaust Postgres and a stuck query
  can't hold locks into a deadlock,
- **short, single-transaction requests** with consistent write ordering,
- **advisory-locked** financial-version creation (serialise on a key, not the table).

Pessimistic locks are avoided because they hold locks across the request and invite the
deadlocks we must not have.

## Consequences

- Clients must handle `409` by re-reading and retrying (the `evam-register-client` does this
  and surfaces `VersionConflictError`).
- Correctness under concurrency is proven by `tests/test_concurrency.py`.
- Deadlocks are rare-by-design but not impossible, which is why [ADR-0006](0006-transient-retry.md)
  adds transparent retry.
