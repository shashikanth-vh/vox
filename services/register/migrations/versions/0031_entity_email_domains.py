"""Companies carry the e-mail domains their people write from.

The mailbox reader (PULSE) attaches a conversation to a company first by the
domain of its outside parties: ``["acmesolar.in", "acme-renewables.com"]``.
JSONB like ``news_aliases``: a handful of names per company, always read with
the company. Empty = matched only through the contact e-mails on its leads and
enquiries, or by hand from the review queue.

Revision ID: 0031
Revises: 0030
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE entities ADD COLUMN IF NOT EXISTS email_domains JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS email_domains")
