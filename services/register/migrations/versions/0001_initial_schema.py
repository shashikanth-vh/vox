"""The Release-1 Register schema — one baseline, no incremental migrations.

Revision ID: 0001
Revises:
Create Date: 2026-08-01

Release 1 ships as a BASELINE: `alembic upgrade head` (run by the container entrypoint)
creates the complete schema in one pass. Each ``_base_*`` section below is a former
incremental migration, preserved verbatim in its original order — the constraints,
triggers, guards and row-level-security policies stay explicit and reviewable, and the
section boundaries keep the domain story readable (master tables → documents → RBAC →
fail-closed RLS → decisions → outbox → reconciliation → evidence → Advaya → CP/CS).

The only net edit versus the historical chain: the Register never creates identity
tables (users/user_roles) — identity lives in the Access service (its own database), and
the original chain created-then-moved them. Everything else is byte-identical DDL,
verified by diffing `pg_dump --schema-only` of this file against the historical chain.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------- #
# initial Register schema — PRISM 7 master tables + ATLAS operational tables
# --------------------------------------------------------------------------- #
def _base_0001_initial_schema() -> None:
    # Every business table shares these trailing columns (tenant/version/audit/soft-delete).
    common = """
        tenant_id      uuid        NOT NULL,
        version        integer     NOT NULL DEFAULT 1,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        created_by     varchar(120),
        updated_by     varchar(120),
        deleted_at     timestamptz
    """


    def _table(name: str, columns: str) -> None:
        op.execute(
            f"""
            CREATE TABLE {name} (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                {columns},
                {common}
            );
            """
        )
        # Auto-maintain updated_at on every UPDATE.
        op.execute(
            f"""
            CREATE TRIGGER trg_{name}_updated_at BEFORE UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )
        op.execute(f"CREATE INDEX ix_{name}_tenant ON {name} (tenant_id);")
        op.execute(f"CREATE INDEX ix_{name}_tenant_active ON {name} (tenant_id) WHERE deleted_at IS NULL;")


    def upgrade() -> None:
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")   # gen_random_uuid()
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")    # trigram search indexes

        op.execute(
            """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
        )

        # --- system tables ---------------------------------------------------
        op.execute(
            """
            CREATE TABLE tenants (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                code varchar(40) NOT NULL UNIQUE,
                name varchar(200) NOT NULL,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                created_by varchar(120),
                updated_by varchar(120)
            );
            """
        )
        op.execute(
            """
            CREATE TABLE tenant_settings (
                tenant_id uuid PRIMARY KEY,
                settings jsonb NOT NULL DEFAULT '{}',
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                created_by varchar(120),
                updated_by varchar(120)
            );
            """
        )
        op.execute(
            """
            CREATE TABLE ref_values (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                category varchar(60) NOT NULL,
                value varchar(120) NOT NULL,
                label varchar(160),
                sort_order integer NOT NULL DEFAULT 0,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                created_by varchar(120),
                updated_by varchar(120),
                CONSTRAINT ref_values_category_value UNIQUE (category, value)
            );
            CREATE INDEX ix_ref_values_category ON ref_values (category);
            """
        )
        op.execute(
            """
            CREATE TABLE idempotency_keys (
                tenant_id uuid NOT NULL,
                key varchar(200) NOT NULL,
                request_hash varchar(64) NOT NULL,
                method varchar(10) NOT NULL,
                path varchar(300) NOT NULL,
                status_code integer NOT NULL,
                response_body jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz NOT NULL,
                PRIMARY KEY (tenant_id, key)
            );
            CREATE INDEX ix_idempotency_expires ON idempotency_keys (expires_at);
            """
        )
        op.execute(
            """
            CREATE TABLE audit_log (
                id bigserial PRIMARY KEY,
                tenant_id uuid,
                at timestamptz NOT NULL DEFAULT now(),
                actor varchar(120),
                action varchar(40) NOT NULL,
                resource_type varchar(60),
                resource_id varchar(64),
                request_id varchar(64),
                changes jsonb
            );
            CREATE INDEX ix_audit_tenant ON audit_log (tenant_id);
            CREATE INDEX ix_audit_at ON audit_log (at);
            CREATE INDEX ix_audit_resource ON audit_log (resource_type, resource_id);
            """
        )

        # --- 1. entities -----------------------------------------------------
        _table("entities", """
            code varchar(60) NOT NULL,
            legal_name varchar(300) NOT NULL,
            display_name varchar(300),
            entity_type varchar(40) NOT NULL DEFAULT 'Company',
            cin varchar(21),
            pan varchar(10),
            gstin varchar(15),
            sector varchar(60),
            sub_sector varchar(120),
            lens varchar(20),
            state varchar(60),
            location varchar(200),
            register_status varchar(40),
            promoter_group_code varchar(60),
            about text,
            toi text,
            notes text,
            tags jsonb,
            CONSTRAINT entities_tenant_code UNIQUE (tenant_id, code)
        """)
        op.execute("CREATE INDEX ix_entities_tenant_sector ON entities (tenant_id, sector);")
        op.execute("CREATE INDEX ix_entities_tenant_cin ON entities (tenant_id, cin);")
        op.execute("CREATE INDEX ix_entities_promoter_group ON entities (tenant_id, promoter_group_code);")
        op.execute("CREATE INDEX ix_entities_legal_name_trgm "
                   "ON entities USING gin (legal_name gin_trgm_ops);")
        op.execute("CREATE INDEX ix_entities_tags ON entities USING gin (tags);")

        # --- people ----------------------------------------------------------
        _table("people", """
            name varchar(120) NOT NULL,
            full_name varchar(200) NOT NULL,
            role varchar(30) NOT NULL,
            email varchar(200),
            phone varchar(30),
            geography varchar(120),
            sectors varchar(300),
            started_on date,
            reports_to varchar(200),
            inactive boolean NOT NULL DEFAULT false,
            notes text,
            CONSTRAINT people_tenant_full_name UNIQUE (tenant_id, full_name)
        """)

        # --- counterparties --------------------------------------------------
        _table("counterparties", """
            name varchar(200) NOT NULL,
            short_name varchar(60),
            counterparty_type varchar(40),
            is_active boolean NOT NULL DEFAULT true,
            sectors varchar(400),
            ticket_min_cr numeric(14,2),
            ticket_max_cr numeric(14,2),
            notes text,
            CONSTRAINT counterparties_tenant_name UNIQUE (tenant_id, name)
        """)
        op.execute("CREATE INDEX ix_counterparties_type ON counterparties (tenant_id, counterparty_type);")

        # --- 2. deals --------------------------------------------------------
        _table("deals", """
            deal_no varchar(40),
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
            code varchar(60),
            product_type varchar(60),
            is_lending boolean NOT NULL DEFAULT false,
            is_syndication boolean NOT NULL DEFAULT false,
            is_asset_mon boolean NOT NULL DEFAULT false,
            rm varchar(120),
            analyst varchar(120),
            stage varchar(30),
            temperature varchar(10),
            source varchar(40),
            source_detail varchar(200),
            source_name varchar(200),
            date_received date,
            ic_date date,
            sanction_date date,
            disbursement_date date,
            exit_date date,
            remarks text,
            CONSTRAINT deals_tenant_deal_no UNIQUE (tenant_id, deal_no)
        """)
        op.execute("CREATE INDEX ix_deals_tenant_entity ON deals (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_deals_entity_fk ON deals (entity_id);")
        op.execute("CREATE INDEX ix_deals_code ON deals (tenant_id, code);")
        # deals.stage is the COMMERCIAL origination funnel (rbac.DEAL_FUNNEL_STAGES) — the deal's
        # ONLY lifecycle; the bank/NBFC credit pipeline lives on lending_tracker.stage.
        op.execute("CREATE INDEX ix_deals_stage ON deals (tenant_id, stage);")

        # --- leads -----------------------------------------------------------
        _table("leads", """
            lead_no varchar(40),
            entity_id uuid REFERENCES entities(id) ON DELETE SET NULL,
            company varchar(300) NOT NULL,
            sector varchar(60),
            lens varchar(20),
            source varchar(40),
            source_name varchar(200),
            rm varchar(120),
            status varchar(20) NOT NULL DEFAULT 'Active',
            temperature varchar(10),
            contact varchar(200),
            designation varchar(120),
            phone varchar(40),
            last_interaction_date date,
            next_action text,
            next_action_date date,
            converted_deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            conv varchar(120),
            notes text,
            CONSTRAINT leads_tenant_lead_no UNIQUE (tenant_id, lead_no)
        """)
        op.execute("CREATE INDEX ix_leads_tenant_status ON leads (tenant_id, status);")
        op.execute("CREATE INDEX ix_leads_entity ON leads (entity_id);")

        # --- lending_tracker -------------------------------------------------
        _table("lending_tracker", """
            tracker_no varchar(40),
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            amount_cr numeric(14,2),
            rm varchar(120),
            analyst varchar(120),
            stage varchar(40),
            stage_updated_at date,
            sanction_date date,
            disbursed_amount numeric(14,2),
            disbursement_date date,
            pending_with varchar(20),
            remarks text,
            stage_history jsonb,
            CONSTRAINT lending_tracker_tenant_no UNIQUE (tenant_id, tracker_no)
        """)
        op.execute("CREATE INDEX ix_lending_tenant_entity ON lending_tracker (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_lending_tenant_stage ON lending_tracker (tenant_id, stage);")
        op.execute("CREATE INDEX ix_lending_entity_fk ON lending_tracker (entity_id);")

        # --- syndication_tracker ---------------------------------------------
        _table("syndication_tracker", """
            tracker_no varchar(40),
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            toi varchar(200),
            rm varchar(120),
            analyst varchar(120),
            lc varchar(120),
            priority varchar(10),
            status varchar(40),
            amount_cr numeric(14,2),
            line varchar(120),
            facility text,
            tenor varchar(20),
            mandate_status text,
            potential text,
            im_status varchar(40),
            sanctioned_lender text,
            ip_lender text,
            date_of_sanction date,
            month_of_sanction varchar(12),
            nature text,
            existing text,
            price text,
            syndication_type varchar(80),
            mandate_status3 varchar(40),
            pending_with varchar(20),
            remarks text,
            status_history jsonb,
            CONSTRAINT syndication_tracker_tenant_no UNIQUE (tenant_id, tracker_no)
        """)
        op.execute("CREATE INDEX ix_syn_tenant_entity ON syndication_tracker (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_syn_tenant_status ON syndication_tracker (tenant_id, status);")
        op.execute("CREATE INDEX ix_syn_entity_fk ON syndication_tracker (entity_id);")

        # --- syndication_lenders ---------------------------------------------
        _table("syndication_lenders", """
            syndication_id uuid NOT NULL REFERENCES syndication_tracker(id) ON DELETE CASCADE,
            counterparty_id uuid REFERENCES counterparties(id) ON DELETE SET NULL,
            lender_name varchar(200) NOT NULL,
            is_existing boolean NOT NULL DEFAULT false,
            status varchar(40),
            since date,
            response_date date,
            chased_date date,
            note text,
            status_history jsonb
        """)
        op.execute("CREATE INDEX ix_synlender_tenant_syn ON syndication_lenders (tenant_id, syndication_id);")
        op.execute("CREATE INDEX ix_synlender_syn_fk ON syndication_lenders (syndication_id);")
        op.execute("CREATE INDEX ix_synlender_tenant_status ON syndication_lenders (tenant_id, status);")

        # --- asset_monetisation ----------------------------------------------
        _table("asset_monetisation", """
            tracker_no varchar(40),
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            state varchar(60),
            indicative_value_cr numeric(14,2),
            size_mw numeric(12,2),
            nature varchar(40),
            deal_type varchar(80),
            investor text,
            investor_type varchar(60),
            status varchar(40),
            teaser_date date,
            notes text,
            CONSTRAINT asset_mon_tenant_no UNIQUE (tenant_id, tracker_no)
        """)
        op.execute("CREATE INDEX ix_am_tenant_entity ON asset_monetisation (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_am_tenant_status ON asset_monetisation (tenant_id, status);")

        # --- 3. financials (versioned) ---------------------------------------
        _table("financials", """
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            statement_type varchar(40) NOT NULL,
            period_type varchar(20),
            period_start date,
            period_end date NOT NULL,
            fiscal_year varchar(12),
            as_of_date date,
            version_no integer NOT NULL DEFAULT 1,
            is_current boolean NOT NULL DEFAULT true,
            provenance text,
            source_document_ref varchar(300),
            currency varchar(3) NOT NULL DEFAULT 'INR',
            is_consolidated boolean,
            is_audited boolean,
            scale varchar(20),
            revenue numeric(18,2),
            ebitda numeric(18,2),
            pat numeric(18,2),
            total_debt numeric(18,2),
            net_worth numeric(18,2),
            dscr numeric(8,3),
            data jsonb,
            CONSTRAINT financials_unique_version
                UNIQUE (tenant_id, entity_id, statement_type, period_end, version_no)
        """)
        op.execute("CREATE INDEX ix_financials_entity ON financials (tenant_id, entity_id);")
        op.execute(
            "CREATE INDEX ix_financials_entity_current ON financials (tenant_id, entity_id, is_current);"
        )
        # At most one current row per (entity, statement_type, period_end).
        op.execute(
            """
            CREATE UNIQUE INDEX uq_financials_one_current
            ON financials (tenant_id, entity_id, statement_type, period_end)
            WHERE is_current AND deleted_at IS NULL;
            """
        )

        # --- 4. contracts_assets ---------------------------------------------
        _table("contracts_assets", """
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            asset_type varchar(40) NOT NULL,
            title varchar(300),
            counterparty_id uuid REFERENCES counterparties(id) ON DELETE SET NULL,
            counterparty_name varchar(200),
            capacity_mw numeric(12,2),
            tariff numeric(10,4),
            location varchar(200),
            state varchar(60),
            start_date date,
            end_date date,
            tenor_years numeric(6,2),
            contract_value_cr numeric(14,2),
            status varchar(60),
            details jsonb
        """)
        op.execute("CREATE INDEX ix_contracts_tenant_entity ON contracts_assets (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_contracts_entity_fk ON contracts_assets (entity_id);")

        # --- 5. interactions (master table 5, "Touchpoints") -----------------
        # The single interaction/touchpoint record. Polymorphic subject (ATLAS refType/refId);
        # holds both the manual note and the full VOX-captured touchpoint.
        _table("interactions", """
            subject_type varchar(30) NOT NULL,
            subject_id uuid NOT NULL,
            entity_id uuid REFERENCES entities(id) ON DELETE CASCADE,
            deal_id uuid REFERENCES deals(id) ON DELETE CASCADE,
            syndication_lender_id uuid REFERENCES syndication_lenders(id) ON DELETE SET NULL,
            lender_name varchar(200),
            interaction_type varchar(60) NOT NULL,
            direction varchar(20),
            occurred_at timestamptz NOT NULL DEFAULT now(),
            summary varchar(300),
            notes text,
            outcome text,
            performed_by varchar(120),
            contact_name varchar(200),
            next_action text,
            next_action_date date,
            next_meeting_date date,
            transcript text,
            language varchar(20),
            gps_lat numeric(9,6),
            gps_lng numeric(9,6),
            location varchar(200),
            attendees jsonb,
            key_intel jsonb,
            next_steps jsonb,
            source_ref varchar(200),
            source varchar(20),
            attachments jsonb,
            meta jsonb
        """)
        op.execute("CREATE INDEX ix_interactions_subject_time "
                   "ON interactions (tenant_id, subject_type, subject_id, occurred_at);")
        op.execute("CREATE INDEX ix_interactions_entity_time "
                   "ON interactions (tenant_id, entity_id, occurred_at);")
        op.execute("CREATE INDEX ix_interactions_deal_time "
                   "ON interactions (tenant_id, deal_id, occurred_at);")
        op.execute("CREATE INDEX ix_interactions_tenant_type ON interactions (tenant_id, interaction_type);")
        op.execute("CREATE INDEX ix_interactions_syn_lender "
                   "ON interactions (tenant_id, syndication_lender_id);")

        # --- 6. external_intelligence ----------------------------------------
        _table("external_intelligence", """
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            intel_type varchar(40) NOT NULL,
            source varchar(120),
            signal varchar(10),
            title varchar(400),
            summary text,
            url text,
            observed_at timestamptz,
            pulled_at timestamptz,
            payload jsonb,
            acknowledged_by varchar(120),
            acknowledged_at timestamptz,
            is_dismissed boolean NOT NULL DEFAULT false
        """)
        op.execute("CREATE INDEX ix_extintel_tenant_entity ON external_intelligence (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_extintel_tenant_type ON external_intelligence (tenant_id, intel_type);")
        op.execute("CREATE INDEX ix_extintel_entity_fk ON external_intelligence (entity_id);")

        # --- 7. monitoring_reporting -----------------------------------------
        _table("monitoring_reporting", """
            entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            record_type varchar(40) NOT NULL,
            covenant_name varchar(200),
            due_date date,
            submitted_date date,
            on_time boolean,
            delay_days integer,
            security_created_within_days integer,
            extension_count integer,
            behavioural_score numeric(6,2),
            period varchar(20),
            status varchar(60),
            feeds_irg boolean NOT NULL DEFAULT true,
            target_value numeric(18,4),
            actual_value numeric(18,4),
            breached boolean,
            waiver_status varchar(60),
            waiver_valid_until date,
            waiver_decision_ref varchar(200),
            waiver_note text,
            details jsonb
        """)
        op.execute("CREATE INDEX ix_monitoring_tenant_entity ON monitoring_reporting (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_monitoring_tenant_type ON monitoring_reporting (tenant_id, record_type);")
        op.execute("CREATE INDEX ix_monitoring_entity_fk ON monitoring_reporting (entity_id);")
        # Recurring-covenant idempotency: ONE observation per (covenant, due date), ever —
        # the covenant sweep's generation is replay-safe by construction.
        op.execute(
            """
            CREATE UNIQUE INDEX monitoring_covenant_period_unique
            ON monitoring_reporting (tenant_id, (details->>'covenant_id'), due_date)
            WHERE record_type = 'Covenant' AND details->>'covenant_id' IS NOT NULL;
            """
        )

        # --- calendar events -------------------------------------------------
        # First-class meeting/follow-up records: Scheduled → Completed / Cancelled;
        # reschedules update the Scheduled row; terminal rows are frozen by trigger.
        _table("calendar_events", """
            subject_type    varchar(30),
            subject_id      varchar(64),
            entity_id       uuid REFERENCES entities(id) ON DELETE SET NULL,
            title           varchar(300) NOT NULL,
            description     text,
            location        varchar(300),
            starts_at       timestamptz NOT NULL,
            ends_at         timestamptz,
            organizer       varchar(200) NOT NULL,
            attendees       jsonb,
            status          varchar(16) NOT NULL DEFAULT 'Scheduled',
            source          varchar(20) NOT NULL DEFAULT 'manual',
            workflow_id     varchar(200),
            external_ref    varchar(200),
            completed_by    varchar(200),
            completed_at    timestamptz,
            completion_note text,
            cancelled_by    varchar(200),
            cancelled_at    timestamptz,
            cancel_note     text,
            CONSTRAINT calendar_events_status
                CHECK (status IN ('Scheduled', 'Completed', 'Cancelled')),
            CONSTRAINT calendar_events_window
                CHECK (ends_at IS NULL OR ends_at >= starts_at)
        """)
        op.execute("CREATE INDEX ix_calendar_events_organizer "
                   "ON calendar_events (tenant_id, organizer, starts_at);")
        op.execute("CREATE INDEX ix_calendar_events_subject "
                   "ON calendar_events (tenant_id, subject_type, subject_id);")
        op.execute("CREATE INDEX ix_calendar_events_window "
                   "ON calendar_events (tenant_id, starts_at) WHERE status = 'Scheduled';")
        op.execute(
            """
        CREATE OR REPLACE FUNCTION calendar_event_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'calendar_events rows cannot be deleted (cancel instead)';
            END IF;
            IF OLD.status IN ('Completed', 'Cancelled') THEN
                RAISE EXCEPTION 'calendar_events row % is % and is frozen',
                    OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
        )
        op.execute(
            """
            CREATE TRIGGER trg_calendar_event_guard
            BEFORE UPDATE OR DELETE ON calendar_events
            FOR EACH ROW EXECUTE FUNCTION calendar_event_guard();
            """
        )

        # --- notifications (in-app inbox) + external-channel delivery outbox --
        # One notification row per recipient (idempotent by dedupe_key), plus one
        # delivery-outbox row per external channel — lease + fencing-token claims,
        # exponential backoff, dead-letter (the same machinery as the decision outbox).
        _table("notifications", """
            recipient      varchar(200) NOT NULL,
            recipient_role varchar(60),
            event          varchar(120) NOT NULL,
            severity       varchar(12)  NOT NULL DEFAULT 'info',
            title          varchar(300) NOT NULL,
            body           text,
            subject_type   varchar(40),
            subject_id     varchar(64),
            workflow_id    varchar(200),
            dedupe_key     varchar(240),
            read_at        timestamptz,
            meta           jsonb,
            CONSTRAINT notifications_severity
                CHECK (severity IN ('info', 'warning', 'critical'))
        """)
        op.execute("CREATE UNIQUE INDEX notifications_tenant_dedupe "
                   "ON notifications (tenant_id, dedupe_key) WHERE dedupe_key IS NOT NULL;")
        op.execute("CREATE INDEX ix_notifications_inbox "
                   "ON notifications (tenant_id, recipient, created_at DESC);")
        op.execute("CREATE INDEX ix_notifications_unread "
                   "ON notifications (tenant_id, recipient) WHERE read_at IS NULL;")

        _table("notification_deliveries", """
            notification_id uuid NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
            channel         varchar(12)  NOT NULL,
            target          varchar(300) NOT NULL,
            status          varchar(12)  NOT NULL DEFAULT 'pending',
            attempts        integer      NOT NULL DEFAULT 0,
            next_attempt_at timestamptz  NOT NULL DEFAULT now(),
            leased_until    timestamptz,
            claim_token     uuid,
            last_error      text,
            delivered_at    timestamptz,
            CONSTRAINT notification_deliveries_channel
                CHECK (channel IN ('email', 'sms', 'webhook')),
            CONSTRAINT notification_deliveries_status
                CHECK (status IN ('pending', 'delivered', 'dead')),
            CONSTRAINT notification_deliveries_unique
                UNIQUE (tenant_id, notification_id, channel)
        """)
        op.execute("CREATE INDEX ix_notification_deliveries_due "
                   "ON notification_deliveries (tenant_id, next_attempt_at) "
                   "WHERE status = 'pending';")

        # --- covenant definitions --------------------------------------------
        # The covenant DEFINITION/schedule; the OBSERVATIONS live in monitoring_reporting
        # (record_type='Covenant', one row per period via the partial unique index above).
        _table("covenants", """
            entity_id     uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            deal_id       uuid REFERENCES deals(id) ON DELETE SET NULL,
            lending_id    uuid,
            name          varchar(200) NOT NULL,
            covenant_type varchar(30)  NOT NULL DEFAULT 'Financial',
            description   text,
            metric        varchar(60),
            operator      varchar(2),
            threshold     numeric(18,4),
            frequency     varchar(12) NOT NULL DEFAULT 'Quarterly',
            first_due_on  date NOT NULL,
            grace_days    integer NOT NULL DEFAULT 0,
            breach_severity varchar(12) NOT NULL DEFAULT 'Amber',
            is_active     boolean NOT NULL DEFAULT true,
            CONSTRAINT covenants_type
                CHECK (covenant_type IN ('Financial', 'Reporting', 'Security', 'Other')),
            CONSTRAINT covenants_frequency
                CHECK (frequency IN ('OneTime', 'Monthly', 'Quarterly', 'SemiAnnual',
                                     'Annual')),
            CONSTRAINT covenants_operator
                CHECK (operator IS NULL OR operator IN ('>=', '<=', '>', '<', '=')),
            CONSTRAINT covenants_severity
                CHECK (breach_severity IN ('Amber', 'Red')),
            CONSTRAINT covenants_financial_shape CHECK (
                covenant_type <> 'Financial'
                OR (metric IS NOT NULL AND operator IS NOT NULL AND threshold IS NOT NULL))
        """)
        op.execute("CREATE INDEX ix_covenants_entity ON covenants (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_covenants_deal ON covenants (tenant_id, deal_id);")
        op.execute("CREATE INDEX ix_covenants_active ON covenants (tenant_id) "
                   "WHERE is_active AND deleted_at IS NULL;")

        # --- EWS cases --------------------------------------------------------
        # Early-warning case file: Open → UnderInvestigation → Escalated → Closed; deduped
        # per trigger source; Closed rows frozen by trigger, never deletable.
        _table("ews_cases", """
            entity_id  uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            deal_id    uuid REFERENCES deals(id) ON DELETE SET NULL,
            source     varchar(30)  NOT NULL,
            source_ref varchar(120) NOT NULL,
            severity   varchar(12)  NOT NULL DEFAULT 'Amber',
            title      varchar(300) NOT NULL,
            summary    text,
            status     varchar(24)  NOT NULL DEFAULT 'Open',
            opened_by  varchar(200),
            assigned_to varchar(200),
            assigned_at timestamptz,
            investigation_note text,
            escalated_by varchar(200),
            escalated_at timestamptz,
            escalation_note text,
            disposition varchar(30),
            closure_note text,
            closed_by  varchar(200),
            closed_at  timestamptz,
            workflow_id varchar(200),
            CONSTRAINT ews_cases_severity CHECK (severity IN ('Amber', 'Red')),
            CONSTRAINT ews_cases_status
                CHECK (status IN ('Open', 'UnderInvestigation', 'Escalated', 'Closed')),
            CONSTRAINT ews_cases_source_dedupe UNIQUE (tenant_id, source, source_ref)
        """)
        op.execute("CREATE INDEX ix_ews_cases_entity ON ews_cases (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_ews_cases_open ON ews_cases (tenant_id, status) "
                   "WHERE status <> 'Closed';")
        op.execute(
            """
        CREATE OR REPLACE FUNCTION ews_case_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'ews_cases rows cannot be deleted (close instead)';
            END IF;
            IF OLD.status = 'Closed' THEN
                RAISE EXCEPTION 'ews_cases row % is Closed and is frozen', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
        )
        op.execute(
            """
            CREATE TRIGGER trg_ews_case_guard
            BEFORE UPDATE OR DELETE ON ews_cases
            FOR EACH ROW EXECUTE FUNCTION ews_case_guard();
            """
        )

        _apply_row_level_security()


    # Tables that carry tenant_id and should be RLS-scoped.
    rls_tables = [
        "entities", "people", "counterparties", "deals", "leads", "lending_tracker",
        "syndication_tracker", "syndication_lenders", "asset_monetisation", "financials",
        "contracts_assets", "interactions", "external_intelligence", "monitoring_reporting",
        "calendar_events", "notifications", "notification_deliveries", "covenants",
        "ews_cases",
    ]


    def _apply_row_level_security() -> None:
        """Defence-in-depth: even if an application query forgets its tenant filter, the
        database refuses to return another tenant's rows. Policies key off the
        ``app.current_tenant`` GUC set per request. RLS is only *enforced* when
        REGISTER_ENFORCE_RLS turns it on for the connecting role; the policies are always
        present so turning it on is a one-line switch."""
        for tbl in rls_tables:
            op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
            op.execute(
                f"""
                CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                USING (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                )
                WITH CHECK (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                );
                """
            )
    upgrade()


# --------------------------------------------------------------------------- #
# documents catalog + checklist template (ATLAS "Data Register")
# --------------------------------------------------------------------------- #
def _base_0002_documents() -> None:
    # Same trailing columns every business table carries (see 0001).
    common = """
        tenant_id      uuid        NOT NULL,
        version        integer     NOT NULL DEFAULT 1,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        created_by     varchar(120),
        updated_by     varchar(120),
        deleted_at     timestamptz
    """


    def _table(name: str, columns: str) -> None:
        op.execute(
            f"""
            CREATE TABLE {name} (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                {columns},
                {common}
            );
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{name}_updated_at BEFORE UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )
        op.execute(f"CREATE INDEX ix_{name}_tenant ON {name} (tenant_id);")
        op.execute(f"CREATE INDEX ix_{name}_tenant_active ON {name} (tenant_id) WHERE deleted_at IS NULL;")


    rls_tables = ["document_checklist", "documents"]


    def upgrade() -> None:
        # --- checklist template ----------------------------------------------
        _table("document_checklist", """
            applies_to varchar(30) NOT NULL DEFAULT '*',
            section varchar(80) NOT NULL,
            section_order integer NOT NULL DEFAULT 0,
            slot_key varchar(60) NOT NULL,
            label varchar(200) NOT NULL,
            is_required boolean NOT NULL DEFAULT false,
            sort_order integer NOT NULL DEFAULT 0,
            is_active boolean NOT NULL DEFAULT true,
            hint text,
            CONSTRAINT document_checklist_unique UNIQUE (tenant_id, applies_to, slot_key)
        """)
        op.execute("CREATE INDEX ix_doc_checklist_applies ON document_checklist (tenant_id, applies_to);")

        # --- documents catalog -----------------------------------------------
        _table("documents", """
            subject_type varchar(30) NOT NULL,
            subject_id uuid NOT NULL,
            entity_id uuid REFERENCES entities(id) ON DELETE CASCADE,
            deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
            section varchar(80),
            slot_key varchar(60),
            doc_type varchar(120),
            title varchar(300) NOT NULL,
            is_required boolean NOT NULL DEFAULT false,
            status varchar(40) NOT NULL DEFAULT 'On File',
            storage_backend varchar(20),
            storage_uri text,
            content_type varchar(120),
            size_bytes bigint,
            checksum varchar(64),
            original_filename varchar(300),
            inline_content bytea,
            uploaded_by varchar(120),
            uploaded_at timestamptz,
            notes text,
            meta jsonb,
            expires_on date,
            verified_by varchar(120),
            verified_at timestamptz,
            status_note text,
            superseded_by uuid REFERENCES documents(id) ON DELETE SET NULL
        """)
        op.execute("CREATE INDEX ix_documents_subject ON documents (tenant_id, subject_type, subject_id);")
        op.execute("CREATE INDEX ix_documents_entity ON documents (tenant_id, entity_id);")
        op.execute("CREATE INDEX ix_documents_slot "
                   "ON documents (tenant_id, subject_type, subject_id, slot_key);")
        op.execute("CREATE INDEX ix_documents_entity_fk ON documents (entity_id);")
        # The expiry sweep scans live documents whose validity window can lapse.
        op.execute("CREATE INDEX ix_documents_expiry ON documents (tenant_id, expires_on) "
                   "WHERE expires_on IS NOT NULL AND deleted_at IS NULL;")

        _apply_row_level_security()


    def _apply_row_level_security() -> None:
        """Same tenant-isolation policy as 0001, keyed off the ``app.current_tenant`` GUC."""
        for tbl in rls_tables:
            op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
            op.execute(
                f"""
                CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                USING (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                )
                WITH CHECK (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                );
                """
            )
    upgrade()


