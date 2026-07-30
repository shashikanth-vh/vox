# Running PRISM on WSL (Docker Compose)

The whole platform runs from one compose file: PostgreSQL + MinIO + Temporal + Register + Access +
Gateway + the workflow worker + Orchestrator + NGINX edge (and VocX/PULSE/ATLAS).

> I run in an isolated sandbox and **cannot reach your WSL machine**, so I can't execute this on your
> box. I validated the compose config here (`docker compose config` passes) and shipped a deployed
> smoke test (`scripts/e2e_smoke.sh`) plus a CI job (`.github/workflows/e2e.yml`) that brings the
> stack up and runs it. Below is the exact runbook to do the same on WSL.

## 0. Prerequisites (Ubuntu on WSL2)

- **Docker Desktop** with the WSL integration enabled for your distro (Settings → Resources → WSL
  Integration), OR Docker Engine installed inside WSL. Verify:
  ```bash
  docker version        # server must respond
  docker compose version
  ```
- Clone the repo inside the WSL filesystem (e.g. `~/vox`), not `/mnt/c/...` (much faster I/O).

## 1. Generate the edge TLS certificate (once)

The edge terminates **HTTPS** with a self-signed certificate. nginx will not start without it:

```bash
cd ~/vox
scripts/gen_dev_certs.sh          # writes deploy/nginx/certs/{tls.crt,tls.key}
```

The cert covers `localhost`, `127.0.0.1`, `nginx` and `prism.local`, and is valid ~2 years.
To trust it instead of bypassing verification:

```bash
sudo cp deploy/nginx/certs/tls.crt /usr/local/share/ca-certificates/prism-dev.crt
sudo update-ca-certificates       # then curl works without -k
```

## 2. Bring the stack up

```bash
cd ~/vox/deploy/compose
docker compose -f docker-compose.yml up --build            # Ctrl-C to stop; add -d to detach
# Just the core (no workflow plane):
#   docker compose -f docker-compose.yml up --build postgres minio register access gateway nginx
# With the Temporal Web UI:
#   docker compose -f docker-compose.yml --profile debug up --build
```

The Register **migrates, provisions the tenant + reference dropdowns, then serves an empty DB**.

Ports: **edge (NGINX) `:8443` HTTPS** — the front door — with `:8080` serving only a 301 to it ·
gateway `:8001` · register `:8000` · access `:8002` · orchestrator `:8006` · MinIO
`:9000`/console `:9001` · Temporal UI (debug) `:8088`. Everything except the edge is a
plaintext dev convenience that **bypasses the gateway's RBAC gate** — treat `:8443` as the
only supported entry point.

Verify TLS:

```bash
curl -k https://localhost:8443/healthz          # -k: self-signed
curl --cacert deploy/nginx/certs/tls.crt https://localhost:8443/healthz   # or verify properly
```

## 3. Smoke-test the deployment

```bash
cd ~/vox
REGISTER_URL=http://localhost:8000 GATEWAY_URL=http://localhost:8001 \
ACCESS_URL=http://localhost:8002 scripts/e2e_smoke.sh
```
This checks health, does real Register CRUD, exercises the **CP/CS maker-checker** end-to-end
against real PostgreSQL (a maker prepares, a different checker approves — self-approval is refused),
and confirms the **handover endpoint is deployed and gated**. Expected final line:
`E2E SMOKE PASSED …`.

## 4. Load data (optional)

```bash
# your own MIS spreadsheet:
curl -H "X-API-Key: dev-local-key" -F "file=@/path/to/mis.xlsx" \
     "http://localhost:8000/v1/import/atlas-xlsx?mode=replace&reason=initial"
# or the shipped synthetic mock (smoke only):
docker compose -f deploy/compose/docker-compose.yml exec register python -m app.seed
```

## 5. Import the Postman collection

Import the collections and the shared environment, then **select `PRISM — Local` in the environment
dropdown** — without it, no `{{variable}}` resolves and nothing sends:
- `postman/PRISM_UI_CRUD.postman_collection.json` — **start here for UI work**: table CRUD only
  (158 requests, one folder per table), every request through the NGINX edge.
- `postman/Register.postman_collection.json` — every Register endpoint (186, incl. handover + CP/CS).
- `postman/Orchestrator.postman_collection.json` — the workflow plane (CP/CS + Advaya handover
  prepare/approve).
- `postman/PRISM.postman_environment.json` — `baseUrl` = `https://localhost:8443` (the edge → gateway
  → Register) and `orchestratorUrl` = `https://localhost:8443/orchestrator`. Because the cert is
  self-signed, turn **off** SSL verification in Postman (Settings → General) or trust
  `deploy/nginx/certs/tls.crt`. Both are the same door: the edge
  forwards everything to the gateway, and the gateway routes `/orchestrator` (and `/atlas`, `/vocx`,
  `/pulse`) to those services while everything else goes to the Register. The gateway **injects**
  each upstream's api key, so Postman does not send one.

Full guide, including the two-person maker-checker sequence: **`docs/POSTMAN.md`**.

## Troubleshooting
- nginx exits with `cannot load certificate "/etc/nginx/certs/tls.crt"`: run
  `scripts/gen_dev_certs.sh`, then `docker compose up -d nginx`.
- `curl: (60) SSL certificate problem: self-signed certificate`: expected — use `-k`, or
  `--cacert deploy/nginx/certs/tls.crt`, or trust the cert (§1).
- Browser warning "Your connection is not private": expected with a self-signed cert; trust it
  per §1 or click through.
- `network ... not found` from a stale run: `docker compose -f docker-compose.yml down --remove-orphans`.
- Register unhealthy: `docker compose logs register` (it waits for a healthy Postgres first).
- Reset everything (drops volumes/data): `docker compose -f docker-compose.yml down -v`.
- Slow builds / file-watch issues: make sure the repo is on the **Linux** filesystem, not `/mnt/c`.
