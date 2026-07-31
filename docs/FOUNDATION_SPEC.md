# PRISM Foundation Specification (Gate 1 — Frozen Contracts)

**Status:** baseline = `prism-vox-M3` (Round-M foundation milestone; see CHANGELOG). This document freezes the shared
contracts every module must reuse. It is **descriptive of the code as built**, not aspirational:
each section cites the authoritative module and its tests. Where a contract is only partially
implemented it is marked **⚠ GAP** with a pointer to the Implementation Matrix.

Change control: a change to any FROZEN contract requires updating this document, the authoritative
module, and its tests **in the same change**. Modules must not re-implement or fork these rules.

---

## 0. System shape

Services (`services/`): `register` (system of record, Postgres+RLS), `access` (RBAC/identity),
`gateway` (front door, OIDC, credential injection), `workflows` (Temporal worker + orchestrator
API), `vocx` (VOX capture), `pulse` (news/intel), `atlas` (read-side BFF).
Shared libraries (`packages/`): `evam-backend-core` (the **single** policy/RBAC/evidence/decision
authority), `evam-register-client` (typed Register client used by every service).

Golden rule: **all write-time governance lives in `evam_backend_core`** and is invoked through
`policy.check_write(...)`. No service re-implements lifecycle, mandatory-field, lock, or evidence
rules. The Register is the enforcement backstop even when a workflow is the intended path.

---

## 1. Canonical entities and relationships — **FROZEN**

Authoritative subject registry: `services/register/app/repositories/subjects.py::SUBJECTS`.
Subject-type strings below are the **API contract** (not ORM class names); the policy engine keys
on them.

```
Company (Entity)                      the company / counterparty of record
 └─ Engagement*                       (conceptual grouping of a company's deals; see GAP)
     ├─ Lead            subject="Lead"                origination record
     ├─ Deal            subject="Deal"                the credit deal (parent of product lines)
     │   ├─ Lending           subject="Lending"             term-loan product line
     │   ├─ Syndication       subject="Syndication"         syndication product line
     │   └─ AssetMonetisation subject="AssetMonetisation"   asset-monetisation product line
     └─ Interactions, Documents, Financials, Intel, Monitoring (child records)
```

Denormalised links (`subjects.py::derive_links`): every line carries `entity_id`; product lines
carry `deal_id`. A Lead links to `entity_id`; conversion (`POST /v1/leads/{id}/convert`) creates
the Deal + product lines atomically and marks the Lead `Converted`.

- **Engagement — FROZEN decision: the Deal IS the engagement unit; no separate table in R1.** A
  Company's relationship is the set of its Deals (each a distinct credit engagement, parent of its
  product lines). We do **not** introduce a separate `Engagement` entity for Release 1 because
  every requirement the reviewer listed (multiple deals under one company, per-deal lifecycle,
  scoping, evidence, audit) is already served by Deal-under-Company: multiple Deals may attach to
  one Entity, each with its own lifecycle and product lines. "Engagement" in reports/UI = a grouped
  view of a Company's Deals, computed on read — not a stored row. Rationale: a separate table adds
  a join, an ownership/scoping surface and a migration with no behaviour R1 needs. **Revisit only
  if** a future requirement needs cross-Deal state that cannot live on the Company or the Deal
  (e.g. a shared limit/exposure envelope spanning deals) — at which point `Engagement` becomes a
  thin parent of Deals. Matrix: *Platform foundation → Engagement model* is marked resolved-by-decision.

Tenancy: every business row has `tenant_id`; isolation is enforced by Postgres RLS
(`current_setting('app.current_tenant')`), fail-closed, `FORCE`d in production
(`REGISTER_ENFORCE_RLS`). See §3.

---

## 2. Lifecycle states and allowed transitions — **FROZEN**

