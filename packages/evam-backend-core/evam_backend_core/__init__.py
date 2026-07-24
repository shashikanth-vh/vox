"""evam-backend-core — the shared production-grade backend platform for PRISM services.

Import the building blocks you need:

    from evam_backend_core.config import BaseServiceSettings
    from evam_backend_core.app import create_service_app
    from evam_backend_core.db.base import RecordBase, AuditLog, Base
    from evam_backend_core.crud import CRUDRepository
    from evam_backend_core.router import api_router
    from evam_backend_core.pagination import Page
    from evam_backend_core.errors import AppError, NotFoundError, ConflictError, ...
"""

from __future__ import annotations

__version__ = "0.1.0"
