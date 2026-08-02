# PRISM — Master E2E Flow: people, machines, states, and wiring

The complete picture in three views: **(1) connectivity** — what talks to what;
**(2) the journey** — every actor's actions in sequence, human and machine;
**(3) state machines** — every register's states with WHO/WHAT causes each transition
(extracted from the live policy core, not drawn from memory). Companions:
`UI_E2E_FLOW.md` (button-level flowchart), `UI_INTEGRATION_GUIDE.md` (mechanics),
`ATLAS_UI_FIELD_MAP.md` (field bindings).

---

## 1 · Connectivity — what talks to what

```mermaid
flowchart LR
  subgraph CLIENT["People"]
    SPA["Browser SPA / Postman<br/>(bearer token)"]
    VOMU["VocX capture console"]
  end
  subgraph EDGE["One door"]
    NG["NGINX :8443 (TLS)"]
    GW["Gateway<br/>verifies OIDC bearer · resolves roles via Access<br/>strips identity headers · mints SIGNED context"]
  end
  subgraph PLANE["Services"]
    REG["REGISTER<br/>system of record + policy enforcement"]
    ACC["ACCESS<br/>users · roles · matrix (authority DB)"]
    ORC["ORCHESTRATOR API"]
    TMP["Temporal server"]
    WRK["Workflow WORKER<br/>runs + monitors + sweeps<br/>acts as svc_workflows"]
    BFF["ATLAS BFF (dashboards)"]
    VOX["VocX service (STT + AI extract)"]
    PUL["PULSE (news radar)"]
    NOT["Notifier daemon<br/>email/sms/webhook outbox"]
  end
  subgraph DATA["State"]
    PG[("PostgreSQL<br/>register · access · temporal")]
    S3[("MinIO/S3 documents")]
  end
  ADV["🟧 ADVAYA (payment platform)<br/>calls back as svc_advaya"]
  IDP["IdP (Dex → Entra/Okta/Google)"]

  SPA -->|login redirect| IDP
  SPA --> NG --> GW
  VOMU --> NG
  GW -->|/v1/*| REG
  GW -->|/access/*| ACC
  GW -->|/orchestrator/*| ORC
  GW -->|/atlas/*| BFF
  GW -->|/vocx/*| VOX
  GW -->|/pulse/*| PUL
  GW -.->|resolve identity| ACC
  ORC --> TMP --> WRK
  WRK -->|svc_workflows key<br/>+ delegated human context| REG
  BFF --> REG
  VOX --> REG
  PUL --> REG
  NOT --> REG
  ADV -->|svc_advaya key:<br/>handoff outcomes · tranches| REG
  REG --> PG
  ACC --> PG
  TMP --> PG
  REG --> S3
```

Trust rules the UI inherits: the browser only ever holds a bearer token (no service
keys); the gateway is the only identity authority; `svc_*` lanes are machine-only; the
Register re-verifies everything regardless of what the UI showed.

---

## 2 · The journey — who does what, in order

🧍 = a person clicks · ⚙ = a machine acts on its own · every message is a real endpoint.

