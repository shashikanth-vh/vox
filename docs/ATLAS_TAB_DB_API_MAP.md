# ATLAS Tabs → Database Tables → APIs → Postman

One row of truth per UI tab: which PostgreSQL tables hold its data, which APIs serve
it (all through the one door, `https://<host>:8443`), and where the ready-made
requests live in Postman. Taken from the React app's `navConfig.ts` and service
modules, the register/access baselines, and the generated collections — the same
sources the running system uses.

**Postman shorthand** — `ALL` = `PRISM_All_APIs` (314 requests, grouped by service
then resource; every endpoint below is there, named by method + path). `E2E-nn` =
folder *nn* of `PRISM_E2E_Full` (142 requests, the journey in run order with
assertions). Field-level column bindings: `ATLAS_UI_FIELD_MAP.md`. Sequence and
triggering: `UI_INTEGRATION_GUIDE.md`.

---

## Sign-in / session (Login page, every tab's bootstrap)

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Token (SSO posture) | — (Dex, in-memory/its own store) | `POST /dex/token` | E2E-00b |
| Who am I / menus | `users`, `user_roles`, `matrix_versions` *(access db)* | `GET /v1/me` (gateway composition), sign-in lookup `GET /access/v1/users?q=` | ALL · Gateway/Access; E2E-01 |
| Session event for Activity | `audit_log` | `POST /v1/session-events` | ALL · Register → Audit |
| Dropdown vocabularies | `ref_values` | `GET /v1/ref` | E2E-00 |

## ⚡ Today

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Follow-ups due / chases / monitoring due | `leads`, `syndication_lenders`, `monitoring_reporting` | `GET /atlas/v1/today` | ALL · ATLAS |
| Red/amber stage bottlenecks (BN-02) | `lending_tracker` (`stage_updated_at`, `pending_with`) | same call → `stage_bottlenecks`, `attention_counts` | ALL · ATLAS |
| **Pending approvals** (all kinds) | Temporal runs + `cp_cs_checklists`, `advaya_handover_packages` | `GET /orchestrator/v1/workflows/pending` (+`?kind=`) | E2E-05/07/08 discovery requests |
| Decision dialog (approve / return / reject) | `workflow_decisions` (durable record) + subject tables | conversions: `…/{id}/approve\|reject`; committee: `…/{id}/committee-decision`; run-control: `…/{id}/control`; CP/CS + handover: their `/approve` `/return` `/reject` | E2E-05, 06, 07, 08 |
| Calendar strip | `calendar_events` | `GET /v1/calendar-events?from_ts=&to_ts=` | ALL · Register → Calendar; E2E-12 |
| Bell / inbox | `notifications`, `notification_deliveries` | `GET /v1/notifications`, `POST /v1/notifications/{id}/read` | E2E-15 |

## 📊 Dashboard

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Counts & sums per register | `leads`, `deals`, `lending_tracker`, `syndication_tracker`, `asset_monetisation`, `external_intelligence` | `GET /atlas/v1/dashboard` | ALL · ATLAS |
| Per-vertical board rows | same, per vertical | `GET /atlas/v1/pipeline/{leads\|deals\|lending\|syndication\|asset-monetisation}` | ALL · ATLAS |

## 🧲 Leads

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Grid + drawer | `leads` (+`entities` link) | `GET/POST /v1/leads`, `GET/PATCH/DELETE /v1/leads/{id}` (auto `lead_no` on omitted) | ALL · Register → Leads; E2E-03 |
| Interactions timeline | `interactions` | `GET/POST /v1/leads/{id}/interactions` | E2E-03 |
| Qualification | `governance_evidence` | `POST /orchestrator/v1/workflows/lead-qualifications` | E2E-04 |
| **Push to Deals** (convert) | `workflow_decisions` → `deals`, `lending_tracker`, `syndication_tracker`, `asset_monetisation`, `line_assignments` | `POST /orchestrator/v1/workflows/lead-conversions` → approve/reject/control | E2E-05 |
| VOX voice capture | `entities`, `leads`, `interactions`, `calendar_events` | `POST /vocx/v1/touchpoints` → `POST /orchestrator/v1/workflows/vox-touchpoints?wait=true` | E2E-03 |

## 🤝 Deals

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Grid / drawer / funnel stage | `deals` (funnel `stage` + `stage_history`) | `GET/POST/PATCH /v1/deals…` | ALL · Register → Deals |
| Add product line | `lending_tracker` / `syndication_tracker` / `asset_monetisation` | `POST /v1/lending` etc. | ALL · per tracker |
| Data Register dialog | `documents`, `document_checklist` | `GET /v1/deals/{id}/data-register`, documents routes | E2E-11 |
| Company drawer (360°) | most business tables | `GET /v1/entities/{id}/dossier`, `GET /atlas/v1/entities/{id}/summary` | ALL · Register → Entities |
| Deal close | `deals`, open-item sources (`ews_cases`, `covenants`, lines) | `GET /v1/deals/{id}/open-items`, `POST /v1/deals/{id}/close` | E2E-14 |

