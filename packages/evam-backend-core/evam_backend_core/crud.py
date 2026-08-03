"""Generic, concurrency-safe CRUD repository shared by every Register table.

Why a generic repository: every table needs the same create / read / list / update /
soft-delete / restore behaviour with the same guarantees. Writing that once, correctly,
is safer than hand-rolling 18 near-identical copies.

Concurrency guarantees implemented here:
* **No lost updates** — updates are optimistic. The caller's ``expected_version`` is
  checked, and SQLAlchemy additionally emits ``UPDATE ... WHERE version = :v`` so two
  racing writers cannot both win; the loser gets a 409 (``VersionConflictError``).
* **No accidental cross-tenant access** — every query is filtered by ``tenant_id``.
* **No hard deletes** — delete is a soft delete (``deleted_at``); data in the source of
  truth is never silently destroyed.
* **Auditable** — every mutation appends an ``audit_log`` row in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import Boolean, Integer, Numeric, and_, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.exc import StaleDataError

from evam_backend_core.errors import NotFoundError, ValidationAppError, VersionConflictError
from evam_backend_core.logging import request_id_ctx
from evam_backend_core.pagination import decode_cursor, encode_cursor
from evam_backend_core.db.base import AuditLog, RegisterBase

M = TypeVar("M", bound=RegisterBase)

# Value field → history field. When a tracker's stage/status changes via update(), the
# server appends {from,to,at,by} to the JSONB history so the timeline is authoritative
# and append-only, rather than trusting the client to maintain the array (which would be
# last-write-wins under concurrency and could drop events).
_HISTORY_FIELDS = (("stage", "stage_history"), ("status", "status_history"))

# Natural-key / display fields, in preference order — the first non-empty one becomes
# the audit row's human ``label`` so an activity screen can say "PIONEER" instead of a
# UUID without a join per row.
_LABEL_FIELDS = ("code", "tracker_no", "lead_no", "deal_no", "company", "legal_name",
                 "name", "full_name", "title", "email")


def _json_safe(value: Any, cap: int = 300) -> Any:
    """A JSONB-storable, size-bounded rendering of a changed value. Primitives pass
    through; everything else (dates, UUIDs, Decimals, nested structures) becomes its
    string form, truncated so one giant field can never bloat the audit trail."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _label_of(obj: Any) -> str | None:
    for field_name in _LABEL_FIELDS:
        value = getattr(obj, field_name, None)
        if value:
            return str(value)[:120]
    return None


