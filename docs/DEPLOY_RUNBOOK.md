# PRISM — deployment runbook (step by step)

Exact steps to stand the platform up, **including where every secret file lives**.
Two paths: **A. Docker Compose on a VM** (the standard single-box deployment) and
**B. Helm on Kubernetes**. Background and options are in
`docs/PRODUCTION_DEPLOYMENT.md`; this file is the checklist you follow.

---

## The secret-file map (memorise this table)

Nothing secret is ever inside the repo or the release zip. Everything secret lives in
exactly these places:

| What | Compose location | Helm location |
|---|---|---|
| All passwords / keys / toggles | `deploy/compose/.env` | `--set` flags / your values file kept outside git |
| Google OAuth **client secret JSON** (VocX voice capture) | `deploy/vocx-secrets/client_secret.json` | `kubectl create secret generic prism-vocx-google --from-file=client_secret.json` |
| Google **token** minted after first VocX consent | appears next to it in `deploy/vocx-secrets/` (VocX writes it) | stored on the VocX PVC |
| Google **SSO client id** (login button — public, not a secret, still config) | `GOOGLE_SSO_CLIENT_ID=` in `.env` | `--set gateway.oidc.audience=…` + ui build arg |
| TLS certificate + key | `deploy/nginx/certs/tls.crt` + `tls.key` | `Secret prism-edge-tls` (cert-manager creates it, or you do) |
| DB / MinIO passwords | `PRISM_DB_PASSWORD`, `MINIO_ROOT_*` in `.env` | `--set postgresql.auth.password=…` etc. |

> **Upgrades never touch these** as long as you follow the upgrade ritual in step A9 —
> it backs up all three compose locations before replacing the tree.

---

## A. Docker Compose on a VM

### A1. Prerequisites

- A VM with Docker Engine + the compose plugin (`docker compose version` ≥ 2.20).
- DNS: an `A` record `prism-evamfinance.com → <VM IP>` (public DNS, or your internal
  DNS / router for an office-only deployment).
- Ports 80 and 443 free on the VM (or set `EDGE_HTTP_PORT` / `EDGE_HTTPS_PORT` later).

### A2. Get the code

```bash
unzip prism-<hash>.zip && cd prism        # or: git clone <repo> prism && cd prism
```

### A3. Create the secret directories and `.env`

```bash
mkdir -p deploy/vocx-secrets deploy/nginx/certs
cp /dev/null deploy/compose/.env          # start empty, then fill from the template:
```

Put this in `deploy/compose/.env` and **replace every CHANGE-ME** (long random strings:
`openssl rand -hex 24`):

```bash
# ---- core secrets (MANDATORY to change for production) ----------------------
PRISM_DB_PASSWORD=CHANGE-ME                 # shared PostgreSQL (all services + backups)
MINIO_ROOT_USER=prism
MINIO_ROOT_PASSWORD=CHANGE-ME               # document store
INTERNAL_SIGNING_SECRET=CHANGE-ME           # signed internal identity context
SVC_ATLAS_KEY=CHANGE-ME                     # per-service principals (one each)
SVC_VOX_KEY=CHANGE-ME
SVC_PULSE_KEY=CHANGE-ME
SVC_WORKFLOWS_KEY=CHANGE-ME
SVC_GATEWAY_KEY=CHANGE-ME
SVC_ADVAYA_KEY=CHANGE-ME
PULSE_API_KEYS=CHANGE-ME
WORKFLOWS_API_KEYS=CHANGE-ME
VOCX_FRONT_KEY=CHANGE-ME
STT_API_KEY=CHANGE-ME

# ---- sign-in ----------------------------------------------------------------
# Google button on the login screen (A5 below creates this id):
GOOGLE_SSO_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
# Accept Google tokens at the gateway/orchestrator (same id as above):
GATEWAY_OIDC_ISSUERS=https://accounts.google.com|1234567890-abc.apps.googleusercontent.com,http://dex:5556/dex|prism
WORKFLOWS_OIDC_ISSUERS=https://accounts.google.com|1234567890-abc.apps.googleusercontent.com,http://dex:5556/dex|prism
# Dex sign-in form posts same-origin through the edge:
UI_DEX_URL=https://prism-evamfinance.com

# ---- AI features (optional but used by CAM drafting / VocX extraction) ------
ANTHROPIC_API_KEY=sk-ant-...
WORKFLOWS_ANTHROPIC_API_KEY=sk-ant-...

# ---- VocX voice capture -----------------------------------------------------
# The OAuth callback as the BROWSER reaches it (must also be registered in Google):
VOCX_REDIRECT_URI=https://prism-evamfinance.com/vocx/v1/auth/callback

# ---- edge / housekeeping (defaults shown — change only if needed) -----------
#EDGE_HTTP_PORT=80
#EDGE_HTTPS_PORT=443
#PGBACKUP_KEEP=14
```

