# Changelog — PRISM Register

Newest first. Use the top entry to confirm you have the latest build (the zip filename
carries the short git hash; match it against `git rev-parse --short HEAD` if you clone the
bundle, or just check that the newest item below is present in your copy).

## Unreleased (working branch: claude/register-service-postgres)

- **Signed internal context — identity propagation realigned to the reference architecture.**
  The gateway→Register channel moves from *plaintext identity headers + a static shared
  secret* (with the Register re-deriving authz from its compiled matrix) to the diagram's
  **"signed user context"**: after OIDC + Access resolve, the gateway mints a short-lived
  **signed** token (`X-Internal-Context`, `evam_backend_core.internal_token`) carrying the
  caller's identity + the *live* effective permissions; the Register verifies the signature
  and enforces from it.
  - **Tamper-evident & expiring** — roles can't be rewritten in flight, a stolen token dies
    in ~2 min, and a leaked static secret can no longer be replayed to forge an identity.
  - **Single source of truth** — `operation_access` / `view_access` prefer the forwarded
    live grant, so an Admin's live Access-matrix edit is enforced by the Register
    immediately; Register and Access can no longer disagree (the prior "policy source"
    caveat is resolved when the secret is set).
  - **Tenant-bound** — a token minted for one tenant is rejected against another.
  - Backward compatible: no signing secret → the legacy header path (dev) is unchanged.
    HS256 default; RS256 supported. Wired through Compose, both Helm charts (Secret-backed)
    and `values-prod.yaml`. New tests: core mint/verify (tamper/expiry/wrong-key) + Register
    e2e (identity-from-token, live-grant allow/deny, forged-header-ignored, cross-tenant).

- **Security audit round 5 — custom routes, transitions, conversion race, RLS deployability.**
  - **Custom-route authz bypasses (R5-1).** The versioned financials create gated on the
    loose `add_company_note`; it now requires `edit_fi_record` (a Deal Analyst / AM RM can
    no longer publish financials). Intelligence acknowledge/dismiss required only company
    READ; they now require `edit_intel`, so a read-only clients viewer (Credit Head, Deal
    Analyst) cannot mutate a signal. The flat `/v1/syndication-lenders` list is now scoped
    by the parent syndication tracker's company — a scoped Syn RM no longer reads every
    lender tenant-wide.
  - **Status transitions cannot bypass the workflow (R5-2).** A generic PATCH can no
    longer set `Lead.status` to `Converted` (that must go through `/convert`, which creates
    the deal + product lines atomically), the change-request flow refuses the same
    transition, and a row lock (Converted lead, Disbursed lending) is now enforced on the
    **target** value of a generic update — not only once the row is already locked.
  - **Conversion concurrency & idempotency (R5-3).** The lead row is locked
    `FOR UPDATE` for the whole conversion, so two concurrent converts serialise (the second
    sees Converted → 409) instead of racing to two deals; the Idempotency-Key is re-checked
    under the lock so a same-key retry replays the first result.
  - **RLS is genuinely deployable (R5-4).** The migration Job now runs as the schema
    **owner** (`database.migrationUser`) while the Deployment runs as the non-owner
    `register_app` — so DDL / role-creation / FORCE succeed and the runtime is actually
    bound by RLS. A new test drives real API CRUD with FORCE RLS on to prove the
    per-request GUC pattern keeps working; `values-prod.yaml` and DEPLOYMENT.md document
    the two-role setup.

- **Security audit — P0 authorization bypasses closed (write/scope/export/tenant/RLS).**
  - **Company-resource writes deny READ/NONE (P0-1/P0-3).** The company-write helper only
    checked SCOPED, so a READ-only viewer could PATCH financials / contracts / intel /
    monitoring / documents. It now requires each resource's specific WRITE operation
    (`edit_fi_record`, `edit_contract`, `edit_intel`, `edit_monitoring`,
    `upload_remove_documents`) — READ/NONE roles are refused, SCOPED is company-scoped,
    FULL passes — and validates the payload's `entity_id` on create. **Entity** profile
    edits gained a dedicated `edit_client` operation, fixing the inversion where humans
    were denied while machine callers slipped through.
  - **Orphan resources are gated (P0-2).** People, counterparties and the document
    checklist now gate reads by a view and writes by an operation (`edit_employee`,
    `manage_counterparty`, `manage_checklist`); the flat `/v1/syndication-lenders` route
    is **read-only** (mutation only through the secured nested routes).
  - **Conversion & workflow-adjacent writes hardened (P0-4).** Lead conversion rejects
    any closed lead (not just Converted), is **idempotent** on the Idempotency-Key, and a
    SCOPED caller now needs **exact write access to the lead** (assignment / own-book /
    vertical default), not mere company visibility. Assignment and change-request lists
    are self-scoped (Admin/Management/Heads see all); machine callers ending an assignment
    honour `enforce_rbac`; change-request approval re-checks the row-lock policy.
  - **Exports are row-scoped (P0-5).** `/export/excel|json|counts` now apply the caller's
    row scope for non-admins; `/export/counts` is gated (was open); a full or
    `include_deleted` backup requires `backup_restore` (Admin-only).
  - **Tenant administration is Admin-only (P0-6).** A separate `X-Admin-Key` credential
    (distinct from the shared data-plane key) plus a **verified Admin identity** are
    required to create / (de)activate tenants — a shared-key holder can no longer
    administer tenants.
  - **PostgreSQL RLS is fail-CLOSED (P0-8).** Migration 0005 recreates every tenant
    table's policy with no NULL escape (missing tenant context → zero rows), extends
    coverage to the tables 0001 missed (documents, checklist, assignments, change
    requests, tenant_settings, idempotency, audit), creates a non-owner `register_app`
    role, and — when `REGISTER_ENFORCE_RLS` is on — FORCEs RLS so even the owner is bound.
    A two-tenant database-level test proves fail-closed + isolation + WITH-CHECK.
  - **Identity propagation tightened (P0-7, partial).** Access now verifies the gateway
    signature on forwarded identity (no Admin impersonation via a shared key); ATLAS Helm
    gains OIDC config so it validates the bearer itself; a `values-prod.yaml` overlay
    flips OIDC/requireAuth/enforceRbac/enforceRls on and points the app at `register_app`.
    (Distinct per-service DB principals and a single authoritative Access/Register decision
    source remain follow-up work.)
  - New adversarial tests: 11 write/scope/export/tenant cases + the two-tenant RLS test.