Authoritative: `evam_backend_core/rbac.py` — `STAGE_VOCAB` (vocabulary), `INITIAL_STATUS`
(entry-stage allowlist), `ALLOWED_TRANSITIONS` (ordered graph). Enforced by
`policy.check_write` for **every** writer (direct PATCH, change-request approval, creation, import,
workflow activity). Tests: `services/register/tests/test_policy_enforcement.py`.

The lifecycle field per subject: Lead→`status`, Deal→`stage`, Lending→`stage`,
Syndication→`status`, AssetMonetisation→`status` (`policy._STAGE_FIELD`).

### Lead (`status`)
Entry: `Active | On Hold | Dropped`. Graph: `Active↔On Hold`, `Active/On Hold→Dropped`,
`Dropped→Active`. `Converted` is **terminal and reachable only via `/convert`** (never a bare
PATCH — for humans or machines).

### Deal (`stage`) — the COMMERCIAL origination funnel
Entry: `New Inquiry | In Screening | In Pipeline | On Hold` (terminals are outcomes, never a birth
state). Graph: forward one step (`New Inquiry → In Screening → In Pipeline → Closed Won/Closed
Lost`), back one step for rework, `On Hold` ↔ any working stage, `Screened Out` re-openable to
`In Screening`; the CLOSED terminals are final. **A deal carries NO credit lifecycle** — the
funnel measures origination, and every credit control below runs on the LENDING line (this is
the release baseline schema; there is no deal-level credit-stage column).

### Lending (`stage`) — the credit pipeline
Entry: `Data Awaited | Diligence`.
```
Data Awaited → Diligence → Note Circulated → Sanctioned → CP/CS Completed →
    Ready for Disbursement → Disbursed   (TERMINAL for the current product scope)
             ↘ (refer-back one step) ↖              (On Hold ↔ from any working stage)
Rejected ← from Data Awaited/Diligence/Note Circulated ;  Rejected → Data Awaited/Diligence
```
`Disbursed` is PRISM's TERMINAL: the last state it can assert on its own authority (there
is no Advaya integration; see §11). The onward disbursement states (`Accepted by Advaya` →
`Disbursement Pending` → `Disbursed`) exist ONLY under a future Advaya integration and enter the
vocabulary only when that mode is enabled — so PRISM never self-disburses.

Governance stages and their gates (see §5, §6):
- **Sanctioned** — on the LENDING line ; requires evidence
  `credit_committee_approval` + `sanction_letter`.
- **CP/CS Completed** (Lending) — requires evidence `cp_cs_completion` (minted from an APPROVED
  maker-checker CP/CS checklist, §6) + `executed_agreement`.
- **Ready for Disbursement** (Lending) — mandatory fields `proposed_disbursement_amount,
  proposed_disbursement_date` ; row-locked (Credit Head/Management/Admin only).
- **Disbursed** (Lending) — row-locked (Credit Head/Management/Admin only). Entered by
  the handover operation, which creates the durable, immutable handover PACKAGE and advances the
  stage transactionally (§11). PRISM's terminal.

### Syndication (`status`)
Entry: `Deal Sourced | Docs Pending | IM in Prep`. Ordered:
`Deal Sourced → Docs Pending → IM in Prep → IM Circulated → Queries Received → IP Received →
Sanctioned → Disbursed`. Terminal: `Rejected | Dropped | Withdrawn`. `On Hold` re-enters any
working stage. Field-lock: `amount_cr` frozen at `Sanctioned` (Syn Head/Management).
**⚠ GAP** — no evidence gate yet (Matrix: *Other products → Syndication evidence*).

### AssetMonetisation (`status`)
Entry: `In Discussion | Teaser Prepared | Teaser Shared`. Ordered:
`Teaser Prepared → Teaser Shared → In Discussion → NBO Received → BO Received → SPA / Documentation
→ Closed`. Terminal: `Closed | Dropped`. **⚠ GAP** — no evidence gate yet.

