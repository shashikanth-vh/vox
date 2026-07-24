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
                 "request_id", "idempotent_write")

    def __init__(self, method: str, path: str, *, params=None, json=None,
                 idempotency_key=None, if_match=None, request_id=None) -> None:
        self.method = method.upper()
        self.path = path
        self.params = params
        self.json = json
        self.idempotency_key = idempotency_key
        self.if_match = if_match
        self.request_id = prepare_request_id(request_id)
        self.idempotent_write = bool(idempotency_key or if_match)


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
