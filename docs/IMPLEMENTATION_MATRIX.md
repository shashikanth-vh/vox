# PRISM Master Implementation Matrix

**Baseline:** `prism-vox-M3` (Round-M foundation milestone; see CHANGELOG). This replaces the review-by-review findings log
with one consolidated backlog. It is the single source of "what exists / what's left". Update this
file as part of every change; do not track status in ZIP review replies.

## How to read the Definition-of-Done (DoD) columns

A requirement is **Done** only when every applicable column is satisfied:

- **API** — Register/orchestrator endpoint exists, authorized, contract-tested.
- **WF** — Temporal workflow/activity where the work is orchestrated.
- **Policy** — lifecycle/mandatory/evidence gate in `evam_backend_core` (never per-module).
- **Evidence** — required governance evidence kind + gate defined and verified.
- **Audit** — governed mutation writes an `audit_log` row (§9 of Foundation Spec).
- **RBAC** — operation + record-scope + tenant isolation enforced, with negative tests.
- **UI** — operator screen / work queue (frontend).
- **Tests** — unit + Postgres integration + RBAC-negative + workflow + (journey) E2E.
- **Deploy** — Helm/compose/CI carry it.

Legend: ✅ done · ◑ partial · ✗ not started · — n/a.

---

## Area 1 — Platform foundation

| Requirement | API | WF | Policy | Evidence | Audit | RBAC | UI | Tests | Deploy |
|---|---|---|---|---|---|---|---|---|---|
| Identity / OIDC / signed context | ✅ | — | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |
| RBAC matrix + record scope | ✅ | — | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |
| Tenant isolation (RLS fail-closed) | ✅ | — | ✅ | — | — | ✅ | — | ✅ | ✅ |
| Audit log (governed mutations) | ✅ | — | — | — | ✅ | ✅ | ✗ | ◑ | ✅ |
| Durable decisions + outbox | ✅ | ✅ | ✅ | — | ✅ | ✅ | — | ✅ | ✅ |
| Evidence store + gate + revocation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| Reconciliation | ✅ | — | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |
| **Engagement entity model** (freeze) | ✗ | — | ✗ | — | — | ✗ | ✗ | ✗ | ✗ |
| **Domain-event backbone** | ✗ | — | — | — | ◑ | — | — | ✗ | ✗ |
| **File/object storage durability** | ◑ | — | — | — | — | ◑ | ✗ | ◑ | ◑ |

## Area 2 — Origination

| Requirement | API | WF | Policy | Evidence | Audit | RBAC | UI | Tests | Deploy |
|---|---|---|---|---|---|---|---|---|---|
| VOX touchpoint capture | ✅ | ✅ | ✅ | — | ✅ | ✅ | ◑ | ✅ | ✅ |
| Manual lead creation | ✅ | — | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |
| Company canonical-match / dedup | ✅ | ✅ | — | — | — | ✅ | ✗ | ✅ | ✅ |
| Lead qualification | ✅ | ✅ | ◑ | ◑ | ✅ | ✅ | ✗ | ◑ | ✅ |
| **Qualification gates conversion** | ✗ | ✗ | ✗ | ✗ | — | — | ✗ | ✗ | — |
| Lead → Deal conversion (atomic) | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |

## Area 3 — Deal execution

| Requirement | API | WF | Policy | Evidence | Audit | RBAC | UI | Tests | Deploy |
|---|---|---|---|---|---|---|---|---|---|
| Deal structuring (→ Note Circulated) | ✅ | ✅ | ✅ | ◑ | ✅ | ✅ | ✗ | ◑ | ✅ |
| Document collection | ✅ | ✅ | ✗ | ◑ | ✅ | ✅ | ✗ | ◑ | ✅ |
| **OCR + maker-checker** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **CIPHER appraisal / CMA** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Fraud / adverse-intel review** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Credit Committee decision (durable, fresh-authorized, persist-before-signal, signal-verified) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| **Committee quorum / multi-member** | ✗ | ✗ | ✗ | ✗ | — | ✗ | ✗ | ✗ | — |
| Sanction gate (evidence-verified) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| **Sanction modification / expiry** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | — |

## Area 4 — Disbursement

| Requirement | API | WF | Policy | Evidence | Audit | RBAC | UI | Tests | Deploy |
|---|---|---|---|---|---|---|---|---|---|
| **CP/CS authoritative checklist + maker-checker mints `cp_cs_completion`** | ✅ | ◑ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| **CP/CS waiver + CS-deferment controls (CP/CS split, authority, reason, expiry)** | ✅ | — | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |
| **Handover maker-checker (prepare→approve, distinct authenticated identities)** | ✅ | ✅ | ✅ | — | ✅ | ✅ | ◑ | ✅ | ✅ |
| **Package integrity (required refs, executed_agreement + checklist reconciled, server digest)** | ✅ | — | ✅ | ✅ | ✅ | ✅ | ◑ | ✅ | ✅ |
| Frozen OpenAPI contracts (register/orchestrator/gateway) for the ATLAS/Node team | ✅ | — | — | — | — | — | — | — | ✅ |
| **CP/CS dedicated Temporal workflow (8th) + business-facing orchestrator/gateway op** | ✅ | ✅ | ✅ | — | — | ✅ | ◑ | ✅ | ✅ |
| **Deployed compose E2E (scripts/e2e_smoke.sh + .github/workflows/e2e.yml; real PG + Temporal)** | ✅ | ✅ | — | — | — | — | — | ✅ | ✅ |
| **cp_cs_completion + executed_agreement gate `CP/CS Completed`** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| `Ready for Disbursement` = proposed drawdown fields + row-lock | ✅ | — | ✅ | — | ✅ | ✅ | ✗ | ✅ | ✅ |
| **Durable, immutable handover PACKAGE; stage advances only after snapshot** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ◑ | ✅ | ✅ |
| **Authenticated handover op (`POST /v1/workflows/advaya-handover`, Credit Head/Mgmt)** | ✅ | ✅ | ✅ | — | ✅ | ✅ | ◑ | ✅ | ✅ |
| **`Disbursed` = terminal (no self-disburse)** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| **Dormant Advaya ack path DISABLED by default (flag + no router + no grant + startup guard)** | ✅ | — | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| Advaya ack VERIFIED vs handoff record — enabled only under integration flag | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ | ✅ | ✅ |
| Legacy import mapping (Documentation/Disbursed → new vocabulary) | ✅ | — | — | — | ✅ | — | — | ✅ | ✅ |
| **Real Advaya round-trip / `Disbursement Pending` advance / timeout — NOT PLANNED** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Partial / multiple disbursements** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

## Area 5 — Monitoring

| Requirement | API | WF | Policy | Evidence | Audit | RBAC | UI | Tests | Deploy |
|---|---|---|---|---|---|---|---|---|---|
| Pulse news/intel writer | ✅ | — | — | — | ◑ | ✅ | ✗ | ✅ | ✅ |
| **Covenants / EWS** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Alerts / escalation** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

## Area 6 — Other products

| Requirement | API | WF | Policy | Evidence | Audit | RBAC | UI | Tests | Deploy |
|---|---|---|---|---|---|---|---|---|---|
| Syndication lifecycle | ✅ | ◑ | ✅ | ✗ | ✅ | ✅ | ✗ | ◑ | ✅ |
| Asset monetisation lifecycle | ✅ | ✗ | ✅ | ✗ | ✅ | ✅ | ✗ | ◑ | ✅ |
| **Co-lending / on-lending** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **DCM / lender matching** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

## Area 7 — Experience (UI)