(Every other knob — SMTP for e-mail notifications, SLA hours, webhooks — is listed with
its default by `grep -ohE '\$\{[A-Z_]+' deploy/compose/*.yml`; all optional.)

### A4. Google Cloud Console — the client secret JSON (VocX)

VocX (voice capture → Gmail/Calendar logging) authenticates to Google **as the app**,
with a client secret file:

1. Google Cloud Console → *APIs & Services → Credentials* → **Create credentials →
   OAuth client ID → Web application**.
2. Add the authorized redirect URI:
   `https://prism-evamfinance.com/vocx/v1/auth/callback`
   (exactly the `VOCX_REDIRECT_URI` from `.env`).
3. **Download the JSON** and save it as — this is the one true location:

   ```
   deploy/vocx-secrets/client_secret.json
   ```

4. That directory is mounted read-only into the VocX container at
   `/run/vocx-secrets/`. After the first in-app Google consent, VocX writes the minted
   token alongside it. **Neither file must ever enter git or a zip** (the delivery
   ritual's secret scan checks precisely this).

### A5. Google Cloud Console — the SSO client id (login button)

1. Same console → **Create credentials → OAuth client ID → Web application**.
2. Authorized **JavaScript origin**: `https://prism-evamfinance.com`.
3. Copy the **client id** (public, ends `.apps.googleusercontent.com`) into `.env` as
   `GOOGLE_SSO_CLIENT_ID` and inside both `*_OIDC_ISSUERS` lines (step A3). Full
   detail: `docs/GOOGLE_SSO.md`.

### A6. Certificates

Pick one (details + renewal automation: `docs/PRODUCTION_DEPLOYMENT.md` §2):

```bash
# (a) Let's Encrypt — public DNS required
sudo certbot certonly --standalone -d prism-evamfinance.com
scripts/install_edge_certs.sh \
  /etc/letsencrypt/live/prism-evamfinance.com/fullchain.pem \
  /etc/letsencrypt/live/prism-evamfinance.com/privkey.pem

# (b) Corporate / purchased pair
scripts/install_edge_certs.sh company-fullchain.pem company-privkey.pem

# (c) Self-signed (internal only)
EXTRA_SANS="DNS:prism-evamfinance.com,IP:<VM-IP>" scripts/gen_dev_certs.sh --force
```

Either way the files end up as `deploy/nginx/certs/tls.crt` + `tls.key`, which the edge
reads. The UI image build needs no cert knowledge.

### A7. First start

```bash
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod-posture.yml \
  --profile sso --profile backup up -d --build
```

First boot: migrations run, the Access service bootstraps the RBAC baseline and the
default users (`admin@evamfinance.com`, `tech@evamfinance.com` — TechAdmin/Admin), the
FI master seeds. Watch it: `docker compose logs -f access register gateway`.

### A8. Verify

```bash
curl -fsS https://prism-evamfinance.com/healthz && echo edge-ok
```

Then in a browser: **https://prism-evamfinance.com** → login (Google button with your
workspace account, or Dex `tech@evamfinance.com` / `prism`) → the Today page. As Admin,
Tools shows Import/Export ledger and Backup/Restore.

### A9. Upgrades (every new release zip)

```bash
# 1. Preserve the three secret locations (they live inside the tree):
cp deploy/compose/.env /root/prism-backup/env
cp -a deploy/vocx-secrets /root/prism-backup/
cp -a deploy/nginx/certs /root/prism-backup/
# 2. Fresh tree — never unzip over the old one:
cd .. && rm -rf prism && unzip prism-<newhash>.zip && cd prism
# 3. Restore secrets:
cp /root/prism-backup/env deploy/compose/.env
cp -a /root/prism-backup/vocx-secrets/. deploy/vocx-secrets/
cp -a /root/prism-backup/certs/. deploy/nginx/certs/
# 4. Rebuild:
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod-posture.yml \
  --profile sso --profile backup up -d --build
```

