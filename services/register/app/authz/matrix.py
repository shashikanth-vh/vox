"""RBAC matrices — re-exported from the shared platform artifact (evam_backend_core.rbac).

The single policy definition lives in ``evam_backend_core.rbac`` so the Access service
(seed), the Gateway (compiled fallback) and the Register (re-verify + scoped) can never
drift. This module keeps the Register's historical import path working.
"""

from __future__ import annotations

from evam_backend_core.rbac import (
    APPROVER_FOR_SUBJECT,
    ASSIGNMENT_AUTHORITY,
    DEFAULT_LINE_OWNER,
    OPERATIONS,
    PRIMARY_ASSIGNMENT_ROLE,
    ROLES,
    VIEW_ACCESS,
    Access,
)

__all__ = [
    "APPROVER_FOR_SUBJECT", "ASSIGNMENT_AUTHORITY", "DEFAULT_LINE_OWNER", "OPERATIONS",
    "PRIMARY_ASSIGNMENT_ROLE", "ROLES", "VIEW_ACCESS", "Access",
]
