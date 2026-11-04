#!/usr/bin/env python3
"""
Tracxn Company Intelligence fetcher (API v2.2 + v3.0 Key Metrics).

Resolves a company on Tracxn from a name, a normalized domain, or a Tracxn company id,
then pulls every company-level dataset Tracxn exposes into ONE comprehensive JSON document.
Every block records the exact URL called, the request body, the HTTP status, timing and
the data (or the error and what it means).

Workflow:
  1. /companies/search       name -> {id, name, domain}         (skipped when a domain or id is given)
  2. /companies              full profile for the id / domain
  3. /transactions           all funding rounds of the company (paginated, 20 per page)
  4. /acquisitiontransactions deals where the company is the target, and deals where it is the acquirer
  5. /investors              investors whose portfolio contains the company
  6. /companies (relations)  competitors, similar companies, subsidiaries (part-of), customers, vendors   [--relations]
  7. /companies/metric       v3.0 Key Metrics: peer counts / funding stats for the company's sector+country [--metrics]

Usage:
    export TRACXN_ACCESS_TOKEN="..."           # playground or production token, they differ
    python3 tracxn_company_intel.py "zomato" -o out/zomato.json
    python3 tracxn_company_intel.py zomato.com --env production --relations --metrics -o out/zomato.json
    python3 tracxn_company_intel.py nrG5itQu_3voVifgJjj52SGgucwK5dAJUNozT6vhxPw --id-type id
    python3 tracxn_company_intel.py zomato.com --dry-run
    python3 tracxn_company_intel.py --list-endpoints --env production

Exit codes: 0 ok, 1 not found / bad input, 2 token problem (401/403), 4 rate or credit limit (429/900).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("tracxn")
HERE = Path(__file__).resolve().parent
DEFAULT_REGISTRY = HERE / "endpoints.json"
TRACXN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{40,50}$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)


class TracxnError(Exception):
    pass


class AuthError(TracxnError):
    pass


class NotFoundError(TracxnError):
    pass


class LimitError(TracxnError):
    pass


def normalize_domain(value: str) -> str:
    v = value.strip().lower()
    if "://" in v or v.startswith("www."):
        v = urlparse(v if "://" in v else "http://" + v).netloc or v
    v = v.split("/")[0]
    return v[4:] if v.startswith("www.") else v


# --------------------------------------------------------------------------- #
class TracxnClient:
    def __init__(self, token: str, base: str, metrics_base: str, timeout: float = 60.0,
                 min_interval: float = 0.5, dry_run: bool = False):
        if not token and not dry_run:
            raise AuthError("TRACXN_ACCESS_TOKEN is not set")
        self.base = base.rstrip("/")
        self.metrics_base = metrics_base.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self.dry_run = dry_run
        self._last = 0.0
        self.calls: List[Dict[str, Any]] = []
        self.session = requests.Session()
        self.session.headers.update({"accessToken": token or "DRY-RUN", "Content-Type": "application/json",
                                     "Accept": "application/json", "User-Agent": "tracxn-company-intel/1.0"})
        retry = Retry(total=3, backoff_factor=2.0, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=("POST",), respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    @staticmethod
    def explain(status: int, body: Any) -> str:
        table = {400: "Bad request: filter/sort syntax invalid (domains must be normalized, dates dd/mm/yyyy).",
                 401: "Authentication issue: accessToken missing or invalid.",
                 403: "Unauthorized: token expired, used in the wrong environment (playground vs production), or access denied.",
                 404: "Endpoint does not exist (check /playground vs production path).",
                 405: "Wrong HTTP method (all Tracxn data endpoints are POST).",
                 429: "Rate limit exceeded (playground: 100 calls/hour, 1000/day; metrics: 10/hour).",
                 500: "Tracxn internal error, retry later.",
                 900: "Credit limit exceeded: contact Tracxn to renew."}
        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("error") or body.get("errorMessage") or "")
        base = table.get(status, f"HTTP {status}")
        return f"{base} Tracxn said: {msg!r}" if msg else base

    def post(self, path: str, payload: Dict[str, Any], metrics: bool = False) -> Dict[str, Any]:
        url = (self.metrics_base if metrics else self.base) + path
        env: Dict[str, Any] = {"url": url, "method": "POST", "request_body": payload,
                               "requested_at": datetime.now(timezone.utc).isoformat()}
        if self.dry_run:
            env.update(ok=None, http_status=None, elapsed_ms=0, dry_run=True)
            self.calls.append({"method": "POST", "url": url, "body": payload, "http_status": None, "ok": None})
            LOG.info("DRY-RUN POST %s %s", url, json.dumps(payload))
            return env
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        t0 = time.monotonic()
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout)
            env["http_status"] = resp.status_code
            try:
                body: Any = resp.json()
            except ValueError:
                body = {"raw": resp.text[:4000]}
            # Tracxn signals credit exhaustion with a non-standard 900 code, sometimes inside a 200 body.
            code = resp.status_code
            if isinstance(body, dict) and str(body.get("statusCode") or body.get("code") or "") == "900":
                code = 900
            if 200 <= code < 300:
                env["ok"] = True
                env["data"] = body
            else:
                env["ok"] = False
                env["error"] = self.explain(code, body)
                env["response"] = body
                env["http_status"] = code
        except requests.RequestException as exc:
            env.update(ok=False, http_status=None, error=f"network error: {exc}")
        finally:
            self._last = time.monotonic()
            env["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        self.calls.append({k: env.get(k) for k in ("method", "url", "http_status", "ok", "elapsed_ms", "error")} | {"body": payload})
        LOG.info("POST %s %s -> %s (%s ms)", url, json.dumps(payload)[:120], env.get("http_status"), env["elapsed_ms"])
        return env


# --------------------------------------------------------------------------- #
def items_of(data: Any) -> List[Dict[str, Any]]:
    """Tracxn wraps results differently per endpoint/version; find the list of entities."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("result", "results", "data", "companies", "transactions", "investors", "acquisitions", "entities", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
            if isinstance(v, dict):
                inner = items_of(v)
                if inner:
                    return inner
    return []