Data (postgres, minio, dex, backups) is on named volumes — untouched by all of this.

---

## B. Helm on Kubernetes

### B1. Prerequisites

- A cluster with an **ingress-nginx** controller and (recommended) **cert-manager**
  with a `letsencrypt-prod` ClusterIssuer.
- A container registry your cluster can pull from.

### B2. Build and push the images

```bash
REG=<registry>; TAG=<version>
docker build -f services/register/Dockerfile  . -t $REG/prism-register:$TAG
docker build -f services/access/Dockerfile    . -t $REG/prism-access:$TAG
docker build -f services/gateway/Dockerfile   . -t $REG/prism-gateway:$TAG
docker build -f services/atlas/Dockerfile     . -t $REG/prism-atlas:$TAG
docker build -f services/vocx/Dockerfile      . -t $REG/prism-vocx:$TAG
docker build -f services/stt/Dockerfile       . -t $REG/prism-stt:$TAG
docker build -f services/pulse/Dockerfile     . -t $REG/prism-pulse:$TAG
docker build -f services/workflows/Dockerfile . -t $REG/prism-workflows:$TAG
docker build -f deploy/ui-image/Dockerfile    . -t $REG/prism-ui:$TAG \
  --build-arg VITE_GOOGLE_SSO_CLIENT_ID=<sso-client-id> \
  --build-arg VITE_USE_REAL_API=true
docker push  # …each of the above
```

(The SSO client id is compiled into the UI bundle — it is public, not a secret.)

### B3. Create the out-of-band secrets

```bash
kubectl create namespace prism

# VocX's Google client secret JSON (the SAME file as compose step A4):
kubectl -n prism create secret generic prism-vocx-google \
  --from-file=client_secret.json=./client_secret.json

# Register's runtime (non-owner, RLS-bound) database login:
kubectl -n prism create secret generic prism-register-app-secret \
  --from-literal=password="$(openssl rand -hex 24)"

# TLS: with cert-manager NOTHING to do (the ingress annotation in values-prod has it
# issue + renew `prism-edge-tls`). Bringing corporate certs instead:
#   kubectl -n prism create secret tls prism-edge-tls --cert=fullchain.pem --key=privkey.pem
#   ...and delete the cert-manager annotation from values-prod.
```

### B4. Install

```bash
helm upgrade --install prism deploy/helm/prism -n prism \
  -f deploy/helm/prism/values.yaml \
  -f deploy/helm/prism/values-prod.yaml \
  --set gateway.ingress.host=prism-evamfinance.com \
  --set gateway.oidc.issuer=https://accounts.google.com \
  --set gateway.oidc.audience=<sso-client-id>.apps.googleusercontent.com \
  --set atlas.oidc.issuer=https://accounts.google.com \
  --set workflows.api.oidcIssuer=https://accounts.google.com \
  --set postgresql.auth.password="$(openssl rand -hex 24)" \
  --set postgresql.persistence.storageClass=<replicated-class> \
  --set minio.persistence.storageClass=<replicated-class> \
  --set vocx.google.existingSecret=prism-vocx-google \
  --set vocx.google.redirectUri=https://prism-evamfinance.com/vocx/v1/auth/callback \
  --set ui.image.repository=$REG/prism-ui --set ui.image.tag=$TAG \
  # …plus one --set per REPLACE-* credential in values-prod.yaml (the render FAILS
  # loudly if any placeholder is left — that guard is intentional).
```

Keep the full `--set` list in a values file **outside git** (or feed it from your
secret manager) so re-installs are reproducible.

### B5. DNS and verify

Point `prism-evamfinance.com` at the ingress controller's external IP
(`kubectl -n ingress-nginx get svc`). Then, exactly like compose:
**https://prism-evamfinance.com** → login → Today. The ingress routes `/ui` (and the
bare `/`) to the UI pods and everything else to the gateway.

### B6. Upgrades

```bash
# new images pushed with a new TAG, then:
helm upgrade prism deploy/helm/prism -n prism -f … --set image tags…
```

PVCs (postgres, minio, vocx) persist across upgrades. Database backups: run your
cluster's Postgres backup tooling or a CronJob with the same `pg_dumpall` command the
compose sidecar uses.
