"""Who is asking — and therefore whose captures they may touch.

VocX authenticated the SERVICE and never the person. Its front door checks the shared
front-door key, which the gateway injects on behalf of every signed-in ATLAS user, and
then every per-person route took the RM from a QUERY PARAMETER. So one signed-in user
could read, edit, delete and play back another's captures by changing `?rm=`. The
recordings are the sharpest edge of that: raw audio of a client meeting.

An unfiled capture is a person's unfinished note, not a record. Nothing supervises it
because there is nothing to supervise until it is committed — at which point it becomes a
register interaction and the register's own scoping governs it. So the rule here is flat:
**a draft belongs to the person who recorded it, and to nobody else.** There is no
supervisor override, deliberately; if one is ever needed it should be an audited
break-glass like the register's, not an ambient Admin privilege.

The key a capture is filed under is derived from the VERIFIED e-mail the gateway forwards
— and from nothing else. An earlier build resolved it against the register's people
roster, which made the location of someone's unfiled work depend on a remote read: it
resolved on one restart and not the next, and a person's own report list came back empty
with the drafts still on disk. The roster handle survives only as a READ alias for
captures written before the change, and only when it is a case-variant of the e-mail's
local part (see aliases_for for why that limit is a boundary and not a nicety).
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from typing import Any

#: The verified e-mail of the person this request is being served for, set by the mount
#: adapter and read by the register writer so a row VocX creates is ATTRIBUTED to the RM
#: who dictated it. Without it the row is stamped with the service and belongs to nobody:
#: the RM who recorded the capture cannot see the lead they just filed, while an Admin
#: can. A ContextVar because the pipeline is synchronous and runs in a threadpool — the
#: context is copied into the worker, so this stays per-request rather than global.
caller_email: contextvars.ContextVar[str] = contextvars.ContextVar(
    "vocx_caller_email", default="")

_TTL_S = 300.0
_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def local_part(email: str) -> str:
    """The fallback handle: the e-mail's local part. Stable, and the same value the ATLAS
    client falls back to, so a roster miss does not split one person into two owners."""
    return (email or "").split("@")[0].strip()


def _lookup(email: str, settings: Any) -> str:
    """The register's handle for this e-mail, or '' when it holds none."""
    import httpx

    base = (getattr(settings, "register_base_url", "") or "").rstrip("/")
    if not base:
        return ""
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{base}/v1/people",
                           params={"q": email, "limit": 5},
                           headers={"X-API-Key": getattr(settings, "register_api_key", ""),
                                    "X-Tenant": getattr(settings, "register_tenant", "")})
        if r.status_code != 200:
            return ""
        rows = r.json()
        rows = rows.get("items") if isinstance(rows, dict) else rows
    except Exception:                                  # noqa: BLE001 - identity, never fatal
        return ""
    wanted = email.strip().lower()
    for row in rows or []:
        if str(row.get("email") or "").strip().lower() == wanted:
            return str(row.get("name") or row.get("full_name") or "").strip()
    return ""


def _handle(email: str, settings: Any) -> str:
    """The roster handle for this e-mail, TTL-cached. '' when the register holds none or
    cannot be reached."""
    email = (email or "").strip()
    if not email:
        return ""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(email)
        if hit and (now - hit[1]) < _TTL_S:
            return hit[0]
    handle = _lookup(email, settings)
    with _lock:
        _cache[email] = (handle, now)
    return handle


def rm_for(email: str, settings: Any = None) -> str:
    """The key this person's captures are filed under — derived from the e-mail alone.

    This deliberately does NOT consult the register. Keying private drafts on a
    roster-resolved handle makes the location of someone's unsaved work depend on a remote
    read: resolve today, fail tomorrow, and the same person's captures are filed under two
    names. Their report list then comes back empty with the drafts still sitting on disk —
    the worst failure this module can have, because it is silent and it is about work
    nobody else can recover for them.

    The local part is also what every ATLAS client already computes, so server and browser
    agree without a round trip.
    """
    return local_part(email)


def aliases_for(email: str, settings: Any) -> list[str]:
    """Every key this person's captures may be read from, the write key first.

    An earlier build filed captures under the roster handle ("Priya"), and the store keeps
    a case-preserving directory per key — so after the switch to the local part ("priya")
    those drafts are still on disk under a name nothing lists any more. The old key is
    therefore read as well.

    Only a CASE-VARIANT of the write key is accepted, and that limit is the point. A
    roster handle unrelated to the e-mail could coincide with a different person's write
    key, and reading it would hand one person another's unfiled captures — the one thing
    this module exists to prevent. A case-variant of your own local part cannot be someone
    else's. Handles that differ by more than case are left unread; those drafts are
    reachable by their own owner only after they are committed.
    """
    primary = rm_for(email)
    if not primary:
        return []
    handle = (_handle(email, settings) or "").strip()
    if handle and handle != primary and handle.lower() == primary.lower():
        return [primary, handle]
    return [primary]


def reset_cache() -> None:                              # test hook
    with _lock:
        _cache.clear()


def owns_audio_ref(ref: str, rm: str) -> bool:
    """Whether an archived recording belongs to this RM.

    The archive names a clip ``{timestamp}_{rm}{ext}`` (see speech/audio_store), so the
    owner is readable from the reference itself and needs no extra index. A ref is opaque
    to the caller but NOT unguessable, and it is the one route where the payload is the
    raw meeting audio — so it gets its own check rather than relying on the caller having
    asked with the right `rm`.
    """
    name = os.path.basename((ref or "").split("?")[0])
    stem = os.path.splitext(name)[0]
    safe = "".join(c for c in (rm or "") if c.isalnum()).lower()
    if not safe or "_" not in stem:
        return False
    return stem.rsplit("_", 1)[-1].lower() == safe
