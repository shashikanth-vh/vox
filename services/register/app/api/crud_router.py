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
        company_scoped: bool = False,
        write_operation: str | None = None,
        parent_scope: tuple | None = None,
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
        # Entity-carrying resource (financials, contracts, intel, monitoring, documents,
        # interactions): list/GET/update scope to the caller's companies via the central
        # evaluator when the clients view is SCOPED. Not a line resource (no assignment).
        self.company_scoped = company_scoped
        # The specific matrix operation a WRITE (create/update) requires — e.g.
        # edit_fi_record for financials, edit_contract for contracts. Enforced in
        # addition to view access, so a READ-only viewer of the module cannot mutate it.
        self.write_operation = write_operation
        # For a child resource with no entity_id of its own (syndication lenders), scope
        # by the PARENT line's company: (parent_model, fk_attr_name).
        self.parent_scope = parent_scope


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
    if spec.subject_type is None:
        return
    from app.authz import enforce_operation
    from app.authz import scope as scope_mod
    from app.authz.engine import can_write_line
    from app.authz.matrix import Access
    from app.core.errors import ForbiddenError

    # Entity (company profile) is NOT an assignment-driven line: its write is gated by a
    # DEDICATED operation (edit_client) plus company scope. Handling it here — rather than
    # through can_write_line, which has no Entity mapping — fixes the inversion where every
    # human was denied while machine callers slipped through. Machine callers (ctx.user
    # None) go through enforce_operation too, so enforce_rbac governs them consistently.
    if spec.subject_type == "Entity":
        granted = enforce_operation(ctx.user, "edit_client")  # NONE→403; None→enforce_rbac
        await _enforce_row_lock(ctx, spec, obj_id)
        if ctx.user is None or granted is Access.FULL or ctx.authz_decision == "FULL":
            return
        user_scope = await scope_mod.build_scope(ctx, ctx.user)
        if await scope_mod.entity_in_scope(ctx, user_scope, obj_id):
            return
        raise ForbiddenError(
            "Scoped access: this company is not in your scope to edit.")

    # Assignment-driven lines (Lead/Deal/Lending/Syndication/AssetMonetisation). Machine
    # callers keep the ingestion carve-out (row-level writes from vetted API keys); when
    # enforce_rbac is on they carry a user and are checked like everyone else.
    if ctx.user is None:
        return
    await _enforce_row_lock(ctx, spec, obj_id)
    if ctx.authz_decision == "FULL":
        return
    if ctx.authz_decision == "SCOPED":
        user_scope = await scope_mod.build_scope(ctx, ctx.user)
        if await scope_mod.can_write_row(ctx, user_scope, spec.subject_type, obj_id):
            return
        raise ForbiddenError(
            f"Scoped access: this {spec.subject_type} line is not in your scope "
            "(not assigned to you or your team, and not an unassigned line of your vertical).")
    if not await can_write_line(ctx.session, ctx.tenant_id, ctx.user,
                                spec.subject_type, obj_id):
        raise ForbiddenError(
            f"Role(s) {sorted(ctx.user.roles)} may not write this {spec.subject_type} line.")


