# ADR 0001 — Build the Register (source of truth) first

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

Evam Finance runs climate-finance operations largely by hand today (spreadsheets, email).
PRISM digitises this as concentric rings: **Doors → Register → Workflows → Intelligence
(CIPHER / PULSE / VOX / ATLAS)**. Every downstream module reads from and writes to one
central data store. A data-model mistake in that store surfaces 6–9 months later and is the
single most expensive thing to get wrong.

## Decision

We will build the **Register** — the single source of truth over PostgreSQL — as the first
module, and stabilise it before layering workflows and intelligence on top. Everything else
integrates through the Register's API, not around it.

## Consequences

- Downstream verticals can be built in parallel later, each against a stable contract.
- Early effort goes into schema, concurrency-safety and data integrity rather than
  user-facing features — deliberately, because those are the expensive-to-change parts.
- The Register's API is a platform contract: breaking it breaks every vertical, so it is
  versioned (`/v1`) and changed conservatively.
