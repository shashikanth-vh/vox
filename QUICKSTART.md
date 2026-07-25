# PRISM Register — Quickstart

Four things: **run the tests**, **build the image**, **run with Docker Compose**, and
**deploy with Helm**. All commands are run from the repo root unless noted.

Prerequisites differ per section:
- Compose: Docker only.
- Tests: Python 3.11+ and a PostgreSQL (a throwaway Docker one is fine).
- Helm: a local Kubernetes (kind / minikube / k3d / Docker Desktop) + `kubectl` + `helm`.

The demo API key is `dev-local-key`. The API listens on port `8000`; docs at `/docs`.

---

## 1) Run with Docker Compose  (easiest — start here)

Compose **builds the Register image for you**, starts PostgreSQL, runs migrations,
provisions the default tenant + reference dropdowns (**no business data**), and serves.
The API is usable immediately; the database has no leads/deals/entities.

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
```

In another terminal, verify:

```bash
curl http://localhost:8000/healthz
curl -H "X-API-Key: dev-local-key" "http://localhost:8000/v1/entities?with_total=true"
# open the interactive docs:
#   http://localhost:8000/docs
```

Load data on demand, only if you want it. Real data is never shipped in the image — you
supply it at runtime:

```bash
# Recommended: upload your own spreadsheet through the API:
curl -H "X-API-Key: dev-local-key" -F "file=@<your-mis>.xlsx" \
  "http://localhost:8000/v1/import/atlas-xlsx?mode=replace"

# Or the synthetic prototype mock (shipped, for smoke tests only):
docker compose -f deploy/compose/docker-compose.yml exec register python -m app.seed
```

Stop it (keep data):

```bash
docker compose -f deploy/compose/docker-compose.yml down
```

Stop and wipe the database volume:

```bash
docker compose -f deploy/compose/docker-compose.yml down -v
```

---

## 2) Build the Docker image (standalone)

The Dockerfile lives in `register/`, but the **build context is the repo root** (so it can
install the shared `packages/evam-backend-core` package alongside the Register):

```bash
# from the repo root:
docker build -f services/register/Dockerfile -t prism-register:0.1.0 .
```

Run it against any PostgreSQL (here, one you start yourself). The entrypoint subcommands
are `serve` (default), `migrate`, `bootstrap` (tenant + reference dropdowns, no business
data), `seed` (adds the ATLAS mock), `import-mis`, and the combos `migrate-serve`,
`migrate-bootstrap-serve`, `migrate-seed-serve`, `migrate-import-serve`:

```bash
docker run --rm -p 8000:8000 \
  -e REGISTER_DB_HOST=<host> -e REGISTER_DB_PORT=5432 \
  -e REGISTER_DB_USER=prism -e REGISTER_DB_PASSWORD=prism -e REGISTER_DB_NAME=register \
  -e REGISTER_API_KEYS=dev-local-key \
  prism-register:0.1.0 migrate-bootstrap-serve   # fresh + usable; -seed- for the mock
```

---

## 3) Run the tests

The suite runs against a real PostgreSQL and a database named `register_test` (it applies
the real Alembic migration, then runs CRUD + concurrency tests).

Spin up a throwaway Postgres and create the test DB:

```bash
docker run -d --name prism-testdb -p 5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres postgres:16-alpine
sleep 3
docker exec -e PGPASSWORD=postgres prism-testdb \
  psql -U postgres -c "CREATE USER register PASSWORD 'register';"
docker exec -e PGPASSWORD=postgres prism-testdb \
  psql -U postgres -c "CREATE DATABASE register_test OWNER register;"
```

Install deps and run:

```bash
# install the shared platform package first, then the Register:
pip install -e packages/evam-backend-core
cd services/register
pip install -e ".[dev]"

# Point the tests at the Postgres above (defaults assume port 5433, so set these):
export REGISTER_DB_HOST=127.0.0.1 REGISTER_DB_PORT=5432 \
       REGISTER_DB_USER=register REGISTER_DB_PASSWORD=register REGISTER_DB_NAME=register_test
pytest              # or: make test
```

You should see ~14 tests pass (including the concurrency tests: no lost updates, idempotent
creates, deadlock-free parallel inserts, serialised financial versions).

Clean up:

```bash
docker rm -f prism-testdb
```

---

## 4) Deploy with Helm

The single **`prism`** umbrella chart contains the modules as subcharts
(postgresql · register · access · gateway · vocx · pulse · atlas · minio ·
temporal · workflows). Subcharts are vendored, so **no
`helm dependency build`** is needed.

You need a local Kubernetes cluster and the Register image available to it.

### 4a. Build the image and load it into your cluster

```bash
# from repo root — build both service images:
docker build -f services/register/Dockerfile  -t prism-register:0.1.0  .
docker build -f services/workflows/Dockerfile -t prism-workflows:0.1.0 .

# load into whichever local cluster you use (kind shown):
kind load docker-image prism-register:0.1.0
kind load docker-image prism-workflows:0.1.0
# minikube image load … / k3d image import … / Docker Desktop shares the daemon
```

The local install brings up the **whole platform**: shared PostgreSQL, the Register,
Temporal (+ Web UI), and the workflows worker. Reach the Temporal UI with
`kubectl -n prism port-forward svc/prism-temporal-ui 8088:8080`. Disable a piece with
e.g. `--set temporal.enabled=false --set workflows.enabled=false`.

### 4b. Install the whole platform (shared DB + Register, seeded)

```bash
helm upgrade --install prism deploy/helm/prism \
  -f deploy/helm/prism/values-local.yaml \
  --namespace prism --create-namespace \
  --set register.image.repository=prism-register \
  --set register.image.tag=0.1.0 \
  --set register.image.pullPolicy=IfNotPresent
```

Watch it come up, then reach the API:

```bash
kubectl -n prism get pods
# prism-postgresql-0            Running
# prism-register-...            Running
# prism-register-migrate-...    Completed   (migrations + seed)

kubectl -n prism port-forward svc/prism-register 8000:80
curl -H "X-API-Key: dev-local-key" "http://localhost:8000/v1/entities?with_total=true"
```

Uninstall:

```bash
helm uninstall prism -n prism
```

### Variants

- **Managed database (production):** disable the bundled DB and point the Register at your
  managed India-resident Postgres:
  ```bash
  helm upgrade --install prism deploy/helm/prism -n prism --create-namespace \
    --set postgresql.enabled=false \
    --set register.database.host=<rds-host> \
    --set register.database.user=<user> \
    --set register.database.existingSecret=<secret> \
    --set register.migrations.asHook=true \
    --set register.image.repository=<registry>/prism-register --set register.image.tag=0.1.0
  ```
- **Just the Register** (DB already exists): `helm upgrade --install register
  deploy/helm/prism/charts/register -n prism --set database.host=... ...`

More detail: [`deploy/helm/prism/README.md`](deploy/helm/prism/README.md).

---

## Exercise CRUD on every table (Postman)

1. Import `postman/Register.postman_collection.json` and
   `postman/Register.postman_environment.json` into Postman.
2. Set the environment `baseUrl=http://localhost:8000`, `apiKey=dev-local-key`.
3. Each table has a folder with Create / List / Get / Update / Delete requests. Create one
   record, copy its `id` into the `id` (or `entityId`) environment variable, and run the
   rest.
