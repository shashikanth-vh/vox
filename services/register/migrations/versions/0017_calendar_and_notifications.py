"""Calendar events + the durable notification store with its delivery outbox.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-01

Three tables, one milestone (Release-1 increment 7):

* ``calendar_events`` — first-class meeting/follow-up records (previously only a
  ``meta.calendar`` hand-off note on interactions). Subject-bound, with a real lifecycle:
  Scheduled → Completed / Cancelled; reschedules update the Scheduled row (audited,
  version-bumped). Terminal rows are frozen by a trigger — a completed or cancelled
  meeting is a fact.

* ``notifications`` — one row per notification a human should see. This IS the in-app
  inbox (recipient-scoped reads, mark-read) and the anchor for every external channel.
  Idempotent by ``dedupe_key`` so a Temporal activity retry can never double-notify.

* ``notification_deliveries`` — the transactional outbox for the EXTERNAL channels
  (email / sms / webhook): one row per channel per notification, created in the same
  transaction as the notification, then driven by the notifier sweep
  (``python -m app.notifier`` in the workflows service) with lease + fencing-token claims,
  exponential backoff and a dead-letter terminal state — the same delivery machinery the
  decision outbox proved out.

All tenant-scoped with fail-closed RLS, matching the existing governance tables.
"""

from __future__ import annotations

import os

from alembic import op

revision = "0017"
down_revision = "0016"
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
    # -- Calendar events -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE calendar_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
            tenant_id  uuid        NOT NULL,
            version    integer     NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120),
            deleted_at timestamptz,
            CONSTRAINT calendar_events_status
                CHECK (status IN ('Scheduled', 'Completed', 'Cancelled')),
            CONSTRAINT calendar_events_window
                CHECK (ends_at IS NULL OR ends_at >= starts_at)
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_calendar_events_updated_at BEFORE UPDATE ON calendar_events
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute("CREATE INDEX ix_calendar_events_organizer "
               "ON calendar_events (tenant_id, organizer, starts_at);")
    op.execute("CREATE INDEX ix_calendar_events_subject "
               "ON calendar_events (tenant_id, subject_type, subject_id);")
    op.execute("CREATE INDEX ix_calendar_events_window "
               "ON calendar_events (tenant_id, starts_at) WHERE status = 'Scheduled';")
    # A completed or cancelled meeting is a FACT: terminal rows freeze; DELETE is refused
    # (the audit trail and any follow-ups cite the row).
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
    _enable_rls("calendar_events", "calendar_events_tenant_isolation")
    _grant("calendar_events", "SELECT, INSERT, UPDATE")

    # -- Notifications (the durable in-app record) ---------------------------
    op.execute(
        """
        CREATE TABLE notifications (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
            tenant_id  uuid        NOT NULL,
            version    integer     NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120),
            deleted_at timestamptz,
            CONSTRAINT notifications_severity
                CHECK (severity IN ('info', 'warning', 'critical'))
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_notifications_updated_at BEFORE UPDATE ON notifications
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    # Idempotency anchor: one notification per (tenant, dedupe_key). Partial — ad-hoc
    # notifications without a key are unconstrained.
    op.execute("CREATE UNIQUE INDEX notifications_tenant_dedupe "
               "ON notifications (tenant_id, dedupe_key) WHERE dedupe_key IS NOT NULL;")
    op.execute("CREATE INDEX ix_notifications_inbox "
               "ON notifications (tenant_id, recipient, created_at DESC);")
    op.execute("CREATE INDEX ix_notifications_unread "
               "ON notifications (tenant_id, recipient) WHERE read_at IS NULL;")
    _enable_rls("notifications", "notifications_tenant_isolation")
    _grant("notifications", "SELECT, INSERT, UPDATE")

    # -- Notification deliveries (external-channel outbox) -------------------
    op.execute(
        """
        CREATE TABLE notification_deliveries (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
            tenant_id  uuid        NOT NULL,
            version    integer     NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120),
            deleted_at timestamptz,
            CONSTRAINT notification_deliveries_channel
                CHECK (channel IN ('email', 'sms', 'webhook')),
            CONSTRAINT notification_deliveries_status
                CHECK (status IN ('pending', 'delivered', 'dead')),
            CONSTRAINT notification_deliveries_unique
                UNIQUE (tenant_id, notification_id, channel)
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_notification_deliveries_updated_at
        BEFORE UPDATE ON notification_deliveries
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute("CREATE INDEX ix_notification_deliveries_due "
               "ON notification_deliveries (tenant_id, next_attempt_at) "
               "WHERE status = 'pending';")
    _enable_rls("notification_deliveries", "notification_deliveries_tenant_isolation")
    _grant("notification_deliveries", "SELECT, INSERT, UPDATE")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_deliveries CASCADE;")
    op.execute("DROP TABLE IF EXISTS notifications CASCADE;")
    op.execute("DROP TABLE IF EXISTS calendar_events CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS calendar_event_guard();")