def total_of(data: Any) -> Optional[int]:
    if isinstance(data, dict):
        for key in ("totalCount", "total", "count", "numFound", "totalResults"):
            if isinstance(data.get(key), int):
                return data[key]
        inner = data.get("data") or data.get("result")
        if isinstance(inner, dict):
            return total_of(inner)
    return None


class CompanyIntel:
    def __init__(self, client: TracxnClient, reg: Dict[str, Any]):
        self.c = client
        self.reg = reg
        self.size = int(reg.get("page_size", 20))

    def ep(self, key: str) -> Dict[str, Any]:
        return self.reg["endpoints"][key]

    def call(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        spec = self.ep(key)
        env = self.c.post(spec["path"], payload, metrics=spec.get("base") == "metrics")
        env["description"] = spec.get("description")
        return env

    def paged(self, key: str, filt: Dict[str, Any], sort: Optional[List[Dict[str, Any]]], max_pages: int) -> Dict[str, Any]:
        """Collect up to max_pages pages of 20 into one section with a 'pages' audit list."""
        all_items: List[Dict[str, Any]] = []
        pages: List[Dict[str, Any]] = []
        first: Dict[str, Any] = {}
        total: Optional[int] = None
        for n in range(max_pages):
            payload: Dict[str, Any] = {"filter": filt, "from": n * self.size, "size": self.size}
            if sort:
                payload["sort"] = sort
            env = self.call(key, payload)
            if n == 0:
                first = env
            pages.append({k: env.get(k) for k in ("url", "http_status", "ok", "elapsed_ms", "error")} | {"from": n * self.size})
            if not env.get("ok"):
                break
            batch = items_of(env.get("data"))
            if total is None:
                total = total_of(env.get("data"))
            all_items.extend(batch)
            if len(batch) < self.size or (total is not None and len(all_items) >= total):
                break
        out = dict(first)
        out["pages"] = pages
        out["items"] = all_items
        out["items_fetched"] = len(all_items)
        out["total_reported"] = total
        out["truncated"] = bool(total and len(all_items) < total)
        out.pop("data", None)
        return out

    # ---- resolution ----
    @staticmethod
    def classify(query: str, forced: str) -> str:
        if forced != "auto":
            return forced
        q = query.strip()
        if TRACXN_ID_RE.match(q) and "." not in q:
            return "id"
        if DOMAIN_RE.match(normalize_domain(q)):
            return "domain"
        return "name"

    def resolve(self, query: str, id_type: str) -> Dict[str, Any]:
        res: Dict[str, Any] = {"input": query, "detected_type": id_type, "steps": []}
        if id_type == "id":
            res.update(id=query.strip(), filter={"id": [query.strip()]})
            return res
        if id_type == "domain":
            d = normalize_domain(query)
            res.update(domain=d, filter={"domain": [d]})
            return res
        env = self.call("company_search", {"filter": {"companyName": query.strip()}})
        res["steps"].append({k: env.get(k) for k in ("url", "http_status", "ok", "error")})
        if env.get("http_status") in (401, 403):
            raise AuthError(env["error"])
        if env.get("http_status") in (429, 900):
            raise LimitError(env["error"])
        if not env.get("ok"):
            raise NotFoundError(f"Name search failed: {env.get('error')}")
        cands = items_of(env.get("data"))
        res["candidates"] = cands
        if not cands:
            raise NotFoundError(f"No Tracxn company matched '{query}'. In the playground only Cybersecurity-feed companies are visible.")
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
        exact = [c for c in cands if norm(c.get("name")) == norm(query)]
        chosen = exact[0] if exact else cands[0]
        res.update(id=chosen.get("id"), domain=chosen.get("domain"), matched_name=chosen.get("name"),
                   match_quality="exact" if exact else "first_result", ambiguous=len(cands) > 1 and not exact)
        res["filter"] = {"id": [chosen["id"]]} if chosen.get("id") else {"domain": [chosen.get("domain")]}
        return res

    # ---- summary ----
    @staticmethod
    def summarise(profile: Dict[str, Any], sections: Dict[str, Any]) -> Dict[str, Any]:
        p = profile or {}
        g = lambda *ks: next((p.get(k) for k in ks if p.get(k) is not None), None)
        s: Dict[str, Any] = {
            "name": g("name"), "domain": g("domain"), "tracxn_id": g("id", "companyId", "tracxnId"), "tracxn_url": g("tracxnUrl"),
            "founded_year": g("foundedYear"), "stage": g("stage"), "description": (g("description") or "")[:600] or None,
            "location": g("location"), "sectors": g("sectorList"), "practice_areas": g("practiceAreaList"),
            "total_money_raised": g("totalMoneyRaised"), "total_equity_funding": g("totalEquityFunding"),
            "latest_valuation": g("latestValuation"), "latest_annual_revenue": g("latestAnnualRevenue"),
            "latest_employee_count": g("latestEmployeeCount"), "employee_info": g("employeeInfo"),
            "tracxn_score": g("tracxnScore"), "investor_score": g("tracxnInvestorScore"),
            "growth_score": g("tracxnGrowthScore"), "size_score": g("tracxnSizeScore"), "execution_score": g("tracxnExecutionScore"),
            "ratings": g("companyRatings"), "ipo": g("ipoInfo"), "acquired_by": g("acquirerInfo"), "deadpool": g("deadpoolInfo"),
            "part_of": g("partOf"), "profile_links": g("profileLinks"), "emails": g("emailList"), "phones": g("contactNumberList"),
        }
        inv = g("investorList") or []
        s["investor_count"] = len(inv) if isinstance(inv, list) else None
        s["investors"] = [i.get("name") or i.get("domain") for i in inv][:50] if isinstance(inv, list) else None
        tx = sections.get("funding_rounds") or {}
        s["funding_rounds_fetched"] = tx.get("items_fetched")
        s["funding_rounds_total"] = tx.get("total_reported")
        for k in ("acquisitions_as_target", "acquisitions_as_acquirer", "investor_profiles", "competitors", "similar", "subsidiaries", "customers", "vendors"):
            sec = sections.get(k)
            if sec:
                s[f"{k}_count"] = sec.get("items_fetched")
        s["sections_ok"] = [k for k, v in sections.items() if v.get("ok")]
        s["sections_failed"] = {k: v.get("error") for k, v in sections.items() if v.get("ok") is False}
        return s


# --------------------------------------------------------------------------- #
def list_endpoints(reg: Dict[str, Any], env: str) -> None:
    base, mbase = reg["base_urls"][env], reg["metrics_base_urls"][env]
    print(f"env: {env}   header: accessToken   token page: {reg['token_pages'][env]}\n")
    for key, spec in reg["endpoints"].items():
        b = mbase if spec.get("base") == "metrics" else base
        print(f"  {spec['method']} {b}{spec['path']}   [{key}]\n       {spec['description']}")
    print("\n  relation lookups reuse POST .../companies with filter:")
    for key, spec in reg["company_relations"].items():
        print(f"    {key:13} {{\"{spec['filter']}\": {'<domain>' if spec.get('single') else '[<domain>]'}}}   {spec['description']}")
    print("\n  rate limits:", reg.get("rate_limits"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="?", help="Company name, normalized domain (zomato.com) or Tracxn company id")
    p.add_argument("--id-type", choices=["auto", "name", "domain", "id"], default="auto")
    p.add_argument("--env", choices=["playground", "production"], default=os.getenv("TRACXN_ENV", "playground"))
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--base-url", help="Override v2.2 base URL (testing)")
    p.add_argument("--metrics-url", help="Override v3.0 metrics base URL (testing)")
    p.add_argument("--max-pages", type=int, default=5, help="Pages of 20 to fetch per list section (default 5 = 100 rows)")
    p.add_argument("--relations", action="store_true", help="Also fetch competitors, similar, subsidiaries, customers, vendors")
    p.add_argument("--metrics", action="store_true", help="Also call v3.0 Key Metrics for the company's primary sector + country")
    p.add_argument("--no-investors", action="store_true")
    p.add_argument("--no-acquisitions", action="store_true")
    p.add_argument("--no-transactions", action="store_true")
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--compact", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--list-endpoints", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if (args.verbose or args.dry_run) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    base = args.base_url or reg["base_urls"][args.env]
    mbase = args.metrics_url or reg["metrics_base_urls"][args.env]
    if args.list_endpoints:
        list_endpoints(reg, args.env)
        return 0
    if not args.query:
        print("error: query required (name / domain / Tracxn id), or --list-endpoints", file=sys.stderr)
        return 1
    try:
        client = TracxnClient(os.getenv("TRACXN_ACCESS_TOKEN", ""), base, mbase, timeout=args.timeout, dry_run=args.dry_run)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intel = CompanyIntel(client, reg)
    t0 = time.monotonic()
    report: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(), "source": "Tracxn", "env": args.env,
                              "base_urls": {"v2.2": base, "v3.0_metrics": mbase}, "query": args.query, "dry_run": args.dry_run}
    sections: Dict[str, Any] = {}
    exit_code = 0
    try:
        id_type = CompanyIntel.classify(args.query, args.id_type)
        res = intel.resolve(args.query, id_type)
        report["resolution"] = res
        if res.get("ambiguous"):
            LOG.warning("Name search ambiguous; using '%s' (%s)", res.get("matched_name"), res.get("domain") or res.get("id"))

        # 2. profile
        prof = intel.call("companies", {"filter": res["filter"], "from": 0, "size": 1})
        if prof.get("http_status") in (401, 403):
            raise AuthError(prof["error"])
        if prof.get("http_status") in (429, 900):
            raise LimitError(prof["error"])
        items = items_of(prof.get("data")) if prof.get("ok") else []
        profile = items[0] if items else {}
        prof["profile"] = profile
        prof.pop("data", None) if items else None
        sections["profile"] = prof
        if prof.get("ok") and not profile and not args.dry_run:
            raise NotFoundError("Company resolved but /companies returned no profile (playground only exposes the Cybersecurity feed; "
                                "use --env production for other companies).")
        domain = profile.get("domain") or res.get("domain")
        cid = profile.get("id") or profile.get("companyId") or res.get("id")
        report["entity"] = {"name": profile.get("name") or res.get("matched_name"), "domain": domain, "tracxn_id": cid,
                            "tracxn_url": profile.get("tracxnUrl")}
        by_domain = {"domain": [domain]} if domain else None
        by_id = {"companiesId": [cid]} if cid else None

        # 3. funding rounds
        if not args.no_transactions and (by_domain or by_id):
            sections["funding_rounds"] = intel.paged("transactions", by_domain or by_id,
                                                     [{"sortField": "transactionFundingRoundDate", "order": "DEFAULT"}], args.max_pages)
        # 4. acquisitions
        if not args.no_acquisitions and domain:
            sections["acquisitions_as_target"] = intel.paged("acquisitions", {"domain": [domain]},
                                                             [{"sortField": "announcementDate", "order": "DEFAULT"}], args.max_pages)
            sections["acquisitions_as_acquirer"] = intel.paged("acquisitions", {"acquirerListDomain": [domain]},
                                                               [{"sortField": "announcementDate", "order": "DEFAULT"}], args.max_pages)
        # 5. investors
        if not args.no_investors and domain:
            sections["investor_profiles"] = intel.paged("investors", {"domain": [domain]}, None, args.max_pages)
        # 6. relations
        if args.relations and domain:
            for key, spec in reg["company_relations"].items():
                filt = {spec["filter"]: (domain if spec.get("single") else [domain])}
                sec = intel.paged("companies", filt, [{"sortField": "tracxnScore", "order": "DEFAULT"}], min(args.max_pages, 2))
                sec["description"] = spec["description"]
                sections[key] = sec
        # 7. metrics
        if args.metrics:
            sectors = profile.get("sectorList") or profile.get("practiceAreaList") or []
            country = ((profile.get("location") or {}).get("country")) if isinstance(profile.get("location"), dict) else None
            filt: Dict[str, Any] = {}
            first = sectors[0] if isinstance(sectors, list) and sectors else None
            if isinstance(first, dict):
                pa = first.get("practiceAreaId") or first.get("practiceArea") or first.get("id")
                if pa:
                    filt["practiceAreaId"] = [pa]
            if country:
                filt["country"] = [country if isinstance(country, str) else country.get("name")]
            if filt:
                m = {}
                for metric in ("count", "sumFundingAmount", "medianFundingAmount", "medianEmployeeCount"):
                    m[metric] = intel.call("company_metrics", {"filter": filt, "metric": metric, "bucket": [{"field": "companyStage"}]})
                sections["sector_metrics"] = {"ok": any(v.get("ok") for v in m.values()), "filter": filt, "items": m,
                                              "url": "(multiple, see items)", "note": "metrics API limit is 10 calls/hour"}
            else:
                sections["sector_metrics"] = {"ok": False, "error": "profile carries no sector/country to aggregate on"}

        report["summary"] = CompanyIntel.summarise(profile, sections)
        report["sections"] = sections
    except AuthError as exc:
        report["error"] = str(exc); exit_code = 2
    except LimitError as exc:
        report["error"] = str(exc); exit_code = 4
    except NotFoundError as exc:
        report["error"] = str(exc); exit_code = 1
    except TracxnError as exc:
        report["error"] = str(exc); exit_code = 1
    finally:
        report.setdefault("sections", sections)
        report["calls"] = client.calls
        report["stats"] = {"total_calls": len(client.calls), "ok": sum(1 for c in client.calls if c.get("ok")),
                           "failed": sum(1 for c in client.calls if c.get("ok") is False),
                           "elapsed_ms": round((time.monotonic() - t0) * 1000)}
    text = json.dumps(report, indent=None if args.compact else 2, ensure_ascii=False, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({report['stats']['ok']}/{report['stats']['total_calls']} calls ok, exit {exit_code})", file=sys.stderr)
    else:
        print(text)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
