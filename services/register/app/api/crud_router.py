"""Factory that builds a full, consistent CRUD router for any resource.

Every table gets the exact same, well-tested surface:

    POST   /                 create (idempotent via Idempotency-Key)
    GET    /                 list   (search, keyset pagination, whitelisted filters)
    GET    /{id}             read one
    PATCH  /{id}             partial update (optimistic concurrency)
    DELETE /{id}             soft delete (optimistic concurrency)
    POST   /{id}/restore     undo a soft delete

Doing this once means the concurrency, tenancy, auditing and error semantics are
identical and correct across all 18 tables rather than copy-pasted 18 times.

NB: this module intentionally does NOT use ``from __future__ import annotations`` —
the CRUD routes are built with per-resource schema classes supplied as *runtime*
annotations (``payload: spec.create_schema``). Stringised annotations would hide those
types from FastAPI's body/response resolution.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import get_settings
from app.core.pagination import Page
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models.system import IdempotencyKey
from app.repositories.crud import CRUDRepository


class ResourceSpec:
    def __init__(
        self,
        *,
        name: str,
        prefix: str,
        tags: list[str],
        repo: CRUDRepository,
        create_schema: type[BaseModel],
        update_schema: type[BaseModel],
        read_schema: type[BaseModel],
        filterable: list[str] | None = None,
        include_create: bool = True,
        include_update: bool = True,
        include_delete: bool = True,
    ) -> None:
        self.name = name
        self.prefix = prefix
        self.tags = tags
        self.repo = repo
        self.create_schema = create_schema
        self.update_schema = update_schema
        self.read_schema = read_schema
        self.filterable = filterable or []
        self.include_create = include_create
        # Append-only resources (e.g. the interaction timeline) omit update/delete.
        self.include_update = include_update
        self.include_delete = include_delete


def _hash_body(payload: dict) -> str:
    import orjson

    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _parse_if_match(if_match: str | None) -> int | None:
    """Interpret an If-Match header as an integer version, if numeric."""
    if not if_match:
        return None
    val = if_match.strip().strip('"')
    return int(val) if val.isdigit() else None


def build_crud_router(spec: ResourceSpec) -> APIRouter:
    router = api_router(prefix=spec.prefix, tags=spec.tags)
    repo = spec.repo
    settings = get_settings()

    if spec.include_create:

        @router.post("", response_model=spec.read_schema, status_code=201,
                     summary=f"Create {spec.name}")
        async def create(
            request: Request,
            response: Response,
            payload: spec.create_schema,
            ctx: RequestContext = Depends(get_context),
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> Any:
            body = payload.model_dump(exclude_unset=False)
            if idempotency_key:
                existing = (
                    await ctx.session.execute(
                        select(IdempotencyKey).where(
                            IdempotencyKey.tenant_id == ctx.tenant_id,
                            IdempotencyKey.key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    # Replay the original outcome; do not create a duplicate row.
                    response.status_code = existing.status_code
                    response.headers["Idempotency-Replay"] = "true"
                    return existing.response_body

            obj = await repo.create(ctx.session, ctx.tenant_id, ctx.actor, body)
            result = spec.read_schema.model_validate(obj)

            if idempotency_key:
                await ctx.session.flush()
                ctx.session.add(
                    IdempotencyKey(
                        tenant_id=ctx.tenant_id,
                        key=idempotency_key,
                        request_hash=_hash_body({"path": request.url.path, "body": body}),
                        method="POST",
                        path=request.url.path,
                        status_code=201,
                        response_body=result.model_dump(mode="json"),
                        expires_at=datetime.now(UTC)
                        + timedelta(hours=settings.idempotency_ttl_hours),
                    )
                )
            response.headers["ETag"] = f'"{obj.version}"'
            return result

    @router.get("", response_model=Page[spec.read_schema], summary=f"List {spec.name}")
    async def list_(
        request: Request,
        ctx: RequestContext = Depends(get_context),
        q: str | None = Query(default=None, description="Full-text-ish search across key fields"),
        limit: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
        cursor: str | None = Query(default=None, description="Opaque keyset cursor"),
        include_deleted: bool = Query(default=False),
        with_total: bool = Query(default=False, description="Include exact total (slower)"),
    ) -> Any:
        # Whitelisted equality filters pulled straight from the query string.
        filters = {
            k: request.query_params[k] for k in spec.filterable if k in request.query_params
        }
        rows, next_cursor, total = await repo.list(
            ctx.session,
            ctx.tenant_id,
            filters=filters,
            q=q,
            limit=limit,
            cursor=cursor,
            include_deleted=include_deleted,
            with_total=with_total,
        )
        items = [spec.read_schema.model_validate(r) for r in rows]
        return Page(items=items, count=len(items), next_cursor=next_cursor, total=total)

    @router.get("/{obj_id}", response_model=spec.read_schema, summary=f"Get {spec.name}")
    async def get_one(
        obj_id: uuid.UUID,
        response: Response,
        ctx: RequestContext = Depends(get_context),
        include_deleted: bool = Query(default=False),
    ) -> Any:
        obj = await repo.get(ctx.session, ctx.tenant_id, obj_id, include_deleted=include_deleted)
        response.headers["ETag"] = f'"{obj.version}"'
        return spec.read_schema.model_validate(obj)

    if spec.include_update:

        @router.patch("/{obj_id}", response_model=spec.read_schema, summary=f"Update {spec.name}")
        async def update(
            obj_id: uuid.UUID,
            response: Response,
            payload: spec.update_schema,
            ctx: RequestContext = Depends(get_context),
            if_match: str | None = Header(default=None, alias="If-Match"),
        ) -> Any:
            data = payload.model_dump(exclude_unset=True)
            expected = data.pop("expected_version", None)
            if expected is None:
                expected = _parse_if_match(if_match)
            obj = await repo.update(
                ctx.session, ctx.tenant_id, obj_id, ctx.actor, data, expected_version=expected
            )
            response.headers["ETag"] = f'"{obj.version}"'
            return spec.read_schema.model_validate(obj)

    if spec.include_delete:

        @router.delete("/{obj_id}", status_code=204, summary=f"Soft-delete {spec.name}")
        async def delete(
            obj_id: uuid.UUID,
            ctx: RequestContext = Depends(get_context),
            if_match: str | None = Header(default=None, alias="If-Match"),
            expected_version: int | None = Query(default=None),
        ) -> Response:
            expected = expected_version if expected_version is not None else _parse_if_match(if_match)
            await repo.soft_delete(
                ctx.session, ctx.tenant_id, obj_id, ctx.actor, expected_version=expected
            )
            return Response(status_code=204)

        @router.post("/{obj_id}/restore", response_model=spec.read_schema,
                     summary=f"Restore {spec.name}")
        async def restore(
            obj_id: uuid.UUID,
            ctx: RequestContext = Depends(get_context),
        ) -> Any:
            obj = await repo.restore(ctx.session, ctx.tenant_id, obj_id, ctx.actor)
            return spec.read_schema.model_validate(obj)

    return router