```mermaid
sequenceDiagram
  autonumber
  actor RM as RM (field)
  actor AN as Analyst (maker)
  actor CH as Credit Head (checker)
  actor MG as BD Head / Mgmt
  participant UI as UI (via gateway)
  participant ORC as Orchestrator+Temporal
  participant REG as Register
  participant M as ⚙ Monitors/Sweeps
  participant ADV as ⚙ Advaya

  rect rgb(235,244,255)
  Note over RM,REG: PHASE 1 — capture → lead (folder 03)
  RM->>UI: 🧍 record meeting, approve VOM card
  UI->>ORC: POST /workflows/vox-touchpoints?wait=true
  ORC->>REG: ⚙ entity + lead + owner assignment + interaction + calendar event
  ORC-->>UI: lead_id → lead page
  RM->>UI: 🧍 PATCH lead (temp, next action) · file qualification
  end

  rect rgb(240,255,240)
  Note over RM,MG: PHASE 2 — conversion (approval pattern)
  RM->>UI: 🧍 "Request conversion" → POST /workflows/lead-conversions
  MG->>UI: 🧍 inbox card → POST /workflows/id/decision approved
  ORC->>REG: ⚙ single-winner decision → lead Converted, DEAL + product LINES created
  UI->>REG: poll lead → deep-link the new deal (lendingId now exists)
  end

  rect rgb(255,250,235)
  Note over AN,REG: PHASE 3 — committee (folder 06)
  AN->>UI: 🧍 "Send to committee" → POST /workflows/deal-structurings
  ORC->>REG: ⚙ walk line → Note Circulated · file credit-note EVIDENCE · notify · PARK
  CH->>UI: 🧍 committee card → POST /workflows/id/committee-decision (conditions, refs)
  ORC->>REG: ⚙ verify authority · persist decision · sanction evidence · stage=Sanctioned · arm validity monitor
  end

  rect rgb(255,240,245)
  Note over AN,CH: PHASE 4 — CP/CS maker–checker (folder 07)
  AN->>UI: 🧍 submit checklist v1 → POST /internal/cpcs-checklists
  CH->>UI: 🧍 RETURN with reasons (v1 freezes)
  AN->>UI: 🧍 amend → v2 (new POST)
  CH->>UI: 🧍 APPROVE v2 → cp_cs_completion evidence
  AN->>UI: 🧍 PATCH stage → CP/CS Completed → Ready for Disbursement (+ proposed drawdown)
  end

  rect rgb(255,235,230)
  Note over AN,ADV: PHASE 5 — the ADVAYA BOUNDARY (folders 08/08b)
  AN->>UI: 🧍 "Prepare handover" → POST /internal/handover-packages (Prepared)
  CH->>UI: 🧍 "Approve" (Approved — stage does NOT move) · "Submit" (Submitted)
  ADV->>REG: ⚙ POST /internal/advaya-handoffs — Rejected + note
  AN->>UI: 🧍 correct → re-prepare → approve → resubmit
  ADV->>REG: ⚙ Accepted + acknowledgement → package FROZEN ✔ PRISM's finish line
  ADV->>REG: ⚙ tranche callbacks → FIRST one flips stage → Disbursed + actuals
  Note over UI: UI renders the stepper + disbursement panel — read-only
  end

  rect rgb(240,240,255)
  Note over RM,M: PHASE 6 — mandates · documents · monitoring (folders 09–13)
  RM->>UI: 🧍 syndication/AM runs: lender & buyer updates, NDA, offers
  MG->>UI: 🧍 mandate decisions (authority-checked) → allocate → Sanctioned/Closed
  AN->>UI: 🧍 upload documents · CH validates (maker≠checker)
  M->>REG: ⚙ expiry sweep → Expired · covenant sweep → observations due/overdue
  AN->>UI: 🧍 enter covenant result → breach AUTO-OPENS EWS case
  CH->>UI: 🧍 work the case: assign → escalate → close (senior-only once escalated)
  CH->>UI: 🧍 waiver decision → /orchestrator/v1/decisions/waiver → apply waive
  M->>REG: ⚙ waiver expiry re-opens the breach
  end

  rect rgb(235,255,245)
  Note over MG,REG: PHASE 7 — closure + the workbook (folders 14–16)
  MG->>UI: 🧍 "Close deal" → GET open-items (blockers listed) → POST /deals/id/close + note
  RM->>UI: 🧍 inbox → notifications read
  MG->>UI: 🧍 GET /v1/export/excel → every register as a sheet 🏁
  end
```

---

## 3 · State machines — every transition, with its cause

Edge label = **who/what** moves it and **how**. Unlabelled edges are ordinary human
PATCHes through the UI (policy still checks role + sequence). These are generated from
the live `ALLOWED_TRANSITIONS` — the server refuses anything not drawn here.

### Lead (`status`)

```mermaid
stateDiagram-v2
  [*] --> Active : VOX run / manual create
  Active --> OnHold : RM
  OnHold --> Active : RM
  Active --> Dropped : RM
  OnHold --> Dropped : RM
  Dropped --> Active : RM (revive)
  Active --> Converted : ⚙ conversion WORKFLOW only<br/>(hand-PATCH refused)
  Converted --> [*]
  state "On Hold" as OnHold
```

### Deal (`stage` — the commercial funnel)

