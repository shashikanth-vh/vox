"""Router factory that binds the transient-retry behaviour to every route.

Use ``api_router(...)`` in place of ``APIRouter(...)`` so every endpoint on the router
inherits transparent retry of transient database failures (see ``app.core.retry``).
"""

from __future__ import annotations

from fastapi import APIRouter

from evam_backend_core.retry import RetryableRoute


def api_router(**kwargs) -> APIRouter:
    kwargs.setdefault("route_class", RetryableRoute)
    return APIRouter(**kwargs)
