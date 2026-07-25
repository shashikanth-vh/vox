"""Object-storage abstraction for document bytes (S3 / MinIO).

The Register stores document *references*; the *bytes* live in an S3-compatible object
store. This package is the seam:

* ``get_storage()`` returns the configured backend, or ``None`` for the "inline" backend
  (small files kept in Postgres — the dev default, no object store required).
* ``S3Storage`` talks to AWS S3 or MinIO (same API, different endpoint) via boto3, with
  the blocking calls off-loaded to a thread so the async event loop never stalls.

Keeping this behind one factory means the endpoints don't care which store is behind them,
and a deployment flips between "inline" and "s3" with one env var.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.storage.base import Storage, StoredObject, parse_s3_uri
from app.storage.s3 import S3Storage

__all__ = ["Storage", "StoredObject", "S3Storage", "get_storage", "reset_storage", "parse_s3_uri"]


@lru_cache
def get_storage() -> Storage | None:
    """The configured object store, or ``None`` when the backend is "inline"."""
    settings = get_settings()
    if settings.storage_backend != "s3":
        return None
    return S3Storage(
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        public_endpoint_url=settings.s3_public_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        use_ssl=settings.s3_use_ssl,
        path_style=settings.s3_path_style,
        presign_expiry_seconds=settings.s3_presign_expiry_seconds,
        auto_create_bucket=settings.s3_auto_create_bucket,
    )


def reset_storage() -> None:
    """Drop the cached backend (tests, or after a settings change)."""
    get_storage.cache_clear()
