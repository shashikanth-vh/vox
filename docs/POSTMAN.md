# Using the PRISM Postman collections

The files in `postman/`:

| File | What it is |
|------|-----------|
| `PRISM_E2E_Full.postman_collection.json` | ⭐ **The end-to-end journey** — 14 folders / 68 requests via the NGINX edge, user-creation → client → lead → deal → lending/syndication/asset-monetisation to terminal, works in the dev AND the prod posture (§10–11) |
| `PRISM_Full.postman_environment.json` | ⭐ Its environment, **dev posture** (`dexUrl` empty ⇒ header trust, sign-in folders skip themselves) |
| `PRISM_Full_Dex.postman_environment.json` | ⭐ Its environment, **Dex / prod posture** — identical except `dexUrl` is pre-filled, which is the one switch that turns folder 00b (sign-in) on. Use THIS instead of hand-copying the dev one: a copy that misses a variable sends literal `{{adminToken}}` text and 401s confusingly |
| `Register.postman_collection.json` | **186 requests** — every Register endpoint, in 27 folders by tag (CRUD + evidence, decisions, CP/CS checklists, handover packages) |
| `Orchestrator.postman_collection.json` | **14 requests** — the whole workflow plane (qualification, structuring, document collection, CP/CS checklist, Advaya handover prepare + approve) |
| `PRISM_UI_CRUD.postman_collection.json` | **158 requests** — just the table CRUD the PRISM UI calls, every one routed through the **NGINX edge** |
| `PRISM.postman_environment.json` | Shared variables for the three reference collections above |

They are generated from the frozen OpenAPI contracts (`docs/openapi/*.json`), so they always
match what the frontend codegens against — never hand-edited.

---

## 1. Import and select the environment

Import all three files, then **pick `PRISM — Local` in the environment dropdown (top right).**

> This is the single most common mistake. Every request uses variables (`{{baseUrl}}`,
> `{{orchestratorUrl}}`, `{{apiKey}}`…). With **"No environment"** selected, nothing resolves,
> URLs show red, and no request will send.

If you still have an older **`PRISM Register — Local`** environment, delete it — it predates the
orchestrator collection and lacks `orchestratorUrl`.

## 1b. The PRISM UI CRUD collection (through NGINX)

`PRISM_UI_CRUD.postman_collection.json` is the one to hand a UI developer. It contains **only the
table CRUD** — 158 requests in 20 folders, one folder per table — and **every request goes through
the NGINX edge**, exactly as the browser will:

```
{{baseUrl}} = https://localhost:8443   →  NGINX (TLS)  →  gateway (RBAC gate)  →  Register
```

That path matters: it is the only one where the **gateway actually authorizes the call**. Hitting
the Register directly on `:8000` (`{{registerDirectUrl}}`) skips the gate, so a UI bug that relies
on direct access will pass in Postman and fail in production.

Every table follows the same six-operation contract:

| Method + path | Purpose |
|---|---|
| `GET /v1/<table>` | List (filters + pagination as query params, disabled by default) |
| `POST /v1/<table>` | Create |
| `GET /v1/<table>/{{id}}` | Read one |
| `PATCH /v1/<table>/{{id}}` | Update (optional `If-Match` for optimistic locking) |
| `DELETE /v1/<table>/{{id}}` | Soft delete (Admin only) |
| `POST /v1/<table>/{{id}}/restore` | Undo the soft delete |

Tables covered: `entities`, `leads`, `deals`, `lending`, `syndication` (+ nested `lenders`),
`asset-monetisation`, `contracts-assets`, `financials`, `monitoring`, `intel`, `people`,
`counterparties`, plus `interactions` and `documents` (nested per subject type), `users`/RBAC,
`ref`, `settings`, `export` and `audit`.

Governance endpoints — CP/CS checklists and Advaya handover packages — are **not** in this
collection; they live in the full `Register.postman_collection.json`, because the UI reaches them
through the workflow plane (maker/checker), not through plain CRUD.

## 2. Know which host you are hitting

The **NGINX edge on `:8443` (HTTPS) is the one front door**:

| Variable | Value | Routes to |
|---|---|---|
| `baseUrl` | `https://localhost:8443` | `/` → gateway → **Register** |
| `orchestratorUrl` | `https://localhost:8443/orchestrator` | → **orchestrator** (workflow plane) |
| `registerDirectUrl` | `http://localhost:8000` | Register directly, plaintext (bypasses the RBAC gate) |
| `orchestratorDirectUrl` | `http://localhost:8006` | Orchestrator directly, plaintext |

**The edge speaks HTTPS.** It terminates TLS with a self-signed certificate, so before the first
run generate one — `scripts/gen_dev_certs.sh` — or nginx will not start. Then either:

