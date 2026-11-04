#!/usr/bin/env python3
"""
Probe42 Company Intelligence fetcher.

Given a company name, CIN, LLPIN, PAN or GSTIN, resolves the entity and calls every
Probe42 endpoint listed in endpoints.json, returning ONE comprehensive JSON document.
Every section records the exact URL that was called, the HTTP status and timing, so
you can see precisely which Probe42 API produced which block of data.

Usage:
    export PROBE42_API_KEY="your-key"
    python3 probe42_company_intel.py "U24239DL2002PTC114413"
    python3 probe42_company_intel.py "Infosys Limited" --env production -o infosys.json
    python3 probe42_company_intel.py "AAA-1234" --type llp
    python3 probe42_company_intel.py --show-ip          # public IP to whitelist on Probe42
    python3 probe42_company_intel.py --list-endpoints   # print the URL catalogue

Exit codes: 0 success, 1 entity not found / bad input, 2 auth or IP-allowlist failure.
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
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("probe42")

HERE = Path(__file__).resolve().parent
DEFAULT_REGISTRY = HERE / "endpoints.json"

# Identifier patterns (MCA formats)
CIN_RE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$", re.I)
LLPIN_RE = re.compile(r"^[A-Z]{3}-?\d{4}$", re.I)
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$", re.I)
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", re.I)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class Probe42Error(Exception):
    pass


class AuthError(Probe42Error):
    """403: bad key, or calling IP is not on the Probe42 allowlist."""


class NotFoundError(Probe42Error):
    pass


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class Probe42Client:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        api_version: str = "1.3",
        timeout: float = 30.0,
        max_retries: int = 3,
        min_interval: float = 0.25,
    ):
        if not api_key:
            raise AuthError("PROBE42_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_call = 0.0
        self.calls: List[Dict[str, Any]] = []  # audit trail of every HTTP call

        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": api_key,
                "x-api-version": api_version,
                "Accept": "application/json",
                "User-Agent": "probe42-company-intel/1.0",
            }
        )
        retry = Retry(
            total=max_retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    # ---- low level ---------------------------------------------------------
    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET base_url+path. Returns an envelope with url, status, elapsed_ms, data|error."""
        url = f"{self.base_url}{path}"
        full_url = f"{url}?{urlencode(params)}" if params else url

        # simple client-side throttle to stay under Probe42 rate limits
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

        started = time.monotonic()
        envelope: Dict[str, Any] = {
            "url": full_url,
            "method": "GET",
            "api_version": self.api_version,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            envelope["http_status"] = resp.status_code
            try:
                body: Any = resp.json()
            except ValueError:
                body = {"raw": resp.text[:2000]}

            if resp.status_code == 200:
                envelope["ok"] = True
                envelope["data"] = body.get("data", body) if isinstance(body, dict) else body
                if isinstance(body, dict) and "metadata" in body:
                    envelope["metadata"] = body["metadata"]
            elif resp.status_code == 403:
                envelope["ok"] = False
                envelope["error"] = (
                    "403 Forbidden: API key invalid, or this server's IP is not on the "
                    "Probe42 allowlist (apiportal.probe42.in > My Account > Whitelisted IPs)."
                )
                envelope["response"] = body
            elif resp.status_code == 404:
                envelope["ok"] = False
                envelope["error"] = "404 Not found: entity has no data here, or this endpoint path is not enabled for your plan."
                envelope["response"] = body
            else:
                envelope["ok"] = False
                envelope["error"] = f"HTTP {resp.status_code}"
                envelope["response"] = body
        except requests.RequestException as exc:
            envelope["ok"] = False
            envelope["http_status"] = None
            envelope["error"] = f"network error: {exc}"
        finally:
            self._last_call = time.monotonic()
            envelope["elapsed_ms"] = round((time.monotonic() - started) * 1000)

        self.calls.append(
            {k: envelope.get(k) for k in ("url", "http_status", "ok", "elapsed_ms", "error")}
        )
        LOG.info("%s -> %s (%s ms)", full_url, envelope.get("http_status"), envelope["elapsed_ms"])
        return envelope


# --------------------------------------------------------------------------- #
# Intelligence orchestrator
# --------------------------------------------------------------------------- #
class CompanyIntel:
    def __init__(self, client: Probe42Client, registry: Dict[str, Any]):
        self.client = client
        self.registry = registry

    # ---- identifier handling ----------------------------------------------
    @staticmethod
    def classify(query: str, forced: Optional[str] = None) -> str:
        if forced and forced != "auto":
            return forced
        q = query.strip().upper()
        if CIN_RE.match(q):
            return "cin"
        if LLPIN_RE.match(q):
            return "llpin"
        if GSTIN_RE.match(q):
            return "gstin"
        if PAN_RE.match(q):
            return "pan"
        return "name"

    # ---- search ------------------------------------------------------------
    def search(self, kind: str, filters: Dict[str, Any], limit: int = 10) -> Dict[str, Any]:
        spec = self.registry["search"][kind]
        return self.client.get(spec["path"], {"filters": json.dumps(filters), "limit": limit, "offset": 0})

    def resolve(self, query: str, id_type: str, entity_type: str) -> Dict[str, Any]:
        """
        Returns {"entity_type": company|llp, "id": CIN/LLPIN, "identifier_type": ..., "resolution": {...}}
        """
        q = query.strip()
        res: Dict[str, Any] = {"input": query, "detected_type": id_type, "steps": []}

        if id_type == "cin":
            res.update(entity_type="company", id=q.upper(), identifier_type="CIN")
            return res
        if id_type == "llpin":
            llpin = q.upper()
            if "-" not in llpin:
                llpin = f"{llpin[:3]}-{llpin[3:]}"
            res.update(entity_type="llp", id=llpin, identifier_type="LLPIN")
            return res
        if id_type in ("pan", "gstin"):
            # Probe42 base-details accepts identifier_type=PAN / GSTIN and returns the CIN.
            res.update(entity_type=entity_type if entity_type != "auto" else "company",
                       id=q.upper(), identifier_type=id_type.upper())
            return res

        # ---- name search ----
        candidates: List[Dict[str, Any]] = []
        kinds = ["companies", "llps"] if entity_type == "auto" else (["companies"] if entity_type == "company" else ["llps"])
        for kind in kinds:
            if kind not in self.registry["search"]:
                continue
            env = self.search(kind, {"nameStartsWith": q})
            res["steps"].append({k: env.get(k) for k in ("url", "http_status", "ok", "error")})
            if not env.get("ok"):
                continue
            data = env["data"] or {}
            items = data.get("companies") or data.get("llps") or data.get("entities") or []
            for it in items:
                it = dict(it)
                it["_kind"] = "company" if kind == "companies" else "llp"
                candidates.append(it)

        res["candidates"] = candidates
        if not candidates:
            raise NotFoundError(f"No company or LLP found for name '{q}'")

        # Prefer exact legal-name match, else the first result.
        def norm(s: str) -> str:
            return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

        exact = [c for c in candidates if norm(c.get("legal_name") or c.get("name")) == norm(q)]
        chosen = exact[0] if exact else candidates[0]
        ident = chosen.get("cin") or chosen.get("llpin") or chosen.get("id")
        if not ident:
            raise NotFoundError(f"Search result for '{q}' did not carry a CIN/LLPIN: {chosen}")
        res.update(
            entity_type=chosen["_kind"],
            id=ident,
            identifier_type="CIN" if chosen["_kind"] == "company" else "LLPIN",
            matched_name=chosen.get("legal_name") or chosen.get("name"),
            match_quality="exact" if exact else "first_result",
            ambiguous=len(candidates) > 1 and not exact,
        )
        return res

    # ---- fetch everything --------------------------------------------------
    def fetch_all(self, entity_type: str, ident: str, identifier_type: str,
                  only: Optional[List[str]] = None, skip: Optional[List[str]] = None) -> Dict[str, Any]:
        sections: Dict[str, Any] = {}
        for spec in self.registry[entity_type]:
            key = spec["key"]
            if spec.get("enabled", True) is False:
                continue
            if only and key not in only:
                continue
            if skip and key in skip:
                continue
            path = spec["path"].replace("{id}", ident)
            params = dict(spec.get("params") or {})
            if "identifier_type" in params:
                params["identifier_type"] = identifier_type
            env = self.client.get(path, params or None)
            env["description"] = spec.get("description")
            env["path_status"] = spec.get("status")  # confirmed | expected
            sections[key] = env
        return sections

    # ---- derived summary ---------------------------------------------------
    @staticmethod
    def summarise(entity_type: str, sections: Dict[str, Any]) -> Dict[str, Any]:
        """Pull the headline facts into one flat block; tolerant of missing sections."""
        s: Dict[str, Any] = {"entity_type": entity_type}
        base = (sections.get("base_details") or {}).get("data") or {}
        comp = base.get("company") or base.get("llp") or base
        for k in ("legal_name", "cin", "llpin", "status", "efiling_status", "incorporation_date",
                  "classification", "paid_up_capital", "authorized_capital", "registered_address",
                  "pan", "last_agm_date", "last_filing_date"):
            if k in comp:
                s[k] = comp[k]

        sig = (sections.get("authorized_signatories") or {}).get("data") or {}
        people = sig.get("authorized_signatories") or sig.get("directors") or []
        s["active_signatories"] = [
            {"name": p.get("name"), "din": p.get("din"), "designation": p.get("designation"),
             "appointed": p.get("date_of_appointment")}
            for p in people if not p.get("date_of_cessation")
        ]
        s["signatory_count_total"] = len(people)

        ch = (sections.get("charges") or {}).get("data") or {}
        charges = ch.get("charges") or []
        s["charges_count"] = len(charges)
        s["charges_total_amount"] = sum((c.get("amount") or 0) for c in charges if isinstance(c.get("amount"), (int, float)))
        s["charge_holders"] = sorted({c.get("holder_name") for c in charges if c.get("holder_name")})

        fin = (sections.get("financials") or {}).get("data") or {}
        years = fin.get("financials") or []
        if years:
            latest = max(years, key=lambda y: str(y.get("year") or ""))
            s["latest_financial_year"] = latest.get("year")
            s["latest_revenue"] = latest.get("revenue")
            s["latest_net_income"] = latest.get("net_income")
            s["latest_total_assets"] = latest.get("total_assets")
            s["latest_total_borrowings"] = (latest.get("long_term_borrowings") or 0) + (latest.get("short_term_borrowings") or 0)
            s["financial_years_available"] = sorted(str(y.get("year")) for y in years)

        s["sections_ok"] = [k for k, v in sections.items() if v.get("ok")]
        s["sections_failed"] = {k: v.get("error") for k, v in sections.items() if not v.get("ok")}
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


def load_registry(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def list_endpoints(registry: Dict[str, Any], base_url: str) -> None:
    print(f"Base URL: {base_url}   (x-api-version: {registry.get('api_version')})\n")
    print("SEARCH")
    for k, v in registry["search"].items():
        print(f"  [{v.get('status','?'):9}] {k:24} GET {base_url}{v['path']}?filters=...&limit=&offset=")
    for et in ("company", "llp"):
        print(f"\n{et.upper()}  ({{id}} = CIN or LLPIN)")
        for spec in registry[et]:
            flag = "" if spec.get("enabled", True) else " (disabled)"
            print(f"  [{spec.get('status','?'):9}] {spec['key']:24} GET {base_url}{spec['path']}{flag}")
            if spec.get("description"):
                print(f"{'':38}{spec['description']}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="?", help="Company name, CIN, LLPIN, PAN or GSTIN")
    p.add_argument("--type", choices=["auto", "company", "llp"], default="auto", help="Entity type (default auto)")
    p.add_argument("--id-type", choices=["auto", "cin", "llpin", "pan", "gstin", "name"], default="auto",
                   help="Force how the query is interpreted (default auto-detect)")
    p.add_argument("--env", choices=["sandbox", "production", "lite"], default=os.getenv("PROBE42_ENV", "sandbox"),
                   help="Which Probe42 base URL to use (default sandbox, or $PROBE42_ENV)")
    p.add_argument("--base-url", help="Override base URL entirely")
    p.add_argument("--api-version", help="Override x-api-version header (default from endpoints.json)")
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY, help="Path to endpoints.json")
    p.add_argument("--only", help="Comma-separated section keys to fetch (e.g. base_details,charges)")
    p.add_argument("--skip", help="Comma-separated section keys to skip")
    p.add_argument("-o", "--output", type=Path, help="Write JSON here (default: stdout)")
    p.add_argument("--pretty", action="store_true", default=True)
    p.add_argument("--compact", action="store_true", help="Compact JSON output")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--show-ip", action="store_true", help="Print this server's public IP (for Probe42 whitelisting) and exit")
    p.add_argument("--list-endpoints", action="store_true", help="Print the endpoint catalogue and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    if args.show_ip:
        ip = public_ip()
        print(ip)
        print("Add this IP at https://apiportal.probe42.in/v1/my-account/whitelisted-ips "
              "(changes can take up to 24 hours to apply).", file=sys.stderr)
        return 0

    registry = load_registry(args.registry)
    base_url = args.base_url or registry["base_urls"][args.env]
    api_version = args.api_version or registry.get("api_version", "1.3")

    if args.list_endpoints:
        list_endpoints(registry, base_url)
        return 0

    if not args.query:
        print("error: a company name / CIN / LLPIN / PAN / GSTIN is required (or use --show-ip / --list-endpoints)", file=sys.stderr)
        return 1

    api_key = os.getenv("PROBE42_API_KEY", "")
    try:
        client = Probe42Client(api_key, base_url, api_version=api_version, timeout=args.timeout)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intel = CompanyIntel(client, registry)
    started = time.monotonic()
    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Probe42",
        "base_url": base_url,
        "api_version": api_version,
        "query": args.query,
    }

    try:
        id_type = CompanyIntel.classify(args.query, args.id_type)
        resolution = intel.resolve(args.query, id_type, args.type)
        report["resolution"] = resolution

        if resolution.get("ambiguous"):
            LOG.warning("Name search was ambiguous; picked '%s' (%s). Pass the CIN to be exact.",
                        resolution.get("matched_name"), resolution["id"])

        only = [s.strip() for s in args.only.split(",")] if args.only else None
        skip = [s.strip() for s in args.skip.split(",")] if args.skip else None
        if resolution["identifier_type"] in ("PAN", "GSTIN"):
            # Base details accepts PAN/GSTIN via identifier_type and reveals the CIN/LLPIN;
            # every other endpoint needs the CIN, so resolve first, then fetch the rest.
            sections = intel.fetch_all(resolution["entity_type"], resolution["id"], resolution["identifier_type"],
                                       only=["base_details"])
            base = (sections.get("base_details") or {}).get("data") or {}
            comp = base.get("company") or base.get("llp") or base
            real_id = comp.get("cin") or comp.get("llpin")
            if not real_id:
                raise NotFoundError(f"Probe42 did not resolve {resolution['identifier_type']} {resolution['id']} to a CIN/LLPIN "
                                    f"({sections['base_details'].get('error') or 'no cin in response'})")
            resolution["resolved_id"] = real_id
            LOG.info("Resolved %s %s -> %s", resolution["identifier_type"], resolution["id"], real_id)
            rest = intel.fetch_all(resolution["entity_type"], real_id, "CIN" if comp.get("cin") else "LLPIN",
                                   only, (skip or []) + ["base_details"])
            sections.update(rest)
        else:
            sections = intel.fetch_all(resolution["entity_type"], resolution["id"], resolution["identifier_type"], only, skip)

        report["entity"] = {"type": resolution["entity_type"], "id": resolution.get("resolved_id") or resolution["id"]}
        report["summary"] = CompanyIntel.summarise(resolution["entity_type"], sections)
        report["sections"] = sections
        exit_code = 0
    except AuthError as exc:
        report["error"] = str(exc)
        exit_code = 2
    except NotFoundError as exc:
        report["error"] = str(exc)
        exit_code = 1
    finally:
        report["calls"] = client.calls
        report["stats"] = {
            "total_calls": len(client.calls),
            "ok": sum(1 for c in client.calls if c.get("ok")),
            "failed": sum(1 for c in client.calls if not c.get("ok")),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        # A 403 on every call means the key or the IP allowlist is the problem.
        if client.calls and all(c.get("http_status") == 403 for c in client.calls):
            report["error"] = ("All calls returned 403. Check PROBE42_API_KEY and that this server's public IP "
                               "(run with --show-ip) is whitelisted on apiportal.probe42.in.")
            exit_code = 2

    text = json.dumps(report, indent=None if args.compact else 2, ensure_ascii=False, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({report['stats']['ok']}/{report['stats']['total_calls']} calls ok)", file=sys.stderr)
    else:
        print(text)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
