# ADR 0002 — Entity-centric, tenant-aware, versioned schema

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

The prototype data (ATLAS MIS) is deal-centric: the same company appears in Leads, Deals,
Lending, Syndication and Asset-Monetisation sheets, duplicated and drifting. Credit
decisions need one coherent view of a *company* over time, across products, with a faithful
history of what data drove which decision.

## Decision

Every record hangs off a single **`entities`** row (one company appears once). Every table
is:

- **tenant-aware** — `tenant_id` on every row (Evam today; co-lenders/DSAs/OEMs later),
- **versioned** — a `version` column for optimistic concurrency, and, for financials, an
  append-only `version_no` provenance chain,
- **auditable** — `created/updated_by/at`, soft-delete, and an `audit_log`.

These are implemented once in `evam_backend_core.db.base.RecordBase` so every table inherits
them.

## Consequences

- Cross-product questions ("everything about this company") are a natural query (the
  dossier); the deal grid joins through `entity_id`.
- The schema is wider and stricter than the source spreadsheets — deliberately, to end the
  duplication/drift.
- Because provenance is preserved (soft-delete + financial versions + audit), nothing in the
  source of truth is silently lost — a regulatory/credit-audit requirement.