- **Round-3 review close-out — the remaining production-blocking findings, fixed.**
  - **MIS import is now a true upsert, not a re-insert.** A second (merge) import of the
    same workbook previously re-inserted people, counterparties, leads, deals and every
    tracker — tripping the `*_tenant_*` unique constraints. Each is now reconciled by its
    natural key: people by `full_name`, counterparties by `name`, and leads / deals /
    lending / syndication / asset-monetisation by their entity (with a `lead_no`/`tracker_no`
    generator that skips numbers already in use). Company **canonicalisation now peels
    corporate suffixes** (`Private Limited`, `Pvt Ltd`, `Ltd`, `LLP`, …) so legal-form
    variants of one name collapse to a single entity. New tests: a same-workbook merge is a
    no-op on counts, and three suffix variants resolve to one entity.
  - **Approval identity is mandatory and token-derived.** The orchestrator's requester
    (`start_conversion`) and decider (`approve`/`reject`) identities now come from the
    verified OIDC token, never a caller-supplied string. A new `WORKFLOWS_REQUIRE_AUTH`
    switch makes this compulsory: with it on and no OIDC configured, the orchestrator
    **refuses** the request rather than trusting `by`/`requested_by`. New unit tests prove
    the refusal (and that the API-key gate fires first).
  - **Orchestrator is no longer open in Compose, and its keys are Secret-backed in Helm.**
    Compose now gives the orchestrator a machine API key (VocX forwards it) plus Access
    wiring; the Helm chart renders the orchestrator API key and Access key into its
    `Secret` and injects them via `secretKeyRef` — no plaintext key in pod env — with a
    `requireAuth` value for production.
  - **One public edge fronts every service.** NGINX now routes `/atlas`, `/vocx`,
    `/pulse` and `/orchestrator` prefixes to their services (carrying the OIDC bearer
    through) alongside `/` → gateway → Register, so the whole platform is reachable on a
    single host instead of a spread of dev ports.

