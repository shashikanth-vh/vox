"""Verified caller identity and exact-path Register context re-binding."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from evam_backend_core.errors import ForbiddenError
from evam_backend_core.internal_token import (
    InternalTokenError,
    mint_internal_context,
    verify_internal_context,
)
from evam_backend_core.logging import get_logger
from fastapi import Request

from app.config import Settings

log = get_logger("chitti.identity")


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    tenant: str
    email: str
    user_id: str
    roles: tuple[str, ...] = ()
    report_ids: tuple[str, ...] = ()
    report_emails: tuple[str, ...] = ()
    effective_views: dict[str, str] = field(default_factory=dict)
    effective_operations: dict[str, str] = field(default_factory=dict)
    matrix_version: int = 0
    decision: str | None = None
    policy_version: str | None = None
    epoch: int = 0
    posture: str = "delegated"

    @property
    def display_scope(self) -> str:
        if self.posture == "local_debug" and "Admin" in self.roles:
            return "DEBUG_FULL"
        return self.posture.upper()


def resolve_caller(request: Request, settings: Settings) -> CallerIdentity:
    """Verify a gateway context or create the unmistakable local debug identity."""

    raw = request.headers.get("X-Internal-Context")
    if raw:
        try:
            context = verify_internal_context(
                raw,
                verify_key=settings.internal_signing_secret,
                algorithms=(settings.internal_signing_algorithm,),
            )
        except InternalTokenError as exc:
            log.warning("chitti_context_verify_failed", extra={"error": str(exc)})
            raise ForbiddenError("Invalid delegated caller context.") from exc
        requested_tenant = request.headers.get("X-Tenant")
        if context.method != request.method or context.path != request.url.path:
            raise ForbiddenError("Delegated caller context is not bound to this Chitti route.")
        if requested_tenant and context.tenant != requested_tenant.strip():
            raise ForbiddenError("Delegated caller context tenant does not match X-Tenant.")
        return CallerIdentity(
            tenant=context.tenant,
            email=context.email,
            user_id=context.user_id,
            roles=tuple(context.roles),
            report_ids=tuple(context.report_ids),
            report_emails=tuple(context.report_emails),
            effective_views=dict(context.effective_views),
            effective_operations=dict(context.effective_operations),
            matrix_version=context.matrix_version,
            decision=context.decision,
            policy_version=context.policy_version,
            epoch=context.epoch,
        )

    if settings.require_delegation:
        raise ForbiddenError("A verified delegated caller context is required.")
    if settings.environment != "local" or not settings.debug_user_email:
        raise ForbiddenError("Local debug identity is not configured.")
    return CallerIdentity(
        tenant=settings.register_tenant,
        email=settings.debug_user_email.strip().lower(),
        user_id=settings.debug_user_id.strip() or str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"prism-user:{settings.debug_user_email.strip().lower()}")
        ),
        roles=tuple(settings.debug_role_list()),
        posture="local_debug",
    )


def mint_register_context(
    identity: CallerIdentity,
    settings: Settings,
    *,
    method: str,
    path: str,
) -> str:
    """Re-bind immutable verified/debug facts to one exact Register request."""

    if method != "GET":
        raise ValueError("Chitti may mint Register context only for GET requests.")
    return mint_internal_context(
        signing_key=settings.internal_signing_secret,
        algorithm=settings.internal_signing_algorithm,
        ttl_seconds=settings.internal_token_ttl_seconds,
        tenant=identity.tenant,
        email=identity.email,
        user_id=identity.user_id,
        roles=list(identity.roles),
        report_ids=list(identity.report_ids),
        report_emails=list(identity.report_emails),
        effective_views=dict(identity.effective_views),
        effective_operations=dict(identity.effective_operations),
        matrix_version=identity.matrix_version,
        decision=identity.decision,
        method=method,
        path=path,
        policy_version=identity.policy_version,
        epoch=identity.epoch,
    )