Rules that bind **every** transition:
1. Unknown/free-text values are rejected (`stage_vocab_error`).
2. First set from NULL obeys the **entry allowlist** (can't be born at a governance stage).
3. Each hop must be in `ALLOWED_TRANSITIONS` (fail-closed on unknown current state).
4. Mandatory fields (§ below) then evidence (§6) then locks (§4) apply.

---

## 3. RBAC, tenant & record-level authorization — **FROZEN**

**Authority model (release 1).** ATLAS (`ATLAS_RBAC_v3.1.xlsx`) is the approved
DESIGN-TIME policy. **PostgreSQL (`access_grants`) is the RUNTIME authority for human
access**: Access resolves it once per user, the Gateway issues a short-lived SIGNED
authorization context (claims: issuer/audience, iat/exp, kid, tenant, identity, roles,
live effective matrices, `matrix_version`, `policy_version`, revocation `epoch`,
method+path binding), and downstream services verify and enforce THAT context. Code
retains the non-editable pieces: the catalog (`rbac_catalog.py`, incl. `POLICY_VERSION`),
service-principal capabilities (`service_policy.py`), lifecycle policy (`lifecycle.py`)
and the evaluation algorithms. The compiled baseline in `rbac.py` is a versioned
reference for the EXPLICIT seed (`python -m app.seed`) and the DRIFT REPORT
(`--check` / `GET /v1/access/drift`) — it never decides a production request. Grants
carry provenance (`baseline` vs `override`); every governance change (role
grant/revoke, (de)activation, cell edit, seed) lands on the append-only `access_audit`
trail stamped with the policy version.

**Revocation window.** The signed context is an authorization CREDENTIAL: its TTL
(gateway default 120s; resolve cache 60s, last-known-good bounded at 300s then FAIL
CLOSED) is the deliberate revocation window. Role changes and deactivation bump the
user's `epoch`. SENSITIVE operations — delete/restore, assignment changes, governed
imports, evidence break-glass (and the orchestrator's decisions, which fresh-authorize
already) — additionally revalidate ONLINE against Access when
`REGISTER_ONLINE_REVALIDATION` is on (the production posture): user still active,
operation still granted, epoch unchanged; Access unreachable → 503, fail closed.

Compiled matrix: `evam_backend_core/rbac.py` — `VIEW_ACCESS`, `OPERATIONS`,
`ASSIGNMENT_AUTHORITY`, `APPROVER_FOR_SUBJECT`, `WRITE/CREATE_OPERATION_FOR_SUBJECT`
(+ re-exported catalog/service/lifecycle modules).
Engine: `services/register/app/authz/` (`enforce_operation`, `view_access`, `build_scope`,
`row_in_scope`, `can_write_row`). Access levels: `FULL | SCOPED | READ | APPROVE | NONE`.

Three enforcement layers, all fail-closed:
1. **Identity** — gateway verifies OIDC and injects a signed internal context; the Register trusts
   only the signed context (`X-Internal-Context`) or, in dev, headers. A leaked generic key is
   fail-closed under `REGISTER_ENFORCE_RBAC`.
2. **Operation** — every write maps to an operation; a **named service** (`svc_*`) may perform only
   its `SERVICE_GRANTS` allowlist (least privilege), regardless of `enforce_rbac`.
3. **Record scope** — a `SCOPED` grant is narrowed to rows in the caller's scope (assignments,
   owned rows, entity scope, default-owner). Enforced on list **and** direct GET **and** writes.

Human roles: `Admin, Management, BD Head, BDRM, Credit Head, Deal Analyst, Syn Head, Syn RM,
AM Head, AM RM`. Service principals: `svc_pulse, svc_vox, svc_workflows, svc_atlas, svc_gateway`.
Tests: `test_rbac*.py`, `test_lead_scoping.py`, `test_rls.py`, `test_internal_context.py`.

---

## 4. Field/row locks — **FROZEN**

