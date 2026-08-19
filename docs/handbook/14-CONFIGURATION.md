# 14 — Configuration Reference

> **Audience:** whoever edits `.env` or a Helm values file.
> **Companion docs:** [02 Deployment](02-DEPLOYMENT-ARCHITECTURE.md) · [12 VocX](12-VOCX-STT.md) · [09 Backup](09-BACKUP-RESTORE.md)

---

## 1. How configuration works

Every Python service uses `pydantic-settings` with its **own env prefix**, subclassing
`BaseServiceSettings` from the shared core.

| Service | Prefix |
| --- | --- |
| register | `REGISTER_` |
| access | `ACCESS_` |
| gateway | `GATEWAY_` |
| atlas | `ATLAS_` |
| vocx | `VOCX_` |
| stt | `STT_` |
| pulse | `PULSE_` |
| workflows / orchestrator / notifier | `WORKFLOWS_` |

The compose file supplies each service's variables, and a **small set of top-level
`${...}` substitutions** comes from `deploy/compose/.env`. That is the file you edit on a
box — not the compose file itself.

### `.env` syntax

Compose's parser accepts **both** forms:

```dotenv
STT_CPU_THREADS=4
PULSE_SMTP_HOST: smtp.gmail.com
```

Both reach the container. Verify what actually landed rather than reasoning about it:

```bash
docker compose -f deploy/compose/docker-compose.yml exec pulse env | grep SMTP
```

### Container environment is captured at creation

A container reads its environment **when it is created**, not when it starts. After an
`.env` change:

```bash
docker compose -f deploy/compose/docker-compose.yml up -d <service>   # recreates
# NOT: docker compose restart <service>                                # keeps the old env
```

---

## 2. The `.env` variables that actually exist

These are the substitutions the compose file reads. Everything else is fixed in the compose
file itself.

### Secrets — change every one of these in production

| Variable | Default (dev) | What it is |
| --- | --- | --- |
| `PRISM_DB_PASSWORD` | `prism` | PostgreSQL password |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `prism` / `prism-secret` | Object store credentials |
| `INTERNAL_SIGNING_SECRET` | `compose-internal-signing-secret` | **Signs the internal context.** Shared by gateway, register, vocx |
| `SVC_ATLAS_KEY` | `compose-svc-atlas` | The `svc_atlas` principal |
| `SVC_VOX_KEY` | `compose-svc-vox` | The `svc_vox` principal |
| `SVC_PULSE_KEY` | `compose-svc-pulse` | The `svc_pulse` principal |
| `SVC_WORKFLOWS_KEY` | `compose-svc-workflows` | The `svc_workflows` principal |
| `SVC_GATEWAY_KEY` | `compose-svc-gateway` | `svc_gateway` — carries **no authority of its own**; it only ever delegates a user |
| `SVC_ADVAYA_KEY` | `compose-svc-advaya` | The Advaya boundary lane |
| `VOCX_FRONT_KEY` | `compose-vocx-front` | VocX's own front door — only the gateway's injected key is accepted |
| `STT_API_KEY` | `compose-stt-key` | STT front door |
| `PULSE_API_KEYS` | `compose-pulse-key` | PULSE front door |
| `WORKFLOWS_API_KEYS` | `compose-workflows-key` | Orchestrator front door |
| `WORKFLOWS_PAYLOAD_ENCRYPTION_KEY` | *(empty)* | **Set in production.** AES-256 key, base64url, exactly 32 bytes decoded |
| `ANTHROPIC_API_KEY`, `WORKFLOWS_ANTHROPIC_API_KEY` | *(empty)* | Empty ⇒ the deterministic stub runs |

### Edge and posture

| Variable | Default | Meaning |
| --- | --- | --- |
| `EDGE_HTTP_PORT` / `EDGE_HTTPS_PORT` | `80` / `443` | Published edge ports |
| `DEV_PORTS_BIND` | `127.0.0.1` | Interface the dev ports bind to. **Keep it loopback in production** |
| `GATEWAY_REQUIRE_AUTH` | `false` | **`true` in production** |
| `GATEWAY_OIDC_ISSUER` | *(empty)* | e.g. `http://dex:5556/dex` — as reachable *inside* the network |
| `GATEWAY_OIDC_AUDIENCE` | `prism` | |
| `GATEWAY_CORS_ORIGINS` | *(empty)* | Only needed if the UI is hosted off-origin |
| `GOOGLE_SSO_CLIENT_ID` | — | Dex's upstream Google connector |
| `UI_DEX_URL`, `UI_USE_REAL_API` | — | UI build-time wiring |

