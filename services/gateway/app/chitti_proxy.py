"""Bounded Chitti proxy with upstream ownership across the response lifetime."""

from __future__ import annotations

import anyio
import httpx
from evam_backend_core.logging import get_logger
from fastapi import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.config import Settings

log = get_logger("gateway.chitti")
_SKIP_HEADERS = {"content-length", "connection", "keep-alive", "transfer-encoding",
                 "server", "date", "set-cookie"}


def _error(status: int, detail: str) -> Response:
    return JSONResponse({"error": {"detail": detail}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


class _OwnedStream(StreamingResponse):
    def __init__(self, upstream, limiter, borrower, deadline):
        self.upstream = upstream
        self.limiter = limiter
        self.borrower = borrower
        self.deadline = deadline
        # Forward raw bytes so Content-Encoding remains valid. Never buffer an SSE
        # stream or persist a user-specific model discovery/completion response.
        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_HEADERS}
        headers.update({"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            with anyio.fail_after(max(0, self.deadline - anyio.current_time())):
                await super().__call__(scope, receive, send)
        finally:
            # Cleanup also runs if the response is cancelled before its iterator starts.
            # Shield the close from Starlette's disconnect cancellation scope.
            with anyio.CancelScope(shield=True):
                try:
                    await self.upstream.aclose()
                finally:
                    self.limiter.release_on_behalf_of(self.borrower)


async def _open_upstream(request: Request, outgoing: httpx.Request) -> httpx.Response | None:
    response = None
    error = None
    try:
        async with anyio.create_task_group() as group:
            async def watch_disconnect():
                while not await request.is_disconnected():
                    await anyio.sleep(0.1)
                group.cancel_scope.cancel()

            group.start_soon(watch_disconnect)
            try:
                response = await request.app.state.client.send(outgoing, stream=True)
            except Exception as exc:
                error = exc
            finally:
                group.cancel_scope.cancel()
    except BaseException:
        if response is not None:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        raise
    if error is not None:
        raise error
    return response


async def proxy_chitti(request: Request, url: str, headers: dict[str, str], settings: Settings) -> Response:
    limiter = request.app.state.chitti_limiter
    borrower = object()
    try:
        limiter.acquire_on_behalf_of_nowait(borrower)
    except anyio.WouldBlock:
        response = _error(429, "Chitti is busy. Try again shortly.")
        response.headers["Retry-After"] = "1"
        return response

    upstream = None
    transferred = False
    deadline = anyio.current_time() + settings.chitti_timeout_s
    try:
        with anyio.fail_after(settings.chitti_timeout_s):
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > settings.chitti_max_request_bytes:
                    return _error(413, "Chat request exceeds the size limit.")
                body.extend(chunk)
            outgoing = request.app.state.client.build_request(
                request.method, url, content=bytes(body), params=request.query_params,
                headers=headers, timeout=settings.chitti_timeout_s,
            )
            upstream = await _open_upstream(request, outgoing)
        if upstream is None:
            return Response(status_code=499)
        response = _OwnedStream(upstream, limiter, borrower, deadline)
        transferred = True
        return response
    except (TimeoutError, httpx.TimeoutException):
        return _error(504, "Chitti exceeded the request time limit.")
    except httpx.HTTPError:
        log.warning("chitti_upstream_unavailable")
        return _error(502, "Chitti is unavailable.")
    finally:
        if not transferred:
            with anyio.CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    limiter.release_on_behalf_of(borrower)
