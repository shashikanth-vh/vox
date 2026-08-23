"""VOX corrected transcript — fix a mis-hearing once, regenerate everywhere.

STT mangles proper nouns, and a name misheard once ("Sarvodaya") propagates
into every field, bullet and snippet of the structured report. The fix is a
CORRECTION, not an edit: the verbatim transcript stays exactly as heard —
evidence is never rewritten — and the corrected copy lives beside it, audited
like any other change. Regeneration re-runs the structuring stage on the
corrected text, and ``preserved_overrides`` carries the reviewer's own
confirmed values across the rebuild so their work survives the refresh.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        ADD COLUMN IF NOT EXISTS corrected_transcript text,
        ADD COLUMN IF NOT EXISTS preserved_overrides jsonb
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        DROP COLUMN IF EXISTS corrected_transcript,
        DROP COLUMN IF EXISTS preserved_overrides
    """)
