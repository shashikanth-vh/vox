"""S3 / MinIO object storage backend (boto3).

One implementation serves both AWS S3 and MinIO — they speak the same API; MinIO just
needs an ``endpoint_url`` and path-style addressing. boto3 is synchronous, so every call
runs in a worker thread (``asyncio.to_thread``) to keep the event loop free.

The bucket is created on first use when ``auto_create_bucket`` is set, so a fresh MinIO
comes up ready without a manual step.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.core.logging import get_logger
from app.storage.base import StoredObject

log = get_logger(__name__)


class StorageError(AppError):
    """Object-storage failure surfaced as a 502 (upstream dependency)."""

    status_code = 502
    error_type = "storage_error"
    title = "Object storage error"


class S3Storage:
    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str | None,
        public_endpoint_url: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        use_ssl: bool,
        path_style: bool,
        presign_expiry_seconds: int,
        auto_create_bucket: bool,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.public_endpoint_url = public_endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.use_ssl = use_ssl
        self.path_style = path_style
        self.presign_expiry_seconds = presign_expiry_seconds
        self.auto_create_bucket = auto_create_bucket

        self._lock = threading.Lock()
        self._client: Any = None
        self._presign_client: Any = None
        self._bucket_ready = False

    # ---- client construction (lazy; created inside the worker thread) ----
    def _config(self) -> Config:
        return Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if self.path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        )

    def _new_client(self, endpoint_url: str | None) -> Any:
        return boto3.client(
            "s3",
            region_name=self.region,
            endpoint_url=endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            use_ssl=self.use_ssl,
            config=self._config(),
        )

    def _client_sync(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._new_client(self.endpoint_url)
        return self._client

    def _presign_client_sync(self) -> Any:
        # Sign against the browser-reachable endpoint when it differs from the internal one.
        if not self.public_endpoint_url or self.public_endpoint_url == self.endpoint_url:
            return self._client_sync()
        if self._presign_client is None:
            with self._lock:
                if self._presign_client is None:
                    self._presign_client = self._new_client(self.public_endpoint_url)
        return self._presign_client

    def _ensure_bucket_sync(self) -> None:
        if self._bucket_ready:
            return
        client = self._client_sync()
        try:
            client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise
            if not self.auto_create_bucket:
                raise StorageError(f"bucket '{self.bucket}' does not exist.") from exc
            kwargs: dict[str, Any] = {"Bucket": self.bucket}
            if self.region and self.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            try:
                client.create_bucket(**kwargs)
            except ClientError as create_exc:
                # Lost a race with another worker — treat "already owned" as success.
                if str(create_exc.response.get("Error", {}).get("Code", "")) not in (
                    "BucketAlreadyOwnedByYou", "BucketAlreadyExists",
                ):
                    raise
            log.info("object storage bucket created: %s", self.bucket)
        self._bucket_ready = True

    # ---- sync operations (run in a worker thread) -----------------------
    def _put_sync(self, key: str, data: bytes, content_type: str | None) -> StoredObject:
        self._ensure_bucket_sync()
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        self._client_sync().put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return StoredObject(uri=f"s3://{self.bucket}/{key}", backend="s3",
                            key=key, bucket=self.bucket)

    def _get_sync(self, key: str) -> bytes:
        resp = self._client_sync().get_object(Bucket=self.bucket, Key=key)
        body: bytes = resp["Body"].read()
        return body

    def _presign_sync(self, key: str, filename: str | None) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
        url: str = self._presign_client_sync().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=self.presign_expiry_seconds
        )
        return url

    def _delete_sync(self, key: str) -> None:
        self._client_sync().delete_object(Bucket=self.bucket, Key=key)

    # ---- async surface --------------------------------------------------
    async def put(self, key: str, data: bytes, content_type: str | None) -> StoredObject:
        try:
            return await asyncio.to_thread(self._put_sync, key, data, content_type)
        except ClientError as exc:
            raise StorageError(f"failed to store object '{key}': {exc}") from exc

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._get_sync, key)
        except ClientError as exc:
            raise StorageError(f"failed to read object '{key}': {exc}") from exc

    async def presigned_get_url(self, key: str, *, filename: str | None = None) -> str:
        try:
            return await asyncio.to_thread(self._presign_sync, key, filename)
        except ClientError as exc:
            raise StorageError(f"failed to sign URL for '{key}': {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._delete_sync, key)
        except ClientError as exc:
            raise StorageError(f"failed to delete object '{key}': {exc}") from exc
