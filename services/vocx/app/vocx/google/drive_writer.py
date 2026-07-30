"""
google.drive_writer — the REAL writer: executes a plan_writes() plan against the
speaking RM's Google Drive + Calendar and the shared ATLAS register.

Drop-in for MockWriter: same execute(ops) shape, plus a `context` carrying the
extraction/transcript/summary needed to render notes. Op dispatch mirrors the op
names plan_writes() emits:

    atlas_create_lead          -> AtlasWriter.create_lead
    atlas_append_interaction   -> AtlasWriter.append_interaction
    drive_write_note (personal)-> DriveWriter into ATLAS_VOX/<Company>/
    drive_write_note (team)    -> DriveWriter into ATLAS_TEAM/VOX/<Company>/
                                  (on failure: flag interaction + queue retry)
    drive_write_company_summary-> DriveWriter.upsert (personal + team)
    calendar_create_event      -> CalendarWriter.create_event
    alias_writeback            -> AtlasWriter.write_aliases

Personal company-folder ids are cached per-RM (a user's own Drive); the shared
team-folder id is cached on the entity via AtlasWriter. Both are keyed by entity
CODE so spelling variants never duplicate folders.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.vocx.google import notes as vocx_note


class FileFolderCache:
    """Per-RM {code: folder_id} cache for personal Drive folders."""

    def __init__(self, cache_dir: str, rm: str):
        self.dir = cache_dir
        os.makedirs(self.dir, exist_ok=True)
        safe = "".join(c for c in (rm or "") if c.isalnum() or c in "-_").lower() or "unknown"
        self.path = os.path.join(self.dir, f"folders_{safe}.json")
        self.data: dict[str, str] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)

    def get(self, code: str) -> str | None:
        return self.data.get(code)

    def set(self, code: str, folder_id: str) -> None:
        self.data[code] = folder_id
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)


class VoxWriter:
    def __init__(self, atlas_writer, drive_writer, calendar_writer, config,
                 personal_cache):
        self.atlas = atlas_writer
        self.drive = drive_writer
        self.cal = calendar_writer
        self.config = config
        self.personal_cache = personal_cache
        self.drive_cfg = config.get("drive", {})

    def execute(self, ops: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
        ext = context["extraction"]
        transcript = context.get("transcript", "")
        summary = context.get("summary", "")
        code, company = _entity_code_company(ops, ext)
        capture_ts = (ext.get("_meta") or {}).get("capture_ts") or ""

        note_md = vocx_note.render_note(ext, summary, transcript, entity_code=code, company=company)
        filename = vocx_note.note_filename(capture_ts, self.drive_cfg.get("note_filename", "{ts}_meeting.md"))

        results: list[dict[str, Any]] = []
        interaction_id: str | None = None

        for op in ops:
            name = op["op"]
            try:
                if name == "atlas_create_lead":
                    r = self.atlas.create_lead(op["record"])
                elif name == "atlas_append_interaction":
                    r = self.atlas.append_interaction(op["record"])
                    interaction_id = op["record"].get("interactionId")
                elif name == "drive_write_note" and op.get("target") == "personal":
                    r = self._write_personal_note(code, company, filename, note_md)
                elif name == "drive_write_note" and op.get("target") == "team_shared":
                    r = self._write_team_note(code, company, filename, note_md, interaction_id, op)
                elif name == "drive_write_company_summary":
                    r = self._write_company_summary(code, company, ext, capture_ts)
                elif name == "calendar_create_event":
                    r = self._create_event(op, company, summary)
                elif name == "alias_writeback":
                    r = self.atlas.write_aliases(op["code"], op["aliases"])
                else:
                    r = {"skipped": True}
                results.append({"op": name, "target": op.get("target"), "status": "ok", "result": r})
            except Exception as e:  # noqa: BLE001 — one op failing must not sink the rest
                results.append({"op": name, "target": op.get("target"),
                                "status": "error", "error": str(e)})
        ok = all(x["status"] != "error" for x in results)
        return {"ok": ok, "count": len(results), "results": results}

    # ---- drive ops ---------------------------------------------------------
    def _write_personal_note(self, code, company, filename, note_md):
        folder = self.drive.resolve_company_folder(
            self.drive_cfg.get("personal_root", "ATLAS_VOX"), company,
            get_cached=lambda: self.personal_cache.get(code),
            set_cached=lambda fid: self.personal_cache.set(code, fid))
        return self.drive.write_file(folder, filename, note_md)

    def _write_team_note(self, code, company, filename, note_md, interaction_id, op):
        try:
            folder = self.drive.resolve_company_folder(
                self.drive_cfg.get("team_root", "ATLAS_TEAM/VOX"), company,
                get_cached=lambda: self.atlas.get_folder_id(code, "team_drive_folder_id"),
                set_cached=lambda fid: self.atlas.set_folder_id(code, "team_drive_folder_id", fid))
            return self.drive.write_file(folder, filename, note_md)
        except Exception as e:  # noqa: BLE001 — spec: flag interaction + queue retry
            if interaction_id:
                self.atlas.flag_interaction_retry(
                    interaction_id, f"team_drive_write_failed: {e}", op)
            raise

    def _write_company_summary(self, code, company, ext, capture_ts):
        last_n = self.drive_cfg.get("rolling_summary_last_n", 8)
        recent = self._recent_captures(code, last_n)
        rolling = "  \n".join("- {}".format(r["summary"]) for r in recent) or ""
        md = vocx_note.render_company_summary(
            company, code, rolling, recent, capture_ts, last_n)
        fname = self.drive_cfg.get("company_summary_filename", "_company_summary.md")
        out = {}
        # personal
        pf = self.drive.resolve_company_folder(
            self.drive_cfg.get("personal_root", "ATLAS_VOX"), company,
            get_cached=lambda: self.personal_cache.get(code),
            set_cached=lambda fid: self.personal_cache.set(code, fid))
        out["personal"] = self.drive.upsert_file(pf, fname, md)
        # team
        tf = self.drive.resolve_company_folder(
            self.drive_cfg.get("team_root", "ATLAS_TEAM/VOX"), company,
            get_cached=lambda: self.atlas.get_folder_id(code, "team_drive_folder_id"),
            set_cached=lambda fid: self.atlas.set_folder_id(code, "team_drive_folder_id", fid))
        out["team"] = self.drive.upsert_file(tf, fname, md)
        return out

    def _recent_captures(self, code, last_n) -> list[dict[str, Any]]:
        ints = [i for i in self.atlas.store.interactions if i.get("refId") == code]
        ints.sort(key=lambda i: (i.get("occurredAt") or "", i.get("loggedAt") or ""), reverse=True)
        return [{"date": i.get("occurredAt"), "summary": (i.get("notes") or "")} for i in ints[:last_n]]

    def _create_event(self, op, company, summary):
        title = op.get("title") or f"Follow-up: {company}"
        na = op.get("next_action")
        if op.get("kind") == "meeting" and na:
            title += " — {}".format(na if len(na) <= 48 else na[:47] + "…")
        desc = op.get("description") or summary or ""
        return self.cal.create_event(title, op["date"], op.get("time"), op.get("mode"), desc)


def build_google_writer(rm: str, store, config: dict[str, Any],
                        token_store=None, cache_dir: str = "vocx_tokens") -> VoxWriter:
    """Assemble a real VoxWriter for `rm`: their Google services (from stored
    refresh token) + the shared register writer. Requires google libraries and a
    saved token for the RM (run the OAuth flow first)."""
    from app.vocx.core.store import AtlasWriter, MutableAtlasStore
    from app.vocx.google.oauth import TokenStore
    from app.vocx.google.workspace import CalendarWriter, DriveWriter, services_from_credentials

    token_store = token_store or TokenStore(cache_dir)
    creds = None
    try:
        creds = token_store.credentials(rm)          # refreshes if needed
    except Exception as primary_err:  # noqa: BLE001 — no token, expired/revoked, missing libs…
        # Demo mode: if this RM's token is missing or unusable, fall back to ANY
        # other connected account so captures still schedule. Try each until one
        # works; if none do, re-raise the ORIGINAL error so the reason surfaces.
        # Set google.fallback_to_any_token=false for real per-RM calendars.
        if config.get("google", {}).get("fallback_to_any_token"):
            import glob
            for f in sorted(glob.glob(os.path.join(token_store.tokens_dir, "*.token.json"))):
                cand = os.path.basename(f)[: -len(".token.json")]
                if cand == rm:
                    continue
                try:
                    creds = token_store.credentials(cand)
                    rm = cand
                    break
                except Exception:  # noqa: BLE001 — try the next token
                    creds = None
        if creds is None:
            raise primary_err
    svc = services_from_credentials(creds)
    if not isinstance(store, MutableAtlasStore):
        raise TypeError("build_google_writer needs a MutableAtlasStore (writable register)")
    atlas = AtlasWriter(store, config)
    drive = DriveWriter(svc["drive"])
    cal = CalendarWriter(svc["calendar"], timezone=config.get("google", {}).get("timezone", "Asia/Kolkata"))
    personal_cache = FileFolderCache(os.path.join(cache_dir, "folders"), rm)
    return VoxWriter(atlas, drive, cal, config, personal_cache)


def _entity_code_company(ops, ext):
    em = ext.get("entity_match") or {}
    code = em.get("code")
    company = em.get("canonical_name") or em.get("proposed_company") or "Unknown"
    for op in ops:
        if op["op"] == "atlas_create_lead":
            code = op["record"].get("id")
            company = op["record"].get("company") or company
        if op["op"] == "atlas_append_interaction":
            code = op["record"].get("refId") or code
    return code, company
