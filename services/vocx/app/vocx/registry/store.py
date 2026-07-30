"""register_store.py — the PRISM Register as the VOX resolution corpus.

The PoC resolved against an ATLAS JSON export (`fixtures/atlas_data.json`). In PRISM the
system of record is the Register service, so this module builds the SAME blob shape the
vendored pipeline expects — `clients{code→…} / leads[] / deals[] / interactions[]` — by
reading the Register API, and returns it as an `AtlasStore`. Nothing downstream (resolver,
gate, search, note) knows the difference.

Field mapping (Register → ATLAS blob):

    Entity   code, display_name|legal_name, sector, lens, state      → clients{code}
    Lead     id(uuid), lead_no, company, sector, lens, rm, status,
             contact, phone, next_action(_date), notes               → leads[]  (id = UUID —
             the writer needs the real row id; lead_no carries LD-V numbering)
    Deal     code, rm, entity_id→entity.code                         → deals[]  (rm-by-code)
    Interaction  subject_type/id, interaction_type, occurred_at,
             summary/notes, contact_name, performed_by               → interactions[]

Reads use VocX's own service key (`svc_vox`) directly against the Register — the same
credential the existing touchpoint path uses. Pagination is capped like ATLAS's reader;
the blob is cached for a short TTL so a burst of captures doesn't hammer the Register.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from app.vocx.core.atlas import AtlasStore


class RegisterAtlasStore(AtlasStore):
    """AtlasStore whose lead ids are REGISTER UUIDs: the LD-V numbering the PoC minted
    into `id` lives in `lead_no` here, so the minting scan must read that field."""

    def next_vox_lead_id(self, prefix: str = "LD-V", pad: int = 2) -> str:
        n = 0
        for lead in self.leads:
            no = str(lead.get("lead_no") or lead.get("id") or "")
            if no.startswith(prefix) and no[len(prefix):].isdigit():
                n = max(n, int(no[len(prefix):]))
        return f"{prefix}{str(n + 1).zfill(pad)}"


_MAX_PAGES = 10
_PAGE = 200


class RegisterStoreLoader:
    """Builds (and TTL-caches) AtlasStore views over the Register.

    Two views, cached independently, because their costs differ by an order of
    magnitude: the RESOLUTION corpus (entities + active leads + deals + ref) is a
    handful of list calls and is what every capture needs; the SEARCH view adds the
    per-subject interaction log (many calls) and only the search/facets/entity
    endpoints need it. Splitting them keeps capture latency flat as the book grows.
    """

    def __init__(self, base_url: str, api_key: str, tenant: str,
                 ttl_s: int = 30, timeout_s: float = 10.0,
                 search_ttl_s: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant = tenant
        self.ttl_s = ttl_s
        self.search_ttl_s = search_ttl_s if search_ttl_s is not None else max(ttl_s, 60)
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cached: AtlasStore | None = None
        self._fetched_at = 0.0
        self._search_cached: AtlasStore | None = None
        self._search_fetched_at = 0.0

    # ---- public ------------------------------------------------------------
    def store(self, force: bool = False) -> AtlasStore:
        """The resolution corpus — no interaction log (cheap, per-capture)."""
        with self._lock:
            if (not force and self._cached is not None
                    and time.monotonic() - self._fetched_at < self.ttl_s):
                return self._cached
            self._cached = RegisterAtlasStore(self._build_blob(with_interactions=False))
            self._fetched_at = time.monotonic()
            return self._cached

    def search_store(self, force: bool = False) -> AtlasStore:
        """The search view — corpus + interaction log (heavier, own TTL)."""
        with self._lock:
            if (not force and self._search_cached is not None
                    and time.monotonic() - self._search_fetched_at < self.search_ttl_s):
                return self._search_cached
            self._search_cached = RegisterAtlasStore(self._build_blob(with_interactions=True))
            self._search_fetched_at = time.monotonic()
            return self._search_cached

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
            self._search_cached = None

    # ---- fetch -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "X-Tenant": self.tenant,
                "X-Actor": "vocx-vox"}

    def _get(self, client: httpx.Client, url: str,
             params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with bounded retry — reads are safe to repeat, and one transient
        Register hiccup must not fail a capture."""
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = client.get(url, params=params, headers=self._headers())
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request,
                                                response=r)
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                time.sleep(0.3 * (2 ** attempt))
        raise RuntimeError(f"Register unreachable after retries: {url} ({last})")

    def _list_all(self, client: httpx.Client, resource: str,
                  params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            q = {"limit": _PAGE, **(params or {})}
            if cursor:
                q["cursor"] = cursor
            r = self._get(client, f"{self.base_url}/v1/{resource}", q)
            r.raise_for_status()
            page = r.json()
            items.extend(page.get("items", []))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return items

    def _build_blob(self, with_interactions: bool = False) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout_s) as client:
            entities = self._list_all(client, "entities")
            leads = self._list_all(client, "leads", {"status": "Active"})
            deals = self._list_all(client, "deals")
            ref = {}
            try:
                r = self._get(client, f"{self.base_url}/v1/ref")
                r.raise_for_status()
                ref = r.json()
            except (httpx.HTTPError, RuntimeError):
                pass
            interactions = (self._interactions(client, leads, deals)
                            if with_interactions else [])

        code_by_entity_id = {e["id"]: e.get("code") for e in entities}
        clients = {e["code"]: {
            "name": e.get("display_name") or e.get("legal_name") or e.get("code"),
            "sector": e.get("sector") or "", "lens": e.get("lens") or "",
            "state": e.get("state") or "", "notes": e.get("notes") or "",
            "_register_entity_id": e["id"],
        } for e in entities if e.get("code")}

        blob_leads = [{
            "id": ld["id"],                      # the REGISTER row id — what writes target
            "lead_no": ld.get("lead_no") or "",
            "company": ld.get("company") or "", "sector": ld.get("sector") or "",
            "lens": ld.get("lens") or "", "rm": ld.get("rm") or "",
            "status": ld.get("status") or "", "temp": ld.get("temperature") or "",
            "contact": ld.get("contact") or "", "phone": ld.get("phone") or "",
            "next": ld.get("next_action") or "", "nextDate": ld.get("next_action_date") or "",
            "notes": ld.get("notes") or "",
            "_register_entity_id": ld.get("entity_id"),
        } for ld in leads]

        blob_deals = [{
            "code": code_by_entity_id.get(d.get("entity_id")) or d.get("code") or "",
            "rm": d.get("rm") or "",
        } for d in deals]

        itypes = []
        for group in ("interaction_types", "interactionTypes"):
            vals = ref.get(group)
            if isinstance(vals, list) and vals:
                itypes = [v.get("value", v) if isinstance(v, dict) else v for v in vals]
                break

        return {"clients": clients, "leads": blob_leads, "deals": blob_deals,
                "lending": [], "interactions": interactions,
                "interactionTypes": itypes, "ref": ref}

    def _interactions(self, client: httpx.Client, leads: list[dict[str, Any]],
                      deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Recent interactions for the search panel — best-effort, never fatal."""
        out: list[dict[str, Any]] = []
        subjects = ([("leads", ld["id"], "Lead") for ld in leads[:50]]
                    + [("deals", d["id"], "Deal") for d in deals[:50] if d.get("id")])
        for resource, sid, ref_type in subjects:
            try:
                r = client.get(f"{self.base_url}/v1/{resource}/{sid}/interactions",
                               params={"limit": 50}, headers=self._headers())
                if r.status_code != 200:
                    continue
                body = r.json()
                rows = body.get("items", body) if isinstance(body, dict) else body
                for i in rows or []:
                    out.append({
                        "interactionId": i.get("id"),
                        "refId": sid, "refType": ref_type,
                        "occurredAt": i.get("occurred_at") or "",
                        "loggedAt": i.get("created_at") or "",
                        "person": i.get("contact_name") or "",
                        "user": i.get("performed_by") or "",
                        "interactionType": i.get("interaction_type") or "",
                        "notes": i.get("summary") or i.get("notes") or "",
                        "nextAction": i.get("next_action") or "",
                        "nextActionDate": i.get("next_action_date") or "",
                    })
            except httpx.HTTPError:
                continue
        out.sort(key=lambda i: i.get("occurredAt") or "", reverse=True)
        return out