| Requirement | Status | Notes |
|---|---|---|
| Dashboard / Today | ✗ | ATLAS BFF read-side exists (API); no frontend |
| Company 360 | ◑ | dossier API exists; no UI |
| Work queues (per role) | ✗ | reconciliation & committee queues needed |
| Deal workspace | ✗ | |
| Timelines | ◑ | interactions API exists; no UI |
| Admin console | ✗ | |

## Area 8 — Operations

| Requirement | Status | Notes |
|---|---|---|
| Helm charts (all services) | ✅ | subcharts present |
| Compose (local) | ✅ | |
| CI (lint/type/test/image/helm) | ◑ | add migration-upgrade, k8s smoke, E2E, security scans |
| Secrets (external manager) | ✗ | env/secret today |
| Observability (tracing/metrics/logs) | ◑ | structured logs + request-id; no metrics/alert stack |
| Backup / restore / DR | ✗ | |
| HA Postgres / Temporal | ✗ | |
| SBOM / image signing / vuln scan | ✗ | |

## Area 9 — Quality

| Requirement | Status | Notes |
|---|---|---|
| Unit tests | ✅ | ebc + services |
| Postgres integration tests | ✅ | register per-file sweep (live PG) |
| RBAC/tenant negative tests | ✅ | |
| Temporal workflow tests | ◑ | run in CI; skipped where test server absent |
| API contract tests | ◑ | `docs/openapi.json` exists; add contract assertions |
| Migration upgrade tests | ✗ | add downgrade/upgrade round-trip |
| Helm render / k8s smoke | ◑ | render only |
| E2E browser journeys | ✗ | no UI yet |
| Performance / security / penetration | ✗ | |

---

## Release plan (vertical journeys, not horizontal layers)

### Release 1 — End-to-end lending journey (Gate 2)
Deliver ONE real loan start→finish, including reject/correct/retry:
`VOX/Manual → Company+Lead → Qualification → Deal Structuring → Document Collection → OCR+Maker-Checker
→ CIPHER → Fraud/Pulse → Credit Committee → Sanction → CP/CS Completed → Ready for Disbursement →
Disbursed` (PRISM's honest terminal; `Disbursement Pending` + Monitoring require a future
Advaya integration).
Ordered work (dependency-first):
1. Freeze Engagement model + Advaya boundary contract (Foundation Spec §1, §11).
2. Orchestrator + Gateway endpoints for LeadQualification / DealStructuring / DocumentCollection;
   persist committee decision under fresh Access auth before signalling; committee quorum model.
3. Evidence kinds + gates for qualification→conversion, OCR maker-checker, CIPHER,
   cp_cs_completion+executed_agreement→`CP/CS Completed`, verified advaya-ack→`Disbursement Pending`.
4. OCR + CIPHER + fraud intelligence activities (Intelligence workstream).
5. Minimal operator UI for the queues this journey needs (committee, maker-checker, work queue).
6. Journey E2E test with the reject/correct/retry branches from the Scenario Catalogue.

### Release 2 — Product expansion (Gate 3)
Reuse the frozen foundation for Syndication, Co-lending/on-lending, Asset monetisation, DCM.

### Release 3 — Production hardening (Gate 4)
HA Postgres/Temporal, object-storage durability, external secret manager, trusted TLS,
metrics/alerting, backup/restore+DR, SBOM/signing/scanning, performance+pen+failure testing.

## Parallel workstreams
Foundation · Business workflows · Intelligence (VOX/Pulse/CIPHER/OCR) · Product UI · Platform/QA.
Integration is continuous through the frozen API contracts (`docs/openapi.json`) and shared test
fixtures.

## Review gates (replace per-ZIP reviews)
- **Gate 1 — Foundation frozen:** this doc + `FOUNDATION_SPEC.md` accepted.
- **Gate 2 — Lending journey complete:** R1 DoD met, E2E demo incl. failure branches.
- **Gate 3 — Full product scope:** R2 complete.
- **Gate 4 — Production readiness:** R3 complete.
Findings inside a milestone go to this backlog and are fixed together — not via a new ZIP each.