Authoritative: `policy.FIELD_LOCKS` (edit-at-stage) + `rbac.ROW_LOCKS` (move-into-locked-value).
- Row locks: Lead→`Converted` (Admin/BD Head/Management), Lending→`Ready for Disbursement` /
  `Disbursed` (Admin/Credit Head/Management).
- Field locks: Lending `rm` frozen at `Sanctioned` (Management); Syndication `amount_cr` frozen at
  `Sanctioned` (Syn Head/Management). Admin is always break-glass. Machine callers (`roles=None`)
  are bound by their service allowlist, not these role locks.

---

## 5. Mandatory fields to enter a stage — **FROZEN**

Authoritative: `policy.MANDATORY_FOR_STAGE`. Lending
`Ready for Disbursement`→`{proposed_disbursement_amount, proposed_disbursement_date}` (also required
at `Disbursed`). The ACTUAL `disbursed_amount`/`disbursement_date` are reserved for a
real disbursement confirmation (future Advaya integration). Checked against the row **merged** with the
change, so a field already present satisfies it. Every workflow round extends its product line here.

---

## 6. Evidence: types, provenance, revocation & supersession — **FROZEN**

Authoritative registry: `evam_backend_core/evidence.py::EVIDENCE_KINDS` (+ `spec_for_kind`).
Gate map: `policy.EVIDENCE_FOR_STAGE`. Store: `governance_evidence` (immutable) +
`governance_evidence_status` (append-only validity ledger) — migrations 0011/0012/0013.
API: `services/register/app/api/evidence.py`. Tests: `test_evidence.py`.

Contract:
- **Controlled vocabulary.** Only registered kinds (or the `document:<name>` prefix) are accepted.
- **Authorised by kind.** Each kind names an RBAC operation (`attach_committee_evidence`,
  `attach_sanction_evidence`, `attach_document_evidence`, `attach_qualification_evidence`). An
  RM/Analyst/unrelated service cannot attach committee/sanction evidence.
- **Subject + scope.** Subject must exist, be a type the kind allows; a SCOPED caller must have it
  in scope. Applied uniformly to attach/list/revoke/supersede.
- **Governance kinds require a digest.** `sha256` mandatory for `governance=True` kinds.
- **Provenance is VERIFIED for committee/sanction kinds.** The caller cites a `decision_ref`; the
  Register resolves it against a durable **single-winner committee decision** (§7) and requires
  matching outcome + tenant + subject + committee authority, then **generates** provenance
  (workflow/run/decider) from the record. One evidence row per `(decision, kind)` (unique index).
- **Immutable but revocable.** Rows are write-once (DB trigger). A mistaken record is neutralised
  by appending `Revoked | Invalidated | Superseded` to the status ledger; the policy loader
  (`app/core/evidence.py::load_evidence_kinds`) counts only currently-valid evidence. Supersession
  requires same tenant + subject + kind, prior currently valid.
- **Break-glass.** A missing-evidence gate is passable only by an audited Admin/Management
  `X-Evidence-Break-Glass` override, logged as `evidence.break_glass`.

Registered kinds today: `credit_committee_approval`, `credit_committee_rejection`,
`sanction_letter`, `executed_agreement`, `cp_cs_completion`, `advaya_acknowledgement`,
`credit_note`, `lead_qualification(_failed)`.
Gates today: Lending `Sanctioned` → committee approval + sanction letter; Lending
`CP/CS Completed` → **`cp_cs_completion` (minted from an APPROVED maker-checker CP/CS checklist —
`cp_cs_checklists`, verified in `evidence.py::_verify_cpcs_checklist`, no longer caller-attached) +
`executed_agreement`**. The onward `advaya_acknowledgement` gate exists only under a future Advaya
integration (§11 — so PRISM rests at `Disbursed` and never self-disburses).
**⚠ GAP** — kinds/gates for OCR maker-checker, CIPHER, CMA, fraud review, and the
qualification→conversion gate are **not yet wired**; syndication/asset-monetisation have no evidence
gate. Real document digests (vs. hashing the reference) — GAP. Matrix: *Deal execution /
Disbursement / Other products*.

