# ADR 0005 — Self-hosted PostgreSQL (no Bitnami); managed later by config

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

The platform needs a database for local dev, and eventually production. Bitnami's PostgreSQL
Helm chart is a common default but is now a paid/subscription path. It was also undecided
whether production would use a managed service (RDS/Cloud SQL) or self-hosted.

## Decision

Use the **free official `postgres` image** everywhere (Docker Compose and a hand-written
Helm subchart under the `prism` umbrella) — **no Bitnami**. Managed vs self-hosted is **not
baked in**: the Register connects via `database.*` settings, so switching to a managed,
India-resident Postgres later is a config change (`postgresql.enabled=false` + host/secret),
not a rebuild.

## Consequences

- No licensing cost or dependency on a third-party chart's conventions.
- One shared Postgres is the platform database (every module connects to it), sized so
  `pool_size × workers × replicas` stays under `max_connections`.
- Production hardening (PgBouncer, read replicas, managed host) is deferred and additive.
