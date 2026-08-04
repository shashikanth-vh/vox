# End-to-end from the UI — what ATLAS can drive, and where it stops

Companion to `E2E_RUNBOOK.md` (the Postman collection, 20 folders / 145 requests) and
`STAGES_AND_APPROVALS.md` (who may move what, and how).

This document answers one question honestly: **how far can a user take a deal using only
the browser?** Every label below is quoted verbatim from the UI. Every step names the API
behind the click. Where the UI has no affordance, it says so and points at the Postman
folder that does the job.

**Short answer.** A lead can be raised, qualified, converted into a client + deal +
product lines, and moved through the *operational* stages entirely from the UI. The maker's
half of the governance chain now has a home too — the **Actions** panel on each product line
in the company drawer (§10) — served by `GET /v1/workflows/actions`, which answers what
*this* user may do to *this* line right now and why the rest is not yet available. The two
steps whose shape a generic form cannot carry (the CP/CS checklist grid and handover
package assembly) are still Postman; Part 2 is the current register of those.
**Documents now reach the register** (§11) — upload, download and the maker-checker verify.

---

## Roles you will need

Sign in as a different user for each column. Role names are the ten in `RBAC Role`, and
the profile chip (top right) shows the role the session is actually running as — check it
first whenever a control is greyed.

**A greyed pencil, dropdown or button is a ROLE, not a fault.** Hovering it says who does
that step. The row still opens read-only on click. The edit rights that catch people out:

| To edit | You need |
| --- | --- |
| A lead | Admin, Management, BD Head, BDRM |
| A lending line (and its stage) | Admin, Management, Credit Head, Deal Analyst |
| A syndication line | Admin, Management, Syn Head, Syn RM, Deal Analyst |
| An asset-monetisation line | Admin, Management, AM Head, AM RM, Deal Analyst |
| A client / deal profile | Admin, Management, BD Head, BDRM |
| An FI record | Admin, Management, Syn Head |
| An employee | Admin, Management |

