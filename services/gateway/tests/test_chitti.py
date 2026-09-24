"""Authenticated Chitti routing and streaming with controlled IdP/Access/upstream fixtures."""

from __future__ import annotations

import asyncio
import json
import ssl
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from evam_backend_core.internal_token import verify_internal_context
from evam_backend_core.oidc import OidcVerifier

from app.config import get_settings
from app.main import create_app
from app.resolver import Resolver

SIGNING_KEY = "test-chitti-context-signing-key-32"
EMAIL = "reader@example.test"


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, *, block=False, fail=False):
        self.block = block
        self.fail = fail
        self.closed = asyncio.Event()
        self.finish = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        if self.block:
            await self.finish.wait()
        if self.fail:
            raise httpx.ReadError("private upstream details")
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed.set()


@pytest.fixture
async def chat(monkeypatch):
    for name, value in {
        "GATEWAY_CHITTI_URL": "http://chitti:8000",
        "GATEWAY_CHITTI_API_KEY": "chitti-service-key",
        "GATEWAY_INTERNAL_SIGNING_SECRET": SIGNING_KEY,
        "GATEWAY_CHITTI_MAX_CONCURRENT_REQUESTS": "1",
        "GATEWAY_CHITTI_MAX_REQUEST_BYTES": "1024",
        "GATEWAY_OIDC_ISSUER": "",
        "GATEWAY_OIDC_ISSUERS": "",
        "GATEWAY_AUTH_EXEMPT_PATHS": "/chitti/v1/models,/chitti/v1/chat/completions",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="test", alg="RS256", use="sig")
    state = {
        "seen": [], "resolves": [], "stream": None, "error": None,
        "access_status": 200, "views": {"leads": "SCOPED", "lending": "NONE"},
        "operations": {"edit_lead": "NONE"}, "upstream_status": 200,
    }

    async def upstream(request):
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": "https://idp.test/jwks"})
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/v1/resolve":
            state["resolves"].append(request)
            return httpx.Response(state["access_status"], json={
                "id": "user-1", "email": EMAIL, "roles": ["BDRM"],
                "views": state["views"], "operations": state["operations"],
                "version": 7, "epoch": 3, "reports": [],
            })
        state["seen"].append(request)
        if state["error"]:
            raise state["error"]
        stream = state["stream"] or httpx.ByteStream(json.dumps({"data": [{"id": "prism-chitti"}]}).encode())
        return httpx.Response(state["upstream_status"], stream=stream,
                              headers={"Content-Type": "text/event-stream" if state["stream"]
                                       else "application/json", "Set-Cookie": "secret=value"})

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
        app.state.client = backend
        app.state.resolver = Resolver(backend)
        app.state.oidc = OidcVerifier("https://idp.test", "prism", backend)
        claims = {"iss": "https://idp.test", "aud": "prism", "sub": "subject-1", "email": EMAIL,
                  "roles": ["Admin"], "exp": int(time.time()) + 300}
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
        state.update(app=app, key=key, claims=claims, token=token)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway",
                                    headers={"Authorization": f"Bearer {token}",
                                             "X-Tenant": "EVAM"}) as client:
            state["client"] = client
            yield state


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", [("/chitti/v1/models", "GET"),
                                         ("/chitti/v1/chat/completions", "POST")])
async def test_verified_identity_and_exact_downstream_binding(chat, path, method):
    response = await chat["client"].request(method, path, content=b"{}", headers={
        "X-User-Email": "admin@forged.test", "X-User-Id": "forged", "X-User-Roles": "Admin",
        "X-Internal-Context": "forged", "X-Authz-Decision": "FULL", "X-API-Key": "forged",
        "X-Chitti-Presentation": "debug", "X-Admin-Key": "forged", "X-On-Behalf-Of": "forged",
        "X-User-Reports": "forged",
    })
    assert response.status_code == 200
    forwarded, = chat["seen"]
    assert str(forwarded.url) == "http://chitti:8000" + path.removeprefix("/chitti")
    assert forwarded.headers["X-API-Key"] == "chitti-service-key"
    assert forwarded.headers["X-Chitti-Presentation"] == "business"
    assert forwarded.headers["X-Request-ID"] == response.headers["X-Request-ID"]
    assert forwarded.headers["X-User-Email"] == EMAIL
    for name in ("Authorization", "X-Admin-Key", "X-On-Behalf-Of", "X-User-Reports", "X-Authz-Decision"):
        assert name not in forwarded.headers
    context = verify_internal_context(forwarded.headers["X-Internal-Context"], verify_key=SIGNING_KEY)
    assert (context.method, context.path) == (method, path.removeprefix("/chitti"))
    assert (context.tenant, context.email, context.user_id) == ("EVAM", EMAIL, "user-1")
    assert context.roles == ["BDRM"]
    assert context.effective_views == chat["views"]
    assert context.effective_operations == chat["operations"]
    assert (context.matrix_version, context.epoch) == (7, 3)
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["", "Bearer invalid", "expired", "tampered"])
async def test_anonymous_invalid_expired_and_tampered_bearers_refused_even_if_exempt(chat, auth):
    if auth == "expired":
        token = jwt.encode({**chat["claims"], "exp": 1}, chat["key"],
                           algorithm="RS256", headers={"kid": "test"})
        auth = f"Bearer {token}"
    elif auth == "tampered":
        head, _, sig = chat["token"].split(".")
        payload = jwt.api_jws.base64url_encode(json.dumps({**chat["claims"], "email": "admin@test"}).encode())
        auth = f"Bearer {head}.{payload.decode()}.{sig}"
    response = await chat["client"].get("/chitti/v1/models", headers={"Authorization": auth,
                                                                 "X-User-Email": "admin@test"})
    assert response.status_code == 401
    assert not chat["seen"] and not chat["resolves"]


