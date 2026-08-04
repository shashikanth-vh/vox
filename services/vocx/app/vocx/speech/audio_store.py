"""audio_store.py — where recorded captures live: MinIO/S3 first, local volume as fallback.

The platform rule is "bytes in object storage, references in the record" (the Register's
documents already work this way). Voice recordings follow it too:

    S3AudioStore     PUT to the vocx captures bucket (MinIO in compose; any S3 in prod),
                     key partitioned by month — captures/<YYYY>/<MM>/<ts>_<rm>.wav — and
                     the returned reference is the canonical  s3://bucket/key  URI, which
                     the committed interaction carries in its notes.
    LocalAudioStore  the volume directory (the pre-MinIO behaviour) — used when S3 is not
                     configured, and as the SAFETY NET when an S3 write fails: losing a
                     client recording is strictly worse than storing it locally, so a
                     failed PUT degrades, it never discards.

Retention is enforced where the bytes live: with S3 a bucket lifecycle rule (applied
best-effort at startup when ``retention_days`` > 0) expires old captures declaratively;
the local store sweeps expired files opportunistically on save. Everything here is
best-effort by design — audio archiving must never fail a capture, so every failure path
logs and degrades instead of raising.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("vocx")

AudioInput = Any  # bytes | file path — mirrors vocx_stt

# What the browser actually records → an honest extension. MediaRecorder produces
# webm/opus (Chromium) or mp4 (Safari); nothing here has ever produced WAV, yet every
# clip was archived as `.wav` and served back as `audio/wav`. Chromium sniffs past the
# lie; Edge's platform decoders take the label at its word and play SILENCE with a
# working timeline — a bug report that reads "I cannot hear the audio".
_EXT_FOR_TYPE: tuple[tuple[str, str], ...] = (
    ("webm", ".webm"), ("ogg", ".ogg"), ("opus", ".ogg"), ("mp4", ".m4a"),
    ("m4a", ".m4a"), ("aac", ".m4a"), ("mpeg", ".mp3"), ("mp3", ".mp3"), ("wav", ".wav"),
)


def ext_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    return next((ext for token, ext in _EXT_FOR_TYPE if token in ct), ".webm")


def sniff_audio_type(payload: bytes) -> str:
    """The container's own magic bytes → its media type. Trusting the bytes rather than
    a stored label also heals every clip archived as `.wav` before this fix."""
    head = payload[:16]
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"RIFF") and payload[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):
        return "audio/mpeg"
    if payload[4:8] == b"ftyp":
        return "audio/mp4"
    # Unknown container: say nothing rather than something wrong — an octet-stream makes
    # every browser fall back to its own sniffing.
    return "application/octet-stream"


def _safe_name(capture_ts: str, rm: str, ext: str = ".wav") -> tuple[str, str, str]:
    """(yyyy, mm, filename) — deterministic, filesystem- and key-safe. A client that
    sends no timestamp gets NOW, not a shared "capture_<rm>" name — a constant key
    would make every new capture silently overwrite the RM's previous recording."""
    import datetime as _dt
    if not (capture_ts or "").strip():
        capture_ts = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    ts = capture_ts.replace("-", "").replace(":", "").replace("T", "_")[:15] or "capture"
    yyyy = ts[:4] if ts[:4].isdigit() else "0000"
    mm = ts[4:6] if ts[4:6].isdigit() else "00"
    safe_rm = "".join(c for c in (rm or "") if c.isalnum()) or "rm"
    return yyyy, mm, f"{ts}_{safe_rm}{ext}"


def _read_bytes(audio: AudioInput, content_type: str = "") -> tuple[bytes | None, str]:
    """(payload, extension) from bytes or a file path; (None, ...) when unreadable.
    Raw bytes are named by the upload's own Content-Type — the clip is whatever the
    browser recorded, not whatever a default said."""
    if isinstance(audio, bytes):
        return audio, (ext_for(content_type) if content_type else ext_for(sniff_audio_type(audio)))
    if isinstance(audio, str) and os.path.exists(audio):
        try:
            with open(audio, "rb") as fh:
                return fh.read(), os.path.splitext(audio)[1] or ".wav"
        except OSError as e:
            log.warning("vocx audio: cannot read %s: %s", audio, e)
    return None, ".wav"


