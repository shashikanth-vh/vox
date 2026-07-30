"""
core.search — query interactions by company / user / date (step 5).

Pure query layer over the ATLAS register. Every interaction's refId is resolved
to its company (client code or lead id) so results carry a human name and the
owning RM, and so a company filter can match the same way the resolver does
(normName containment), not just on a raw code.

Filters (all optional, AND-combined):
  company     name substring (normName containment) OR exact entity code
  user        interaction.person OR the entity's RM (first-name tolerant)
  date_from   occurredAt >= (YYYY-MM-DD)
  date_to     occurredAt <= (YYYY-MM-DD)
  itype       interactionType exact
  ref_type    Lead / Deal / Syndication
  q           free text over notes / nextAction / person / company

Returns enriched, sorted, paginated rows plus facet counts for the panel.
"""

from __future__ import annotations

from typing import Any

from app.vocx.core.atlas import AtlasStore
from app.vocx.core.resolve import norm_name


class InteractionSearch:
    def __init__(self, store: AtlasStore, config: dict[str, Any] | None = None):
        self.store = store
        self.config = config or {}
        self._index = self._build_index()

    def _build_index(self) -> dict[str, dict[str, Any]]:
        idx: dict[str, dict[str, Any]] = {}
        for code, c in self.store.clients.items():
            idx[code] = {"code": code, "name": c.get("name") or code,
                         "rm": self.store.rm_for_client(code), "kind": "client"}
        for l in self.store.leads:
            lid = l.get("id")
            if lid:
                idx[lid] = {"code": lid, "name": l.get("company") or lid,
                            "rm": (l.get("rm") or "").strip(), "kind": "lead"}
        return idx

    def _enrich(self, i: dict[str, Any]) -> dict[str, Any]:
        info = self._index.get(i.get("refId"), {})
        return {
            "interactionId": i.get("interactionId"),
            "occurredAt": i.get("occurredAt"),
            "loggedAt": i.get("loggedAt"),
            "refId": i.get("refId"),
            "refType": i.get("refType"),
            "company": info.get("name") or i.get("refId") or "—",
            "code": info.get("code"),
            "kind": info.get("kind"),
            "rm": info.get("rm", ""),
            "person": i.get("person") or "",
            "interactionType": i.get("interactionType") or "",
            "direction": i.get("direction"),
            "notes": i.get("notes") or "",
            "nextAction": i.get("nextAction"),
            "nextActionDate": i.get("nextActionDate"),
            "flagged": bool(i.get("_voxFlags")),
        }

    # ---- filtering ---------------------------------------------------------
    def _matches(self, row, company, user, date_from, date_to, itype, ref_type, q) -> bool:
        if company:
            cq = norm_name(company)
            name_n = norm_name(row["company"])
            code_match = (row.get("code") or "").lower() == company.strip().lower()
            if not (code_match or (cq and cq in name_n)):
                return False
        if user and not _user_match(user, row["person"], row["rm"]):
            return False
        occ = row.get("occurredAt") or ""
        if date_from and occ < date_from:
            return False
        if date_to and occ > date_to:
            return False
        if itype and row["interactionType"] != itype:
            return False
        if ref_type and (row.get("refType") or "") != ref_type:
            return False
        if q:
            hay = " ".join([row["notes"], row.get("nextAction") or "", row["person"],
                            row["company"]]).lower()
            if q.strip().lower() not in hay:
                return False
        return True

    def _filtered(self, **f) -> list[dict[str, Any]]:
        rows = [self._enrich(i) for i in self.store.interactions]
        return [r for r in rows if self._matches(
            r, f.get("company"), f.get("user"), f.get("date_from"), f.get("date_to"),
            f.get("itype"), f.get("ref_type"), f.get("q"))]

    def search(self, company=None, user=None, date_from=None, date_to=None, itype=None,
               ref_type=None, q=None, limit=50, offset=0, sort="desc") -> dict[str, Any]:
        rows = self._filtered(company=company, user=user, date_from=date_from,
                              date_to=date_to, itype=itype, ref_type=ref_type, q=q)
        rows.sort(key=lambda r: (r.get("occurredAt") or "", r.get("loggedAt") or ""),
                  reverse=(sort != "asc"))
        total = len(rows)
        limit = max(0, int(limit)) if limit is not None else total
        offset = max(0, int(offset or 0))
        page = rows[offset:offset + limit] if limit else rows[offset:]
        return {"total": total, "count": len(page), "offset": offset,
                "limit": limit, "results": page}

    def facets(self, **f) -> dict[str, Any]:
        rows = self._filtered(**f)
        companies: dict[str, dict[str, Any]] = {}
        users: dict[str, int] = {}
        by_date: dict[str, int] = {}
        types: dict[str, int] = {}
        for r in rows:
            key = r.get("code") or r["company"]
            c = companies.setdefault(key, {"code": r.get("code"), "name": r["company"], "count": 0})
            c["count"] += 1
            for u in {r["person"], r["rm"]}:
                if u:
                    users[u] = users.get(u, 0) + 1
            if r.get("occurredAt"):
                by_date[r["occurredAt"]] = by_date.get(r["occurredAt"], 0) + 1
            if r["interactionType"]:
                types[r["interactionType"]] = types.get(r["interactionType"], 0) + 1
        return {
            "total": len(rows),
            "companies": sorted(companies.values(), key=lambda x: -x["count"])[:50],
            "users": [{"name": k, "count": v} for k, v in sorted(users.items(), key=lambda x: -x[1])],
            "byDate": [{"date": k, "count": by_date[k]} for k in sorted(by_date)],
            "types": [{"type": k, "count": v} for k, v in sorted(types.items(), key=lambda x: -x[1])],
        }

    def entity(self, code: str) -> dict[str, Any]:
        """One entity's summary + all its interactions (for the detail drawer)."""
        info = self._index.get(code, {"code": code, "name": code, "rm": "", "kind": "unknown"})
        rows = self.search(company=None, limit=None)  # all
        mine = [r for r in rows["results"] if r.get("refId") == code]
        return {"entity": info, "count": len(mine), "interactions": mine,
                "aliases": self.store.data.get("voxAliases", {}).get(code, [])}


def _user_match(query: str, person: str, rm: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    for cand in (person, rm):
        c = (cand or "").strip().lower()
        if not c:
            continue
        if q == c or q in c or c.split()[0] == q.split()[0]:
            return True
    return False
