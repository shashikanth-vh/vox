# PRISM — Architecture &amp; Call Flows

The climate-finance operating platform as built: a signed-identity gateway in front of a
tenant-isolated system of record, with durable workflows for field capture and
human-approved conversions.

- **Services:** Register (system of record), Access (identity + live RBAC matrix), Gateway
  (edge authz), ATLAS (read-side BFF), PULSE (news radar), VocX (field capture), Workflows
  (Orchestrator API + Temporal worker).
- **Stack:** FastAPI · Python 3.12 · SQLAlchemy async · PostgreSQL (fail-closed RLS) ·
  Temporal · MinIO/S3 · OIDC.

---

## 1. Component view — three trust boundaries, one system of record

Every request enters at the public edge, is authenticated and authorized at the **Gateway**
against the **Access** service's live matrix, then crosses into the data plane carrying a
**signed internal context**. The **Register** is the only writer of the book; Postgres
enforces tenant isolation underneath it.

```mermaid
flowchart TB
  subgraph CL["Clients"]
    UI["ATLAS UI · browser"]
    API["API clients · SDK / Postman"]
    FLD["VocX field capture"]
  end
  IDP["OIDC IdP<br/>Dex · Auth0 · Entra"]
  subgraph EDGE["Public edge"]
    NG["NGINX<br/>/ · /atlas · /vocx · /pulse · /orchestrator"]
  end
  subgraph MESH["Internal service mesh"]
    GW["Gateway<br/>verify OIDC · resolve+cache<br/>operation gate · mint signed context<br/>strip forged headers"]
    AC["Access<br/>users · roles · LIVE matrix · /v1/resolve"]
    AT["ATLAS BFF<br/>read-side dashboards"]
    PU["PULSE<br/>idempotent intel writer"]
    OR["Orchestrator API<br/>+ Temporal worker / activities"]
    TE["Temporal server"]
  end
  subgraph DP["Data plane"]
    RG["Register · system of record<br/>layered authz · custom routes · export · tenant admin"]
    PG[("PostgreSQL<br/>fail-closed RLS · register_app role")]
    APG[("Access DB")]
    OBJ[("Object store · MinIO / S3")]
  end
  UI --> NG
  API --> NG
  FLD --> NG
  UI -. login .-> IDP
  NG --> GW
  NG -. /atlas .-> AT
  NG -. /orchestrator .-> OR
  GW -. verify token .-> IDP
  GW -->|resolve · cached| AC
  GW -->|signed X-Internal-Context| RG
  AT -->|signed identity| RG
  PU -->|API key| RG
  OR --> TE
  OR -->|activities| RG
  FLD -. company-name capture .-> OR
  AC --> APG
  RG --> PG
  RG --> OBJ
```

---

## 2. Request authorization — the write ladder

Authorization is layered, and each rung has a distinct failure code. The first rungs are
enforced at the Gateway (from the **live** matrix) and re-verified in the Register; the
data-adjacent rungs — record scope, field/transition, and tenant isolation — live next to
the data.

```mermaid
flowchart TB
  A["Incoming request"] --> B{"API key valid?"}
  B -->|No| R1["401 · API key"]
  B -->|Yes| C{"Identity verified?<br/>signed context / OIDC"}
  C -->|No| R2["401 · unauthenticated"]
  C -->|Yes| D{"Operation allowed?<br/>live effective grant"}
  D -->|No| R3["403 · forbidden"]
  D -->|Yes| E{"Record in scope?<br/>assignment · book · vertical"}
  E -->|No| R4["403 · or protected 404"]
  E -->|Yes| F{"Field & transition allowed?"}
  F -->|No| R5["409 / 422 · rejected"]
  F -->|Yes| G{"Tenant RLS + version valid?"}
  G -->|No| R6["404 · not found / 412 · conflict"]
  G -->|Yes| OK["Commit + audit + response"]
```

---

## 3. Call flows

### Flow A — Authenticated read / write