- **Security & robustness pass — the RBAC/workflow review findings, fixed.**
  - **Wrong-company VOX lead (data-integrity bug), fixed.** `Lead` now whitelists
    `entity_id` as a filter, and the core CRUD repository **refuses an unknown filter
    instead of silently dropping it** — a dropped `entity_id=` filter was how "the
    company's active lead" degraded to "the newest active lead in the tenant". Two
    regression tests (register + the VOX e2e) create a NEWER unrelated lead first, so
    the old behaviour would fail them.
  - **Gateway internal-header injection, fixed.** The gateway now strips every
    internal identity/authz header (`X-Authz-Decision`, `X-Gateway-Auth`, `X-User-Id`,
    `X-User-Roles`, the reporting-team headers) from the INCOMING request before adding
    its server-derived values — a client can no longer inject `X-Authz-Decision: FULL`
    on an unmapped route and have the gateway stamp its valid secret onto the forgery.
    New e2e test CF8 proves the wall.
  - **Scope evaluator applied to the remaining routes:** versioned financials
    create/history, syndication-lender nested list/add, the lender matrix, the
    Data-Register roll-up, intel acknowledge/dismiss, change-request creation
    (a SCOPED requester must be on the line/company), scoped deal/product-line creation,
    and `/v1/authz/check` — which now answers from the CENTRAL evaluator (own book /
    connected company / team / vertical-Head default) instead of exact-assignment only,
    so it can never disagree with actual enforcement.
  - **Verified OIDC identity (opt-in), the impersonation fix.** New shared
    `evam_backend_core.oidc` (JWKS fetch + RS256/ES256 validation of iss/aud/exp). With
    an issuer configured, the **gateway** derives the caller's e-mail from the bearer
    TOKEN (not the client-assertable `X-User-Email`), and the **orchestrator** derives
    the approver from the token AND confirms an approver role via the Access service —
    a caller can no longer claim to be the Credit/BD Head. Unit tests cover valid /
    wrong-audience / unknown-key tokens.
  - **ATLAS forwards verified identity.** ATLAS read paths now attach the caller's
    identity + roles (+ the gateway secret) to their Register client, so the Register's
    row-level scope applies to dashboards — a scoped user no longer receives tenant-wide
    rows. (New SDK `extra_headers` supports this.)
  - **MIS import made safe.** The replace import was a `TRUNCATE … CASCADE` that wiped
    EVERY tenant; it is now a **tenant-scoped `DELETE … WHERE tenant_id`** (child→parent).
    Merge is a **real upsert**: entities are matched canonically and reused (empty fields
    enriched, curated data never clobbered) instead of re-inserted. Regression test
    proves a replace import for tenant B leaves tenant A's rows intact.
  - **Deploy hardening.** Access `enforceRbac: true` in the umbrella (an identity-less
    Access-key holder can't mint users/roles); the Orchestrator ships a real API key
    (`prism-workflows-key`) + optional OIDC/Access wiring; the Register ingress stays
    off in favour of the gateway ingress.
  - **Build correctness.** The workflows image installed shared packages `--no-deps`
    but the service under-declared its transitive deps (`httpx`, `pydantic-settings`,
    `pyjwt`) — now declared, so runtime imports resolve. New CI jobs **build every
    service image and smoke-import its app**, and **`helm lint` + `helm template`** the
    umbrella and every subchart — the class of failure editable installs hide.
  - **Workflow robustness.** VOX-created leads now get a REAL `LineAssignment` for the
    capturing BDRM (`assigned_rm_id`) via a narrow machine-caller carve-out (primary-
    owner role only), so the actual RM owns the lead. Lead conversion is now
    **compensating** — a failure mid-apply soft-deletes the deal + created lines so no
    orphan survives — and **restartable**: re-requesting after a rejected/timed-out run
    starts a fresh attempt (`leadconv-{lead}#{n}`) instead of colliding with terminal
    history.
  - Still open, stated plainly: real STT/Haiku understanding and an approval UI, actual
    calendar event creation (today it records `meta.calendar.status=pending`), forced
    RLS (`REGISTER_ENFORCE_RLS` is still advisory), a single public ingress fronting
    ATLAS/VocX/PULSE/Orchestrator, the full field-policy engine, the remaining lifecycle
    workflows, and production-grade (HA/mTLS) Temporal.

- **RBAC 3.1 scope completion — ONE central scope evaluator, invoked everywhere**
  (closing the scope-scenario gaps in the RBAC review):
  - **`app/authz/scope.py`** — the single definition of "in my scope": direct
    **assignment** ∪ **connected company** (assigned to any line of the company →
    READ across its records) ∪ **own book** (rows you created — authenticated writes
    are now stamped with the VERIFIED user e-mail, never the spoofable X-Actor) ∪
    **team** (the Access service resolves the transitive `reports_to` tree; the
    gateway forwards it as `X-User-Report-Ids`/`X-User-Reports`) ∪ **vertical-Head
    default ownership** (an UNASSIGNED line belongs to Credit/Syn/AM Head —
    operational, not descriptive).
  - Invoked consistently: **list** filtering (SQL disjunction incl. unassigned-line
    ownership), **direct GET-by-id** (a scoped user can no longer fetch an unrelated
    row by knowing its id — the EcoSoch/Meera hole, closed), **create** (operation
    gate from the matrix + **auto-assignment**: a BDRM's new lead is assigned to them
    at birth, so their scoped list can never hide it), **update** (evaluator-backed),
    **documents** (subject writes verify the referenced line/company; downloads check
    the company), **interaction timelines** (log + read), **dossier**, **entities**
    (the clients view is now scope-aware), **audit** (Admin-only guardrail enforced),
    **restore** (mirrors Admin-only delete), **MIS import** (backup_restore,
    Admin-only), **settings** (Admin/Management), **exports** (export_csv).
  - **Field Rules, first slice — row locks** (`ROW_LOCKS` in the shared policy):
    a Converted lead locks against edits except Admin/Management/BD Head; a
    Disbursed lending line except Admin/Management/Credit Head.
  - **Access service**: user governance (create/edit users, grant/revoke roles) now
    allows **Management** as the spec says (`edit_employee` = F F …); the access
    MATRIX stays Admin-only. `/v1/resolve` returns the transitive reporting tree.
  - **Machine-caller policy made explicit**: vetted API keys (VocX/PULSE/workflows)
    keep ingestion write paths; the RBAC-mandatory flag hard-gates the destructive /
    audit surfaces for identity-less callers. The Helm umbrella now ships
    `register.enforceRbac: true`, and the platform's ingress moved to the **gateway
    chart** (Register + Access stay cluster-internal).
  - **Tests** (register 65 → 71; access 5 → 6): the concrete review scenarios —
    Arun's auto-owned lead, Meera's direct-GET wall + connected-company reads,
    Syn RM connected-dossier protection, team scope via reports headers, vertical-Head
    default ownership, the Converted-lead lock, audit/restore guardrails, Management
    governance + resolve reports.
  - Still open from the review, called out honestly: Dex/OIDC token-derived identity,
    the full field-policy engine (mandatory fields, per-stage field locks, author-only
    interaction edits, duplicate matching), tenant-scoped MIS reconciliation, and the
    ATLAS v17 front-end wiring.

- **The workflow plane made operational — Orchestrator API + a genuine end-to-end VOX
  workflow + human-in-the-loop signals** (closing the gaps in the Temporal review):
  - **Orchestrator API** (`services/workflows/app/api.py`, `python -m app.api`; compose
    `orchestrator` :8006, Helm `prism-workflows-api`): starts workflows over HTTP with
    **stable business workflow ids** (`vox-{capture_id}`, `leadconv-{lead_id}`) and
    **idempotent starts** (an id that already ran attaches instead of duplicating);
    delivers `approve`/`reject` signals; answers status (execution state + in-workflow
    stage + result). Nothing needs a Temporal client or CLI anymore.
  - **`VoxTouchpointWorkflow`** — the full capture flow: resolve company by **canonical
    name** (suffix-stripped matching), create entity + lead when missing / link & update
    the active lead when present; the interaction now carries **transcript, audio
    reference, language, GPS, attendees, key intel, next steps, contact, acting RM +
    assigned RM, follow-up dates**, the Temporal workflow id in `source_ref`, and a
    calendar hand-off record in `meta.calendar` for the calendar integration.
  - **`LeadConversionWorkflow`** — Temporal **signals** (`approve`/`reject` with
    decided-by + note, first decision wins), a **query** (`status`) and a decision
    timeout; approval applies deal + product lines + lead Converted atomically through
    idempotent activities; rejection/timeout is recorded on the lead.
  - **VocX** routes company-name captures through the orchestrator automatically
    (`VOCX_ORCHESTRATOR_URL`); resolved-subject captures keep the direct fast path.
  - **Tests**: canonical-matching + full-payload activity tests on the mock Register,
    plus a real e2e suite (Orchestrator → Temporal test server → worker → real Register
    on real Postgres) covering new-company capture, **duplicate-retry replay** (same
    capture id → same rows, nothing new), existing-company lead linking, and
    signal-driven conversion approval. The Temporal-runtime tests skip where the test
    server can't be downloaded (offline sandboxes) and run in CI.
  - Still deferred from the review, called out honestly: Dex/OIDC identity, ingress
    hardening, tenant-scoped MIS reconciliation import, the remaining lifecycle
    workflows (lending stages, syndication chase, document/OCR, CIPHER, Advaya,
    covenant monitoring), and the production Temporal posture (dedicated HA datastore,
    mTLS, search attributes, worker versioning) — see docs/DEPLOYMENT.md notes.

- **A full README in every service — each one runnable on its own.** All seven
  services (`register`, `access`, `gateway`, `vocx`, `pulse`, `atlas`, `workflows`)
  now carry the same README shape: what it is and why, the API table, the complete
  env-var configuration table, **"Run it standalone"** (plain `docker run` with a
  throwaway dependency where needed, the one-file-compose subset command, and the
  vendored-chart `helm install` for just that service), tests, and an
  "extending it — start here if you are new" section. A user can pick any single
  service — e.g. Access as a standalone user-management API, or the Register as a
  standalone system of record — and run it with nothing else from the platform.

- **PULSE + ATLAS as individually deployable services — every PRISM module now ships
  on its own.** Two NEW stateless services on the platform SDK:
  - **`services/pulse`** — the news / adverse-media radar. Pluggable providers
    (RSS / JSON endpoint / offline sample), explainable entity matching (name match +
    configurable RED/GREEN keyword signals), and idempotent intel writes — every
    (item, entity) pair is keyed `pulse:{tenant}:{entity}:{url-hash}`, so re-running a
    scan never duplicates an alert. `POST /v1/scan` (cron/Temporal-triggered; the
    pulse Helm chart ships a CronJob for the 7 AM IST run), `POST /v1/items` (push
    door for scrapers/webhooks), `GET /v1/digest` (RED/AMBER/GREEN digest payload).
    Multi-tenant per request via `X-Tenant`; optional own API key (`PULSE_API_KEYS`).
  - **`services/atlas`** — the live management dashboard service (read-side BFF).
    `GET /v1/dashboard` (every vertical summarised: counts by stage/status, ₹ Cr
    amounts, open intel), `GET /v1/today` (due lead actions, lender chases, covenants
    due), `GET /v1/pipeline/{vertical}`, `GET /v1/entities/{id}/summary`. View-level
    RBAC through the Access service's admin-editable view matrix (TTL cache +
    last-known-good, same policy as the gateway); row-level security stays in the
    Register. Pure aggregation functions live in `app/aggregations.py` (unit-tested
    in isolation).
  - **Deploy**: compose grows to 12 services (PULSE :8004, ATLAS :8005); Helm umbrella
    grows to 10 vendored subcharts, every module behind an `enabled:` flag — install
    the whole platform, one module, or any subset (see the new
    **`docs/DEPLOYMENT.md`**: need-basis installs, public-cloud posture with managed
    Postgres/S3, multi-tenant onboarding, scaling guidance for 1000s of transactions).
  - **Docs for newcomers**: **`docs/ONBOARDING.md`** — the freshman tour: the
    60-second mental model, the five platform habits (env config, request-id JSON
    logs, problem-JSON errors, idempotent writes, optimistic locking), a worked
    first-change example, and a debugging checklist.
  - Tests: PULSE (matching unit tests + scan→intel→digest e2e vs a real Register) and
    ATLAS (aggregation unit tests + composed-view e2e). CI and `make ci` run both.

