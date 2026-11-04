# Probe42 Company Intelligence

One command that resolves an Indian company or LLP on Probe42 and pulls every data block
Probe42 exposes for it into a single JSON document. Every block records the exact URL that
was called, the HTTP status and the timing.

## Setup on the staging server

```bash
cd tools/probe42
python3 -m pip install -r requirements.txt
export PROBE42_API_KEY="<key from apiportal.probe42.in > My Account > API Keys>"
export PROBE42_ENV=sandbox        # or production
```

### Whitelist the staging server IP

Probe42 rejects every request from an IP that is not on the allowlist (HTTP 403).

```bash
python3 probe42_company_intel.py --show-ip
```

Add the printed address at **apiportal.probe42.in > My Account > Whitelisted IPs > Add New IP**.
Use the egress / NAT address if staging sits behind a NAT gateway or load balancer; the
script prints what the outside world sees. Probe42 says additions can take up to 24 hours.

## Usage

```bash
# by CIN
python3 probe42_company_intel.py U24239DL2002PTC114413 -o out/loanzen.json

# by name (exact legal-name match preferred, else first search hit; candidates are kept in the JSON)
python3 probe42_company_intel.py "Infosys Limited" --env production -o out/infosys.json

# by LLPIN, PAN or GSTIN (auto-detected; PAN/GSTIN are resolved to the CIN first)
python3 probe42_company_intel.py AAA-1234
python3 probe42_company_intel.py AAACI1681G --id-type pan

# only some blocks, verbose logging of each URL
python3 probe42_company_intel.py U24239DL2002PTC114413 --only base_details,charges,financials -v

# print the full URL catalogue for the chosen environment
python3 probe42_company_intel.py --list-endpoints --env production
```

Exit codes: `0` ok, `1` entity not found or bad input, `2` API key or IP allowlist problem.

## Output shape

```jsonc
{
  "generated_at": "...", "source": "Probe42", "base_url": "...", "api_version": "1.3",
  "query": "Infosys Limited",
  "resolution": { "detected_type": "name", "id": "L85110KA1981PLC013115", "matched_name": "...", "match_quality": "exact", "candidates": [...], "steps": [ {"url": "...", "http_status": 200} ] },
  "entity":   { "type": "company", "id": "L85110KA1981PLC013115" },
  "summary":  { "legal_name": "...", "status": "...", "active_signatories": [...], "charges_count": 3, "latest_revenue": ..., "sections_ok": [...], "sections_failed": {...} },
  "sections": {
    "base_details":   { "url": "https://api.probe42.in/probe_pro/companies/L85110KA1981PLC013115?identifier_type=CIN", "http_status": 200, "ok": true, "elapsed_ms": 412, "description": "...", "path_status": "confirmed", "data": {...} },
    "charges":        { "url": ".../charges", "http_status": 200, "ok": true, "data": {...} },
    "gst_details":    { "url": ".../gst-details", "http_status": 404, "ok": false, "error": "..." }
  },
  "calls": [ {"url": "...", "http_status": 200, "ok": true, "elapsed_ms": 412} ],
  "stats": { "total_calls": 17, "ok": 12, "failed": 5, "elapsed_ms": 6100 }
}
```

`sections` is the comprehensive data. `summary` is a flat headline block derived from it.
`calls` is the complete audit trail of URLs, in order.

## Endpoint registry (`endpoints.json`)

All URLs come from `endpoints.json`, not from code. Each entry has a `status`:

| status | meaning |
|---|---|
| `confirmed` | path verified against Probe42's public Python client (`/companies` search, `/companies/{cin}`, `/authorized-signatories`, `/charges`, `/financials`) |
| `expected` | path as it appears on the Probe42 API portal "Explore API" page. If your portal shows a different path for a block, edit it here. A wrong path simply returns 404 in that section and the rest of the report is unaffected. |

To add a block Probe42 enables for your plan later, add one object to the `company` or `llp`
array with `key`, `path` (use `{id}` for the CIN/LLPIN) and `description`. To stop calling a
block, set `"enabled": false`. `base_urls` holds sandbox, production and lite; `api_version`
sets the `x-api-version` header.

## Behaviour notes

- Headers sent: `x-api-key`, `x-api-version`, `Accept: application/json`.
- Retries with back-off on 429 and 5xx, honouring `Retry-After`; a small client-side throttle keeps calls spaced.
- A 403 on every call is reported as a key or IP-allowlist problem (exit 2).
- A 404 on one block never aborts the run; it is recorded in that section and in `summary.sections_failed`.
- Each run is one HTTP call per block, so a full pull on production costs roughly one credit per enabled block. Use `--only` while testing.