```mermaid
stateDiagram-v2
  [*] --> NewInquiry : conversion creates the deal
  NewInquiry --> InScreening
  InScreening --> InPipeline
  InScreening --> NewInquiry
  InPipeline --> InScreening
  NewInquiry --> ScreenedOut
  InScreening --> ScreenedOut
  ScreenedOut --> InScreening
  NewInquiry --> OnHold
  InScreening --> OnHold
  InPipeline --> OnHold
  ScreenedOut --> OnHold
  OnHold --> NewInquiry
  OnHold --> InScreening
  OnHold --> InPipeline
  OnHold --> ScreenedOut
  InPipeline --> ClosedWon : 🧍 /deals/id/close ONLY —<br/>open-items empty + note (hand-PATCH refused)
  NewInquiry --> ClosedLost : 🧍 /deals/id/close + note
  InScreening --> ClosedLost : 🧍 /deals/id/close + note
  InPipeline --> ClosedLost : 🧍 /deals/id/close + note
  OnHold --> ClosedLost : 🧍 /deals/id/close + note
  ClosedWon --> [*]
  ClosedLost --> [*]
  state "New Inquiry" as NewInquiry
  state "In Screening" as InScreening
  state "In Pipeline" as InPipeline
  state "On Hold" as OnHold
  state "Screened Out" as ScreenedOut
  state "Closed Won" as ClosedWon
  state "Closed Lost" as ClosedLost
```

### Lending line (`stage` — the credit pipeline)

```mermaid
stateDiagram-v2
  [*] --> DataAwaited : conversion / "Add product"
  DataAwaited --> Diligence : analyst
  Diligence --> DataAwaited : analyst (back)
  Diligence --> NoteCirculated : ⚙ structuring run walks it<br/>(or analyst)
  NoteCirculated --> Diligence : analyst (back)
  NoteCirculated --> Sanctioned : ⚙ COMMITTEE DECISION workflow only<br/>+ committee & sanction evidence
  Sanctioned --> NoteCirculated : senior (unwind)
  Sanctioned --> CPCS : 🧍 needs APPROVED CP/CS checklist<br/>→ cp_cs_completion + executed_agreement evidence
  CPCS --> Sanctioned : senior (back)
  CPCS --> Ready : 🧍 + proposed drawdown amount/date (required)
  Ready --> CPCS : senior (back)
  Ready --> Disbursed : ⚙ ADVAYA's first tranche callback<br/>(handover must be Accepted) ·<br/>manual senior PATCH = audited override
  DataAwaited --> Rejected : credit
  Diligence --> Rejected : credit
  NoteCirculated --> Rejected : credit
  Rejected --> DataAwaited : revive
  Rejected --> Diligence : revive
  Disbursed --> OnHold : senior
  DataAwaited --> OnHold
  Diligence --> OnHold
  NoteCirculated --> OnHold
  Sanctioned --> OnHold
  CPCS --> OnHold
  Ready --> OnHold
  OnHold --> DataAwaited
  OnHold --> Diligence
  OnHold --> NoteCirculated
  OnHold --> Sanctioned
  OnHold --> CPCS
  OnHold --> Ready
  OnHold --> Disbursed
  state "Data Awaited" as DataAwaited
  state "Note Circulated" as NoteCirculated
  state "CP/CS Completed" as CPCS
  state "Ready for Disbursement" as Ready
  state "On Hold" as OnHold
```

### Advaya handover package (`status`)

```mermaid
stateDiagram-v2
  [*] --> Prepared : 🧍 MAKER prepares<br/>(server verifies evidence, mints digest)
  Prepared --> Approved : 🧍 CHECKER (≠ maker) — stage does NOT move
  Prepared --> Returned : 🧍 checker returns + reasons
  Returned --> Prepared : 🧍 maker re-prepares (same row)
  Approved --> Submitted : 🧍 "Submit to Advaya"
  Submitted --> Accepted : ⚙ ADVAYA accepts →<br/>acknowledgement = advaya_reference · row FROZEN
  Submitted --> Rejected : ⚙ ADVAYA rejects + note
  Rejected --> Prepared : 🧍 maker corrects & re-prepares
  Accepted --> [*] : ═ PRISM workflow boundary ═
```

### Syndication mandate (`status`)

```mermaid
stateDiagram-v2
  [*] --> DealSourced
  DealSourced --> DocsPending
  DocsPending --> IMinPrep
  IMinPrep --> IMCirculated : ⚙ mandate run files versioned IM evidence
  IMCirculated --> QueriesReceived
  QueriesReceived --> IPReceived
  IPReceived --> Sanctioned : ⚙ SYNDICATION DECISION workflow only<br/>(authority-checked) + allocation
  Sanctioned --> Disbursed
  DealSourced --> Withdrawn
  DealSourced --> Dropped
  DealSourced --> Rejected
  Withdrawn --> [*]
  Rejected --> [*]
  Dropped --> [*]
  note right of QueriesReceived
    per-LENDER rows track each bank
    (Identified → IM Circulated → Queries →
     IP Received → Sanctioned / Declined)
    — the chase list & matrix render these
  end note
  state "Deal Sourced" as DealSourced
  state "Docs Pending" as DocsPending
  state "IM in Prep" as IMinPrep
  state "IM Circulated" as IMCirculated
  state "Queries Received" as QueriesReceived
  state "IP Received" as IPReceived
```

