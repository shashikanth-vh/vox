#!/usr/bin/env python3
"""
Probe42 Company Intelligence fetcher (API v1.3).

Resolves a company / LLP / proprietorship-partnership (P&P) on Probe42 from a CIN, LLPIN,
PAN, BID (Probe42's 64-char unique id) or legal name, then pulls every data block Probe42
exposes into ONE comprehensive JSON document. Every block records the exact URL called,
the HTTP status, the elapsed time and the data (or the error and what to do about it).

Workflow (mirrors Probe42's documented flow):
  1. base-details           cheap; overview + signatories + open charges; resolves CIN from PAN/BID
  2. datastatus             freshness dates; decides whether an update is needed
  3. comprehensive-details  the full dataset (consumes credits)
       404 "not probed yet"  -> with --auto-update: POST /update, poll get-update-status until FULFILLED, retry
  4. --directors            director details + directorship network for each active signatory
  5. --report / --inc-docs / --documents   PDFs from the reports API, saved next to the JSON

Usage:
    export PROBE42_API_KEY="..."
    python3 probe42_company_intel.py U24239DL2002PTC114413 --env sandbox -o out/acme.json
    python3 probe42_company_intel.py AAACB8573G --auto-update --directors --report -o out/cocacola.json
    python3 probe42_company_intel.py 5eeeb9ce3e0a...4ac1e4 --id-type bid
    python3 probe42_company_intel.py 00121863 --id-type din             # director lookup only
    python3 probe42_company_intel.py --show-ip                            # IP to whitelist
    python3 probe42_company_intel.py --list-endpoints --env production    # URL catalogue
    python3 probe42_company_intel.py <id> --dry-run                       # print URLs, call nothing

Exit codes: 0 ok, 1 entity not found / bad input, 2 auth or IP-allowlist problem, 3 update still pending.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("probe42")
HERE = Path(__file__).resolve().parent
DEFAULT_REGISTRY = HERE / "endpoints.json"

CIN_RE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$", re.I)
LLPIN_RE = re.compile(r"^[A-Z]{3}-?\d{4}$", re.I)
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$", re.I)
BID_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
DIN_RE = re.compile(r"^\d{8}$")
NEW_CIN_RE = re.compile(r"New CIN is\s+([LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})", re.I)

# PAN 4th character -> holder type
PAN_TYPE = {"C": "company", "F": "llp", "P": "pnp", "H": "pnp", "A": "pnp", "B": "pnp", "T": "pnp", "G": "company", "L": "company", "J": "company"}


class Probe42Error(Exception):
    pass


class AuthError(Probe42Error):
    pass


class NotFoundError(Probe42Error):
    pass


class PendingError(Probe42Error):
    """Data is being prepared on Probe42's side (424 PAN mapping / update not FULFILLED)."""


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class Probe42Client:
    def __init__(self, api_key: str, data_base: str, reports_base: str, api_version: str,
                 timeout: float = 60.0, min_interval: float = 0.25, dry_run: bool = False):
        if not api_key and not dry_run:
            raise AuthError("PROBE42_API_KEY is not set")
        self.data_base = data_base.rstrip("/")
        self.reports_base = reports_base.rstrip("/")
        self.api_version = api_version
        self.timeout = timeout
        self.min_interval = min_interval
        self.dry_run = dry_run
        self._last = 0.0
        self.calls: List[Dict[str, Any]] = []

        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key or "DRY-RUN",
            "x-api-version": api_version,
            "Accept": "application/json",
            "User-Agent": "probe42-company-intel/2.0",
        })
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=("GET", "POST"), respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    @staticmethod
    def _explain(status: int, body: Any) -> str:
        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("error") or body.get("detail") or "")
        table = {
            400: "Bad request: invalid URL/parameters (name search needs > 3 characters; director lookup needs DIN or PAN).",
            403: "Forbidden: API key invalid/missing, or this server's IP is not on the Probe42 allowlist (apiportal.probe42.in > My Account > Whitelisted IPs).",
            404: "Not found: entity does not exist for this identifier, or data not probed yet (call update), or API version unsupported.",
            422: "Unprocessable: invalid CIN/DIN/PAN format, entity inactive, or CIN has changed (see message).",
            424: "PAN mapping initiated on Probe42's side: retry in ~4 hours (stop after 10 days).",
            429: "Rate limit exceeded or insufficient credits for this call (buy credits on the platform).",
            500: "Probe42 internal error: retry later.",
        }
        if "sandbox" in msg.lower() and "unsupported" in msg.lower():
            return ("Sandbox only serves its fixed set of sample identifiers; this one is not among them. "
                    "Use --env production for real companies (consumes credits), or a sandbox sample CIN from the portal.")
        if status == 403 and "missing authentication token" in msg.lower():
            # AWS API Gateway wording for "no such route": the path is wrong, not the key or IP.
            return ("Endpoint path does not exist on this Probe42 API (gateway said 'Missing Authentication Token'). "
                    "Check the path in endpoints.json; the key and IP allowlist are not the problem.")
        base = table.get(status, f"HTTP {status}")
        return f"{base} Probe42 said: {msg!r}" if msg else base

    def call(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
             stream_binary: bool = False, allow_redirects: bool = True) -> Dict[str, Any]:
        full = f"{url}?{urlencode(params)}" if params else url
        env: Dict[str, Any] = {"url": full, "method": method, "api_version": self.api_version,
                               "requested_at": datetime.now(timezone.utc).isoformat()}
        if self.dry_run:
            env.update(ok=None, http_status=None, elapsed_ms=0, dry_run=True)
            self.calls.append({k: env.get(k) for k in ("method", "url", "http_status", "ok", "elapsed_ms")})
            LOG.info("DRY-RUN %s %s", method, full)
            return env

        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        t0 = time.monotonic()
        try:
            resp = self.session.request(method, url, params=params, timeout=self.timeout,
                                        allow_redirects=allow_redirects)
            env["http_status"] = resp.status_code
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and (stream_binary or "pdf" in ctype or "octet-stream" in ctype):
                env["ok"] = True
                env["content_type"] = ctype
                env["bytes"] = len(resp.content)
                env["_binary"] = resp.content
            else:
                try:
                    body: Any = resp.json()
                except ValueError:
                    body = {"raw": resp.text[:4000]}
                if 200 <= resp.status_code < 300:
                    env["ok"] = True
                    env["data"] = body.get("data", body) if isinstance(body, dict) else body
                    if isinstance(body, dict) and "metadata" in body:
                        env["metadata"] = body["metadata"]
                else:
                    env["ok"] = False
                    env["error"] = self._explain(resp.status_code, body)
                    env["response"] = body
        except requests.RequestException as exc:
            env.update(ok=False, http_status=None, error=f"network error: {exc}")
        finally:
            self._last = time.monotonic()
            env["elapsed_ms"] = round((time.monotonic() - t0) * 1000)

        self.calls.append({k: env.get(k) for k in ("method", "url", "http_status", "ok", "elapsed_ms", "error")})
        LOG.info("%s %s -> %s (%s ms)", method, full, env.get("http_status"), env["elapsed_ms"])
        return env

    def data(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call(method, self.data_base + path, params)

    def report(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("GET", self.reports_base + path, params, stream_binary=True)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
class CompanyIntel:
    def __init__(self, client: Probe42Client, registry: Dict[str, Any]):
        self.c = client
        self.reg = registry
        self.prefix = registry["entity_prefix"]

    # ---- identifiers ----
    @staticmethod
    def classify(query: str, forced: str = "auto") -> str:
        if forced != "auto":
            return forced
        q = query.strip().upper()
        if CIN_RE.match(q):
            return "cin"
        if LLPIN_RE.match(q):
            return "llpin"
        if BID_RE.match(q):
            return "bid"
        if PAN_RE.match(q):
            return "pan"
        if DIN_RE.match(q):
            return "din"
        return "name"

    def _path(self, key: str, group: str, entity: str, ident: str) -> Tuple[str, Dict[str, Any]]:
        spec = self.reg[group][key]
        if spec.get("enabled", True) is False:
            raise Probe42Error(f"endpoint {key} is disabled in endpoints.json")
        if entity not in spec["entities"]:
            raise Probe42Error(f"endpoint {key} is not available for entity type {entity}")
        path = spec["path"].replace("{prefix}", self.prefix.get(entity, "")).replace("{id}", ident)
        return path, spec

    @staticmethod
    def _id_params(id_type: str) -> Dict[str, Any]:
        # CIN / LLPIN need no identifier_type; PAN and BID must be declared.
        return {"identifier_type": id_type.upper()} if id_type.lower() in ("pan", "bid") else {}

    # ---- resolution ----
    def resolve(self, query: str, id_type: str, entity_type: str) -> Dict[str, Any]:
        q = query.strip()
        res: Dict[str, Any] = {"input": query, "detected_type": id_type, "steps": []}
        if id_type == "cin":
            res.update(entity_type="company", id=q.upper(), identifier_type="CIN")
        elif id_type == "llpin":
            llpin = q.upper()
            llpin = llpin if "-" in llpin else f"{llpin[:3]}-{llpin[3:]}"
            res.update(entity_type="llp", id=llpin, identifier_type="LLPIN")
        elif id_type == "bid":
            res.update(entity_type=entity_type if entity_type != "auto" else "company", id=q.lower(), identifier_type="BID")
        elif id_type == "pan":
            guess = PAN_TYPE.get(q.upper()[3], "company")
            res.update(entity_type=entity_type if entity_type != "auto" else guess, id=q.upper(), identifier_type="PAN",
                       pan_holder_hint=guess)
        elif id_type == "din":
            res.update(entity_type="director", id=q, identifier_type="DIN")
        else:
            self._resolve_by_name(q, entity_type, res)
        return res

    def _resolve_by_name(self, name: str, entity_type: str, res: Dict[str, Any]) -> None:
        if len(name) <= 3:
            raise NotFoundError("Search string must be longer than three characters")
        spec = self.reg.get("search", {}).get("companies")
        if not spec:
            raise NotFoundError("No search endpoint configured; pass a CIN/LLPIN/PAN/BID instead")
        env = self.c.data("GET", spec["path"], {"filters": json.dumps({"nameStartsWith": name}), "limit": 10, "offset": 0})
        env["path_status"] = spec.get("status")
        res["steps"].append({k: env.get(k) for k in ("url", "http_status", "ok", "error")})
        if not env.get("ok"):
            raise NotFoundError(f"Name search failed ({env.get('error')}). The search endpoint path in endpoints.json is "
                                f"unverified; look the company up on business.probe42.in and pass its CIN/LLPIN/PAN/BID.")
        data = env.get("data") or {}
        items = data.get("companies") or data.get("llps") or data.get("entities") or []
        res["candidates"] = items
        if not items:
            raise NotFoundError(f"No entity found for name '{name}'")
        norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper())
        exact = [i for i in items if norm(i.get("legal_name") or i.get("name")) == norm(name)]
        chosen = exact[0] if exact else items[0]
        if chosen.get("cin"):
            res.update(entity_type="company", id=chosen["cin"], identifier_type="CIN")
        elif chosen.get("llpin"):
            res.update(entity_type="llp", id=chosen["llpin"], identifier_type="LLPIN")
        else:
            raise NotFoundError(f"Search hit has no CIN/LLPIN: {chosen}")
        res.update(matched_name=chosen.get("legal_name") or chosen.get("name"),
                   match_quality="exact" if exact else "first_result", ambiguous=len(items) > 1 and not exact)

    # ---- blocks ----
    def fetch(self, key: str, entity: str, ident: str, id_type: str, extra: Optional[Dict[str, Any]] = None,
              group: str = "data") -> Dict[str, Any]:
        path, spec = self._path(key, group, entity, ident)
        params = {**self._id_params(id_type), **(extra or {})}
        env = self.c.report(path, params or None) if group == "reports" else self.c.data(spec["method"], path, params or None)
        env["description"] = spec.get("description")
        env["cost"] = spec.get("cost")
        return env

    def wait_for_update(self, entity: str, ident: str, id_type: str, request_id: str,
                        poll_every: float, max_wait: float) -> Dict[str, Any]:
        """Poll get-update-status until FULFILLED. Returns the last status envelope with 'polls'."""
        deadline = time.monotonic() + max_wait
        polls: List[Dict[str, Any]] = []
        last: Dict[str, Any] = {}
        while True:
            last = self.fetch("get_update_status", entity, ident, id_type, {"request_id": request_id})
            status = ((last.get("data") or {}).get("status") or "").upper()
            polls.append({"at": last["requested_at"], "http_status": last.get("http_status"), "status": status or last.get("error")})
            if status == "FULFILLED":
                break
            if self.c.dry_run or time.monotonic() >= deadline:
                break
            time.sleep(poll_every)
        last["polls"] = polls
        last["fulfilled"] = ((last.get("data") or {}).get("status") or "").upper() == "FULFILLED"
        return last

    # ---- summary ----
    @staticmethod
    def summarise(entity: str, sections: Dict[str, Any]) -> Dict[str, Any]:
        s: Dict[str, Any] = {"entity_type": entity}
        base = (sections.get("base_details") or {}).get("data") or {}
        comp = (sections.get("comprehensive_details") or {}).get("data") or {}
        core_key = {"company": "company", "llp": "llp", "pnp": "pnp"}.get(entity, "company")
        core = comp.get(core_key) or base.get(core_key) or {}
        for k in ("legal_name", "cin", "llpin", "pan", "status", "efiling_status", "active_compliance", "cirp_status",
                  "incorporation_date", "classification", "category", "sub_category", "class", "paid_up_capital",
                  "authorized_capital", "sum_of_charges", "total_obligation_of_contribution", "last_agm_date",
                  "last_filing_date", "last_annual_returns_filed_date", "last_financial_reporting_date",
                  "next_cin", "email", "website", "registered_address"):
            if core.get(k) is not None:
                s[k] = core[k]

        people = (comp.get("authorized_signatories") or comp.get("directors") or comp.get("owners")
                  or base.get("authorized_signatories") or base.get("directors") or base.get("owners") or [])
        s["active_signatories"] = [{"name": p.get("name"), "din": p.get("din"), "pan": p.get("pan"),
                                    "designation": p.get("designation"), "din_status": p.get("din_status"),
                                    "appointed": p.get("date_of_appointment")}
                                   for p in people if isinstance(p, dict) and not p.get("date_of_cessation")]
        s["signatories_total"] = len(people)

        charges = comp.get("open_charges") or base.get("open_charges") or []
        if isinstance(charges, list):
            s["open_charges_count"] = len(charges)
            s["open_charges_amount"] = sum(c.get("amount") or 0 for c in charges if isinstance(c.get("amount"), (int, float)))
            s["charge_holders"] = sorted({c.get("holder_name") for c in charges if c.get("holder_name")})

        if comp:
            s["key_indicators"] = comp.get("key_indicators")
            s["probe_financial_score"] = comp.get("probe_financial_score")
            s["filing_dates"] = comp.get("filing_dates")
            s["struck_off"] = comp.get("struckoff248_details")
            fins = comp.get("financials") or []
            if isinstance(fins, list) and fins:
                latest = max(fins, key=lambda f: str(f.get("year") or ""))
                s["latest_financials"] = {"year": latest.get("year"), "nature": latest.get("nature"),
                                          "filing_standard": latest.get("filing_standard"),
                                          "ratios": latest.get("ratios"),
                                          "pnl_subtotals": (latest.get("pnl") or {}).get("subTotals"),
                                          "bs_subtotals": (latest.get("bs") or {}).get("subTotals")}
                s["financial_years_available"] = sorted(str(f.get("year")) for f in fins)
            gst = comp.get("gst_details") or []
            if isinstance(gst, list):
                s["gst"] = {"registrations": len(gst),
                            "active": sum(1 for g in gst if str(g.get("status", "")).lower() == "active"),
                            "filing_timeliness": sorted({g.get("filing_timeliness") for g in gst if g.get("filing_timeliness")})}
            epfo = comp.get("establishments_registered_with_epfo") or []
            if isinstance(epfo, list):
                s["epfo"] = {"establishments": len(epfo),
                             "employees_latest": sum(e.get("no_of_employees") or 0 for e in epfo if isinstance(e.get("no_of_employees"), (int, float))),
                             "payment_timeliness": sorted({e.get("payment_timeliness") for e in epfo if e.get("payment_timeliness")})}
            legal = comp.get("legal_history") or []
            if isinstance(legal, list):
                s["legal"] = {"cases": len(legal),
                              "pending": sum(1 for l in legal if str(l.get("case_status", "")).lower() == "pending"),
                              "against_corporate": sum(1 for l in legal if "against" in str(l.get("case_type", "")).lower()),
                              "severe": sum(1 for l in legal if str(l.get("severity", "")).lower() in ("high", "severe"))}
            ratings = comp.get("credit_ratings") or []
            s["credit_ratings_count"] = len(ratings) if isinstance(ratings, list) else (1 if ratings else 0)
            for k in ("holding_entities", "subsidiary_entities", "associate_entities", "joint_ventures"):
                v = comp.get(k)
                if isinstance(v, dict):
                    s[k + "_count"] = sum(len(x) for x in (v.get("company"), v.get("llp"), v.get("others")) if isinstance(x, list))
            s["defaulter_list"] = comp.get("defaulter_list") or None
            s["msme_payment_delays"] = (comp.get("msme_supplier_payment_delays") or {}).get("trend")

        ds = (sections.get("data_status") or {}).get("data") or {}
        if ds.get("data_status"):
            s["data_freshness"] = ds["data_status"]
        s["sections_ok"] = [k for k, v in sections.items() if v.get("ok")]
        s["sections_failed"] = {k: v.get("error") for k, v in sections.items() if v.get("ok") is False}
        return s


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def public_ip() -> str:
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ifconfig.me/ip"):
        try:
            r = requests.get(url, timeout=10)
            if r.ok:
                return r.text.strip()
        except requests.RequestException:
            continue
    return "unknown (no outbound access to IP echo services)"


def list_endpoints(reg: Dict[str, Any], env: str) -> None:
    print(f"x-api-version: {reg['api_version']}   env: {env}")
    for group in ("data", "reports"):
        base = reg["base_urls"][group][env]
        print(f"\n[{group.upper()}]  base {base}")
        for key, spec in reg[group].items():
            if key.startswith("_"):
                continue
            flag = "" if spec.get("enabled", True) else "  (disabled)"
            for ent in spec["entities"]:
                path = spec["path"].replace("{prefix}", reg["entity_prefix"].get(ent, "")).replace("{id}", "{identifier}")
                q = "?" + "&".join(f"{p}=" for p in spec.get("params", [])) if spec.get("params") else ""
                print(f"  {spec['method']:4} {base}{path}{q}   [{key} / {ent}]{flag}")
            print(f"       {spec.get('description', '')}")
    s = reg.get("search", {}).get("companies")
    if s:
        print(f"\n[SEARCH] ({s.get('status')})  GET {reg['base_urls']['data'][env]}{s['path']}?filters={{\"nameStartsWith\":..}}&limit=&offset=")


def save_pdf(env: Dict[str, Any], dest: Path) -> None:
    blob = env.pop("_binary", None)
    if blob is None and env.get("ok") and isinstance(env.get("data"), (str, dict)):
        d = env["data"]
        b64 = d if isinstance(d, str) else (d.get("file") or d.get("base64") or d.get("content"))
        if isinstance(b64, str):
            try:
                blob = base64.b64decode(b64)
            except Exception:
                blob = None
    if blob:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        env["saved_to"] = str(dest)
        env["bytes"] = len(blob)
        env.pop("data", None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="?", help="CIN, LLPIN, PAN, BID, DIN or legal name")
    p.add_argument("--type", choices=["auto", "company", "llp", "pnp"], default="auto", help="Entity type (default: auto from identifier)")
    p.add_argument("--id-type", choices=["auto", "cin", "llpin", "pan", "bid", "din", "name"], default="auto")
    p.add_argument("--env", choices=["sandbox", "production"], default=os.getenv("PROBE42_ENV", "sandbox"))
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--api-version", help="Override x-api-version")
    p.add_argument("--base-url", help="Override DATA base URL (testing)")
    p.add_argument("--reports-url", help="Override REPORTS base URL (testing)")
    p.add_argument("--mode", choices=["base", "full"], default="full",
                   help="base = base-details + datastatus only (no credits); full = also comprehensive-details")
    p.add_argument("--auto-update", action="store_true",
                   help="If comprehensive data is not probed yet (404) POST /update and poll until FULFILLED")
    p.add_argument("--refresh", action="store_true", help="Force POST /update before fetching comprehensive data")
    p.add_argument("--max-age-days", type=int, help="POST /update if last_details_updated is older than this many days")
    p.add_argument("--poll-every", type=float, default=20.0, help="Seconds between get-update-status polls")
    p.add_argument("--max-wait", type=float, default=600.0, help="Max seconds to wait for an update to be FULFILLED")
    p.add_argument("--directors", action="store_true", help="Also fetch director details + network for active signatories")
    p.add_argument("--max-directors", type=int, default=10)
    p.add_argument("--report", action="store_true", help="Download the curated PDF report (reports API)")
    p.add_argument("--client-name", default=os.getenv("PROBE42_CLIENT_NAME", "evam"), help="client_name for the PDF report")
    p.add_argument("--unit", default="INR", help="Financial unit for the PDF report")
    p.add_argument("--inc-docs", help="Comma list of incorporation documents to download: AoA,MoA,CoI,INC20A (company) / CoI (LLP)")
    p.add_argument("--documents", type=int, default=0, metavar="N",
                   help="Download up to N MCA reference documents whose doc_id appears in comprehensive-details")
    p.add_argument("--no-datastatus", action="store_true")
    p.add_argument("-o", "--output", type=Path, help="Write JSON here (PDFs go to <output-stem>_files/)")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Print the URLs that would be called, call nothing")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--show-ip", action="store_true")
    p.add_argument("--list-endpoints", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def find_doc_ids(obj: Any, found: Optional[List[str]] = None) -> List[str]:
    found = found if found is not None else []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "doc_id" and isinstance(v, str) and v and v not in found:
                found.append(v)
            else:
                find_doc_ids(v, found)
    elif isinstance(obj, list):
        for it in obj:
            find_doc_ids(it, found)
    return found


# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if (args.verbose or args.dry_run) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    if args.show_ip:
        print(public_ip())
        print("Whitelist it at https://apiportal.probe42.in/v1/my-account/whitelisted-ips (may take up to 24h).", file=sys.stderr)
        return 0

    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    api_version = args.api_version or reg["api_version"]
    data_base = args.base_url or reg["base_urls"]["data"][args.env]
    reports_base = args.reports_url or reg["base_urls"]["reports"][args.env]

    if args.list_endpoints:
        list_endpoints(reg, args.env)
        return 0
    if not args.query:
        print("error: query required (CIN / LLPIN / PAN / BID / DIN / name), or --show-ip / --list-endpoints", file=sys.stderr)
        return 1

    try:
        client = Probe42Client(os.getenv("PROBE42_API_KEY", ""), data_base, reports_base, api_version,
                               timeout=args.timeout, dry_run=args.dry_run)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intel = CompanyIntel(client, reg)
    t0 = time.monotonic()
    files_dir = (args.output.parent / f"{args.output.stem}_files") if args.output else Path("probe42_files")
    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "source": "Probe42", "api_version": api_version,
        "env": args.env, "base_urls": {"data": data_base, "reports": reports_base}, "query": args.query,
        "dry_run": args.dry_run,
    }
    sections: Dict[str, Any] = {}
    exit_code = 0

    try:
        id_type = CompanyIntel.classify(args.query, args.id_type)
        res = intel.resolve(args.query, id_type, args.type)
        report["resolution"] = res
        if res.get("ambiguous"):
            LOG.warning("Name search ambiguous; using '%s' (%s)", res.get("matched_name"), res["id"])

        # ---------- director-only mode ----------
        if res["entity_type"] == "director":
            sections["director_details"] = intel.fetch("director_details", "director", res["id"], "cin", {"identifier_type": "DIN"})
            path, spec = intel._path("director_network", "data", "director", "")
            env = client.data("GET", path, {"din": res["id"]})
            env.update(description=spec.get("description"), cost=spec.get("cost"))
            sections["director_network"] = env
            report["entity"] = {"type": "director", "id": res["id"]}
            report["sections"] = sections
            raise StopIteration

        entity, ident, idt = res["entity_type"], res["id"], res["identifier_type"]

        # ---------- 1. base details (also resolves PAN/BID -> CIN/LLPIN, handles changed CIN) ----------
        base = intel.fetch("base_details", entity, ident, idt)
        if base.get("http_status") == 404 and idt == "PAN" and args.type == "auto":
            # PAN prefix guess was wrong: try the other entity families
            for alt in [e for e in ("company", "llp", "pnp") if e != entity]:
                alt_env = intel.fetch("base_details", alt, ident, idt)
                if alt_env.get("ok"):
                    entity, base = alt, alt_env
                    res["entity_type"] = alt
                    res["entity_type_note"] = f"PAN resolved under {alt} after {res.get('pan_holder_hint')} returned 404"
                    break
        if base.get("http_status") == 422:
            m = NEW_CIN_RE.search(json.dumps(base.get("response")))
            if m:
                res["cin_changed_to"] = m.group(1)
                ident, idt = m.group(1), "CIN"
                base = intel.fetch("base_details", entity, ident, idt)
        sections["base_details"] = base
        if base.get("http_status") == 424:
            raise PendingError(base["error"])
        if base.get("http_status") == 403:
            raise AuthError(base["error"])
        if base.get("http_status") in (404, 422):
            raise NotFoundError(base["error"])

        core = (base.get("data") or {})
        core = core.get("company") or core.get("llp") or core.get("pnp") or {}
        real_id = core.get("cin") or core.get("llpin")
        if real_id and idt in ("PAN", "BID"):
            res["resolved_id"] = real_id
            ident, idt = real_id, ("CIN" if core.get("cin") else "LLPIN")
        if core.get("next_cin"):
            res["next_cin"] = core["next_cin"]
        report["entity"] = {"type": entity, "id": ident, "identifier_type": idt, "legal_name": core.get("legal_name"),
                            "bid": (base.get("metadata") or {}).get("bid")}

        # ---------- 2. data status ----------
        needs_update = args.refresh
        if not args.no_datastatus:
            ds = intel.fetch("data_status", entity, ident, idt)
            sections["data_status"] = ds
            last_details = ((ds.get("data") or {}).get("data_status") or {}).get("last_details_updated")
            if args.max_age_days and last_details:
                try:
                    age = (datetime.now(timezone.utc).date() - datetime.strptime(last_details, "%Y-%m-%d").date()).days
                    report.setdefault("notes", []).append(f"comprehensive data is {age} days old (last_details_updated={last_details})")
                    if age > args.max_age_days:
                        needs_update = True
                except ValueError:
                    pass

        # ---------- 3. comprehensive details (+ async update workflow) ----------
        if args.mode == "full":
            def run_update() -> bool:
                upd = intel.fetch("update", entity, ident, idt)
                sections["update_request"] = upd
                rid = (upd.get("data") or {}).get("request_id")
                if not upd.get("ok") or not rid:
                    return False
                st = intel.wait_for_update(entity, ident, idt, rid, args.poll_every, args.max_wait)
                sections["update_status"] = st
                return bool(st.get("fulfilled"))

            if needs_update:
                fulfilled = run_update()
                if not fulfilled and not args.dry_run:
                    report.setdefault("notes", []).append("update not FULFILLED within --max-wait; data below may be stale")

            comp = intel.fetch("comprehensive_details", entity, ident, idt)
            msg = json.dumps(comp.get("response", "")).lower()
            if comp.get("http_status") == 404 and "not probed" in msg:
                comp["hint"] = ("Entity never probed. Re-run with --auto-update (POST /update, poll, re-fetch), "
                                "or trigger the update on the Probe42 platform.")
                if args.auto_update and not needs_update:
                    if run_update():
                        comp = intel.fetch("comprehensive_details", entity, ident, idt)
                    else:
                        exit_code = 3
                elif not args.auto_update:
                    exit_code = 3
            if comp.get("http_status") == 429:
                comp["hint"] = "Comprehensive calls consume credits; top up on the platform or use --mode base."
            sections["comprehensive_details"] = comp
            comp_data = comp.get("data") or {}

            # ---------- 4. directors ----------
            if args.directors:
                people = (comp_data.get("authorized_signatories") or comp_data.get("directors")
                          or (base.get("data") or {}).get("authorized_signatories") or (base.get("data") or {}).get("directors") or [])
                active = [p for p in people if isinstance(p, dict) and not p.get("date_of_cessation") and (p.get("din") or p.get("pan"))]
                dsec: Dict[str, Any] = {}
                for p in active[: args.max_directors]:
                    key = p.get("din") or p.get("pan")
                    kind = "DIN" if p.get("din") else "PAN"
                    det = intel.fetch("director_details", "director", key, "cin", {"identifier_type": kind})
                    path, spec = intel._path("director_network", "data", "director", "")
                    net = client.data("GET", path, {"din": p["din"]} if p.get("din") else {"pan": p["pan"]})
                    net.update(description=spec.get("description"), cost=spec.get("cost"))
                    dsec[key] = {"name": p.get("name"), "designation": p.get("designation"), "details": det, "network": net}
                sections["directors"] = {"ok": True, "count": len(dsec), "skipped": max(0, len(active) - args.max_directors),
                                         "items": dsec, "url": "(multiple, see items)"}

            # ---------- 5. PDFs ----------
            if args.report:
                env = intel.fetch("report_pdf", entity, ident, idt,
                                  {"type": "pdf", "client_name": args.client_name, "unit": args.unit}, group="reports")
                save_pdf(env, files_dir / f"{ident}_report.pdf")
                sections["report_pdf"] = env
            if args.inc_docs:
                allowed = reg.get("incorporation_document_types", {}).get(entity, [])
                docs: Dict[str, Any] = {}
                for t in [x.strip() for x in args.inc_docs.split(",") if x.strip()]:
                    if allowed and t not in allowed:
                        docs[t] = {"ok": False, "error": f"{t} not available for {entity}; allowed: {allowed}"}
                        continue
                    env = intel.fetch("incorporation_document", entity, ident, idt, {"type": t}, group="reports")
                    save_pdf(env, files_dir / f"{ident}_{t}.pdf")
                    docs[t] = env
                sections["incorporation_documents"] = {"ok": any(d.get("ok") for d in docs.values()), "items": docs, "url": "(multiple, see items)"}
            if args.documents > 0:
                ids = find_doc_ids(comp_data)[: args.documents]
                docs = {}
                for i, doc_id in enumerate(ids, 1):
                    # live API expects 'doc-id' (hyphen) even though the spec writes doc_id
                    env = intel.fetch("document_by_id", entity, ident, idt, {"doc-id": doc_id}, group="reports")
                    save_pdf(env, files_dir / f"{ident}_doc{i}.pdf")
                    docs[doc_id] = env
                sections["reference_documents"] = {"ok": any(d.get("ok") for d in docs.values()), "available_doc_ids": find_doc_ids(comp_data),
                                                   "items": docs, "url": "(multiple, see items)"}

        report["summary"] = CompanyIntel.summarise(entity, sections)
        report["sections"] = sections

    except StopIteration:
        pass
    except AuthError as exc:
        report["error"] = str(exc); exit_code = 2
    except PendingError as exc:
        report["error"] = str(exc); exit_code = 3
    except NotFoundError as exc:
        report["error"] = str(exc); exit_code = 1
    except Probe42Error as exc:
        report["error"] = str(exc); exit_code = 1
    finally:
        report.setdefault("sections", sections)
        for sec in report["sections"].values():
            if isinstance(sec, dict):
                sec.pop("_binary", None)
        report["calls"] = client.calls
        report["stats"] = {"total_calls": len(client.calls), "ok": sum(1 for c in client.calls if c.get("ok")),
                           "failed": sum(1 for c in client.calls if c.get("ok") is False),
                           "elapsed_ms": round((time.monotonic() - t0) * 1000)}
        if client.calls and all(c.get("http_status") == 403 and "does not exist" not in str(c.get("error")) for c in client.calls):
            report["error"] = ("All calls returned 403: check PROBE42_API_KEY and that this server's public IP "
                               "(--show-ip) is whitelisted on apiportal.probe42.in.")
            exit_code = 2

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
