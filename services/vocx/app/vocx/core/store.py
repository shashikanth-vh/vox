"""
core.store — the SHARED ATLAS register writer.

Register/interaction writes hit the shared ATLAS store (not per-user Google
Drive). ATLAS itself persists the whole `S` object as one JSON blob; server-side
that blob lives in a file (atlas_serve.py's store). MutableAtlasStore loads it,
applies VOX writes, and saves atomically.

VOX-owned additions to the blob (namespaced so they never collide with ATLAS):
  S.voxAliases   {code -> [aliases]}          learned spoken forms
  S.voxFolders   {code -> {team_drive_folder_id}}   shared team folder per entity
  entity.drive_folder_id / team_drive_folder_id are ALSO written onto the entity
  record itself (per spec) when known.

All writes are keyed by entity CODE / lead id, so spelling variants never create
duplicates.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from app.vocx.core.atlas import AtlasStore


class MutableAtlasStore(AtlasStore):
    """AtlasStore that can persist mutations back to the shared register file."""

    def __init__(self, data: dict[str, Any], path: str | None = None):
        super().__init__(data)
        self.path = path
        # AtlasStore's `... or []` yields a fresh list when the source is empty,
        # which detaches it from self.data. Rebind so mutations to the instance
        # lists are what save() actually serialises.
        self.data["interactions"] = self.interactions
        self.data["leads"] = self.leads
        self.data.setdefault("voxAliases", {})
        self.data.setdefault("voxFolders", {})

    @classmethod
    def from_file(cls, path: str) -> MutableAtlasStore:
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), path=path)

    @classmethod
    def from_entities(cls, entities: dict[str, Any]) -> MutableAtlasStore:
        """An in-memory, non-persisting store built from the register the ATLAS
        browser posts (its live S) — so resolution and LD-V## minting reflect the
        exact register the RM is looking at. path=None => save() is a no-op; the
        ATLAS client is the one that persists the returned records into S."""
        from app.vocx.core.atlas import DEFAULT_INTERACTION_TYPES
        entities = entities or {}
        data = {
            "clients": entities.get("clients", {}) or {},
            "leads": entities.get("leads", []) or [],
            "deals": entities.get("deals", []) or [],
            "lending": entities.get("lending", []) or [],
            "interactions": [],
            "interactionTypes": entities.get("interactionTypes") or DEFAULT_INTERACTION_TYPES,
            "voxAliases": entities.get("voxAliases", {}) or {},
            "ref": entities.get("ref", {}) or {},
        }
        return cls(data, path=None)

    def save(self) -> None:
        """Atomic write (temp file + rename) so a crash never truncates the blob."""
        if not self.path:
            return
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


class AtlasWriter:
    """Applies register ops to a MutableAtlasStore."""

    def __init__(self, store: MutableAtlasStore, config: dict[str, Any] | None = None):
        self.store = store
        self.config = config or {}

    # ---- interactions ------------------------------------------------------
    def append_interaction(self, record: dict[str, Any]) -> dict[str, Any]:
        self.store.interactions.append(record)
        self.store.save()
        return {"interactionId": record.get("interactionId"), "refId": record.get("refId")}

    def flag_interaction_retry(self, interaction_id: str, reason: str, op: dict[str, Any]) -> None:
        """A shared-Drive copy failed -> flag the interaction and queue a retry."""
        for rec in self.store.interactions:
            if rec.get("interactionId") == interaction_id:
                rec.setdefault("_voxFlags", []).append({"reason": reason})
                break
        queue = self.store.data.setdefault("voxRetryQueue", [])
        queue.append({"interactionId": interaction_id, "reason": reason, "op": op})
        self.store.save()

    # ---- leads -------------------------------------------------------------
    def create_lead(self, record: dict[str, Any]) -> dict[str, Any]:
        # Guard against a duplicate id if the same capture is replayed.
        if not any(l.get("id") == record.get("id") for l in self.store.leads):
            self.store.leads.insert(0, record)   # ATLAS unshift()s new leads
            self.store.save()
        return {"id": record.get("id"), "company": record.get("company")}

    # ---- alias write-back --------------------------------------------------
    def write_aliases(self, code: str, aliases: list[str]) -> dict[str, Any]:
        amap = self.store.data.setdefault("voxAliases", {})
        cur = set(amap.get(code, []))
        cur.update(a for a in aliases if a)
        amap[code] = sorted(cur)
        self.store.save()
        return {"code": code, "aliases": amap[code]}

    # ---- drive folder ids (dedupe by code, not spoken name) ----------------
    def get_folder_id(self, code: str, field: str) -> str | None:
        if not code:
            return None
        ent = self._entity(code)
        if ent and ent.get(field):
            return ent[field]
        return self.store.data.get("voxFolders", {}).get(code, {}).get(field)

    def set_folder_id(self, code: str, field: str, folder_id: str) -> None:
        if not code or not folder_id:
            return
        ent = self._entity(code)
        if ent is not None:
            ent[field] = folder_id
        self.store.data.setdefault("voxFolders", {}).setdefault(code, {})[field] = folder_id
        self.store.save()

    def _entity(self, code: str) -> dict[str, Any] | None:
        if code in self.store.clients:
            return self.store.clients[code]
        for l in self.store.leads:
            if l.get("id") == code:
                return l
        return None
