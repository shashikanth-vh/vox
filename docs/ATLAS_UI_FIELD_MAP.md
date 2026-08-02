# ATLAS Prototype ↔ PRISM Register — Field Mapping

The ATLAS prototype (the Excel-derived web dashboard) and the PRISM register describe the
same book of business, but at two different layers: the prototype shows **presentation
columns**; the register stores **normalized, typed fields**. There is **no conflict** —
the register is a strict superset of the prototype, with a handful of renames. This
document is the contract between them, extracted from `app/seed/loader.py` (the code that
actually translates the ATLAS dataset into register rows), so it cannot drift from what
the loader does.

Use it two ways:

* **Frontend teams**: bind grid columns to the register/BFF field on the right; the label
  on the left is what users already know.
* **Anyone auditing an import**: the middle column is the key in `atlas_data.json` /
  the MIS workbook that feeds the field.

The read-side APIs (ATLAS BFF `/atlas/v1/*`, register `GET /v1/*`) return the **register
field names** — the mapping below is applied only in the UI's column definitions.

## Clients → `entities`

| Prototype column / key | ATLAS data key | Register field | Notes |
|---|---|---|---|
| Group Code (row key) | client code (dict key) | `code` | The COMPANY code. Label it "Code" in the new UI — see the naming caveat below. |
| Legal name | `name` | `legal_name` | |
| — | — | `display_name` | Short UI name; new in PRISM, optional. |
| Sector | `sector` | `sector` | |
| — | — | `sub_sector` | Finer grain; new in PRISM. |
| Lens | `lens` | `lens` | |
| Lifecycle | — | `register_status` | Same concept, clearer storage name (Pipeline / Client / Market Intelligence / …). |
| State | `state` | `state` | |
| About | `about` | `about` | The curated company blurb. |
| — | `toi` | `toi` | Carried verbatim from the ATLAS sheet's `toi` column. |
| — | `notes` | `notes` | Working commentary — distinct from `about`. |
| — | — | `entity_type`, `cin`, `pan`, `gstin`, `location`, `promoter_group_code`, `tags` | New in PRISM: statutory identifiers + grouping. All optional. |

> **Naming caveat:** the prototype's "Group Code" column is really the *company* code
> (`code`). The register ALSO has `promoter_group_code` — the promoter/parent group.
> In the new UI, label `code` as **Code** and `promoter_group_code` as **Group** so the
> two concepts stop sharing a name.

## Leads → `leads`

| ATLAS data key | Register field |
|---|---|
| `id` | `lead_no` |
| `company` | `company` |
| `sector` / `lens` | `sector` / `lens` |
| `source` / `sourceName` | `source` / `source_name` |
| `rm` | `rm` |
| `status` | `status` |
| `temp` | `temperature` |
| `contact` / `phone` | `contact` / `phone` |
| `last` | `last_interaction_date` |
| `next` / `nextDate` | `next_action` / `next_action_date` |
| `conv` | `conv` |
| `notes` | `notes` |
| `createdAt` | `created_at` (preserved on import) |

## Deals → `deals`

| ATLAS data key | Register field |
|---|---|
| `code` | `deal_no` **and** `code` (ATLAS keys a deal by client code) |
| `lend` / `syn` / `am` | `is_lending` / `is_syndication` / `is_asset_mon` |
| `rm` / `an` | `rm` / `analyst` |
| `temp` | `temperature` |
| `source` / `sourceDetail` / `sourceName` | `source` / `source_detail` / `source_name` |
| `createdAt` | `date_received` |
| `remarks` | `remarks` |

PRISM adds the funnel `stage` (Sourcing → … → Closed Won/Lost), which the prototype
tracked implicitly.

## Lending → `lending_trackers`

| ATLAS data key | Register field |
|---|---|
| `id` | `tracker_no` |
| `code` | `entity_id` + `deal_id` (resolved) |
| `amt` | `amount_cr` |
| `rm` / `an` | `rm` / `analyst` |
| `stage` / `updated` | `stage` / `stage_updated_at` |
| `sanc` | `sanction_date` |
| `pendingWith` | `pending_with` |
| `remarks` | `remarks` |
| `h` | `stage_history` |

## Syndication → `syndication_trackers` (+ nested `syndication_lenders`)

| ATLAS data key | Register field |
|---|---|
| `id` / `code` | `tracker_no` / entity+deal linkage |
| `toi` | `toi` |
| `rm` / `an` / `lc` | `rm` / `analyst` / `lc` |
| `pri` / `status` | `priority` / `status` |
| `amt` / `line` / `fac` / `tenor` | `amount_cr` / `line` / `facility` / `tenor` |
| `mstat` / `mstat3` | `mandate_status` / `mandate_status3` |
| `pot` / `im` | `potential` / `im_status` |
| `sancL` / `ipL` | `sanctioned_lender` / `ip_lender` |
| `dos` / `mos` | `date_of_sanction` / `month_of_sanction` |
| `nature` / `exist` / `price` / `synType` | `nature` / `existing` / `price` / `syndication_type` |
| `pendingWith` / `remarks` / `h` | `pending_with` / `remarks` / `status_history` |
| lender rows: `name`, `ex`, `st`, `since`, `resp`, `chased`, `note`, `h` | `lender_name`, `is_existing`, `status`, `since`, `response_date`, `chased_date`, `note`, `status_history` |

## Asset Monetisation → `asset_monetisation`

| ATLAS data key | Register field |
|---|---|
| `id` / `code` | `tracker_no` / entity+deal linkage |
| `state` | `state` |
| `val` / `mw` | `indicative_value_cr` / `size_mw` |
| `nature` / `dtype` | `nature` / `deal_type` |
| `inv` / `itype` | `investor` / `investor_type` |
| `status` / `teaser` / `notes` | `status` / `teaser_date` / `notes` |

## Fields with no prototype counterpart

The mapping above is COMPLETE in one direction: **every prototype field, in every
register, lands in the schema** — no register has a legacy column without a home. What
varies is only how much PRISM added on top:

| Register | Prototype fields mapped | PRISM-only additions |
|---|---|---|
| Entities | all 7 dataset keys | `display_name`, `entity_type`, `cin`, `pan`, `gstin`, `sub_sector`, `location`, `promoter_group_code`, `tags`, `register_status` |
| Leads | all 16 | `entity_id`, `converted_deal_id`, `designation` |
| Deals | all 14 | `stage` + `stage_history` (governed funnel), `product_type`, `ic_date`, `sanction_date`, `disbursement_date`, `exit_date`, `reconciliation_status` |
| Lending | all 12 | `proposed_disbursement_amount/date`, `disbursed_amount`, `disbursement_date`, `reconciliation_status` |
| Syndication | all 28 | `reconciliation_status` (closest to 1:1) |
| Asset Monetisation | all 13 | `status_history`, `reconciliation_status` |

The additions cluster into three deliberate groups: **linkage** the spreadsheet could
not express (`entity_id` / `deal_id` / `converted_deal_id`); **governance & lifecycle**
introduced by Release 1 (the deal funnel, histories, sanction/disbursement dates); and
**`reconciliation_status`** on every business register — the MIS-import bookkeeping
column that matches a re-imported spreadsheet row against what PRISM already holds.

Every register row also carries the PRISM platform columns — `tenant_id` (multi-tenancy +
row-level security), `version` (optimistic locking), `created_at/by`, `updated_at/by`,
`deleted_at` (soft delete) — plus the Release-1 operational registers the prototype never
had (documents, calendar, covenants, EWS cases, decisions, evidence, notifications…).
These simply have no legacy column to map: they are new capability, visible in the
Excel export as their own sheets.
