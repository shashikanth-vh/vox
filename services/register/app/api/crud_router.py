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
        subject_type: str | None = None,
        view_name: str | None = None,
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
        # RBAC: line resources (Lead/Deal/Lending/Syndication/AssetMonetisation) enforce
        # scoped writes (assignment-driven) and scoped list filtering when a user context
        # is present. None = not a line resource; only the delete gate applies.
        self.subject_type = subject_type
        self.view_name = view_name


def _hash_body(payload: dict) -> str:
    import orjson

    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _parse_if_match(if_match: str | None) -> int | None:
    """Interpret an If-Match header as an integer version, if numeric."""
    if not if_match:
        return None
    val = if_match.strip().strip('"')
    return int(val) if val.isdigit() else None


async def _enforce_row_lock(ctx: "RequestContext", spec: "ResourceSpec",
                            obj_id: uuid.UUID) -> None:
    """Field Rules, row-lock slice: a Converted lead / Disbursed lending line refuses
    further edits except from the roles the policy names (Field Rules sheet)."""
    if ctx.user is None or spec.subject_type is None:
        return
    from app.authz.matrix import ROW_LOCKS
    from app.core.errors import ForbiddenError

    lock = ROW_LOCKS.get(spec.subject_type)
    if lock is None:
        return
    fieldname, locking_values, allowed_roles = lock
    row = await spec.repo.get(ctx.session, ctx.tenant_id, obj_id)
    if getattr(row, fieldname, None) in locking_values and not (
        ctx.user.roles & allowed_roles
    ):
        raise ForbiddenError(
            f"This {spec.subject_type} is locked ({fieldname} = "
            f"{getattr(row, fieldname)!r}); only {sorted(allowed_roles)} may edit it.")


