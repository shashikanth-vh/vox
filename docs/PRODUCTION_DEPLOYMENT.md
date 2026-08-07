# PRISM — production deployment guide

How to run the platform for real users: one clean URL, real certificates, durable
volumes, backups, and the hardened security posture. Covers the **Docker Compose**
deployment (a single VM — the common case) and the **Helm** deployment (Kubernetes).

---

## 1. The URL: `https://prism-evamfinance.com` — nothing else to type

The edge now publishes the **standard web ports** (80 and 443), and the whole chain is
already wired: `http://…` → 301 to HTTPS → `/` → `/ui/` → login → the **Today** page.
So once DNS points at the VM, users type just the domain.

1. **DNS**: create an `A` record `prism-evamfinance.com → <VM IP>` (for an internal-only
   deployment, an entry in your internal DNS or the office router works the same).
2. Bring the stack up as usual — nothing extra to configure. Ports 80/443 are the
   defaults; if something else on the VM already holds them, override in
   `deploy/compose/.env`:

   ```bash
   EDGE_HTTP_PORT=80
   EDGE_HTTPS_PORT=443
   ```

   The old `:8080` / `:8443` mappings are still published, so existing bookmarks keep
   working during the transition.

## 2. Certificates

The edge terminates TLS from files at `deploy/nginx/certs/tls.crt` + `tls.key`
(bind-mounted read-only into the nginx container). Three ways to fill them, best first:

### a) Let's Encrypt (public DNS, free, auto-renewing)

Requires the domain to resolve publicly and port 80 reachable from the internet.

```bash
# One-time issue (stop the edge for a minute so certbot can bind :80, or use the
# webroot/DNS method your infra prefers):
sudo certbot certonly --standalone -d prism-evamfinance.com

# Install on the edge + zero-downtime reload:
scripts/install_edge_certs.sh \
  /etc/letsencrypt/live/prism-evamfinance.com/fullchain.pem \
  /etc/letsencrypt/live/prism-evamfinance.com/privkey.pem

# Make renewals self-applying:
sudo certbot renew --deploy-hook \
  "/path/to/prism/scripts/install_edge_certs.sh /etc/letsencrypt/live/prism-evamfinance.com/fullchain.pem /etc/letsencrypt/live/prism-evamfinance.com/privkey.pem"
```

### b) Corporate / purchased certificate

Get a pair for `prism-evamfinance.com` from your CA and install it the same way:

```bash
scripts/install_edge_certs.sh company-fullchain.pem company-privkey.pem
```

The script sanity-checks that the key matches the cert and reloads the running edge.

### c) Self-signed (internal networks with no CA)

```bash
EXTRA_SANS="DNS:prism-evamfinance.com,IP:192.168.44.128" scripts/gen_dev_certs.sh --force
```

Browsers will warn once per device (the cert is CA-capable, so you can also distribute
`tls.crt` to devices as a trusted root — see `docs/WSL_DEPLOY.md`). Prefer (a) or (b)
for anything users touch daily.

**After real certs are live**, enable HSTS in `deploy/nginx/nginx.conf` (the commented
`Strict-Transport-Security` line) so browsers pin HTTPS.

## 3. Secrets — the `.env` checklist

Everything secret lives in `deploy/compose/.env` (never in the repo, never in the zip —
your deploy ritual backs it up and restores it). New knobs join the existing ones:

| Variable | What it is | Default if unset |
|---|---|---|
| `PRISM_DB_PASSWORD` | The shared PostgreSQL password (used by postgres, register, access, temporal, backups) | `prism` — **override in production** |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | Object-store credentials (documents) | `prism` / `prism-secret` — **override** |
| `EDGE_HTTP_PORT` / `EDGE_HTTPS_PORT` | Host ports the edge publishes | `80` / `443` |
| `PGBACKUP_KEEP` | Days of nightly DB dumps to keep | `14` |
| `GOOGLE_SSO_CLIENT_ID`, `UI_DEX_URL`, service keys, signing secrets… | as before — see the comments in `docker-compose.prod-posture.yml` | |

Changing `PRISM_DB_PASSWORD` on an **existing** database: Postgres only reads
`POSTGRES_PASSWORD` at first init, so also run
`docker compose exec postgres psql -U prism -c "ALTER USER prism PASSWORD 'newpass'"`
once, then restart the stack.

## 4. Durability — what lives on which volume

All state is on named Docker volumes (they survive `down`, rebuilds, and VM reboots;
they are destroyed only by an explicit `docker compose down -v`):

| Volume | Holds | Loss means |
|---|---|---|
| `pgdata` | **Everything business**: register, access grants, temporal workflows | the book — protect and back up |
| `miniodata` | Document bytes (CAMs, financials, uploads) | the document store |
| `vocx_state` | VocX capture state | in-flight voice notes |
| `dexdata` | Dex signing keys + sessions (new) | everyone re-logs-in once |
| `pgbackups` | Nightly database dumps (new) | your safety net |

Every service now carries `restart: unless-stopped` (postgres included), so the whole
stack self-heals after a VM reboot: `docker compose … up -d` once, then it stays up.