---

## 7. Durable approval & committee-decision model — **FROZEN**

Authoritative: `services/register/app/api/decisions.py` + `app/models/decisions.py`
(`WorkflowDecision`, `WorkflowDecisionOutbox`). Tests: `test_decisions.py`.
- **Single-winner.** `UNIQUE(tenant_id, workflow_id)`: the first decision wins; the same outcome
  replays idempotently; the opposite is refused (409) even after the workflow completed.
- **Server-set provenance.** `decided_by`, roles and grants come from the verified delegated
  approver in the signed internal context — never a request body field. Restricted to
  `svc_workflows`.
- **Two decision kinds.** `lead_conversion` (bound by `lead_id`) and `committee` (bound to
  `subject_type`+`subject_id`, requires committee authority Credit Head/Management/Admin). Committee
  decisions back committee/sanction evidence (§6).
- **Transactional outbox + reconciler.** Recording a decision creates a `pending` outbox row in the
  same transaction; a background reconciler drives it to `applied`/`dead` with fencing tokens, so an
  accepted decision is never lost across worker/Register outages.
- **Immutability.** The decision row is immutable at the DB (migration 0007).

The **orchestrator persists a committee decision under fresh Access authority (Credit Head/
Management/Admin, re-checked at decision time) BEFORE signalling** the Deal-Structuring workflow
(`POST /orchestrator/v1/workflows/{id}/committee-decision`): single-winner + subject-bound. The
**workflow's `committee_decision` signal is a WAKE-UP ONLY** — the run calls
`verify_committee_decision` which re-reads the authoritative record and derives the outcome,
approver, note AND references from it, rejecting a missing / cross-subject record. So a direct
Temporal signal carries no trusted outcome and is ignored (the run keeps waiting → TimedOut). The
committee/sanction evidence is then verified against the same record. Tests: `test_business_workflows`
(spoofed-signal-ignored), `test_evidence` (provenance verification).

**⚠ GAP — committee quorum / multi-member voting / conditional-approve / abstain / no-quorum.** The
model still records a single committee outcome; it does not yet model member votes or quorum (a
signed per-member committee token feeding a quorum tally). Matrix: *Deal execution → Committee
quorum / multi-member*.

---

## 8. Reconciliation handling — **FROZEN**

Authoritative: `app/core/reconciliation.py` (exclusion predicate), `app/api/reconciliation.py`
(Admin/Management workflow), `app/models/reconciliation.py`, migration 0010. Tests: within
`test_import.py`.
- A governed import may **retain** a row missing its stage's mandatory data; each opens a durable
  `import_reconciliation_items` record and flags the subject `reconciliation_status`.
- **Three operational classes:** `NULL` (complete, visible), `Required` (hidden), `Waived` (a
  deliberate senior exception — **also hidden** from routine reads/exports/counts). Centralised
  predicate hides every still-flagged record; only an explicit Admin/Management inclusion surfaces
  them. Applied to list, GET, exports, counts.
- **Concurrency-safe resolution:** locks all open items for `(tenant, subject_type, subject_id)`
  FOR UPDATE in id order, then the subject row, recomputes the flag from the settled set;
  If-Match optimistic version. **Waiver requires Management** and a ticket. Audited.

---

## 9. Audit / event / outbox conventions — **FROZEN**

- **Audit:** every governed mutation writes an `audit_log` row (`actor`, `action`, `resource_type`,
  `resource_id`, `request_id`, `changes`). Actions in use include `reconciliation.resolve`,
  `evidence.attach`, `evidence.revoke`, `evidence.break_glass`. New governed actions MUST follow
  this shape.
- **Outbox:** durable side-effects use the transactional-outbox pattern (decisions today);
  reconciled by a fenced background claimer. New async effects reuse this, not ad-hoc dispatch.
- **⚠ GAP** — no general domain-event stream/bus beyond the decision outbox. Matrix: *Platform
  foundation → Event backbone* (decide before Monitoring/EWS).

