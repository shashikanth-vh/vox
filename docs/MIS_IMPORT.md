# The Evam MIS workbook & the PRISM import — team guide

How the spreadsheet is structured, how PRISM stores it, what the lossless-import
change did, and what to follow when filling the sheet going forward.

---

## 1 · The workbook (source of truth)

`Evam_ATLAS_MIS_Consolidated_v4.xlsx` — six sheets, one row-per-thing:

| sheet | one row per | lifecycle column(s) |
| --- | --- | --- |
| **Leads** | prospect company | `Status` = Hot / Warm / Cold *(a temperature, not a lifecycle)* |
| **Deals** | company in the origination funnel | `Stage` = New Inquiry · In Screening · In Pipeline · Screened Out · Closed Won · Closed Lost · On Hold; `Status` = Hot/Warm/Cold |
| **Lending Tracker** | credit facility being executed | `Stage` = Data Awaited · Diligence · Note Circulated · Sanctioned · … (the bank pipeline) |
| **Syndication** | (company, bank) pair | `Deal Status` = Deal Live / Dropped / Closed *(coarse)*; `Status` = IM Circulated · Queries Received · IP Received · … *(the bank's real position)* |
| **Asset Mon** | asset-sale mandate — **one row per MANDATE**, a company may have several | `Status` = Teaser Prepared · In Discussion · NBO Received · Dropped |
| **Mandate Tracker** | mandate paperwork | Sent/Not Sent · Signed/Pending |

The key structural fact: **the workbook itself uses two different stage languages** —
a CRM-style *origination funnel* on the Deals sheet, and the *Indian bank/NBFC credit
process* on the trackers. That's normal and correct: the funnel measures conversion,
the credit pipeline governs execution.

## 2 · How PRISM models it

PRISM is entity-centric and keeps the same two layers, explicitly:

```
Company Name (any sheet) ──► entities        (one per distinct company)
Leads sheet              ──► leads           (status Active/Dropped/…, temperature)
Deals sheet              ──► deals           (stage = the FUNNEL, verbatim — a deal's ONLY stage)
Lending Tracker          ──► lending_tracker (bank pipeline — GOVERNED)
Syndication              ──► syndication_tracker (one per company)
                             + syndication_lenders (one per bank row)
Asset Mon                ──► asset_monetisation
Mandate Tracker          ──► syndication_tracker.mandate_status
```

"Governed" means the credit stages carry the platform's controls: the fail-closed
transition graph (no stage-skipping), mandatory data per stage, evidence gates
(committee approval, sanction letter), maker–checker, and the Temporal structuring
workflow. **All of it keys on the LENDING TRACKER line** — a deal carries no credit
lifecycle at all in the release baseline schema.

## 3 · Before the change — why rows vanished

The import screens every lifecycle value against the canonical vocabulary and
**quarantines** (skips + reports) anything unknown — the right instinct (no garbage in
the credit book), but with two mismatches for v4:

- Deals `Stage` was screened against the **credit** vocabulary → only `On Hold`
  matched → **3 of 127 deals imported**.
- The syndication tracker screened the coarse `Deal Status` column (Deal Live/…)
  which matches nothing → **0 of 162 syndication trackers imported**.
- Wording variants (`IP received`, `IM under preparation`, `IM Sent`,
  `Final sanction received`) quarantined for spelling.

## 4 · What changed (the lossless import)

1. **The funnel IS `deals.stage`** — the Deals sheet's value lands **verbatim** as the
   deal's one and only stage (schema-validated against the funnel vocabulary; in
   `/v1/ref` as "Deal Funnel Stage"; in exports and the API as `stage`). A
   credit-lifecycle word on the Deals sheet quarantines by name — credit values belong
   on the Lending Tracker sheet, whose line carries the governed pipeline.
2. **Case/whitespace-insensitive matching everywhere**, plus curated wording aliases
   (`IP received → IP Received`, `IM under preparation → IM in Prep`,
   `IM Sent → IM Circulated`, `Final sanction received → Sanctioned`). Every
   normalisation is **listed in the import response's `translated` array** — nothing
   is ever silently rewritten.
