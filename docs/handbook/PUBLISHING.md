# Publishing the handbook to Confluence

> **Audience:** whoever keeps the Confluence mirror current.
> **Rule:** `docs/handbook/` in git is the source of truth. Confluence is a **read-only
> mirror**. Never edit the Confluence copy — it will drift from the code the documents
> quote, and a handbook that has drifted is worse than none, because people trust it.

---

## Why a PDF and not pages

Confluence renders neither Markdown files nor Mermaid. A mirror there has to be something a
browser can already draw, so the handbook is published as **one PDF** built from the
Markdown with the diagrams rendered.

| | One PDF attachment | 16 Confluence pages |
| --- | --- | --- |
| Effort to publish | Drag one file | Paste 16 documents |
| Effort to **re-publish** after a docs change | Re-attach one file | Re-paste whatever changed |
| Diagrams | **Rendered, vector, searchable text** | Need a Marketplace app, or manual images |
| Confluence search | Yes — attachment text is indexed | Yes |
| Drift risk | **None** — nobody can edit it | High — anyone can edit a page |

The last row is the real argument. A PDF cannot be quietly edited out of agreement with the
repository.

---

## Build it

```bash
# once — the build deps are NOT vendored into the repo
mkdir -p ~/prism-docs-build && cd ~/prism-docs-build
npm init -y && npm i marked mermaid playwright-core

# every time
cd /path/to/vox
BUILD_DEPS=~/prism-docs-build node scripts/build_handbook_pdf.mjs
# → PRISM-Handbook.pdf   (gitignored — it is an artefact, not source)
```

Chromium: the script uses the one already on the box under `/opt/pw-browsers`. If yours is
elsewhere, edit the `CHROME` list at the top, or install one with
`npx playwright install chromium`.

### It is also the diagram validator

Every diagram is rendered by real Mermaid in real Chromium. A diagram that will not parse
**fails the build by name** and the script exits non-zero:

```
3 diagram(s) FAILED to render:
  03-MODULE-INTERACTION.md mmd-9: Parse error on line 9 …
```

Run it before pushing docs changes. It catches what reading the source does not — a
semicolon inside sequence-diagram text is a statement separator, for instance, and silently
breaks the diagram on GitHub too.

### Checking the styling

```bash
BUILD_DEPS=~/prism-docs-build \
HANDBOOK_PREVIEW=/tmp/pv HANDBOOK_PREVIEW_IDS=doc-01-architecture,mmd-12 \
  node scripts/build_handbook_pdf.mjs
```

Writes `/tmp/pv-*.png` straight from the rendered page — quicker than opening a PDF viewer.
IDs are `doc-<filename>` for a document section, or `mmd-<n>` for one diagram (the build log
prints how many diagrams each file contributes, in order).

---

## Publish it

1. Open the **PRISM** space → **Prism Home**.
2. **Edit** → type `/attachment` (or drag the file onto the page).
3. Attach `PRISM-Handbook.pdf`. It previews inline — a reader clicks it and reads it in the
   browser, no download.
4. Above it, paste the contents list (§below) so people can see what is inside before
   opening it.
5. **Publish.**

To update: **Edit → attach the new file with the same name.** Confluence versions the
attachment, so the page keeps working and the old version stays available.

### A contents block for the page

```
The PRISM Handbook — architecture, deployment, operation and use.
Source of truth: docs/handbook/ in the vox repository. This PDF is a generated mirror;
raise doc changes as a pull request there, not here.

01 Architecture             what PRISM is, service catalogue, request path, trust boundaries
02 Deployment architecture  compose and Helm topologies, the edge, ports, volumes, sizing
03 Module interaction       who calls whom, identity propagation, timeouts, failure posture
04 Running flows            capture, conversion, stage change, import, intel, handover
05 Temporal workflows       the fourteen workflows, decisions, retries, SLA
06 Code map                 directory by directory + "where do I change X?"
07 User management & RBAC   authority model, twelve roles, two matrices, provisioning
08 The Register             CRUD factory, concurrency, lifecycle, reconciliation
09 Backup & restore         what survives, the restore drill, RPO/RTO
10 Upgrade & rollback       prism-deploy.sh, schema changes, the failure playbook
11 ATLAS usage              the ten tabs, what each role sees, common tasks
12 VocX & STT               capture pipeline, timeouts, recording cap, sizing
13 Operations               health checks, logs, symptom to fix
14 Configuration            every variable, compose vs Helm, production checklist
15 Data model & ERD         every table, the row spine, RLS, migrations
```

---

## If you would rather have real Confluence pages

Two things change, and both are ongoing costs rather than one-off ones:

- **Mermaid needs an app.** Install *Mermaid Diagrams for Confluence* from the Marketplace
  and emit `mermaid-cloud` macros, or pre-render each diagram to PNG and attach it.
- **Publishing needs a script**, or you re-paste by hand every time. The API shape is
  `GET /wiki/api/v2/spaces?keys=PRISM` for the space id, then `POST /wiki/api/v2/pages` to
  create and `PUT` with an incremented `version.number` to update in place, with
  `body.representation = "storage"`. Auth is Basic, `email:api_token`. Markdown has to be
  converted to Confluence storage format — fenced code becomes
  `<ac:structured-macro ac:name="code">`.

Worth doing if the handbook becomes something non-engineers edit. While git is the source of
truth, it is a lot of machinery to reproduce a PDF.