class LocalAudioStore:
    """The volume directory — fallback tier and the no-S3 default."""

    kind = "local"

    def __init__(self, directory: str, retention_days: int = 0) -> None:
        self.directory = directory
        self.retention_days = retention_days
        self._swept = 0.0

    def playback(self, ref: str) -> tuple[str, Any] | None:
        """("bytes", data) for a ref INSIDE the archive directory; None otherwise.
        The realpath check stops path traversal — only archived captures are servable."""
        try:
            real = os.path.realpath(ref)
            root = os.path.realpath(self.directory)
            if not real.startswith(root + os.sep):
                return None
            with open(real, "rb") as fh:
                return ("bytes", fh.read())
        except OSError:
            return None

    def save(self, audio: AudioInput, capture_ts: str, rm: str,
             content_type: str = "") -> str | None:
        payload, ext = _read_bytes(audio, content_type)
        if payload is None:
            return None
        try:
            _, _, name = _safe_name(capture_ts, rm, ext)
            os.makedirs(self.directory, exist_ok=True)
            dst = os.path.join(self.directory, name)
            with open(dst, "wb") as fh:
                fh.write(payload)
            self._sweep()
            return dst
        except OSError as e:
            log.error("vocx audio: local archive failed: %s", e)
            return None

    def _sweep(self) -> None:
        """Opportunistic retention for the local tier (at most hourly)."""
        if not self.retention_days or time.monotonic() - self._swept < 3600:
            return
        self._swept = time.monotonic()
        cutoff = time.time() - self.retention_days * 86400
        try:
            for name in os.listdir(self.directory):
                path = os.path.join(self.directory, name)
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except OSError as e:  # pragma: no cover — never let cleanup hurt a capture
            log.warning("vocx audio: retention sweep failed: %s", e)


