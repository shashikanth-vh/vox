# Frozen OpenAPI contracts

These are the **frozen HTTP contracts** the ATLAS / Node.js team builds against. They are generated
from the live FastAPI apps (the code is the source of truth) and committed so the frontend can
codegen clients and review diffs in PRs.

| File | Service | Base URL (via gateway) | Notes |
|------|---------|------------------------|-------|
| `register.openapi.json` | Register (system of record) | `/` (or `/register/…` fronted) | full CRUD + governance (evidence, decisions, CP/CS checklists, handover packages) |
| `orchestrator.openapi.json` | Workflows orchestrator | `/orchestrator/…` | starts/decides workflows (qualification, structuring, document collection, **Advaya handover prepare + approve**) |
| `gateway.openapi.json` | Gateway | `/` | a transparent authz proxy (single catch-all route); the real shapes are the two specs above |

## Key handover / CP/CS operations (this milestone)

Orchestrator (business-facing, via gateway):
- `POST /v1/workflows/advaya-handover` — **maker** prepares the handover package (senior credit authority).
- `POST /v1/workflows/advaya-handover/{lending_id}/approve` — **checker** (different person) approves; advances the stage.

Register (internal / delegated):
- `POST /v1/internal/handover-packages` — prepare (Prepared); server-generates + digests the package.
- `POST /v1/internal/handover-packages/{lending_id}/approve` — checker approval; freezes package + advances stage.
- `GET  /v1/lending/{id}/handover-package`, `POST /v1/lending/{id}/handover-package/download`.
- `POST /v1/internal/cpcs-checklists` (+ `/{id}/approve`) — authoritative CP/CS checklist (CP/CS, waiver, CS-deferment).

## Postman collections

The Postman collections are generated **from these frozen specs** (not by importing the services), so
they always match what the frontend codegens against:

| File | Covers |
|------|--------|
| `postman/Register.postman_collection.json` | every Register endpoint (186) — CRUD + evidence, decisions, CP/CS checklists, handover packages |
| `postman/Orchestrator.postman_collection.json` | every workflow-plane endpoint (14) — start/decide workflows incl. **CP/CS checklist** and **Advaya handover prepare + approve** |
| `postman/PRISM.postman_environment.json` | shared vars (`baseUrl` → Register, `orchestratorUrl` → gateway `/orchestrator`, api key, tenant, ids) |

Import both collections + the environment. Register requests carry `X-API-Key/X-Tenant/X-Actor`;
orchestrator requests go through the gateway (`orchestratorUrl`) with `X-User-Email/X-User-Roles` so the
gateway resolves the caller identity for maker/checker separation.

The access / gateway / VocX / PULSE / ATLAS services are service-internal (no business-facing REST plane
of their own) and are intentionally not in the collections; the gateway is a transparent proxy for the
two specs above.

## Regenerate

Run after any API change (kept in sync in the same PR) — this refreshes the specs **and** the Postman
collections:

```bash
scripts/export_openapi.sh
```
