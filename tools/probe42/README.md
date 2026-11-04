# Probe42 Company Intelligence (API v1.3)

One command that resolves an Indian company, LLP or proprietorship/partnership (P&P) on
Probe42 and pulls every data block Probe42 exposes into a single JSON document. Every block
records the exact URL called, the HTTP status, the timing and the data or the error.

Built from Probe42's official OpenAPI specs (company_*, llp_*, p&p_*, director_* v1.3).

## Setup on the staging server

```bash
cd tools/probe42
python3 -m pip install -r requirements.txt
export PROBE42_API_KEY="<apiportal.probe42.in > My Account > API Keys>"
export PROBE42_ENV=sandbox            # or production
export PROBE42_CLIENT_NAME=evam       # client_name sent for PDF reports
```

### Whitelist the staging server IP

Probe42 rejects every request from an IP that is not on the allowlist (HTTP 403).

```bash
python3 probe42_company_intel.py --show-ip
```

Add the printed address at **apiportal.probe42.in > My Account > Whitelisted IPs > Add New IP**.
Use the egress / NAT address if staging sits behind a gateway; the script prints what the
outside world sees. Probe42 says additions can take up to 24 hours.

## Usage

```bash
# CIN / LLPIN / PAN / BID (Probe42's 64-char id from the business.probe42.in URL) are auto-detected
python3 probe42_company_intel.py U15549PN1992FTC065522 -o out/cocacola.json
python3 probe42_company_intel.py AAACB8573G -o out/cocacola.json                 # PAN -> resolved to CIN
python3 probe42_company_intel.py 5eeeb9ce3e0afb0d...bd4ac1e4 -o out/x.json         # BID
python3 probe42_company_intel.py AAA-1234 -o out/llp.json                          # LLPIN

# first-time entity: let the script trigger Probe42's async update and wait for it
python3 probe42_company_intel.py U15549PN1992FTC065522 --auto-update -o out/cocacola.json

# everything: comprehensive + director profiles/networks + curated PDF report + AoA/MoA + MCA docs
python3 probe42_company_intel.py AAACB8573G --auto-update --directors --report --inc-docs AoA,MoA,CoI --documents 5 -o out/cocacola.json

# cheap check only (no credits): base-details + datastatus
python3 probe42_company_intel.py U15549PN1992FTC065522 --mode base

# refresh stale data: force an update, or only if older than N days
python3 probe42_company_intel.py U15549PN1992FTC065522 --refresh
python3 probe42_company_intel.py U15549PN1992FTC065522 --max-age-days 30

# director lookup only (8-digit DIN)
python3 probe42_company_intel.py 00121863

# see every URL without calling anything / print the catalogue
python3 probe42_company_intel.py AAACB8573G --dry-run --directors --report
python3 probe42_company_intel.py --list-endpoints --env production
```

Exit codes: `0` ok · `1` not found / bad input · `2` API key or IP allowlist problem ·
`3` data pending on Probe42's side (update not fulfilled yet, or PAN mapping in progress: retry later).

## What gets called, in order

| step | URL (data base: `probe_pro_sandbox` / `probe_pro`) | notes |
|---|---|---|
| 1 | `GET {prefix}/{id}/base-details[?identifier_type=PAN\|BID]` | overview, signatories, open charges; resolves CIN/LLPIN from PAN/BID; follows a changed CIN (422) |
| 2 | `GET {prefix}/{id}/datastatus` | freshness dates; drives `--max-age-days` |
| 3 | `POST {prefix}/{id}/update` → `GET {prefix}/{id}/get-update-status?request_id=` (poll) | only with `--auto-update`, `--refresh` or `--max-age-days`; waits for `FULFILLED` |
| 4 | `GET {prefix}/{id}/comprehensive-details` | the full dataset, consumes credits |
| 5 | `GET /directors/{din}?identifier_type=DIN` + `GET /director?din=` | with `--directors`, per active signatory (cap `--max-directors`) |
| 6 | reports base `probe_reports[_sandbox]`: `GET {prefix}/{id}/reports?type=pdf&client_name=&unit=INR` | with `--report`, saved to `<output>_files/` |
| 7 | `GET {prefix}/{id}/reference-document?type=AoA\|MoA\|CoI\|INC20A` | with `--inc-docs` |
| 8 | `GET {prefix}/{id}/get-reference-document-by-id?doc_id=` | with `--documents N`, doc_ids harvested from comprehensive-details |