So an **AM RM** can edit asset-monetisation lines and nothing else — a lending row is
visible to them (they may be in its company's scope) but not editable, which is correct.


| Step | Role |
| --- | --- |
| Raise + push the lead | BDRM (or BD Head / Admin) |
| Approve the conversion | BD Head, Management or Admin |
| Operational lending stages | Credit Head / Deal Analyst / Admin |
| Committee, CP/CS, handover decisions | Credit Head, Management or Admin |
| Syndication decisions | Syn Head, Management or Admin |
| Asset-monetisation decisions | AM Head, Management or Admin |

Approver queues are **role-scoped, not person-scoped** — any Credit Head sees any
credit decision. `GET /v1/workflows/pending` returns `scoped_to` naming the email, roles
and `approver_for` verticals it answered for; if a queue looks wrong, read that first.

---

## Part 1 — what the UI drives end to end

### 1. Raise the lead — **Leads**

`Add lead` (top-left of the grid; needs `addLead`). The dialog is titled **Add lead**.

Required: **Company**, **Sector**, **Source**, **BDRM**. **Source detail** becomes
required when Source is DSA, Referral or Event. Save with **Add lead**.

→ `POST /v1/entities` then `POST /v1/leads` (a lead hangs off a company, so the entity is
written first). `lead_no` is minted server-side as `L-0001`, `L-0002`, …

> The **BDRM** and **Allot analyst** lists come from the register's own people directory
> (`GET /v1/ref`, derived live from `people`). A name that is not on that list cannot be
> chosen, which is deliberate: the conversion validates `rm`/`analyst` against the same
> table.

### 2. Work the lead — **Leads ▸ row click**

The drawer opens the current record (`GET /v1/leads/{id}`). **Log interaction** files a
touchpoint (`POST /v1/leads/{id}/interactions`); its **Next action** and **Due by** are
what Today's *Follow-ups due* reads. Save with **Save & close**.

### 3. Push to Deals — **Leads ▸ 🚀 `Push to deal`** (or the drawer's `Push to Deals →`)

Dialog: **Push to Deals — {company}**, subtitled `Group Code: … (new)` or
`(existing client — records will merge)`.

* **Client** section — **Segment / sector** and **State** are required.
* **Products** — tick any of **Lending (own book)**, **Platform Deals**,
  **Asset Monetisation**. At least one is required, and each ticked line's amount is
  mandatory with no default (`Amount ₹ Cr`, `Ask ₹ Cr`, `Indicative value ₹ Cr`).

Submit with **Push to Deals** (busy text: `Qualifying & converting…`).

→ Two orchestrator calls, in order and gated: `POST /v1/workflows/lead-qualifications`,
then `POST /v1/workflows/lead-conversions`. The conversion **pre-flights** the lead: the
company is matched canonically against the client master ("Pvt Ltd" == "Private Limited")
and linked, or created from the dialog's fields, before any run starts. A lead with no
company anywhere is refused 422 with the remedy in the message.

The response is `202` with `status: "pending approval"` — **nothing is converted yet.**

### 4. Approve the conversion — **Today ▸ Workflow approvals** (as BD Head / Management / Admin)

The queue polls `GET /v1/workflows/pending` every 60 seconds. Each row offers only the
verbs the plane actually hands back — **Approve**, **Return**, **Reject**.

The dialog is titled **Approve — {kind}** / **Return for revision — {kind}** /
**Reject — {kind}**. The field is labelled **Note**; it is **optional on approve and
mandatory on return and reject**. The CTAs are **Approve**, **Return to maker** and
**Reject**.

* **Approve** → the run applies the conversion: client, deal and the ticked product rows
  are written in one transaction, and the lead leaves the register as `Converted`.
* **Return** → non-terminal, goes back to the maker with your note.
* **Reject** → terminal.

> **Round-trip check.** Return the conversion once, confirm it leaves the queue, then
> re-push the lead from Leads and approve on the second pass. That exercises the full
> return-and-resubmit loop for the one workflow where the UI has both halves.

### 5. Confirm the books — **Deals**, **Lending**, **Platform Deals**, **Asset Monetisation**

The deal appears with the product lines you ticked. **Masters ▸ Clients** shows the
company. **Activity ▸ Audit trail** shows every write with actor and before/after.

### 6. Operational lending stages — **Lending ▸ Stage** dropdown

Change the dropdown in the grid (or in the company drawer). The change is **awaited**: the
row moves only once the register accepts it, and a refusal appears in a warning strip.

**Reachable here:** `Data Awaited` → `Diligence` → `Note Circulated`, plus `On Hold` and
`Rejected`.

**Refused here, by design:** `Sanctioned`, `CP/CS Completed`, `Ready for Disbursement`,
`Disbursed`. These are *governed* — they are written only by the workflows that carry
their approval. Selecting one shows the register's refusal rather than moving the row.

### 7. Syndication — **Platform Deals**

Three views: **Chase list**, **Matrix**, **Register (by bank)**.

* **Chase list** — `Add lender (name)` + **`+ Add`** (`POST /v1/syndication/{id}/lenders`);
  the per-lender status select (`PATCH …/lenders/{lender_id}`); **Log chase** and
  **Log reply**, each opening `Log chase — {lender}` with **Save**.
* **Matrix** — click a dot to advance a lender. Without `advanceMatrix` you get
  *"You do not have permission to advance the matrix — update statuses in the Chase List"*.
* Deal-level **Status of proposal** lives in the company drawer.

### 8. Asset monetisation — **Asset Monetisation ▸ Status**

Inline status dropdown. `Closed` is governed server-side and refused here.

### 9. Masters

**Clients** → `Add client` · **FI Master** → `Add bank / FI` → **Add FI**
(`POST /v1/counterparties`) · **Employees** → `Add employee`.

### 10. The maker's next steps — company drawer ▸ **Actions**

Open the company drawer (row click on Deals, Lending, Platform Deals or Asset
Monetisation). Each product line carries an **Actions** row underneath its fields, served
by `GET /v1/workflows/actions?subject_type=…&subject_id=…`.

Every step the platform knows about is shown. Ones you can take now are buttons; ones you
cannot are **greyed, and hovering gives the reason** — *"Available once the committee has
sanctioned this facility"*, *"This step is done by Credit Head, Management, Admin"*,
*"A committee run is already open on this deal"*. The sequence is meant to be readable off
the panel.

Clicking one opens a dialog built from the form the plane sent — no per-step screen, so a
new workflow step needs a catalogue entry on the orchestrator and no UI change at all.

On a **Lending** line: `Send to credit committee` · `File a revised credit note` ·
`Send back for decision` (both appear once a run is returned to you) ·
`Prepare CP/CS checklist` · `Prepare the Advaya handover package` ·
`Submit the handover to Advaya` · `Record an Advaya confirmation`.

Two of those open a screen of their own rather than a generic form, because a flat form
cannot express them honestly:

* **Prepare CP/CS checklist** — a repeating list of conditions, each with type (CP / CS),
  status, evidence reference and a reason. The checklist is filed as **Completed**, so
  every **required CP** must be Completed, Waived (with a reason) or Deferred as CS (with a
  reason *and* a date) — a required CP left `Pending` is refused, and the dialog blocks on
  it rather than letting the run die after the screen has closed. The **Version** field
  starts at 1; raise it when re-preparing after a checker returned the previous one, since
  the register keys a checklist on (lending, version).
* **Prepare the Advaya handover package** — tick the executed documents from the company's
  Data Register, add any reference not yet uploaded, name the recipient and the delivery
  method.

Both go to a *different* checker on Today. You cannot approve your own.

On **Platform Deals**: `Start the mandate run` · `Record a lender response` ·
`Allocate the sanctioned amounts`.

On **Asset Monetisation**: `Start the mandate run` · `Record an NDA` · `Record an offer`.

The ids are pre-filled by the plane, so the form only ever asks for what a human actually
knows. The gating lives on the orchestrator, beside the workflows that enforce it — which
is the point: a client deciding for itself which buttons to show is how the stage dropdown
came to offer four stages the register would always refuse.

---

### 11. Documents — company drawer ▸ **📁 Data Register**

Titled **📁 Data Register — {company}**. Each checklist row carries **Upload** (or
**Replace** once something is on file), and once uploaded: **View**, **Verify** and
**Remove**.

Files go to the register — `POST /v1/entities/{id}/documents/upload`, multipart, keyed by
the checklist's section and slot — so they survive a refresh, are downloadable again
(`GET /v1/documents/{id}/content`) and count toward the completeness the register itself
reports. Previously this dialog wrote to browser memory only: the ticks and the progress
bar worked while nothing was ever stored.

**Verify** is the second pair of eyes (`POST /v1/documents/{id}/validate`). The register
**refuses a verification by whoever uploaded the file** — *"A document must be verified by
a DIFFERENT checker than its uploader (maker–checker)"* — and that refusal is shown rather
than swallowed. A status chip on each row reads `On File`, `Verified` or `Rejected`.

A company with no register record yet says so, and uploads stay session-only until it has
one.

> **View needs `REGISTER_S3_STREAM_THROUGH_API=true`** when documents live in object
> storage (the compose file sets it). Without it the register 302s the browser to a
> presigned `http://minio:9000/...` URL — a docker-internal host it cannot resolve — and
> the download dies silently. Streaming through the API also keeps the one-door posture:
> everything the browser touches arrives on `:8443`.

---

---

## Part 2 — where the UI stops

Everything below is exercised by the Postman collection and has **no UI affordance**.
Run these from `PRISM_E2E_Full`; the approver half of the starred ones can then be done
in the browser on **Today**.

### Lending governance — the whole spine

| Missing step | Postman | Approver half in UI? |
| --- | --- | --- |
| Start a deal-structuring (committee) run | `06` | ★ yes — Today |
| File a revised credit note after a Return | `06` | — |
| Resubmit a returned run (run-control) | `06` | — |
| Prepare a CP/CS checklist (v1, and v2 after a return) | `07` | ★ yes — Today |
| Prepare / re-prepare a handover package | `08` | ★ yes — Today |
| Submit the handover package | `08` | — |
| Advaya attestation: acceptance, rejection, UTR per tranche | `08b` | — |
| Tranche reconciliation view | `08b` | — |

Consequence: **`Disbursed` is unreachable from the browser.** It is written only by the
Advaya attestation lane.

### Syndication and asset-monetisation workflow planes

`POST /v1/workflows/syndications` and its `lender-update` / `syndication-decision` /
`allocate` signals, and `POST /v1/workflows/asset-monetisations` with `buyer-update`,
`record-nda`, `record-offer`, `am-decision` — **none exist in the UI**. The Chase List
writes lender status straight to the register, which is a *different lane* from the
workflow's `lender-update`.

### Documents, financials, governance

* **Documents** now upload, download and verify from the UI (§11). Still missing:
  document **expiry** is not surfaced anywhere, and **Replace** uploads a new file into
  the slot rather than going through the register's replace route, so no `Superseded`
  chain is built — use folder `08` when the supersede trail matters.
* **Financials** (`POST /v1/financials`) — no UI.
* **Calendar events** — a follow-up can be created (as an interaction's next action) but
  never completed or cancelled.
* **Covenants, monitoring results, waivers, EWS cases** — no UI.
* **Deal closure** (`GET /v1/deals/{id}/open-items`, `POST /v1/deals/{id}/close`) — no UI.
  The drawer's `Lifecycle (Vistaar journey)` is a *client* field; it is not deal closure.
* **Notifications** (`GET /v1/notifications`) — no inbox, no bell.
* **Evidence** (`POST /v1/evidence`) — no UI, though `Sanctioned` is gated on it.

### UI/register mismatches — FIXED

This section used to list writes that went to routes the register never exposed (a
leftover of the localStorage prototype), failing silently while the screen updated.
All of them are now wired to the real routes, addressed by the register's UUIDs, with
UI field names mapped to the wire schema:

| Control | Now sends |
| --- | --- |
| Client drawer edits / delete | `PATCH`/`DELETE /v1/entities/{id}` |
| Employee add / edits / delete | `POST /v1/people` (part of provisioning, with Access) · `PATCH`/`DELETE /v1/people/{id}` |
| `Add product` on a deal | `POST /v1/lending` \| `/syndication` \| `/asset-monetisation` (+ the deal flag), awaited |
| Deal field edits / delete | `PATCH`/`DELETE /v1/deals/{id}` |
| Lending / Asset-Mon field edits | `PATCH /v1/lending/{id}` / `/v1/asset-monetisation/{id}` (wire field names) |

`Add employee` now creates BOTH halves of a person — the Access sign-in identity and the
register `people` row the BDRM/RM/Analyst dropdowns, conversions and VocX resolve
against — idempotently, so a retry after a partial failure completes the missing half.

Inline field edits remain fire-and-forget (`remote()`, logged on failure); creations and
deletions that gate other flows are awaited and surface the register's refusal.

### Stage-change requests are local only

The drawer's **⟳ Request stage change** (visible only to users who *cannot* edit the line)
and Today's **Stage-change requests** queue live entirely in the browser: no HTTP call,
nothing persisted, invisible to anyone else. It is not the governance path — that is
Today ▸ **Workflow approvals**.

---

## Part 3 — the recommended mixed run

The shortest path that exercises every approval gate with a return round-trip:

1. **Browser** — raise the lead, push to deals (§1–3).
2. **Browser** — Today: **Return** the conversion, re-push, then **Approve** (§4).
3. **Browser** — Lending: `Data Awaited` → `Diligence` → `Note Circulated` (§6).
4. **Postman `06`** — start the deal-structuring run.
   **Browser** — Today: **Return** it, file the revised note in Postman, then **Approve**.
   Lending reaches `Sanctioned`.
5. **Postman `07`** — prepare the CP/CS checklist v1.
   **Browser** — Today: **Return** it; prepare v2 in Postman; **Approve**.
   Lending reaches `CP/CS Completed`.
6. **Postman `08`** — upload and validate documents, prepare the handover package.
   **Browser** — Today: **Approve** the handover (four-eyes: the approver must not be the
   preparer).
7. **Postman `08b`** — submit, then attest the Advaya acceptance and each tranche UTR.
   Lending reaches `Disbursed`.
8. **Postman `09` / `10`** — the syndication and asset-monetisation runs, deciding each on
   **Today** in the browser.
9. **Postman `12`–`15`** — covenants, EWS, deal closure.

Steps 2, 4, 5, 6, 8 are the ones worth doing in the browser: they are where the UI and the
workflow plane meet, and where a real approver will actually work.
