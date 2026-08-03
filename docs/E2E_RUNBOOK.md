# PRISM — the ONE end-to-end run (Postman)

`PRISM_E2E_Full` is the single flow that covers the whole EVAM journey — lending,
syndication and asset monetisation, with document upload/validation, and the
**approver triad (approve / return-for-revision / reject) exercised at every human
gate**. Run it top-to-bottom in Collection Runner and you have walked the entire
platform: field capture → lead → conversion approval → committee → CP/CS
maker-checker → Advaya handover → syndication mandate → AM mandate → documents →
calendar → covenants/EWS/waiver → deal closure → notifications → Excel export.

## How to run it (the happy path)

1. **Import** `PRISM_E2E_Full.postman_collection.json` + the environment for your
   posture — `PRISM_Full_Dex` for the prod posture (Dex sign-in), `PRISM_Full` for
   dev header-trust.
2. Set `baseUrl`/`accessUrl`/`orchestratorUrl` to the one door
   (`https://<host>:8443` …) if your host differs.
3. **Collection Runner → run the WHOLE collection in order.** Folder 00 clears every
   derived id and mints a fresh `runSuffix`, so each full run is a brand-new journey
   (new client, new lead, new deal) — no cleanup needed between runs.
4. Watch the WAIT/poll requests: they re-run themselves until Temporal settles a
   stage. In Runner they loop automatically; sending them manually, just re-send
   until the assertion passes.

**Do not cherry-pick folders on a fresh system.** Every folder assumes the state its
predecessors left. Folders 06/07/08 now open with a **GATE request** that reads the
lending line and fails with "complete folder N first — current stage: X" when the
entry state is wrong, so a mis-ordered run stops at request one with instructions
instead of a cryptic 422 mid-folder.

## Every human gate in the journey (who acts, and the triad at each)

| Folder | Gate | Who acts (persona) | Approve | Return (revise & come back) | Reject (terminal) |
|---|---|---|---|---|---|
| 05 | Lead → Deal conversion | BD Head/Management (`adminToken`) | `…/{{convWorkflowId}}/approve` → deal + product lines created | `…/control {action:"return"}` → RM revises, `resubmit` | `…/reject` (note mandatory) |
| 06 | Credit Committee on the credit note | Checker (Credit authority) | `…/committee-decision {approved:true}` → evidence filed, stage → **Sanctioned** | `…/control return` → maker `revise-credit-note` (v2) → `…/control resubmit` | `…/committee-decision {approved:false}` |
| 07 | CP/CS checklist (maker ≠ checker) | Maker prepares; Checker decides | `…/cpcs-checklists/{id}/approve` → evidence → stage → **CP/CS Completed** | `…/{id}/return` → maker amends v2 | `…/{id}/reject` (v3 demo; revival = fresh version) |
| 08 | Advaya handover package | Maker prepares; Checker decides | `…/approve` → `submit` → outcome attested | `…/return` → re-prepare | `…/reject` (folder opens with this, then re-prepare → approve) |
| 08/08b | **Advaya's offline confirmations** (pre-integration reality) | Authorised human (Credit Head/Mgmt/Admin) **on Advaya's behalf** | `POST /v1/lending/{id}/advaya-events` `{event: accepted\|disbursed, reference}` — the cited letter/UTR is mandatory; provenance `manual-attestation` | — | `{event: rejected, reference}` reopens prepare → approve → submit |
| 09 | Syndication mandate sanction | Syn Head | `…/syndication-decision` | `…/control return/resubmit` | negative decision |
| 10 | AM mandate closure | AM Head | `…/am-decision` | `…/control return/resubmit` | negative decision |
| 11 | Document validation | Uploader ≠ validator | `…/documents/{id}/validate` | replace flow (expiry → new version) | refuse validation |
| 13 | Covenant waiver | Senior credit | `POST /orchestrator/v1/decisions/waiver` → `/v1/monitoring/{id}/waive` | — | breach stands, EWS case escalates |
| 14 | Deal closure | Admin | `POST /v1/deals/{id}/close` (open items validated, note mandatory) | — | refused while open items exist |

Where an approver must FIND the work later: `GET /orchestrator/v1/workflows/pending`
(the Today list) returns every parked run AND the CP/CS + handover checker queues,
each with its ready-made decision URLs — demonstrated inside folders 05/07/08.

## When a run stops mid-way (resume matrix)

The journey is durable — a failure parks it, never loses it. Find where you are,
resume there:

| Symptom | Where you are | Resume from |
|---|---|---|
| GATE request fails naming a folder | predecessor unfinished | the named folder, at its first unexecuted request |
| 422 "may not move X → Y" on a stage PATCH | you skipped ahead | check `GET /v1/lending/{{lendingId}}` → `stage_history`; resume the folder that produces the missing stage |
| A `?wait=true` call timed out / errored but the workflow exists | run still parked | `GET /orchestrator/v1/workflows/{id}` shows `stage`; deliver its pending decision |
| 409 "already has decision" | you re-sent a decided gate | move to the next request — the first decision stood |
| Token expired (401s everywhere) | — | re-run folder 00b (sign-in) only, then continue where you were |

Golden rule: **`runSuffix` is the journey's identity.** Resuming = same environment,
same `runSuffix`, next unexecuted request. Starting over = re-run folder 00 (fresh
`runSuffix`) and go top-to-bottom; the old journey's rows remain in the register as
history, which is realistic and harmless.

## What each folder leaves behind (state map)

00 fresh ids · 00b tokens · 01 users/roles/people · 02 `entities` row →
03 `leads` (+interaction, via VOX) → 04 qualification evidence →
05 `deals` + `lending_tracker`(Data Awaited) + `syndication_tracker` +
`asset_monetisation` + assignments → 06 credit-note evidence + committee decision →
lending **Sanctioned** → 07 `cp_cs_checklists` v1/v2 (+v3 rejected) + evidence →
**CP/CS Completed** → 08 `advaya_handover_packages` + handoff → **Ready for
Disbursement** → submitted/accepted → 08b Advaya callbacks → **Disbursed** →
09 lender rows + IM evidence + syndication decision → 10 teaser/NDA/offer evidence +
AM decision → 11 `documents` validated/replaced → 12 `calendar_events` →
13 `covenants`/`ews_cases`/waiver → 14 deal Closed Won → 15 notifications read →
16 export proves the lot.
