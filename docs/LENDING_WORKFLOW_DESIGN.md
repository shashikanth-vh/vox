# Lending Workflow — End-to-End Design

*From converted lead to closed loan: CAM → Credit Committee → Sanction → CP →
Disbursement (Advaya, manual) → CS → Covenants → LMS.*

This document maps the required lending flow onto what PRISM already has, names what
is genuinely new, and lays out the build order. File references are to the current
codebase, so each phase starts from real seams rather than a blank page.

---

## 1. The flow, end to end

```
Lead ──(approved conversion)──▶ Deal + Lending line (Data Awaited)
                                      │
                       [A] Pre-CS document collection
                                      │
                       [B] CAM workbench (Claude Haiku)
                            select docs + prompt doc → draft CAM
                            rework loop with Claude until satisfied
                            finalise → Data Register document
                                      │
                       [C] Credit Committee: Approve / Amend / Reject
                            Amend → analyst revises CAM (new version) → resubmit
                            Approve → evidence: credit_committee_approval
                                      │
                       [D] Sanction letter (template filled from committee terms)
                            evidence: sanction_letter  →  stage: Sanctioned
                            terms captured as structured data: CP items, CS items,
                            covenants, and the LMS account terms
                                      │
                       [E] CP (conditions precedent)
                            versioned checklist; items met / pending / waived;
                            approval WITH exceptions
                            → CP/CS Completed → Ready for Disbursement
                                      │
                            CP approved ─────────────────┐
                                      │                  │
                       [F] Disbursement — Advaya,   [G] CS (conditions subsequent)
                            MANUAL handshake             checklist SEEDED at sanction
                            handover package →           (early papers file any time);
                            operator executes in         the CHASE starts at CP
                            Advaya → manual              approval — parallel with F,
                            acknowledgement →            never blocking it, and
                            Disbursed; LMS account       continuing long after
                            OPENS on 1st tranche         Disbursed until every
                                      │                  item lands
                                      │◀─────────────────┘
                       [H] Covenants — periodic until closure
                            scheduler mints each due cycle; docs + metric tests;
                            breach → EWS case (severity from the covenant)
                                      │
                       [I] LMS — accrual, EMIs, DPD, classification
                            daily interest accrual (configurable formula),
                            repayments posted manually (no bank feed),
                            Standard/SMA/Sub-standard classification by DPD,
                            account statement view = the Excel, but live
                                      │
                                  Loan Closure  (stops G/H/I schedulers)
```

**Rule of the whole design:** every transition above is enforced by the register's
existing gate machinery (role → stage → run-state → evidence → package status →
provenance), never by UI convention. The catalogue describes; the register refuses.

---

## 2. What already exists (build on it, don't rebuild it)

| Capability | Where | State |
| --- | --- | --- |
| Lead → Deal conversion with approval | `services/workflows` LeadConversionWorkflow + register `/convert` | done |
| Lending stage machine + evidence gates | `evam-backend-core/policy.py`, `register/app/api/*` — `Sanctioned` requires `credit_committee_approval` + `sanction_letter`; `CP/CS Completed` requires `cp_cs_completion` + `executed_agreement` | done |
| Data Register (documents, checksums, sections) | `register/app/models/documents.py` + document routes | done |
| Versioned maker-checker checklist (Draft → Completed → Approved/Rejected/Returned, amend = next version) | `register/app/models/cpcs.py` `CpcsChecklist` | done — **the template for CAM review too** |
| Workflow decisions, signed + verified | `register/app/models/decisions.py` | done |
| Advaya manual disbursement handover (package → manual ack), tranches | `register/app/models/advaya.py` (`AdvayaHandoverPackage`, `DisbursementTranche`), manual lane routes | done |
| Covenant model: metric/operator/threshold, frequency, first_due_on, grace, breach severity; EWS cases | `register/app/models/covenants.py` | model done, **no scheduler** |
| Notifications + deliveries, calendar events | `register/app/models/notifications.py`, `calendar.py` | done |
| Server-described actions (UI renders blind) | `workflows/app/api.py` `/v1/workflows/actions` | done |
| Anthropic (Haiku) integration pattern: prompt building, retries, structured output | `services/vocx/app/vocx/` (extraction + `template_fill`) | done — pattern to copy |

**Genuinely new:** the CAM workbench (B), the sanction-terms capture (D), the CS
reminder + covenant schedulers (G/H), and the LMS (I).

---

## 3. Phase design

### [A0] Document taxonomy — one vocabulary for the whole loan life

Every document uploaded against a lending line carries a **lifecycle section**, so the
dossier reads by phase and each checklist filters to its own shelf:

