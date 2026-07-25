# PRISM Helm chart

One umbrella chart — **`prism`** — that contains the platform's pieces as subcharts:

```
deploy/helm/prism/
  Chart.yaml            umbrella (lists subcharts + enable/disable conditions)
  values.yaml           platform defaults
  values-local.yaml     one-command local stack (bundled DB + seeded Register)
  charts/
    postgresql/         SHARED PRISM database — service `prism-postgresql`
    register/           the Register module — connects via database.* and storage.*
    minio/              S3-compatible object store — service `prism-minio:9000` (doc bytes)
    temporal/           Temporal engine (Workflows ring) — service `prism-temporal:7233`
    workflows/          the PRISM worker — activities write the Register via the SDK
    (cipher/ pulse/ vox/ atlas/  ← added here as they are built)
```

`helm upgrade --install prism …` brings up the **whole platform**: shared PostgreSQL, the
Register, Temporal (+ Web UI), and the workflows worker. Disable any piece with
`--set <name>.enabled=false`. Temporal here is a dev/staging single-Deployment
(`auto-setup`) on the shared DB in its own databases; for production point
`temporal.datastore.*` at a dedicated instance (or swap in the official Temporal chart).
The **edge** in-cluster is the Register's `ingress` (enable + set your ingress class /
annotations) — the NGINX role is played by your ingress controller.

The one architectural decision this encodes: **PostgreSQL is a shared platform service,
not owned by any module.** The `postgresql` subchart runs one database that every module
connects to; each module gets its own database on that server
(`postgresql.extraDatabases`). The same holds for **object storage**: `minio` is a shared
S3-compatible store for document/attachment bytes; the Register keeps only references.
For production, disable it (`--set minio.enabled=false`) and point
`register.storage.s3.*` at a managed S3 (or set `register.storage.backend=inline`).

Subcharts are vendored under `charts/`, so there is **no `helm dependency build`** step
and no external registry — air-gap / localise-everything friendly.

## Deploy options

### A) Whole platform, local (recommended for dev / a single environment)

```bash
helm upgrade --install prism deploy/helm/prism \
  -f deploy/helm/prism/values-local.yaml \
  --namespace prism --create-namespace
```

Brings up `prism-postgresql` + the Register (migrated and seeded with the ATLAS mock).

### B) Whole platform, managed database (production)

Disable the bundled DB and point modules at your managed India-resident Postgres
(RDS Mumbai / Azure DB for PostgreSQL):

```bash
helm upgrade --install prism deploy/helm/prism --namespace prism --create-namespace \
  --set postgresql.enabled=false \
  --set register.database.host=<rds-host> \
  --set register.database.user=<user> \
  --set register.database.existingSecret=<secret> \
  --set register.migrations.asHook=true
```

### C) A single module on its own

Each subchart is a valid chart, so you can deploy just one — e.g. the Register against an
existing shared/managed DB:

```bash
helm upgrade --install register deploy/helm/prism/charts/register \
  -f deploy/helm/prism/charts/register/values-local.yaml --namespace prism
```

## Adding a module later

1. Drop its chart under `charts/<module>/`.
2. Add it to `dependencies:` in `Chart.yaml` with `condition: <module>.enabled`.
3. Give it a database on the shared server via `postgresql.extraDatabases: [<module>]`
   and point it at `prism-postgresql`.

## Why a shared DB, not one-per-module

The Register is the single source of truth; every module reads from and writes to it.
One Postgres server (with a database per module) keeps that data coherent, puts
connection/backup/HA/audit in one place, and matches PRISM's localise-everything posture.
In-cluster Postgres (the `postgresql` subchart) suits local/self-hosted; production should
use a managed service — the module charts don't care which, they just take `database.*`.
