"""The register-side of the pipeline: a thin, honest HTTP client for the VOX
conversation endpoints.

Timeouts are explicit, retries are bounded and only for transport faults (a 4xx
means the request is WRONG and must not be hammered), and a 409 from the guarded
status machine surfaces as its own error — the runner treats it as "someone else
advanced this row", not as a failure to hide.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("vox.pipeline.register")

_TRANSPORT_RETRIES = 3
_BACKOFF_BASE_S = 0.5


class RegisterConflict(RuntimeError):
    """The register's status machine refused the move (409) — the row moved on
    under us. Re-read and reconcile; do not retry the same write."""


class RegisterRefusal(RuntimeError):
    """A 4xx other than conflict: the request itself is wrong. Never retried."""


class RegisterClient:
    def __init__(self, base_url: str, api_key: str, tenant: str,
                 timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant = tenant
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        last_exc: Exception | None = None
        for attempt in range(1, _TRANSPORT_RETRIES + 1):
            req = urllib.request.Request(url, data=payload, method=method, headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
                "X-Tenant": self.tenant,
                "X-Actor": "vox-pipeline",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 409:
                    raise RegisterConflict(detail) from exc
                if 400 <= exc.code < 500:
                    raise RegisterRefusal(f"{exc.code}: {detail}") from exc
                last_exc = RuntimeError(f"register {exc.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
            if attempt < _TRANSPORT_RETRIES:
                sleep = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.warning("register %s %s attempt %d failed (%s); retrying in %.1fs",
                            method, path, attempt, last_exc, sleep)
                time.sleep(sleep)
        raise RuntimeError(f"register unreachable after {_TRANSPORT_RETRIES} attempts: "
                           f"{last_exc}") from last_exc

    # ------------------------------------------------------------------- the API

    def get(self, conversation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/vox/conversations/{conversation_id}")

    def patch(self, conversation_id: str, **fields: Any) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/v1/vox/conversations/{conversation_id}/pipeline", fields)

    def create(self, **fields: Any) -> dict[str, Any]:
        return self._request("POST", "/v1/vox/conversations", fields)

    def consent(self, certification_text: str, device_meta: dict | None = None) -> dict:
        return self._request("POST", "/v1/vox/consents", {
            "certification_text": certification_text, "device_meta": device_meta})