async def _enforce_company_write(ctx: "RequestContext", spec: "ResourceSpec",
                                 obj_id: uuid.UUID | None = None, *,
                                 payload_entity_id: Any = None) -> None:
    """Write authorization for an entity-carrying resource (financials, contracts, intel,
    monitoring, documents).

    Fixes the bypass where READ/NONE viewers could mutate rows:
      * NONE / READ  → 403 (a read-only viewer cannot write).
      * FULL         → allow.
      * SCOPED       → the target company must be in the caller's scope
                       (existing row on update; the payload's entity_id on create).

    Enforcement is via the resource's specific WRITE operation (edit_fi_record,
    edit_contract, …) so the check is exactly the matrix's write column, not merely the
    view. Machine callers (no user) honour ``enforce_rbac`` through enforce_operation.
    """
    if not spec.company_scoped or spec.view_name is None:
        return
    from app.authz import enforce_operation
    from app.authz import scope as scope_mod
    from app.authz.engine import view_access
    from app.authz.matrix import Access
    from app.core.errors import ForbiddenError

    if spec.write_operation is not None:
        granted = enforce_operation(ctx.user, spec.write_operation)  # NONE→403; None→enforce_rbac
    else:  # safety net: no dedicated op → derive write capability from the view level.
        if ctx.user is None:
            return
        view = view_access(ctx.user, spec.view_name)
        if view in (Access.NONE, Access.READ):
            raise ForbiddenError(
                f"Role(s) {sorted(ctx.user.roles)} have read-only or no access to "
                f"{spec.view_name}; cannot write {spec.name}.")
        granted = view

    if ctx.user is None or granted is Access.FULL:
        return  # machine caller (compat) or unrestricted writer

    # SCOPED: the target company must be in scope.
    user_scope = await scope_mod.build_scope(ctx, ctx.user)
    if payload_entity_id is not None:  # create path
        if not await scope_mod.entity_in_scope(ctx, user_scope, payload_entity_id):
            raise ForbiddenError(
                f"Scoped access: this company is not in your scope to add a {spec.name} for.")
        return
    assert obj_id is not None  # update path always supplies the row id
    obj = await spec.repo.get(ctx.session, ctx.tenant_id, obj_id)
    if not scope_mod.company_row_in_scope(user_scope, obj):
        raise ForbiddenError(f"Scoped access: this {spec.name}'s company is not in your scope.")


def _enforce_transition(ctx: "RequestContext", spec: "ResourceSpec", data: dict) -> None:
    """Protected status/stage transitions on a generic update.

    Two gaps this closes (the reviewer's "status transitions can bypass the workflow"):

    * A row lock (Converted lead, Disbursed lending line) is enforced on the TARGET value,
      not only the current one — otherwise a scoped/assigned user could set the field
      straight to the locked value because the row wasn't locked *yet*.
    * ``Lead.status`` can never be set to ``Converted`` through the generic PATCH — that
      transition MUST go through ``POST /v1/leads/{id}/convert`` (which atomically creates
      the deal + product lines). The convert endpoint writes the status via the repository
      directly, so it is unaffected by this route-level guard.
    """
    if ctx.user is None or spec.subject_type is None or not data:
        return
    from app.authz.matrix import ROW_LOCKS
    from app.core.errors import ForbiddenError, ValidationAppError

    if spec.subject_type == "Lead" and data.get("status") == "Converted":
        raise ValidationAppError(
            "A lead is converted via POST /v1/leads/{id}/convert (which creates the deal "
            "and product lines atomically), not by setting status directly.")

    lock = ROW_LOCKS.get(spec.subject_type)
    if lock is not None:
        field_name, locking_values, allowed_roles = lock
        if (field_name in data and data[field_name] in locking_values
                and not (ctx.user.roles & allowed_roles)):
            raise ForbiddenError(
                f"Moving {spec.subject_type}.{field_name} to {data[field_name]!r} is a "
                f"locked transition only {sorted(allowed_roles)} may make.")


