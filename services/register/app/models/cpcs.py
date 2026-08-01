"""The CP/CS (conditions precedent / subsequent) checklist — the AUTHORITATIVE source the
``cp_cs_completion`` governance evidence is minted from.

Before this existed, ``cp_cs_completion`` was caller-attached: whoever could attach evidence could
assert CP/CS was done. Now the evidence is VERIFIED (``evidence.py::_verify_cpcs_checklist``) against
an ``Approved`` checklist for the same Lending line — a checklist a maker COMPLETED and a DIFFERENT
checker APPROVED (maker-checker). A Draft/Completed checklist mints nothing; only an approved one
does. Single-winner on ``(tenant_id, lending_id, version)``; once ``Approved``/``Rejected`` the row is
frozen (a trigger blocks further mutation) so the record the evidence cites can never change."""

from __future__ import annotations

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class CpcsChecklist(RegisterBase):
    __tablename__ = "cp_cs_checklists"
    __table_args__ = (
        UniqueConstraint("tenant_id", "lending_id", "checklist_version",
                         name="cp_cs_checklists_tenant_lending_version"),
    )

    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deal_id: Mapped[str | None] = mapped_column(String(64))
    # Business version of the checklist (distinct from RegisterBase.version, the optimistic lock).
    checklist_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # [{key, label, required (bool), status ('Pending'|'Completed'|'Waived'), note}]
    items: Mapped[list | None] = mapped_column(JSONB)
    # Draft -> Completed -> Approved | Rejected | Returned (checker sent it back; the maker
    # amends by submitting the NEXT checklist_version — returned versions stay on record).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Draft")
    prepared_by: Mapped[str | None] = mapped_column(String(120))
    prepared_by_id: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_by_id: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
