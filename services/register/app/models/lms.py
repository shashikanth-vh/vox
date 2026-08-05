"""The LMS core — lending increment ④.

``LoanAccount`` is opened AUTOMATICALLY on the first Advaya-confirmed disbursement
tranche (never by a PRISM approval): the account header the servicing team keeps in
their statement sheet — account number, borrower, facility, disbursement date, amount,
rate, tenor, EMI, day count, classification. Later tranches raise its principal.

``LoanLedgerEntry`` is the statement itself: Date | Particulars | Debit | Credit |
Balance, append-only in spirit — corrections are new entries. Interest rows are
COMPUTED (balance × rate × days / day-count) through the accrue endpoint, so the
figures are reproducible, never hand-keyed arithmetic.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class LoanAccount(RegisterBase):
    __tablename__ = "loan_accounts"

    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deal_id: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    account_no: Mapped[int] = mapped_column(Integer, nullable=False)

    borrower: Mapped[str | None] = mapped_column(String(300))
    facility_type: Mapped[str | None] = mapped_column(String(80))
    disbursed_on: Mapped[date | None] = mapped_column(Date)
    # The account's principal — cumulative confirmed disbursements (grows per tranche).
    amount: Mapped[float | None] = mapped_column(Numeric(20, 7))
    rate_kind: Mapped[str] = mapped_column(String(10), nullable=False, default="Fixed",
                                           server_default="Fixed")
    rate_pct: Mapped[float | None] = mapped_column(Numeric(7, 4))
    tenor_months: Mapped[int | None] = mapped_column(Integer)
    emi_amount: Mapped[float | None] = mapped_column(Numeric(20, 7))
    repayment_start: Mapped[date | None] = mapped_column(Date)
    day_count: Mapped[str] = mapped_column(String(8), nullable=False, default="365",
                                           server_default="365")

    # Classification, as the sheet keeps it: Standard | SMA | Sub-Standard | Doubtful | Loss.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="Standard",
                                        server_default="Standard")
    overdue_position: Mapped[str] = mapped_column(String(120), nullable=False,
                                                  default="Nil", server_default="Nil")
    provisioning_amount: Mapped[float | None] = mapped_column(Numeric(20, 7))
    closed_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)


class LoanLedgerEntry(RegisterBase):
    __tablename__ = "loan_ledger_entries"

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("loan_accounts.id", ondelete="CASCADE"),
        nullable=False)
    entry_no: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    particulars: Mapped[str] = mapped_column(String(300), nullable=False)
    # Disbursement | Interest | EMI | Receipt | Charge | Adjustment
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    debit: Mapped[float | None] = mapped_column(Numeric(20, 7))
    credit: Mapped[float | None] = mapped_column(Numeric(20, 7))
    balance: Mapped[float] = mapped_column(Numeric(20, 7), nullable=False)


class LoanAccountCondition(RegisterBase):
    """The LMS's OWN register of a loan's CP/CS conditions — handed over from the LOS
    checklist when the account OPENS (completed and uncompleted items alike), owned by
    servicing from then on. The LOS checklist freezes into a decision record; receipts,
    expiry and reminders live here, with no runtime dependency on LOS."""

    __tablename__ = "loan_account_conditions"

    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("loan_accounts.id", ondelete="CASCADE"),
        nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(1000), nullable=False)
    condition_type: Mapped[str] = mapped_column(String(8), nullable=False, default="CS",
                                                server_default="CS")
    required: Mapped[bool] = mapped_column(nullable=False, default=True,
                                           server_default="true")
    # Pending | Completed | Waived | Deferred as CS (open) — carried over as handed.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Pending",
                                        server_default="Pending")
    reason: Mapped[str | None] = mapped_column(Text)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    evidence_ref: Mapped[str | None] = mapped_column(String(300))
    source_version: Mapped[int | None] = mapped_column(Integer)  # checklist version
    completed_on: Mapped[date | None] = mapped_column(Date)
    completed_by: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