@pytest.mark.asyncio
async def test_local_header_trust_cannot_access_chat(chat):
    chat["app"].state.oidc = None
    response = await chat["client"].get("/chitti/v1/models", headers={"X-User-Email": EMAIL})
    assert response.status_code == 401
    assert not chat["seen"]


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["chitti_url", "chitti_api_key", "internal_signing_secret"])
async def test_missing_configuration_fails_closed(chat, setting):
    setattr(get_settings(), setting, "")
    response = await chat["client"].get("/chitti/v1/models")
    assert response.status_code == 503
    assert not chat["seen"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chitti", "/chitti/v1/tenants", "/chitti/healthz"])
async def test_unknown_routes_do_not_reach_register(chat, path):
    response = await chat["client"].get(path)
    assert response.status_code == 404
    assert not chat["seen"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(404, 403), (503, 502)])
async def test_denied_user_or_access_outage_cannot_reach_chat(chat, status, expected):
    chat["access_status"] = status
    response = await chat["client"].get("/chitti/v1/models")
    assert response.status_code == expected
    assert not chat["seen"]


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["FULL", "SCOPED", "NONE"])
async def test_chat_never_upgrades_record_permissions(chat, grant):
    chat["views"] = {"leads": grant}
    response = await chat["client"].post("/chitti/v1/chat/completions", json={"stream": True})
    assert response.status_code == 200
    context = verify_internal_context(chat["seen"][0].headers["X-Internal-Context"], verify_key=SIGNING_KEY)
    assert context.effective_views == {"leads": grant}
    assert context.decision is None


@pytest.mark.asyncio
async def test_chunked_oversized_body_rejected_and_slot_released(chat):
    async def chunks():
        yield b"a" * 800
        yield b"b" * 800
    response = await chat["client"].post("/chitti/v1/chat/completions", content=chunks())
    assert response.status_code == 413
    assert not chat["seen"]
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0
    assert (await chat["client"].get("/chitti/v1/models")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(httpx.ConnectError("private"), 502),
                                         (httpx.ReadTimeout("private"), 504)])
async def test_upstream_failure_releases_slot_without_exposing_details(chat, error, status):
    chat["error"] = error
    response = await chat["client"].get("/chitti/v1/models")
    assert response.status_code == status
    assert "private" not in response.text
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0


@pytest.mark.asyncio
async def test_sse_bytes_status_and_correlation_survive_gateway(chat):
    chat["stream"] = ControlledStream()
    response = await chat["client"].post("/chitti/v1/chat/completions", json={"stream": True},
                                         headers={"X-Request-ID": "chat-request-123"})
    assert response.content.endswith(b"data: [DONE]\n\n")
    assert b"Hello" in response.content
    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["X-Request-ID"] == "chat-request-123"
    assert chat["seen"][0].headers["X-Request-ID"] == "chat-request-123"
    assert chat["stream"].closed.is_set()
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0


