"""Sensitive-operation ONLINE revalidation — the release-one revocation window closer.

The signed context is a short-lived authorization credential; its TTL is a deliberate
revocation window. For the operations where that window is unacceptable (irreversible
deletes/restores, assignment changes, governed imports, evidence break-glass), the
Register — when ``REGISTER_ONLINE_REVALIDATION`` is on — re-resolves the caller against
Access LIVE before acting: user still active, operation still granted, revocation epoch
unchanged. Access unreachable → 503, fail closed; the static matrix is never consulted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.core.security import RequestContext


async def revalidate_sensitive(ctx: RequestContext, operation: str) -> None:
    """No-op when the flag is off or the caller is a machine principal (services are bound
    by their code-level allowlist, not human grants). Otherwise revalidate ONLINE and
    refuse (403) or fail closed (503)."""
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.online_revalidation or ctx.user is None:
        return
    from app.core.access_client import AccessUnavailableError, revalidate_operation
    from app.core.errors import ForbiddenError, ServiceUnavailableError

    try:
        problem = await revalidate_operation(
            ctx.tenant_code, ctx.user.email, operation,
            token_epoch=getattr(ctx.user, "epoch", 0))
    except AccessUnavailableError as exc:
        raise ServiceUnavailableError(
            f"Sensitive operation '{operation}' requires live authorization and the "
            f"Access service cannot answer: {exc}") from exc
    if problem is not None:
        raise ForbiddenError(f"Online revalidation refused '{operation}': {problem}")
