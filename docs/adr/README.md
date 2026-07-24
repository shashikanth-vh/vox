# Architecture Decision Records

Short, immutable notes on **why** PRISM is built the way it is. New engineers: read these
before proposing a change that revisits one of these decisions — the trade-offs are
already captured, so you either build on them or write a new ADR that supersedes one.

Copy [`0000-template.md`](0000-template.md) for a new record. Number sequentially. An ADR
is never edited after it's accepted (except status) — supersede it with a new one instead.

| # | Decision | Status |
|---|---|---|
| [0001](0001-register-first.md) | Build the Register (source of truth) first | Accepted |
| [0002](0002-entity-centric-versioned-schema.md) | Entity-centric, tenant-aware, versioned schema | Accepted |
| [0003](0003-optimistic-concurrency.md) | Optimistic concurrency + idempotency for safety under load | Accepted |
| [0004](0004-monorepo-shared-core.md) | Monorepo with a shared backend platform + Register client | Accepted |
| [0005](0005-postgresql-self-hosted.md) | Self-hosted PostgreSQL (no Bitnami); managed later by config | Accepted |
| [0006](0006-transient-retry.md) | Transparent retry of transient DB failures | Accepted |