## 5. Backups & restore

Enable the nightly database backup sidecar by adding `--profile backup` to your usual
command:

```bash
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod-posture.yml \
  --profile sso --profile backup up -d --build
```

It `pg_dumpall`s every database (register, access, temporal) once a day into the
`pgbackups` volume, gzipped, keeping `PGBACKUP_KEEP` days. Also snapshot the volume
off-VM (any file backup of `/var/lib/docker/volumes/…_pgbackups` works, or
`docker run --rm -v compose_pgbackups:/b -v /backup/target:/out alpine cp -a /b /out`).

**Restore** (to a fresh or broken stack):

```bash
# list what you have
docker compose exec pgbackup ls -lh /backups
# restore everything (stop app services first so nothing writes mid-restore).
# The pgbackup container holds both the dumps and the connection env — run it there:
docker compose stop register access gateway workflows-worker temporal
docker compose exec -T pgbackup sh -c \
  'gunzip -c /backups/prism-<timestamp>.sql.gz | psql -d postgres'
docker compose start register access gateway workflows-worker temporal
```

Documents: `miniodata` is plain files — back the volume up the same way. The in-app
**Admin → Tools → Export ledger / Backup** flows add a business-level export on top.

## 5b. Container logs — bounded, so a chatty service can't fill the disk

Docker's default logging grows **without limit** — a verbose or error-looping container
slowly fills `/var/lib/docker` until the disk is full, and a full disk stops Postgres,
the stack, and Docker itself. The compose file now caps every service at
**10 MB × 5 rotated files (50 MB per container, ~1 GB worst-case for the whole stack)**;
`docker logs <svc>` works exactly as before within that window. The cap applies when a
container is (re)created — the next `up -d --build` after this change does it.

Belt-and-braces (optional): make the same limit the *host-wide* Docker default, so even
containers started outside compose are covered — `/etc/docker/daemon.json`:

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "5" } }
```

then `sudo systemctl restart docker` (one-time; do it in a maintenance moment — it
restarts every container).

Watch disk health occasionally: `df -h /var/lib/docker` and `docker system df`.
Reclaim space from old images after upgrades with `docker image prune -af` (safe — it
removes only unused images; volumes are never touched).

## 6. Sign-in hardening (recap)

Production runs the prod-posture overlay: OIDC everywhere, RBAC + RLS enforced,
online revalidation for sensitive operations. Dex ships for dev/demo sign-in — for
real production, point the issuers at your IdP (Google Workspace / Entra / Okta) as
documented in `docs/GOOGLE_SSO.md`, and remove the `--profile sso` Dex service. Dex now
stores its signing keys on `dexdata` (sqlite), so restarts no longer log everyone out.

## 7. Kubernetes (Helm)

The umbrella chart at `deploy/helm/prism` deploys the full platform. Production:

```bash
# Build + push the images your registry (CI does this; the UI image is:)
docker build -f deploy/ui-image/Dockerfile . -t <registry>/prism-ui:<tag>

helm upgrade --install prism deploy/helm/prism \
  -f deploy/helm/prism/values.yaml \
  -f deploy/helm/prism/values-prod.yaml \
  --set gateway.oidc.issuer=https://accounts.google.com \
  --set gateway.oidc.audience=<client-id>.apps.googleusercontent.com \
  --set atlas.oidc.issuer=https://accounts.google.com \
  --set workflows.api.oidcIssuer=https://accounts.google.com \
  # …plus every REPLACE-* credential from your secret manager (the render FAILS
  # if any placeholder is left — that guard is on in values-prod).
```

What the prod overlay now gives you out of the box:

- **Ingress** on `prism-evamfinance.com` with TLS via **cert-manager**
  (`cert-manager.io/cluster-issuer: letsencrypt-prod` — or delete the annotation and
  pre-create the `prism-edge-tls` Secret with corporate certs).
- **The ATLAS UI** as its own subchart (`ui`, 2 replicas, static nginx): the ingress
  routes `/ui` to it and sends the bare `/` to its redirect — `https://prism-evamfinance.com`
  opens the app, exactly like compose.
- **Persistence**: PostgreSQL (10Gi PVC) and MinIO (PVC) — set
  `postgresql.persistence.storageClass` / `minio.persistence.storageClass` to your
  cluster's replicated storage class for real durability, and size them
  (`--set postgresql.persistence.size=50Gi`).
- Database backups in K8s: use your cluster's PostgreSQL backup operator (or a simple
  CronJob running `pg_dumpall` against `prism-postgresql` — the compose sidecar's
  command works verbatim in a CronJob).

## 8. Upgrade procedure (compose, the day-to-day ritual)

```bash
cp deploy/compose/.env /root/prism-env.backup     # 1. keep the secrets
rm -rf prism && unzip prism-<hash>.zip            # 2. fresh tree (never unzip-over)
cp /root/prism-env.backup prism/deploy/compose/.env
cd prism
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod-posture.yml \
  --profile sso --profile backup up -d --build    # 3. rebuild what changed
```

Volumes are untouched by all of this — data survives every upgrade.
