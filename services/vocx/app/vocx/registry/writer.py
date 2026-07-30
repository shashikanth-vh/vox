"""register_writer.py — executes a VOX write plan against the PRISM Register (+ Google).

The PoC's writers targeted the ATLAS JSON blob and the RM's Google Drive/Calendar. In
PRISM the register writes go to the Register service as the `svc_vox` principal — REAL
writes, always, on an approved commit — while the Google side stays per-RM and optional:

    atlas_create_lead        → POST /v1/leads          (minted LD-V## kept in lead_no)
    atlas_append_interaction → POST /v1/interactions   (subject resolved to a Register row)
    calendar_create_event    → the speaking RM's Google Calendar, when they have
                               connected (per-RM OAuth token); otherwise reported skipped
    drive_write_*            → only when drive_enabled AND connected; else skipped
    alias_writeback          → VOX-side alias map (JSON next to the token store) — the
                               Register has no alias field, matching the PoC's design

Ref translation: the resolver's candidates carry ATLAS-style refs (client CODE for
Deal-type matches, Register lead UUID for leads, a freshly minted LD-V id for new leads).
The Register needs UUIDs, so the writer resolves code→entity id via the store blob and
minted-id→row id via the lead it just created.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import httpx

from app.vocx.core.atlas import AtlasStore

log = logging.getLogger("vocx")


class _RegisterRefusedError(RuntimeError):
    """A 4xx from the Register — deterministic, never retried."""


class _SideFile:
    """Tiny thread-safe JSON map persisted next to the token store."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def update(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)


class RegisterWriter:
    """The AtlasWriter counterpart: shared-register ops via the Register API."""

    def __init__(self, base_url: str, api_key: str, tenant: str, store: AtlasStore,
                 state_dir: str, timeout_s: float = 10.0,
                 capture_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant = tenant
        self.store = store
        self.timeout_s = timeout_s
        # The capture id makes every Register write IDEMPOTENT: the same approved
        # capture, committed twice (client retry, double tap), replays the original
        # rows instead of duplicating them — the Register's Idempotency-Key replay.
        self.capture_id = capture_id
        self.aliases = _SideFile(os.path.join(state_dir, "vocx_aliases.json"))
        self.folders = _SideFile(os.path.join(state_dir, "vocx_folders.json"))
        self._minted: dict[str, str] = {}    # LD-V## → created Register lead uuid

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "X-Tenant": self.tenant, "X-Actor": "vocx-vox"}

    def _post(self, path: str, body: dict[str, Any], op: str) -> dict[str, Any]:
        headers = self._headers()
        idempotent = bool(self.capture_id)
        if idempotent:
            headers["Idempotency-Key"] = f"vocx:{self.capture_id}:{op}"
        last: Exception | None = None
        for attempt in range(3 if idempotent else 1):   # only keyed writes may retry
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    r = client.post(f"{self.base_url}{path}", json=body, headers=headers)
                if r.status_code >= 500:
                    raise RuntimeError(f"Register {path} → {r.status_code}: {r.text[:400]}")
                if r.status_code >= 400:
                    # 4xx is a real refusal (validation/RBAC) — retrying cannot help.
                    raise _RegisterRefusedError(f"Register {path} → {r.status_code}: {r.text[:400]}")
                return r.json()
            except _RegisterRefusedError:
                raise
            except (httpx.TransportError, RuntimeError) as e:
                last = e
                time.sleep(0.4 * (2 ** attempt))
        raise RuntimeError(f"Register write failed after retries: {path} ({last})")

    # ---- ops ----------------------------------------------------------------
    def create_lead(self, record: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in {
            "lead_no": record.get("id"),
            "company": record.get("company"),
            "sector": record.get("sector") or None,
            "lens": record.get("lens") or None,
            "source": record.get("source") or None,
            "source_name": record.get("sourceDetail") or None,
            "rm": record.get("rm") or None,
            "status": record.get("status") or "Active",
            "temperature": record.get("temp") or None,
            "contact": record.get("contact") or None,
            "phone": record.get("phone") or None,
            "last_interaction_date": record.get("last") or None,
            "next_action": record.get("next") or None,
            "next_action_date": record.get("nextDate") or None,
            "notes": record.get("notes") or None,
        }.items() if v is not None}
        created = self._post("/v1/leads", body, op="lead")
        if record.get("id"):
            self._minted[record["id"]] = created["id"]
        return {"lead_id": created["id"], "lead_no": record.get("id")}

    def append_interaction(self, record: dict[str, Any],
                           subject_override: dict[str, str] | None = None) -> dict[str, Any]:
        if subject_override:
            subject_type = subject_override["subject_type"]
            subject_id = subject_override["subject_id"]
        else:
            subject_type, subject_id = self._subject_of(record)
        occurred = record.get("occurredAt") or ""
        if len(occurred) == 10:                       # date-only → a concrete timestamp
            occurred = occurred + "T09:00:00Z"
        notes = record.get("notes") or ""
        body = {k: v for k, v in {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "interaction_type": record.get("interactionType") or "In-Person Meeting",
            "direction": record.get("direction"),
            "occurred_at": occurred or None,
            "summary": (notes.splitlines()[0][:300] if notes else None),
            "notes": notes or None,
            "performed_by": record.get("user") or None,
            "contact_name": (record.get("person") or "")[:200] or None,
            "lender_name": record.get("lenderName"),
        }.items() if v is not None}
        created = self._post("/v1/interactions", body, op="interaction")
        return {"interaction_id": created.get("id"), "subject_type": subject_type,
                "subject_id": subject_id}

    def _subject_of(self, record: dict[str, Any]) -> tuple[str, str]:
        ref_id = str(record.get("refId") or "")
        ref_type = record.get("refType") or "Deal"
        if ref_type == "Lead":
            return "Lead", self._minted.get(ref_id, ref_id)
        # Deal / Syndication matches carry the client CODE — the conversation logs on
        # the company's own timeline (the deal-profile view ATLAS renders).
        client = (self.store.clients or {}).get(ref_id) or {}
        entity_id = client.get("_register_entity_id")
        if not entity_id:
            raise RuntimeError(f"cannot resolve entity for client code {ref_id!r}")
        return "Entity", entity_id

    def write_aliases(self, code: str, aliases: list[str]) -> dict[str, Any]:
        cur = set(self.aliases.data.get(code, []) or [])
        cur.update(a for a in aliases if a)
        self.aliases.update(code, sorted(cur))
        return {"code": code, "aliases": sorted(cur)}

    def flag_interaction_retry(self, interaction_id: str, reason: str,
                               op: dict[str, Any]) -> None:
        log.warning("VOX drive retry queued for interaction %s: %s", interaction_id, reason)

    # Drive folder-id cache (used only when Drive is enabled).
    def get_folder_id(self, code: str, field: str) -> str | None:
        return (self.folders.data.get(code) or {}).get(field)

    def set_folder_id(self, code: str, field: str, folder_id: str) -> None:
        entry = dict(self.folders.data.get(code) or {})
        entry[field] = folder_id
        self.folders.update(code, entry)