# --------------------------------------------------------------------------- #
# user management & RBAC — line assignments, change requests (identity lives in Access)
# --------------------------------------------------------------------------- #
def _base_0003_users_rbac() -> None:
    common = """
        tenant_id      uuid        NOT NULL,
        version        integer     NOT NULL DEFAULT 1,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        created_by     varchar(120),
        updated_by     varchar(120),
        deleted_at     timestamptz
    """

    def _table(name: str, columns: str) -> None:
        op.execute(
            f"""
            CREATE TABLE {name} (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                {columns},
                {common}
            );
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{name}_updated_at BEFORE UPDATE ON {name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )
        op.execute(f"CREATE INDEX ix_{name}_tenant ON {name} (tenant_id);")
        op.execute(f"CREATE INDEX ix_{name}_tenant_active ON {name} (tenant_id) WHERE deleted_at IS NULL;")

    rls_tables = ["line_assignments", "change_requests"]

    def upgrade() -> None:
        # NOTE (identity): the original chain created users/user_roles here and moved them to
        # the Access service two revisions later. The baseline never creates them —
        # line_assignments.user_id / change_requests reference Access-service users, and
        # identity arrives per-request via gateway-forwarded headers. That is also why
        # user_id carries no FK: the referenced rows live in another database.

        # --- line_assignments (assignment-driven permission) --------------------
        _table("line_assignments", """
            user_id uuid NOT NULL,
            subject_type varchar(30) NOT NULL,
            subject_id uuid NOT NULL,
            assignment_role varchar(30) NOT NULL,
            assigned_by varchar(200),
            ended_at timestamptz,
            ended_by varchar(200),
            note text
        """)
        op.execute("CREATE INDEX ix_assign_subject "
                   "ON line_assignments (tenant_id, subject_type, subject_id);")
        op.execute("CREATE INDEX ix_assign_user_active ON line_assignments (tenant_id, user_id, ended_at);")
        # One ACTIVE assignment per (user, line, capacity) — history rows keep ended_at.
        op.execute(
            """
            CREATE UNIQUE INDEX uq_assign_active
            ON line_assignments (tenant_id, subject_type, subject_id, user_id, assignment_role)
            WHERE ended_at IS NULL AND deleted_at IS NULL;
            """
        )

        # --- change_requests (request → approve/reject flow) --------------------
        _table("change_requests", """
            subject_type varchar(30) NOT NULL,
            subject_id uuid NOT NULL,
            field varchar(60) NOT NULL,
            from_value varchar(120),
            to_value varchar(120) NOT NULL,
            note text,
            requested_by varchar(200) NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'Pending',
            decided_by varchar(200),
            decided_at timestamptz,
            decision_note text
        """)
        op.execute("CREATE INDEX ix_chreq_status ON change_requests (tenant_id, status);")
        op.execute("CREATE INDEX ix_chreq_subject ON change_requests (tenant_id, subject_type, subject_id);")

        _apply_row_level_security()

    def _apply_row_level_security() -> None:
        for tbl in rls_tables:
            op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
            op.execute(
                f"""
                CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                USING (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                )
                WITH CHECK (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                );
                """
            )
    upgrade()


# --------------------------------------------------------------------------- #
# Row-level security becomes a real, fail-CLOSED boundary.
# --------------------------------------------------------------------------- #
def _base_0005_rls_fail_closed() -> None:
    # Every table that carries tenant_id and holds business/enforcement data.
    tenant_tables = [
        "entities", "people", "counterparties", "deals", "leads", "lending_tracker",
        "syndication_tracker", "syndication_lenders", "asset_monetisation", "financials",
        "contracts_assets", "interactions", "external_intelligence", "monitoring_reporting",
        "documents", "document_checklist", "line_assignments", "change_requests",
        "tenant_settings", "idempotency_keys",
        "calendar_events", "notifications", "notification_deliveries", "covenants",
        "ews_cases",
    ]


    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        force = _truthy(os.getenv("REGISTER_ENFORCE_RLS"))

        for tbl in tenant_tables:
            op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
            # Drop any prior policy (0001 created *_tenant_isolation on a subset).
            op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl};")
            # Fail-CLOSED: no NULL escape. Unset GUC → current_setting(...) is NULL (or '' when a
            # pooled session's transaction-local set_config reverted — NULLIF folds that to NULL
            # instead of erroring the cast) →
            # tenant_id = NULL is NULL (never true) → zero rows / rejected writes.
            op.execute(f"""
                CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
            """)
            if force:
                op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")

        # audit_log is append-only and its tenant_id is nullable (system-level events). Isolate
        # tenant-scoped rows but still allow the NULL-tenant system rows through.
        op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;")
        op.execute("DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;")
        op.execute("""
            CREATE POLICY audit_log_tenant_isolation ON audit_log
            USING (
                tenant_id IS NULL
                OR tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
            WITH CHECK (
                tenant_id IS NULL
                OR tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            );
        """)
        if force:
            op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;")

        # A non-owner application role: RLS is always enforced for it (no owner bypass).
        # Created NOLOGIN — operators grant it a password + LOGIN and point
        # REGISTER_DB_USER at it. Best-effort + idempotent: if the migration role lacks
        # CREATEROLE (managed Postgres, CI), the whole block is skipped with a NOTICE rather
        # than failing the migration — operators then create register_app by hand per the
        # deploy docs. New tables/sequences inherit the grants via ALTER DEFAULT PRIVILEGES.
        op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                CREATE ROLE register_app NOLOGIN;
            END IF;
            EXECUTE 'GRANT USAGE ON SCHEMA public TO register_app';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                    'IN SCHEMA public TO register_app';
            EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO register_app';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO register_app';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT USAGE, SELECT ON SEQUENCES TO register_app';
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'register_app role/grants skipped (insufficient privilege).';
        END
        $$;
    """)
    upgrade()


# --------------------------------------------------------------------------- #
# A dedicated, single-winner workflow-decision resource.
# --------------------------------------------------------------------------- #
def _base_0006_workflow_decisions() -> None:
    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        op.execute(
            """
            CREATE TABLE workflow_decisions (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workflow_id     varchar(200) NOT NULL,
                lead_id         varchar(64),
                decision        varchar(20)  NOT NULL,
                decided_by      varchar(200) NOT NULL,
                decided_by_id   varchar(64),
                roles           jsonb NOT NULL DEFAULT '[]'::jsonb,
                operations      jsonb NOT NULL DEFAULT '{}'::jsonb,
                views           jsonb NOT NULL DEFAULT '{}'::jsonb,
                note            text,
                tenant_id      uuid        NOT NULL,
                version        integer     NOT NULL DEFAULT 1,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                created_by     varchar(120),
                updated_by     varchar(120),
                deleted_at     timestamptz,
                -- The single-winner guarantee: ONE decision per workflow, per tenant. A second,
                -- DIFFERENT decision hits this constraint; the app turns that into a 409.
                CONSTRAINT workflow_decisions_tenant_wf UNIQUE (tenant_id, workflow_id)
            );
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_workflow_decisions_updated_at BEFORE UPDATE ON workflow_decisions
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )
        op.execute("CREATE INDEX ix_workflow_decisions_tenant ON workflow_decisions (tenant_id);")

        # Fail-CLOSED RLS, identical to every other tenant table (0005): an unset GUC denies.
        op.execute("ALTER TABLE workflow_decisions ENABLE ROW LEVEL SECURITY;")
        op.execute(
            """
            CREATE POLICY workflow_decisions_tenant_isolation ON workflow_decisions
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
            """
        )
        if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
            op.execute("ALTER TABLE workflow_decisions FORCE ROW LEVEL SECURITY;")

        # The non-owner app role gets DML (0005 set ALTER DEFAULT PRIVILEGES, but grant explicitly
        # so an existing register_app picks up this new table immediately). Best-effort.
        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_decisions '
                        'TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'workflow_decisions grant to register_app skipped.';
        END
        $$;
        """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Make workflow_decisions immutable at the database level.
# --------------------------------------------------------------------------- #
def _base_0007_decision_immutability() -> None:
    def upgrade() -> None:
        # 1. Least privilege: the app role may read and append, never mutate or remove.
        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'REVOKE UPDATE, DELETE ON workflow_decisions FROM register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'REVOKE on workflow_decisions skipped (insufficient privilege).';
        END
        $$;
        """
        )
        # 2. Hard immutability: block UPDATE/DELETE at the row level, for everyone.
        op.execute(
            """
        CREATE OR REPLACE FUNCTION workflow_decisions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'workflow_decisions is append-only; % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
        )
        op.execute(
            """
            CREATE TRIGGER trg_workflow_decisions_immutable
            BEFORE UPDATE OR DELETE ON workflow_decisions
            FOR EACH ROW EXECUTE FUNCTION workflow_decisions_immutable();
            """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# A transactional delivery outbox for workflow decisions.
# --------------------------------------------------------------------------- #
def _base_0008_decision_outbox() -> None:
    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        op.execute(
            """
            CREATE TABLE workflow_decision_outbox (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workflow_id     varchar(200) NOT NULL,
                decision        varchar(20)  NOT NULL,
                -- pending → not yet confirmed applied; applied → the run converted with this
                -- outcome; dead → the run closed without applying it or retries were exhausted.
                status          varchar(12)  NOT NULL DEFAULT 'pending',
                attempts        integer      NOT NULL DEFAULT 0,
                next_attempt_at timestamptz  NOT NULL DEFAULT now(),
                leased_until    timestamptz,
                last_error      text,
                applied_at      timestamptz,
                tenant_id      uuid        NOT NULL,
                version        integer     NOT NULL DEFAULT 1,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                created_by     varchar(120),
                updated_by     varchar(120),
                deleted_at     timestamptz,
                CONSTRAINT workflow_decision_outbox_tenant_wf UNIQUE (tenant_id, workflow_id),
                CONSTRAINT workflow_decision_outbox_status
                    CHECK (status IN ('pending', 'applied', 'dead'))
            );
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_workflow_decision_outbox_updated_at
            BEFORE UPDATE ON workflow_decision_outbox
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )
        op.execute("CREATE INDEX ix_workflow_decision_outbox_tenant "
                   "ON workflow_decision_outbox (tenant_id);")
        # The reconciler's claim query filters on (status, next_attempt_at) — index it.
        op.execute("CREATE INDEX ix_workflow_decision_outbox_due "
                   "ON workflow_decision_outbox (tenant_id, status, next_attempt_at);")

        op.execute("ALTER TABLE workflow_decision_outbox ENABLE ROW LEVEL SECURITY;")
        op.execute(
            """
            CREATE POLICY workflow_decision_outbox_tenant_isolation ON workflow_decision_outbox
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
            """
        )
        if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
            op.execute("ALTER TABLE workflow_decision_outbox FORCE ROW LEVEL SECURITY;")

        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_decision_outbox '
                        'TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'workflow_decision_outbox grant to register_app skipped.';
        END
        $$;
        """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Backfill the decision outbox and add a claim/lease fencing token.
# --------------------------------------------------------------------------- #
def _base_0009_outbox_backfill_and_fencing() -> None:
    def upgrade() -> None:
        op.execute("ALTER TABLE workflow_decision_outbox ADD COLUMN claim_token uuid;")
        # Backfill: one pending delivery per existing decision. ON CONFLICT DO NOTHING makes it
        # idempotent (re-runnable) and harmless where an outbox row already exists.
        op.execute(
            """
            INSERT INTO workflow_decision_outbox
                (tenant_id, workflow_id, decision, status, attempts, next_attempt_at)
            SELECT d.tenant_id, d.workflow_id, d.decision, 'pending', 0, now()
            FROM workflow_decisions d
            ON CONFLICT ON CONSTRAINT workflow_decision_outbox_tenant_wf DO NOTHING;
            """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Persist import reconciliation as an operational object + give every product line stage history.
# --------------------------------------------------------------------------- #
def _base_0010_import_reconciliation() -> None:
    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        # Deal and Asset Monetisation gain an append-only history (parity with the other trackers).
        op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS stage_history jsonb;")
        op.execute("ALTER TABLE asset_monetisation ADD COLUMN IF NOT EXISTS status_history jsonb;")
        # A lightweight per-record flag so operational reads can exclude unreconciled imports.
        for table in ("lending_tracker", "deals", "syndication_tracker", "asset_monetisation"):
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                       "reconciliation_status varchar(20);")

        op.execute(
            """
            CREATE TABLE import_reconciliation_items (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                import_batch_id  varchar(80)  NOT NULL,
                checksum         varchar(80),
                subject_type     varchar(40)  NOT NULL,
                subject_id       uuid,
                sheet            varchar(80),
                company          varchar(200),
                stage_field      varchar(40),
                stage_value      varchar(80),
                missing_fields   jsonb        NOT NULL DEFAULT '[]'::jsonb,
                original_values  jsonb,
                status           varchar(20)  NOT NULL DEFAULT 'Required',
                owner            varchar(120),
                resolution_note  text,
                resolved_by      varchar(120),
                resolved_at      timestamptz,
                tenant_id      uuid        NOT NULL,
                version        integer     NOT NULL DEFAULT 1,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                created_by     varchar(120),
                updated_by     varchar(120),
                deleted_at     timestamptz,
                CONSTRAINT import_reconciliation_status
                    CHECK (status IN ('Required', 'Resolved', 'Waived'))
            );
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_import_reconciliation_items_updated_at
            BEFORE UPDATE ON import_reconciliation_items
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )
        op.execute("CREATE INDEX ix_import_reconciliation_tenant "
                   "ON import_reconciliation_items (tenant_id, status);")
        op.execute("CREATE INDEX ix_import_reconciliation_batch "
                   "ON import_reconciliation_items (tenant_id, import_batch_id);")

        op.execute("ALTER TABLE import_reconciliation_items ENABLE ROW LEVEL SECURITY;")
        op.execute(
            """
            CREATE POLICY import_reconciliation_items_tenant_isolation ON import_reconciliation_items
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
            """
        )
        if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
            op.execute("ALTER TABLE import_reconciliation_items FORCE ROW LEVEL SECURITY;")

        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON import_reconciliation_items '
                        'TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'import_reconciliation_items grant to register_app skipped.';
        END
        $$;
        """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Immutable governance-evidence store for evidence-based lifecycle gates.
# --------------------------------------------------------------------------- #
def _base_0011_governance_evidence() -> None:
    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        op.execute(
            """
            CREATE TABLE governance_evidence (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                subject_type   varchar(40)  NOT NULL,
                subject_id     uuid         NOT NULL,
                evidence_kind  varchar(60)  NOT NULL,
                reference      varchar(500) NOT NULL,
                sha256         varchar(64),
                note           text,
                recorded_by    varchar(120),
                tenant_id      uuid        NOT NULL,
                version        integer     NOT NULL DEFAULT 1,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                created_by     varchar(120),
                updated_by     varchar(120),
                deleted_at     timestamptz
            );
            """
        )
        op.execute("CREATE INDEX ix_governance_evidence_subject "
                   "ON governance_evidence (tenant_id, subject_type, subject_id, evidence_kind);")

        # WRITE-ONCE immutability: an evidence row may be inserted but never updated or deleted, so it
        # cannot be silently altered after it has justified (or been relied on to justify) a transition.
        op.execute(
            """
        CREATE OR REPLACE FUNCTION governance_evidence_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'governance_evidence is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'raise_exception';
        END;
        $$ LANGUAGE plpgsql;
        """
        )
        op.execute(
            """
            CREATE TRIGGER trg_governance_evidence_immutable
            BEFORE UPDATE OR DELETE ON governance_evidence
            FOR EACH ROW EXECUTE FUNCTION governance_evidence_immutable();
            """
        )

        op.execute("ALTER TABLE governance_evidence ENABLE ROW LEVEL SECURITY;")
        op.execute(
            """
            CREATE POLICY governance_evidence_tenant_isolation ON governance_evidence
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
            """
        )
        if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
            op.execute("ALTER TABLE governance_evidence FORCE ROW LEVEL SECURITY;")

        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                -- INSERT + SELECT only: the app never needs UPDATE/DELETE, and the trigger blocks
                -- them anyway, so the grant matches the intent (append + read).
                EXECUTE 'GRANT SELECT, INSERT ON governance_evidence TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'governance_evidence grant to register_app skipped.';
        END
        $$;
        """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Make governance evidence AUTHORITATIVE: provenance binding + an append-only validity status.
# --------------------------------------------------------------------------- #
def _base_0012_evidence_provenance_and_status() -> None:
    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        op.execute("ALTER TABLE governance_evidence "
                   "ADD COLUMN IF NOT EXISTS workflow_id varchar(200);")
        op.execute("ALTER TABLE governance_evidence "
                   "ADD COLUMN IF NOT EXISTS run_id varchar(200);")
        op.execute("ALTER TABLE governance_evidence "
                   "ADD COLUMN IF NOT EXISTS decision_ref varchar(200);")
        op.execute("ALTER TABLE governance_evidence "
                   "ADD COLUMN IF NOT EXISTS supersedes_id uuid;")
        op.execute("ALTER TABLE governance_evidence "
                   "ADD COLUMN IF NOT EXISTS effective_date timestamptz;")

        op.execute(
            """
            CREATE TABLE governance_evidence_status (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                evidence_id  uuid         NOT NULL REFERENCES governance_evidence(id),
                status       varchar(20)  NOT NULL,
                reason       text         NOT NULL,
                actor        varchar(120),
                tenant_id    uuid        NOT NULL,
                version      integer     NOT NULL DEFAULT 1,
                created_at   timestamptz NOT NULL DEFAULT now(),
                updated_at   timestamptz NOT NULL DEFAULT now(),
                created_by   varchar(120),
                updated_by   varchar(120),
                deleted_at   timestamptz,
                CONSTRAINT governance_evidence_status_value
                    CHECK (status IN ('Revoked', 'Invalidated', 'Superseded'))
            );
            """
        )
        op.execute("CREATE INDEX ix_governance_evidence_status_evidence "
                   "ON governance_evidence_status (tenant_id, evidence_id);")

        # The status ledger is itself append-only — a revocation cannot later be silently reversed.
        op.execute(
            """
            CREATE TRIGGER trg_governance_evidence_status_immutable
            BEFORE UPDATE OR DELETE ON governance_evidence_status
            FOR EACH ROW EXECUTE FUNCTION governance_evidence_immutable();
            """
        )

        op.execute("ALTER TABLE governance_evidence_status ENABLE ROW LEVEL SECURITY;")
        op.execute(
            """
            CREATE POLICY governance_evidence_status_tenant_isolation ON governance_evidence_status
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
            """
        )
        if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
            op.execute("ALTER TABLE governance_evidence_status FORCE ROW LEVEL SECURITY;")

        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT ON governance_evidence_status TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'governance_evidence_status grant to register_app skipped.';
        END
        $$;
        """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Bind workflow decisions to a subject + outcome, and make governance evidence reference them.
# --------------------------------------------------------------------------- #
def _base_0013_decision_subject_binding() -> None:
    def upgrade() -> None:
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS subject_type varchar(40);")
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS subject_id varchar(64);")
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS run_id varchar(200);")
        # One authoritative decision backs at most one evidence row of each kind.
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_governance_evidence_decision "
            "ON governance_evidence (tenant_id, decision_ref, evidence_kind) "
            "WHERE decision_ref IS NOT NULL;")
    upgrade()


# --------------------------------------------------------------------------- #
# Carry the committee/sanction references on the authoritative decision record.
# --------------------------------------------------------------------------- #
def _base_0014_committee_decision_references() -> None:
    def upgrade() -> None:
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS committee_reference varchar(500);")
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS sanction_letter_reference varchar(500);")
        # Conditional approval: the committee's conditions text and the sanction's validity
        # window (days) — recorded per decision (per facility for committee decisions).
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS conditions text;")
        op.execute("ALTER TABLE workflow_decisions "
                   "ADD COLUMN IF NOT EXISTS valid_days integer;")
    upgrade()


# --------------------------------------------------------------------------- #
# Authoritative, immutable Advaya-handoff record — so the disbursement acknowledgement cannot be
# --------------------------------------------------------------------------- #
def _base_0015_advaya_handoff() -> None:
    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in {"1", "true", "yes", "on"}


    def upgrade() -> None:
        op.execute(
            """
            CREATE TABLE advaya_handoffs (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                handoff_key       varchar(200) NOT NULL,
                lending_id        varchar(64)  NOT NULL,
                payload_sha256    varchar(64)  NOT NULL,
                status            varchar(20)  NOT NULL,
                acknowledgement_id varchar(200),
                workflow_id       varchar(200),
                run_id            varchar(200),
                note              text,
                tenant_id      uuid        NOT NULL,
                version        integer     NOT NULL DEFAULT 1,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                created_by     varchar(120),
                updated_by     varchar(120),
                deleted_at     timestamptz,
                CONSTRAINT advaya_handoffs_status CHECK (status IN ('Accepted', 'Rejected')),
                CONSTRAINT advaya_handoffs_tenant_key UNIQUE (tenant_id, handoff_key)
            );
            """
        )
        op.execute("CREATE INDEX ix_advaya_handoffs_lending "
                   "ON advaya_handoffs (tenant_id, lending_id);")
        # Immutable: an accepted handoff cannot be silently altered/removed to (un)justify a disbursement.
        op.execute(
            """
            CREATE TRIGGER trg_advaya_handoffs_immutable
            BEFORE UPDATE OR DELETE ON advaya_handoffs
            FOR EACH ROW EXECUTE FUNCTION governance_evidence_immutable();
            """
        )
        op.execute("ALTER TABLE advaya_handoffs ENABLE ROW LEVEL SECURITY;")
        op.execute(
            """
            CREATE POLICY advaya_handoffs_tenant_isolation ON advaya_handoffs
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
            """
        )
        if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
            op.execute("ALTER TABLE advaya_handoffs FORCE ROW LEVEL SECURITY;")
        op.execute(
            """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT ON advaya_handoffs TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'advaya_handoffs grant to register_app skipped.';
        END
        $$;
        """
        )
    upgrade()


# --------------------------------------------------------------------------- #
# Durable Advaya handover package + authoritative CP/CS checklist, and the proposed-disbursement
# --------------------------------------------------------------------------- #
def _base_0016_handover_package_and_cpcs() -> None:
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
    upgrade()


def upgrade() -> None:
    _base_0001_initial_schema()
    _base_0002_documents()
    _base_0003_users_rbac()
    _base_0005_rls_fail_closed()
    _base_0006_workflow_decisions()
    _base_0007_decision_immutability()
    _base_0008_decision_outbox()
    _base_0009_outbox_backfill_and_fencing()
    _base_0010_import_reconciliation()
    _base_0011_governance_evidence()
    _base_0012_evidence_provenance_and_status()
    _base_0013_decision_subject_binding()
    _base_0014_committee_decision_references()
    _base_0015_advaya_handoff()
    _base_0016_handover_package_and_cpcs()


def downgrade() -> None:
    """Full teardown — the baseline's inverse is an empty database."""
    for tbl in [
        "disbursement_tranches", "advaya_handover_packages", "cp_cs_checklists",
        "advaya_handoffs", "governance_evidence_status", "governance_evidence",
        "import_reconciliation_items", "workflow_decision_outbox", "workflow_decisions",
        "ews_cases", "covenants", "notification_deliveries", "notifications",
        "calendar_events", "change_requests", "line_assignments", "documents",
        "document_checklist", "monitoring_reporting", "external_intelligence",
        "interactions", "contracts_assets", "financials", "asset_monetisation",
        "syndication_lenders", "syndication_tracker", "lending_tracker", "leads",
        "deals", "counterparties", "people", "entities", "audit_log",
        "idempotency_keys", "ref_values", "tenant_settings", "tenants",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
    for fn in [
        "disbursement_tranche_guard", "advaya_handover_package_guard",
        "cp_cs_checklist_guard", "governance_evidence_immutable",
        "workflow_decisions_immutable", "ews_case_guard", "calendar_event_guard",
        "set_updated_at",
    ]:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}() CASCADE;")
