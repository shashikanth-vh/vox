# ATLAS v19 Cross-Check — Backend Impact Assessment

Source: `ATLAS_EVAM_v19.html` (2026-08, 8,495 lines — baseline app + 29 augment overlays,
localStorage-persisted). Method: full extraction of the prototype's data model (every
grid's field keys, the embedded dataset shape, computed columns, forms, RBAC v2.1, audit
taxonomy, integration endpoints), diffed against the PRISM register/BFF schemas.

## Verdict

**The backend already covers v19's stored data model almost completely — one real schema
gap was found and fixed (AM ownership). Everything else v19 adds is either (a) a derived
read-side view the backend can serve from existing data, (b) something PRISM already does
more robustly, or (c) a vocabulary decision to make, not code.**

## 1. The one schema change (made)

| Gap | Fix |
|---|---|
| `am[].rm` / `am[].an` — v19 scopes the AM book, scorecards, and rollups by mandate-level RM/analyst, and its MIS import writes `rm`; PRISM's `asset_monetisation` had neither column (ownership existed only at deal level) | `rm` + `analyst` columns added to `asset_monetisation` (model, create/update/read schemas, baseline DDL, MIS importer maps the `RM`/`Analyst` workbook columns), covered by a test. Existing dev DBs need the usual one-time recreate. |

## 2. Covered — stored registers (no change needed)

| v19 store | PRISM home | Notes |
|---|---|---|
| `clients` | `entities` | superset; see §4 for `lifecycle` vocabulary |
| `leads` | `leads` | `sourceDetail`→`source_name`; `linkedDealId`→`converted_deal_id`; `remarks`/`notes` duality collapses into `notes`; v19's never-written `leads[].an` needs no home |
| `deals` | `deals` | + governed stage funnel |
| `lending` | `lending_trackers` | 1:1 incl. `pendingWith`, `h[]`→`stage_history`, auto-stamped `sanc`→`sanction_date` |
| `syn` + `syn[].lenders[]` | `syndication_trackers` + `syndication_lenders` | the per-deal×per-lender register incl. `ex`→`is_existing`, `resp`/`chased` clocks, history; v19's legacy `matrix` is a projection of it — PRISM correctly stores only the lender rows |
| `am` | `asset_monetisation` | + the new `rm`/`analyst` |
| `lenders` (FI master) | `counterparties` | PRISM already resolved v19's dual `active:"Yes"`/`inactive` and `sectors`/`preferredSectors` fields into single normalized columns |
| `people` | Access `users` (identity, multi-role stacking = RBAC v2.1's `roles[]`) + `people` register (profile) | 10-role model maps onto the ATLAS RBAC 3.1 matrix |
| `interactions` | `interactions` | superset (VOX transcript/GPS/intel columns); v19 rewrites `refId` on lead conversion — PRISM keeps typed `subject_type/subject_id` immutable and reaches lead-era history via `converted_deal_id`, which is the stronger model |
| `docs` vault | `documents` + `document_checklist_items` | sections/slots/required-% already modelled; real storage instead of base64-in-localStorage |
| `approvals` | workflow decisions + orchestrator approvals | maker–checker with verified identity, single-winner, durable — strictly stronger |
| `news` + verdicts | PULSE + `external_intelligence` | the v19 triage engine (RED/AMBER/BLUE/GREEN, context-flip for live borrowers) is the client-side mirror of the pulse module |
| `notes{code:[…]}` | `interactions` (type Note) | |
| MIS sync (`/api/mis/*`) | `POST /v1/import/atlas-xlsx` + `GET /v1/export/excel` | governed, audited, reconciled |
| audit (capped 800) | durable histories + decisions + audit log + access_audit | not size-capped, tamper-evident where it matters |

## 3. Read-side / BFF work (deferred by design — no storage change)

v19's derived layer computes everything client-side. All inputs already exist in PRISM;
these become ATLAS-BFF endpoints (or frontend computations) when the real UI is built:

* **Today worklist** — the BN-01…BN-08 attention rules, chase flags (SILENT / queries
  open / stale chase), contact staleness; thresholds = v19's `th` (config, not schema).
  The BFF's `/v1/today` exists; extend to the full rule set when the frontend lands.
* **Dashboard v4** — hero KPIs, closed/pipeline tiles, funnels, velocity (median/P75 per
  stage transition — computable from `stage_history`), sourcing mix, RM origination,
  analyst throughput, bank engagement, lens/sector/geography splits.
* **Scores** — lender partner score, sector affinity, sanction ratio (formulas documented
  in the v19 inventory; inputs are histories + lender rows).
* **Snooze/park (`snz`)** — UI state; keep client-side, or later as notification state.
* **Per-entity news watch terms (`newswatch`)** — carry via `entities.tags` or pulse
  config when the news UI is wired to PULSE.

## 4. Decisions needed (vocabulary, not code)

1. **Client lifecycle**: v19 contains TWO conflicting vocabularies —
   `Prospect → Onboarded → Active → Serviced → Vistaar — Expansion → Dormant` and the
   v2.1 list `Prospect → Engaged → Documented → Under Review → Committed → Live → Wound
   Down`. PRISM's `entities.register_status` is the field; pick ONE list and it becomes
   refdata (one small change). Until then the field is free-form.
2. **`clients.state` mandatory-at-push**: schema supports it; enforcing it is a policy
   toggle to mirror v2.1's validation once the data is actually filled (it is empty in
   all 132 v19 records today).

## 5. v19 defects the backend deliberately does NOT inherit

Client-side ID minting (collision-prone epoch/array-length ids) → server-minted; mixed
string/number amounts → typed numerics with import coercion; the seeded-but-derived
`matrix` duplicate (which already disagrees with the lender rows in v19's own seed) →
single source of truth; polymorphic re-written `refId` → typed immutable subjects;
800-entry audit cap feeding analytics → durable histories; `x{}` passthrough bags →
obsolete once PRISM is the system of record and the workbook is an EXPORT.
