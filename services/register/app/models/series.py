"""Number series — controlled, gap-aware document numbering.

A bank numbers its instruments from a register, not from whoever is typing: the credit
note sent to committee carries the NEXT number in its series, and two makers sending on
the same day can never collide. One row per series (``series_key`` names it — e.g.
``credit-note/ACME/202608``), and the mint endpoint advances ``last_value`` atomically,
so the same number is never issued twice even under concurrent sends.
"""

from __future__ import annotations

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class NumberSeries(RegisterBase):
    """One numbering series: the key that names it and the last value issued."""

    __tablename__ = "number_series"
    __table_args__ = (
        # A REAL unique constraint (not a partial index): the mint upsert's ON CONFLICT
        # arbiter. Series rows are never soft-deleted — a series that stopped being used
        # simply stops advancing.
        UniqueConstraint("tenant_id", "series_key", name="number_series_tenant_key"),
    )

    series_key: Mapped[str] = mapped_column(String(200), nullable=False)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                            server_default="0")