- **Postman:** Settings → General → turn **"SSL certificate verification" OFF** (simplest), or
- **trust the cert:** import `deploy/nginx/certs/tls.crt` (Settings → Certificates, or your OS
  trust store) and leave verification on.

`http://localhost:8080` still answers, but only with a **301 to HTTPS** (plus `/healthz`,
`/readyz`), so plaintext can't be used by accident. Don't point `baseUrl` at `:8080`: clients
that follow the 301 replay a POST as a GET, so every create quietly turns into a list.

**Postman on one machine, PRISM on another (e.g. an Ubuntu VM)?** Replace `localhost` with the
VM's IP in `baseUrl`, `accessUrl`, `orchestratorUrl` and `dexUrl` — e.g.
`https://192.168.44.128:8443` and `http://192.168.44.128:5556`. The compose file publishes both
ports on all interfaces, so nothing changes server-side. Certificate-wise, either keep SSL
verification off, or mint the cert with the VM's address in its SANs so it verifies:

```bash
EXTRA_SANS="IP:192.168.44.128" scripts/gen_dev_certs.sh --force   # then restart nginx
```

The edge forwards **everything** to the gateway; the **gateway** is what routes by path prefix
(`/atlas`, `/vocx`, `/pulse`, `/orchestrator` → those services, stripping the prefix and injecting
that service's scoped credential; anything else → the Register behind the RBAC gate). Nothing is
meant to be reached around the gateway, which is why both hosts above are the same door — and why
the `*DirectUrl` values are for debugging only: they bypass the gate.

## 3. Credentials — the gateway injects them, you don't

**Postman does not send a backend API key.** The gateway *strips* any `X-API-Key` a client
presents and injects the correct **scoped upstream credential** itself (`GATEWAY_REGISTER_API_KEY`,
`GATEWAY_ORCHESTRATOR_API_KEY`, …). That is deliberate: a client can never hold a data-plane key,
and each backend accepts only the gateway's own key.

So `X-API-Key: {{apiKey}}` ships **disabled** on every request. Enable it only if you repoint
`baseUrl` at `{{registerDirectUrl}}` (`:8000`) or `{{orchestratorDirectUrl}}` (`:8006`) — which
bypasses the RBAC gate and is for debugging, not for validating behaviour.

Every request does send `X-Tenant: {{tenant}}` (`EVAM`), which binds PostgreSQL row-level
security, so you only ever see that tenant's rows.

## 4. Identity: how RBAC decides

| Header | What the gateway does with it |
|---|---|
| `Authorization: Bearer …` | **Preserved.** The real identity in production — set `bearerToken` and enable the header once `GATEWAY_OIDC_ISSUER` is configured |
| `X-User-Email` | Read as the caller **only in dev** (no OIDC issuer set), then stripped and re-minted into a signed internal context. This is what makes local testing work |
| `X-User-Roles` | **Not trusted.** The gateway resolves the caller's roles from the **Access** service, so editing this variable does not change authorization at the edge |

That last row matters: to test role behaviour, change **the user** (`userEmail`) to someone whose
roles in Access are what you want to exercise — don't just change `userRoles`. For example, with a
`BDRM` user, `edit_lead` and `log_interaction` are **SCOPED**: their own book succeeds, another
RM's lead returns **403**. A `Credit Head` is required for CP/CS and handover approval.

If `GATEWAY_REQUIRE_AUTH=true` (or an issuer is set) and you send no bearer, the gateway returns
**401** — it will not fall back to trusting `X-User-Email`.

## 5. Maker–checker requests need two different people

CP/CS approval and Advaya handover approval **enforce that the checker is a different
authenticated user than the maker** — self-approval is refused by the Register, not just by the UI.
The environment ships two identities for this:

- `makerEmail` = `maker@evamfinance.com` — prepares
- `checkerEmail` = `checker@evamfinance.com` — approves
- `seniorRoles` = `Credit Head` — the authority both need

So for the handover pair:

1. `POST {{orchestratorUrl}}/v1/workflows/advaya-handover` with
   `X-User-Email: {{makerEmail}}`, `X-User-Roles: {{seniorRoles}}` → package **Prepared**
   (the stage does **not** advance yet).
2. `POST {{orchestratorUrl}}/v1/workflows/advaya-handover/{{lendingId}}/approve` with
   `X-User-Email: {{checkerEmail}}` → **HandedOver**, stage advances in one transaction.

Reuse the maker's e-mail on step 2 and you should get a **403** — that's the control working.

## 6. Chaining requests — fill the id variables

Ids start empty and are meant to be filled as you go: `id`, `entityId`, `lendingId`,
`checklistId`, `workflowId`, `synId`. Either paste a value from a previous response, or add a
Postman **test script** to capture it automatically:

```javascript
// On a create request's Tests tab:
pm.environment.set("lendingId", pm.response.json().id);
```

A typical first run:

1. `GET {{baseUrl}}/healthz` — confirm the stack is up.
2. `GET {{baseUrl}}/v1/entities` — list companies (empty on a fresh DB; see
   `docs/WSL_DEPLOY.md` §3 to load data).
3. `POST {{baseUrl}}/v1/entities` — create one, save its `id` into `entityId`.
4. `POST {{baseUrl}}/v1/lending` — open a lending line, save `id` into `lendingId`.
5. Walk the stage pipeline, then exercise the CP/CS + handover pairs above.

## 7. Concurrency and idempotency headers

- **`If-Match`** — present but **disabled** on every `PATCH`. Enable it (value `"1"`, the row's
  `version`) to get optimistic locking; a stale version returns **409**. Leave it off for
  last-write-wins.
- **`Idempotency-Key`** — send any unique string on a create to make a retry safe: the original
  outcome replays with `Idempotency-Replay: true` instead of creating a duplicate row.
- **`X-Request-ID`** — set by the edge if you don't send one; it appears in every service log, so
  it's the fastest way to trace one call end to end.

## 8. Sample bodies are scaffolding, not valid data

Bodies are generated from the JSON Schemas: strings come through as `"sample <field>"`, enums take
their first value, dates are `2026-03-31`, and nested objects (executed document refs, CP/CS
checklist items) are filled in one level deep. **Replace the values** — the Register validates
mandatory fields, lifecycle transitions and evidence, so placeholder text will legitimately be
rejected (usually **422**).

## 9. Regenerating after an API change

```bash
scripts/export_openapi.sh      # refresh docs/openapi/*.json AND regenerate the collections
python scripts/gen_postman.py  # collections only (env is always rewritten)
```

Commit the regenerated files in the same PR as the API change so the contract, the collections and
the code never drift. Running the generator without the specs present rewrites only the
environment and tells you to run `export_openapi.sh` first.

## Troubleshooting

| Symptom | Cause |
|---|---|
| URL shows red / `{{baseUrl}}` unresolved | No environment selected — pick `PRISM — Local` |
| `SSL Error: self signed certificate` | Turn off SSL verification in Postman, or trust `deploy/nginx/certs/tls.crt` |
| Connection refused on `:8443` | Certs missing — run `scripts/gen_dev_certs.sh`, then restart nginx |
| Unexpected **301** | You used `http://…:8080`; the edge redirects to HTTPS |
| **404** on an orchestrator request | Missing the `/orchestrator` prefix — the gateway routes on it |
| **401** | OIDC is on and no valid `Authorization: Bearer` was sent (the gateway will not fall back to `X-User-Email`) |
| **403** on approve | Maker and checker are the same person, or the role lacks senior authority |
| **403** on a lead edit | Role is `BDRM` and the record isn't in that RM's scope — working as designed |
| **409** | `If-Match` version is stale — re-read the row and retry |
| **422** | Placeholder body, or a lifecycle/evidence rule refused the write — read the `detail` |

---

## 11. Running the journey in the PRODUCTION POSTURE

The dev-default run proves the business flow. It does **not** prove the controls that matter most in
production, because four of them default to `false`. Run it again with them on:

```bash
scripts/gen_dev_certs.sh                      # once
cd deploy/compose
docker compose -f docker-compose.yml -f docker-compose.prod-posture.yml \
  --profile sso up -d --build     # --profile sso is REQUIRED — it starts Dex, the issuer
```

| Control | Dev default | Prod posture |
|---|---|---|
| `GATEWAY_REQUIRE_AUTH` + OIDC issuer | off — `X-User-Email` trusted | **on** — identity ONLY from a verified bearer |
| `WORKFLOWS_REQUIRE_AUTH` + issuer | off — an approver's `by` trusted | **on** |
| `REGISTER_ENFORCE_RBAC` | off | **on** — no user context ⇒ refused |
| `REGISTER_ENFORCE_RLS` | off | **on** — PostgreSQL row-level security converged at startup |

**`--profile sso` is not optional.** Dex is profile-gated in the base compose file and an override cannot un-gate it — Compose filters profiled services out before merging, so the flag is what actually starts the issuer. Omit it and the stack runs with `REQUIRE_AUTH` on and nothing to validate against, so every request 401s.

### The collection works in both postures, unchanged

Every request carries `Authorization: Bearer {{…Token}}` **and** `X-User-Email`:

* **dev** — select **`PRISM — Full (via NGINX)`**: `dexUrl` is empty, so folder **00b skips
  itself**, the token variables stay empty, `Bearer ` carries no token, and the gateway falls back
  to header trust.
* **prod posture** — select the shipped **`PRISM — Full (via NGINX) · Dex prod posture`**
  environment (`PRISM_Full_Dex.postman_environment.json`): identical except `dexUrl` is pre-filled
  with `http://localhost:5556`, so folder **00b · Sign in (Dex)** fills `adminToken` /
  `makerToken` / `checkerToken` via the password grant, and the verified bearer becomes the
  identity; `X-User-Email` is ignored. Don't hand-copy the dev environment instead — a copy that
  misses a variable sends literal `{{adminToken}}` text and 401s in a way that looks like a
  platform bug.

The skip is a **pre-request** `pm.execution.skipRequest()`, not mere tolerance of an error: when Dex
isn't running the request would fail at the transport layer, the test script would never run, and a
`--bail` newman run would abort the journey right there. Empty `dexUrl` ⇒ no request goes out at all.

### Why the identities are fixed

A bearer can only be issued for someone the IdP knows, and maker-checker needs two DISTINCT verified
people. So the journey uses stable e-mails — `e2e.rm@`, `e2e.maker@`, `e2e.checker@evamfinance.com`
(password `prism`, defined in `deploy/compose/dex/config.yaml`) — instead of per-run generated ones.
User provisioning is therefore idempotent: the create accepts **201 or 409**, and a follow-up
`GET /access/v1/users?q=<email>` resolves the id either way.

For a real deployment, point `GATEWAY_OIDC_ISSUER` at your own IdP (Entra ID, Okta, Keycloak),
delete the `dex` service, and get tokens from there — nothing else changes.

### 11b. Google in production, Dex in dev — both at once

You do not have to choose. Every service (gateway, orchestrator, ATLAS) accepts a **registry** of
issuers instead of a single one:

```bash
GATEWAY_OIDC_ISSUERS="https://accounts.google.com|<client-id>.apps.googleusercontent.com,http://dex:5556/dex|prism"
GATEWAY_OIDC_ALLOWED_DOMAINS="evamfinance.com"
```

The format is `issuer|audience` pairs, comma-separated. When set it takes precedence over the single
`GATEWAY_OIDC_ISSUER` / `_AUDIENCE` pair. A token is verified **only** by the issuer matching its own
`iss` claim, so adding a dev IdP never weakens the production one, and an unknown `iss` is refused.

**`_ALLOWED_DOMAINS` is mandatory whenever Google (or Microsoft) is accepted.** A Google token proves
the account is real — not that it belongs to Evam. Without the allowlist any personal `@gmail.com`
authenticates successfully and is stopped only later by the user lookup; with it, a non-Evam identity
is refused during authentication. The Helm chart **fails the render** if you configure a public
issuer with an empty allowlist, per service.

Recommended split:

| environment | issuer | why |
| --- | --- | --- |
| production | Google only, allowlist set | staff already have Workspace accounts; no passwords to hold |
| dev / CI | Dex only | Google has no password grant, so an unattended run needs Dex |
| staging | both | verify the real IdP while CI keeps its automated sign-in |

#### Signing in with Google from Postman — folder `00c`

Google publishes **no password grant**, so an unattended collection cannot mint a token from an
e-mail and password. Folder **`00c · Sign in (Google)`** therefore uses the **refresh-token** grant,
which needs a one-time consent per test identity:

1. In Google Cloud → *Credentials*, create an **OAuth client (Web application)** and add
   `https://oauth.pstmn.io/v1/callback` as an authorised redirect URI.
2. In Postman, on the collection's *Authorization* tab, run **Get New Access Token** with
   `access_type=offline` and `prompt=consent`, signing in as the test identity.
3. Copy the **refresh token** into `adminRefreshToken` / `makerRefreshToken` /
   `checkerRefreshToken`, and the client id/secret into `googleClientId` / `googleClientSecret`.

Then folder 00c exchanges each refresh token for a fresh token on every run — no interactive step.

Two things that will bite you otherwise:

* **Take the `id_token`, not the `access_token`.** Google's access tokens are opaque strings with no
  verifiable claims; only the `id_token` is a JWT the gateway can validate. Folder 00c reads
  `id_token` for exactly this reason.
* **The audience is the client id**, not `prism` — hence the `|<client-id>.apps.googleusercontent.com`
  half of the issuer spec above.

> **Secrets.** A refresh token is a long-lived credential and a client secret is a real secret. Keep
> both in the Postman **Vault** or a secret-typed environment variable. Never commit them, and never
> hand over an exported collection or environment containing them — an export includes variable
> values.

Folder 00c **skips itself** when `googleClientId` is empty (a pre-request `pm.execution.skipRequest()`),
so a Dex or dev run makes no outbound call to Google at all and the run report stays clean. Fill in
the client id to activate it.

### In CI

`.github/workflows/e2e.yml` runs the collection with **newman twice** — once dev-default, once under
the prod-posture overlay — with `--bail`, and uploads both JSON reports. An integration bug now fails
the build instead of surfacing in someone's Postman.
