# PRISM Scenario Catalogue

**Baseline:** `prism-vox-M3` (Round-M foundation milestone; see CHANGELOG). The reusable, thorough test scenario set —
happy paths **and** the failure/governance/concurrency paths that matter. Every Release must green
its applicable rows. This is the acceptance backbone behind the review gates.

Coverage legend: ✅ automated & passing · ◑ partial (some branches / not E2E) · ✗ not covered.
"Where" points at the current test (or the workstream that will own it).

---

## A. Business scenarios

| # | Scenario | Coverage | Where / owner |
|---|---|---|---|
| A1 | New company + new lead (VOX) | ✅ | workflows/test_vox_e2e |
| A2 | Existing company links active lead (canonical match) | ✅ | workflows/test_vox_e2e |
| A3 | Manual lead creation | ✅ | register/test_crud, test_rbac |
| A4 | Duplicate company/lead resolution | ◑ | test_vox_e2e (company dedup ✅; lead dedup ◑) |
| A5 | Duplicate VOX capture retry (idempotent) | ✅ | test_vox_e2e |
| A6 | Multiple deals under one engagement | ✗ | needs Engagement model (Foundation §1) |
| A7 | Qualification pass → structuring | ◑ | workflows/test_business_workflows (WF only) |
| A8 | Qualification fail (records reason, stops) | ◑ | test_business_workflows |
| A9 | Qualification refer-back | ✗ | Origination workstream |
| A10 | Missing mandatory data at a stage | ✅ | register/test_policy_enforcement |
| A11 | Invalid / conflicting documents | ✗ | Deal-execution (OCR) |
| A12 | OCR correction + maker-checker reject | ✗ | Intelligence workstream |
| A13 | CIPHER success / low-confidence / provider-unavailable fallback | ✗ | Intelligence workstream |
| A14 | Committee approve | ✅ | test_business_workflows, register/test_evidence |
| A15 | Committee reject (no sanction, rejection evidence) | ✅ | test_business_workflows |
| A16 | Committee conditional-approve / abstain / no-quorum | ✗ | needs quorum model (Foundation §7) |
| A17 | Sanction blocked without evidence | ✅ | register/test_evidence |
| A18 | Sanction modification / expiry | ✗ | Deal-execution |
| A19 | CP/CS incomplete / waived / deferred | ✅ | CP/CS split + waiver (senior authority + reason) + CS-deferment (CP-only, reason + expiry); ≥1 item required (register/test_cpcs) |
| A20 | CP/CS authoritative checklist + maker-checker; executed docs before handover | ✅ | `cp_cs_completion` minted only from an APPROVED maker-checker checklist (register/test_cpcs); gate wired at `CP/CS Completed` (test_policy_enforcement) |
| A21 | Advaya handover — two-person maker-checker; package integrity; PRISM never self-disburses | ✅ | two-phase prepare→approve with DISTINCT authenticated identities, package refs reconciled vs executed_agreement + CP/CS checklist, server-side digest, stage advances only on checker approval (register/test_handover, workflows/test_business_workflows); dormant ack path DISABLED by default (test_handover, test_evidence). Real round-trip/timeout: NOT PLANNED |
| A22 | Partial + multiple disbursements | ✗ | Disbursement |
| A23 | Covenant breach → EWS escalation | ✗ | Monitoring |
| A24 | Ordered-pipeline enforcement (no stage skip) | ✅ | register/test_policy_enforcement |
| A25 | Import retains incomplete row → reconciliation | ✅ | register/test_import |

## B. Technical & governance scenarios

| # | Scenario | Coverage | Where / owner |
|---|---|---|---|
| B1 | Tenant isolation (app + RLS at DB) | ✅ | register/test_rls, test_decisions (direct-RLS) |
| B2 | Unauthorized role transition refused | ✅ | register/test_rbac_writes, test_policy_enforcement |
| B3 | Scoped caller can't reach out-of-scope row (list+GET) | ✅ | register/test_lead_scoping, test_rbac |
| B4 | Revoked authority while workflow waits | ✗ | needs fresh-Access re-check at decision time |
| B5 | Duplicate API call (idempotency key) | ✅ | register/test_retry, test_crud |
| B6 | Duplicate / spoofed Temporal signal | ✅ | Lead Conversion + committee signal both verify against the durable record (spoofed committee signal ignored → TimedOut); workflows/test_business_workflows |
| B7 | Concurrent approval attempts → one winner | ✅ | register/test_decisions |
| B8 | Concurrent reconciliation resolution (serialised) | ✅ | register/test_import |
| B9 | Service restart mid-processing (durable retry) | ◑ | decision outbox tested; broad restart ✗ |
| B10 | Postgres / Temporal outage → reconcile, no loss | ◑ | decision reconciler tested; infra-level ✗ |
| B11 | Reconciliation Required vs Waived exclusion | ✅ | register/test_import |
| B12 | Evidence: unauthorized attach (RM/Analyst/service) | ✅ | register/test_evidence |
| B13 | Evidence: unverified/invented provenance refused | ✅ | register/test_evidence |
| B14 | Evidence: revocation + supersession integrity | ✅ | register/test_evidence |
| B15 | Evidence immutable at DB (UPDATE/DELETE blocked) | ✅ | register/test_evidence |
| B16 | Break-glass evidence override (audited, senior-only) | ✅ | register/test_evidence |
| B17 | Expired tokens / secret rotation | ✗ | Platform/QA |
| B18 | Audit completeness for governed mutations | ◑ | per-action asserts exist; no global audit sweep |
| B19 | Backup / restore + DR | ✗ | Operations |
| B20 | Migration upgrade/downgrade round-trip | ◑ | conftest migrates each session; no explicit round-trip test |

---

## Cross-cutting acceptance rules (apply to every scenario)

1. **Negative-first:** each governed action needs at least one refusal test (wrong role, wrong
   tenant, out-of-scope subject, missing evidence, stale version).
2. **Idempotency:** any externally-triggered write is retried in the test and asserted to produce
   one effect.
3. **Concurrency:** any shared-subject mutation has a simultaneous-actor test asserting a
   deterministic outcome (no lost update, no deadlock).
4. **Audit:** the governed-write scenarios assert an `audit_log` row with the expected action.
5. **Journey branches:** the Release-1 E2E must include the reject, correct, and retry branches —
   not only the happy path.

## Immediate gaps to close for Release 1 (from A/B above)
A6, A7-A9 (API+UI), A11-A13, A16, A18-A19, A21 (timeout branch)-A22, B4, B17-B20. These map 1:1 to
the ✗/◑ rows in `IMPLEMENTATION_MATRIX.md`.