`{prefix}` is `/companies`, `/llps` or `/pnps`. PAN holder type (4th letter) picks the family:
C = company, F = LLP, P/H/A/B/T = P&P; if that guess 404s the other families are tried.

## Output shape

```jsonc
{
  "generated_at": "...", "source": "Probe42", "api_version": "1.3", "env": "sandbox",
  "base_urls": {"data": "...", "reports": "..."}, "query": "AAACB8573G",
  "resolution": {"detected_type": "pan", "id": "AAACB8573G", "resolved_id": "U15549PN1992FTC065522", ...},
  "entity":     {"type": "company", "id": "U15549PN1992FTC065522", "identifier_type": "CIN", "legal_name": "...", "bid": "..."},
  "summary":    {"status": "...", "active_compliance": "...", "active_signatories": [...], "open_charges_amount": 0,
                 "key_indicators": {...}, "probe_financial_score": {...}, "latest_financials": {"year": "2025-03-31", "ratios": {...}, "pnl_subtotals": {...}},
                 "gst": {...}, "epfo": {...}, "legal": {...}, "data_freshness": {...}, "sections_ok": [...], "sections_failed": {...}},
  "sections": {
    "base_details":          {"url": "https://api.probe42.in/probe_pro/companies/AAACB8573G/base-details?identifier_type=PAN", "http_status": 200, "ok": true, "elapsed_ms": 412, "cost": "base", "description": "...", "metadata": {...}, "data": {...}},
    "data_status":           {...},
    "update_request":        {...},            // only when an update was triggered
    "update_status":         {"polls": [{"status": "REQUESTED"}, {"status": "FULFILLED"}], "fulfilled": true, ...},
    "comprehensive_details": {"url": ".../comprehensive-details", "http_status": 200, "data": { "company", "authorized_signatories", "director_network",
                               "open_charges", "charge_sequence", "financials", "financial_parameters", "shareholdings", "shareholdings_more_than_five_percent",
                               "director_shareholdings", "gst_details", "establishments_registered_with_epfo", "legal_history", "credit_ratings",
                               "holding_entities", "subsidiary_entities", "related_party_transactions", "peer_comparison", "defaulter_list",
                               "struckoff248_details", "msme_supplier_payment_delays", "probe_financial_score", "key_indicators", "filing_dates", ... }},
    "directors":             {"items": {"08289108": {"details": {...}, "network": {...}}}},
    "report_pdf":            {"url": "...", "saved_to": "out/cocacola_files/U15549PN1992FTC065522_report.pdf", "bytes": 183221},
    "incorporation_documents": {"items": {"AoA": {...}, "MoA": {...}}},
    "reference_documents":   {"available_doc_ids": [...], "items": {...}}
  },
  "calls": [{"method": "GET", "url": "...", "http_status": 200, "ok": true, "elapsed_ms": 412}],
  "stats": {"total_calls": 13, "ok": 12, "failed": 1, "elapsed_ms": 9100}
}
```

## Error handling (from Probe42's error catalogue)

| HTTP | meaning | script behaviour |
|---|---|---|
| 403 | bad key or IP not whitelisted | exit 2 with a pointer to the allowlist page |
| 404 "not probed yet" | entity never pulled into Probe42 | `--auto-update` triggers update + poll; otherwise exit 3 with a hint |
| 404 "does not exist" / "Version is not supported" | wrong identifier or `x-api-version` | exit 1, message in `error` |
| 422 "changed CIN. New CIN is …" | entity re-registered | new CIN parsed and the run continues with it |
| 422 "Invalid CIN format" / inactive | bad input | exit 1 |
| 424 "PAN mapping is initiated" | Probe42 is mapping the PAN | exit 3, retry in ~4 hours |
| 429 limit / credits | rate or credit exhaustion | recorded in the section with a hint; use `--mode base` |
| 5xx | Probe42 side | retried 3 times with back-off |

## Endpoint registry (`endpoints.json`)

Every URL comes from `endpoints.json`. `base_urls` hold the data and reports hosts per environment,
`entity_prefix` maps company / llp / pnp to their path prefixes, and each endpoint lists the
entity types it supports and a `cost` label. Set `"enabled": false` to skip one. The legal-name
search entry is marked `unverified` because its spec was not in the bundle: prefer passing a
CIN, PAN, LLPIN or BID.

## Cost notes

- `base-details` and `datastatus` are cheap. `comprehensive-details`, `update` and the PDF report
  consume credits; `--directors` adds two calls per director.
- Use `--dry-run` to see the exact URL plan before spending credits, and `--mode base` for KYB checks.
