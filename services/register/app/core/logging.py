"""Structured logging — re-exported from evam_backend_core (the shared platform).

Kept as ``app.core.logging`` so existing imports are stable; the implementation lives in
``evam_backend_core.logging``.
"""

from __future__ import annotations

from evam_backend_core.logging import (
    ContextFilter,
    actor_ctx,
    configure_logging,
    get_logger,
    request_id_ctx,
    tenant_ctx,
)

__all__ = [
    "ContextFilter", "actor_ctx", "configure_logging", "get_logger",
    "request_id_ctx", "tenant_ctx",
]
