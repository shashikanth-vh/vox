# Architecture diagrams

Self-contained HTML pages (open directly in any browser — diagrams are Mermaid + CSS,
no external dependencies beyond the Mermaid renderer in the hosted viewer; in a plain
browser the sequence/flow blocks show as readable text).

- **`prism-architecture.html`** — the whole platform as built: 9-service block diagram
  (NGINX → Gateway → Register/Access, one PostgreSQL with a database per service, MinIO,
  Temporal), Compose + Helm deployment topology, and functionality flows F1–F14.
- **`prism-rbac-design.html`** — the three-service RBAC enforcement: Gateway (cached
  binary gate) + Access (users/roles + admin-editable matrix with guardrails) + Register
  (scoped enforcement next to the data); decision ladder and call flows CF1–CF8, each
  covered by an end-to-end test.

Both are also published as live artifacts; these copies are the repo-versioned snapshot
matching the commit they ship with.
