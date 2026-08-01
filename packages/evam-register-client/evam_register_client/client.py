"""Async + sync clients for the PRISM Register.

    from evam_register_client import AsyncRegisterClient

    async with AsyncRegisterClient(base_url="http://register:8000", api_key="...",
                                   tenant="EVAM", actor="vox") as reg:
        ent = await reg.create("entities", {"code": "ACME", "legal_name": "Acme Ltd"})
        await reg.log_interaction("Entity", ent["id"], "Phone Call", source="VOX",
                                  summary="Intro call", transcript="...")

Every call: sends auth headers, forwards/mints a correlation id, retries transient
failures with backoff, attaches an Idempotency-Key to creates, and raises a typed error on
failure. See ``README.md``.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
from typing import Any

import httpx

from evam_register_client._core import (
    backoff_delay,
    build_headers,
    new_idempotency_key,
    prepare_request_id,
    should_retry,
)
from evam_register_client.config import RegisterClientConfig
from evam_register_client.errors import RegisterError, error_from_payload
from evam_register_client.models import Page


class _Plan:
    """A prepared request — pure data, shared by the async client and sync facade."""

    __slots__ = ("method", "path", "params", "json", "idempotency_key", "if_match",
                 "request_id", "idempotent_write", "extra_headers")

    def __init__(self, method: str, path: str, *, params=None, json=None,
                 idempotency_key=None, if_match=None, request_id=None,
                 extra_headers=None) -> None:
        self.method = method.upper()
        self.path = path
        self.params = params
        self.json = json
        self.idempotency_key = idempotency_key
        self.if_match = if_match
        self.request_id = prepare_request_id(request_id)
        self.idempotent_write = bool(idempotency_key or if_match)
        # Per-call header overrides (e.g. attributing a conversion to the human approver
        # via X-Actor) — merged AFTER the config defaults so they win.
        self.extra_headers = extra_headers


class AsyncRegisterClient:
    """Full-featured async client. Reuse one instance per process (it pools connections)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        tenant: str | None = None,
        actor: str | None = None,
        config: RegisterClientConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        cfg = config or RegisterClientConfig()
        if base_url is not None:
            cfg.base_url = base_url
        if api_key is not None:
            cfg.api_key = api_key
        if tenant is not None:
            cfg.tenant = tenant
        if actor is not None:
            cfg.actor = actor
        self.config = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=httpx.Timeout(cfg.read_timeout_s, connect=cfg.connect_timeout_s),
            limits=httpx.Limits(max_connections=cfg.max_connections,
                                max_keepalive_connections=cfg.max_keepalive_connections),
            transport=transport,
        )

    # -- lifecycle --------------------------------------------------------
    async def __aenter__(self) -> "AsyncRegisterClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- core send + retry ------------------------------------------------
    async def _send(self, plan: _Plan) -> Any:
        cfg = self.config
        headers = build_headers(
            api_key=cfg.api_key, tenant=cfg.tenant, actor=cfg.actor,
            idempotency_key=plan.idempotency_key, if_match=plan.if_match,
            request_id=plan.request_id, content_type_json=plan.json is not None,
            extra={**(cfg.extra_headers or {}), **(plan.extra_headers or {})} or None,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client.request(
                    plan.method, plan.path, params=plan.params, json=plan.json, headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < cfg.retry_max_attempts and should_retry(
                    method=plan.method, status=None, is_network_error=True,
                    idempotent_write=plan.idempotent_write,
                ):
                    await asyncio.sleep(backoff_delay(
                        attempt, base=cfg.retry_base_delay_s, cap=cfg.retry_max_delay_s))
                    continue
                raise RegisterError(f"transport error calling {plan.method} {plan.path}: {exc}",
                                    request_id=plan.request_id) from exc

            if attempt < cfg.retry_max_attempts and should_retry(
                method=plan.method, status=resp.status_code, is_network_error=False,
                idempotent_write=plan.idempotent_write,
            ):
                retry_after = _retry_after(resp)
                await asyncio.sleep(backoff_delay(
                    attempt, base=cfg.retry_base_delay_s, cap=cfg.retry_max_delay_s,
                    retry_after=retry_after))
                continue
            return _handle(resp)

    # -- generic resource operations -------------------------------------
    async def create(self, resource: str, data: dict, *, idempotency_key: str | None = None,
                     request_id: str | None = None) -> dict:
        key = idempotency_key or (new_idempotency_key() if self.config.auto_idempotency else None)
        return await self._send(_Plan("POST", f"/v1/{resource}", json=data,
                                      idempotency_key=key, request_id=request_id))

    async def get(self, resource: str, obj_id: str, *, include_deleted: bool = False,
                  request_id: str | None = None) -> dict:
        params = {"include_deleted": "true"} if include_deleted else None
        return await self._send(_Plan("GET", f"/v1/{resource}/{obj_id}", params=params,
                                      request_id=request_id))

    async def list(self, resource: str, *, limit: int = 50, cursor: str | None = None,
                   q: str | None = None, with_total: bool = False,
                   include_deleted: bool = False, request_id: str | None = None,
                   **filters: Any) -> Page:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if q:
            params["q"] = q
        if with_total:
            params["with_total"] = "true"
        if include_deleted:
            params["include_deleted"] = "true"
        params.update({k: v for k, v in filters.items() if v is not None})
        body = await self._send(_Plan("GET", f"/v1/{resource}", params=params,
                                      request_id=request_id))
        return Page.from_body(body)

    async def iterate(self, resource: str, *, page_size: int = 200,
                      request_id: str | None = None, **filters: Any):
        """Async-iterate every row across pages (keyset cursor under the hood)."""
        cursor: str | None = None
        while True:
            page = await self.list(resource, limit=page_size, cursor=cursor,
                                   request_id=request_id, **filters)
            for item in page.items:
                yield item
            if not page.next_cursor:
                break
            cursor = page.next_cursor

    async def update(self, resource: str, obj_id: str, data: dict, *,
                     expected_version: int | None = None, request_id: str | None = None) -> dict:
        return await self._send(_Plan("PATCH", f"/v1/{resource}/{obj_id}", json=data,
                                      if_match=expected_version, request_id=request_id))

    async def delete(self, resource: str, obj_id: str, *, expected_version: int | None = None,
                     request_id: str | None = None) -> None:
        await self._send(_Plan("DELETE", f"/v1/{resource}/{obj_id}",
                               if_match=expected_version, request_id=request_id))

    async def restore(self, resource: str, obj_id: str, *, request_id: str | None = None) -> dict:
        return await self._send(_Plan("POST", f"/v1/{resource}/{obj_id}/restore",
                                      request_id=request_id))

    # -- VOX: interactions ------------------------------------------------
    async def log_interaction(self, subject_type: str, subject_id: str, interaction_type: str,
                              *, source: str = "Manual", idempotency_key: str | None = None,
                              request_id: str | None = None, **fields: Any) -> dict:
        payload = {"subject_type": subject_type, "subject_id": subject_id,
                   "interaction_type": interaction_type, "source": source,
                   **{k: v for k, v in fields.items() if v is not None}}
        key = idempotency_key or (new_idempotency_key() if self.config.auto_idempotency else None)
        return await self._send(_Plan("POST", "/v1/interactions", json=payload,
                                      idempotency_key=key, request_id=request_id))

    # -- CIPHER: financials ----------------------------------------------
    async def create_financial_version(self, entity_id: str, statement_type: str,
                                       period_end: str, *, idempotency_key: str | None = None,
                                       request_id: str | None = None, **fields: Any) -> dict:
        payload = {"entity_id": entity_id, "statement_type": statement_type,
                   "period_end": period_end,
                   **{k: v for k, v in fields.items() if v is not None}}
        key = idempotency_key or (new_idempotency_key() if self.config.auto_idempotency else None)
        return await self._send(_Plan("POST", "/v1/financials", json=payload,
                                      idempotency_key=key, request_id=request_id))

    async def financial_history(self, entity_id: str, statement_type: str, *,
                                period_end: str | None = None,
                                request_id: str | None = None) -> builtins.list[dict]:
        params = {"entity_id": entity_id, "statement_type": statement_type}
        if period_end:
            params["period_end"] = period_end
        return await self._send(_Plan("GET", "/v1/financials/history", params=params,
                                      request_id=request_id))

    # -- PULSE: external intelligence ------------------------------------
    async def convert_lead(self, lead_id: str, *, is_lending: bool = False,
                           is_syndication: bool = False, is_asset_mon: bool = False,
                           product_type: str | None = None, amount_cr: float | None = None,
                           rm: str | None = None, analyst: str | None = None,
                           rm_id: str | None = None, analyst_id: str | None = None,
                           note: str | None = None,
                           approved_by: str | None = None,
                           idempotency_key: str | None = None,
                           request_id: str | None = None) -> dict:
        """Atomically convert a lead → deal (+ product lines) in ONE Register
        transaction — no client-side compensation.

        ``rm_id`` / ``analyst_id`` (Access user ids) auto-create the line assignments on the
        server. ``approved_by`` records the human approver as conversion provenance in the
        body (a service key can never masquerade as a person via X-Actor, so it is data, not
        the audit actor)."""
        payload = {"is_lending": is_lending, "is_syndication": is_syndication,
                   "is_asset_mon": is_asset_mon, "product_type": product_type,
                   "amount_cr": amount_cr, "rm": rm, "analyst": analyst,
                   "rm_id": rm_id, "analyst_id": analyst_id, "note": note,
                   "approved_by": approved_by}
        return await self._send(_Plan(
            "POST", f"/v1/leads/{lead_id}/convert",
            json={k: v for k, v in payload.items() if v is not None},
            idempotency_key=idempotency_key, request_id=request_id))

    async def create_intelligence(self, entity_id: str, intel_type: str, *,
                                  signal: str | None = None, idempotency_key: str | None = None,
                                  request_id: str | None = None, **fields: Any) -> dict:
        payload = {"entity_id": entity_id, "intel_type": intel_type,
                   **({"signal": signal} if signal else {}),
                   **{k: v for k, v in fields.items() if v is not None}}
        key = idempotency_key or (new_idempotency_key() if self.config.auto_idempotency else None)
        return await self._send(_Plan("POST", "/v1/external-intelligence", json=payload,
                                      idempotency_key=key, request_id=request_id))

    async def acknowledge_intelligence(self, intel_id: str, *,
                                       request_id: str | None = None) -> dict:
        return await self._send(_Plan("POST", f"/v1/external-intelligence/{intel_id}/acknowledge",
                                      request_id=request_id))

    async def dismiss_intelligence(self, intel_id: str, *, request_id: str | None = None) -> dict:
        return await self._send(_Plan("POST", f"/v1/external-intelligence/{intel_id}/dismiss",
                                      request_id=request_id))

    # -- common reads / admin --------------------------------------------
    async def ref(self, category: str | None = None, *, request_id: str | None = None) -> Any:
        path = f"/v1/ref/{category}" if category else "/v1/ref"
        return await self._send(_Plan("GET", path, request_id=request_id))

    async def dossier(self, entity_id: str, *, request_id: str | None = None) -> dict:
        return await self._send(_Plan("GET", f"/v1/entities/{entity_id}/dossier",
                                      request_id=request_id))

    async def lender_matrix(self, entity_id: str, *, request_id: str | None = None) -> dict:
        return await self._send(_Plan("GET", f"/v1/entities/{entity_id}/lender-matrix",
                                      request_id=request_id))

    async def get_settings(self, *, request_id: str | None = None) -> dict:
        return await self._send(_Plan("GET", "/v1/settings", request_id=request_id))

    async def put_settings(self, settings: dict, *, request_id: str | None = None) -> dict:
        return await self._send(_Plan("PUT", "/v1/settings", json={"settings": settings},
                                      request_id=request_id))

    async def create_tenant(self, code: str, name: str, *,
                            request_id: str | None = None) -> dict:
        return await self._send(_Plan("POST", "/v1/tenants", json={"code": code, "name": name},
                                      idempotency_key=None, request_id=request_id))

    async def record_decision(self, workflow_id: str, decision: str, *,
                              lead_id: str | None = None, note: str | None = None,
                              extra_headers: dict[str, str] | None = None,
                              request_id: str | None = None) -> dict:
        """Record a lead-conversion decision on the dedicated single-winner resource. The
        server enforces one decision per (tenant, workflow): a replay of the same decision
        returns the original, the opposite decision raises ``ConflictError`` (409). Provenance
        is taken from the delegated approver context in ``extra_headers`` (X-Internal-Context),
        not any field here."""
        body = {"workflow_id": workflow_id, "decision": decision}
        if lead_id is not None:
            body["lead_id"] = lead_id
        if note is not None:
            body["note"] = note
        return await self._send(_Plan("POST", "/v1/internal/decisions", json=body,
                                      extra_headers=extra_headers, request_id=request_id))

    async def get_decision(self, workflow_id: str, *, request_id: str | None = None) -> dict:
        """Read the recorded decision for a workflow. Raises ``NotFoundError`` (404) when no
        decision has been recorded; transport/5xx errors propagate so the caller can retry.

        The workflow id is percent-encoded so retry ids (and any future id shape) address the
        right row instead of being truncated at an unsafe character."""
        from urllib.parse import quote

        return await self._send(_Plan(
            "GET", f"/v1/internal/decisions/{quote(workflow_id, safe='')}",
            request_id=request_id))

    async def claim_deliveries(self, *, limit: int = 20, lease_seconds: int = 60,
                               request_id: str | None = None) -> builtins.list[dict]:
        """Reconciler: atomically claim a batch of due, pending decision deliveries (leased for
        ``lease_seconds``). Returns ``[{workflow_id, decision, attempts}, ...]``."""
        resp = await self._send(_Plan(
            "POST", "/v1/internal/decisions/deliveries/claim",
            json={"limit": limit, "lease_seconds": lease_seconds}, request_id=request_id))
        return list(resp.get("claimed", []))

    async def update_delivery(self, workflow_id: str, status: str, *, claim_token: str,
                              error: str | None = None, backoff_seconds: int = 60,
                              request_id: str | None = None) -> dict:
        """Reconciler: mark a delivery ``applied`` / ``retry`` (with backoff) / ``dead``. The
        ``claim_token`` from the claim must still be current, else the update is a no-op (a
        stalled claimant can't overwrite a re-claimed or terminal row)."""
        body: dict[str, Any] = {"status": status, "claim_token": claim_token,
                                "backoff_seconds": backoff_seconds}
        if error is not None:
            body["error"] = error
        return await self._send(_Plan(
            "POST", f"/v1/internal/decisions/{workflow_id}/delivery", json=body,
            request_id=request_id))

    # -- Notifications (increment 7) --------------------------------------
    async def create_notification(self, recipient: str, event: str, title: str, *,
                                  severity: str = "info", body: str | None = None,
                                  subject_type: str | None = None,
                                  subject_id: str | None = None,
                                  workflow_id: str | None = None,
                                  dedupe_key: str | None = None,
                                  channels: builtins.list[str] | None = None,
                                  sms_to: str | None = None,
                                  webhook_url: str | None = None,
                                  recipient_role: str | None = None,
                                  meta: dict | None = None,
                                  request_id: str | None = None) -> dict:
        """Create a durable notification (the in-app inbox row) plus one delivery-outbox
        row per external channel — idempotent by ``dedupe_key`` (a replay returns the
        original row instead of double-notifying)."""
        payload = {"recipient": recipient, "event": event, "title": title,
                   "severity": severity, "body": body, "subject_type": subject_type,
                   "subject_id": subject_id, "workflow_id": workflow_id,
                   "dedupe_key": dedupe_key, "channels": channels or [],
                   "sms_to": sms_to, "webhook_url": webhook_url,
                   "recipient_role": recipient_role, "meta": meta}
        return await self._send(_Plan(
            "POST", "/v1/internal/notifications",
            json={k: v for k, v in payload.items() if v is not None},
            request_id=request_id))

    async def claim_notification_deliveries(self, *, limit: int = 20,
                                            lease_seconds: int = 60,
                                            request_id: str | None = None
                                            ) -> builtins.list[dict]:
        """Notifier: atomically claim a batch of due, pending notification deliveries
        (leased). Each claim carries channel/target and the rendered content."""
        resp = await self._send(_Plan(
            "POST", "/v1/internal/notifications/deliveries/claim",
            json={"limit": limit, "lease_seconds": lease_seconds},
            request_id=request_id))
        return list(resp.get("claimed", []))

    async def update_notification_delivery(self, delivery_id: str, status: str, *,
                                           claim_token: str, error: str | None = None,
                                           backoff_seconds: int = 60,
                                           request_id: str | None = None) -> dict:
        """Notifier: mark a delivery ``delivered`` / ``retry`` (backoff) / ``dead`` —
        fenced by the claim token exactly like the decision outbox."""
        body: dict[str, Any] = {"status": status, "claim_token": claim_token,
                                "backoff_seconds": backoff_seconds}
        if error is not None:
            body["error"] = error
        return await self._send(_Plan(
            "POST", f"/v1/internal/notifications/deliveries/{delivery_id}", json=body,
            request_id=request_id))

    async def sweep_document_expiry(self, *, warn_days: int = 7, limit: int = 500,
                                    request_id: str | None = None) -> dict:
        """Run the document expiry sweep: lapsed documents are marked ``Expired`` and
        returned; documents expiring within ``warn_days`` are reported untouched."""
        return await self._send(_Plan(
            "POST", "/v1/internal/documents/expiry-sweep",
            json={"warn_days": warn_days, "limit": limit}, request_id=request_id))

    # -- Covenants + EWS (increment 8) ------------------------------------
    async def run_covenant_sweep(self, *, horizon_days: int = 30, limit: int = 500,
                                 request_id: str | None = None) -> dict:
        """Run the covenant sweep: generate the schedule's due observations (idempotent),
        mark newly-overdue submissions, and expire lapsed waivers (each reported once).
        Returns ``{generated, overdue: [...], waivers_expired: [...]}``."""
        return await self._send(_Plan(
            "POST", "/v1/internal/covenants/run-sweep",
            json={"horizon_days": horizon_days, "limit": limit},
            request_id=request_id))

    async def auto_escalate_ews(self, case_id: str, reason: str, *,
                                workflow_id: str | None = None,
                                request_id: str | None = None) -> dict:
        """Escalate an EWS case whose investigation SLA lapsed (service plumbing;
        idempotent — an already-escalated or closed case is returned unchanged)."""
        body: dict[str, Any] = {"reason": reason}
        if workflow_id:
            body["workflow_id"] = workflow_id
        return await self._send(_Plan(
            "POST", f"/v1/internal/ews-cases/{case_id}/auto-escalate", json=body,
            request_id=request_id))

    async def redrive_delivery(self, workflow_id: str, *, reason: str,
                               ticket: str | None = None,
                               extra_headers: dict[str, str] | None = None,
                               request_id: str | None = None) -> dict:
        """Recover a dead-lettered delivery back to pending. An ADMIN action — pass the verified
        Admin's delegated context in ``extra_headers`` (X-Internal-Context); the Register refuses
        it otherwise. A non-empty ``reason`` is MANDATORY and an optional ``ticket`` reference may
        be given; both are preserved (with the previous dead-letter cause) in the immutable audit
        event that names the admin."""
        body: dict[str, Any] = {"reason": reason}
        if ticket is not None:
            body["ticket"] = ticket
        return await self._send(_Plan(
            "POST", f"/v1/internal/decisions/{workflow_id}/redrive", json=body,
            extra_headers=extra_headers, request_id=request_id))

    async def delivery_stats(self, *, request_id: str | None = None) -> dict:
        """Reconciler metrics: ``{pending, applied, dead, aged_pending}`` for the tenant."""
        return await self._send(_Plan("GET", "/v1/internal/decisions/deliveries/stats",
                                      request_id=request_id))

    async def notification_delivery_stats(self, *, request_id: str | None = None) -> dict:
        """Notifier metrics: ``{pending, delivered, dead, aged_pending}`` for the tenant."""
        return await self._send(_Plan("GET", "/v1/internal/notifications/deliveries/stats",
                                      request_id=request_id))

    async def internal_tenants(self, *, request_id: str | None = None) -> builtins.list[str]:
        """Reconciler: the active tenant codes to scan for pending deliveries."""
        resp = await self._send(_Plan("GET", "/v1/internal/tenants", request_id=request_id))
        return list(resp.get("tenants", []))

    async def audit(self, *, resource_type: str | None = None, resource_id: str | None = None,
                    limit: int = 100, request_id: str | None = None) -> builtins.list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if resource_type:
            params["resource_type"] = resource_type
        if resource_id:
            params["resource_id"] = resource_id
        return await self._send(_Plan("GET", "/v1/audit", params=params, request_id=request_id))

    async def healthy(self) -> bool:
        try:
            await self._send(_Plan("GET", "/healthz"))
            return True
        except RegisterError:
            return False

    async def ready(self) -> bool:
        try:
            await self._send(_Plan("GET", "/readyz"))
            return True
        except RegisterError:
            return False


class RegisterClient:
    """Synchronous facade over :class:`AsyncRegisterClient` for scripts / cron / batch
    verticals. Wraps every coroutine method on a private event loop; async generators
    (``iterate``) are drained to a list. Not thread-safe — one instance per thread."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._loop = asyncio.new_event_loop()
        self._async = AsyncRegisterClient(*args, **kwargs)

    def __enter__(self) -> "RegisterClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._loop.run_until_complete(self._async.aclose())
        self._loop.close()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._async, name)
        if inspect.isasyncgenfunction(attr):
            def gen_wrapper(*a: Any, **k: Any) -> list:
                async def _drain() -> list:
                    return [x async for x in attr(*a, **k)]
                return self._loop.run_until_complete(_drain())
            return gen_wrapper
        if inspect.iscoroutinefunction(attr):
            def wrapper(*a: Any, **k: Any) -> Any:
                return self._loop.run_until_complete(attr(*a, **k))
            return wrapper
        return attr


# --------------------------------------------------------------------------- #
def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _handle(resp: httpx.Response) -> Any:
    if 200 <= resp.status_code < 300:
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()
    try:
        body: Any = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON error body
        body = resp.text
    raise error_from_payload(resp.status_code, body, dict(resp.headers))
