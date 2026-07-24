"""Structured logging.

Production emits one JSON object per line (easy to ship to CloudWatch / Loki /
OpenSearch). Local development gets a compact human-readable line. Every log record
inside a request carries the request_id so a single API call can be traced end to end.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from pythonjsonlogger import json as jsonlogger

# Correlation id for the in-flight request; set by middleware, read by the log filter.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_ctx: ContextVar[str | None] = ContextVar("tenant", default=None)
actor_ctx: ContextVar[str | None] = ContextVar("actor", default=None)


class ContextFilter(logging.Filter):
    """Injects request-scoped context vars onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.tenant = tenant_ctx.get()
        record.actor = actor_ctx.get()
        return True


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())

    fmt: logging.Formatter
    if json_logs:
        fmt = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            "%(request_id)s %(tenant)s %(actor)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            timestamp=True,
        )
    else:
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s [req=%(request_id)s] %(message)s"
        )
    handler.setFormatter(fmt)
    root.addHandler(handler)

    # Quiet down noisy libraries; let SQLAlchemy echo be governed by config.
    for noisy in ("uvicorn.access", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
