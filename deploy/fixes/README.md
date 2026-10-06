# One-off register fixes

Scripts for data problems the October bug register found. Each one PRINTS what
it would change and writes nothing unless run with `apply`; deletes are the
register's own soft delete (`deleted_at`), so a row can be restored.

    sudo ./prism/deploy/fixes/run.sh <name>            # dry run — read the list
    sudo ./prism/deploy/fixes/run.sh <name> apply      # write

| name | bug | what it does |
|---|---|---|
| `recompute_deal_flags` | B42 | Sets each deal's Lending / Platform Deals / Asset Monetisation badges from the live tracker lines behind it. The register now keeps them in step on every line write; this brings the existing book to the same rule. |
| `attach_orphan_lines` | B05 | Attaches a tracker line that has no deal to the company's live deal when there is exactly one; lists the rest for the desk (open the company in Deals → Add product). |
| `purge_test_companies` | B03 | Soft-deletes the test companies and everything under them (leads, deal, lines, lender rows, interactions, documents, prospect row). Default pattern `%test compan%`; pass a third argument to change it: `run.sh purge_test_companies apply "%chennai test%"`. **Read the dry-run list first — it names every row.** |

Run order on production: `purge_test_companies` → `attach_orphan_lines` →
`recompute_deal_flags`. Take a backup first (`deploy/backup/`). Afterwards
`deploy/reconcile.sh` should show section 5c (deal flags) and 6b (orphan lines)
empty, and no test company in section 1.

## Playing the website: `enquiry.sh`

Until the website is wired up, `enquiry.sh` posts a signed sample enquiry to this
box exactly as the website will, and drives the rest of the flow:

    sudo ./prism/deploy/fixes/enquiry.sh send capital --to you@evamfinance.com   # a sample enquiry, mailed to you
    sudo ./prism/deploy/fixes/enquiry.sh send assets                            # PRISM picks the approvers
    sudo ./prism/deploy/fixes/enquiry.sh list                                   # every enquiry and its stage
    sudo ./prism/deploy/fixes/enquiry.sh mail                                   # the mails and whether they went out
    sudo ./prism/deploy/fixes/enquiry.sh age EV123456 4 && sudo ./prism/deploy/fixes/enquiry.sh chase   # reminder now
    sudo ./prism/deploy/fixes/enquiry.sh expire EV123456 && sudo ./prism/deploy/fixes/enquiry.sh chase  # escalation now

It reads `INTAKE_WEBHOOK_SECRET`, `INTAKE_PUBLIC_BASE_URL` and `SVC_WORKFLOWS_KEY`
from `deploy/compose/.env`. `PRISM_URL=https://host` overrides the target and
`PRISM_INSECURE=1` skips certificate checks on a self-signed staging box.
