"""Durable Advaya handover package + authoritative CP/CS checklist, and the proposed-disbursement
fields — so a handover proves WHAT was handed over and CP/CS completion is verified, not asserted.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-29

Three changes, one milestone:

* ``lending_tracker`` gains ``proposed_disbursement_amount`` / ``proposed_disbursement_date`` —
  the PROPOSED drawdown fixed at 'Ready for Disbursement' and carried into the handover package.
  The pre-existing ``disbursed_amount`` / ``disbursement_date`` are reserved for a real disbursement
  confirmation (future Advaya integration); PRISM never sets them on its own authority.

* ``cp_cs_checklists`` — the authoritative CP/CS checklist. ``cp_cs_completion`` evidence is minted
  ONLY from an ``Approved`` checklist (maker completed, a DIFFERENT checker approved). A trigger
  freezes the row once it reaches a terminal status ('Approved'/'Rejected'), and blocks DELETE, so
  the record the evidence cites can never change.

* ``advaya_handover_packages`` — the durable, immutable snapshot of a handover. A trigger blocks
  DELETE and every UPDATE except a one-time set of the (initially NULL) manual ``advaya_reference``.
  Created transactionally with the advance to 'Disbursed'.

All tenant-scoped with fail-closed RLS, matching the existing governance tables.
"""

from __future__ import annotations