class PrismVoxWriter:
    """Executes a plan: Register ops always; Calendar/Drive per-RM, best-effort.

    Unlike the PoC (where a missing Google token demoted EVERYTHING to a MockWriter),
    an approved commit here always lands the register writes — the calendar event is
    an add-on that reports `skipped` with a reason when the RM hasn't connected.
    """

    def __init__(self, register: RegisterWriter, config: dict[str, Any],
                 calendar: Any = None, drive_writer: Any = None) -> None:
        self.register = register
        self.config = config
        self.cal = calendar
        self.drive = drive_writer

    def execute(self, ops: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        ok = True
        # The recording's canonical reference (s3://… once MinIO is configured) rides on
        # the interaction, so the register row always points back at what was said.
        ref = ((context.get("extraction") or {}).get("_meta") or {}).get("transcript_ref")
        # Explicit "Log To" from the approval card: the interaction lands on the chosen
        # subject (a Lending/Syndication/AM line, a deal, a lead) instead of the
        # resolver's default routing. Validated upstream; None = automatic routing.
        log_to = context.get("log_to")
        for op in ops:
            name = op["op"]
            try:
                if name == "atlas_create_lead":
                    r = self.register.create_lead(op["record"])
                elif name == "atlas_append_interaction":
                    record = dict(op["record"])
                    if ref:
                        record["notes"] = ((record.get("notes") or "").rstrip()
                                           + f"\n\nRecording: {ref}").strip()
                    r = self.register.append_interaction(record, subject_override=log_to)
                elif name == "calendar_create_event":
                    if self.cal is None:
                        results.append({"op": name, "status": "skipped",
                                        "reason": "google_not_connected"})
                        continue
                    r = self.cal.create_event(
                        op.get("title") or "Follow-up", op.get("date"), op.get("time"),
                        op.get("mode"), op.get("description") or "")
                elif name == "alias_writeback":
                    r = self.register.write_aliases(op["code"], op["aliases"])
                elif name.startswith("drive_"):
                    results.append({"op": name, "status": "skipped",
                                    "reason": "drive_disabled" if self.drive is None
                                    else "not_implemented"})
                    continue
                else:
                    results.append({"op": name, "status": "skipped", "reason": "unknown_op"})
                    continue
                results.append({"op": name, "status": "ok", "result": r})
            except Exception as e:  # noqa: BLE001 — one op failing must not sink the rest
                ok = False
                log.exception("VOX write op %s failed", name)
                results.append({"op": name, "status": "error", "error": str(e)})
        return {"ok": ok, "count": len(results), "results": results}


def make_writer_factory(settings: Any, state_dir: str):
    """(rm, store, config) → PrismVoxWriter. Calendar attaches only when the RM has a
    Google token; Register writes need nothing but the service key."""

    def factory(rm: str, store: AtlasStore, config: dict[str, Any],
                capture_id: str | None = None) -> PrismVoxWriter:
        register = RegisterWriter(settings.register_base_url, settings.register_api_key,
                                  settings.register_tenant, store, state_dir,
                                  capture_id=capture_id)
        calendar = None
        gcfg = config.get("google", {}) or {}
        try:
            from app.vocx.google.oauth import TokenStore
            tokens = TokenStore(gcfg.get("tokens_dir", "vocx_tokens"))
            if gcfg.get("calendar_enabled", True) and tokens.has(rm):
                from app.vocx.google.workspace import CalendarWriter, services_from_credentials
                services = services_from_credentials(tokens.credentials(rm))
                calendar = CalendarWriter(services["calendar"],
                                          gcfg.get("timezone", "Asia/Kolkata"))
        except Exception as e:  # noqa: BLE001 — google stack absent/broken ⇒ register-only
            log.warning("VOX calendar unavailable for %s: %s", rm, e)
        return PrismVoxWriter(register, config, calendar=calendar)

    return factory