### VocX

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOCX_STT_BACKEND` | `api` | `api` · `faster_whisper` · `stub` |
| `VOCX_REDIRECT_URI` | localhost | **As the browser sees it.** Must match Google Console exactly |
| `VOCX_S3_PUBLIC_ENDPOINT_URL` | `http://localhost:9000` | Presigned playback URLs are opened by the browser |
| `VOCX_AUDIO_RETENTION_DAYS` | `0` | `0` = keep forever |
| `VOCX_DEV_UI` | `true` in dev | The `/vocx/v1/dev-ui` console. **Pinned off by the prod-posture overlay** |
| `VOCX_STT_PRIMING` | `true` | Prime Whisper with client names and finance terms |
| `VOCX_EXTRACT_GLOSSARY` / `_FEW_SHOT` / `_STRUCTURED` | `true` | Extraction quality switches |

### STT

| Variable | Default | Meaning |
| --- | --- | --- |
| `STT_CPU_THREADS` | `0` | `0` ⇒ CTranslate2 reads the **host's** cores. See §5 |

### PULSE

| Variable | Meaning |
| --- | --- |
| `PULSE_SCHEDULER_ENABLED` | Whether PULSE schedules its own scans |
| `PULSE_DISABLE_GDELT` | Turn off one source |
| `PULSE_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` / `_FROM` / `_FROM_NAME` | Digest email |

### Workflows / orchestrator / notifier

| Variable | Meaning |
| --- | --- |
| `WORKFLOWS_REQUIRE_AUTH` | Verify the approver's bearer rather than trusting a `by` field |
| `WORKFLOWS_APPROVER_NOTIFY` | Who is told a decision is awaited |
| `WORKFLOWS_NOTIFICATIONS_ENABLED`, `_NOTIFY_CHANNELS`, `_NOTIFY_WEBHOOK_URL`, `_SMS_WEBHOOK_URL`, `_OPS_WEBHOOK_URL` | Delivery |
| `WORKFLOWS_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_FROM` | Mail |
| `WORKFLOWS_NOTIFIER_INTERVAL_SECONDS`, `_NOTIFIER_MAX_ATTEMPTS` | Outbox drain |
| `WORKFLOWS_CAM_ENGINE` | `provider:model` for the CAM workbench |
| `WORKFLOWS_DOC_EXPIRY_WARN_DAYS`, `_DOC_EXPIRY_INTERVAL_HOURS` | Document expiry monitor |
| `WORKFLOWS_COVENANT_HORIZON_DAYS`, `_COVENANT_INTERVAL_HOURS` | Covenant monitor |
| `WORKFLOWS_EWS_ASSIGN_SLA_HOURS`, `_EWS_INVESTIGATION_SLA_HOURS`, `_EWS_ESCALATED_REMINDER_HOURS` | EWS SLAs |
| `WORKFLOWS_VOX_CONFIRM_AMBIGUOUS_COMPANY`, `_VOX_CONFIRM_LEAD_SELECTION` | Whether VocX parks for human confirmation |
| `WORKFLOWS_QUALIFICATION_CHECKLIST` | The qualification bar |
| `WORKFLOWS_SEARCH_ATTRIBUTES_ENABLED` | Reflect business status into Temporal search attributes |
| `WORKFLOWS_WORKER_BUILD_ID`, `_METRICS_BIND_ADDRESS` | Worker versioning and Prometheus |

### Other

| Variable | Meaning |
| --- | --- |
| `LMS_ENABLED` | **`false` today.** The book ends at `Disbursed`; no loan account is opened |
| `ADVAYA_URL` | The Advaya endpoint (simulated by default) |
| `ATLAS_STAGE_AMBER_DAYS` / `_RED_DAYS` | Ageing thresholds on the dashboard |
| `PGBACKUP_KEEP` | Dump retention days (default 14) |

