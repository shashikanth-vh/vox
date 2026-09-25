"""The prospect universe — curated market lists, one step upstream of a lead.

The desk's research produces per-vertical company lists (Solar, ESS, EV, Bioenergy,
Waste & Water …) of companies Evam MAY want to connect with. They are reference
data, not relationships: keeping them out of the client master protects its meaning
(every client traces to a lead), while a first-class row here gives the desk CRUD,
filters, provenance and the one promotion that matters — "Create lead", which hands
over to the ordinary lead → client-master birth-linking.

Identity is CIN-anchored where the lists carry one (they mostly do); the same
company researched into two verticals is ONE prospect carrying both tags — the
import engine (app.imports.prospects_xlsx) merges on CIN, then canonical name +
domain.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class Prospect(RegisterBase):
    __tablename__ = "prospects"
    # A number people can say ("check P-0042") — allocated on create like lead_no.
    __auto_number__ = ("prospect_no", "P-", 4)
    __table_args__ = (
        Index("prospects_tenant_no", "tenant_id", "prospect_no", unique=True,
              postgresql_where=text("deleted_at IS NULL")),
        # One live prospect per CIN per tenant — the import engine's anchor. Rows
        # without a CIN (the lists have a few) fall outside the constraint and are
        # deduplicated by name_key + domain in the engine instead.
        Index("prospects_tenant_cin", "tenant_id", "cin", unique=True,
              postgresql_where=text("deleted_at IS NULL AND cin IS NOT NULL")),
        Index("ix_prospects_tenant_name_key", "tenant_id", "name_key"),
        Index("ix_prospects_tenant_status", "tenant_id", "status"),
        # The chips: membership filters on the JSONB tag lists.
        Index("ix_prospects_verticals", "verticals", postgresql_using="gin"),
        Index("ix_prospects_sub_sectors", "sub_sectors", postgresql_using="gin"),
    )

    prospect_no: Mapped[str | None] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    # canonical_name(name) — the dedupe key shared with the client master's matching
    # (evam_backend_core.company_identity), maintained by the write paths.
    name_key: Mapped[str | None] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(200))
    cin: Mapped[str | None] = mapped_column(String(40))
    overview: Mapped[str | None] = mapped_column(Text)

    # Which curated lists this company appears on (Solar, ESS, …) and the lists' own
    # sub-sector labels (OEM, EPC, Battery Cells & Core Chemistry, …). Tag lists, not
    # the locked deal taxonomy — reconciling the vocabularies is a later, deliberate
    # step; the universe keeps the research's own words.
    verticals: Mapped[list | None] = mapped_column(JSONB)
    sub_sectors: Mapped[list | None] = mapped_column(JSONB)

    emails: Mapped[list | None] = mapped_column(JSONB)
    phones: Mapped[list | None] = mapped_column(JSONB)
    founded_year: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str | None] = mapped_column(String(60))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(60))

    revenue_cr: Mapped[float | None] = mapped_column(Numeric(14, 4))
    net_profit_cr: Mapped[float | None] = mapped_column(Numeric(14, 4))
    ebitda_cr: Mapped[float | None] = mapped_column(Numeric(14, 4))
    total_funding_cr: Mapped[float | None] = mapped_column(Numeric(14, 4))
    latest_funding_cr: Mapped[float | None] = mapped_column(Numeric(14, 4))
    latest_valuation_cr: Mapped[float | None] = mapped_column(Numeric(14, 4))
    latest_funded_on: Mapped[date | None] = mapped_column(Date)

    # The prospecting lifecycle — hygiene, not governance. lead_created is set by the
    # promotion endpoint and is NOT terminal: a prospect can spawn many leads.
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="uncontacted",
                                        server_default="uncontacted")
    remarks: Mapped[str | None] = mapped_column(Text)

    # Provenance + promotion links. lead_ids is append-only (every lead this prospect
    # spawned); entity_id is the client-master row the first promotion settled on.
    source: Mapped[str | None] = mapped_column(String(300))
    import_batch: Mapped[str | None] = mapped_column(String(64))
    lead_ids: Mapped[list | None] = mapped_column(JSONB)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
