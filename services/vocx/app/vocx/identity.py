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

The RM handle is resolved from the VERIFIED e-mail the gateway forwards, against the
register's own people roster, so it matches the handle the rest of the platform addresses
that person by. The result is cached briefly: identity changes rarely, and a lookup per
capture would put the register in the path of every keystroke of a typeahead.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

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


def rm_for(email: str, settings: Any) -> str:
    """The RM handle captures are filed under for this verified e-mail."""
    email = (email or "").strip()
    if not email:
        return ""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(email)
        if hit and (now - hit[1]) < _TTL_S:
            return hit[0]
    handle = _lookup(email, settings) or local_part(email)
    with _lock:
        _cache[email] = (handle, now)
    return handle


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