class CRUDRepository(Generic[M]):
    def __init__(
        self,
        model: type[M],
        *,
        searchable: list[str] | None = None,
        filterable: list[str] | None = None,
    ) -> None:
        self.model = model
        self.resource = model.__tablename__
        self.searchable = searchable or []
        # Columns clients may filter by equality on; defaults to declared filterables.
        self.filterable = set(filterable or [])

    # ---- helpers ---------------------------------------------------------
    def _col(self, name: str) -> InstrumentedAttribute:
        return getattr(self.model, name)

    def _coerce_filter(self, name: str, value: Any) -> Any:
        """Coerce a query-string filter value to the column's Python type.

        Filter values arrive as raw strings from the URL; comparing a string to a
        BOOLEAN / UUID / numeric column would fail or silently mismatch, so cast here.
        A value that CANNOT be cast is the caller's error and refuses the request as
        422 — passed through raw it reaches the driver's bind codec and blows up as an
        opaque 500 (the literal string "null" from an unset client variable hitting
        ``entity_id`` took down VOX runs exactly that way).
        """
        if not isinstance(value, str):
            return value
        col_type = self._col(name).property.columns[0].type
        try:
            if isinstance(col_type, Boolean):
                return value.strip().lower() in {"true", "1", "yes", "t"}
            if isinstance(col_type, PGUUID):
                return uuid.UUID(value)
            if isinstance(col_type, Integer):
                return int(value)
            if isinstance(col_type, Numeric):
                return float(value)
        except ValueError:
            kind = ("a UUID" if isinstance(col_type, PGUUID)
                    else "an integer" if isinstance(col_type, Integer) else "a number")
            raise ValidationAppError(
                f"Invalid value {value!r} for filter '{name}' — {kind} is required."
            ) from None
        except AttributeError:
            return value
        return value

    async def _audit(
        self, session: AsyncSession, tenant_id: uuid.UUID, actor: str, action: str,
        resource_id: Any, changes: dict | None = None,
    ) -> None:
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                resource_type=self.resource,
                resource_id=str(resource_id),
                request_id=request_id_ctx.get(),
                changes=changes,
            )
        )

    # ---- create ----------------------------------------------------------
    async def create(
        self, session: AsyncSession, tenant_id: uuid.UUID, actor: str, data: dict
    ) -> M:
        obj = self.model(**data)
        obj.tenant_id = tenant_id
        obj.created_by = actor
        obj.updated_by = actor
        session.add(obj)
        await session.flush()  # surfaces integrity errors now, within the transaction
        await self._audit(session, tenant_id, actor, "create", obj.id,
                          changes={"label": _label_of(obj)})
        await session.refresh(obj)
        return obj

    # ---- read ------------------------------------------------------------
    async def get(
        self, session: AsyncSession, tenant_id: uuid.UUID, obj_id: uuid.UUID,
        *, include_deleted: bool = False,
    ) -> M:
        stmt = select(self.model).where(
            self.model.id == obj_id, self.model.tenant_id == tenant_id
        )
        if not include_deleted:
            stmt = stmt.where(self.model.deleted_at.is_(None))
        obj = (await session.execute(stmt)).scalar_one_or_none()
        if obj is None:
            raise NotFoundError(f"{self.resource} '{obj_id}' not found.")
        return obj

    # ---- list ------------------------------------------------------------
    async def list(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        filters: dict[str, Any] | None = None,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        include_deleted: bool = False,
        with_total: bool = False,
        id_in: list | None = None,
        condition: Any = None,
    ) -> tuple[list[M], str | None, int | None]:
        conds = [self.model.tenant_id == tenant_id]
        if not include_deleted:
            conds.append(self.model.deleted_at.is_(None))
        if id_in is not None:
            # Row-scope restriction (e.g. RBAC scoped access: only assigned lines).
            conds.append(self.model.id.in_(id_in))
        if condition is not None:
            # Arbitrary scope clause (e.g. the RBAC scope evaluator's own-book /
            # connected-company / unassigned-default-owner disjunction).
            conds.append(condition)

        for key, value in (filters or {}).items():
            if value is None:
                continue
            if key not in self.filterable:
                # NEVER drop a filter silently: a caller that asked for
                # entity_id=X and gets the whole tenant back is a data-leak
                # waiting to happen (the wrong-company VOX lead bug).
                raise ValueError(
                    f"'{key}' is not a filterable column for {self.model.__name__} "
                    f"(allowed: {sorted(self.filterable)})")
            conds.append(self._col(key) == self._coerce_filter(key, value))

        if q and self.searchable:
            like = f"%{q}%"
            conds.append(or_(*[self._col(c).ilike(like) for c in self.searchable]))

        total: int | None = None
        if with_total:
            total = (
                await session.execute(select(func.count()).select_from(self.model).where(and_(*conds)))
            ).scalar_one()

        # Keyset pagination on (created_at, id) descending — stable and O(1) at depth.
        if cursor:
            c_created, c_id = decode_cursor(cursor)
            conds.append(
                tuple_(self.model.created_at, self.model.id)
                < tuple_(c_created, uuid.UUID(str(c_id)))  # type: ignore[arg-type]
            )

        stmt = (
            select(self.model)
            .where(and_(*conds))
            .order_by(self.model.created_at.desc(), self.model.id.desc())
            .limit(limit + 1)  # fetch one extra to know if there's a next page
        )
        rows = list((await session.execute(stmt)).scalars().all())

        next_cursor: str | None = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(last.created_at, last.id)
            rows = rows[:limit]
        return rows, next_cursor, total

    # ---- update (optimistic) --------------------------------------------
    async def update(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        obj_id: uuid.UUID,
        actor: str,
        data: dict,
        *,
        expected_version: int | None = None,
    ) -> M:
        obj = await self.get(session, tenant_id, obj_id)
        if expected_version is not None and obj.version != expected_version:
            raise VersionConflictError(expected=expected_version, actual=obj.version)

        # Snapshot the values that carry a history log *before* we mutate them.
        olds = {
            vf: getattr(obj, vf)
            for vf, hf in _HISTORY_FIELDS
            if hasattr(obj, vf) and hasattr(obj, hf)
        }

        changed: dict[str, Any] = {}
        before: dict[str, Any] = {}
        for key, value in data.items():
            if key in {"id", "tenant_id", "version", "created_at", "created_by"}:
                continue
            if getattr(obj, key) != value:
                before[key] = getattr(obj, key)
                changed[key] = value
                setattr(obj, key, value)

        if not changed:
            return obj  # no-op update: don't bump version or write audit noise

        # Auto-append a history event whenever a tracked value field actually changed.
        for vf, hf in _HISTORY_FIELDS:
            if vf in changed and vf in olds:
                hist = list(getattr(obj, hf) or [])
                hist.append({
                    "from": olds[vf], "to": getattr(obj, vf),
                    "at": datetime.now(UTC).isoformat(), "by": actor,
                })
                setattr(obj, hf, hist)

        obj.updated_by = actor
        try:
            await session.flush()  # version_id_col → UPDATE ... WHERE version = current
        except StaleDataError as exc:
            # A concurrent writer won the race between our read and flush.
            raise VersionConflictError(expected=expected_version, actual=None) from exc
        # ``fields`` (names) stays for backward compatibility; ``values`` carries the
        # before→after pairs so an activity screen can render "Data Awaited → Diligence"
        # from the audit row alone, and ``label`` names the row in human terms.
        await self._audit(session, tenant_id, actor, "update", obj.id, changes={
            "fields": list(changed),
            "values": {k: {"from": _json_safe(before[k]), "to": _json_safe(changed[k])}
                       for k in changed},
            "label": _label_of(obj)})
        await session.refresh(obj)
        return obj

    # ---- delete / restore ------------------------------------------------
    async def soft_delete(
        self, session: AsyncSession, tenant_id: uuid.UUID, obj_id: uuid.UUID, actor: str,
        *, expected_version: int | None = None,
    ) -> None:
        obj = await self.get(session, tenant_id, obj_id)
        if expected_version is not None and obj.version != expected_version:
            raise VersionConflictError(expected=expected_version, actual=obj.version)
        obj.deleted_at = func.now()
        obj.updated_by = actor
        try:
            await session.flush()
        except StaleDataError as exc:
            raise VersionConflictError(expected=expected_version, actual=None) from exc
        await self._audit(session, tenant_id, actor, "delete", obj.id,
                          changes={"label": _label_of(obj)})

    async def restore(
        self, session: AsyncSession, tenant_id: uuid.UUID, obj_id: uuid.UUID, actor: str
    ) -> M:
        obj = await self.get(session, tenant_id, obj_id, include_deleted=True)
        if obj.deleted_at is None:
            return obj
        obj.deleted_at = None
        obj.updated_by = actor
        await session.flush()
        await self._audit(session, tenant_id, actor, "restore", obj.id,
                          changes={"label": _label_of(obj)})
        await session.refresh(obj)
        return obj