async def _enforce_line_write(ctx: "RequestContext", spec: "ResourceSpec",
                              obj_id: uuid.UUID) -> None:
    """Write scope for line resources when a user context is present.

    Trust ladder: an upstream (gateway) decision header settles the BINARY half —
    FULL passes, SCOPED goes to the central scope evaluator (own/team assignment on
    THIS line, or vertical-Head default ownership of an unassigned line). With no
    decision header (direct/bypass call), fall back to the code-matrix check
    (defense in depth). Row locks apply to every human edit regardless of scope.
    """
    if ctx.user is None or spec.subject_type is None:
        return
    from app.authz import scope as scope_mod
    from app.authz.engine import can_write_line
    from app.core.errors import ForbiddenError

    await _enforce_row_lock(ctx, spec, obj_id)
    if ctx.authz_decision == "FULL":
        return
    if ctx.authz_decision == "SCOPED":
        user_scope = await scope_mod.build_scope(ctx, ctx.user)
        if spec.subject_type == "Entity":
            if await scope_mod.entity_in_scope(ctx, user_scope, obj_id):
                return
        elif await scope_mod.can_write_row(ctx, user_scope, spec.subject_type, obj_id):
            return
        raise ForbiddenError(
            f"Scoped access: this {spec.subject_type} line is not in your scope "
            "(not assigned to you or your team, and not an unassigned line of your vertical).")
    if not await can_write_line(ctx.session, ctx.tenant_id, ctx.user,
                                spec.subject_type, obj_id):
        raise ForbiddenError(
            f"Role(s) {sorted(ctx.user.roles)} may not write this {spec.subject_type} line.")


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
            # RBAC: creating a line resource is gated by its operation from the matrix
            # (add_lead / push_lead_to_deals / add_product_line) — the same operation
            # the gateway checks at the front door (defense in depth). Machine callers
            # (vetted API keys: workflows, VocX, PULSE) keep their write path — the
            # RBAC-mandatory flag hard-gates the destructive surfaces, not ingestion.
            if spec.subject_type is not None and ctx.user is not None:
                from app.authz import enforce_operation
                from app.authz.matrix import CREATE_OPERATION_FOR_SUBJECT

                op = CREATE_OPERATION_FOR_SUBJECT.get(spec.subject_type)
                if op is not None:
                    enforce_operation(ctx.user, op)
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

            # Spec: the creator AUTOMATICALLY owns the new line when they hold its
            # primary assignment role (a BDRM's new lead is assigned to them at birth,
            # so their scoped list can never hide their own record).
            if spec.subject_type is not None and ctx.user is not None:
                from app.authz.matrix import PRIMARY_ASSIGNMENT_ROLE
                from app.models.users import LineAssignment

                primary = PRIMARY_ASSIGNMENT_ROLE.get(spec.subject_type)
                if primary and primary in ctx.user.roles:
                    await ctx.session.flush()
                    ctx.session.add(LineAssignment(
                        tenant_id=ctx.tenant_id, user_id=ctx.user.id,
                        subject_type=spec.subject_type, subject_id=obj.id,
                        assignment_role=primary, assigned_by="auto:create",
                        note="Auto-assigned: creator holds the primary role.",
                        created_by=ctx.actor, updated_by=ctx.actor))
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
        # RBAC on line-resource lists (user context present): view access NONE → 403;
        # SCOPED → the central scope evaluator (assignment ∪ connected company ∪
        # own/team book ∪ vertical-Head default ownership); READ/FULL → everything.
        scope_condition = None
        if ctx.user is not None and spec.view_name is not None:
            from app.authz import scope as scope_mod
            from app.authz.engine import _stacked
            from app.authz.matrix import VIEW_ACCESS, Access
            from app.core.errors import ForbiddenError

            granted = _stacked(VIEW_ACCESS[spec.view_name], ctx.user.roles)
            if granted is Access.NONE:
                raise ForbiddenError(
                    f"Role(s) {sorted(ctx.user.roles)} have no access to {spec.view_name}.")
            if granted is Access.SCOPED and spec.subject_type is not None:
                user_scope = await scope_mod.build_scope(ctx, ctx.user)
                if spec.subject_type == "Entity":
                    scope_condition = scope_mod.entity_list_condition(
                        user_scope, spec.repo.model)
                else:
                    scope_condition = scope_mod.list_condition(user_scope, spec.subject_type)

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
            condition=scope_condition,
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
        # RBAC: direct GET honours the same scope as the list — a SCOPED user cannot
        # fetch an unrelated row just by knowing its id.
        if ctx.user is not None and spec.view_name is not None and spec.subject_type is not None:
            from app.authz import scope as scope_mod
            from app.authz.engine import _stacked
            from app.authz.matrix import VIEW_ACCESS, Access
            from app.core.errors import ForbiddenError

            granted = _stacked(VIEW_ACCESS[spec.view_name], ctx.user.roles)
            if granted is Access.NONE:
                raise ForbiddenError(
                    f"Role(s) {sorted(ctx.user.roles)} have no access to {spec.view_name}.")
            if granted is Access.SCOPED:
                user_scope = await scope_mod.build_scope(ctx, ctx.user)
                if spec.subject_type == "Entity":
                    ok = await scope_mod.entity_in_scope(ctx, user_scope, obj.id)
                else:
                    ok = await scope_mod.row_in_scope(ctx, user_scope, spec.subject_type, obj)
                if not ok:
                    raise ForbiddenError(
                        f"Scoped access: this {spec.subject_type} is not in your scope.")
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
            await _enforce_line_write(ctx, spec, obj_id)
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
            # RBAC: "Delete a row — Admin ONLY" (checked whenever a user context is
            # present; machine-to-machine behaviour follows REGISTER_ENFORCE_RBAC).
            from app.authz import enforce_operation

            enforce_operation(ctx.user, "delete_row")
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
            # RBAC: restoring a soft-deleted row mirrors the delete gate (Admin-only).
            from app.authz import enforce_operation

            enforce_operation(ctx.user, "delete_row")
            obj = await repo.restore(ctx.session, ctx.tenant_id, obj_id, ctx.actor)
            return spec.read_schema.model_validate(obj)

    return router