---

## 3. Fixed configuration worth knowing

These are set in the compose file, not `.env`, but you will need them.

| Setting | Value | Note |
| --- | --- | --- |
| `REGISTER_SERVICE_API_KEYS` | `svc_atlas:… , svc_vox:… , svc_pulse:… , svc_workflows:… , svc_gateway:… , svc_advaya:…` | The named-principal map |
| `REGISTER_S3_STREAM_THROUGH_API` | `true` | Serve document bytes **through the API** rather than redirecting to a presigned MinIO URL. Without it, "View" 302s to `http://minio:9000/...` — a docker-internal host the browser cannot resolve — and the download dies silently. It also keeps the one-door posture |
| `REGISTER_WEB_CONCURRENCY` | `4` | |
| `REGISTER_DB_POOL_SIZE` | `20` | Postgres `max_connections` is capped so `pool_size × replicas` stays under the ceiling |
| `GATEWAY_CACHE_TTL_S` | `30` | Permission cache |
| `ATLAS_PERMISSION_CACHE_TTL_S` | `30` | |
| `ATLAS_MAX_PAGES_PER_RESOURCE` | `10` | ×200 rows per vertical per request |
| `REGISTER_ADVAYA_INTEGRATION_ENABLED` | `true` | The acknowledgement lane, against a simulated peer |

---

## 4. The production posture — what flips

`docker-compose.prod-posture.yml`, applied as a second `-f`:

| Setting | Dev | Production |
| --- | --- | --- |
| `GATEWAY_REQUIRE_AUTH` | `false` | **`true`** |
| `GATEWAY_OIDC_ISSUER` | empty | `http://dex:5556/dex` |
| `GATEWAY_OIDC_ALLOWED_DOMAINS` | empty | **`evamfinance.com`** |
| `REGISTER_ENFORCE_RBAC` | `false` | **`true`** |
| `REGISTER_ENFORCE_RLS` | `false` | **`true`** |
| `REGISTER_ONLINE_REVALIDATION` | `false` | **`true`** |
| `ACCESS_AUTO_SEED` | `true` | **`false`** |
| `WORKFLOWS_REQUIRE_AUTH` | `false` | **`true`** |
| `VOCX_DEV_UI` | `true` | **`false`** |

```bash
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/docker-compose.prod-posture.yml \
               --profile sso --profile backup up -d --build
```

> **`--profile sso` is required.** Dex is profile-gated in the base file and an override
> cannot un-gate it — Compose filters profiled services out *before* merging. Without the
> flag you get `REQUIRE_AUTH` on with no issuer reachable, and every request 401s.

---

## 5. `STT_CPU_THREADS`, once and clearly

```python
# services/stt/app/config.py
cpu_threads: int = 0
```

| Value | Behaviour |
| --- | --- |
| `0` | CTranslate2 chooses, based on the **host's** core count — which it reads regardless of the container's CPU quota |
| `N` | Exactly N decoder threads |

**It changes scheduling only. Accuracy, model, beam width and transcript are untouched.**

| Deployment | `stt` CPU limit | Correct value |
| --- | --- | --- |
| **Helm** | `limits.cpu: 2` | **`2`** |
| **Compose** | **none** | Derive from `nproc`. `0` is often right; `2` on a 4-core box is a throttle |

The failure mode `0` guards against: a container limited to 2 cores spawning a thread per
*host* core, then having those threads fight over the 2 it actually has — decoding **slower**
than 2 threads would.

---

## 6. UI build-time variables

The UI is a static bundle, so these are **baked in at build time** — changing them means
rebuilding the `ui` image.

| Variable | Default | Meaning |
| --- | --- | --- |
| `VITE_USE_REAL_API` | — | `true` for a real backend |
| `VITE_API_BASE_URL` | `/v1` | The Register prefix |
| `VITE_ACCESS_URL` | `/access` | |
| `VITE_VOCX_URL` | `/vocx` | Rooted at the origin, **not** under `/v1` |
| `VITE_VOCX_MAX_SECONDS` | `180` | Recording cap, clamped 30–600 |