---

## 10. Workflow idempotency & retry rules — **FROZEN**

Authoritative: `services/workflows/app/workflows.py`, `activities.py`.
- **Idempotency key = workflow-derived** (`wf:{workflow_id}:{step}`), so a retried activity or a
  replayed workflow never duplicates a Register write — exactly-once effect.
- **Retry policy:** ordinary activities use bounded retry (5 attempts); **decision-critical** ones
  (verify/convert/record-outcome/attach-evidence) use `_DURABLE` unbounded retry with capped
  backoff, so an accepted decision is reconciled through a Register outage, never dropped.
- **Untrusted signals:** approve/reject signals are queued and **verified** against the durable
  decision record before they count. Lead Conversion AND the Deal-Structuring committee signal both
  do this: the committee signal is a **wake-up only**, and the workflow derives the outcome from the
  authoritative persisted decision (`verify_committee_decision`) — a spoofed/direct signal without a
  matching record is ignored and the run keeps waiting (→ TimedOut).
- Determinism: no `Date.now`/random in workflow code; all I/O in activities.

---

## 11. PRISM–Advaya boundary — **FROZEN (contract); integration NOT planned**

Advaya is the downstream disbursement / loan-management system. PRISM owns
origination→sanction→CP/CS and **hands the facility OVER to Advaya**. There is **no Advaya
integration planned at this time**, so PRISM's honest TERMINAL is `Disbursed`: the last
state it can assert on its own authority. PRISM does **not** mark a loan disbursed on its own — the
`advaya_handoffs` record + verified-ack machinery below is retained, **dormant**, as the ready hook
for a future integration.

Frozen contract:
- **Ownership boundary.** PRISM is the system of record up to and including `Disbursed`;
  Advaya owns disbursement and post-disbursement servicing. PRISM never marks a loan disbursed on its
  own authority.
- **The durable handover PACKAGE + two-phase maker-checker.** Advancing the stage alone would prove
  only that *someone changed the stage*, not *what was handed over* — and one person must not both
  initiate and authorise it. So the handover is TWO PHASES on an `advaya_handover_packages` row
  (migration 0016):
    - **Prepare** (`POST /v1/internal/handover-packages`, register `handover.py`). The MAKER drafts a
      **Prepared** package. The Register snapshots the authoritative facility + proposed drawdown
      amount/date (from the Lending row, never the caller), REQUIRES executed-document refs + delivery
      method + recipient, RECONCILES the executed-doc refs against the on-file `executed_agreement`
      digest and the CP/CS checklist version against the approved checklist that minted
      `cp_cs_completion`, GENERATES the package manifest and computes its digest **server-side**, and
      stores it — WITHOUT advancing the stage. The maker's identity is the authenticated caller.
    - **Approve** (`POST /v1/internal/handover-packages/{lending_id}/approve`). A DIFFERENT CHECKER
      (authenticated) approves; the Register REQUIRES the checker's user id to differ from the maker's,
      sets the package `HandedOver` (freezing it), and ONLY THEN advances the stage — one transaction.
  A trigger keeps the row mutable only while `Prepared`, then freezes it (except a one-time manual
  `advaya_reference`); DELETE is always refused.
- **Authenticated handover operation.** `POST /v1/workflows/advaya-handover` (maker) and
  `POST /v1/workflows/advaya-handover/{lending_id}/approve` (checker) — each requires **Credit Head /
  Management / Admin** authority (checked fresh via Access) and resolves the identity from the
  verified caller. The maker starts `AdvayaHandoffWorkflow` (prepare); the checker's approval calls
  the Register approve endpoint AS the verified checker (server-minted delegated context). The package
  is exposed in the deal workspace (`GET /v1/lending/{id}/handover-package`, `POST …/download` — the
  latter returns the generated document and self-verifies its digest).
