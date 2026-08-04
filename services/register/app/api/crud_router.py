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
from sqlalchemy import and_, select
from sqlalchemy import text as sa_text

from app.core import reconciliation as recon
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
        pre_write: Any = None,
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
        # An async (ctx, body, obj_id|None) -> None hook run immediately before a create
        # or update lands, for the invariants a column constraint cannot express — see
        # app.api.people_rules. It raises; it never edits the body.
        self.pre_write = pre_write
        # For a child resource with no entity_id of its own (syndication lenders), scope
        # by the PARENT line's company: (parent_model, fk_attr_name).
        self.parent_scope = parent_scope
        # (field, prefix): when the create body omits the field, assign the next free
        # "{prefix}{NNNN}" for the tenant (e.g. lead_no "L-0001"). Explicit values pass
        # through untouched and keep the natural-key uniqueness guarantee.


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
    """Field Rules, row-lock slice: a Converted lead / handed-over lending line refuses
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

    # Assignment-driven lines (Lead/Deal/Lending/Syndication/AssetMonetisation). A machine
    # caller must be a SERVICE permitted to edit this line — svc_atlas (read-only) is
    # refused, svc_vox may edit_lead, etc. A generic key follows enforce_rbac.
    if ctx.user is None:
        from app.authz.matrix import WRITE_OPERATION_FOR_SUBJECT

        op = WRITE_OPERATION_FOR_SUBJECT.get(spec.subject_type)
        if op is not None:
            enforce_operation(None, op)
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


async def _enforce_transition(ctx: "RequestContext", spec: "ResourceSpec",
                              obj_id: uuid.UUID, data: dict,
                              break_glass_justification: str | None = None) -> None:
    """Protected status/stage transitions on a generic update.

    Closes the "status transitions can bypass the workflow" gaps:

    * ``Lead.status`` can never be set to ``Converted`` through the generic PATCH — by ANY
      caller, machine included — it MUST go through ``POST /v1/leads/{id}/convert``. (The
      convert endpoint writes the status via the repository directly, unaffected by this.)
    * The transition itself must be ALLOWED by policy (``ALLOWED_TRANSITIONS``): e.g. a Lead
      may go Active→Dropped and Dropped→Active, but an arbitrary jump is rejected (422).
    * A row lock (Converted lead, handed-over lending) is enforced on the TARGET value, not
      only once the row is already locked.
    * A sensitive stage's EVIDENCE gate must be satisfied — the immutable governance evidence
      that stage requires must be on file — unless a designated senior authority supplies an
      audited break-glass justification.
    """
    if spec.subject_type is None or not data:
        return
    from evam_backend_core import policy

    from app.core import evidence as ev
    from app.core.errors import ForbiddenError, ValidationAppError

    # Converting a lead is never a bare status edit — for humans AND machines.
    if spec.subject_type == "Lead" and data.get("status") == "Converted":
        raise ValidationAppError(
            "A lead is converted via POST /v1/leads/{id}/convert (which creates the deal "
            "and product lines atomically), not by setting status directly.")

    # Closing a deal is never a bare stage edit either (increment 8): the closed
    # terminals are reachable ONLY through POST /v1/deals/{id}/close, which validates
    # the deal's OPEN ITEMS first — open EWS cases, un-excused covenant breaches,
    # product lines mid-pipeline. (The close endpoint writes the stage via the
    # repository directly, unaffected by this.)
    if spec.subject_type == "Deal" and data.get("stage") in ("Closed Won", "Closed Lost"):
        raise ValidationAppError(
            "A deal is closed via POST /v1/deals/{id}/close (open-item validated, note "
            "mandatory), not by setting the stage directly.")

    # The evidence kinds already on file for this record — the policy engine's evidence gate
    # checks them against what the target stage requires. Loaded once, for every caller.
    evidence = await ev.load_evidence_kinds(ctx, spec.subject_type, obj_id)

    # BREAK-GLASS: the only way past a MISSING-evidence gate. It is reserved to a designated
    # senior authority (Admin/Management), must carry a justification, and is AUDITED. A caller who
    # supplies a justification but lacks the authority is refused outright (never silently ignored).
    break_glass = False
    bg_reason: str | None = None
    if break_glass_justification and break_glass_justification.strip():
        if not ev.break_glass_allowed(ctx):
            raise ForbiddenError(
                "An evidence break-glass is reserved to a designated senior authority "
                "(Admin or Management).")
        from app.authz.revalidate import revalidate_sensitive
        await revalidate_sensitive(ctx, "approve_stage_change")
        break_glass = True
        bg_reason = break_glass_justification.strip()

    # Read the CURRENT row ONCE and hand it, with the proposed change, to the SINGLE shared
    # policy authority — the exact same call the change-request approval and creation paths make,
    # so no write path can enforce a different (or no) policy. It runs transition-graph
    # validation, mandatory-fields-to-enter-a-stage, role/stage field locks, row locks and the
    # evidence gate. roles=None for a machine caller (services are bound by their service allowlist
    # above); a human passes their roles so the role-based locks apply.
    existing = await spec.repo.get(ctx.session, ctx.tenant_id, obj_id)
    current = {c.name: getattr(existing, c.name) for c in existing.__table__.columns}
    roles = ctx.user.roles if ctx.user is not None else None
    violation = policy.check_write(spec.subject_type, current=current, changes=data, roles=roles,
                                   evidence=evidence, break_glass=break_glass)
    if violation is not None:
        if violation.kind == "forbidden":
            raise ForbiddenError(violation.message)
        raise ValidationAppError(violation.message)

    # Record an audit trail whenever a break-glass ACTUALLY bypassed a missing-evidence gate, so a
    # senior override of the evidence requirement is never invisible.
    if break_glass:
        stage_field = policy.stage_field_of(spec.subject_type)
        target = data.get(stage_field) if stage_field else None
        if target and policy.evidence_error(spec.subject_type, target, evidence) is not None:
            from app.core.logging import request_id_ctx
            from app.db.base import AuditLog
            ctx.session.add(AuditLog(
                tenant_id=ctx.tenant_id, actor=ctx.actor, action="evidence.break_glass",
                resource_type=spec.subject_type, resource_id=str(obj_id),
                request_id=request_id_ctx.get(),
                changes={"target_stage": target, "justification": bg_reason,
                         "evidence_on_file": sorted(evidence),
                         "by": ctx.user.email if ctx.user else None}))


def _recon_included(ctx: "RequestContext", include_reconciliation: bool) -> bool:
    """Whether still-'Required' rows may be returned. Only an ADMIN human may opt in explicitly;
    a service caller (no user) can NEVER include them — operational reads fail closed."""
    return bool(include_reconciliation and ctx.user is not None and ctx.user.is_admin)


def _gate_include_deleted(ctx: "RequestContext", include_deleted: bool) -> None:
    """Reading soft-deleted rows is an audit/backup capability, not a normal read: a human
    caller must hold the ``audit`` view (Admin); a NAMED service may never read deleted
    rows. Closes "read-only services can still read deleted records"."""
    if not include_deleted:
        return
    from app.authz.engine import service_ctx, view_access
    from app.authz.matrix import Access
    from app.core.errors import ForbiddenError

    if ctx.user is None:
        # A NAMED service never reads deleted rows; an UNNAMED key may only under
        # compatibility mode — under enforce_rbac it fails closed like every other machine
        # read, so a leaked generic key can't pull the deleted-row history.
        from app.core.config import get_settings

        if service_ctx.get() is not None:
            raise ForbiddenError("Services may not read soft-deleted rows.")
        if get_settings().enforce_rbac:
            raise ForbiddenError(
                "include_deleted requires a user context with the audit capability "
                "(RBAC enforced); an unnamed key may not read deleted rows.")
        return
    if view_access(ctx.user, "audit") is Access.NONE:
        raise ForbiddenError("include_deleted requires the audit capability (Admin).")


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
            # (add_lead / push_lead_to_deals / add_product_line) — for HUMANS and for
            # machine callers alike. enforce_operation binds a machine caller to its
            # SERVICE allowlist (svc_vox may add_lead, svc_pulse may not), so ingestion is
            # least-privilege rather than a blanket write.
            if spec.subject_type is not None:
                from evam_backend_core import policy

                from app.authz import enforce_operation
                from app.authz.matrix import CREATE_OPERATION_FOR_SUBJECT
                from app.core.errors import ForbiddenError, ValidationAppError

                # Creation-time lifecycle via the SAME shared authority as PATCH/approval: a
                # terminal/governance state (a Converted lead, a Sanctioned deal, a Disbursed
                # lending line) can never be set at birth — it is reached only through the
                # proper flow or an approved transition — AND any mandatory fields a supplied
                # stage requires must be present. Same rule for humans and machines.
                roles = ctx.user.roles if ctx.user is not None else None
                violation = policy.check_write(spec.subject_type, current={}, changes=body,
                                               roles=roles, is_creation=True)
                if violation is not None:
                    if violation.kind == "forbidden":
                        raise ForbiddenError(violation.message)
                    raise ValidationAppError(violation.message)

                op = CREATE_OPERATION_FOR_SUBJECT.get(spec.subject_type)
                if op is not None:
                    granted = enforce_operation(ctx.user, op)
                    entity_id = body.get("entity_id")
                    if (ctx.user is not None and granted.name == "SCOPED"
                            and entity_id is not None and spec.subject_type != "Entity"):
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

            if spec.pre_write is not None:
                await spec.pre_write(ctx, body, None)
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
        include_reconciliation: bool = Query(
            default=False,
            description="Admin-only: include records still flagged reconciliation_status="
                        "'Required'. By default they are EXCLUDED from operational lists/totals."),
        with_total: bool = Query(default=False, description="Include exact total (slower)"),
    ) -> Any:
        from app.authz.engine import enforce_service_read
        enforce_service_read(spec.prefix, ctx.user)
        _gate_include_deleted(ctx, include_deleted)
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
        # Operational reads EXCLUDE records still flagged reconciliation_status='Required' by
        # default — an incomplete governed import must not silently count toward disbursed/
        # sanctioned totals or trigger downstream processing. Services (no user) can NEVER opt in
        # (fail closed); only an Admin human may, explicitly, via include_reconciliation=true.
        # (Centralised predicate — the same one exports, counts and direct reads apply.)
        if not _recon_included(ctx, include_reconciliation):
            exclude = recon.model_exclusion(spec.repo.model)
            if exclude is not None:
                scope_condition = (exclude if scope_condition is None
                                   else and_(scope_condition, exclude))

        _reserved = {"q", "limit", "cursor", "include_deleted", "include_reconciliation",
                     "with_total", "scope"}
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
        include_reconciliation: bool = Query(
            default=False,
            description="Admin-only: also return a record still flagged reconciliation_status="
                        "'Required' (hidden from operational reads by default)."),
    ) -> Any:
        from app.authz.engine import enforce_service_read
        enforce_service_read(spec.prefix, ctx.user)
        _gate_include_deleted(ctx, include_deleted)
        obj = await repo.get(ctx.session, ctx.tenant_id, obj_id, include_deleted=include_deleted)
        # Fail closed on a known-id read too: a record still requiring reconciliation is invisible
        # to operational callers (services always; humans unless an Admin opts in), so a downstream
        # service that happens to know the id still cannot fetch and process it.
        if (getattr(obj, "reconciliation_status", None) in recon.HIDDEN_STATUSES
                and not _recon_included(ctx, include_reconciliation)):
            from app.core.errors import NotFoundError
            raise NotFoundError(f"{spec.name} '{obj_id}' not found.")
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
            break_glass: str | None = Header(default=None, alias="X-Evidence-Break-Glass"),
        ) -> Any:
            await _enforce_line_write(ctx, spec, obj_id)
            await _enforce_company_write(ctx, spec, obj_id)
            _enforce_simple_write(ctx, spec)  # people / counterparties / checklist update
            data = payload.model_dump(exclude_unset=True)
            await _enforce_transition(ctx, spec, obj_id, data,  # protected transitions
                                      break_glass_justification=break_glass)
            expected = data.pop("expected_version", None)
            if expected is None:
                expected = _parse_if_match(if_match)
            if spec.pre_write is not None:
                await spec.pre_write(ctx, data, obj_id)
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
            from app.authz.revalidate import revalidate_sensitive

            enforce_operation(ctx.user, "delete_row")
            await revalidate_sensitive(ctx, "delete_row")
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
            from app.authz.revalidate import revalidate_sensitive

            enforce_operation(ctx.user, "delete_row")
            await revalidate_sensitive(ctx, "delete_row")
            obj = await repo.restore(ctx.session, ctx.tenant_id, obj_id, ctx.actor)
            return spec.read_schema.model_validate(obj)

    return router
