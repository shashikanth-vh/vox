# Changelog — PRISM Register

Newest first. Use the top entry to confirm you have the latest build (the zip filename
carries the short git hash; match it against `git rev-parse --short HEAD` if you clone the
bundle, or just check that the newest item below is present in your copy).

## Unreleased (working branch: claude/register-service-postgres)

- **Committee approval is FACILITY-SPECIFIC.** `POST /v1/workflows/{id}/committee-decision`
  now takes either `facilities` (one outcome per lending line — approve/reject + its own
  note/conditions; must cover exactly the deal's lines, no gaps/unknowns/duplicates) or the
  grouped `approved` form — which is still RECORDED as a separate per-facility decision for
  every line, so the audit trail always answers per facility. The structuring workflow reads
  the per-line records (fail-closed: a missing line record = spoof, keep waiting) and acts on
  each facility's own outcome — sanction evidence + `Sanctioned` for approved lines,
  rejection evidence + `Rejected` for refused ones — reporting `Sanctioned` /
  `PartiallySanctioned` / `Rejected` with a `line_outcomes` map. A single deal-wide result
  never implicitly sanctions all lending lines. All 8 workflow input contracts now carry
  `schema_version` for worker/version compatibility.

- **RBAC authority model hardened for release 1: PostgreSQL decides, code enforces.**
  The compiled matrix is DEMOTED to a versioned reference (`rbac.py` split into
  `rbac_catalog` / `service_policy` / `lifecycle` with compatibility re-exports;
  `POLICY_VERSION` + `policy_fingerprint()`); it never decides a production request.
  The signed internal context now carries `policy_version`, the caller's revocation
  `epoch` and the signing-key id (`kid`). Gateway: the last-known-good resolve cache is
  BOUNDED (`GATEWAY_CACHE_MAX_STALE_S`, default 300s) — past it, FAIL CLOSED instead of
  serving stale grants through an Access outage. Access: users carry a revocation epoch
  (bumped on role grant/revoke and (de)activation), matrix cells carry provenance
  (`baseline` from the approved policy vs runtime `override`), every governance change
  lands on the append-only `access_audit` trail (DB-trigger immutable) stamped with the
  policy version (migration 0002); seeding is EXPLICIT (`python -m app.seed`, gated off
  at container start by `ACCESS_AUTO_SEED=false` in the production posture) and
  `--check` / `GET /v1/access/drift` compare the live matrix against the approved
  baseline without writing (exit 3 on drift for pipeline gating). Register: SENSITIVE
  operations (delete/restore, assignments, governed imports, evidence break-glass)
  revalidate ONLINE against Access under `REGISTER_ONLINE_REVALIDATION` (on in the
  production posture): user active, operation still granted, epoch unchanged; Access
  unreachable → 503 fail closed. New tests across core/access/gateway/register pin all
  of it; FOUNDATION_SPEC §3 documents the authority model.

- **Two-layer stage model, folded into the RELEASE BASELINE: the deal's stage IS the
  funnel; credit governance lives on the Lending line.** A Deal carries exactly one
  business stage — the commercial origination funnel (`New Inquiry → In Screening →
  In Pipeline → Closed Won/Lost`, plus `Screened Out`/`On Hold`), RM-owned, with its
  own ordered transition graph (rework back-steps, re-openable Screened Out, final
  Closed terminals). Because this ships pre-release, there are NO incremental
  migrations: the baseline schema (0001) creates `deals.stage` as the funnel (indexed)
  and no deal-level credit-stage column exists at all — a fresh install is born
  correct (recreate the dev database: `docker compose down -v && up -d`).
  ALL credit governance now keys on the LENDING line: `DealStructuringWorkflow` walks
  the lending line(s) to Note Circulated, files credit-note/committee/sanction (and
  rejection) evidence against the Lending subject citing the per-line subject-bound
  decisions, sanctions the lines, and fails clearly (`NoLendingLine`) when a deal has
  no lending line; the sanction basics (product_type/rm) land on the deal as plain
  data via a new `update_fields` activity. `MANDATORY_FOR_STAGE`/`EVIDENCE_FOR_STAGE`
  Deal entries removed; the sanctioned-`rm` field lock moved to Lending. Lead
  conversion (endpoint + workflow) creates deals at funnel `In Pipeline`. The importer
  routes the Deals sheet purely to the funnel (a credit word there quarantines by
  name). Exports hide the legacy column; ATLAS "deals by stage" now groups the funnel;
  OpenAPI/Postman/E2E collections regenerated; register/workflows/atlas tests
  rewritten to the two-layer model. `docs/FOUNDATION_SPEC.md` §4/§5/§6 and
  `docs/MIS_IMPORT.md` updated.
- **Team guide `docs/MIS_IMPORT.md`** — the workbook's structure, the two stage
  languages, what the lossless import does, and the fill-the-sheet rules the team
  follows (allowed values, don't-invent-stages, how to read the import report).
- **MIS v4 imports LOSSLESSLY: funnel dimension + normalisation — zero omissions.**
  The v4 Deals sheet speaks the ORIGINATION FUNNEL (New Inquiry / In Screening / In
  Pipeline / Screened Out / Closed Won / Closed Lost / On Hold) — a different layer
  from the governed bank/NBFC credit pipeline, which stays untouched (structuring
  workflow, evidence gates, committee governance all keyed to credit stages). Deals
  gain a funnel-stage dimension (now the baseline `deals.stage`; schema-validated vocabulary;
  `/v1/ref` "Deal Funnel Stage"): the sheet's value lands VERBATIM there, and the
  credit `stage` is set only where the value IS credit semantics (On Hold). The
  importer also gains **case/whitespace-insensitive canonicalisation** with curated
  wording aliases (`IP received → IP Received`, `IM under preparation → IM in Prep`,
  `IM Sent → IM Circulated`, `Final sanction received → Sanctioned`) — every
  normalisation is RECORDED in the import report's new `translated` list. Syndication
  semantics fixed for v4: the per-bank "Status" drives the tracker's pipeline position
  (previously the coarse "Deal Status" column quarantined ALL 162 rows); "Deal
  Dropped/Deal Closed" overlay the Dropped/Disbursed terminals; a live deal with no
  bank status enters at Deal Sourced. Unknown future values still quarantine with a
  named reason (fail-closed drift alarm). Verified against the real v4 workbook: 127
  deals + 162 syndication + 61 lending + 26 asset-mon lifecycle rows, **0 quarantined**;
  pure-function tests pin the v4 value census (run DB-less via REGISTER_TESTS_NO_DB=1).

- **Intelligence features are now independently switchable.** Four env flags —
  `VOCX_STT_PRIMING`, `VOCX_EXTRACT_GLOSSARY`, `VOCX_EXTRACT_FEW_SHOT`,
  `VOCX_EXTRACT_STRUCTURED` — each defaulting ON, wired through compose
  (`${VAR:-true}` passthroughs), the Helm vocx chart (`pipeline.intelligence.*`),
  and a config.json `intelligence` block for standalone use (env wins). Disabling a
  flag reverts exactly that one feature to pre-Batch-1 behaviour; a test proves the
  full off→on round trip.

- **VocX intelligence Batch 1 + eval harness.** (1) *STT vocabulary priming*: the STT
  service and both VocX backends accept a priming `prompt` (Whisper `initial_prompt`);
  VocX builds it per capture from `config.stt.vocabulary` (32 finance/climate terms) +
  the live Register client/lead names (names last — Whisper reads only the tail).
  (2) *Evam glossary* in the extraction prompt (`config.glossary`): business lines,
  lifecycle vocabularies, IM/IP/NBO/CP-CS/PPA/SECI/discom/VGF/CBG expanded — signals
  land in fields, not prose. (3) *Few-shot worked examples* (`config.few_shot_examples`:
  lending, syndication chase, AM teaser) set report depth/shape; swap in real
  RM-approved reports over time. (4) *Enforced structured output*: extraction is a
  forced tool call validated against `EXTRACTION_SCHEMA`, with the lenient text parse
  kept as fallback. (5) *Eval harness* `services/vocx/evals/` — 6 scored cases (EN +
  Hinglish, all three business lines), `python -m evals.run` live / `--offline` smoke;
  run before/after any prompt or model change. Tests: prompt carries glossary+examples;
  capture primes the transcriber with vocabulary + corpus names in the right order;
  STT service accepts the prompt field.

- **Prod-posture overlay: multi-issuer via `.env`.** `GATEWAY_OIDC_ISSUERS` /
  `WORKFLOWS_OIDC_ISSUERS` are now env passthroughs, so Google + Dex side by side is a
  two-line `.env` entry (issuer|audience pairs) instead of a YAML edit — keep
  `--profile sso` so Dex is running for its tokens to verify.

- **Dev console: the calendar banner now tells the truth per RM.** It checked only the
  deployment flag (client secret mounted) and promised an event even when the RM had
  never connected Google — the commit then skipped `calendar_create_event`. The card
  now queries `auth/status?rm=` and shows either the green "will be added to <RM>'s
  calendar" banner or an amber "not connected — Connect now, then Approve" warning
  with the connect link inline.

- **VocX Postman collection works under Dex too** — request 00 does the password-grant
  sign-in (identical skip-guard pattern to the E2E journey: `dexUrl` empty ⇒ skips,
  header trust; set ⇒ every call carries the captured ID token as `vocxToken`). Audit
  clean (0 blocking): the commit example no longer ships a `log_to` with an unset
  dealId, and playback skips itself for typed captures (no recording) instead of
  failing a --bail run.

- **VocX API integration reference** (`docs/VOCX_API.md`) — the complete REST contract
  for the real PRISM/VOM UI: auth/headers through the edge, the five-call UI flow,
  every endpoint with request/response examples (capture incl. GPS/language/capture_id
  reuse, suggest, save/commit with idempotency semantics, reports incl. print,
  streaming audio playback with the dual-shape handler, template_fill, Google auth,
  the read-side log), plus the full endpoint index. Postman `PRISM_VOCX` collection
  regenerated: 15 requests (adds suggest, reports/get, reports/print, template_fill;
  GPS on capture; streaming-playback notes).

- **Fix: a capture with no client timestamp stored its audio at a CONSTANT key**
  (`captures/0000/00/capture_<rm>.wav`) — every new recording silently overwrote the
  RM's previous one. Empty timestamps now default to NOW (UTC), giving correct
  year/month partitions and unique keys.

- **Dev console restyled as VOM.** The temporary test UI now looks and flows like the
  real VOM app: Record / Reports / Calendar tabs; "Tap. Speak. Done." record screen
  (pulsing mic, 90-second cap + timer, Type-instead, live GPS chip); a full report
  card — status badge, Hot/Warm/Cold, Log-To chips, Download PDF / Approve / Save
  changes, original-audio player, editable summary / key-intel / nuances / next steps
  / attendees, next-meeting with the calendar banner, collapsible transcript with
  re-analyze (same capture id), Additional details with the ATLAS-entity typeahead
  (create-new-company offer), template-field chips + ✨ auto-fill, opportunity score;
  Reports tab lists drafts→ready→committed with open/print/delete; Calendar tab shows
  Google status/connect/test. Same single file, same VOCX_DEV_UI flag, same removal.

- **English-at-rest: capture in ANY language, store English.** Whisper's built-in
  `translate` task is now the default end to end — the STT service accepts
  `task=transcribe|translate` (validated), both VocX backends send `translate`
  (config `stt.task`), so the transcript text is English no matter what was spoken
  (identity for English input; guaranteed even when Claude extraction is offline).
  The DETECTED spoken language still lands in the interaction's `language` column.
  Extraction prompt now also mandates English for every produced field (summaries,
  bullets, next steps — proper nouns kept as-is). Tests pin the contract from both
  sides.

- **Audio playback fixed: recordings now STREAM through VocX** (`/vocx/v1/audio`) —
  the dev console is HTTPS and browsers block plain-http MinIO presigned links as
  mixed content (and `localhost:9000` links never worked from another machine).
  Default playback fetches the object server-side and streams the bytes over the
  edge's HTTPS behind the normal auth; presigned URLs are opt-in
  (`VOCX_AUDIO_PRESIGN=true`, for TLS-fronted object storage). Foreign bucket/prefix
  refs stay refused in both modes.
- **Company typeahead** (`GET /vocx/v1/suggest?q=&rm=`) — ranks the live corpus
  (entities + open leads) with the SAME scorer capture resolution uses (incl. the
  own-client boost); `new_company: true` when nothing is even a weak match, so the UI
  offers "create as new company" instead of a wrong link. Demoed in the dev console
  (Company lookup box).

- **Committed interactions now land in the Register's STRUCTURED columns** (the
  interactions table was designed for this — VocX finally uses it). Per commit:
  `transcript` + `language`, `gps_lat`/`gps_lng`/`location` (new GPS/language
  passthrough on both capture endpoints; junk coordinates dropped, never fatal),
  `attendees`, `key_intel` (bullets + nuances + deal facts + template fields as JSON),
  `next_steps`, `next_action(_date)`, `next_meeting_date`, **`performed_by` = the
  capturing RM** (the service key is only transport), `contact_name`,
  `source="VOX"`, `source_ref` = capture id, the recording as a first-class
  **attachment** (no more "Recording:" line in prose), and `meta` (opportunity score,
  pipeline stage, deal temp, business line). **Notes is now the lean human
  narrative** — tag line, summary, "captured by <RM> via VocX" — the RM's field, not
  a machine dump (the timeline is append-only by design; RMs shape notes in the
  report card before Approve). Key-intel extraction tightened: every bullet must be
  self-contained (amount + concrete date + who). Tests assert the structured row.

- **Report print view** (`GET /v1/reports/print?rm=&id=`) — the stored report rendered
  as print-ready HTML (VocX · EVAM FIELD INTEL header, summary, key intel, details
  incl. labeled template fields, next steps, nuances, attendees, full transcript;
  content HTML-escaped). The PoC's "Download PDF" is the browser printing exactly this
  view, so parity = serve it (browser → Ctrl+P → PDF; a Print button is hidden in
  print media). Dev console report rows link to it. Full-card parity audit result:
  everything else in the VOM report card already exists in the backend — the report
  schema (summary/key-intel/nuances/next-steps/attendees/additional details/pipeline
  stage/opportunity score/deal temperature), the template catalog + /v1/template_fill
  ("Auto-fill from transcript"), re-analyze (POST /v1/capture accepts capture_id so
  the SAME draft updates), and the commit note carries tags, deal facts, labeled
  template extras, next steps, nuances and attendees.

- **Dedicated STT service (`services/stt`)** — speech-to-text moved out of the VocX
  process into its own container: faster-whisper behind an OpenAI-compatible
  `POST /v1/audio/transcriptions` (multipart; Bearer/X-API-Key front door; 25 MB cap;
  stub engine for tests). ONE shared model instance per pod instead of one per VocX
  worker; the model is downloaded at **image build time** (`--build-arg
  STT_MODEL_SIZE=small`) and the container serves with `HF_HUB_OFFLINE=1` — no runtime
  dependency on huggingface.co, no first-request stall, air-gap friendly. VocX now
  defaults to `VOCX_STT_BACKEND=api` (its `APITranscriber` ported from requests to
  httpx + bounded retries; 4xx surface immediately) and its image no longer bakes
  faster-whisper (slimmer builds; the `[stt]` extra remains an opt-in fallback).
  Wired everywhere: compose service `stt` (internal-only, no host port), Helm subchart
  `charts/stt` (+ umbrella dependency, readiness gates on the preloaded model, vocx
  chart gets `pipeline.stt.apiUrl/apiKey`), CI (install/lint/mypy/pytest). Contract
  pinned from both sides: STT service tests (multipart/auth/caps) and a VocX
  APITranscriber test against the same shapes.

- **Fix: first Whisper model download died with `Permission denied`.** The model obeys
  `download_root` (the volume), but huggingface_hub's xet backend keeps its chunk cache
  under `$HOME/.cache/huggingface` — and the container's system user has no home. The
  image now sets `HF_HOME=/data/vocx/hf`, so every HF artefact (model, chunk cache,
  logs) lives on the state volume. Rebuild vocx: `docker compose ... up -d --build vocx`.

- **Gateway error titles now match the status.** A 401 ("Authentication required")
  was labelled `bad_gateway / "Upstream unavailable"`, which sent debugging in exactly
  the wrong direction. 401 → `unauthorized`, 403 → `forbidden`, everything else keeps
  the upstream wording.

- **Fix: VocX captures 500'd — svc_vox could not read the deal book.** The pipeline's
  resolution corpus reads `/v1/entities` + `/v1/leads` + `/v1/deals`, but `svc_vox`'s
  own-key read grant predated the pipeline and stopped at leads, so the Register 403'd
  the deals read and every `/vocx/v1/*` call died at corpus build. The grant now covers
  every book a touchpoint can land on — `/v1/deals` plus `/v1/lending`,
  `/v1/syndication`, `/v1/asset-monetisation` (the commit-time `log_to` targets) —
  read-only; the write stays log_interaction only. Regression test asserts all six
  corpus reads are 200 on the svc_vox key while `/v1/financials` / `/v1/documents`
  stay 403. **Rebuild the
  Register** (the grant lives in `evam-backend-core`, baked into its image):
  `docker compose -f deploy/compose/docker-compose.yml up -d --build register`.

- **TEMPORARY VocX dev test console** at `https://<host>:8443/vocx/v1/dev-ui` — a single
  self-contained page served by VocX itself for real end-to-end testing from a browser:
  record (MediaRecorder) or type a transcript → preview with entity match/alternatives/
  new-lead choice, summary + next-meeting edits, Log-To targeting → commit with the
  per-op write results → server-side reports list (open/delete) and audio playback.
  Gated by `VOCX_DEV_UI` (dev compose defaults it ON; the prod-posture overlay pins it
  OFF; the code default is OFF), hidden from OpenAPI so it never enters generated
  collections, and behind the same front-door key as every pipeline route. **To remove
  permanently:** delete `services/vocx/app/vocx/dev_ui.html` + the dev-ui block at the
  bottom of `app/vocx/mount.py` (and the `VOCX_DEV_UI` lines in the compose files).
  Test: hidden-unless-enabled + out-of-schema. Also: vendored-file ruff exemption
  extended (`UP038`, `N802`) so the suite stays green across ruff versions.

- **VocX pipeline API versioned + the three UI-parity features.** The pipeline moved from
  `/api/vocx/*` to **`/v1/*`** (`/vocx/v1/…` through the edge — consistent with every other
  PRISM API), and the catch-all adapter route was replaced by an **explicit route table**, so all
  19 endpoints are real OpenAPI operations (generated collections enumerate them) and nothing can
  shadow `/v1/touchpoints`. The gateway's OAuth exemption follows (`/vocx/v1/auth/callback`).
  - **Server-side reports** (`app/vocx/reports.py`): the RM's pending-capture list is a backend
    fact — MinIO JSON under `reports/<rm>/<capture_id>.json` (captures bucket, volume fallback,
    atomic local writes). Every preview **auto-saves a draft**, save marks `ready`, a successful
    commit stores the write results as `committed` (further saves 409). List/get/save/delete
    endpoints; 512 KB cap; ids validated.
  - **Recorded-audio playback** (`GET /v1/audio?ref=`): MinIO refs answer a **presigned GET**
    signed against the browser-reachable endpoint (`VOCX_S3_PUBLIC_ENDPOINT_URL`); refs outside
    OUR bucket + captures prefix are refused (never a generic presigner); volume refs stream
    bytes behind a realpath traversal guard.
  - **Explicit "Log To"** on commit: `log_to {subject_type, subject_id}` targets the interaction
    at a chosen Lending/Syndication/AssetMonetisation/Deal/Lead/Entity row (validated: allowlist
    + UUID; 400 otherwise) instead of the resolver's automatic routing.
  - 14 pipeline tests (report lifecycle incl. the 409-after-commit rule, own-bucket-only
    presigning, path-traversal refusal, log_to targeting + validation); gateway exemption tests
    updated; ruff + mypy green. (This drop also restores the a9ef508 logging fix onto the
    28e31be base after the workspace loss.)


- **`app/vocx` restructured into responsibility-based subpackages** — the 19-file flat module
  became `core/` (the pipeline engine: pipeline · extract · resolve · gate · server · atlas ·
  store · search), `speech/` (stt + audio_store), `registry/` (the Register store/writer
  adapters) and `google/` (oauth · workspace · notes · drive_writer), with `mount.py` /
  `loader.py` / `config.json` at the top. Imports were rewritten to absolute aliased form
  (`from app.vocx.core import gate as vocx_gate`), so every call site inside the vendored code
  is untouched — the restructure is import-lines-only and mechanically reviewable. Lint/mypy
  exemptions now follow the directories (engine relaxed; PRISM-owned modules strict). Fixed on
  the way: `load_config`'s fallback resolved `config.json` against the wrong directory after the
  move, and a stray `vox.log` from the PoC was removed. 10 tests, ruff, mypy: all green.


- **Recorded audio now lives in MinIO, and every committed interaction points back at it.**
  The platform rule — bytes in object storage, references in the record — now covers voice
  captures, not just documents.
  - **`app/vocx/audio_store.py` (new).** `S3AudioStore` PUTs each capture to the
    `prism-vocx-captures` bucket (keys partitioned `captures/<YYYY>/<MM>/…`, boto3 with bounded
    retries/timeouts, lazy thread-safe client, auto-create bucket) and returns the canonical
    `s3://bucket/key` URI; `LocalAudioStore` (the volume) is the no-S3 default AND the safety
    net — **a failed S3 PUT degrades to the volume, a recording is never discarded**. BOTH
    capture paths archive now (raw-audio endpoint and inline `audio_b64` — the latter previously
    didn't). Fixed while building: dashed ISO timestamps broke the month partition (`2026/00/…`).
  - **The commit's interaction carries `Recording: s3://…` in its notes**, so the register row —
    the audit record — always references what was actually said. Covered by an end-to-end test
    (audio in → stubbed S3 → commit → notes assertion).
  - **Retention is enforced where the bytes live**: `VOCX_AUDIO_RETENTION_DAYS > 0` becomes a
    bucket LIFECYCLE rule on the captures prefix (applied by the store, tolerated where the S3
    implementation lacks lifecycle); the local tier sweeps opportunistically. 0 = keep forever.
  - Wiring: compose points VocX at the stack's MinIO out of the box (`VOCX_S3_*`,
    `depends_on: minio`); the Helm chart gains `pipeline.audio` (endpoint, bucket, retention,
    credentials via existingSecret); `capabilities` reports `audio_store: s3|local|off`.
    12 pipeline tests, ruff + mypy green.


- **The voice pipeline is production-grade — and consistently named `vocx`.** Package
  `app/vocx` (modules `vocx_*`), endpoints `/vocx/api/vocx/*`, env vars `VOCX_TOKENS_DIR` /
  `VOCX_OAUTH_REDIRECT_URI` / `VOCX_STT_BACKEND` / `VOCX_GOOGLE_CLIENT_SECRET_FILE`, volume
  `vocx_state`, secrets dir `deploy/vocx-secrets/`.
  - **Idempotent commits.** Preview mints `_meta.capture_id`; the commit's Register writes carry
    `Idempotency-Key: vocx:<capture_id>:<op>`, so a client retry / double-tap REPLAYS the original
    rows instead of duplicating the interaction — closing the caveat called out at integration.
    Keyed writes retry with backoff on transport errors/5xx; 4xx refusals never retry.
  - **Scalability.** The resolution corpus (entities+leads+deals, a handful of list calls) and the
    search view (adds the per-subject interaction log) are cached SEPARATELY with their own TTLs —
    capture latency no longer pays for the interaction log as the book grows. Register reads retry
    with backoff.
  - **faster-whisper ships in the image** (`pip install .[stt]` in the Dockerfile): every
    deployment takes audio out of the box. Only the library is baked — the model (default `small`,
    CPU int8, tunable) downloads once into the vocx VOLUME and is reused across restarts and
    replicas. Transcription is serialized per process (ctranslate2 thread-safety); gunicorn runs
    2 workers × 4 threads with a 120 s timeout. Input caps: 25 MB audio, 40k-char transcripts.
  - **Multi-replica-safe OAuth.** The PKCE verifier now persists on the vocx volume (15-min TTL)
    instead of process memory, so a restart or a second replica between /auth/start and the
    callback no longer breaks the round-trip. The gateway exempts EXACTLY
    `/vocx/api/vocx/auth/callback` from require_auth (Google's redirect carries no bearer) — safe
    because completing the exchange needs the verifier persisted by an authenticated start; the
    exempt list is configurable (`GATEWAY_AUTH_EXEMPT_PATHS`) and covered by a gateway test.
  - **Containers log JSON to stdout** — the PoC's rotating `vox.log` file is gone. Helm: the vocx
    chart gains `pipeline:` values (Anthropic key via secret, state PVC, Google secret mount,
    redirect URI, STT backend); chart + umbrella render verified.
  - Tests: 9 pipeline tests (idempotency-key equality across a double commit; oversized inputs
    413) + the gateway exemption test; ruff + mypy fully green.


- **The VOX voice-capture pipeline is now part of the PRISM backend** (`services/vocx/app/vox`,
  vendored from the field PoC — backend only, no panel/PWA). Endpoints under
  `/vocx/api/vox/*` through the edge:
  - **`POST capture` / `capture_audio`** — transcript (or audio → STT) → Claude-Haiku
    extraction (offline stub without `ANTHROPIC_API_KEY`) → entity resolution **against the
    live Register** (entities + active leads + deal-RM ownership, TTL-cached; no more JSON
    fixture) → the per-critical-field confidence gate → a preview: extraction, decision,
    approval card, write plan. Preview never writes.
  - **`POST commit`** — the approved (possibly edited) capture executes its plan: interaction
    appended / lead created in the REGISTER as `svc_vox` (LD-V numbering preserved in
    `lead_no`; client codes and minted ids translated to real rows), a follow-up event on the
    speaking RM's own Google Calendar when they've connected (per-RM OAuth under
    `auth/start|callback|status`, tokens on a volume), Drive off by default. Register writes
    are REAL on every commit — a missing Google token skips the calendar op, it no longer
    demotes the whole plan to a mock.
  - `capabilities`, `interactions`, `facets`, `entity`, `template_fill` round out the surface.
    STT backends: faster-whisper (optional `[stt]` extra), Whisper-compatible API, stub.
  - Fixed while porting: an EMPTY Google client-secret path reported `google_configured: true`
    (`os.path.join(HERE, "")` is the package dir); lead-id minting scanned `id` (a Register
    UUID here) instead of `lead_no`, so every VOX lead would have been `LD-V01`.
  - Tests (no key, no Google, no model, no DB): preview resolves against a stubbed Register,
    commit writes the interaction to the right subject, new-lead commit creates the lead and
    logs on it, capabilities reports the degraded truth, blob mapping + minting. Secrets
    hygiene: client secrets / tokens are git-ignored and env-mounted; nothing from the PoC's
    `client_secret.json`, token files or `RESTORE_SECRETS.txt` was carried over.


- **E2E collection: folder 00 was erasing the fixed identities — and a run-order auditor now makes
  the whole class impossible.** The run-setup request cleared `makerEmail` / `checkerEmail` along
  with the derived ids, and nothing rewrote them: the MAKER/CHECKER creates sent a literal
  `{{makerEmail}}`, failed e-mail validation, the id resolves found nobody, and
  `POST /v1/leads/{id}/convert` 422'd on an empty `analyst_id` (caught live). The RM path survived
  only because `rmEmail` wasn't in the clear list.
  - The clear list now holds **derived state only**; the fixed identities survive, and folder 00
    self-heals them if a hand-edited environment lost them. Vestigial `bdrmEmail` removed.
  - **`scripts/audit_postman.py` (new): a static run-order audit.** It simulates a top-to-bottom
    Collection Runner pass — every `pm.environment.set`/`unset` in every script, every `{{var}}`
    consumed in URLs/headers/bodies — and classifies findings (HARD never-written / ORDER
    written-later / UNSET cleared-then-used / COND only-written-inside-if / EMPTY). This exact bug
    reports as 15 blocking findings on the previous collection; the fixed one audits clean.
  - The generator now **refuses to emit** a collection with blocking findings (both environments
    audited at generation time), and CI runs the same audit on the committed artifacts.


- **Dex rejected every sign-in: the config's bcrypt hash encoded `"password"`, not `"prism"`.**
  All five static identities shared a hash lifted from the Dex documentation example — which is
  the hash of the literal string `password` — while the comment above it, `ssoPassword` in both
  Postman environments, and every doc said `prism`. Result: folder 00b failed with Dex's own
  `access_denied / Invalid username or password` on every identity (caught live on a user's VM).
  All five hashes replaced with a verified bcrypt of `prism`, and CI now **checks every
  `staticPasswords` hash verifies against the documented password** (`bcrypt.checkpw`) so a
  non-verifying hash can never ship silently again. To pick the fix up on a running stack the
  `dex` container must be recreated — a config-file edit alone changes nothing until then.

- **HTTPS is the one front door everywhere — and the Dex/compose path is turnkey.** The edge
  already terminated TLS on `:8443` with `:8080` reduced to a 301, but several consumers still
  pointed at `:8080`, where a followed 301 replays a POST as a GET (every create silently becomes a
  list).
  - **CI (`e2e.yml`) actually goes through the edge now.** The dev-posture newman run targeted the
    NGINX port without ever *starting* NGINX (it wasn't in the first `up` list), no step generated
    the TLS certs NGINX refuses to start without, and both runs used `http://…:8080`. Now: certs
    are generated first, `nginx` is in both service lists, an edge health-wait precedes newman, and
    both runs enter at `https://localhost:8443` (`--insecure` for the self-signed dev cert).
  - **`postman/PRISM_Full_Dex.postman_environment.json` (new, generated).** The prod-posture run no
    longer needs a hand-edited environment: this one is identical to the dev environment except
    `dexUrl` is pre-filled — the single switch that turns folder 00b (sign-in) on. A hand-copy that
    misses a variable sends literal `{{adminToken}}` text and 401s in a way that looks like a
    platform bug; both shipped environments are asserted to carry the identical variable set. The
    CI prod-posture run uses it.
  - **`scripts/gen_dev_certs.sh` learned `EXTRA_SANS`** (`EXTRA_SANS="IP:192.168.44.128" … --force`)
    so a stack reached from another machine — Postman on the host, PRISM in a VM — can have the
    VM's address in the certificate SANs and keep TLS verification ON, instead of being forced to
    disable it. Invalid entries are refused with an explanation.
  - Docs: POSTMAN.md's file table now leads with the three E2E files and both environments; the
    stale `http://localhost:8080` base-URL diagram is corrected; a "Postman on one machine, PRISM
    in a VM" section covers the IP swap + SAN regeneration; README/QUICKSTART put
    `scripts/gen_dev_certs.sh` before every compose `up` (NGINX will not start without certs).
  - Verified here: both environments generated and asserted identical modulo `dexUrl`; folder 00b
    proven to make **zero outbound requests** under the dev environment and real sign-in attempts
    under the Dex one; collection parses (68 items); merged compose config valid with all 13
    services; workflow YAML valid; cert script exercised with and without `EXTRA_SANS` and with a
    malformed entry.

- **Two identity providers at once: Google for production, Dex for dev/test.** Previously each
  service accepted exactly one issuer, so choosing Google for staff meant losing the local
  password-grant IdP the Postman journey signs in with. Both are now supported side by side, with
  the membership check that a public IdP makes mandatory.
  - **`MultiIssuerVerifier` (`evam_backend_core.oidc`).** Accepts a registry of issuers configured
    as `"issuer|audience,issuer2|audience2"`. A token is verified **only by the verifier whose
    `iss` claim matches** — never by trying each in turn, so a weaker issuer can never vouch for
    another's audience. An unrecognised `iss` is refused outright.
  - **E-mail domain allowlist (`*_OIDC_ALLOWED_DOMAINS`) — the control a consumer IdP requires.**
    A valid Google token proves the account is *real*, not that it belongs to Evam: any
    `@gmail.com` would otherwise authenticate and be stopped only later, by the user lookup. The
    domain is now checked during **authentication**, so a non-organisation identity never reaches
    authorization at all. Empty = no restriction (the dev default, so local setups are unaffected).
  - **`build_verifier()`** is the single construction path, used identically by the gateway,
    orchestrator and ATLAS. It returns `None` when nothing is configured, which is exactly what
    preserves the existing dev header-trust behaviour.
  - **ATLAS was still building its verifier by hand** and so would have accepted only one issuer,
    and no allowlist, even after the gateway was fixed. It now uses `build_verifier` too, verified
    across all four configurations (unset / single / multi / allowlist).
  - **The Helm charts did not render the new keys**, which would have made the `values-prod.yaml`
    entries silently inert. `gateway`, `workflows` and `atlas` now emit `*_OIDC_ISSUERS`,
    `*_OIDC_ALLOWED_DOMAINS` and `*_OIDC_EMAIL_CLAIM`; confirmed by rendering the production
    overlay and reading the values back off the containers.
  - **The chart now refuses to render an unsafe combination.** Accepting a public issuer
    (Google/Microsoft) with an empty `allowedDomains` fails the render with an explanation, per
    service — a misconfiguration that would otherwise deploy quietly and authenticate anyone. The
    `requireAuth` guard was also widened: it previously demanded the *single* issuer setting and so
    rejected a valid multi-issuer-only configuration.
  - **Postman: folder `00c · Sign in (Google)`.** Google publishes no password grant, so the
    collection uses the **refresh-token** grant and takes the **`id_token`** (Google's access
    tokens are opaque and carry no verifiable claims). Disabled by default, so the Dex-based dev
    run is unchanged. Keep client secrets and refresh tokens in the Postman **Vault** or a secret
    environment variable — never in an exported collection.
  - Tests: `packages/evam-backend-core/tests/test_oidc_multi_issuer.py`, 14 cases, asserting the
    **refusals** as hard as the successes (unknown issuer, no cross-verifier fallthrough, malformed
    token, `@gmail.com` rejected despite a valid signature). CI gained the four render assertions.

- **Production-posture verification, CI enforcement, and two control hardenings.** The green E2E
  proved the business flow but ran with four controls at their dev default of `false`, so the
  controls themselves were unproven.
  - **`deploy/compose/docker-compose.prod-posture.yml` (new).** An overlay that turns on
    `GATEWAY_REQUIRE_AUTH` + OIDC issuer, `WORKFLOWS_REQUIRE_AUTH` + issuer,
    `REGISTER_ENFORCE_RBAC` and `REGISTER_ENFORCE_RLS`. It must be used **with `--profile sso`** to
    start Dex: an override cannot un-gate a profile-gated service (Compose filters profiled services
    out before merging, so `profiles: []` has no effect — verified on Compose v5). Without the flag
    the stack runs with REQUIRE_AUTH on and no reachable issuer, and every request 401s. Merged
    config validated, dex confirmed present in the service list.
  - **The E2E collection now runs in BOTH postures unchanged.** Every request carries
    `Authorization: Bearer {{…Token}}` alongside `X-User-Email`: empty token ⇒ the gateway falls
    back to header trust (dev), a verified token wins under `REQUIRE_AUTH` (prod). New folder
    **00b · Sign in (Dex)** obtains ID tokens via the password grant and is tolerant when Dex is
    absent.
  - **Fixed E2E identities + idempotent provisioning.** A bearer can only be issued for an identity
    the IdP knows, and maker-checker needs two distinct verified people — so the journey uses stable
    e-mails (`e2e.rm@`, `e2e.maker@`, `e2e.checker@`, added to `dex/config.yaml`) instead of per-run
    generated ones. Creates accept **201 or 409** and the id is resolved with
    `GET /access/v1/users?q=<email>`, so re-runs work against `UNIQUE(tenant_id, email)`.
  - **CI runs the collection with newman, twice** (dev default and prod posture), `--bail`, reports
    uploaded as artifacts. The three integration bugs found by hand this week would all have failed
    the build.
  - **Governance provenance is now VERIFIABLE, not merely present (control fix).** `executed_agreement`
    required a `workflow_id` + `run_id` but never checked them, so a caller could cite an invented
    run and have it recorded as provenance. The cited workflow must now resolve to a decision
    recorded for **this tenant and this subject**; a mismatched subject is refused. The collection
    accordingly cites the per-line committee decision (`{workflow_id}:lending:{line_id}`).
  - **A swallowed lookup no longer hides a divergence.** If the orchestrator cannot list a deal's
    lending lines, the deal sanctions while its facility does not. That path logged a WARNING; it is
    now an **ERROR** carrying the operational impact and the remedy (re-send the committee decision).
  - Deferred, as agreed: the VocX identity-propagation gap and name-only company matching.

- **E2E collection: the audit check filtered on the wrong `resource_type` (returned `[]`).** The
  audit log records `resource_type` as the **table name** — `CRUDRepository.resource =
  model.__tablename__`, so `lending_tracker` — not the URL segment `lending` nor the subject_type
  `Lending`, and different writers use different conventions (`governance_evidence`,
  `cp_cs_checklists`, `advaya_handover_packages`, `Lending` for the break-glass path). The final
  verification now filters by **`resource_id` alone**, which is unique and cannot drift, and logs
  the distinct `resource_type` and `action` values it found so the trail is visible.

- **E2E collection: file `cp_cs_completion` after the CP/CS approval (missing step).** Approving the
  checklist does NOT create evidence — `evidence.py` only *verifies* that kind against an approved
  checklist, so `PATCH lending → 'CP/CS Completed'` was refused with
  *"without the governance evidence that stage requires: ['cp_cs_completion']"*. The collection now
  files it between the approval and the transition, citing the **checklist id** as `decision_ref`;
  the Register proves the checklist is `Approved`, belongs to that lending line, and was approved by
  a different checker than its preparer, then GENERATES the provenance from it
  (`workflow_id = "cpcs:{id}"`, `run_id = checklist_version`) — which is why, unlike
  `executed_agreement`, no `sha256` / `workflow_id` / `run_id` is sent. Corrected the docs that
  claimed the approval minted it.

- **Lending sanction fan-out: send only fields the lending line has.** The fan-out reused the
  DEAL's `extra` payload, which carries `product_type` — a Deal field. `LendingUpdate` is
  `extra="forbid"`, so the PATCH came back 422 and the activity retried indefinitely (attempt 8+),
  leaving the line at `Data Awaited`. It now builds its own `line_extra` (`rm` only); verified that
  every field the fan-out sends (`stage`, `rm`) exists on `LendingUpdate`.
  **Note:** Temporal records an activity's arguments in history when it is scheduled, so a run
  already stuck on the bad PATCH will keep retrying with the old payload. Rebuild the worker and
  start a NEW deal-structuring run — resuming the old one cannot pick up the corrected arguments.

- **Fixed the delegated-caller deserialisation that stalled every workflow that writes to the
  Register.** A live run of `DealStructuringWorkflow` failed on its FIRST `attach_evidence` with
  `AttributeError: 'dict' object has no attribute 'tenant'` and then retried forever, leaving the
  deal parked at `Note Circulated` with no evidence on file. Cause: Temporal's payload converter
  cannot always resolve the `CallerContext` annotation (with `from __future__ import annotations`
  every hint is a string), so it hands the activity a plain `dict` — and `_client()` accessed
  `caller.tenant` directly. `_as_caller()` now normalises either shape (ignoring unknown keys, so
  an older or newer payload can never break a running worker) and `_client()` calls it first. Every
  `caller.*` access lives inside `_client`, so the single fix covers all activities. Verified
  against the exact payload from the failure: dict → CallerContext, passthrough, None and junk all
  behave. This was never caught because the workflow tests use mocked activities and a
  time-skipping server — the real converter only runs against a live worker.
- **E2E collection: two client-side fixes found by running it.**
  - The orchestrator's `GET /v1/workflows/{id}` requires a **verified** identity (an OIDC bearer)
    and answers 401 to a dev `X-User-Email`, even though the committee-decision write on the same
    headers succeeds. Those two status reads are replaced by bounded **polls against the Register**
    (`/v1/deals/{id}`, `/v1/lending/{id}`) that re-run themselves via `setNextRequest` up to 15
    times with a short pause — which also absorbs Temporal's asynchrony. (Polling loops only in the
    Collection Runner.)
  - `executed_agreement` is governance evidence and must carry provenance — the Register refused it
    with *"must cite its workflow_id and run_id"*. Both fields are now sent.

- **A committee decision now sanctions the deal AND its lending facility (correctness fix).** The
  lending line could never reach `Sanctioned`, which made `CP/CS Completed`, `Ready for
  Disbursement` and `Disbursed` unreachable — the entire handover feature set was
  effectively dead. Cause: `DealStructuringWorkflow` filed evidence and advanced the stage on the
  **Deal** only (every `attach_evidence` used `"Deal"`/`"Lead"`, every `advance_stage` used
  `"deals"`), while the Register binds a decision to its subject
  (`_verify_committee_decision` refuses "a different subject"). So Lending's own evidence gate had
  no possible producer.
  - **Orchestrator**: when recording the committee decision it now ALSO records a subject-bound
    decision for each of the deal's lending lines, keyed `{workflow_id}:lending:{line_id}`, carrying
    the deciding human's committee authority — which the workflow, a service principal, could never
    supply. Best-effort per line: a line that cannot be recorded is simply not sanctioned, and the
    deal outcome still stands.
  - **Workflow**: after the deal is Sanctioned, `DealStructuringWorkflow` walks each lending line to
    `Note Circulated`, files `credit_committee_approval` + `sanction_letter` against the **Lending**
    subject citing that per-line decision, then advances the line to `Sanctioned`.
  - **New activity** `find_lines_for_deal(resource, deal_id, caller)` (registered on the worker), and
    **`deal_id` is now a whitelisted filter** on lending / syndication / asset-monetisation — the
    fan-out needs it, and the UI wants it.
- **The whole platform is reachable through the one public door.** The gateway now prefix-routes
  **`/access`** to the Access service (injecting Access's own credential, as it already did for
  `/atlas`, `/vocx`, `/pulse`, `/orchestrator`). User provisioning no longer needs the direct
  `:8002` port, so a client never has to step outside the edge.
- **`postman/PRISM_E2E_Full.postman_collection.json` (new, 58 requests / 12 folders).** Every
  request enters at `https://<host>:8443`; Postman presents **no backend api key** (the gateway
  injects each upstream's). All three product lines reach terminal state — Lending
  `Disbursed` via the Temporal committee decision, Syndication `Disbursed`, Asset
  monetisation `Closed` — with real maker-checker on CP/CS and the handover. Four requests must
  fail (unknown filter, lead→`Converted`, hand-typed `Sanctioned`), proving the gates. Generated by
  `scripts/gen_e2e_full.py`; all 32 request bodies validated against the frozen register +
  orchestrator specs (fields, maxLength, regex patterns, nested array items).

- **The edge now speaks HTTPS (self-signed for local/demo).** TLS terminates at NGINX, so no
  client talks to PRISM in plaintext.
  - `scripts/gen_dev_certs.sh` generates a self-signed pair into `deploy/nginx/certs/`, with
    Subject Alternative Names for `localhost`, `127.0.0.1`, `::1`, `nginx` and `prism.local` (a
    cert without SANs fails verification even when trusted). Also exposed as `make certs`.
  - **nginx**: `:443` terminates TLS (TLSv1.2/1.3, HTTP/2) and `:80` now serves **only** a 301 to
    HTTPS plus the health probes, so plaintext cannot be used by accident. Compose publishes
    `8443:443` (the front door) and `8080:80` (redirect), and mounts the certs read-only. HSTS is
    deliberately left off while the cert is self-signed — it would pin browsers to HTTPS for
    `localhost` and break later local debugging.
  - **Private keys are gitignored** (`deploy/nginx/certs/*`, keeping `.gitkeep` + a README that
    documents the two expected paths). Production mounts a CA-issued pair at the same paths — or
    cert-manager provides them in Kubernetes — with no `nginx.conf` change.
  - Postman/`docs` updated for the self-signed cert: turn off SSL verification, or trust
    `deploy/nginx/certs/tls.crt`. `docs/WSL_DEPLOY.md` gains the cert step, TLS verification
    commands and matching troubleshooting.

- **Postman: routed through the NGINX edge, plus a UI-facing CRUD collection.** Fixes two real
  defects in the generated environment and adds the collection a PRISM UI developer actually needs.
  - **Routing fix (was broken).** `orchestratorUrl` pointed at the **gateway's** own port
    (`:8001/orchestrator`) rather than the public edge. Everything now enters at one contact
    point — `baseUrl = https://localhost:8443` and
    `orchestratorUrl = https://localhost:8443/orchestrator` — matching how the platform actually
    routes: the **edge forwards every request to the gateway**, and the **gateway** routes by path
    prefix (`/atlas`, `/vocx`, `/pulse`, `/orchestrator` → those services, stripping the prefix;
    anything else → the Register behind the RBAC gate). `registerDirectUrl` (:8000) /
    `orchestratorDirectUrl` (:8006) are kept for debugging only — they bypass the gate.
  - **Credentials are injected, not sent (corrected).** The gateway **strips** any client
    `X-API-Key` and injects the correct scoped upstream credential itself, so Postman no longer
    sends one — `X-API-Key` ships disabled, for direct-port debugging only. Likewise
    `X-User-Roles` is **not** trusted at the edge (the gateway resolves roles from the **Access**
    service), so the guide now says to change the *user* rather than the roles header. Added
    `bearerToken` for the OIDC path, which is the real identity once an issuer is configured.
  - **`PRISM_UI_CRUD.postman_collection.json` (new, 158 requests / 20 folders).** Table CRUD only,
    one folder per table, **every request through the edge** — the same path the browser takes, and
    the only one where the gateway actually authorizes the call. Governance operations (CP/CS,
    handover) are deliberately excluded: the UI reaches those through the workflow plane's
    maker/checker pair, not plain CRUD.
  - **Identity headers on Register requests.** `X-User-Email` / `X-User-Roles` are now sent
    alongside the API key, so the RBAC matrix and record scope are observable from Postman: set
    `userRoles` to `BDRM` and scoped operations refuse another RM's records. Added distinct
    `makerEmail` / `checkerEmail` / `seniorRoles` so the CP/CS and handover maker-checker controls
    can be exercised (self-approval must 403). Removed the stale
    `Register.postman_environment.json`, and retired the superseded Register-only generator
    (`services/register/scripts/gen_postman.py`) — its `make postman` target now runs the
    repo-level generator, so it can no longer clobber the full collections.
  - **`docs/POSTMAN.md` (new).** How to use all three collections: selecting the environment (the
    usual cause of unresolved `{{variables}}`), which host serves which plane, per-plane
    credentials, the six-operation contract every table follows, the two-person maker-checker
    sequence, chaining ids, `If-Match` / `Idempotency-Key` / `X-Request-ID`, why sample bodies must
    be replaced, and a troubleshooting table.
  - **`docs/ARCHITECTURE.md` §6 — VocX identity.** Documents that VocX is a capture surface, not an
    authority, in both shapes it ships as (a button inside the PRISM UI, where the signed-in user's
    token and roles are already in hand; or a standalone field app against the same OIDC issuer).
    The rule — **VocX proposes, the user approves, the Register decides and writes** — with a
    per-action/per-record decision table and an honest status table: the human path already enforces
    tenant + role + record scope + lifecycle, but VocX still writes as `svc_vox` without propagating
    the verified user, so record scope cannot bind on that path (open gap, with the touchpoint
    review/approve step).

- **Postman collections now cover the full REST surface (Register + Orchestrator).** The previous
  collection was Register-only and missing the workflow plane. `scripts/gen_postman.py` now generates
  **from the frozen OpenAPI specs** (`docs/openapi/*.json`, not by importing the services) into
  `postman/`: `Register.postman_collection.json` (186 requests — all CRUD + evidence, decisions, CP/CS
  checklists, handover packages), `Orchestrator.postman_collection.json` (14 requests — every
  workflow-plane endpoint incl. **CP/CS checklist** and **Advaya handover prepare + approve**), and a
  shared `PRISM.postman_environment.json` (`baseUrl` → Register, `orchestratorUrl` → gateway
  `/orchestrator`). Request bodies are sampled from the schemas (nested objects like document refs and
  checklist items filled in, not left empty). `scripts/export_openapi.sh` regenerates the collections
  alongside the specs so they never drift. The stale single-service `Register.postman_environment.json`
  was removed. (access/gateway/VocX/PULSE/ATLAS are service-internal and intentionally not in the
  collections; the gateway is a transparent proxy for the two specs above.)

- **CP/CS Temporal workflow (8th) + deployed compose E2E harness.**
  - **`CpcsChecklistWorkflow`** — the 8th workflow. The maker's phase records the authoritative CP/CS
    checklist through the Register (`activities.prepare_cpcs_checklist`); it's exposed business-facing
    via the orchestrator (`POST /v1/workflows/cpcs-checklists` [maker], `…/{id}/approve` [checker,
    senior authority, different person]) and mapped at the gateway. Registered on the worker;
    workflow test added (skips offline like the other Temporal tests).
  - **Deployed E2E.** `scripts/e2e_smoke.sh` runs against a live compose stack (real PostgreSQL +
    Temporal + all services): health checks, Register CRUD, the CP/CS maker-checker end-to-end
    (prepare → self-approval refused → different-checker approve → Approved), and the handover
    endpoint's precondition gate. `.github/workflows/e2e.yml` brings the stack up with `docker compose`
    and runs it in CI. Compose fix: the orchestrator now gets `WORKFLOWS_REGISTER_BASE_URL` (needed
    for the durable decision + handover-approval calls it makes to the Register).
  - **WSL runbook** (`docs/WSL_DEPLOY.md`) — one-command bring-up + smoke on Docker Desktop/WSL2.
    OpenAPI + Postman regenerated for the new CP/CS orchestrator routes.

- **Handover maker-checker enforced, package integrity verified, CP/CS waiver/CS-deferment controls,
  frozen OpenAPI contracts.** Consolidated milestone hardening the handover + CP/CS operations.
  (Pre-release: migration 0016 amended in place as the schema baseline.)
  - **Real two-person maker-checker (P1).** The handover is now two phases. A MAKER *prepares* the
    package (`POST /v1/internal/handover-packages`) in a **Prepared** state — this no longer advances
    the stage. A DIFFERENT CHECKER *approves* it (`POST /v1/internal/handover-packages/{lending_id}/approve`),
    which freezes the package and advances the stage transactionally. Both identities are resolved
    from the AUTHENTICATED context (never submitted names), and the Register REQUIRES the checker's
    user id to differ from the maker's — one person can no longer initiate and approve. The
    orchestrator exposes both as `POST /v1/workflows/advaya-handover` (maker) and
    `POST /v1/workflows/advaya-handover/{lending_id}/approve` (checker), each senior-authority-gated;
    gateway mappings added.
  - **Package integrity + real document (P1).** `executed_document_refs`, `delivery_method` and
    `recipient` are now REQUIRED. The executed-document references are RECONCILED against the on-file
    `executed_agreement` evidence (the agreement's digest must appear among them), and the CP/CS
    checklist version is RECONCILED against the approved checklist that minted `cp_cs_completion`
    (mismatch refused). The package MANIFEST is generated and its digest computed **server-side**
    (callers can't submit a digest), stored, and returned by the download endpoint — which
    self-verifies the digest against the stored document. An empty/partial package can no longer
    advance the stage.
  - **CP/CS waiver + CS-deferment controls (P1).** Checklist items now carry a `condition_type`
    (**CP** vs **CS**); a checklist must have ≥1 item. Waiving or deferring a condition requires
    senior authority (Credit Head/Management/Admin) — a non-senior maker is refused. A **Waived**
    item requires a reason; a CP may be **Deferred as CS** only with a reason AND an `expiry_date`
    (a CS cannot be deferred). A completed checklist may not leave a required CP outstanding.
  - **Frozen OpenAPI contracts (P1).** `docs/openapi/{register,orchestrator,gateway}.openapi.json`
    are generated from the live apps and committed for the ATLAS/Node.js team, with
    `scripts/export_openapi.sh` to regenerate. The Postman collection is regenerated to cover the new
    endpoints.
  - Tests: rewritten `register/test_handover.py` (two-phase maker-checker + package integrity) and
    `register/test_cpcs.py` (waiver/CS-deferment); updated the workflow prepare-only test + mock.
  - **Still open (reviewer's program, deferred):** a dedicated CP/CS Temporal workflow (the CP/CS
    operation is exposed as a business-facing Register/orchestrator operation for now); a
    deployed live-Temporal E2E (the time-skipping test server isn't available in this sandbox — the
    register handover/CP/CS flow is exercised end-to-end against real Postgres).

- **Operationally complete handover: durable package, authenticated operation, disabled dormant path,
  authoritative CP/CS.** Completes the three handover P1s so `Disbursed` is a real,
  provable operation — not just a stage change.
  - **Durable, immutable handover PACKAGE (P1-1).** New `advaya_handover_packages` aggregate
    (migration 0016): handover id/timestamp, lending+deal ids, the AUTHORITATIVE facility + proposed
    drawdown amount/date (snapshotted from the Lending row server-side, never trusted from input),
    CP/CS checklist version, executed-document references+hashes, exported package reference+digest,
    initiator+approver, delivery method/recipient, notes, and a full snapshot. Created transactionally
    via `POST /v1/internal/handover-packages` (register `handover.py`): it loads the line, confirms
    `Ready for Disbursement`, re-verifies CP/CS + executed-document evidence, writes the immutable
    snapshot, and ONLY THEN advances the stage. A trigger blocks DELETE and every UPDATE except a
    one-time set of the manual `advaya_reference`. So the record proves WHAT was handed over.
  - **Authenticated handover operation (P1-2).** New orchestrator `POST /v1/workflows/advaya-handover`
    — loads the Lending record server-side, confirms `Ready for Disbursement`, uses authoritative
    amounts (not workflow inputs), requires **Credit Head / Management / Admin** authority (fresh via
    Access; maker+checker recorded), and starts `AdvayaHandoffWorkflow`, which calls the package
    endpoint. Exposed to the workspace via `GET /v1/lending/{id}/handover-package` and
    `POST …/download`; gateway mapping added. The workflow no longer does a bare stage PATCH.
  - **Dormant Advaya acknowledgement path DISABLED by default (P1-3).** `Disbursement Pending` is
    removed from the current lifecycle — `Disbursed` is now the terminal. A default-off
    `REGISTER_ADVAYA_INTEGRATION_ENABLED` flag gates the whole ack path: the internal
    `/v1/internal/advaya-handoffs` router is not registered, `attach_advaya_evidence` is not in the
    workflow service's grant, the `advaya_acknowledgement` attach is refused as disabled, and startup
    fails closed if the flag is on without a configured endpoint. Enabling it re-arms everything
    together for a future real integration.
  - **Authoritative CP/CS checklist + maker-checker.** New `cp_cs_checklists` aggregate: a maker
    prepares/completes it, a DIFFERENT checker approves it (frozen once terminal). `cp_cs_completion`
    evidence is now VERIFIED against an `Approved` checklist (`evidence.py::_verify_cpcs_checklist`,
    verify-source `cpcs`) — no longer caller-attached.
  - **Proposed vs actual disbursement fields.** Lending gains `proposed_disbursement_amount` /
    `proposed_disbursement_date` (the drawdown PRISM proposes, gated at `Ready for Disbursement`); the
    pre-existing `disbursed_amount` / `disbursement_date` are reserved for a real disbursement
    confirmation and never set by PRISM.
  - **Legacy import mapping.** The governed XLSX importer maps ATLAS-era `Documentation` →
    `CP/CS Completed`; `Disbursed` imports verbatim (a Disbursed row's amount/date
    become the proposed drawdown), so historical spreadsheets load into the current vocabulary.
  - New tests: `register/test_handover.py`, `register/test_cpcs.py`, updated `test_evidence`,
    `test_policy_enforcement`, `test_import`, `test_rbac_writes`, `workflows/test_business_workflows`.

- **Honest disbursement lifecycle: PRISM hands a facility OVER to Advaya and never self-disburses.**
  With no Advaya integration planned, PRISM must not mark a loan `Disbursed` on its own authority.
  The post-sanction credit pipeline is renamed for the real-world work at each milestone and the
  synthetic self-disbursement is removed:
  - **New stage chain** (Deal & Lending share it):
    `Sanctioned → CP/CS Completed → Ready for Disbursement → Disbursed →
    Disbursement Pending`. The old vague `Documentation` / `Disbursed` labels are gone (Syndication
    keeps its own `Disbursed` status — unchanged). `Disbursed` is PRISM's honest
    TERMINAL: the last state it can assert on its own authority.
  - **Gates.** `CP/CS Completed` requires `cp_cs_completion` + `executed_agreement` evidence;
    `Ready for Disbursement` requires the `disbursed_amount` + `disbursement_date` mandatory fields;
    `Ready for Disbursement` / `Disbursed` / `Disbursement Pending` are row-locked to
    senior credit authority (Admin / Management / Credit Head). `Disbursement Pending` (Advaya has
    taken the package up) is reachable ONLY on a real, verified `advaya_acknowledgement` — which
    nothing can produce until Advaya is integrated, so a facility honestly rests at the
    terminal and onward states are never fabricated.
  - **Workflow.** `AdvayaHandoffWorkflow` now performs the single handover hop
    `Ready for Disbursement → Disbursed` (no Advaya call, no fabricated acknowledgement);
    the `advaya_handoffs` record + verified-ack machinery is retained, dormant, as the ready hook for
    a future Advaya integration.
  - **Vocabulary + import.** `STAGE_VOCAB`, the `Lending Stage` / `Terminal (Lending)` reference
    dropdowns, and the `LendingStage` enum are updated together (the vocab-vs-dropdown drift test
    guards them). The governed XLSX importer keys on the new vocabulary. NOTE: legacy ATLAS-era
    spreadsheets that carry the old `Documentation` / `Disbursed` lending labels are not yet mapped
    to the new stages — that stage-label migration is a follow-up.
  - Tests updated across `evam-backend-core`, register (`test_policy_enforcement`, `test_rbac_writes`,
    `test_import`) and workflows to walk the new chain and assert the honest terminal.

- **Round M foundation milestone: committee decision is now signal-verified, the Advaya
  acknowledgement is authoritative, CP/CS is enforced, and the control docs are corrected.**
  - **Committee verification (P1).** The Deal-Structuring workflow no longer re-records the decision
    with the initiator context and no longer trusts the signal. `record_committee_decision` is
    removed; a new `verify_committee_decision(deal_id)` activity reads the AUTHORITATIVE decision the
    orchestrator persisted (fresh-authorized, single-winner) and derives the outcome, approver, note
    AND references from it — rejecting missing / cross-subject / non-terminal records. The
    `committee_decision` signal is a wake-up only; a spoofed/direct signal is ignored and the run
    keeps waiting (→ TimedOut). Decision references (`committee_reference`,
    `sanction_letter_reference`) are carried on the decision record (migration 0014). New tests:
    approved/rejected via the record, and a spoofed-signal-without-a-record → TimedOut.
  - **Advaya acknowledgement is authoritative (P1).** A new immutable, single-winner `advaya_handoffs`
    record (migration 0015; handoff key, payload digest, status, ack id; UPDATE/DELETE-blocked;
    fail-closed RLS) with a service-only API (`/v1/internal/advaya-handoffs`). `advaya_acknowledgement`
    evidence is now VERIFIED against an `Accepted` handoff for the same Lending line with a matching
    payload digest (provenance generated from the record) — so Admin/Management can no longer invent
    workflow/run values to satisfy the disbursement gate. A new `AdvayaHandoffWorkflow` performs the
    handoff (idempotent on the key), records the outcome, files the verified ack, and advances to
    `Disbursed`. New tests: invented/rejected/wrong-digest handoffs refused; single-winner handoff
    record; handoff workflow disburses only via the verified ack.
  - **CP/CS enforced (P1).** Lending `Disbursed` now requires `cp_cs_completion` in addition to
    `executed_agreement` + `advaya_acknowledgement`.
  - **Control docs corrected.** FOUNDATION_SPEC / IMPLEMENTATION_MATRIX / SCENARIO_CATALOGUE
    re-baselined and the stale claims fixed (executed-doc gate IS wired; Advaya contract IS frozen;
    committee signal IS verified).
  - Still open (matrix): committee quorum/multi-member; the Advaya handoff TIMEOUT branch +
    orchestrator start endpoint; CP/CS waiver state model (mandatory/waived/pending/completed +
    waiver authority/reason/expiry); OCR maker-checker; CIPHER.

- **Milestone: freeze the Engagement + Advaya contracts and gate money-movement on executed docs +
  Advaya acknowledgement.**
  - **Advaya boundary FROZEN** (`FOUNDATION_SPEC.md` §11): ownership boundary, handoff preconditions,
    idempotent handoff key, the `advaya_acknowledgement` evidence kind, the Disbursed gate, and the
    timeout/duplicate/retry/rejection semantics the handoff workflow must implement.
  - **Engagement model FROZEN by decision** (§1): the Deal is the engagement unit; no separate
    `Engagement` table in Release 1 (a Company's engagement = the grouped view of its Deals). Rationale
    + the condition under which to revisit are documented.
  - **New evidence kinds** `advaya_acknowledgement` (op `attach_advaya_evidence`, reserved to the
    Advaya-handoff service / Ops / Management / Admin) and `cp_cs_completion`; both governance-grade
    (digest + run provenance).
  - **Lending `Disbursed` is now evidence-gated** on `executed_agreement` + `advaya_acknowledgement`
    (`policy.EVIDENCE_FOR_STAGE`), so money moves for humans AND services only once the executed
    facility agreement is on file and Advaya has acknowledged the handoff. Enforced by the shared
    `check_write` on the generic PATCH and the change-request approval path. Tests: the real-lending
    route test now proves Disbursed is refused without the evidence and allowed with it; the ebc
    single-authority test covers the gate.
  - Still open (matrix): the `AdvayaHandoffWorkflow` + activity + orchestrator endpoint that DRIVE
    the handoff and produce the ack evidence; the `cp_cs_completion` stage gate; qualification→
    conversion gate; OCR/CIPHER/fraud.

- **Gate-1 foundation freeze + Milestone: the three business workflows are now operationally
  exposed and the Credit Committee decision is fresh-authorized and persisted before signalling.**
  - **Docs:** `docs/FOUNDATION_SPEC.md` (frozen shared contracts, code-grounded, gaps marked),
    `docs/IMPLEMENTATION_MATRIX.md` (consolidated backlog with Definition-of-Done columns + honest
    status + release/gate plan), `docs/SCENARIO_CATALOGUE.md` (business + governance scenarios with
    current coverage). These replace the per-ZIP review loop with milestone gates.
  - **Orchestrator endpoints** for the three workflows: `POST /v1/workflows/lead-qualifications`,
    `/deal-structurings`, `/document-collections` (auth-gated, verified initiator, fail-closed
    delegated identity, tenant-bound idempotent ids), plus `/{id}/committee-decision` and
    `/{id}/document-received` signal endpoints. Gateway route-operation mappings added for all five.
  - **Committee decision governance.** `/{id}/committee-decision` re-checks committee authority
    (Credit Head/Management/Admin) FRESH via Access at decision time, DURABLY records a
    single-winner, subject-bound committee decision (provenance server-set) BEFORE signalling the
    Deal-Structuring workflow — so the Round-L evidence gate verifies the sanction against it and a
    raw Temporal signal alone cannot manufacture a committee outcome. `WorkflowDecision` gained the
    `committee` kind + subject binding in Round L; this wires the front door to it.
  - **Tests:** orchestrator auth-gating for every new endpoint (no verified identity → 401, bad key
    → 401) and gateway route classification for all five routes. Still open (tracked in the matrix):
    committee quorum/multi-member, OCR/CIPHER/CP-CS/Advaya, broader evidence gates, UI.

- **Round L — governance provenance is now VERIFIED, not just recorded; committee decisions are
  durable; and revocation/supersession are integrity-checked.** Closes the "provenance is a caller
  assertion" bypass the Round K review found.
  - **Committee/sanction evidence is verified against a durable decision.** ``workflow_decisions``
    gains ``subject_type`` / ``subject_id`` / ``run_id`` (migration 0013) so a decision can be a
    single-winner Credit Committee decision bound to a specific Deal/Lending, recorded only by
    committee authority (Credit Head / Management / Admin) through the workflow plane. Attaching a
    ``credit_committee_approval`` / ``credit_committee_rejection`` / ``sanction_letter`` record now
    cites ONLY a ``decision_ref``; the Register RESOLVES it against that decision and refuses unless
    the outcome matches (Approved/Rejected), the tenant + subject match, and the decision was
    recorded by committee authority — then GENERATES the evidence's provenance (workflow/run/decider)
    from the record. Invented, mismatched, rejected and cross-subject decisions are all refused, and
    a partial unique index enforces one evidence row per (decision, kind) — no duplicate manufacture.
  - **The Deal-Structuring workflow records the decision before filing evidence.** A new
    ``record_committee_decision`` activity persists the committee outcome as a durable, single-winner
    record (the same pattern Lead Conversion uses) BEFORE any evidence is attached; the evidence then
    verifies against it. So a raw Temporal signal alone can no longer manufacture a committee
    outcome — the Register rejects evidence with no backing decision.
  - **Revocation / supersession integrity.** Attachment, listing, revocation and supersession now
    share ONE authorization path that reloads the subject and enforces a SCOPED caller's row scope
    (a FULL grant, Admin/Management, or a named service pass) — so listing and revoking are no longer
    open to any same-tenant principal, and an out-of-scope revoke/list is refused. Supersession
    requires the prior row to be the SAME tenant, subject AND evidence_kind and currently valid (a
    scoped document authority can no longer "supersede" — and thereby invalidate — committee
    evidence). Revocation locks the evidence row and rejects a repeated/contradictory terminal status.
  - **Tests.** Provenance verification (nonexistent / rejected / cross-subject / non-committee-
    authority decisions refused; genuine decision accepted; provenance copied from the record;
    one-to-one uniqueness), and revocation/supersession integrity (cross-kind supersession refused,
    out-of-scope revoke/list refused, double-revoke refused). The ordered-sequencing and
    change-request tests seed a real committee decision before walking through Sanctioned.

- **Round K — governance evidence is now trustworthy: authorised by kind, bound to provenance, and
  revocable.** Closes the "any identified principal can manufacture sanction evidence" bypass the
  Round J review found, and hardens evidence authority.
  - **Attachment is authorised BY KIND.** A new shared registry
    (``evam_backend_core.evidence``) maps every evidence kind to the RBAC operation a caller must
    hold. Four operations were added — ``attach_committee_evidence`` / ``attach_sanction_evidence``
    (reserved to Credit Head / Management / Admin and the workflow service),
    ``attach_document_evidence`` and ``attach_qualification_evidence``. So a committee approval or
    sanction letter can no longer be minted by an ordinary RM/Analyst or an unrelated service —
    ``POST /v1/evidence`` runs ``enforce_operation`` for the kind's op (fail-closed for both users
    and named services), and an anonymous unnamed key is refused outright. Arbitrary
    ``evidence_kind`` strings are rejected (controlled vocabulary, with a controlled
    ``document:<name>`` prefix).
  - **Bound to a real subject, scope and provenance.** The subject must exist and be a type the
    kind allows; a SCOPED authority may only attach to a subject in their scope. Governance-grade
    kinds (committee/sanction/executed) additionally REQUIRE an integrity digest (``sha256``) AND a
    binding to the authoritative Temporal run + decision (``workflow_id`` / ``run_id`` /
    ``decision_ref``) — a free-typed committee record is refused (422).
  - **Immutable but revocable.** Evidence rows stay write-once, but a mistaken or fraudulent record
    is neutralised by APPENDING a terminal status to a new append-only ledger
    (``governance_evidence_status`` — migration 0012, fail-closed RLS, UPDATE/DELETE-blocked):
    ``POST /v1/evidence/{id}/revoke`` marks it ``Revoked`` / ``Invalidated`` (and supersession marks
    the prior row ``Superseded``). The policy loader now consumes only CURRENTLY-VALID evidence, so
    a revoked committee approval no longer satisfies the sanction gate while its history is
    preserved. The evidence gate is enforced identically on the generic PATCH and the change-request
    approval path.
  - **Workflows file provenance.** The ``attach_evidence`` activity now binds each governance record
    to this run's ``workflow_id`` / ``run_id`` and a committee ``decision_ref``, with an integrity
    digest, so the Deal-Structuring workflow's committee/sanction evidence is traceable to the run
    that produced it. New authorization/provenance/revocation tests (RM and Analyst refused,
    unknown kind refused, wrong subject type refused, missing digest/provenance refused, phantom
    subject refused, out-of-scope subject refused, revoked evidence no longer satisfies the gate).

- **Round J — reconciliation is safe under concurrency, waived records stay out of operational
  reads, and evidence-based lifecycle gates land with the first business workflows.** Closes the
  three P1s the Round I review left open.
  - **Concurrent resolution can no longer leave a wrong subject flag.** Resolving a reconciliation
    item now SERIALISES on the business record: it locks every open item for the
    ``(tenant, subject_type, subject_id)`` set ``FOR UPDATE`` in a deterministic id order (which
    also covers the target item), then locks the subject row, then recomputes the flag against the
    settled set — so two admins closing different items on the same subject queue instead of racing,
    and the ordered lock acquisition avoids a deadlock. Assignment and closure are idempotent via an
    optional ``If-Match`` item version (a stale version → 409). New tests fire two simultaneous
    resolutions (``asyncio.gather``) and assert the final flag is correct, and that a stale
    ``If-Match`` is refused.
  - **Waived incomplete records are excluded from routine reads by default.** Reconciliation now
    has THREE operational classes — ``NULL`` (complete, visible), ``Required`` (hidden), and
    ``Waived`` (a deliberate senior exception, ALSO hidden). The centralised exclusion predicate
    hides every still-flagged record (only ``NULL`` is operationally complete), so a waived-but-
    incomplete Disbursed line can no longer silently enter disbursed totals or trigger downstream;
    it surfaces only under an explicit Admin/Management inclusion, still marked ``Waived``.
  - **Waiver requires designated senior authority.** A waiver keeps an incomplete record in the
    business of record, so it is now reserved to Management (not any single Admin operator); a plain
    Admin waiver is refused (403). Reconciliation list/assign/resolve require an Admin **or**
    Management identity (never a service).
  - **Evidence-based lifecycle gates (P1-C).** A new gate in the shared policy engine
    (``EVIDENCE_FOR_STAGE`` + ``evidence_error``, wired into ``check_write``) refuses a sensitive
    transition until the IMMUTABLE governance evidence that stage requires is on file — for humans
    AND services alike. Seeded on the sanction milestone: a Deal/Lending line reaches ``Sanctioned``
    only once both the Credit Committee approval AND the sanction letter are recorded. Evidence
    lives in a new append-only ``governance_evidence`` table (migration 0011, fail-closed RLS, a
    trigger that rejects UPDATE/DELETE) with an attach/list API (``/v1/evidence``); the gate is
    enforced on the generic PATCH transition AND the change-request approval path (no bypass). The
    only way past a missing-evidence gate is an audited break-glass reserved to Admin/Management
    (``X-Evidence-Break-Glass`` justification header), recorded in the audit log. A no-op
    re-assertion of the stage a row already holds does not trigger the gate (so reconciliation's
    re-verification of an existing stage still works).
  - **First business workflows started.** Three durable Temporal workflows now orchestrate the
    governance work and file the evidence the gate requires: ``LeadQualificationWorkflow`` (records
    the qualification review as evidence and hands off), ``DealStructuringWorkflow`` (walks the
    ordered pipeline, circulates the credit note, waits for the Credit Committee decision by signal,
    then FILES the committee-approval + sanction-letter evidence and advances to ``Sanctioned``
    through the Register's normal API — which accepts it precisely because the evidence now exists),
    and ``DocumentCollectionWorkflow`` (collects the required documents by signal and files the
    executed-agreement evidence when complete). New ``attach_evidence`` / ``advance_stage`` /
    ``get_resource`` activities (evidence attach is idempotent); registered on the worker. Tests
    prove the structuring workflow files evidence BEFORE it can sanction (and a rejection never
    does), and that an ``advance_stage`` to Sanctioned is refused until the evidence is present.

- **Round I — reconciliation is closed off as a write bypass, and the exclusion is consistent
  everywhere.** Closes the reconciliation-safety defects the review found in rounds G/H.
  - **No inline write bypass.** The resolve endpoint no longer accepts an inline ``corrected``
    (or any business field) — that path applied arbitrary attributes with ``setattr``, skipping the
    update schema, the policy engine, field/row locks, optimistic locking and history. A record is
    now corrected only through its normal, policy-enforcing update API; resolution merely VERIFIES
    (all declared missing fields present AND the complete ``policy.check_write`` accepts the record
    at its current stage) and is refused otherwise. Immutable/system fields can't be touched at all
    (``extra='forbid'``).
  - **Exclusion is centralised and applied to every read path.** A single predicate now hides
    still-``Required`` records from the generic list, the **single-record GET** (404, fail closed
    for services — a known id can't fetch it), and the **Excel / JSON / counts exports**. Only an
    Admin may include them, explicitly, via ``include_reconciliation=true``; services never can.
  - **Waived stays distinct.** A waiver now persists ``reconciliation_status='Waived'`` on the
    subject (not cleared to look complete) — it remains visible but marked, a deliberate exception,
    never equivalent to fully reconciled.
  - **Multiple-item flag keyed correctly.** The remaining-open-item check now matches
    ``subject_type`` + ``subject_id`` (ids live in separate tables), so a different resource that
    happens to share a uuid can't hold — or wrongly clear — another's flag.

- **Round H — reconciliation resolution now proves the fix, and unresolved records are kept out
  of operational reads.** Closes the two reconciliation-safety P1s that Round G left open.
  - **Resolve must prove the data is corrected.** ``POST /v1/reconciliation/{id}/resolve`` no
    longer clears the flag on a note alone. For **Resolved**, every field the item flagged missing
    must now be present on the record (corrected via its normal, policy-enforcing API, or supplied
    inline as ``corrected``), and the record must pass ``policy.mandatory_field_error`` at its
    current stage — otherwise the resolution is refused (422). **Waived** is an explicit
    break-glass outcome that keeps an incomplete record and therefore requires a **ticket**. The
    audit event preserves the original imported values plus the before/after of the corrected
    fields.
  - **The subject flag clears only when nothing is left open.** Resolving one item no longer
    clears ``reconciliation_status`` while another ``Required`` item still exists for the same
    subject — a record with a second open issue stays flagged.
  - **Unresolved records are excluded from operational reads by default.** Every generic list now
    excludes rows flagged ``reconciliation_status='Required'`` (so they can't count toward
    disbursed/sanctioned totals or feed downstream processing). Services can **never** opt in (fail
    closed); only an Admin may, explicitly, via ``include_reconciliation=true``. They remain
    visible in the dedicated ``/v1/reconciliation`` work queue. Tests prove an unresolved record
    is absent from operational lists and returns only under the Admin opt-in.

- **Round G — reconciliation is now a persistent operational object, and import history is
  complete for every product line.** Closes two of the three follow-up P1s (the third — business
  evidence gates — is deferred to the workflow build, since it depends on the workflows that
  produce the evidence). Migration ``0010``.
  - **Persisted reconciliation (P1).** A retained-incomplete import row (``retain_incomplete=true``)
    now opens a durable ``import_reconciliation_items`` record — batch id + checksum lineage, the
    missing fields, the ORIGINAL imported values (preserved), an owner, and an open/closed status —
    AND flags the subject record ``reconciliation_status='Required'``, so it is excluded from
    "operationally complete" reads (the flag is exposed on every tracker's read model). New Admin
    endpoints list / assign / resolve the items (``/v1/reconciliation``); resolution is audited
    (``reconciliation.resolve``, preserving the original values) and clears the subject's flag. The
    information no longer lives only in the HTTP response / audit event.
  - **Complete, unified import history (P1).** One helper now records history for ALL four product
    lines (Deal gains a ``stage_history`` column; Asset Monetisation gains ``status_history``). A
    NEW historical row imported directly at an advanced stage gets an INITIAL ``null → stage``
    ``xlsx-import`` event; a MERGE that changes a stage records the transition — for Deal, Lending,
    Syndication and Asset Monetisation alike. Every event carries source + batch id + checksum +
    sheet + actor + timestamp, and the record always ends AT the value its final history entry
    names. Tests cover new + merge imports for every line.
  - **Deferred (P1, workflow-dependent).** Stage-specific business-evidence gates (document
    completeness, CIPHER appraisal, Credit Committee outcome, sanction conditions, Advaya
    acknowledgement) and their break-glass overrides require the workflows that generate that
    evidence; they land with each product line's workflow. The policy module already documents its
    mandatory-field rules as a seed.

- **Round F — real ORDERED lifecycle sequencing + imports that can't persist policy-invalid
  states.** Closes the two follow-up P1s: the graphs were any-to-any (stage-skipping), and the
  importer only warned on (still imported) mandatory-field violations.
  - **Ordered business transition graphs.** The any-to-any `_pipeline_graph` is replaced with real
    ORDERED graphs per product line (Deal/Lending credit pipeline; Syndication mobilisation;
    Asset-Monetisation). A step may only advance to the NEXT stage, step back one for
    refer-back/rework, go On Hold and resume, or move to a terminal outcome — so an API update or
    an approved request can no longer skip document/diligence/appraisal/committee/sanction gates
    (e.g. `Data Awaited → Disbursed` is now refused). Deeper workflow-generated-evidence gates land
    with each product line's workflow.
  - **Creation restricted to genuine ENTRY stages.** The creation allowlist is narrowed to the true
    start of each lifecycle (e.g. Deal/Lending only `Data Awaited`/`Diligence`; not
    `Note Circulated`/`Documentation`), and the same entry gate now applies to setting the FIRST
    stage of a stage-less row via PATCH — closing the NULL-source loophole where a row could jump
    straight to an advanced stage.
  - **Imports can no longer persist an incomplete terminal state.** A known stage missing its
    mandatory data (a Disbursed lending line with no amount/date) is now QUARANTINED by DEFAULT —
    the same state the interactive API rejects — instead of being imported with a warning. Retaining
    genuine historical-incomplete data is an explicit, audited override (`retain_incomplete=true`),
    and each retained row is flagged `reconciliation_status=Required` with its missing-field list and
    batch id so it is never treated as operationally complete.
  - **Import lineage + stage-history.** Every import now carries a batch id tying its accepted /
    quarantined / reconciliation rows and history events together. A MERGE import that changes an
    existing tracker's stage appends an `xlsx-import` event (source + batch id + timestamp) to the
    append-only history — an import-driven transition is recorded, not silently overwritten — and the
    changes are reported and audited.

- **Round E — authoritative lifecycle vocabularies + a governed spreadsheet-import path.**
  Closes the two follow-up P1s: creation protection was only a denylist, and the xlsx importer
  bypassed the policy authority entirely.
  - **Authoritative vocabularies, not a denylist.** ``STAGE_VOCAB`` now defines the complete legal
    stage/status set for Lead / Deal / Lending / Syndication / Asset Monetisation, mirrored from
    the ATLAS reference dropdowns (``REF_VALUES``) with a test that cross-checks the two so they
    cannot drift. An **unknown / free-text lifecycle value is rejected on every interactive path**
    (create, PATCH, approval) — including the case where it would be the FIRST stage of a row whose
    stage was still NULL (which the transition graph exempts).
  - **Explicit allowed-initial states (allowlist).** Creation is gated by an ALLOWLIST of
    intake/working states per product line, replacing the partial reserved-state denylist — so a
    resource can be born only in a working state; every governance outcome (Sanctioned, Disbursed,
    Closed) and terminal state (Rejected, Withdrawn, Dropped, Converted) is reached only through
    the flow or an approved transition.
  - **Full transition graphs for all four product lines.** Each product line now has a complete
    graph over its authoritative vocabulary: legitimate non-linear moves within the pipeline pass,
    unknown values are rejected, and a **terminal/committed state cannot be silently reversed**
    (e.g. a Disbursed line cannot go back to Diligence). Strict stage-by-stage GATING (must pass
    diligence before sanction) remains each product line's workflow's job.
  - **The spreadsheet importer is now governed.** ``POST /v1/import/atlas-xlsx`` requires a
    mandatory **reason** (and optional ticket), records the file's **SHA-256 checksum**, and writes
    an immutable ``mis.import`` audit event naming the importer, mode, checksum, reason, counts and
    the exceptions. Rows whose lifecycle value is **unknown are QUARANTINED** (skipped, surfaced in
    the response report and audit) rather than silently written; a KNOWN terminal state missing its
    mandatory data is imported but recorded as a **warning** for reconciliation (historical data
    may legitimately begin at a later stage — the governed exception, made explicit).

- **Round D — the shared policy engine is now UNAVOIDABLE, and creation obeys the lifecycle.**
  Closes two P1 policy bypasses: the change-request approval path and resource creation each
  enforced only a subset of the rules a direct PATCH does.
  - **One authority for every write path.** A single ``evam_backend_core.policy.check_write(...)``
    now runs the whole policy — transition validation, mandatory-fields-to-enter-a-stage,
    role/stage field locks and row locks — and returns a typed violation (``validation`` → 422,
    ``forbidden`` → 403). The direct PATCH (``_enforce_transition``), the **change-request
    approval** (``/v1/requests/{id}/approve``) and **resource creation** all call it, so no path
    can enforce a different (or no) policy. Authorization (locks, 403) is checked before data
    validation (422) so an unauthorised caller isn't handed hints about what a stage needs.
  - **Approval can no longer bypass mandatory fields.** Approving a change merges the current row
    with the proposed field and runs the full check — so a Lending line cannot be approved into
    ``Disbursed`` without ``disbursed_amount``/``disbursement_date`` on the row, and a Deal cannot
    be approved into ``Sanctioned`` without ``product_type``/``rm`` (these bind the approver, even
    Admin — they are data-completeness invariants, not permissions).
  - **Creation obeys the lifecycle.** A new ``RESERVED_INITIAL`` denylist stops a resource being
    born in a governance-terminal state — a Deal as ``Sanctioned``, a Lending line as
    ``Disbursed``, a Syndication as ``Sanctioned``, an Asset Monetisation in a terminal state —
    for humans and machines alike; those are reached only through the flow or an approved
    transition. Creation also applies the mandatory-field rules when a lifecycle value is
    supplied. (A NULL/unset stage is treated as an initial set, not a transition.)
  - **Scope note.** Full per-product transition GRAPHS for Deal / Lending / Syndication / Asset
    Monetisation are intentionally NOT added here: their real stage vocabularies are richer than
    any seed sample (e.g. Lending has ``Note Circulated``), and a fail-closed graph seeded from an
    incomplete vocabulary would reject legitimate stages. Those graphs land with each product
    line's workflow round, which owns its authoritative stage list; creation is meanwhile guarded
    by the reserved-state denylist (safe with an incomplete vocabulary).

- **Round C — close the two policy/redrive gaps flagged in review.**
  - **Product-line subject names now match the resource registry.** The policy stage map keyed
    on ``LendingTracker`` / ``SyndicationTracker`` (the ORM class names), but the Register passes
    the real ``subject_type`` strings ``Lending`` / ``Syndication`` — so ``stage_field_of`` returned
    ``None`` and every mandatory-field / field-lock rule for those two product lines silently
    no-op'd. Corrected to ``Lending`` / ``Syndication`` (``AssetMonetisation`` already matched),
    with a comment tying the keys to the registry and unit + **integration tests that PATCH the
    real ``/v1/lending`` and ``/v1/syndication`` routes** so the wiring can't drift again. Seeded
    the first rules for both lines while here: a **Lending** line cannot be marked ``Disbursed``
    without ``disbursed_amount`` + ``disbursement_date``; a **Syndication**'s committed
    ``amount_cr`` is locked at ``Sanctioned`` to Syn Head / Management / Admin.
  - **Redrive now requires — and audits — a reason and the previous failure.** ``POST
    /v1/internal/decisions/{id}/redrive`` takes a **mandatory non-empty ``reason``** (422 if
    absent/blank) and an optional ``ticket`` reference, and the immutable ``decision.redrive``
    audit event now records the reason, the ticket, and the **previous dead-letter cause**
    (captured atomically via a CTE before ``last_error`` is cleared) alongside the admin and the
    from/to transition. The client helper takes ``reason`` (required) and ``ticket``.

- **Round B — a shared stage/field policy engine, enforced centrally in the Register.** A new
  ``evam_backend_core.policy`` module is now the single server-side authority for the three
  lifecycle rules every PRISM workflow needs, so each workflow stops reinventing (and drifting
  on) validation:
  - **Transitions** — the legal status/stage graph. Re-exported from the existing
    ``ALLOWED_TRANSITIONS`` / ``transition_error`` / ``initial_status_error`` so callers depend
    on ONE module, not scattered copies.
  - **Mandatory fields to enter a stage** — a resource may not advance to a target status/stage
    until the fields that stage requires are present (in the change, or already on the row).
    Seeded so a Deal cannot reach ``Sanctioned`` without ``product_type`` and ``rm``.
  - **Role/stage field locks** — at a given stage a field may be edited only by the named roles
    (Admin is always break-glass). Seeded so a Sanctioned deal's ``rm`` can be reassigned only by
    Management/Admin.
  - **One enforcement chokepoint.** The Register applies all three in ``_enforce_transition`` on
    the generic update path, reading the current row ONCE for transition, mandatory-field and
    field-lock checks. The policy DATA is seeded conservatively for the Lead/Deal flows that
    exist today and is the documented extension point every subsequent workflow round adds its
    product line's rules to. Unit tests cover the evaluators; integration tests exercise the
    Register enforcement against live Postgres.

- **Round A — redrive is now an audited Admin action.** ``POST
  /v1/internal/decisions/{id}/redrive`` re-activates a financial decision, so it now requires a
  verified **Admin** identity in the delegated context (``svc_workflows`` is only the transport;
  a service key or a non-Admin human is refused), and it writes an **immutable audit event**
  (``decision.redrive``) naming the admin, the workflow and the from/to transition. The client
  helper takes the delegated Admin context via ``extra_headers``.

- **Security audit round 15h — reconciler durability hardening: backfill, lease fencing,
  no-false-dead-letter, unbounded retry for accepted decisions, and a redrive path.**
  - **Backfill (migration 0009).** The outbox is now seeded from every existing
    ``workflow_decisions`` row (``INSERT … SELECT … ON CONFLICT DO NOTHING``), so decisions
    accepted before the outbox existed (rounds 15–15f) become deliverable instead of being
    invisible orphans. Idempotent same-outcome replay ALSO re-ensures the outbox row.
  - **Lease fencing.** A claim now stamps a ``claim_token`` and returns it; a delivery update
    applies only where ``status='pending' AND claim_token`` matches. A stalled claimant whose
    lease expired can no longer regress a row another replica has re-claimed — and ``applied``
    is terminal (a stale completion is a reported no-op, never a corruption).
  - **No false permanent dead-letter.** A transient ``result()`` failure on a COMPLETED run now
    PROPAGATES (→ retry) instead of being coerced to "different outcome, dead". Dead-lettering
    happens only on an authoritative terminal mismatch (missing run, or a run that
    completed/closed with a different outcome).
  - **Accepted decisions retry indefinitely.** The attempt-count dead-letter cap is removed —
    a transient worker/Temporal outage no longer decides a financial outcome by exhausting a
    budget; running/transient cases retry until they resolve, surfaced via the aged-pending
    gauge.
  - **Audited redrive.** ``POST /v1/internal/decisions/{id}/redrive`` returns a dead-lettered
    delivery to pending (attempts reset) for operator recovery.

- **Security audit round 15g — the background decision outbox + reconciler, and the Helm
  embedded-placeholder fix.**
  - **Transactional decision-delivery outbox.** Recording a decision now ALSO creates a
    delivery row (``workflow_decision_outbox``, migration 0008) in the SAME transaction — so an
    accepted decision always has a durable "deliver me" record. The outbox is mutable (unlike
    the immutable decision), fail-closed RLS like every other tenant table, and carries
    ``status`` / ``attempts`` / ``next_attempt_at`` / ``leased_until`` for the reconciler.
  - **Background reconciler daemon** (``python -m app.reconciler``, a new small deployment).
    It scans every active tenant and, for each pending delivery: claims it with a **lease**
    (``SELECT … FOR UPDATE SKIP LOCKED`` — safe across replicas), re-delivers it to the
    workflow, marks it **applied** once the run has converted with that outcome, **retries**
    with backoff while the run is still running, and **dead-letters** it when the run closed
    without applying or attempts are exhausted. Aged-pending and dead counts are logged each
    sweep for metrics/alerting. New Register endpoints back it: claim, mark-delivery, stats,
    and an active-tenant list — all restricted to ``svc_workflows``.
  - **Helm embedded-placeholder bypass fixed.** The secret guard now rejects a value that
    CONTAINS ``REPLACE`` anywhere (not only as a prefix), so a composite key like
    ``svc_atlas:REPLACE-…,svc_vox:REPLACE-…`` no longer slips through. CI gains a positive
    render with every credential replaced and a targeted negative that leaves only the embedded
    service-key placeholder.

- **Security audit round 15f — transient signal recovery and comprehensive Helm secret
  validation.**
  - **Transient Temporal signal failures are no longer misreported as a closed workflow.** A
    signal ``RPCError`` is no longer assumed to mean "closed": the orchestrator re-describes and
    acts on the ACTUAL state — if the run is still RUNNING (a transport blip), it returns **503
    "decision persisted; retry delivery"** (a retry re-signals safely, and the run ignores a
    duplicate once decided) instead of a bogus "closed (RUNNING)" 409; only a genuinely closed
    run is reconciled (already-applied 200 vs a real 409).
  - **Every production credential is validated centrally.** A new umbrella template fails the
    render if ANY production credential is left as a ``REPLACE-*`` / ``change-me-in-prod``
    placeholder — gated by ``global.requireRealSecrets`` (ON in values-prod.yaml, OFF by default
    so dev/CI renders are unaffected). Each check is SKIPPED when an ``existingSecret`` is
    configured (the auditor's usability fix — a valid external Secret is never rejected for a
    stale inline value), and every path is read with ``dig`` so a deploy with real secrets always
    renders. The gateway's earlier inline REPLACE check (which ignored ``existingSecret``) is
    removed in favour of this.
  - **Positive + negative Helm render tests in CI.** The prod overlay renders cleanly with the
    guard off (positive), and — with the guard on — the shipped placeholders and a blank OIDC
    issuer both FAIL the render (negative), asserted in the CI helm job.

- **Security audit round 15e — decision delivery made idempotent + reconciling, fail-closed
  least privilege, CI privilege check, and Helm auth guards.**
  - **Idempotent, reconciling decision delivery.** ``approve``/``reject`` now reconcile against
    a closed workflow instead of returning a misleading error: if the run already COMPLETED with
    this decision's outcome (the signal landed but the caller lost the response and retried), the
    API returns the AUTHORITATIVE applied result (200 ``already_applied``); if the run closed in
    the tiny window after the RUNNING check but before the signal, the same reconciliation runs
    against the final state; and a run closed with a different/absent outcome returns a clear 409
    to start a fresh attempt (the persisted decision row remains for reconciliation). This
    addresses the persist→signal race and the lost-response retry directly.
  - **Fail-closed least-privilege convergence.** When the deploy intends a runtime login
    (``REGISTER_APP_PASSWORD`` set), ``apply_rls`` now REVOKEs UPDATE/DELETE on the append-only
    tables and VERIFIES (via ``has_table_privilege``) that ``register_app`` holds neither — the
    Job FAILS if it still does, so a false least-privilege posture is never shipped. Dev stays
    best-effort.
  - **CI privilege assertion.** The end-to-end ``apply_rls`` provisioning test now asserts
    ``register_app`` lacks UPDATE/DELETE on ``workflow_decisions`` after convergence (runs where
    a superuser can provision the role; skips otherwise).
  - **Helm auth guards.** The gateway and orchestrator templates now ``fail`` the render when
    ``requireAuth`` is true but the OIDC issuer is empty (and the gateway flags a remaining
    ``REPLACE-*`` shared secret), so a production deploy can't silently render with auth
    mis-configured.
  - **Still open (larger, separate workstreams):** a true background outbox/reconciler for
    un-retried orphaned decisions; coordinated cross-service tenant provisioning; the shared
    field/stage policy engine; full audit + MIS reconciliation; production Temporal (HA/mTLS/
    namespace auth); and the assignment UI. These are tracked, not closed.

- **Security audit round 15d — make the round-15c fixes actually hold at runtime: correct
  Temporal memo access, a real (non-superuser) RLS proof, and least privilege that survives
  provisioning.**
  - **Temporal memo is read via the async accessor.** ``desc.memo`` is a coroutine method in the
    Temporal SDK, not a dict — the previous ``isinstance(memo, dict)`` was always false, so
    ``lead_id`` came back None AND the workflow **initiator was never recognised** (a normal
    requester polling their own conversion status got 403). Now ``await desc.memo_value(...)``
    is used for both the decision lead id and status authorization. The orchestrator test fake
    now exposes the real async ``memo_value`` (so this class of bug fails the test), plus a new
    test proves the memo initiator can read status while a stranger cannot.
  - **The RLS proof runs as a role that cannot bypass RLS.** The prior test queried through the
    schema owner, which — when the deploy connects as a PostgreSQL superuser (as the postgres
    image creates ``POSTGRES_USER``) — bypasses RLS even under FORCE, so it proved nothing. It
    now hops to a NOSUPERUSER + NOBYPASSRLS role and asserts the effective role genuinely can't
    bypass before checking that a different tenant sees zero rows.
  - **Least privilege survives ``apply_rls``.** ``apply_rls`` grants DML on ALL tables, which
    re-granted UPDATE/DELETE on the append-only ``workflow_decisions`` that migration 0007
    revoked. It now REVOKEs UPDATE/DELETE on the append-only tables again after the generic
    grant, every run — so the least-privilege claim is true post-provisioning (the 0007 trigger
    remains the hard stop regardless).
  - **Approval responses echo the persisted outcome + note**, not only the approver — so the API
    response fully matches the run and the database.

- **Security audit round 15c — decision-resource hardening: correct retry lead id, DB-level
  immutability, authoritative API responses, and a direct RLS proof.**
  - **Retry decisions record the REAL lead id.** The orchestrator now carries the lead id in
    the workflow memo and reads it back when recording a decision, instead of deriving it from
    the (retry-suffixed) workflow id — so a ``lead1-r2`` attempt records ``lead1``.
  - **The decision row is immutable at the database (migration 0007).** ``register_app`` loses
    UPDATE/DELETE on ``workflow_decisions`` (SELECT/INSERT only), and a trigger RAISEs on any
    UPDATE or DELETE — so a recorded decision cannot be altered or removed even by the owner.
  - **API responses report the AUTHORITATIVE approver.** ``approve``/``reject`` now return the
    approver, note and outcome from the persisted single-winner record (the FIRST approver on
    an idempotent replay), so the HTTP response can no longer name a later caller while the run
    and DB use the first.
  - **Direct RLS proof for the new table.** A test enables FORCE ROW LEVEL SECURITY and shows
    the owner connection sees a decision row only under its own tenant GUC — proving isolation
    at the database, not just via the application's WHERE clause. Also added: DB-level
    immutability test.
  - **Known, consciously-deferred (not P0):** (1) ``describe → persist → signal`` is not a
    single atomic step — a workflow could close in the tiny window after the RUNNING check,
    leaving a persisted-but-unapplied decision; this is benign under the ``-r2`` scheme (a
    terminal id is never reused, so the row is never consumed) and a full outbox/validated
    Temporal Update is tracked as remaining work. (2) No legacy ``#2`` retry ids exist to
    migrate — nothing was ever deployed — so no upgrade shim is added.

- **Security audit round 15b — retry workflows made durable: URL-safe retry ids, record-only
  attribution, no decisions for phantom workflows, and RLS convergence for the new table.**
  - **URL-safe retry ids (P0).** A retried conversion attempt now uses ``{id}-r2`` instead of
    ``{id}#2``. The ``#`` made every generated approval/status URL and the decision-lookup path
    truncate at the fragment, so a retry attempt's durable decision could not be fetched and a
    legitimate approval could be discarded after the token expired. The client also
    percent-encodes the workflow id in the decision path defensively.
  - **The record is the SOLE authority for attribution.** ``verify_decision`` now always
    derives the outcome, the approver identity AND the note from the persisted single-winner
    record — never the signal's (latest-caller) token or note. When two approvers submit the
    same outcome, the run and the database always name the same (first) approver and note.
  - **No decisions for phantom/closed workflows.** The orchestrator now confirms the workflow
    exists and is RUNNING (404 if missing, 409 if already closed) BEFORE it writes the decision
    record — so a stale row on a deterministic id can't be pre-seeded and later consumed by a
    run that reuses the id.
  - **RLS convergence covers the new table.** ``workflow_decisions`` is added to
    ``apply_rls.py``'s recurring convergence list, so later enforcement runs keep it fail-closed
    and FORCEd, matching every other tenant table.
  - Tests added: retry ``-r2`` resolution, truly-concurrent Approve-vs-Reject (one 201 / one
    409), same-outcome-different-approver attribution, decisions for nonexistent/closed
    workflows (404/409), and tenant isolation of ``workflow_decisions``.

- **Security audit round 15 — a dedicated, single-winner decision resource: no lost decisions
  on a transient failure, and Approved/Rejected can never both persist.**
  - **New internal decision resource in the Register.** A dedicated ``workflow_decisions``
    table (migration 0006) with a **UNIQUE (tenant_id, workflow_id)** constraint, fronted by
    service-only endpoints ``POST /v1/internal/decisions`` and
    ``GET /v1/internal/decisions/{workflow_id}``. It replaces the earlier approach of recording
    the decision as a general interaction, and fixes the two remaining P0s:
    - **Single-winner (atomic).** The FIRST decision for a workflow wins at the database level
      (``INSERT ... ON CONFLICT DO NOTHING``): replaying the SAME decision returns the original
      record, and the OPPOSITE decision returns **409** — even after the workflow completed. A
      concurrent Approve+Reject can no longer both persist and both be acknowledged. The
      orchestrator surfaces the 409 and does NOT signal.
    - **Transient failure no longer loses a decision.** The worker's durable-path read now
      distinguishes a genuine ``NotFound`` (invalid reference → discard) from a transport /
      429 / 5xx error, which is **re-raised** so Temporal retries the activity WITHOUT
      consuming the decision. Previously every exception was swallowed into ``valid: false``,
      which discarded a legitimate, already-acknowledged decision on a brief Register blip.
  - **Server-controlled provenance + least privilege.** The decision's ``decided_by`` and the
    approver's grant are set server-side from the verified delegated context (the signed
    internal token), never a client field; and the endpoints are restricted to the
    ``svc_workflows`` principal — so the over-broad ``/v1/interactions`` read grant added in
    14b is **removed** (the workflow service no longer reads every tenant interaction).
  - **Genuinely unbounded reconciliation.** The decision-critical activities drop the 7-day
    ``schedule_to_close`` cap; retries are bounded only by the workflow's own execution
    timeout, so a long Register outage after acceptance reconciles rather than exhausting.
  - **Note concurrency.** ``mark_lead_note`` now writes with an If-Match (``expected_version``)
    precondition, so a concurrent edit to a lead's notes can't be silently overwritten.
  - Fail-closed RLS covers the new table exactly like every other tenant table (FORCEd in
    production). Validated against real Postgres + the migration.

- **Security audit round 14b — decision durability closed properly: freshness restored and
  synchronous, reconciling persistence.**
  - **Decision freshness restored.** The approval token no longer verifies with expiry off. It
    is checked WITH expiry enforced (`verify_exp=True`), so a captured token is not an
    indefinitely-valid credential. A decision that outlives its short token is honoured only
    through the durable record below — never a stale bearer token.
  - **Synchronous, at-acceptance persistence.** The orchestrator now writes an IMMUTABLE,
    audited decision record to the Register **synchronously, before it acknowledges** the
    approve/reject and before the signal is delivered. If that write fails the API returns 502
    and no signal is sent — a decision is never acknowledged unless it is already recorded. The
    signal carries the record id, and the worker CONSUMES that record (verifying its
    workflow + decision binding) as the authority when the token has since expired. The
    workflow service gains a least-privilege read grant for `/v1/interactions`, and the
    orchestrator deployment gains the Register credentials it needs to write.
  - **Reconciling retries — no fail-after-accept.** The decision-critical activities (verify,
    convert, record rejection/timeout) run under a long-lived, unbounded retry policy
    (capped 5-minute backoff, 7-day reconciliation window) instead of the 5-attempt default,
    so a Register outage AFTER the API accepted a decision reconciles until the write lands
    rather than failing the run.
  - **Rejection/timeout auditing.** The rejection note is now attributed to the VERIFIED
    rejecter (or the system actor on a timeout), never the original requester, and
    `mark_lead_note` is idempotent (it won't double-append under retry).

- **Security audit round 14 — the decision path made operable and tamper-proof: registered
  verification, durable audited decisions, decision-bound tokens, a first-wins decision queue,
  and fail-closed conversion.**
  - **`verify_decision` (and the new `record_decision`) are now REGISTERED on the worker.**
    They were called by the conversion workflow but missing from the worker's activity list,
    so in production every approve/reject would have failed with "activity is not registered"
    and no decision could ever complete. Both the real worker and the e2e worker now register
    them. This was the blocker that made the whole round-13 gate non-functional.
  - **Durable, audited decisions — no longer dependent on a short-lived JWT.** The instant a
    decision is verified, the workflow writes an immutable, audited **decision record** to the
    Register (a `Conversion Decision` interaction on the lead, carrying the verified
    decider's identity, tenant, the decision and its reason). And `verify_decision` no longer
    requires the approval token to be *unexpired* (`verify_exp=False`): a worker that was down
    longer than the token's TTL still honours a decision already made — durability comes from
    Temporal history + the audited record, while signature and bindings stay enforced.
  - **The token binds the DECISION, not just the workflow.** The approval token now carries an
    immutable `Approved`/`Rejected` claim, and `verify_decision` requires an exact match. An
    approve token can no longer be replayed as a `reject` signal (or vice-versa) to flip the
    outcome.
  - **A first-wins decision QUEUE replaces the single pending slot.** Two signals arriving
    close together (approve+reject, or two approves) are queued and verified in arrival order;
    the first that verifies wins and the rest are ignored. A single mutable slot previously let
    a later signal silently overwrite an earlier one (last-writer-wins).
  - **Conversion fails closed without a verified approver.** `convert_lead_txn` no longer falls
    back to the original requester's context (`approver or inp.caller`): in production it
    refuses (non-retryable) when there is no verified approver, so a run can never convert on
    the requester's own authority. Provenance is strictly the verified approver identity.

- **Security audit round 13 — decisions verified before they count, durable approval, mandatory VOX binding, fail-closed orchestrator starts, status subject-scope, Temporal isolation.**
  - **Every conversion decision is verified BEFORE it becomes the workflow's first
    decision.** Signals are now untrusted input: the handler stores a *pending* decision, and
    the run loop validates it via a `verify_decision` activity — for BOTH approve AND reject
    — before it counts. A spoofed **reject** can no longer finalise a fake rejection, and a
    spoofed **approve** can no longer lock the run into a failing Approved state (the
    authorization-DoS): an unverified signal is discarded and the run keeps waiting.
  - **Durable approval.** The verified approver context is captured in workflow history at
    decision time, so the conversion no longer depends on a short-lived JWT surviving a
    worker outage; `approved_by` now comes from the verified token identity, not the signal
    argument.
  - **VocX requires the token binding to be PRESENT.** An unbound token (method/path absent)
    is now rejected, not accepted — method, path AND tenant claims must be present and exact.
  - **Orchestrator starts fail closed.** With signing configured, a workflow start (VocX
    capture or conversion) is REFUSED unless it carries a verified, route-bound delegated
    identity — no silent fallback to `svc_workflows` authority. Unbound tokens don't delegate.
  - **Workflow status is subject-scoped.** `GET /v1/workflows/{id}` is readable only by the
    run's INITIATOR (recorded in the workflow memo at start) or an approver-role holder for
    its vertical — not any same-tenant caller who merely knows the id.
  - **Temporal network isolation (Helm).** A NetworkPolicy restricts the Temporal frontend
    (:7233) to the labelled worker + orchestrator only, and production disables the
    signalling Web UI (with a pointer to mTLS + namespace authorization).

- **Security audit round 12 — direct-Temporal approval fail-closed, VOX token binding, workflow-status identity, fail-closed RLS convergence.**
  - **A direct Temporal signal can no longer approve a conversion.** The orchestrator mints a
    SIGNED approval token bound to the specific workflow id (after its fresh Access check);
    the convert activity REQUIRES and verifies that token (signature + workflow binding) in
    production and **never falls back to the requester's authority** — a raw Temporal signal,
    which can't forge the token, fails closed. Compose no longer publishes the Temporal gRPC
    port (7233) and gates the UI behind a `debug` profile; the code notes mTLS + NetworkPolicy
    for production.
  - **VocX enforces the incoming token's binding.** Beyond the signature, VocX now requires
    the token to be bound to `POST /v1/touchpoints` AND its signed tenant to equal the
    request's `X-Tenant`, and re-mints for the orchestrator using the **verified** tenant —
    so a VocX-key holder can't replay a tenant-A token and re-scope it to tenant B. The
    orchestrator now **rejects unbound tokens** (binding must be present, not just matching).
  - **Workflow status requires a verified identity.** `GET /v1/workflows/{id}` now needs a
    verified caller (not just the shared orchestrator key) under the production identity
    posture, and a legacy (pre-tenant-binding) workflow id **fails closed** rather than
    skipping tenant validation. (Subject/assignment-level scoping is noted as a further step.)
  - **RLS convergence fails closed.** After hardening `register_app`, `apply_rls` now QUERIES
    `pg_roles` and **fails the migration** unless both `rolsuper` and `rolbypassrls` are
    false — a pre-existing unsafe role that couldn't be fixed refuses to deploy rather than
    silently bypassing RLS. The integration test asserts both attributes.

- **Security audit round 11 — fresh-approver authorization, workflow tenant isolation, no generic Register key, hop-bound tokens, RLS role convergence.**
  - **Conversion is authorized as the VERIFIED APPROVER, not the stale requester.** The
    approve/reject decision now resolves the approver's identity + live grant from Access
    **at decision time** (a role revoked mid-wait is caught now), carries that `CallerContext`
    into the workflow, and the worker re-mints the conversion under the **approver's** fresh
    authority — closing "approval uses the requester's stale snapshot" and "approved_by is
    payload, not verified delegated identity".
  - **Workflow approval/status is tenant-isolated.** Approve, reject and status/result are
    bound to the workflow's tenant (the request's `X-Tenant` must reproduce the tenant slug
    in the business id), the Access approver lookup now carries `X-Tenant`, and the tenant
    slug is **collision-free** (a hash suffix, so `A-B` and `AB` no longer share a prefix).
  - **No generic Register key in production, and fail-closed if one exists.** `values-prod`
    drops the unnamed data-plane key (every caller is a named principal or delegated human),
    and under `enforce_rbac` the Register now **fails closed for any unnamed key** — it can
    no longer read tenant-wide, request `include_deleted`, or pivot tenants via `X-Tenant`
    without a user context.
  - **Conversion assignments reach the Register.** The orchestrator's HTTP model now accepts
    `rm_id`/`analyst_id`, so an API-started conversion actually creates the RM/analyst
    product-line assignments (they were always `None` before).
  - **Hop-bound tokens.** VocX re-mints a token **bound to the orchestrator's route** for the
    next hop (instead of forwarding the one minted for its own `/v1/touchpoints`), and the
    orchestrator **enforces** the method/path binding — a token minted for another route no
    longer delegates.
  - **RLS runtime role converges to a safe shape.** `apply_rls` now explicitly resets
    `register_app` to `NOSUPERUSER NOBYPASSRLS` every run (best-effort under a savepoint, so
    it never poisons the migrate transaction), rather than assuming a pre-existing role is
    harmless.

- **Security audit round 10 — workflow delegation + multi-tenancy, composite-read isolation, named gateway key, creation-time lifecycle, VOX fail-closed, proven RLS bootstrap.**
  - **Durable workflows carry the caller's TENANT and IDENTITY.** Every workflow input now
    holds a `CallerContext` (tenant + human identity + live grant). The worker's Register
    calls run in the caller's tenant (never the fixed `WORKFLOWS_REGISTER_TENANT` default),
    and — when signing is configured — re-mint a signed context per activity so a lead
    conversion (and a company-name capture's writes) is authorized as the HUMAN with their
    scope, not the worker's full `svc_workflows` grant. Workflow ids are tenant-scoped so a
    tenant-B run can't collide with tenant-A. Closes the "SCOPED caller authorization is lost
    in the workflow" and "workflows use a fixed tenant" P0s.
  - **VOX company-name capture delegates.** VocX forwards the tenant + the gateway's signed
    context to the orchestrator, which verifies it and threads the identity into the
    workflow — so the company-name path is delegated end to end, matching the direct path.
  - **VOX fails closed + guards its front door.** With signing configured, a user-triggered
    capture that carries no valid delegated identity is REFUSED (no silent fallback to the
    service key), and VocX validates the gateway-injected `X-API-Key` so it is not an open
    endpoint any pod can call.
  - **Composite company reads are isolated.** Dossier, financial history, timelines,
    documents (+ content) and lender-matrix are gated on a capability NO service holds on
    its own key — so an entity-matching service (`svc_pulse`) can match entities but cannot
    pull a company's full footprint; it must forward a user context.
  - **The gateway→Register key is NAMED.** `svc_gateway` is a pure delegation transport with
    NO own authority — a call on that key WITHOUT a signed context can neither read nor
    write anything, closing the "unnamed gateway key reads tenant-wide, including deleted".
  - **Creation-time lifecycle.** `POST /v1/leads` rejects an invalid initial status (a lead
    can no longer be born `Converted`) via a shared `initial_status_error` — the same
    lifecycle a later edit obeys, for humans and machines.
  - **RLS bootstrap is fail-closed and PROVEN by running it.** The `ALTER ROLE … PASSWORD`
    now uses a safely-escaped inline literal (PostgreSQL utility DDL can't bind a param —
    the previous form would have failed at runtime), and the integration test EXECUTES
    `apply_rls.apply()` itself, then logs in as the real `register_app` role and proves
    tenant isolation under FORCE RLS.
  - **Tenant reads work in production.** The gateway now injects the admin credential on
    tenant GET/list too (not only writes), so `GET /v1/tenants` no longer 401s behind the
    boundary. Compose now enables named service principals + per-service front doors so it
    demonstrates the production trust model, not `dev-local-key` everywhere.

- **Security audit round 9 — delegated service reads, capability-route authz, VOX identity propagation, fail-closed transitions, fail-closed RLS bootstrap, admin-key inside the boundary.**
  - **ATLAS reads unblocked; service reads isolated.** `enforce_service_read` now
    distinguishes a DELEGATED read (a signed user context is present → the user's view/row
    scope governs, so `svc_atlas` serves legitimate reads) from an OWN-KEY read (restricted
    to a per-service resource allowlist, `SERVICE_READ_GRANTS`). A write grant no longer
    implies tenant-wide read of every table — `svc_pulse` reads its intelligence context,
    not deals.
  - **Gateway authorizes capability routes.** `/vocx/v1/touchpoints`, `/pulse/v1/scan|items`
    and `/orchestrator/v1/workflows/*` now map to RBAC operations, so an unauthorized user is
    stopped at the door before a backend (or a durable workflow) is ever reached; each
    backend still enforces its own final authorization.
  - **VocX propagates the human identity.** When the signed-context channel is configured,
    VocX verifies the caller context and RE-MINTS one bound to the Register interaction
    write, so the human — not VocX's service key — is the authorization identity. Production
    also gives VocX the orchestrator front-door key (was missing → 401 on company-name
    captures).
  - **One fail-closed transition validator.** `transition_error()` is shared by the direct
    PATCH and the change-request approval path, and it now rejects an UNRECOGNISED current
    state (free-text / undefined) instead of waving it through — closing the approval bypass.
  - **RLS bootstrap fails closed.** When `REGISTER_APP_PASSWORD` is set (production intent),
    a failure to create/grant/log-in the runtime role now FAILS the migrate Job instead of
    being swallowed; a new integration test actually LOGS IN as the non-owner `register_app`
    role and proves tenant-isolated reads/writes under FORCE RLS.
  - **Admin credential stays inside the boundary.** The gateway injects the
    tenant-administration key on tenant-admin routes for a verified Admin only (and always
    strips a client-supplied one), so the browser never holds it. Compose no longer
    publishes the fronted services' host ports — they're reachable only through the gateway.
  - **Name↔id binding on conversion.** A supplied RM/analyst name must denote the same
    Access identity as the supplied id (no one-person's-UUID-with-another's-name).

- **Security audit round 8 — mandatory gateway path, injected service credentials, assignee verification, deployable RLS, proven under a superuser.**
  - **The gateway is now the single trust boundary.** It fronts *every* service and routes
    by path prefix (`/atlas`, `/vocx`, `/pulse`, `/orchestrator` → their services, stripping
    the prefix; everything else → the Register). The Compose NGINX edge no longer routes
    around the gateway to sub-services — one entry, one boundary. Closes "Compose routes
    services around Gateway; Gateway only knows how to forward to Register".
  - **Injected internal credentials.** The gateway strips the client's `X-API-Key` at the
    edge and injects the *scoped per-upstream* service credential (a leaked edge token can
    never be replayed on the data plane); the signed internal context is bound to the
    **downstream (stripped)** method + path. Closes "Gateway forwards the caller's API key".
  - **Read-only service principals can't read on their own key.** `enforce_service_read`
    blocks a named service with an empty allowlist (e.g. `svc_atlas`) from reading the data
    plane or `include_deleted` rows without a forwarded user context; machine line-writes go
    through the same operation gate as humans.
  - **Assignments.** The assignee's identity + role are **verified against the Access
    service** before an assignment (or a conversion's auto-assignment) is placed — a service
    can no longer assign an arbitrary UUID, nor a role the user doesn't hold. A machine
    caller may not enumerate the tenant-wide assignment directory (must filter by
    user/line).
  - **Change requests.** A Head now sees only their **vertical's** queue (derived from the
    same approver map enforcement uses) plus requests they raised; the approval path applies
    the **same transition state-machine** a direct edit obeys; and each decision takes a
    `FOR UPDATE` lock so concurrent approve/reject calls serialise.
  - **Conversion.** The idempotency hash is **bound to the lead id** (a reused key on a
    different lead is a 409, not a silent replay); auto-assignment ids are verified; and the
    Temporal workflow propagates the human **approver** as conversion provenance.
  - **RLS is deployable and PROVEN.** The Helm migration Job now injects
    `REGISTER_APP_PASSWORD` from the runtime secret, so a fresh deploy self-provisions the
    non-owner login with no manual `psql` step. The RLS boundary test no longer *skips*
    under a CI superuser — it creates a `NOBYPASSRLS` probe role and asserts fail-closed /
    isolation / `WITH CHECK` under `SET LOCAL ROLE`, so RLS is verified in exactly the
    environment that used to bypass it.
  - **Production values connect.** ATLAS/Workflows now present the shared Access key (was a
    mismatch); PULSE's front-door key uses the correct `config.apiKeys` path; the gateway's
    upstream URLs + injected per-upstream keys are wired via anchors so a merged render
    actually routes.

- **Security audit round 7 — service principals, mandatory signed context, transitions, RLS bootstrap, deployable prod values.**
  - **Signed context is now the SOLE identity path once configured.** When
    `internal_signing_secret` is set the Register no longer accepts legacy `X-User-*`
    headers (no downgrade), and every token is **bound to the request method + path** so it
    can't be replayed across routes within its TTL. ATLAS now mints a `GET`-bound signed
    context (replacing its plaintext headers + shared secret) that can never be replayed to
    a write route.
  - **Service principals (least privilege for machines).** A named service key
    (`svc_pulse`/`svc_vox`/`svc_workflows`/`svc_atlas`) binds a machine caller to an
    operation allowlist — enforced on line creates, company-scoped writes, custom
    financial/intel routes, interactions and assignments. `svc_pulse` can write
    intelligence but not create leads; `svc_atlas` is read-only. Closes "any Register-key
    holder can create financials / ack intel / log interactions", and unblocks the Temporal
    conversion under `enforceRbac` (svc_workflows may `push_lead_to_deals`).
  - **Transitions.** `Lead.status → Converted` is blocked in the generic PATCH for **every**
    caller (machine included); a transition graph (`ALLOWED_TRANSITIONS`) rejects undefined
    jumps; and change-request approval now re-checks the current value still equals
    `from_value` (409 on a stale request).
  - **Conversion.** Authorization now runs **before** any idempotency replay; the
    Idempotency-Key stores a real `request_hash` and rejects reuse with a different body;
    `rm`/`analyst` must be known people; and product-line owner assignments are created from
    `rm_id`/`analyst_id`.
  - **Exports & tenant admin.** Each exported table is gated by its own view permission;
    `include_deleted` always needs `backup_restore` (Admin-only, even for Management);
    generic-CRUD `include_deleted` is gated on the audit capability; tenant administration
    requires a **verified Admin identity** in addition to the admin key, stamps the verified
    actor (not client `X-Actor`), and sets the tenant GUC so its audit insert survives
    forced RLS.
  - **RLS is self-bootstrapping and deterministic.** A migrate-time `app.db.apply_rls` step
    runs every deploy: it creates/refreshes `register_app` (LOGIN + password from
    `REGISTER_APP_PASSWORD`) and (re)asserts FORCE to match the flag — so flipping
    `enforceRls` takes effect without hand-editing the DB. The RLS boundary test now
    honestly SKIPs under a superuser (which bypasses FORCE) instead of passing hollow.
  - **Production values connect as supplied.** `values-prod.yaml` uses YAML anchors so every
    cross-service credential matches (gateway↔access key, register data key, per-service
    keys, signing secret, gateway secret); CI now renders the prod overlay.

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
