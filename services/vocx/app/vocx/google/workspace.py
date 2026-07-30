"""
google.workspace — Google Drive + Calendar writers for the speaking RM's account.

Drive layout:
  Personal: ATLAS_VOX/<Company>/<YYYY-MM-DD_HHMM>_meeting.md  + _company_summary.md
  Shared:   ATLAS_TEAM/VOX/<Company>/<same>   (a content copy in the team folder)

Folders are resolved through caller-supplied get/set callbacks keyed by the entity
CODE, so spelling variants of a company never spawn duplicate folders. The service
objects (drive v3, calendar v3) are injected, so these writers run against real
googleapiclient resources in production and against fakes in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

FOLDER_MIME = "application/vnd.google-apps.folder"
MD_MIME = "text/markdown"


def services_from_credentials(creds: Any) -> dict[str, Any]:
    """Build {drive, calendar} googleapiclient resources from OAuth credentials."""
    from googleapiclient.discovery import build
    return {
        "drive": build("drive", "v3", credentials=creds, cache_discovery=False),
        "calendar": build("calendar", "v3", credentials=creds, cache_discovery=False),
    }


class DriveWriter:
    def __init__(self, drive_service: Any):
        self.drive = drive_service

    # ---- folder plumbing ---------------------------------------------------
    def ensure_folder(self, name: str, parent_id: str | None) -> str:
        """Return the id of folder `name` under `parent_id`, creating it if absent."""
        q = (f"mimeType='{FOLDER_MIME}' and name='{_esc(name)}' and trashed=false")
        if parent_id:
            q += f" and '{parent_id}' in parents"
        resp = self.drive.files().list(
            q=q, fields="files(id,name)", spaces="drive",
            includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
        files = resp.get("files", [])
        if files:
            return files[0]["id"]
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        created = self.drive.files().create(
            body=body, fields="id", supportsAllDrives=True).execute()
        return created["id"]

    def ensure_path(self, path: str, root_parent_id: str | None = None) -> str:
        """Ensure a slash-separated folder path exists; return the leaf id."""
        parent = root_parent_id
        for segment in [s for s in path.split("/") if s]:
            parent = self.ensure_folder(segment, parent)
        return parent

    def resolve_company_folder(
        self,
        root_path: str,
        company: str,
        get_cached: Callable[[], str | None],
        set_cached: Callable[[str], None],
        root_parent_id: str | None = None,
    ) -> str:
        """Company folder id, cached by entity code (dedupe by code, not name)."""
        cached = get_cached()
        if cached:
            return cached
        root_leaf = self.ensure_path(root_path, root_parent_id)
        folder_id = self.ensure_folder(company, root_leaf)
        set_cached(folder_id)
        return folder_id

    # ---- files -------------------------------------------------------------
    def write_file(self, folder_id: str, filename: str, content: str,
                   mime: str = MD_MIME) -> dict[str, Any]:
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime, resumable=False)
        created = self.drive.files().create(
            body={"name": filename, "parents": [folder_id], "mimeType": mime},
            media_body=media, fields="id,webViewLink", supportsAllDrives=True).execute()
        return {"id": created.get("id"), "link": created.get("webViewLink"), "name": filename}

    def upsert_file(self, folder_id: str, filename: str, content: str,
                    mime: str = MD_MIME) -> dict[str, Any]:
        """Create, or overwrite in place if `filename` already exists in the folder.
        Used for the fixed-name rolling _company_summary.md."""
        from googleapiclient.http import MediaInMemoryUpload
        q = f"name='{_esc(filename)}' and '{folder_id}' in parents and trashed=false"
        resp = self.drive.files().list(
            q=q, fields="files(id)", spaces="drive",
            includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime, resumable=False)
        existing = resp.get("files", [])
        if existing:
            fid = existing[0]["id"]
            updated = self.drive.files().update(
                fileId=fid, media_body=media, fields="id,webViewLink",
                supportsAllDrives=True).execute()
            return {"id": updated.get("id"), "link": updated.get("webViewLink"),
                    "name": filename, "updated": True}
        return self.write_file(folder_id, filename, content, mime)


class CalendarWriter:
    def __init__(self, calendar_service: Any, timezone: str = "Asia/Kolkata"):
        self.calendar = calendar_service
        self.timezone = timezone

    def create_event(self, summary: str, date: str, time: str | None = None,
                     mode: str | None = None, description: str = "",
                     duration_min: int = 45, calendar_id: str = "primary") -> dict[str, Any]:
        """Create a follow-up event. Timed if `time` is given, else all-day."""
        if time:
            start_dt = f"{date}T{_hhmm(time)}:00"
            end_dt = f"{date}T{_add_minutes(_hhmm(time), duration_min)}:00"
            start = {"dateTime": start_dt, "timeZone": self.timezone}
            end = {"dateTime": end_dt, "timeZone": self.timezone}
        else:
            start = {"date": date}
            end = {"date": date}
        loc = {"in-person": "In person", "video": "Video call", "phone": "Phone call",
               "site": "Site visit"}.get(mode or "", "")
        body = {"summary": summary, "description": description, "start": start, "end": end}
        if loc:
            body["location"] = loc
        created = self.calendar.events().insert(calendarId=calendar_id, body=body).execute()
        return {"id": created.get("id"), "link": created.get("htmlLink"),
                "start": created.get("start")}


def _esc(s: str) -> str:
    return str(s or "").replace("\\", "\\\\").replace("'", "\\'")


def _hhmm(time_str: str) -> str:
    t = str(time_str).strip()
    if ":" in t and len(t) >= 4:
        h, m = t.split(":")[:2]
        return f"{int(h):02d}:{int(m):02d}"
    return "09:00"


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = [int(x) for x in hhmm.split(":")]
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"
