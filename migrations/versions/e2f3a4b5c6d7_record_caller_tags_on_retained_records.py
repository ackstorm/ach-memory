"""record caller tags on retained records

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-10 09:10:00.000000

Nullable on purpose, and it stays nullable.

Every row written from here on gets a list, empty when the caller named no
tags. Every row written before this migration gets NULL, and NULL is read as
"not recorded" rather than "there were none" -- see `curation_service.
_tags_for`. Backfilling `[]` would be the lie: it would assert that those
records carried no caller tags, and a mental model narrowed by one would then
be provably excluded by data that was never observed, which is exactly the
silent staleness this column exists to end.

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: str | Sequence[str] | None = 'd1e2f3a4b5c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "retained_records",
        sa.Column("caller_tags", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema.

    Lossy and knowingly so: the caller tags are recorded nowhere else, so
    dropping the column discards them. A re-upgrade brings back NULLs, which
    the withhold rule then reads as "not recorded" -- correct, if pessimistic,
    for rows that really did carry tags before the downgrade.
    """
    op.drop_column("retained_records", "caller_tags")
