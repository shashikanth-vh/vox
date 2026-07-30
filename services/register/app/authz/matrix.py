"""RBAC matrices — re-exported from the shared platform artifact (evam_backend_core.rbac).

The single policy definition lives in ``evam_backend_core.rbac`` so the Access service
(seed), the Gateway (compiled fallback) and the Register (re-verify + scoped) can never
drift. This module keeps the Register's historical import path working.
"""

from __future__ import annotations

from evam_backend_core.rbac import (
    ALLOWED_TRANSITIONS,
    APPROVER_FOR_SUBJECT,
    ASSIGNMENT_AUTHORITY,
    CREATE_OPERATION_FOR_SUBJECT,
    DEFAULT_LINE_OWNER,
    INITIAL_STATUS,
    OPERATIONS,
    PRIMARY_ASSIGNMENT_ROLE,
    ROLES,
    ROW_LOCKS,
    SERVICE_GRANTS,
    SERVICE_READ_GRANTS,
    VIEW_ACCESS,
    WRITE_OPERATION_FOR_SUBJECT,
    Access,
    initial_status_error,
    transition_error,
)

__all__ = [
    "ALLOWED_TRANSITIONS", "APPROVER_FOR_SUBJECT", "ASSIGNMENT_AUTHORITY",
    "CREATE_OPERATION_FOR_SUBJECT", "DEFAULT_LINE_OWNER", "INITIAL_STATUS", "OPERATIONS",
    "PRIMARY_ASSIGNMENT_ROLE", "ROLES", "ROW_LOCKS", "SERVICE_GRANTS",
    "SERVICE_READ_GRANTS", "VIEW_ACCESS", "WRITE_OPERATION_FOR_SUBJECT", "Access",
    "initial_status_error", "transition_error",
]
