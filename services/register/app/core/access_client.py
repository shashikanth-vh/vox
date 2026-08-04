"""Optional client that verifies an ASSIGNEE against the Access service.

Assignments reference an Access-service user id (there is no local FK). When
``REGISTER_ACCESS_URL`` is configured, ``verify_assignee`` confirms the user exists, is
active, and holds a role compatible with the assignment role being placed — so a service
principal cannot assign an arbitrary UUID, nor a role the assignee does not actually hold.

When Access is NOT configured (dev / local), the caller falls back to the local Person
roster. The result is cached briefly (identity changes rarely within a request burst) and
the Register serves last-known-good if Access is momentarily unreachable, matching the
gateway's resolver posture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

# assignment_role → the RBAC role(s) an assignee must hold to receive it. Admin/Management
# may hold any assignment (they can act across verticals). Kept here (not the matrix) as it
# is an identity-shape rule, not an access cell.
_ROLE_FOR_ASSIGNMENT: dict[str, set[str]] = {
    "BDRM": {"BDRM"},
    "Deal Analyst": {"Deal Analyst"},
    "Syn RM": {"Syn RM"},
    "AM RM": {"AM RM"},
}
_UNIVERSAL = {"Admin", "Management"}


@dataclass
class _Cached:
    roles: set[str]
    active: bool
    at: float
    full_name: str = ""
    email: str = ""


class AccessUnavailableError(RuntimeError):
    """Access is configured but could not be reached and there is no cached answer."""


_CACHE: dict[tuple[str, str], _Cached] = {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


async def _resolve(tenant_code: str, user_id: str) -> _Cached | None:
    """Fetch (roles, active) for a user id from Access, cached by (tenant, id)."""
    settings = get_settings()
    key = (tenant_code, user_id)
    cached = _CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached.at) < settings.access_verify_ttl_s:
        return cached
    try:
        async with _client() as client:
            resp = await client.get(
                f"{settings.access_url.rstrip('/')}/v1/users/{user_id}",
                headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant_code})
    except httpx.HTTPError as exc:
        if cached is not None:
            return cached  # last-known-good
        raise AccessUnavailableError(str(exc)) from exc
    if resp.status_code == 404:
        _CACHE[key] = _Cached(roles=set(), active=False, at=now)
        return None
    if resp.status_code != 200:
        if cached is not None:
            return cached
        raise AccessUnavailableError(f"Access returned {resp.status_code}")
    body = resp.json()
    entry = _Cached(roles=set(body.get("roles", [])),
                    active=bool(body.get("is_active", True)), at=now,
                    full_name=str(body.get("full_name", "")),
                    email=str(body.get("email", "")))
    _CACHE[key] = entry
    return entry


def _name_matches(entry: _Cached, expected_name: str) -> bool:
    """The provided display name must denote the SAME identity as the id — so a caller
    can't pass one person's UUID with another's name.

    Matches the full name, the e-mail, or its local part, case-insensitively. Callers in
    the Register resolve the name against the people roster first and send the roster's
    full name (app.core.people.canonical_name), so a picker offering a short handle is
    not compared against a mailbox it was never derived from.
    """
    want = expected_name.strip().lower()
    if not want:
        return True
    if entry.full_name and entry.full_name.strip().lower() == want:
        return True
    if not entry.email:
        return False
    email = entry.email.strip().lower()
    return want in (email, email.split("@")[0])


async def verify_assignee(tenant_code: str, user_id: str, assignment_role: str,
                          expected_name: str | None = None) -> str | None:
    """Return an error string if the assignee is invalid for this assignment, else None.

    When ``expected_name`` is given, it must denote the SAME Access identity as ``user_id``
    (so the entered RM/analyst name is BOUND to the id, not free-text next to it).

    No-op (returns None) when Access is not configured — the caller then falls back to the
    local Person roster.
    """
    if not get_settings().access_url:
        return None
    entry = await _resolve(tenant_code, user_id)
    if entry is None:
        return f"Assignee '{user_id}' is not a known Access user in this tenant."
    if not entry.active:
        return f"Assignee '{user_id}' is not an active user."
    needed = _ROLE_FOR_ASSIGNMENT.get(assignment_role, set())
    if needed and not (entry.roles & (needed | _UNIVERSAL)):
        return (f"Assignee '{user_id}' does not hold a role permitting a "
                f"'{assignment_role}' assignment (has {sorted(entry.roles) or ['<none>']}).")
    if expected_name and not _name_matches(entry, expected_name):
        return (f"Assignee name {expected_name!r} does not match the identity of "
                f"'{user_id}' ({entry.full_name or entry.email or 'unknown'}).")
    return None


def _reset_cache() -> None:  # test hook
    _CACHE.clear()


async def revalidate_operation(tenant_code: str, email: str, operation: str,
                               token_epoch: int | None = None) -> str | None:
    """SENSITIVE-OPERATION online revalidation: re-resolve the caller against Access LIVE
    (no cache) and require (a) the user still active, (b) ``operation`` still granted, and
    (c) the revocation epoch unchanged since the signed context was minted — so a role
    revocation or deactivation takes effect immediately for the operations that matter
    most, regardless of any token still being within its TTL.

    Returns a problem string (→ 403) when the caller no longer qualifies; None when the
    action may proceed. Raises :class:`AccessUnavailableError` when Access cannot answer —
    the caller MUST fail closed (503), never fall back to the static matrix."""
    settings = get_settings()
    if not settings.access_url:
        raise AccessUnavailableError("online revalidation requires REGISTER_ACCESS_URL")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.access_url.rstrip('/')}/v1/resolve",
                params={"email": email},
                headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant_code},
            )
    except httpx.HTTPError as exc:
        raise AccessUnavailableError(str(exc)) from exc
    if resp.status_code == 404:
        return f"user '{email}' no longer exists or is inactive."
    if resp.status_code >= 400:
        raise AccessUnavailableError(f"access /resolve returned {resp.status_code}")
    body = resp.json()
    if not body.get("is_active", False):
        return f"user '{email}' has been deactivated."
    level = str(body.get("operations", {}).get(operation, "NONE"))
    if level in ("", "NONE"):
        return f"operation '{operation}' is no longer granted to '{email}'."
    fresh_epoch = int(body.get("epoch", 0))
    if token_epoch is not None and fresh_epoch > token_epoch:
        return ("authorization changed since sign-in (revocation epoch advanced) — "
                "retry the request to obtain a fresh context.")
    return None
