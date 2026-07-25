# PRISM Access service

User management & access-control facts: users (Employees governance), stacked roles, and
the **access matrix as admin-editable data** (seeded from the ATLAS RBAC v3.1 spec in
`evam_backend_core.rbac`; guardrail cells immutable). The gateway calls `/v1/resolve`
to fill its cache — never per request. Own `access` database on the shared Postgres.

Run tests: create an `access_test` database, `pip install -e ../../packages/evam-backend-core -e ".[dev]"`, `pytest`.