*(Backward hops and On Hold exist as in the policy dump; terminals Withdrawn/Rejected/
Dropped are frozen. Any non-terminal may also go On Hold and back.)*

### Asset Monetisation mandate (`status`)

```mermaid
stateDiagram-v2
  [*] --> TeaserPrepared
  TeaserPrepared --> TeaserShared : 🧍 record teaser
  TeaserShared --> InDiscussion : 🧍 NDA / data room recorded
  InDiscussion --> NBOReceived : 🧍 record offer (nbo)
  NBOReceived --> BOReceived : 🧍 record offer (binding)
  BOReceived --> SPA
  SPA --> Closed : ⚙ AM DECISION workflow only<br/>(authority) + closure reference
  TeaserPrepared --> Dropped
  TeaserShared --> Dropped
  InDiscussion --> Dropped
  NBOReceived --> Dropped
  BOReceived --> Dropped
  SPA --> Dropped
  Closed --> [*]
  Dropped --> [*]
  state "Teaser Prepared" as TeaserPrepared
  state "Teaser Shared" as TeaserShared
  state "In Discussion" as InDiscussion
  state "NBO Received" as NBOReceived
  state "BO Received" as BOReceived
  state "SPA / Documentation" as SPA
```

### EWS case (`status`) · Document (`status`) · Calendar event

```mermaid
stateDiagram-v2
  direction LR
  state EWS_case {
    [*] --> Open : ⚙ AUTO-OPENED by a covenant breach<br/>(same transaction) — deduped per source
    Open --> UnderInvestigation : 🧍 assign
    UnderInvestigation --> Escalated : 🧍 escalate (reasons)
    Open --> Closed : 🧍 close + disposition
    UnderInvestigation --> Closed : 🧍 close
    Escalated --> Closed : 🧍 SENIOR only
    Closed --> [*] : frozen (DB trigger) —<br/>waiver EXPIRY opens a FRESH case
  }
  state "Under Investigation" as UnderInvestigation
```

```mermaid
stateDiagram-v2
  direction LR
  state Document {
    [*] --> Uploaded : 🧍 upload into a checklist slot
    Uploaded --> Verified : 🧍 CHECKER (≠ uploader)
    Verified --> Expired : ⚙ expiry SWEEP on expires_on
    Uploaded --> Expired : ⚙ sweep
    Expired --> Superseded : 🧍 replace (chain kept)
    Uploaded --> Superseded : 🧍 replace
    Verified --> Superseded : 🧍 replace
  }
  state Calendar_event {
    [*] --> Scheduled : 🧍 create (or ⚙ VOX follow-up)
    Scheduled --> Scheduled : 🧍 reschedule in place
    Scheduled --> Completed : 🧍 complete → FROZEN
    Scheduled --> Cancelled : 🧍 cancel → FROZEN
  }
```

### Covenant observation (the recurring loop)

```mermaid
stateDiagram-v2
  [*] --> Due : ⚙ SWEEP generates per schedule (exactly-once per period)
  Due --> Overdue : ⚙ sweep, past due date (reported once)
  Due --> OK : 🧍 result within threshold
  Due --> Breached : 🧍 result breaches → ⚙ EWS case auto-opens
  Overdue --> Breached : 🧍 late result breaches
  Overdue --> OK : 🧍 late result ok
  Breached --> Waived : 🧍 verified waiver decision +<br/>/monitoring/id/waive (time-boxed)
  Waived --> Breached : ⚙ sweep — waiver EXPIRED →<br/>breach live again, fresh EWS case
  OK --> [*]
```

---

## 4 · Reading this as a team

* **People rows** in §2 are the application's screens; **⚙ rows** are things the UI only
  ever *renders* (timelines, badges, panels).
* Every labelled edge in §3 that names a workflow or a machine is a **disabled control**
  in the UI — the state moves, but never from a dropdown.
* The Postman collection executes §2 top-to-bottom and proves every guard in §3; when a
  screen and this document disagree, run the folder — the collection is the law.