import os

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _enable_rls(table: str, policy: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {policy} ON {table}
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")


def _grant(table: str, privileges: str) -> None:
    # ``table``/``privileges`` are literal constants supplied by this migration (not user input).
    stmt = (
        "DO $$ BEGIN "  # noqa: S608
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN "
        f"EXECUTE 'GRANT {privileges} ON {table} TO register_app'; "
        "END IF; "
        "EXCEPTION WHEN insufficient_privilege THEN "
        f"RAISE NOTICE '{table} grant to register_app skipped.'; "
        "END $$;"
    )
    op.execute(stmt)


def upgrade() -> None:
    # -- proposed-disbursement fields ----------------------------------------
    op.execute("ALTER TABLE lending_tracker ADD COLUMN proposed_disbursement_amount numeric(14,2);")
    op.execute("ALTER TABLE lending_tracker ADD COLUMN proposed_disbursement_date date;")

    # -- CP/CS checklist -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE cp_cs_checklists (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            lending_id        varchar(64) NOT NULL,
            deal_id           varchar(64),
            checklist_version integer     NOT NULL DEFAULT 1,
            items             jsonb,
            status         varchar(20) NOT NULL DEFAULT 'Draft',
            prepared_by    varchar(120),
            prepared_by_id varchar(64),
            approved_by    varchar(120),
            approved_by_id varchar(64),
            note           text,
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz,
            CONSTRAINT cp_cs_checklists_status
                CHECK (status IN ('Draft', 'Completed', 'Approved', 'Rejected', 'Returned')),
            CONSTRAINT cp_cs_checklists_tenant_lending_version
                UNIQUE (tenant_id, lending_id, checklist_version)
        );
        """
    )
    op.execute("CREATE INDEX ix_cp_cs_checklists_lending "
               "ON cp_cs_checklists (tenant_id, lending_id);")
    # Freeze once terminal (Approved/Rejected); never delete — the evidence cites this record.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cp_cs_checklist_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'cp_cs_checklists rows are append-only and cannot be deleted';
            END IF;
            IF OLD.status IN ('Approved', 'Rejected', 'Returned') THEN
                RAISE EXCEPTION 'cp_cs_checklists row % is % and is frozen', OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_cp_cs_checklist_guard
        BEFORE UPDATE OR DELETE ON cp_cs_checklists
        FOR EACH ROW EXECUTE FUNCTION cp_cs_checklist_guard();
        """
    )
    _enable_rls("cp_cs_checklists", "cp_cs_checklists_tenant_isolation")
    _grant("cp_cs_checklists", "SELECT, INSERT, UPDATE")

    # -- Advaya handover package --------------------------------------------
    op.execute(
        """
        CREATE TABLE advaya_handover_packages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            handover_key                 varchar(200) NOT NULL,
            lending_id                   varchar(64)  NOT NULL,
            deal_id                      varchar(64),
            facility_amount              numeric(14,2),
            proposed_disbursement_amount numeric(14,2),
            proposed_disbursement_date   date,
            cpcs_checklist_version       integer,
            executed_document_refs       jsonb,
            package_reference            varchar(300),
            package_sha256               varchar(64),
            package_document             text,
            initiated_by                 varchar(120),
            initiated_by_id              varchar(64),
            approved_by                  varchar(120),
            approved_by_id               varchar(64),
            delivery_method              varchar(60),
            recipient                    varchar(200),
            advaya_reference             varchar(200),
            status                       varchar(20) NOT NULL DEFAULT 'Prepared',
            CONSTRAINT advaya_handover_packages_status
                CHECK (status IN ('Prepared', 'HandedOver', 'Returned')),
            note                         text,
            snapshot                     jsonb,
            tenant_id  uuid        NOT NULL,
            version    integer     NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120),
            deleted_at timestamptz,
            CONSTRAINT advaya_handover_packages_tenant_key UNIQUE (tenant_id, handover_key)
        );
        """
    )
    op.execute("CREATE INDEX ix_advaya_handover_packages_lending "
               "ON advaya_handover_packages (tenant_id, lending_id);")
    # Two-phase: mutable while 'Prepared' (the maker's draft + the checker's approval transition),
    # then FROZEN once 'HandedOver' — except the manual advaya_reference, which may be set ONCE from
    # NULL (operator's Advaya-side reference, available only later). DELETE is always refused.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION advaya_handover_package_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'advaya_handover_packages rows cannot be deleted';
            END IF;
            IF OLD.status IN ('Prepared', 'Returned') THEN
                RETURN NEW;   -- preparation / return / re-prepare / approval transitions
            END IF;
            -- OLD.status = 'HandedOver' → frozen except a one-time advaya_reference set.
            IF OLD.advaya_reference IS NOT NULL OR NEW.advaya_reference IS NULL THEN
                RAISE EXCEPTION 'advaya_handover_packages row % is immutable once handed over', OLD.id;
            END IF;
            IF ROW(NEW.handover_key, NEW.lending_id, NEW.deal_id, NEW.facility_amount,
                   NEW.proposed_disbursement_amount, NEW.proposed_disbursement_date,
                   NEW.cpcs_checklist_version, NEW.executed_document_refs, NEW.package_reference,
                   NEW.package_sha256, NEW.package_document, NEW.initiated_by, NEW.initiated_by_id,
                   NEW.approved_by, NEW.approved_by_id, NEW.delivery_method, NEW.recipient,
                   NEW.status, NEW.note, NEW.snapshot, NEW.tenant_id, NEW.created_at, NEW.created_by)
               IS DISTINCT FROM
               ROW(OLD.handover_key, OLD.lending_id, OLD.deal_id, OLD.facility_amount,
                   OLD.proposed_disbursement_amount, OLD.proposed_disbursement_date,
                   OLD.cpcs_checklist_version, OLD.executed_document_refs, OLD.package_reference,
                   OLD.package_sha256, OLD.package_document, OLD.initiated_by, OLD.initiated_by_id,
                   OLD.approved_by, OLD.approved_by_id, OLD.delivery_method, OLD.recipient,
                   OLD.status, OLD.note, OLD.snapshot, OLD.tenant_id, OLD.created_at, OLD.created_by)
            THEN
                RAISE EXCEPTION 'advaya_handover_packages row % is immutable (only advaya_reference '
                                'may be set once)', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_advaya_handover_package_guard
        BEFORE UPDATE OR DELETE ON advaya_handover_packages
        FOR EACH ROW EXECUTE FUNCTION advaya_handover_package_guard();
        """
    )
    _enable_rls("advaya_handover_packages", "advaya_handover_packages_tenant_isolation")
    _grant("advaya_handover_packages", "SELECT, INSERT, UPDATE")

    # -- Disbursement tranches ----------------------------------------------
    # Tranche-level disbursement callbacks (Advaya, or ops on its behalf): one row per
    # tranche, idempotent on (tenant, lending, tranche_ref), append-only — a recorded
    # disbursement is a fact, never edited; a correction is a NEW tranche (negative
    # amounts are refused at the API; reversals get their own ref and a note).
    op.execute(
        """
        CREATE TABLE disbursement_tranches (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            lending_id    varchar(64)  NOT NULL,
            deal_id       varchar(64),
            tranche_ref   varchar(200) NOT NULL,
            amount        numeric(14,2) NOT NULL,
            disbursed_on  date,
            advaya_reference varchar(200),
            note          text,
            recorded_by   varchar(120),
            tenant_id  uuid        NOT NULL,
            version    integer     NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120),
            deleted_at timestamptz,
            CONSTRAINT disbursement_tranches_tenant_ref
                UNIQUE (tenant_id, lending_id, tranche_ref)
        );
        """
    )
    op.execute("CREATE INDEX ix_disbursement_tranches_lending "
               "ON disbursement_tranches (tenant_id, lending_id);")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION disbursement_tranche_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'disbursement_tranches rows are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_disbursement_tranche_guard
        BEFORE UPDATE OR DELETE ON disbursement_tranches
        FOR EACH ROW EXECUTE FUNCTION disbursement_tranche_guard();
        """
    )
    _enable_rls("disbursement_tranches", "disbursement_tranches_tenant_isolation")
    _grant("disbursement_tranches", "SELECT, INSERT")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS disbursement_tranches;")
    op.execute("DROP FUNCTION IF EXISTS disbursement_tranche_guard();")
    op.execute("DROP TABLE IF EXISTS advaya_handover_packages;")
    op.execute("DROP FUNCTION IF EXISTS advaya_handover_package_guard();")
    op.execute("DROP TABLE IF EXISTS cp_cs_checklists;")
    op.execute("DROP FUNCTION IF EXISTS cp_cs_checklist_guard();")
    op.execute("ALTER TABLE lending_tracker DROP COLUMN IF EXISTS proposed_disbursement_amount;")
    op.execute("ALTER TABLE lending_tracker DROP COLUMN IF EXISTS proposed_disbursement_date;")