- **Three-service RBAC architecture — Gateway + Access service + Register (the agreed
  design, implemented).** Two NEW microservices, one rewire:
  - **`services/access`** — user management & access-control facts: `users`, `user_roles`
    and the **access matrix as admin-editable tables** (`access_grants` +
    `matrix_versions`), seeded from the spec artifact (now shared as
    `evam_backend_core.rbac`). Admin-only governance APIs with **guardrail cells**
    (delete/backup-restore/audit surfaces immutable even to Admin); every edit bumps a
    matrix version. `GET /v1/resolve` returns user → roles + effective matrices +
    version for the gateway's cache. Own `access` database on the shared Postgres.
  - **`services/gateway`** — the REST-API service: **cached binary RBAC gate**
    (route → operation map; NONE → 403 at the gateway, FULL/SCOPED forwarded with an
    `X-Authz-Decision` header), identity-header forwarding stamped with a shared secret,
    reverse proxy to the Register, and a composed `GET /v1/me` (Access facts + Register
    assignments). Stateless; the future home of client-specific logic. Facts are fetched
    from Access **on cache miss/TTL only — never per request** (last-known-good on
    outage).
  - **Register rewired** (migration `0004`, reversible): local `users`/`user_roles`
    dropped (identity lives in Access); identity arrives via gateway-verified headers
    (`X-Gateway-Auth` secret — spoofed identity on direct calls is rejected);
    `line_assignments` + `change_requests` + the **scoped enforcement stay next to the
    data**: scoped writes on the 5 line resources require an active assignment, scoped
    list access filters to assigned lines, delete stays Admin-only.
  - **Verified end-to-end**: 7 gateway e2e tests run the REAL three-service stack
    (register + access as live uvicorn servers on their own test DBs) covering CF1–CF7 —
    incl. **admin edits a matrix cell → new rule live with no deploy**, and the bypass
    wall. Plus 5 access-service tests and the register suite. Compose gains `access` +
    `gateway` (NGINX now fronts the gateway); Helm gains both subcharts (9 services /
    7 subcharts total).