- **Preconditions to hand off.** The Lending line is `Ready for Disbursement`: CP/CS are complete
  (`cp_cs_completion`, minted from an APPROVED maker-checker checklist that distinguishes CP vs CS and
  governs waivers / CS-deferment) + `executed_agreement` evidence gated at `CP/CS Completed`, §6, and
  the mandatory `proposed_disbursement_amount` / `proposed_disbursement_date`, §5, are set. Handing
  over is row-locked to senior credit authority (§4).
- **Dormant Advaya acknowledgement path — DEFAULT OFF.** The onward disbursement states and the
  `advaya_acknowledgement` gate exist ONLY under an enabled Advaya integration
  (`REGISTER_ADVAYA_INTEGRATION_ENABLED`, default off). While off: the internal
  `/v1/internal/advaya-handoffs` router is **not registered**, `attach_advaya_evidence` is **not** in
  the workflow service's grant, the ack is refused as disabled, and startup **fails closed** if the
  flag is on without a configured endpoint. When a real acknowledgement channel exists, enabling the
  flag re-arms the whole path together: `advaya_acknowledgement` is then VERIFIED against an
  `Accepted` `advaya_handoffs` record (immutable, single-winner, migration 0015) with a matching
  payload digest (`evidence.py::_verify_advaya_handoff`), and gates `Disbursement Pending`.
- **`AdvayaHandoffWorkflow`** performs the handover via the package endpoint — **no Advaya call, no
  fabricated acknowledgement**. It stops at `Disbursed`.
- **⚠ NOT PLANNED — the actual Advaya round-trip** (real handoff call, ack ingestion, the
  `Disbursement Pending` advance, and the handoff TIMEOUT branch) is deferred until an Advaya
  integration is scheduled. Matrix: *Disbursement → Advaya handoff workflow*.

The **durable handover package + authenticated operation, the CP/CS maker-checker checklist, and the
default-off dormant acknowledgement path are implemented and tested** (`test_handover`, `test_cpcs`,
`test_policy_enforcement`, `test_evidence`, `workflows/test_business_workflows`).

---

## Frozen-contract summary table

| # | Contract | Authoritative source | Enforced by | Tests | Status |
|---|----------|----------------------|-------------|-------|--------|
| 1 | Entities & links | `repositories/subjects.py` | Register CRUD | test_crud | ✅ (Engagement ⚠) |
| 2 | Lifecycle & transitions | `rbac.ALLOWED_TRANSITIONS` | `policy.check_write` | test_policy_enforcement | ✅ |
| 3 | RBAC / tenant / scope | `rbac.OPERATIONS`, `authz/` | gateway+register | test_rbac*, test_rls | ✅ |
| 4 | Field/row locks | `policy.FIELD_LOCKS`, `rbac.ROW_LOCKS` | `policy.check_write` | test_policy_enforcement | ✅ |
| 5 | Mandatory fields | `policy.MANDATORY_FOR_STAGE` | `policy.check_write` | test_policy_enforcement | ✅ |
| 6 | Evidence model | `evidence.py`, `policy.EVIDENCE_FOR_STAGE` | evidence API + policy | test_evidence | ✅ (coverage ⚠) |
| 7 | Decisions/committee | `api/decisions.py` | decisions API | test_decisions | ✅ (quorum ⚠) |
| 8 | Reconciliation | `core/reconciliation.py` | read paths + recon API | test_import | ✅ |
| 9 | Audit/outbox | `audit_log`, decision outbox | all governed writes | test_decisions | ✅ (event bus ⚠) |
| 10 | Workflow idempotency | `workflows.py` | Temporal + idem keys | test_workflow, test_vox_e2e | ✅ (committee + conversion signals verified) |
| 11 | Advaya boundary | §11 + `handover.py` + `advaya_handover_packages` | durable package + authenticated handover op; terminal = Disbursed; ack path default-off | test_handover, test_cpcs | ✅ handover+package+disabled dormant path (real integration not planned) |