def _enforce_simple_write(ctx: "RequestContext", spec: "ResourceSpec") -> None:
    """Write gate for a plain directory/reference resource (people, counterparties,
    document-checklist) that is neither a line nor company-scoped: just require its
    write operation. Machine callers honour ``enforce_rbac`` via enforce_operation."""
    if (spec.write_operation is None or spec.company_scoped
            or spec.subject_type is not None):
        return
    from app.authz import enforce_operation

    enforce_operation(ctx.user, spec.write_operation)


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
            # RBAC on an entity-carrying resource create (contracts, intel, monitoring):
            # require the resource's write operation AND, for a SCOPED creator, that the
            # payload's company is in their scope. Closes "creation validates neither the
            # target company nor a write operation."
            if spec.company_scoped:
                await _enforce_company_write(
                    ctx, spec, payload_entity_id=body.get("entity_id"))
            _enforce_simple_write(ctx, spec)  # people / counterparties / checklist create
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
                    granted = enforce_operation(ctx.user, op)
                    entity_id = body.get("entity_id")
                    if (granted.name == "SCOPED" and entity_id is not None
                            and spec.subject_type != "Entity"):
                        # A SCOPED creator may only open lines for companies in
                        # their scope (their book / connected / team / vertical).
                        from app.authz import scope as scope_mod
                        from app.core.errors import ForbiddenError

                        user_scope = await scope_mod.build_scope(ctx, ctx.user)
                        if not await scope_mod.entity_in_scope(ctx, user_scope, entity_id):
                            raise ForbiddenError(
                                "Scoped access: this company is not in your scope "
                                f"to open a {spec.subject_type} line for.")
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
            from app.authz.engine import view_access
            from app.authz.matrix import Access
            from app.core.errors import ForbiddenError

            granted = view_access(ctx.user, spec.view_name)
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
            elif granted is Access.SCOPED and spec.company_scoped:
                user_scope = await scope_mod.build_scope(ctx, ctx.user)
                scope_condition = scope_mod.company_scoped_condition(
                    user_scope, spec.repo.model)
            elif granted is Access.SCOPED and spec.parent_scope is not None:
                user_scope = await scope_mod.build_scope(ctx, ctx.user)
                parent_model, fk_attr = spec.parent_scope
                scope_condition = scope_mod.parent_company_condition(
                    user_scope, spec.repo.model, parent_model, fk_attr)

        # Filters: only whitelisted equality filters are honoured. Reject an UNKNOWN
        # filter param loudly (422) rather than silently ignoring it — a dropped filter
        # is how the wrong-company lead bug leaked data.
        _reserved = {"q", "limit", "cursor", "include_deleted", "with_total", "scope"}
        unknown = [k for k in request.query_params
                   if k not in spec.filterable and k not in _reserved]
        if unknown:
            from app.core.errors import ValidationAppError

            raise ValidationAppError(
                f"Unknown query parameter(s) {unknown} for {spec.name}. "
                f"Filterable: {sorted(spec.filterable)}.")
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
        if ctx.user is not None and spec.view_name is not None:
            from app.authz import scope as scope_mod
            from app.authz.engine import view_access
            from app.authz.matrix import Access
            from app.core.errors import ForbiddenError

            granted = view_access(ctx.user, spec.view_name)
            if granted is Access.NONE:
                raise ForbiddenError(
                    f"Role(s) {sorted(ctx.user.roles)} have no access to {spec.view_name}.")
            if granted is Access.SCOPED:
                user_scope = await scope_mod.build_scope(ctx, ctx.user)
                if spec.subject_type == "Entity":
                    ok = await scope_mod.entity_in_scope(ctx, user_scope, obj.id)
                elif spec.subject_type is not None:
                    ok = await scope_mod.row_in_scope(ctx, user_scope, spec.subject_type, obj)
                elif spec.company_scoped:
                    ok = scope_mod.company_row_in_scope(user_scope, obj)
                elif spec.parent_scope is not None:
                    parent_model, fk_attr = spec.parent_scope
                    ok = await scope_mod.parent_company_row_in_scope(
                        ctx, user_scope, obj, parent_model, fk_attr)
                else:
                    ok = True
                if not ok:
                    raise ForbiddenError(
                        f"Scoped access: this {spec.name} is not in your scope.")
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
            await _enforce_company_write(ctx, spec, obj_id)
            _enforce_simple_write(ctx, spec)  # people / counterparties / checklist update
            data = payload.model_dump(exclude_unset=True)
            _enforce_transition(ctx, spec, data)  # protected status/stage transitions
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
