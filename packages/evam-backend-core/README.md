# evam-backend-core

Shared production-grade backend platform for PRISM services. Provides structured logging,
an RFC-9457 error contract, request-id/tenant/actor correlation, a bounded async DB pool
with hard timeouts and transparent transient-retry, optimistic-locking CRUD, keyset
pagination, health probes, and a one-call app factory.

See `BACKEND_STANDARDS.md` at the repo root for conventions and how to build a new service.
