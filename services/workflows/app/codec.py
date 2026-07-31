"""Sensitive-payload encryption for Temporal histories.

Workflow inputs, activity arguments, and results all live in Temporal's event history —
outside the Register's PostgreSQL and its RLS. With ``WORKFLOWS_PAYLOAD_ENCRYPTION_KEY``
set, every payload is encrypted client-side (worker AND orchestrator) with AES-256-GCM
before it reaches the Temporal server, so the history stores only ciphertext; the Temporal
UI shows opaque blobs unless a codec server is configured. Without the key the default
(plaintext) converter is used — the dev posture.

Key format: base64url, exactly 32 bytes once decoded (AES-256). Rotation: the 4-byte key id
prefix in the metadata means a NEW key can be introduced while the old one is still
accepted for decode — pass previous keys via ``retired`` when rotating.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Iterable, Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec, default as default_converter

_ENCODING = b"binary/encrypted"


def _key_id(key: bytes) -> bytes:
    # A short, non-reversible identifier for the key — enough to select the right one on
    # decode, useless for recovering key material.
    return hashlib.sha256(key).digest()[:4]


class EncryptionCodec(PayloadCodec):
    """AES-256-GCM payload codec. Encode always uses the CURRENT key; decode accepts the
    current key plus any retired ones (rotation window)."""

    def __init__(self, current: bytes, retired: Iterable[bytes] = ()) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(current) != 32:
            raise ValueError("payload encryption key must decode to exactly 32 bytes")
        self._current_id = _key_id(current)
        self._keys: dict[bytes, AESGCM] = {self._current_id: AESGCM(current)}
        for key in retired:
            if len(key) != 32:
                raise ValueError("retired payload key must decode to exactly 32 bytes")
            self._keys.setdefault(_key_id(key), AESGCM(key))

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out: list[Payload] = []
        for p in payloads:
            nonce = os.urandom(12)
            sealed = self._keys[self._current_id].encrypt(nonce, p.SerializeToString(), None)
            out.append(Payload(
                metadata={"encoding": _ENCODING, "key_id": self._current_id},
                data=nonce + sealed))
        return out

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out: list[Payload] = []
        for p in payloads:
            if p.metadata.get("encoding") != _ENCODING:
                out.append(p)                       # plaintext from before encryption was on
                continue
            aead = self._keys.get(bytes(p.metadata.get("key_id", b"")))
            if aead is None:
                raise ValueError("payload encrypted with an unknown key id — configure the "
                                 "retired-keys list for rotation")
            opened = aead.decrypt(bytes(p.data[:12]), bytes(p.data[12:]), None)
            inner = Payload()
            inner.ParseFromString(opened)
            out.append(inner)
        return out


def build_data_converter(encryption_key_b64: str) -> DataConverter:
    """The data converter for BOTH the worker and the orchestrator API client. Empty key →
    Temporal's default (plaintext) converter; set → the same converter with the encryption
    codec layered on, so business data is ciphertext at rest in Temporal."""
    if not encryption_key_b64:
        return default_converter()
    key = base64.urlsafe_b64decode(encryption_key_b64 + "=" * (-len(encryption_key_b64) % 4))
    import dataclasses

    return dataclasses.replace(default_converter(), payload_codec=EncryptionCodec(key))
