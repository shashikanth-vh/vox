# Chitti service-chain acceptance

This suite runs real Dex, Gateway, Access, Chitti routing/delegation, Register,
and PostgreSQL in the `prism-chitti-acceptance` Compose project. Its database,
identities, and business records are synthetic. It publishes only the fixture edge
at `http://127.0.0.1:18439` and uses separate image tags and volumes.

`probe.py` supplies deterministic query selection and wording. Register reads,
plan execution, aggregate calculation, and qualitative corpus construction use
the application code. The suite verifies authorization and failure handling;
model planning and answer quality are separate checks. No model-provider key or
existing application volume is used.

From the repository root:

```bash
docker compose -p prism-chitti-acceptance \
  -f services/chitti/tests/acceptance/compose.yml up -d --build --wait
docker compose -p prism-chitti-acceptance \
  -f services/chitti/tests/acceptance/compose.yml exec -T postgres \
  psql -U prism -d register < services/chitti/tests/acceptance/seed.sql
.venv/bin/python services/chitti/tests/acceptance/check.py
```

The checks cover the actual runtime database login, fail-closed tenant RLS,
whole-book versus explicitly scoped lending access, counts, qualitative evidence,
denied users, cross-tenant membership, forged identity, service-only denial,
stream timeouts, repeated disconnects, dependency outages/recovery, and deactivation.
The fixture uses one-second cache freshness and a two-second stale bound to
exercise the same policy without waiting for the deployment's longer defaults.
It restores stopped services and the deactivated fixture user after checks.

The default Deal Analyst visibility policy grants whole-book READ. `seed.sql`
sets an explicit SCOPED matrix override inside this fixture. It uses separate
companies so the platform's connected-company rule does not widen that scope.

Stop the fixture while retaining its synthetic data:

```bash
docker compose -p prism-chitti-acceptance \
  -f services/chitti/tests/acceptance/compose.yml down
```

Helm render checks are in `test_helm.py`; production Compose checks are in
`test_compose.py`. The [Helm guide](../../../../deploy/helm/prism/README.md#chitti)
describes installation and rollback with real artifact jobs. For an isolated
single-node Kubernetes run, ReadWriteOnce model storage is sufficient; multi-node
deployments use shared storage or pin preparation and serving to the same node.
