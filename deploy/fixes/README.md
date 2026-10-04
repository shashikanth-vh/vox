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
