"""Sanction terms + the CAM workbench record (lending increments 1–2).

``SanctionTerms`` is the structured sanction — captured ONCE at committee approval and
seeding the CP/CS checklist and covenant register (and, later, the LMS account) from a
single save. The seeded artefacts keep their own lifecycles; this row remembers what was
entered and what it seeded.

``CamReport`` follows the CpcsChecklist maker-checker shape verbatim: one row per CAM
VERSION, Draft → Submitted → Approved | Returned | Rejected, the preparer barred from
deciding. It also records its provenance — which engine (provider:model) drafted it,
from which documents and which prompt doc — and ``CamTurn`` keeps the rework transcript,
so "why does the CAM say this?" always has an answer.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class SanctionTerms(RegisterBase):
    __tablename__ = "sanction_terms"

    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deal_id: Mapped[str | None] = mapped_column(String(64))

    amount_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    rate_kind: Mapped[str] = mapped_column(String(10), nullable=False, default="Fixed",
                                           server_default="Fixed")   # Fixed | Floating
    rate_pct: Mapped[float | None] = mapped_column(Numeric(7, 4))
    spread_pct: Mapped[float | None] = mapped_column(Numeric(7, 4))
    tenor_months: Mapped[int | None] = mapped_column(Integer)
    emi_amount: Mapped[float | None] = mapped_column(Numeric(16, 2))
    repayment_start: Mapped[date | None] = mapped_column(Date)
    day_count: Mapped[str] = mapped_column(String(8), nullable=False, default="365",
                                           server_default="365")     # 365 | 360
    penal_rate_pct: Mapped[float | None] = mapped_column(Numeric(7, 4))
    moratorium_months: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                   server_default="0")
    schedule_kind: Mapped[str] = mapped_column(String(10), nullable=False, default="EMI",
                                               server_default="EMI") # EMI | Bullet | Custom

    cp_items: Mapped[list | None] = mapped_column(JSONB)
    cs_items: Mapped[list | None] = mapped_column(JSONB)
    covenants: Mapped[list | None] = mapped_column(JSONB)
    seeded_checklist_id: Mapped[str | None] = mapped_column(String(64))
    seeded_covenant_ids: Mapped[list | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)


class CamReport(RegisterBase):
    __tablename__ = "cam_reports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "lending_id", "report_version",
                         name="cam_reports_tenant_lending_version"),
    )

    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deal_id: Mapped[str | None] = mapped_column(String(64))
    report_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Draft (workbench) → Submitted (to committee) → Approved | Returned | Rejected.
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="Draft")
    engine: Mapped[str | None] = mapped_column(String(120))   # "anthropic:claude-haiku-…"
    source_doc_ids: Mapped[list | None] = mapped_column(JSONB)
    prompt_doc_id: Mapped[str | None] = mapped_column(String(64))
    draft_md: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[str | None] = mapped_column(String(64))  # the finalised register doc
    prepared_by: Mapped[str | None] = mapped_column(String(120))
    prepared_by_id: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(120))
    decided_by_id: Mapped[str | None] = mapped_column(String(64))
    decision_note: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class CamTurn(RegisterBase):
    __tablename__ = "cam_turns"

    report_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cam_reports.id", ondelete="CASCADE"),
        nullable=False)
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)   # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
