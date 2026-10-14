"""Companies carry what the News Radar watches: their key people and their news names.

A borrower's risk is often its promoters' risk: a director in a fraud case is news
about the company even when the company's name is never printed. The entity master
gains ``key_people`` — a short list of ``{name, role, city}`` the analyst keeps — so
the News Radar can search for them alongside the firm. JSONB, like ``tags``: a
handful of names per company, always read with the company, never queried alone.

``news_aliases`` are the names the press actually prints ("Enerparc" for "Enerparc
Energy", "Saera" for "Saera Electric Auto"); when set they are the whole list the
firm is searched by, so a firm named after a word ("Land") can be watched under a
fuller name instead. ``news_watch`` takes a record out of the news digest
(a placeholder, a test row, a place or a person entered as a company); NULL means
watched, so every existing company stays in.

Revision ID: 0030
Revises: 0029
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE entities
            ADD COLUMN IF NOT EXISTS key_people JSONB,
            ADD COLUMN IF NOT EXISTS news_aliases JSONB,
            ADD COLUMN IF NOT EXISTS news_watch BOOLEAN
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS key_people, "
               "DROP COLUMN IF EXISTS news_aliases, DROP COLUMN IF EXISTS news_watch")
