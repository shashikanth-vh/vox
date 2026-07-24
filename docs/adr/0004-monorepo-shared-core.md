# ADR 0004 — Monorepo with a shared backend platform + Register client

- **Status:** Accepted
- **Date:** 2026-07-25

## Context

PRISM is explicitly multi-vertical (CIPHER, PULSE, VOX, ATLAS, gateways) — several backend
services, all integrating with the Register. Without shared foundations, each would
re-implement logging, error shape, DB pooling, tenancy, pagination and retry, and drift
apart — worst of all *against the one system that must stay consistent*.

## Decision

A **monorepo** with two shared packages, and services that compose them:

- **`packages/evam-backend-core`** — build a service on it (logging, RFC-9457 errors,
  request correlation, bounded pool + timeouts + retry, optimistic-locking CRUD, keyset
  pagination, health, app factory). The Register is its reference implementation.
- **`packages/evam-register-client`** — call the Register from any vertical (auth,
  idempotency, optimistic concurrency, retry, correlation, typed errors). No vertical
  hand-rolls HTTP against the Register.
- **`services/*`** — deployable services; **`packages/*`** — shared libraries.

We extract to shared packages *now*, with one service, because the architecture already
guarantees more consumers — this is not speculative ("rule of three" is satisfied by the
documented verticals).

## Consequences

- Every vertical speaks the Register's contract identically; cross-cutting fixes land once.
- The Docker build context is the repo root so an image can bundle the packages it needs.
- A new service is `make new-service NAME=…` + define models/resources — everything
  production-grade is inherited.
- Trade-off accepted: slightly more build/layout ceremony than a single package, in exchange
  for consistency across the platform.
