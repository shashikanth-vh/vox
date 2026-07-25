"""Storage protocol + shared value types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StoredObject:
    """The outcome of storing bytes: where they went, and how to reference them."""

    uri: str          # canonical reference, e.g. "s3://bucket/key"
    backend: str      # "s3"
    key: str          # object key within the bucket
    bucket: str


@runtime_checkable
class Storage(Protocol):
    """An S3-compatible object store. Blocking I/O is off-loaded to a thread inside each
    implementation, so every method is awaitable."""

    backend: str

    async def put(self, key: str, data: bytes, content_type: str | None) -> StoredObject: ...

    async def get(self, key: str) -> bytes: ...

    async def presigned_get_url(self, key: str, *, filename: str | None = None) -> str: ...

    async def delete(self, key: str) -> None: ...


def parse_s3_uri(uri: str) -> tuple[str, str] | None:
    """Split ``s3://bucket/key/with/slashes`` → ``("bucket", "key/with/slashes")``.

    Returns ``None`` for anything that isn't an ``s3://`` URI.
    """
    if not uri or not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return bucket, key
