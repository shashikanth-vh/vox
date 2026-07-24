# Contributing to PRISM

Welcome. PRISM is Evam Finance's tech-first operating system for climate finance. This
guide gets a new engineer productive fast and keeps the codebase consistent as the team
grows. Read [`README.md`](README.md) for the big picture and
[`BACKEND_STANDARDS.md`](BACKEND_STANDARDS.md) for the engineering contract.

## Repository layout

```
services/          Deployable services (each is its own process/container)
  register/          The source of truth (first + reference service)
packages/          Shared libraries every service uses
  evam-backend-core/     Build a service on it (logging, errors, DB, CRUD, retry, app factory)
  evam-register-client/  Call the Register from any service (typed client)
deploy/            Docker Compose + Helm
docs/              Schema, OpenAPI, and Architecture Decision Records (adr/)
scripts/           Repo tooling (e.g. new_service.py)
```

Rule: **services live in `services/`, shared code in `packages/`.** Never copy shared
concerns into a service — extend the package instead.

## One-time setup (zero → productive)

Prereqs: Python 3.12, Docker (for a throwaway Postgres), `make`.

```bash
# 1. install everything (shared packages first, then services), editable + dev deps
make install

# 2. optional but recommended — auto-lint/format on commit
pip install pre-commit && pre-commit install

# 3. a Postgres for the tests (throwaway)
docker run -d --name prism-db -p 5432:5432 \
  -e POSTGRES_USER=register -e POSTGRES_PASSWORD=register -e POSTGRES_DB=register_test \
  postgres:16
export REGISTER_DB_HOST=127.0.0.1 REGISTER_DB_PORT=5432 \
       REGISTER_DB_USER=register REGISTER_DB_PASSWORD=register REGISTER_DB_NAME=register_test

# 4. run the full gate
make ci        # lint + type-check + tests
```

To run the Register locally: see [`QUICKSTART.md`](QUICKSTART.md) (Docker Compose is the
one-command path).

## The quality gate (what CI enforces on every PR)

- **`ruff`** — lint + format. `make lint` / `make fmt`.
- **`mypy`** — type-check. `make type`. The gate is green; keep it that way.
- **`pytest`** — Register suite (runs the real Alembic migration against Postgres) + the
  client suite. `make test`.

Nothing merges red. If a check is failing, fix it before asking for review.

## How to… (common tasks)

### Add a field to a table
1. Model in `services/register/app/models/*.py`.
2. Same column in `migrations/versions/0001_initial_schema.py` (pre-release we edit the
   single baseline; after release, add a new revision).
3. Expose it in the Pydantic `*Create` / `*Update` / `*Read` schema in `app/schemas/resources.py`.
4. Add/extend a test. `make test`.

### Add an endpoint
Use `api_router(...)` (never bare `APIRouter`) so retry applies. Raise the typed errors
from `evam_backend_core.errors`; don't hand-format error responses.

### Add a reference dropdown
Add the category + values to `app/seed/refdata.py`; it's served at `/v1/ref`.

### Add a new service (CIPHER / PULSE / VOX / gateway)
```bash
make new-service NAME=cipher
```
This scaffolds `services/cipher/` on `evam-backend-core`. To talk to the Register from it,
use `evam-register-client` — never hand-roll HTTP. See `BACKEND_STANDARDS.md`.

## Coding conventions (the short list)

- **Naming**: tables `snake_case`; services `prism-<name>`; packages `evam-<name>`;
  env vars `<SERVICE>_...`; API paths `/v1/<resource>`; DB constraints follow the
  deterministic naming convention in `evam_backend_core.db.base`.
- **Logging**: `log = get_logger(__name__)`; log events with `extra={...}`, not sentences.
- **Transactions**: one unit of work per request; writes use optimistic locking (`version`),
  never long-held row locks.
- **Types**: annotate public functions; keep `mypy` green.
- **Docstrings**: every module gets a short "why", not just "what".

## Commits & PRs

- Small, focused commits; imperative subject ("Add covenant fields", not "added").
- A PR should pass `make ci` locally before review.
- Record significant architectural decisions as an ADR in [`docs/adr/`](docs/adr/) — copy
  `docs/adr/0000-template.md`.

## Architecture Decision Records

The *why* behind the big choices (Register-first, entity-centric schema, optimistic
concurrency, monorepo + shared core, …) lives in [`docs/adr/`](docs/adr/). Read them before
proposing a change that revisits one — and add a new ADR when you make a decision that a
future engineer would otherwise have to reverse-engineer.
