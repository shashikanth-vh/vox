"""Async engine + session management — re-exported from evam_backend_core.

Registers the Register's ``get_settings`` as the platform's settings provider so the
shared engine can resolve configuration on any lazy-init path.
"""

from __future__ import annotations

from evam_backend_core.db.session import (
    dispose_engine,
    get_engine,
    get_session,
    get_sessionmaker,
    init_engine,
    register_settings_provider,
    session_scope,
)

from app.core.config import get_settings

# Wire the Register's settings into the shared engine (used by lazy-init fallbacks).
register_settings_provider(get_settings)

__all__ = [
    "dispose_engine", "get_engine", "get_session", "get_sessionmaker", "init_engine",
    "session_scope",
]