3. **Syndication semantics corrected**: the per-bank `Status` drives the tracker's
   pipeline position; `Deal Dropped`/`Deal Closed` overlay the Dropped/Disbursed
   terminals; a live deal with no bank status enters at `Deal Sourced`. Each bank row
   still becomes a `syndication_lenders` entry with its own status.
4. **Fail-closed kept**: a value the map has never seen still quarantines with a named
   reason — the alarm that the sheet's vocabulary drifted.

**Verified on the real v4 file: 127 + 162 + 61 + 26 = 376 lifecycle rows, 0 quarantined.**

## 5 · Impact on the running system

- **Two clean layers.** The deal's `stage` is the commercial funnel (RM-owned:
  ordered movement, rework steps, On Hold, final Closed Won/Lost). Credit execution —
  structuring, committee decision, evidence gates, sanction, CP/CS, handover — runs
  end-to-end on the deal's LENDING line(s); the structuring workflow fails clearly if
  a deal has no lending line. A lead conversion creates the deal at funnel
  `In Pipeline` and its lending line at credit `Data Awaited`.
- **Dashboards**: "deals by stage" now groups by the funnel — every deal has a stage;
  lending/syndication/asset-mon boards keep their own lifecycle groupings.
- **Re-imports are safe**: merge mode upserts; a NULL in the sheet never blanks a
  value the system advanced; every stage change lands in the append-only history with
  the import batch id.

## 6 · What the team follows from now on

**Filling the sheet — the allowed values per column** (case/spacing don't matter;
wording does):

- Deals `Stage`: `New Inquiry` · `In Screening` · `In Pipeline` · `Screened Out` ·
  `Closed Won` · `Closed Lost` · `On Hold`
- Lending `Stage`: `Data Awaited` · `Diligence` · `Note Circulated` · `Sanctioned` ·
  `CP/CS Completed` · `Ready for Disbursement` · `Disbursed` · `Rejected` · `On Hold`
  (legacy `Documentation` auto-maps to `CP/CS Completed`; a `Disbursed` row's proposed
  drawdown amount/date are derived from `Lending Amount` + `Stage Updated` when the sheet
  carries no disbursement columns — reported in the import response's `derived` list)
- Syndication `Status` (per bank): `IM in Prep` · `IM Circulated` ·
  `Queries Received` · `IP Received` · `Sanctioned` · `Rejected`
  (the four historical spellings keep working); `Deal Status`: `Deal Live` ·
  `Deal Dropped` · `Deal Closed`
- Asset Mon `Status`: `Teaser Prepared` · `Teaser Shared` · `In Discussion` ·
  `NBO Received` · `Dropped` · `Closed`

**Don't invent new stage words.** A new value doesn't import silently — it lands in
the report's `quarantined` list by name. If the business genuinely needs a new stage,
it's a 10-minute vocabulary/alias addition — ask, don't improvise in the sheet.

**Running an import** (Admin):

```bash
curl -sk -H "X-API-Key: dev-local-key" -H "X-User-Email: admin@evamfinance.com" \
     -F "file=@Evam_ATLAS_MIS_Consolidated_v4.xlsx" \
     "http://localhost:8000/v1/import/atlas-xlsx?mode=merge&reason=<why>"
```

`mode=merge` (default) upserts — the routine refresh. `mode=replace` wipes the
tenant's business data first — explicit, for clean reloads. Always read the response:
per-sheet counts, `translated` (what normalisation did), `quarantined` (what needs a
sheet fix), `reconciliation` (imported but flagged incomplete). Every run is audited
(who, file SHA-256, reason).

**The hand-off rule between layers**: the sheet owns the funnel and the historical
load; once a deal is live in PRISM, credit-stage movement happens **on the lending
line, in the system** (interactive transitions or the structuring workflow, with
their approvals and evidence) — the sheet doesn't drive `Sanctioned` by itself, and a
merge won't drag a system-advanced stage backwards.
