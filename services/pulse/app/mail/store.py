"""The mailbox reader's memory, persisted atomically, one section per tenant.

Tokens are the only secret here and they are encrypted at rest (Fernet). The key
comes from ``PULSE_MAIL_TOKEN_KEY``; when that is blank a key file is generated next
to the store on first use, so a fresh deployment works and the key lives only on
the data volume, never in the repository or the compose file.

Bodies are NOT kept: a filed conversation remembers its identity, the message it
was filed up to and the interaction it became. A queued conversation keeps the
little the reviewer needs to place it (subject, parties, date, a short excerpt).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("pulse.mail")

QUEUE_MAX = 2000               # per tenant; oldest entries fall off first
CONVERSATION_MEMORY_MAX = 50_000


class MailStoreError(RuntimeError):
    """The store cannot be written (permissions, disk) — reported, never silent."""


class MailStore:
    def __init__(self, path: str, token_key: str = "") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._fernet = Fernet(self._key(token_key))
        self.load()

    # -- the key ---------------------------------------------------------------
    def _key(self, given: str) -> bytes:
        if given.strip():
            return given.strip().encode()
        key_path = os.path.join(os.path.dirname(self.path) or ".", "mail.key")
        try:
            with open(key_path, "rb") as fh:
                key = fh.read().strip()
            if key:
                return key
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("pulse_mail_key_unreadable", extra={"path": key_path, "error": str(exc)})
        key = Fernet.generate_key()
        try:
            os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
        except OSError as exc:
            # No durable key: tokens would not survive a restart. Still usable now.
            log.error("pulse_mail_key_unwritable", extra={"path": key_path, "error": str(exc)})
        return key

    def seal(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def open_token(self, sealed: str) -> str | None:
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except (InvalidToken, ValueError, TypeError):
            return None

    # -- persistence -----------------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as exc:
            log.error("pulse_mail_store_unreadable", extra={"path": self.path, "error": str(exc)})
            self._data = {}

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.error("pulse_mail_save_failed", extra={"error": str(exc)})
                raise MailStoreError(str(exc)) from exc

    def probe(self) -> tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path + ".probe", "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(self.path + ".probe")
            return True, ""
        except OSError as exc:
            return False, str(exc)

    # -- sections --------------------------------------------------------------
    def tenant(self, tenant: str) -> dict[str, Any]:
        with self._lock:
            t = self._data.setdefault(tenant.upper(), {})
            t.setdefault("mailboxes", {})      # email -> connection record
            t.setdefault("conversations", {})  # conversation key -> what was filed
            t.setdefault("queue", {})          # conversation key -> waiting for a person
            t.setdefault("ignored", {})        # conversation key -> when
            t.setdefault("run", {})
            return t

    def mailbox(self, tenant: str, email: str) -> dict[str, Any] | None:
        with self._lock:
            return self.tenant(tenant)["mailboxes"].get(email.lower())

    def mailboxes(self, tenant: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self.tenant(tenant)["mailboxes"])

    def put_mailbox(self, tenant: str, email: str, record: dict[str, Any]) -> None:
        with self._lock:
            self.tenant(tenant)["mailboxes"][email.lower()] = record

    def drop_mailbox(self, tenant: str, email: str) -> bool:
        with self._lock:
            return self.tenant(tenant)["mailboxes"].pop(email.lower(), None) is not None

    def conversation(self, tenant: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self.tenant(tenant)["conversations"].get(key)

    def put_conversation(self, tenant: str, key: str, record: dict[str, Any]) -> None:
        with self._lock:
            convs = self.tenant(tenant)["conversations"]
            convs[key] = record
            if len(convs) > CONVERSATION_MEMORY_MAX:
                oldest = sorted(convs, key=lambda k: convs[k].get("filed_at", 0))
                for k in oldest[: len(convs) - CONVERSATION_MEMORY_MAX]:
                    del convs[k]

    def queue(self, tenant: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self.tenant(tenant)["queue"])

    def enqueue(self, tenant: str, key: str, entry: dict[str, Any]) -> None:
        with self._lock:
            q = self.tenant(tenant)["queue"]
            q[key] = entry
            if len(q) > QUEUE_MAX:
                oldest = sorted(q, key=lambda k: q[k].get("queued_at", 0))
                for k in oldest[: len(q) - QUEUE_MAX]:
                    del q[k]

    def dequeue(self, tenant: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self.tenant(tenant)["queue"].pop(key, None)

    def ignore(self, tenant: str, key: str) -> None:
        with self._lock:
            self.tenant(tenant)["ignored"][key] = time.time()

    def is_ignored(self, tenant: str, key: str) -> bool:
        with self._lock:
            return key in self.tenant(tenant)["ignored"]

    def expire_queue(self, tenant: str, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self._lock:
            q = self.tenant(tenant)["queue"]
            gone = [k for k, e in q.items() if e.get("queued_at", 0) < cutoff]
            for k in gone:
                del q[k]
            return len(gone)