## 🏦 Lending

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Tracker grid, stage edits | `lending_tracker` (+`stage_history`) | `GET/PATCH /v1/lending…` (governed stages refused by hand) | ALL · Register → Lending Tracker; E2E-06 |
| Send to committee → sanction | `workflow_decisions`, `governance_evidence` | `POST /orchestrator/v1/workflows/deal-structurings` → return/revise/resubmit → `committee-decision` | E2E-06 |
| CP/CS maker-checker | `cp_cs_checklists`, `governance_evidence` | `POST /v1/internal/cpcs-checklists` (+list/queue), `/approve` `/return` `/reject` | E2E-07 |
| Advaya handover | `advaya_handover_packages`, `advaya_handoffs`, `disbursement_tranches` | prepare/list `/approve` `/return` `/reject` `/submit`; svc lane `/v1/internal/advaya-handoffs`, tranches | E2E-08, 08b |
| Covenants & EWS | `covenants`, `monitoring_reporting`, `ews_cases`, `workflow_decisions` (waivers) | covenant routes, `POST /orchestrator/v1/decisions/waiver`, `/v1/monitoring/{id}/waive`, EWS routes | E2E-13 |

## 🔗 Platform Deals (Syndication)

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Register / matrix / chase views | `syndication_tracker`, `syndication_lenders` | `GET/PATCH /v1/syndication…`, lender-row routes | ALL · Register → Syndication |
| Mandate run + decision | `workflow_decisions`, `governance_evidence` (IM versions) | `POST /orchestrator/v1/workflows/syndications` → `circulate-im` / `lender-update` / `allocate` / control → `syndication-decision` | E2E-09 |

## ♻️ Asset Monetisation

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Tracker + summary | `asset_monetisation` (+`status_history`) | `GET/PATCH /v1/asset-monetisation…` | ALL · Register → Asset Monetisation |
| Mandate run + closure decision | `workflow_decisions`, `governance_evidence` (teaser/NDA/offers) | `POST /orchestrator/v1/workflows/asset-monetisations` → `circulate-teaser` / `record-nda` / `record-offer` / `buyer-update` / control → `am-decision` | E2E-10 |

## 🗂️ Masters (sub-tabs: Clients · FI Master · Employees)

| Sub-tab | Tables | APIs | Postman |
|---|---|---|---|
| Clients | `entities` (incl. `lifecycle`, `register_status`) | `GET/POST/PATCH /v1/entities…`, dossier | ALL · Register → Entities; E2E-02 |
| FI Master | `counterparties` | `GET/POST/PATCH /v1/counterparties…` | ALL · Register → Counterparties |
| Employees | `people` (directory) + `users`/`user_roles` *(access db, RBAC)* | `GET/POST /v1/people…`; `GET/POST /access/v1/users`, `/access/v1/roles` | ALL · Register → People, Access; E2E-01 |

## 🕘 Activity (sub-tabs: Activity Log · Audit trail)

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| Both sub-tabs (one fetch, two renderers) | `audit_log` (with `changes.values` before→after + `changes.label`) | `GET /v1/audit?resource_type=&actor=&action=&since=&until=` | ALL · Register → Audit |
| "Signed in" rows | `audit_log` (`resource_type=session`) | `POST /v1/session-events` | ALL · Register → Audit |

## 🧰 Tools

| Concern | Tables | APIs | Postman |
|---|---|---|---|
| News radar | `external_intelligence` (writer: PULSE) | `GET /v1/intel…`, `/pulse/*` | ALL · Register → Intel, PULSE |
| Excel export / counts | every business table | `GET /v1/export/excel`, `GET /v1/export/counts` | E2E-16 |
| MIS import | upserts across business tables + `import_reconciliation_items` | `POST /v1/import/atlas-xlsx`, reconciliation routes | ALL · Register → Import |

---

## Tables with no tab of their own (platform plumbing)

`tenants`, `tenant_settings` (multi-tenancy + per-tenant config) ·
`idempotency_keys` (create-retry dedupe) · `line_assignments` (RBAC scope; surfaced
via `/v1/me` and assignment routes) · `change_requests` (assignment change
governance) · `workflow_decision_outbox` (decision delivery reconciler) ·
`governance_evidence_status` (supersede/invalidate trail) · `contracts_assets`,
`financials`, `documents` detail routes (drawer sections) · access db:
`access_grants`, `access_audit`, `matrix_versions` (admin screens under
`/access/v1/*`).

Every API named here is present in `PRISM_All_APIs` with a fireable sample request,
and the governed flows are additionally walked with assertions in `PRISM_E2E_Full`
(folders 00–16, including the approve/return/reject triad, checker queues, and the
Today-list discovery calls).