async def _drive_stream(chat, *, disconnect=True):
    """Drive real ASGI send/receive: HTTPX's ASGI transport buffers until completion."""
    queue = asyncio.Queue()
    await queue.put({"type": "http.request", "body": b'{"stream":true}', "more_body": False})
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/chitti/v1/chat/completions",
             "raw_path": b"/chitti/v1/chat/completions", "query_string": b"",
             "root_path": "", "server": ("gateway", 80), "client": ("127.0.0.1", 12345),
             "headers": [(b"authorization", f'Bearer {chat["token"]}'.encode()),
                         (b"content-type", b"application/json")]}
    delivered = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body" and b"Hello" in message.get("body", b""):
            delivered.set()

    task = asyncio.create_task(chat["app"](scope, queue.get, send))
    try:
        await asyncio.wait_for(delivered.wait(), 2)
        # First chunk is available while upstream is still blocked: no buffering.
        assert not chat["stream"].finish.is_set()
        busy = await chat["client"].get("/chitti/v1/models")
        assert busy.status_code == 429
        assert busy.headers["Retry-After"] == "1"
        if disconnect:
            await queue.put({"type": "http.disconnect"})
        else:
            chat["stream"].finish.set()
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(chat["stream"].closed.wait(), 2)
        assert chat["app"].state.chitti_limiter.borrowed_tokens == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [True, False])
async def test_stream_delivers_before_completion_and_releases_capacity(chat, disconnect):
    chat["stream"] = ControlledStream(block=True)
    await _drive_stream(chat, disconnect=disconnect)


@pytest.mark.asyncio
async def test_timeout_during_stream_closes_upstream_and_releases_slot(chat):
    chat["stream"] = ControlledStream(block=True)
    get_settings().chitti_timeout_s = 0.05
    # Once response headers are sent, failure terminates the stream; it cannot become
    # a new HTTP 504 response or a successful [DONE] event.
    with pytest.raises((TimeoutError, ExceptionGroup)) as failure:
        await chat["client"].post("/chitti/v1/chat/completions", json={"stream": True})
    assert all(isinstance(exc, TimeoutError) for exc in _leaves(failure.value))
    assert chat["stream"].closed.is_set()
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0


@pytest.mark.asyncio
async def test_read_failure_during_stream_closes_upstream_and_releases_slot(chat):
    chat["stream"] = ControlledStream(fail=True)
    with pytest.raises((httpx.ReadError, ExceptionGroup)) as failure:
        await chat["client"].post("/chitti/v1/chat/completions", json={"stream": True})
    assert all(isinstance(exc, httpx.ReadError) for exc in _leaves(failure.value))
    assert chat["stream"].closed.is_set()
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0


@pytest.mark.asyncio
async def test_upstream_error_status_preserved(chat):
    chat["upstream_status"] = 403
    response = await chat["client"].get("/chitti/v1/models")
    assert response.status_code == 403
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0


def _leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for nested in exc.exceptions for leaf in _leaves(nested)]
    return [exc]


@pytest.mark.asyncio
async def test_timeout_before_upstream_headers_releases_slot(chat, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    original = chat["app"].state.client.send

    async def slow_headers(outgoing, **kwargs):
        if outgoing.url.host != "chitti":
            return await original(outgoing, **kwargs)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(chat["app"].state.client, "send", slow_headers)
    get_settings().chitti_timeout_s = 0.05
    response = await chat["client"].get("/chitti/v1/models")
    assert response.status_code == 504
    assert started.is_set() and cancelled.is_set()
    assert chat["app"].state.chitti_limiter.borrowed_tokens == 0


@pytest.mark.asyncio
async def test_disconnect_before_upstream_headers_cancels_request(chat, monkeypatch):
    from starlette.requests import Request

    from app.chitti_proxy import proxy_chitti

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_headers(outgoing, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(chat["app"].state.client, "send", slow_headers)
    queue = asyncio.Queue()
    await queue.put({"type": "http.request", "body": b"", "more_body": False})
    request = Request({"type": "http", "method": "GET", "path": "/chitti/v1/models",
                       "query_string": b"", "headers": [], "app": chat["app"]}, queue.get)
    task = asyncio.create_task(proxy_chitti(request, "http://chitti/v1/models", {}, get_settings()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await queue.put({"type": "http.disconnect"})
        response = await asyncio.wait_for(task, 1)
        assert response.status_code == 499
        assert cancelled.is_set()
        assert chat["app"].state.chitti_limiter.borrowed_tokens == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_upstream_ca_preserves_public_roots_and_hostname_verification(tmp_path):
    settings = get_settings()
    baseline = set(ssl.create_default_context().get_ca_certs(binary_form=True))
    cert = tmp_path / "ca.pem"
    cert.write_text(ssl.DER_cert_to_PEM_cert(next(iter(baseline))))
    settings.upstream_ca_file = str(cert)
    context = settings.tls_verify()
    assert baseline <= set(context.get_ca_certs(binary_form=True))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    settings.upstream_ca_file = str(tmp_path / "missing.pem")
    with pytest.raises(FileNotFoundError):
        settings.tls_verify()
