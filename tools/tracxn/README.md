# Tracxn Company Intelligence (API v2.2 + v3.0 Key Metrics)

One command that resolves a company on Tracxn and pulls every company-level dataset Tracxn
exposes into a single JSON document. Every block records the exact URL and request body,
the HTTP status, the timing, and the data or the error.

Built from Tracxn's "TracxnAPI Playground" Postman collection.

## Getting the API token

Tracxn tokens are **environment specific**. A playground token does not work on production
and vice versa.

**Playground token (free, testing only, not for commercial use)**

1. Log in at https://platform.tracxn.com.
2. Go to **Data Solutions > API & Developer Tools > Playground > API Token**,
   or open https://platform.tracxn.com/a/api/playground/apitoken directly.
3. Click **Generate API Token** and copy it.

**Production token**

Same menu, **Data Solutions > API & Developer Tools > Production > API Token**. It only
appears once your account has an API subscription; otherwise contact your Tracxn account
manager to enable it.

The playground mirrors production exactly (same filters, same response shape) but only
returns companies, rounds, investors and acquisitions tagged to the **Cybersecurity feed**.
Any other company, such as an Indian NBFC borrower, returns nothing until you switch to
`--env production`.

## Setup on the staging server

```bash
cd tools/tracxn
python3 -m pip install -r requirements.txt     # or use the same .venv as probe42
export TRACXN_ACCESS_TOKEN="<token>"
export TRACXN_ENV=playground                   # or production
```

Tracxn has no IP allowlist. Authentication is the `accessToken` header only.

## Usage

```bash
# by name (search -> best match), by normalized domain, or by Tracxn id
python3 tracxn_company_intel.py "Zomato" -o out/zomato.json
python3 tracxn_company_intel.py zomato.com --env production -o out/zomato.json
python3 tracxn_company_intel.py nrG5itQu_3voVifgJjj52SGgucwK5dAJUNozT6vhxPw --id-type id

# everything: profile + rounds + M&A + investors + competitors/similar/subsidiaries/customers/vendors + sector metrics
python3 tracxn_company_intel.py zomato.com --env production --relations --metrics -o out/zomato.json

# cheaper runs
python3 tracxn_company_intel.py zomato.com --max-pages 1 --no-investors --no-acquisitions

# see the URL plan without spending calls / print the catalogue
python3 tracxn_company_intel.py zomato.com --dry-run --relations
python3 tracxn_company_intel.py --list-endpoints --env production
```

Exit codes: `0` ok · `1` not found / bad input · `2` token problem (401/403) · `4` rate or credit limit (429/900).

## What gets called, in order

| step | URL (production; playground inserts `/playground` after `2.2` or `3.0`) | body filter | flag |
|---|---|---|---|
| 1 | `POST /api/2.2/companies/search` | `{"companyName": "<name>"}` | only for name input |
| 2 | `POST /api/2.2/companies` | `{"id": [..]}` or `{"domain": [..]}` | always |
| 3 | `POST /api/2.2/transactions` | `{"domain": [..]}`, paginated 20 per page | unless `--no-transactions` |
| 4 | `POST /api/2.2/acquisitiontransactions` × 2 | `{"domain": [..]}` (as target) and `{"acquirerListDomain": [..]}` (as acquirer) | unless `--no-acquisitions` |
| 5 | `POST /api/2.2/investors` | `{"domain": [..]}` (portfolio company) | unless `--no-investors` |
| 6 | `POST /api/2.2/companies` × 5 | `competitorOf`, `similarsOf`, `partOfDomain`, `vendorsDomain`, `customersDomain` | `--relations` |
| 7 | `POST /api/3.0/companies/metric` × 4 | company's primary practice area + country, bucketed by stage | `--metrics` |

Each list section pages through results 20 at a time up to `--max-pages` (default 5, so 100 rows)
and reports `total_reported` and `truncated` so you know if more exist.

## Output shape

```jsonc
{
  "generated_at": "...", "source": "Tracxn", "env": "production",
  "base_urls": {"v2.2": "...", "v3.0_metrics": "..."}, "query": "Zomato",
  "resolution": {"detected_type": "name", "candidates": [...], "id": "...", "domain": "zomato.com", "match_quality": "exact"},
  "entity":     {"name": "Zomato", "domain": "zomato.com", "tracxn_id": "...", "tracxn_url": "..."},
  "summary":    {"stage": "...", "founded_year": 2008, "sectors": [...], "total_money_raised": {...}, "latest_valuation": {...},
                 "latest_annual_revenue": {...}, "latest_employee_count": {...}, "tracxn_score": 82.5, "investors": [...],
                 "funding_rounds_total": 45, "competitors_count": 12, "sections_ok": [...], "sections_failed": {...}},
  "sections": {
    "profile":                 {"url": ".../companies", "request_body": {...}, "http_status": 200, "ok": true, "elapsed_ms": 410, "profile": { ...all Companies API output keys... }},
    "funding_rounds":          {"url": ".../transactions", "pages": [{"from": 0, "http_status": 200}, ...], "items": [...], "items_fetched": 45, "total_reported": 45, "truncated": false},
    "acquisitions_as_target":  {...}, "acquisitions_as_acquirer": {...},
    "investor_profiles":       {...},
    "competitors": {...}, "similar": {...}, "subsidiaries": {...}, "customers": {...}, "vendors": {...},
    "sector_metrics":          {"filter": {...}, "items": {"count": {...}, "sumFundingAmount": {...}, "medianFundingAmount": {...}, "medianEmployeeCount": {...}}}
  },
  "calls": [{"method": "POST", "url": "...", "body": {...}, "http_status": 200, "ok": true, "elapsed_ms": 410}],
  "stats": {"total_calls": 17, "ok": 17, "failed": 0, "elapsed_ms": 8100}
}
```

## Limits and errors (from the collection)

| code | meaning | script behaviour |
|---|---|---|
| 400 | filter/sort syntax invalid | recorded in the section; domains must be normalized, dates `dd/mm/yyyy` |
| 401 | token missing or invalid | exit 2 |
| 403 | token expired or wrong environment | exit 2 |
| 404 | endpoint missing (playground vs production path) | recorded |
| 429 | rate limit: playground 100/hour and 1000/day, metrics 10/hour and 100/day | exit 4 |
| 900 | credit limit exhausted (Tracxn custom code) | exit 4 |
| 5xx | Tracxn side | retried 3 times with back-off |

The script spaces calls half a second apart. A full run with `--relations --metrics` is about
17 calls, so the playground allows roughly five such runs per hour.

## Notes

- Tracxn v2.2 is being deprecated in favour of v3.0; today only the Key Metrics endpoints exist
  on v3.0, so the entity endpoints still use 2.2. When Tracxn publishes 3.0 entity endpoints,
  change `base_urls` in `endpoints.json`.
- The collection ships no example responses, so the script locates the result list
  generically (`result`, `results`, `data`, ...) and keeps the raw body under `data` when it
  cannot. If a section shows `items_fetched: 0` with a 200, open its raw body and tell me the
  key name; it is a one-line change.
- Tracxn is startup and private-market intelligence. For Indian registry, compliance and
  financial-statement data use the Probe42 tool in `tools/probe42`; the two complement each other.
