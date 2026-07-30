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
    NG["NGINX · HTTPS :8443<br/>terminates TLS · rate-limit<br/>301 from :8080 · forwards ALL to gateway"]
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
  NG -->|EVERYTHING| GW
  GW -. /atlas · injects scoped key .-> AT
  GW -. /orchestrator · injects scoped key .-> OR
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

---

## 5. Deployment topology

The same images ship two ways: a single Docker Compose stack for dev/demo, and a Helm
umbrella whose subcharts each toggle with an `enabled` flag. The identity signing secret is
shared gateway↔Register, and Postgres runs with **two roles** — the migration Job as owner,
the runtime as the non-owner `register_app` so RLS actually binds.

### Docker Compose — dev / demo

Every service is reachable on a dev port; production traffic enters only through NGINX. The
`sso` profile adds Dex and flips OIDC on.

```mermaid
flowchart TB
  subgraph EDGE["Edge"]
    NG["nginx · :8080"]
    DEX["dex · :5556<br/>profile sso"]
  end
  subgraph APP["App services"]
    GW["gateway · :8001"]
    AC["access · :8002"]
    RG["register · :8000"]
    AT["atlas · :8005"]
    VX["vocx · :8003"]
    PU["pulse · :8004"]
  end
  subgraph WF["Workflow plane"]
    OR["orchestrator · :8006"]
    WK["workflows worker"]
    TE["temporal · :7233"]
    TU["temporal-ui · :8088"]
  end
  subgraph BK["Backing services"]
    PG[("postgres · :5432")]
    MO[("minio · :9000 / :9001")]
  end
  NG -->|EVERYTHING| GW
  GW -. /atlas · injects scoped key .-> AT
  GW -. /orchestrator · injects scoped key .-> OR
  GW --> AC
  GW -->|signed context| RG
  AT --> RG
  PU --> RG
  VX --> OR
  OR --> TE
  WK --> TE
  WK -->|activities| RG
  TU -. reads .-> TE
  RG --> PG
  RG --> MO
  GW -. verify token .-> DEX
```

### Helm umbrella — Kubernetes / production

Ten subcharts (`postgresql`, `register`, `access`, `gateway`, `minio`, `temporal`, `vocx`,
`pulse`, `atlas`, `workflows`) under one release, each gated by `X.enabled`.
`values-prod.yaml` turns on OIDC + `requireAuth`, `enforceRbac`, fail-closed RLS, the signed
context, and the separate tenant-admin key. Secrets are per-chart and never in values.

```mermaid
flowchart TB
  IDP["OIDC IdP · external"]
  ING["Ingress · TLS"]
  subgraph UM["Helm umbrella: prism · each subchart X.enabled"]
    GWc["gateway"]
    ACc["access"]
    RGc["register"]
    ATc["atlas"]
    VXc["vocx"]
    PUc["pulse"]
    WFc["workflows + api"]
    PGc["postgresql"]
    MOc["minio"]
    TEc["temporal"]
  end
  subgraph ROLES["Register &harr; PostgreSQL · two roles"]
    MIG["migrate Job = OWNER<br/>DDL · create register_app · FORCE RLS"]
    RUN["Deployment = register_app<br/>non-owner → RLS binds, fail-closed"]
  end
  ING --> GWc
  IDP -. verify token .-> GWc
  GWc -->|resolve| ACc
  GWc -->|signed context| RGc
  ATc --> RGc
  PUc --> RGc
  WFc --> TEc
  WFc -->|activities| RGc
  RGc --> MOc
  RGc -. migrate .-> MIG
  RGc -. serve .-> RUN
  MIG --> PGc
  RUN --> PGc
```

In production, set `postgresql.enabled=false` / `minio.enabled=false` and point at managed
Postgres + S3; the bundled charts are for a self-contained install.

> Rendered, theme-aware version of these diagrams: publish `docs/ARCHITECTURE.md` locally, or
> see the shared artifact deck. Green rungs of the ladder are enforced at the gateway and
> re-verified at the Register; data-adjacent rungs live next to the data.

---

## 6. VocX identity — capture runs as the USER, never as a service

VocX is **a capture surface, not an authority**. It is reached one of two ways, and the
authorization story must be identical in both:

- **Shape A — a button inside the PRISM UI.** The user is already signed in, so the browser
  session already holds the verified token and the resolved roles. Nothing new has to be
  invented: the call carries the user's bearer exactly like any other PRISM action.
- **Shape B — a standalone field app.** The user signs in to that app against the **same OIDC
  issuer / audience**, and VocX verifies the incoming token and forwards the verified identity.

```mermaid
flowchart TB
  subgraph A["Shape A — VocX button in the PRISM UI"]
    U1["RM in PRISM UI<br/>already signed in · roles resolved"]
    V1["VocX capture<br/>transcribe · extract · PROPOSE"]
    RV1["RM reviews + APPROVES the proposal"]
  end
  subgraph B["Shape B — standalone VocX app"]
    U2["RM in field app"]
    V2["VocX capture<br/>verifies token · same issuer"]
  end
  ED["NGINX edge :8080"]
  GW["Gateway<br/>verify token · resolve roles<br/>operation gate · mint signed context"]
  OR["Orchestrator / Temporal<br/>governed transitions only<br/>acts AS the approving user"]
  RG["Register<br/>tenant + role + RECORD SCOPE<br/>+ lifecycle policy"]
  PG[("PostgreSQL · RLS<br/>one transaction")]

  U1 --> V1 --> RV1
  U2 --> V2
  RV1 -->|"user's bearer"| ED
  V2 -->|"user's bearer"| ED
  ED --> GW
  GW -->|"direct field update<br/>+ log interaction"| RG
  GW -->|"qualification · conversion<br/>governed status change"| OR
  OR -->|"signed context AS the user"| RG
  RG --> PG
```

### The decision is the USER's role — per action, per record

| VocX outcome | Operation checked | Register behaviour |
|---|---|---|
| New company + new lead | `create_client` + `add_lead` | Create both, then log the interaction — one transaction |
| Existing company, new lead | `add_lead` | Allowed within the caller's tenant + scope |
| Update an existing lead | `edit_lead` (**S** for BDRM) | Allowed only for a lead **in that RM's scope**; another RM's lead → **403** |
| Log interaction only | `log_interaction` (**S** for BDRM) | Requires access to the linked company/lead |
| Qualification / conversion / governed status change | transition-specific roles | **Must** go through the Orchestrator/Temporal workflow; a direct `PATCH` is refused |

Because Shape A already has the signed-in user, the RM-scope example works naturally:
VocX matches a lead **assigned to that RM** → update proceeds; it matches **another RM's**
lead → the Register returns 403 and the workflow must request reassignment or an authorized
approval; it matches **no lead** → creation proceeds only if the caller holds `add_lead`.

### The rule

> **VocX proposes. The user approves. The Register decides and writes.**

Neither VocX nor Temporal may write user-originated data under an unrestricted service
identity. They propagate the **original user identity, tenant, roles, correlation id and
idempotency key**; the Register makes the final authorization decision and owns the
transaction. A named service principal (`SERVICE_GRANTS`) is legitimate **only for unattended
work** — PULSE news scans, system reconciliation — never as a stand-in for a person.

### Implementation status (be precise about this)

| Requirement | State |
|---|---|
| Human calls: tenant + role + record scope + lifecycle enforced at the Register | **Done** — signed internal context, `_ensure_subject_scope`, RLS, policy engine |
| Governed transitions confined to workflows (direct status `PATCH` fail-closed) | **Done** — `ALLOWED_TRANSITIONS`; conversion only via `/convert` |
| Correlation id + idempotency key propagated on every Register call | **Done** — `evam-register-client` sends `X-Request-ID` + `Idempotency-Key` |
| Orchestrator acts **as the approving user** for governed operations | **Done for CP/CS + handover** (`_register_post_as` mints the user's signed context) |
| VocX forwards the verified user identity to the Register | **GAP** — VocX writes as `svc_vox`; the user is not propagated, so record scope cannot bind |
| VocX touchpoint has an explicit user review/approve step before the write | **GAP** — only conversion has an approval gate today |

Closing the two gaps is a contained change, because Shape A means the identity is already in
hand at the moment of the call: carry the caller's verified identity into the workflow input,
have the activity mint the signed context **for that user** (the same
`mint_internal_context` the gateway and orchestrator already use) instead of using the bare
service key, and add the review/approve signal to the touchpoint workflow. `svc_vox` then
keeps only genuinely unattended grants.
