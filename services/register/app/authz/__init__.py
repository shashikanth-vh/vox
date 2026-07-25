"""RBAC for the Register — the ATLAS RBAC spec (v3.1) as an enforcement engine.

``matrix``  — the spec's three sheets encoded verbatim (roles, view access, operations).
``engine``  — evaluation: role stacking (highest wins), scope resolution, assignment-driven
              line permissions, approval routing, and the request-context enforcement hook.
"""

from __future__ import annotations

from app.authz.engine import (
    UserContext,
    can_approve,
    can_write_line,
    effective_operations,
    effective_views,
    enforce_operation,
    user_context_from_headers,
)
from app.authz.matrix import (
    ASSIGNMENT_AUTHORITY,
    DEFAULT_LINE_OWNER,
    OPERATIONS,
    ROLES,
    VIEW_ACCESS,
    Access,
)

__all__ = [
    "Access", "ROLES", "VIEW_ACCESS", "OPERATIONS", "DEFAULT_LINE_OWNER",
    "ASSIGNMENT_AUTHORITY", "UserContext", "user_context_from_headers", "effective_views",
    "effective_operations", "enforce_operation", "can_write_line", "can_approve",
]