Identity comes from the OIDC token; the Register enforces the live grant carried in the
gateway-signed context and isolates the query by tenant.

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant UI as ATLAS UI
  participant GW as Edge + Gateway
  participant ID as OIDC
  participant AC as Access
  participant RG as Register
  participant DB as PostgreSQL
  U->>UI: open PRISM
  UI->>ID: OIDC login
  ID-->>UI: bearer token
  UI->>GW: API request + bearer
  GW->>GW: verify token · strip forged headers
  GW->>AC: resolve roles + grant (cached)
  AC-->>GW: roles · views · operations · version
  GW->>GW: operation gate (live grant)
  GW->>RG: signed X-Internal-Context
  RG->>RG: verify signature · bind tenant (RLS)
  RG->>RG: op · record scope · field/transition · version
  RG->>DB: tenant-isolated read/write
  DB-->>RG: rows
  RG->>RG: commit + append audit
  RG-->>GW: authorized result
  GW-->>UI: response
  UI-->>U: render
```

### Flow B — VOX field capture → durable workflow

A company-name capture with no resolved id runs as a Temporal workflow keyed on the capture
id, so a retried upload replays the same writes — exactly-once.

```mermaid
sequenceDiagram
  autonumber
  participant VX as VocX capture
  participant OR as Orchestrator
  participant TP as Temporal
  participant RG as Register
  VX->>OR: POST vox-touchpoint (X-API-Key)
  OR->>TP: start VoxTouchpointWorkflow · id vox-{capture_id}
  Note over TP: a retried upload replays the SAME workflow id
  TP->>RG: resolve / create entity (idem key)
  TP->>RG: find / create lead (+ assign BDRM)
  TP->>RG: log interaction (idem key)
  Note over TP,RG: workflow-derived keys → exactly-once writes
  TP-->>OR: entity · lead · interaction
  OR-->>VX: 202 + status url
```

### Flow C — Lead conversion with human approval

The workflow waits durably for a Head's decision; the approver's identity is OIDC-verified
and role-checked before the Register applies the conversion in one locked transaction — no
orphan deal can survive a mid-apply failure.

```mermaid
sequenceDiagram
  autonumber
  actor RM as RM / UI
  participant OR as Orchestrator
  participant TP as Temporal
  actor HD as Head (approver)
  participant AC as Access
  participant RG as Register
  RM->>OR: request conversion · id leadconv-{lead_id}
  OR->>TP: start LeadConversionWorkflow
  Note over TP: waits durably for a decision (days if needed)
  HD->>OR: approve (OIDC-verified identity)
  OR->>AC: confirm approver role for the vertical
  AC-->>OR: role ok
  OR->>TP: approve signal
  TP->>RG: convert_lead_txn activity
  RG->>RG: lock lead FOR UPDATE · re-check open
  RG->>RG: deal + product lines + mark Converted (one tx)
  RG-->>TP: deal + line ids
  TP-->>OR: Approved
```

### Flow D — Live RBAC change, no redeploy

Because the signed context carries the effective grant, an Admin's matrix edit in Access is
enforced by the Register on the very next request. One source of truth.

```mermaid
sequenceDiagram
  autonumber
  actor AD as Admin
  participant AC as Access
  participant GW as Gateway
  participant RG as Register
  AD->>AC: edit matrix cell (guardrails hold)
  AC->>AC: bump matrix version
  Note over GW: next request re-resolves on version change
  GW->>AC: resolve (new version)
  AC-->>GW: new effective grant
  GW->>RG: signed context carries new grant
  RG->>RG: enforce updated grant immediately
```

---

## 4. Key mechanisms

| Mechanism | What it buys |
|---|---|
| **Signed context** (`evam_backend_core.internal_token`) | Short-lived JWT carries identity + the live effective grant; tamper-evident, expiring, tenant-bound — no static-secret forgery. |
| **Fail-closed RLS** (migration `0005`) | Postgres denies every row when the tenant GUC is unset; the app connects as a **non-owner role** (`register_app`) so RLS actually binds. |
| **Exactly-once** | Workflow-derived idempotency keys + stable workflow ids make retried captures and conversions replay, never duplicate. |
| **Optimistic locking** | Every row is versioned; a stale write returns **412** instead of a silent lost update. Conversion also takes a `FOR UPDATE` row lock. |
| **Central scope** (`app.authz.scope`) | One evaluator answers "is this row mine?" — assignment ∪ own book ∪ team ∪ vertical default — for list, GET, and write alike. |
| **Append-only audit** | Every mutation writes an audit row; the timeline and dossier compose from immutable interaction records. |

> Rendered, theme-aware version of these diagrams: publish `docs/ARCHITECTURE.md` locally, or
> see the shared artifact deck. Green rungs of the ladder are enforced at the gateway and
> re-verified at the Register; data-adjacent rungs live next to the data.