```
"Pre-CS"      collected before the CAM (phase A)
"CP"          conditions precedent, before disbursement (phase E)
"Post-CP/CS"  conditions subsequent, after disbursement (phase G)
"Covenant"    the periodic health documents (phase H)
"Sanction"    CAM versions, committee report, sanction letter (phases B–D)
```

These are `Document Section` reference values (the vocabulary already exists in
refdata), so the ops team can rename or extend them without a deploy. A CS item marked
Completed must point at a document filed under "Post-CP/CS" — the checklist and the
shelf can never disagree about where a paper lives.

### [A] Pre-CS collection — *no new machinery*

A document **section** in the Data Register (`"Pre-CS"`), uploaded against the lending
line. The existing checklist pattern covers "what is still missing": seed a
`CpcsChecklist`-style list (or reuse the document checklist items) whose items are the
expected pre-CS docs. The analyst's screen is the existing documents UI filtered to
the section.

### [B] CAM workbench — *new service module*

The one place PRISM calls Claude for credit work. **Recommendation: a `cam` router
mounted in the workflows service** (it already holds an HTTP client, identity
verification, and the register client) rather than a new container — the sessions are
short-lived drafts, not a pipeline. If it grows, it lifts out cleanly.

Data (register, new tables):

```
cam_reports        one row per CAM VERSION (the CpcsChecklist pattern verbatim):
                   lending_id, report_version, status Draft→Submitted→Approved|
                   Returned|Rejected, prepared_by, committee fields, document_id
                   (the filed PDF/MD in the Data Register), source_doc_ids JSONB,
                   prompt_doc_id, model, note
cam_turns          the rework transcript per version: role (user|assistant), text,
                   created_by — the audit answer to "why does the CAM say this?"
```

API (workflows service, `/v1/cam/...`, identity-bound like everything else):

```
POST /v1/cam/{lending_id}/generate     {doc_ids[], prompt_doc_id} → drafts v1
POST /v1/cam/{lending_id}/refine       {instruction} → next assistant turn
POST /v1/cam/{lending_id}/finalise     → renders the draft to a Data Register doc
                                         (checksum recorded), status Submitted
```

**Provider seam — today Claude, tomorrow whatever wins.** The workbench never calls
Anthropic directly; it calls an engine interface (the same shape as VocX's pluggable
STT backends):

```
CamEngine.generate(system, turns, docs) -> text      # one implementation per provider
engine = build_engine(settings)                      # CAM_ENGINE=anthropic:claude-haiku-…
```

`cam_reports.model` records the provider AND model that produced each version, so two
CAMs drafted by different engines are distinguishable forever. Adding a provider is a
new implementation + a config value — no workbench, UI, or register change. The
selected documents and the prompt doc are the engine's ONLY inputs, which is what
makes the engine swappable: all the credit judgement lives in the analyst's prompt
doc, none in provider-specific code.

Mechanics, all proven elsewhere in the repo:
* Document content is pulled from the Data Register by id; the **prompt doc is data,
  not code** — analysts own it, exactly like `config.json` owns VocX templates.
* Claude Haiku via the same client pattern as VocX extraction; **the model never
  writes to the register** — it produces text; the human files it.
* Every generate/refine turn is stored (`cam_turns`) — the committee can see how the
  sausage was made if they ask.
* Token/size guard: selected docs are truncated per-doc with a stated budget, and the
  request records which docs (and how much of each) actually went in — a CAM must
  never silently omit a document it claims to cover.

### [C] Credit committee — *existing patterns, one new surface*

The `CpcsChecklist` maker-checker lifecycle applies verbatim to `cam_reports`:
committee member opens the CAM document (Data Register), records **Approve / Amend
(→ Returned) / Reject** with a committee note — that decision is a
`WorkflowDecision`, and the committee report itself can be a filed document. On
Approve, the decision mints the `credit_committee_approval` **evidence** on the
lending line (the gate the register already enforces for `Sanctioned`). Amend →
analyst opens version n+1 in the workbench, refines, resubmits. Rejected is terminal
for the CAM (a new one can be started).

Four-eyes: `prepared_by` may not be the approver — same rule the CP/CS checklist
already enforces.

### [D] Sanction letter + terms capture — *small new piece, high leverage*

On committee approval the analyst fills the **sanction letter template**. The template
itself is chosen, not hardcoded — resolution order:

1. **Uploaded for this line** — the analyst uploads a case-specific template against
   the lending line (Data Register, section "Sanction", kind `sanction_template`);