class S3AudioStore:
    """MinIO/S3 captures bucket. Lazy client, thread-safe, local fallback on failure."""

    kind = "s3"

    def __init__(self, *, bucket: str, endpoint_url: str, access_key_id: str,
                 secret_access_key: str, auto_create_bucket: bool = True,
                 retention_days: int = 0, prefix: str = "captures",
                 public_endpoint_url: str = "", presign: bool = False,
                 fallback: LocalAudioStore | None = None) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        # Presigned URLs are opened by the BROWSER, which cannot resolve in-cluster
        # hostnames — sign against the public endpoint when one is configured.
        self.public_endpoint_url = public_endpoint_url or endpoint_url
        # Playback default is STREAMING the bytes through VocX (HTTPS via the edge, same
        # auth as every route). Presigned URLs are opt-in: the page is served over HTTPS,
        # so a plain-http MinIO link is blocked as mixed content by every modern browser
        # — presign only when object storage is properly reachable over TLS.
        self.presign = presign
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.auto_create_bucket = auto_create_bucket
        self.retention_days = retention_days
        self.prefix = prefix.strip("/")
        self.fallback = fallback
        self._client: Any = None
        self._lock = threading.Lock()

    # -- client / bucket ------------------------------------------------------
    def _s3(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3
                from botocore.config import Config
                self._client = boto3.client(
                    "s3", endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    config=Config(s3={"addressing_style": "path"},
                                  retries={"max_attempts": 3, "mode": "standard"},
                                  connect_timeout=5, read_timeout=30))
                self._ensure_bucket(self._client)
            return self._client

    def _ensure_bucket(self, client: Any) -> None:
        from botocore.exceptions import ClientError
        try:
            client.head_bucket(Bucket=self.bucket)
        except ClientError:
            if not self.auto_create_bucket:
                raise
            client.create_bucket(Bucket=self.bucket)
            log.info("vocx audio: created bucket %s", self.bucket)
        self._apply_lifecycle(client)

    def _apply_lifecycle(self, client: Any) -> None:
        """Retention lives WITH the bytes: a lifecycle rule, not an app cron."""
        if not self.retention_days:
            return
        try:
            client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration={"Rules": [{
                    "ID": "vocx-captures-retention",
                    "Status": "Enabled",
                    "Filter": {"Prefix": self.prefix + "/"},
                    "Expiration": {"Days": self.retention_days},
                }]})
        except Exception as e:  # noqa: BLE001 — some S3 impls lack lifecycle; not fatal
            log.warning("vocx audio: could not apply lifecycle rule: %s", e)

    def _public_s3(self) -> Any:
        import boto3
        from botocore.config import Config
        return boto3.client(
            "s3", endpoint_url=self.public_endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"))

    def playback(self, ref: str, expires_s: int = 900) -> tuple[str, Any] | None:
        """Playback for a ref in OUR bucket+prefix; local refs go to the fallback tier.
        Default: ("bytes", data) — the audio streams through VocX itself, so it rides
        the edge's HTTPS and the normal auth path. With presign=True: ("url", presigned
        GET) against the public endpoint. A ref pointing at any other bucket/prefix is
        refused — this must never become a generic S3 reader/presigner."""
        if not ref.startswith("s3://"):
            return self.fallback.playback(ref) if self.fallback else None
        bucket, _, key = ref[5:].partition("/")
        if bucket != self.bucket or not key.startswith(self.prefix + "/"):
            log.warning("vocx audio: playback refused for foreign ref %r", ref)
            return None
        if self.presign:
            try:
                url = self._public_s3().generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=max(60, min(expires_s, 3600)))
                return ("url", url)
            except Exception as e:  # noqa: BLE001
                log.error("vocx audio: presign failed for %r: %s", ref, e)
                return None
        try:
            obj = self._s3().get_object(Bucket=bucket, Key=key)
            return ("bytes", obj["Body"].read())
        except Exception as e:  # noqa: BLE001
            log.error("vocx audio: fetch failed for %r: %s", ref, e)
            return None

    # -- save -----------------------------------------------------------------
    def save(self, audio: AudioInput, capture_ts: str, rm: str,
             content_type: str = "") -> str | None:
        payload, ext = _read_bytes(audio, content_type)
        if payload is None:
            return None
        yyyy, mm, name = _safe_name(capture_ts, rm, ext)
        key = f"{self.prefix}/{yyyy}/{mm}/{name}"
        try:
            self._s3().put_object(Bucket=self.bucket, Key=key, Body=payload,
                                  ContentType=sniff_audio_type(payload))
            return f"s3://{self.bucket}/{key}"
        except Exception as e:  # noqa: BLE001 — degrade, never discard the recording
            log.error("vocx audio: S3 put failed (%s) — falling back to local", e)
            if self.fallback is not None:
                return self.fallback.save(payload, capture_ts, rm, content_type)
            return None


def build_audio_store(settings: Any) -> S3AudioStore | LocalAudioStore | None:
    """S3 when configured, else the volume directory, else off. Never raises."""
    tokens_dir = getattr(settings, "tokens_dir", "") or ""
    retention = int(getattr(settings, "audio_retention_days", 0) or 0)
    local = (LocalAudioStore(os.path.join(tokens_dir, "captures"), retention)
             if tokens_dir else None)
    endpoint = getattr(settings, "s3_endpoint_url", "") or ""
    bucket = getattr(settings, "s3_bucket", "") or ""
    if endpoint and bucket:
        return S3AudioStore(
            bucket=bucket, endpoint_url=endpoint,
            access_key_id=getattr(settings, "s3_access_key_id", "") or "",
            secret_access_key=getattr(settings, "s3_secret_access_key", "") or "",
            auto_create_bucket=bool(getattr(settings, "s3_auto_create_bucket", True)),
            public_endpoint_url=getattr(settings, "s3_public_endpoint_url", "") or "",
            presign=bool(getattr(settings, "audio_presign", False)),
            retention_days=retention, fallback=local)
    return local


__all__ = ["LocalAudioStore", "S3AudioStore", "build_audio_store"]