```bash
# Rebuild after changing any of them
docker compose -f deploy/compose/docker-compose.yml build ui
docker compose -f deploy/compose/docker-compose.yml up -d ui
docker compose -f deploy/compose/docker-compose.yml exec nginx nginx -s reload
# then hard-refresh the browser (Ctrl+Shift+R)
```

Declared as `ARG`/`ENV` pairs in `deploy/ui-image/Dockerfile`.

---

## 7. Timeouts — the four places that must agree

| Path | Browser | nginx | gateway | Helm |
| --- | --- | --- | --- | --- |
| default | axios default | 65 s | 60 s | main Ingress |
| `/vocx/v1/capture` | **300 s** | 305 s | 600 s | slow Ingress |
| `/orchestrator/v1/cam/` | 620 s | 625 s | 600 s | slow Ingress |
| `/pulse/v1/news/sweep` | — | 625 s | 600 s | slow Ingress |

Adding a slow endpoint means editing **all four**:

1. `deploy/nginx/nginx.conf` — a `location` with its own `proxy_read_timeout`
2. `services/gateway/app/main.py` — `_SLOW_PATHS`
3. The browser client — `services/atlas/ui/src/api/*.ts`
4. `gateway.ingress.slowPaths` in Helm values

Widening one alone does nothing; the shortest hop decides.

---

## 8. Helm values

`deploy/helm/prism/values.yaml`, one block per subchart:

```yaml
global:
postgresql:
register:
access:
gateway:
ui:
vocx:
stt:            # config.cpuThreads: 2   ← matches limits.cpu: 2
pulse:
atlas:
minio:
temporal:
workflows:
```

```bash
helm upgrade --install prism deploy/helm/prism -n prism --create-namespace -f prod-values.yaml
```

> Set `gateway.ingress.slowPaths` from a **values file**, never `--set`. Helm's `--set`
> grammar mis-parses `slowPaths=[]`; a values file behaves correctly.

Secrets in Kubernetes go in `Secret` objects referenced by the values, not inline.

---

## 9. Secrets handling

The three paths that live only on disk, **never** in git or a delivery archive:

```
deploy/compose/.env
deploy/vocx-secrets/
deploy/nginx/certs/
```

Verify every archive you produce:

```bash
unzip -l "$ZIP" | grep -Ei "client_secret|token\.json|vocx-secrets/.*json|vocx_tokens|compose/\.env"
# exit 1 (no output) = clean
```

`prism-deploy.sh` snapshots all three before an upgrade and restores them into the new tree,
failing hard if `.env` is missing afterwards.

---

## 10. Configuration checklist for a new production deployment

- [ ] Every secret in §2 changed from its dev default
- [ ] `INTERNAL_SIGNING_SECRET` is long and random — it signs identity
- [ ] `WORKFLOWS_PAYLOAD_ENCRYPTION_KEY` set (32 bytes, base64url) so Temporal history is ciphertext
- [ ] `GATEWAY_REQUIRE_AUTH=true` and an issuer configured
- [ ] `GATEWAY_OIDC_ALLOWED_DOMAINS` set — mandatory with a consumer IdP
- [ ] `REGISTER_ENFORCE_RBAC`, `REGISTER_ENFORCE_RLS`, `REGISTER_ONLINE_REVALIDATION` all `true`
- [ ] `ACCESS_AUTO_SEED=false`
- [ ] `VOCX_DEV_UI=false`
- [ ] `VOCX_REDIRECT_URI` is the public HTTPS URL, matching Google Console exactly
- [ ] `VOCX_S3_PUBLIC_ENDPOINT_URL` is browser-reachable
- [ ] `DEV_PORTS_BIND=127.0.0.1` (or the ports closed at the security group)
- [ ] `STT_CPU_THREADS` derived from `nproc` (compose) or set to `2` (Helm)
- [ ] `--profile sso --profile backup`
- [ ] A CA-issued certificate in `deploy/nginx/certs/`
- [ ] SMTP configured, and a test digest actually received
- [ ] `.env` is `chmod 600` and owned by the deploy user