2. **Tenant default** — otherwise the deployment's default sanction template, itself
   just a Data Register document an Admin maintains (replacing the default is an
   upload, not a deploy).

The fill screen offers the choice explicitly: "use the default template" or "upload
one for this case". The engine ([B]'s same provider seam) can pre-fill the chosen
template from the CAM + committee note; the human corrects and files. Filing mints
the `sanction_letter` evidence → the existing gate lets the stage move to
**Sanctioned** — and the filed letter records WHICH template (document id + checksum)
it was produced from.

The important part is that the sanction terms are captured **structured**, not only
as prose, because three downstream phases are born from them:

```
sanction_terms   lending_id, amount_cr, rate_kind (Fixed|Floating), rate_pct,
                 spread_pct, tenor_months, emi_amount, repayment_start,
                 day_count ('365'|'360'|'ACT/ACT'), penal_rate_pct,
                 moratorium_months, schedule_kind (EMI|Bullet|Custom)
   → seeds the LMS account [I]
cp items         → seed the CP checklist [E]
cs items         → seed the CS checklist [G]
covenants        → seed covenant rows [H] (model exists)
```

One entry screen, four registers seeded — the analyst types the terms once.

### [E] CP with conditional approval — *extend the existing checklist*

`CpcsChecklist` already versions items with `Pending|Completed|Waived`. Two
additions:

* **Approval with exceptions**: the checker may approve a checklist that still has
  `Pending` items *only* by explicitly marking each as `Deferred` with a reason and a
  due date. The approval record carries the exception list — "approved with 2
  conditions outstanding" is a first-class fact, not a note.
* The **Ready for Disbursement** action (already gated on package status) also
  carries those outstanding items into the Advaya handover package note, so the
  operator sees what was consciously deferred.

### [F] Disbursement — *exists; keep it manual and honest*

The Advaya lane stays exactly as built: prepare handover package (gated on Ready for
Disbursement + evidence), operator executes in Advaya's own screens, then records the
**manual acknowledgement** (payload hash, UTR/reference, date) — `DisbursementTranche`
rows per tranche. New: recording an accepted tranche **opens/updates the LMS account**
(first tranche = account opening + `Loan Disbursement` ledger row, like row 13 of the
Excel).

### [G] CS + reminder workflow — *new Temporal workflow, existing carriers*

CS checklist = another `CpcsChecklist` (kind `CS`). Two distinct moments, deliberately
apart:

* **Seeded at sanction** — the terms fan-out creates the item list on day one, so a
  customer's early paper has somewhere to be filed the moment it arrives.
* **Chased from CP approval** — the reminder workflow starts when the CP checklist is
  approved, runs in PARALLEL with the disbursement lane (never blocking it — that is
  what "subsequent" means), and continues long after Disbursed until every item lands.
  Starting the chase at sanction would double the nagging while the analyst is still
  assembling CP, for documents that mostly cannot exist before the money moves
  (end-use certificates, post-disbursement filings).

