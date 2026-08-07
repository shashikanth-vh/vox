# PRISM — user management & access control, the complete picture

Who can see what, who can write what, who approves what — with the *why*, and a
worked example across the whole pipeline. Everything here describes the enforced
policy (compiled baseline **v3.8** in `evam_backend_core/rbac.py`; the live authority
is the Access service's database, seeded from it).

---

## 1. The mental model — five layers

```
WHO you are   →  Google/Dex sign-in proves the person (email)
WHAT you are  →  Access service: your ROLE(S)            (Masters → Employees)
WHAT you may  →  the MATRIX: per view + per operation    (Full/Scoped/Read/Approve/None)
WHICH rows    →  SCOPE: your own book, via assignments
WHERE checked →  UI hides → gateway verifies → register enforces → database RLS
```

Two consequences worth internalising:

* **The UI is a convenience, not the security.** Even if someone hand-crafts an API
  call, the gateway and the Register re-check identity, role, operation and scope, and
  PostgreSQL row-level security is the final backstop. Hiding a button is politeness;
  the refusal happens server-side.
* **Roles stack.** A person can hold several roles (e.g. `BD Head, Syn Head`); their
  effective access on anything is the *maximum* across held roles.

## 2. The roles — and why each exists

| Role | Tier | Owns | Why it exists |
|---|---|---|---|
| **Admin** | Leadership | The system | Platform operation: users, tools, backups, delete, audit. Not a business approver by default — a governance guard-rail. |
| **Management** | Leadership | Everything business | Whole-book visibility + approval authority everywhere, but no system verbs (no delete, no backup/restore, no audit page). |
| **BD Head** | Head | Business development | Runs the top of the funnel: all leads/deals, reassignments, approves lead/deal changes. |
| **BDRM** | IC | Their own leads/deals | The field RM: creates and works *their* book; cannot touch others'. |
| **Credit Head** | Head | Lending (own book credit) | The credit authority: lending stages, analyst pool, committee/sanction evidence, CP/CS approval. |
| **Deal Analyst** | IC | Assigned lending lines | Works the credit files they're assigned; scoped writes; prepares CP/CS. |
| **Syn Head** | Head | Platform deals (syndication) | Owns the lender matrix: mandates, lender assignment, FI master, syndication evidence. |
| **Syn RM** | IC | Assigned syndication lines | Chases banks on their mandates; scoped writes on the matrix. |
| **AM Head** | Head | Asset monetisation | Owns the AM book and its evidence; assigns AM RMs. |
| **AM RM** | IC | Assigned AM records | Updates their AM mandates (AM is deliberately a plain update surface — no approval workflow). |
| **LMS Operator** | IC | Loan servicing (maker) | Post-sanction: posts routine ledger events (EMIs, accruals, charges). |
| **LMS Management** | Head | Loan servicing (checker) | The hard-to-reverse servicing verbs: books/authorizes the loan account, classification (SMA/NPA), closure. Maker-checker with the Operator. |

**Why maker/checker pairs?** Anywhere money or classification moves (CP/CS, Advaya
handover, loan booking), the person who *prepares* can never be the person who
*approves* — two different humans, enforced server-side.

## 3. Access levels

| Level | Meaning |
|---|---|
| **Full (F)** | Read + write everything in that module |
| **Scoped (S)** | Read/write **only rows in your own book** (see §5) |
| **Read (R)** | See everything, change nothing |
| **Approve (A)** | Not a data write — the authority to approve/reject a request |
| **— (None)** | The tab doesn't even render; API calls are refused |

## 4. Who sees what — the view matrix

Order: Admin · Mgmt · BD Head · BDRM · Credit Head · Deal Analyst · Syn Head · Syn RM · AM Head · AM RM · LMS Op · LMS Mgmt

| View | Adm | Mgt | BDH | BDRM | CrH | DA | SyH | SyRM | AMH | AMRM | LMSOp | LMSMg |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Today / Dashboard | F | F | S | S | S | S | S | S | S | S | S | S |
| Leads | F | F | F | S | — | — | S | S | S | S | — | — |
| Deals | F | F | F | S | S | S | S | S | S | S | — | — |
| Lending | F | F | F | S | **F** | S | R | R | R | R | R | R |
| Platform Deals (Syndication) | F | F | F | S | S | S | **F** | S | R | R | — | — |
| Asset Monetisation | F | F | F | S | S | S | R | R | **F** | S | — | — |
| FI Master (banks) | F | F | F | R | S | S | **F** | S | R | R | — | — |
| Clients | F | F | F | S | R | R | S | S | S | S | R | R |
| Employees | F | F | R | R | R | R | R | R | R | R | — | — |
| Audit / Activity Log | **F** | — | — | — | — | — | — | — | — | — | — | — |
| Tools (import/export/backup) | **F** | R | R | R | R | R | R | R | R | R | — | — |

Reading it: *Credit Head sees the whole lending book (F) but only reads syndication
context (S on deals, R nowhere needed)*. *Credit/Deal Analyst can't even open Leads —
pre-conversion prospects are BD's world.* *Only Admin ever sees Audit.* *LMS roles
read the origination books but write nothing there — their writes are servicing verbs.*

## 5. Scope — what "your own book" actually means

A **Scoped** user's rows are determined by the Register (the authority), not by name
matching in the UI. A row is *yours* if **any** of these hold:

1. You hold an **active assignment** on that line (assignments are records, started and
   ended — history preserved).
2. You **created** it (a BDRM automatically owns a lead she creates).
3. Another line of the **same company** is yours (you see the company's context).
4. It belongs to someone who **reports to you**.
5. It's **unassigned** and you're the vertical Head — unowned lines default to their
   Head (`Lending → Credit Head`, `Syndication → Syn Head`, `AM → AM Head`), so
   nothing ever sits ownerless.

**Who may assign whom** (also enforced):

| Line | Role being placed | Who can do it |
|---|---|---|
| Lead / Deal | BDRM | BD Head, Management, Admin |
| Lending / Syn / AM | Deal Analyst | **Credit Head** (owns the analyst pool), Mgmt, Admin |
| Syndication | Syn RM | Syn Head, BD Head, Mgmt, Admin |
| Asset Monetisation | AM RM | AM Head, BD Head, Mgmt, Admin |

## 6. Who writes what — the key operations

Grouped by pipeline area (F/S/A as above; roles not listed = None):

**Leads (BD only)**
- `add_lead`: Admin, Mgmt, BD Head — and BDRM (auto-owns what she creates)
- `edit_lead`: BD Head full; BDRM scoped
- `reassign_lead`: BD Head/Mgmt/Admin only — an RM can't hand off her own book
- `push_lead_to_deals` (conversion): BD Head full; BDRM scoped (her own leads)

**Deals / company profile**
- `edit_deal_profile`: BD full/scoped; Credit, Syn, AM desks scoped
- `edit_deal_ownership`: Heads only (BD/Syn/AM) + Mgmt/Admin
- `add_product_line`: BD Head, Credit Head, Syn Head, AM Head (their own line), BDRM scoped

**Lending (credit governance)**
- `change_lending_stage`: BD Head & Credit Head full; Deal Analyst scoped; *BDRM: none — she requests instead*
- `edit_lending_line`: Credit Head full; BDRM/Analyst scoped
- Committee & sanction **evidence**: Credit Head / Mgmt / Admin only
- CP/CS checklist: Analyst/Credit Head **prepare**; a *different* Credit Head-level person **approves**
- Advaya handover: Credit Head initiates, Analyst records the package, a *different* senior **approves** → line becomes Disbursed

**Platform deals (syndication)**
- `add_lender_to_mandate`, `advance_matrix_cell`, `log_chase/response`: Syn Head full; Syn RM scoped; Credit Head may work cells too (full), Analyst scoped
- `edit_fi_record` (bank master): Syn Head + Admin/Mgmt only
- Syndication evidence (IM versions, allocations, sanction record): Syn Head full, Syn RM scoped

**Asset monetisation** — deliberately light: `edit_am_record` (AM Head full, AM RM/BDRM scoped, Credit full for oversight). No stage-request lane, no closure gate — the desk asked for a plain update surface.

**Servicing (post-sanction)**
- `record_ledger_entry`: LMS Operator/Management (+ Credit Head, Analyst scoped)
- `authorize_loan_account` (booking, classification, closure): **LMS Management only** (+ Admin) — origination can never both create and book an exposure

**Requests & approvals**
- `request_stage_change`: every desk role *scoped to its own line* — note Admin/Mgmt have **none**: they don't request, they decide
- `approve_stage_change`: **A** for Admin, Mgmt, and the subject's Head — routed by line: Lending → Credit Head · Syndication → Syn Head · AM → AM Head · Lead/Deal → BD Head (plus Mgmt/Admin everywhere)

**System**
- `delete_row`, `backup_restore` (ledger import/export, backups), Audit: **Admin only**
- `edit_employee` / `add_employee_assign_role`: Admin + Management
- `export_csv`: every business role; LMS roles excluded

## 7. The pipeline, walked end to end (with a cast)

*Cast: Priya (BDRM) · Rohit (BD Head) · Divya (Credit Head) · Arun (Deal Analyst) ·
Sneha (Syn RM) · Kiran (Syn Head) · Tech Admin (Admin).*

1. **Priya logs a lead** ("Solar Co", her meeting). She owns it automatically
   (creator rule). *Another BDRM cannot see it* (Leads is Scoped for BDRMs); Rohit
   sees the whole funnel (Full).
2. **Priya pushes to deals** — her lead, so `push_lead_to_deals` (Scoped) allows the
   request; the conversion runs as an orchestrated workflow and the approval lands
   with the **BD authority** (Lead subject → BD Head). Rohit approves on his Today
   page ("Approvals waiting on you"). If Priya tried to push *someone else's* lead,
   the Register refuses her mid-workflow — scope, not UI, is the gate.
3. **The deal is born** with product lines. The Lending line is unassigned →
   defaults to **Divya** (Credit Head owns unowned lending). Divya assigns **Arun**
   as Deal Analyst (only she/Mgmt/Admin can — the analyst pool is hers).
4. **Lending progresses.** Arun updates his line (Scoped) and *requests* a stage
   change to Sanctioned; the request routes to **Divya** (`approve_stage_change`,
   Lending → Credit Head). A sanction needs **evidence** — only Divya (or
   Mgmt/Admin) may attach the committee outcome / sanction letter. In desk language
   on Today: "the CAM went to Credit committee; the committee's credit note comes back."
5. **Platform deals.** The syndication line runs the lender matrix. Sneha (assigned
   Syn RM) identifies banks, logs chases, advances cells — *forward-only*
   (Identified → IM Circulated → Queries → IP → Approved/Declined), enforced
   server-side. She edits the FI master? No — bank records belong to **Kiran**.
   Priya can *view* the matrix (deal context) but not move cells.
6. **Asset monetisation** rows (if ticked at push) are plain updates by the AM desk —
   no approvals, by design.
7. **Sanction → servicing.** CP/CS: Arun prepares, Divya approves (two people,
   enforced). Disbursement handover: prepared and then approved by a *different*
   senior. The booked loan is then LMS territory: the Operator posts EMIs; only
   **LMS Management** can classify SMA/NPA or close the account.
8. **Oversight.** Management sees everything and approves anywhere, but cannot
   delete rows, run backups, or read the audit page — those are **Tech Admin**'s,
   who conversely doesn't work deals. The audit trail records every change with who,
   when, under which policy version.

## 8. Managing users day to day

- **Add a person:** Masters → Employees (as Admin/Management): email, full name,
  role(s). With Google SSO that's *all* — they sign in with their workspace account
  and get exactly their roles. (Dex password sign-in additionally needs an entry in
  `deploy/compose/dex/config.yaml` — see `docs/GOOGLE_SSO.md` for why Google-only is
  the recommended posture.)
- **Change access:** edit their role stack in the same screen. Effect is immediate;
  sensitive operations even re-check Access *online* at call time, so a revocation
  takes effect mid-session.
- **Leaver:** deactivate the user (their history and assignments remain; they can no
  longer sign in or act). Never delete.
- **Default accounts:** first boot seeds `admin@evamfinance.com` (System
  Administrator; Admin + Management) and `tech@evamfinance.com` (TechAdmin; Admin) —
  idempotently, and additively on later starts, so the platform is never born locked out.
- **Assignments** (who owns which line) are made in the app by the authorities in §5's
  table — they're records with start/end, not free-text RM columns.

## 9. Why you can trust it (enforcement recap)

1. **UI** renders only what your roles allow — comfort, not security.
2. **Gateway** (the single public door) verifies your identity token, resolves your
   live roles from Access, and mints a short-lived *signed context* — services never
   trust a bare header.
3. **Register** re-evaluates every operation against the matrix + scope + lifecycle
   rules (stage transitions, evidence gates, maker≠checker) under that signed context.
   Machine callers (ATLAS, VocX, workflows…) are *named service principals* with
   code-fixed allowlists — a dashboard key cannot write, a workflow key cannot touch
   the FI master.
4. **PostgreSQL RLS** — even a code bug can't leak another tenant's rows; the
   database itself refuses.
5. **Audit** — every write lands in the audit trail with actor, timestamp, and the
   policy version that allowed it (`policy_fingerprint`).
