"""record the reason on curation operations

Revision ID: f1a2b3c4d5e6
Revises: e2f3a4b5c6d7
Create Date: 2026-09-11 12:00:00.000000

Nullable on purpose, and it stays nullable.

The reason a caller gives `forget` used to be forwarded to Hindsight on the
triggering call and kept nowhere (QA F-14): `memory_history` could show that
a claim was forgotten but never why. From here on `curation_service` stores
it on the operation row, capped at the column width. Every row written
before this migration gets NULL, and NULL reads as "not recorded" rather
than "no reason given" -- correct/restore/delete never carried one, and a
forget from before this column may well have. Backfilling an empty string
would assert the second where only the first is known.

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: str | Sequence[str] | None = 'e2f3a4b5c6d7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "curation_operations",
        sa.Column("reason", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema.

    Lossy and knowingly so: the reason is recorded nowhere else, so dropping
    the column discards it. A re-upgrade brings back NULLs, which read as
    "not recorded" -- true again, for rows that once carried one.
    """
    op.drop_column("curation_operations", "reason")