- **User management & RBAC — the ATLAS RBAC spec (v3.1), implemented.** Four new tables
  (migration `0003`, reversible, RLS'd): `users` (the Employees governance table —
  @evamfinance.com e-mail enforced, active flag, `reports_to`), `user_roles` (role
  stacking across the 10-role catalogue; highest role wins), `line_assignments` (the
  assignment-driven permission primitive — assigning a user to a Lending/Syn/AM line
  grants write on THAT line until unassigned; co-assignees supported), and
  `change_requests` (the request → approve/reject stage-change flow).
  - **Matrices encoded verbatim** from the spec (`app/authz/matrix.py`): 13-view access
    matrix, 35-operation matrix, assignment authority (Credit Head owns the analyst
    pool), approval routing (Admin/Mgmt/relevant vertical Head), ownership defaults
    (unassigned line → its Head).
  - **Endpoints:** `/v1/users` (+ grant/revoke roles), `/v1/assignments` (+ end),
    `/v1/requests` (+ approve — which APPLIES the change with history/audit — + reject),
    `/v1/me` (effective views/operations/assignments — ATLAS renders its menu from
    this), `/v1/authz/check` (evaluate any operation, optionally against a line).
  - **Enforcement:** requests carrying `X-User-Email` are always checked (e.g. "Delete a
    row — Admin ONLY" now 403s Management); machine-to-machine API-key calls keep
    working, governed by `REGISTER_ENFORCE_RBAC` (default off). Bootstrap provisions
    `admin@evamfinance.com` (Admin+Management) so a fresh Register is governable.
  - 8 new tests (domain validation, stacking, cross-vertical assignment + revoke,
    authority denial, approval routing incl. wrong-vertical Head, applied stage change
    with auto-history, admin-only delete, inactive-user lockout). Suite: 68 passing.
- **Single Docker Compose file — whole platform, one command, ONE Postgres.** Merged
  `docker-compose.workflows.yml` into `docker-compose.yml`; a plain
  `docker compose up --build` now brings up everything: NGINX + Register + Postgres +
  MinIO + Temporal (server + UI) + the worker. The second Postgres container is gone —
  Temporal now persists to the shared Postgres in its own `temporal` /
  `temporal_visibility` databases (auto-created on first start), one server with a
  database per concern, matching the Helm umbrella. Name just the core services on the
  command line if you don't want the workflow plane. No second `-f` file, no `--profile`
  flag. (If an older run left stale containers, one `docker compose down
  --remove-orphans` resets the network.)
- **Object storage (S3 / MinIO) for document bytes.** The Register now *stores the bytes*,
  not just references. New `app/storage/` backend (boto3; works with AWS S3 and MinIO —
  same API, different endpoint), with blocking calls off the event loop.
  - **Upload endpoints:** `POST /v1/documents/upload` and nested
    `POST /v1/<subject>/{id}/documents/upload` (multipart) — the Register puts the bytes in
    the bucket (auto-created) and catalogs the resulting `storage_uri`.
  - **Download** (`GET /v1/documents/{id}/content`) redirects to a freshly-signed
    **presigned URL** (or streams through the API when configured); inline small files
    still stream directly.
  - **Backend switch:** `REGISTER_STORAGE_BACKEND=inline|s3` (+ `REGISTER_S3_*`); inline
    stays the dev default so nothing external is required.
  - **Deploy:** MinIO added to Docker Compose (console :9001) and as a vendored Helm
    subchart (`charts/minio`, PVC-backed); the Register subchart gains `storage.s3.*` and
    wires the secret. Production points `register.storage.s3.*` at a managed S3.
  - Verified with an in-process S3 mock (moto): put/get/presign/delete, bucket auto-create,
    and the full upload→catalog→presigned-download path (6 new tests).
- **Documents & the ATLAS "Data Register".** The catalog + checklist behind ATLAS's
  Data Register modal (17 required documents across 6 sections). Two new tables:
  - `documents` — one row per document on file: a **reference** (`storage_uri` into
    object storage) plus metadata (title, size, checksum, owner, time). Large-file **bytes
    live in object storage**; a bounded `inline_content` (default ≤400 KB, config
    `REGISTER_DOCUMENTS_INLINE_MAX_BYTES`) is the small-file fallback until MinIO/S3 is
    wired — mirroring ATLAS ("files up to 400 KB stay viewable, larger are recorded").
    Attaches to a polymorphic subject (Lead/Entity/Deal/…) and denormalises `entity_id`.
  - `document_checklist` — the per-tenant checklist **template** (sections + required
    slots), seeded with Evam's default 24-slot / 17-required list; configurable via
    `/v1/document-checklist`.
  - **Endpoints:** subject-aware `POST /v1/documents` + nested
    `GET/POST /v1/<subject>/{id}/documents`; the rollup
    `GET /v1/<subject>/{id}/data-register` (sections, per-slot on-file/pending,
    percent-complete — exactly what the modal renders); `GET /v1/document-checklist/template`;
    `GET /v1/documents/{id}/content` (streams inline bytes / redirects to an http(s)
    reference); plus generic CRUD for both tables.
  - **Company-wide (entity-scoped) access.** Documents are shared across ALL of a company's
    records: upload the COI once against the lead and it shows on the Data Register for the
    deal, the lending tracker, the syndication and the entity alike (the read side keys off
    the denormalised `entity_id`, like interactions). `?scope=subject` narrows to only what
    was attached to that exact record; `scope=auto` (default) is the company-wide view.
  - Migration `0002_documents` (reversible); shared polymorphic-subject resolver extracted
    to `app/repositories/subjects.py` (interactions + documents use one definition).
- **ATLAS MIS xlsx importer.** Load the authoritative 6-sheet MIS spreadsheet into the
  Register — `POST /v1/import/atlas-xlsx?mode=replace|merge` (upload) and
  `python -m app.seed.xlsx_cli <file>` (CLI). Maps every sheet to its table, dedups
  companies into `entities`, folds Mandate Tracker onto the syndication mandate field.
  Verified round-trip: 100% company coverage vs the source (260/260), Leads/Deals/
  Lending/Asset-Mon counts match exactly.
- **ATLAS coverage audit → schema/API hardening.** Cross-checked every ATLAS UI parameter
  (from `atlas_data.json`) against the schema at three layers (DB column → API read → API
  write) and closed the gaps so the ATLAS front-end works seamlessly:
  - **Entity tags** (`tags` JSONB + GIN index) — Core-33 / Adaptation-10 / showcase
    memberships, seeded from the ATLAS curated lists (43 entities tagged).
  - **Lending sanctioned-vs-drawn** — `disbursed_amount` + `disbursement_date`.
  - **Financials basis/scale** — `is_consolidated`, `is_audited`, `scale`; plus a **typed
    `data` line-item contract** (`FinancialLineItem`/`FinancialData`) so a statements grid
    binds to a real shape instead of an untyped blob.
  - **External-intelligence triage** — `acknowledged_by/at`, `is_dismissed`, with
    `POST /v1/external-intelligence/{id}/acknowledge|dismiss`; the dossier hides dismissed.
  - **Covenant compliance** — Monitoring gains `target_value`, `actual_value`, `breached`,
    `waiver_status`.
  - **Interaction attachments** — first-class `attachments` list (was buried in `meta`).
  - **Server-side stage/status history append** — changing a tracker's `stage`/`status`
    now auto-appends `{from,to,at,by}` to its history (was a client-overwritten blob →
    last-write-wins); append-only and concurrency-safe under the version guard.
  - **Embedded syndication lenders** — `SyndicationRead` now carries `lenders[]` inline
    (ATLAS row shape) via an eager, soft-delete-aware relationship.
  - **Per-tenant settings** — `GET/PUT /v1/settings` backs the ATLAS alert thresholds
    (`tenant_settings` table; built-in defaults merged on read).
  - **Derived lender matrix** — `GET /v1/entities/{id}/lender-matrix` rolls up lender
    posture from `syndication_lenders` (derived, never stored — the source had conflicts).
  - **Reference dropdowns** — added `Syndication Type`, `Mandate Status 3`, `Yes/No`,
    `Terminal (Lending)`, `RM`, `Analyst`, `Financial Section`, `Scale`, `Waiver Status`.
  - **Seed fidelity** — `people.started_on`, lead `created_at`, and tracker→deal linkage
    (61/61 lending, 74/74 syndication) now preserved. 11 new tests → **42 total**.
- **Tenant CRUD API.** New `/v1/tenants` endpoints (`POST` create, `GET` list, `GET/PATCH/
  DELETE {code}`) so tenants can be managed over the API, not only via `bootstrap`/SQL.
  These sit **above** tenancy: gated by `X-API-Key` alone, no `X-Tenant` header (the key is
  the admin credential). `code` is immutable; `DELETE` deactivates (soft — never orphans
  business rows), `PATCH {"is_active":true}` reactivates; changes are audited and invalidate
  the tenant cache immediately. 4 new tests (full lifecycle, 404, validation, auth) → 31 total.
- **Fresh-but-usable bootstrap.** New `bootstrap` step (`python -m app.seed.bootstrap`,
  entrypoint `migrate-bootstrap-serve`) provisions the default tenant + reference dropdowns
  and **no business data** — so tenant-scoped requests work on a fresh DB instead of failing
  `403 "Unknown or inactive tenant"`. It's now the Docker Compose default and the Helm
  production default (`migrations.bootstrap: true`). A bare `migrate-serve` still gives a
  totally empty DB; provision the tenant yourself with `python -m app.seed.bootstrap`.
- **Production posture: start fresh, no real data in the repo or image.** Docker Compose now
  comes up with **no business data** — nothing is auto-loaded on boot. The
  real consolidated MIS spreadsheet has been **removed from git and is `.gitignore`d /
  `.dockerignore`d** (`register/data/*.xlsx|xlsm|csv`), so no real financial data ships in the
  image. Load on demand at runtime: upload your own file via `POST /v1/import/atlas-xlsx`
  (recommended), or `docker compose cp` it in and run `python -m app.seed.xlsx_cli <path>`.
  The synthetic prototype mock (`data/atlas_data.json`, `python -m app.seed`) is kept for
  smoke tests. `import-mis`/`migrate-import-serve` now leave the DB empty (no synthetic
  fallback) when the file is absent.
- **DB → Excel / JSON export** for verification and backup: `GET /v1/export/excel`
  (one sheet per table), `GET /v1/export/json` (type-faithful), `GET /v1/export/counts`
  (row counts). Tenant-scoped; supports `?include_deleted` and `?tables=`.
- **Migrations squashed to a single baseline for the first release.** There is now one
  Alembic migration (`0001_initial_schema`) that creates the entire schema, including
  `interactions`. Alembic is kept as the runner (container/Helm/tests use
  `alembic upgrade head`); incremental versions (`0002`, `0003`, …) start only after the
  first release ships.
- **Interactions / Touchpoints merged into ONE table.** Removed the separate
  `touchpoints` table; the single `interactions` table is now PRISM master table 5
  ("Touchpoints" in the architecture, "interactions" in the ATLAS UI / VOX). VOX-rich
  fields folded in: `transcript`, `language`, `gps_lat`/`gps_lng`, `location`,
  `attendees`, `key_intel`, `next_steps`, `source_ref`.
  → Verify: `register/app/models/interactions.py` exists; there is **no**
  `register/app/models/*touchpoint*`; a fresh DB has an `interactions` table and no
  `touchpoints`.
- **Interactions are append-only** (create + read only; `PATCH`/`DELETE` return 405) to
  match the ATLAS modal's "Records are append-only".
- **Who added/updated tracking**: every interaction carries `performed_by` (who did it,
  the modal's PERSON) plus `created_by`/`updated_by`/`created_at`/`updated_at` (who logged
  it, from `X-Actor`) and an `audit_log` row.
- **Polymorphic interaction subjects** matching ATLAS `refType`/`refId`: log against a
  Lead, Deal, Entity, Counterparty, or a Lending / Syndication / Asset-Monetisation
  tracker. A Syndication interaction with a lender + direction updates that lender's
  response (inbound) / chased (outbound) date. VOX writes with `source:"VOX"`.
- **Helm**: single `prism` umbrella chart containing `charts/postgresql` (shared, free
  official `postgres` image — no Bitnami) and `charts/register`; `values-local.yaml` for a
  one-command local stack.
- **Docker**: fixed `.dockerignore` (keep `README.md`) and made `psycopg` a runtime dep so
  in-container migrations work.
- **Deploy layout**: Docker Compose + Helm both under `deploy/`.
- **QUICKSTART.md** added: run tests / build image / Docker Compose / Helm.

- **Helm now deploys the *whole* platform.** Added two vendored subcharts under the `prism`
  umbrella so `helm upgrade --install prism …` brings up everything, not just Register + DB:
  - **`temporal`** — dev/staging Temporal server (`auto-setup`) + optional Web UI, backed by
    the shared PostgreSQL in its own databases (production: point `temporal.datastore.*` at a
    dedicated instance). Service `prism-temporal:7233`.
  - **`workflows`** — the PRISM worker Deployment (runs `services/workflows`), wired to
    `prism-temporal` and the Register via env + secret; non-root, read-only rootfs.
  - Umbrella `Chart.yaml`/`values.yaml`/`values-local.yaml` updated with enable-conditions and
    stable service names (`prism-register`/`prism-temporal`/`prism-workflows`); NOTES + README
    refreshed. The in-cluster **edge** is the Register `ingress` (the NGINX role, via your
    ingress controller). All chart YAML validated.
- **NGINX edge + Temporal workflow engine — the Doors and Workflows rings, realized.**
  - **NGINX** reverse proxy in front of the Register (`deploy/nginx/nginx.conf` + a `nginx`
    service in Compose): TLS-ready, routing/load-balancing, **edge rate-limiting**,
    correlation-id minted at the boundary, security headers, gzip, timeouts. Edge on
    `:8080`, Register direct on `:8000`.
  - **`services/workflows`** — a Temporal worker service on `evam-backend-core`: durable
    workflows whose activities write the Register through `evam-register-client`. Reference
    `IngestInteractionWorkflow` (record interaction → read dossier) shows the pattern, with a
    workflow-derived idempotency key so **Temporal retries × idempotency = exactly-once
    effect** on the source of truth. The single compose file brings up
    Temporal + its *own* datastore + Web UI + the worker. 3 tests (activities on Temporal's
    ActivityEnvironment vs a mock Register; workflow on the in-memory test server, skipped
    offline). CI + Makefile now cover it; mypy/ruff clean.
- **Monorepo restructure + maintainability guardrails (team-scale readiness).**
  - **Layout**: `register/` → `services/register/`; the shape is now self-documenting —
    `services/*` (deployable) + `packages/*` (shared libs). Docker/compose/Helm/docs paths
    updated (build context stays repo root).
  - **CI** (`.github/workflows/ci.yml`): `ruff` + `mypy` + `pytest` (with a real Postgres)
    across the service and both packages, on every PR. Nothing merges red.
  - **Type gate is now green and enforced** — fixed the outstanding `mypy` findings and
    added `py.typed` markers to both packages; `mypy` passes on all three.
  - **Onboarding**: `CONTRIBUTING.md` (zero-to-productive runbook + how-tos), a root
    `Makefile` (`make install/lint/type/test/ci/new-service`), `.pre-commit-config.yaml`
    and `.editorconfig`.
  - **Decision records**: `docs/adr/` capturing the *why* (Register-first, entity-centric
    schema, optimistic concurrency, monorepo+shared-core, self-hosted Postgres, retry).
  - **Scaffolder**: `scripts/new_service.py` (`make new-service NAME=…`) spins a new vertical
    on `evam-backend-core` — verified it produces a lint-clean, buildable service.
- **`evam-register-client` — the shared Register SDK.** New `packages/evam-register-client`:
  a typed **async + sync** client every vertical (VOX / CIPHER / PULSE / gateway) uses to
  talk to the Register, so they all speak the contract identically. Built-in: auth headers,
  auto **Idempotency-Key** on creates (at-least-once safe), **optimistic concurrency**
  (`expected_version` → `If-Match`, → `VersionConflictError`), **transient retry** with
  backoff+jitter (network/timeout/429/502/503/504; writes only when idempotent), **request-id
  correlation**, **keyset pagination** (`Page` + `iterate`), and **typed errors** mapped from
  the RFC-9457 body. Vertical helpers: `log_interaction` (VOX), `create_financial_version`
  (CIPHER), `create_intelligence`/`acknowledge`/`dismiss` (PULSE), plus `dossier`,
  `lender_matrix`, `ref`, settings and tenant admin. 16 tests against a contract mock, and
  verified end-to-end against the real Register in-process.
- **Transient-error retry (production robustness).** New `RetryableRoute` transparently
  retries transient DB failures — deadlock (`40P01`) and serialization (`40001`) always
  (Postgres has rolled back, so it's safe), connection drops for reads only — with
  exponential backoff + jitter. Bound to every endpoint via `api_router()`; tuned by
  `REGISTER_DB_RETRY_*`. 5 new tests (classifier + retry/no-retry paths) → **47 total**.
- **Extracted `evam-backend-core` — the shared backend platform.** All cross-cutting
  concerns now live in `packages/evam-backend-core` (logging, RFC-9457 errors, request
  correlation, bounded pool + timeouts + retry, optimistic-locking CRUD, keyset pagination,
  health probes, and a one-call app factory). The Register is refactored to consume it as
  the reference implementation — its `app/core/*`, `app/db/*` and CRUD repo are now thin
  re-export shims; `Settings` subclasses `BaseServiceSettings`; `main.py` uses
  `create_service_app`. Future PRISM services inherit the whole stack. See
  `BACKEND_STANDARDS.md` and the runnable `examples/widget_service.py` (a full service in
  ~40 lines). Docker build context moved to the repo root so the image bundles the package.

## 0.1.0 — initial

- Register service: FastAPI + SQLAlchemy 2.0 async + asyncpg + Alembic + PostgreSQL 16.
- PRISM 7 master tables + ATLAS operational tables; tenant-aware, versioned, audited,
  soft-delete, RLS.
- Full CRUD per table (generic router), keyset pagination, optimistic locking,
  idempotency keys, structured logging, seed loader for the ATLAS mock dataset.
- Concurrency test suite (no lost updates / idempotent creates / deadlock-free / versioned
  financials), Postman collection, docs.
