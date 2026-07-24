"""Declarative base + shared mixins — re-exported from evam_backend_core.

The Register's models inherit ``RegisterBase`` (an alias of the platform's ``RecordBase``):
tenant-aware, versioned (optimistic locking) and auditable/soft-deletable.
"""

from __future__ import annotations

from evam_backend_core.db.base import (
    NAMING_CONVENTION,
    AuditLog,
    Base,
    RecordBase,
    RegisterBase,
    SoftDeleteMixin,
    TimestampMixin,
)

__all__ = [
    "NAMING_CONVENTION", "AuditLog", "Base", "RecordBase", "RegisterBase",
    "SoftDeleteMixin", "TimestampMixin",
]