`CsFollowUpWorkflow` (Temporal, one per lending line, started on CP approval): sleeps
on a configurable cadence (default: 7 days), and while any required CS item is
`Pending`:
* mints a **Notification** to the analyst ("call the customer — N documents
  outstanding: …") and a **CalendarEvent**;
* escalates to the Credit Head after a configurable number of silent cycles;
* ends when every item is Completed/Waived — or the loan closes.

Durable by construction — a worker restart mid-quarter loses nothing (same
foundation the conversion workflow already uses).

### [H] Covenant cycles — *new scheduler over the existing model*

`CovenantCycleWorkflow` (Temporal, one per active covenant): computes due dates from
`frequency` + `first_due_on`, and per cycle creates a `covenant_reviews` row
(due_on, status `Due → Submitted → Passed|Breached|Waived`, document_id, metric_value)
plus the analyst notification. On breach (metric test fails, or grace expires with
nothing filed): open an **EWS case** at the covenant's `breach_severity` — the model
hook already exists. Cycles stop at loan closure. Waivers use the existing Waiver
Status vocabulary.

### [I] LMS — *new register module; the Excel, made live*

New tables (mirroring the sheet the operations team already trusts):

```
loan_accounts    lending_id (1:1), account_no (auto-numbered), borrower display,
                 disbursed_on, principal, rate terms (from sanction_terms),
                 emi_amount, tenor_months, status Standard|SMA-0|SMA-1|SMA-2|
                 Sub-Standard|Doubtful|Loss, overdue_amount, npa_provision_amount,
                 closed_on
loan_ledger      account_id, entry_date, particulars, debit, credit, balance,
                 kind (Disbursement|InterestAccrual|InterestPaid|EMI|Penal|
                 Fee|Adjustment|Closure), source (manual|accrual-job|tranche),
                 idempotency_key — append-only; corrections are reversing entries,
                 never edits (it is a ledger)
lms_config       PER-TENANT, PER-PRODUCT formula parameters — the "configurable
                 percentage" requirement lives HERE as data:
                   day_count ('365'|'360'), compounding (simple|monthly),
                   penal_rate_pct, grace_days,
                   dpd_buckets: [{status:'SMA-0',from:1,to:30}, …,
                                 {status:'Sub-Standard',from:91,…}],
                   provisioning: [{status:'Sub-Standard', pct:15}, …]
```

Jobs (Temporal cron, daily):
* **Accrual**: per open account, interest = outstanding × rate/day_count × days
  since last accrual → `InterestAccrual` ledger row. Idempotent per (account, date).
* **Classification**: DPD from the oldest unpaid due vs. payments; map through the
  configured buckets → account status + overdue amount + provisioning amount. The
  formula never lives in code that needs a deploy to change — the bucket table and
  rates are register data an Admin can edit.

Writes: repayments are **manual postings** (no bank feed exists) — an ops screen
posts `EMI Paid` / `Interest paid` rows with date + amount; the ledger recomputes the
running balance. Advaya tranche acceptance posts `Loan Disbursement` automatically.

Read: `GET /v1/lms/accounts/{id}/statement` returns exactly the Excel's shape —
header block (account no, borrower, disbursement date, facility, amount, rate,
tenure, repayment, **overdue position, loan status, provisioning**) + the dated
debit/credit/balance rows. The UI renders it; CSV/PDF export reuses the existing
export machinery.

---

## 4. Build order

Each increment ships independently and is useful on its own.

| # | Increment | Contents | New surface |
| --- | --- | --- | --- |
| 1 ✅ | **Sanction terms + seeds** | `sanction_terms` table + entry screen; seeding CP/CS checklists + covenant rows + (later) LMS account from one save | register + UI |
| 2 ✅ | **CAM workbench** | `cam_reports`/`cam_turns`, generate/refine/finalise with Haiku, committee Approve/Amend/Reject wired to the existing evidence gate | workflows + register + UI |
| 3 | **CP exceptions** | Deferred-with-reason items, approval-with-exceptions record, handover note carries them | register + UI (small) |
| 4 | **LMS core** | `loan_accounts`/`loan_ledger`/`lms_config`, tranche→ledger hook, manual postings, statement view | register + UI |
| 5 | **Accrual + classification jobs** | daily Temporal crons, DPD buckets, provisioning; status onto the statement header | workflows |
| 6 | **CS reminders** | `CsFollowUpWorkflow` + notifications/calendar + escalation (checklist seeded at sanction; the chase starts on CP approval, parallel with disbursement) | workflows |
| 7 | **Covenant cycles** | `CovenantCycleWorkflow`, `covenant_reviews`, breach→EWS | workflows + register |
| 8 | **Closure** | closure action (gated: zero balance), stops G/H/I, terminal ledger row | register |

Increments 1–2 are BUILT (register migration 0002; `register/app/api/sanction.py`;
`workflows/app/cam.py`; the default sanction template ships as bootstrap seed —
`register/app/seed/templates/sanction_letter_default.docx`, the credit team's own
letterhead, replaceable by upload). Their UI screens land with increment 3.

Ordering rationale: **1 first** because D is the fan-out point — every later phase is
seeded from sanction terms, and capturing them structured from day one avoids a
migration later. **2 second** because the CAM is the analysts' loudest pain and it
touches nothing downstream. LMS (4–5) before the schedulers (6–7) because reminders
and covenants are calendar-driven and can be operated manually for a while, whereas
interest that isn't accruing is silently wrong data.

---

## 5. Non-negotiables carried from the rest of PRISM

* **Claude drafts; humans file.** No LLM output reaches the register without a human
  finalising it, and every filed CAM records its inputs (doc ids, prompt doc,
  model) and its rework transcript.
* **Gates are register facts** — committee approval and sanction letter are evidence
  rows verified by the existing stage gate, not UI states.
* **Maker-checker everywhere a status flips** — the CpcsChecklist lifecycle is the
  single pattern (CAM, CP, CS reuse it), including "preparer may not approve".
* **The ledger is append-only**; corrections are reversing entries.
* **All formulas are data** (`lms_config`), editable by Admin, versioned by audit.
* **Idempotency** on every writer the schedulers touch — a cron that fires twice
  must not accrue twice.
