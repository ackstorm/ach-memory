"""project metadata

Revision ID: a1b2c3d4e5f6
Revises: b7d99d980665
Create Date: 2026-08-28 10:12:44.019283

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = 'b7d99d980665'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Orientation as a record, not as memory: without these columns a project's
    # name, spec pointer and purpose can only be stored as Hindsight facts,
    # where they compete with real memory for a profile item budget.
    # Nullable because every existing project has none of them, and a NOT NULL
    # with a default would invent orientation nobody wrote.
    op.add_column("projects", sa.Column("name", sa.String(length=128), nullable=True))
    op.add_column(
        "projects", sa.Column("canonical_spec", sa.String(length=512), nullable=True)
    )
    op.add_column("projects", sa.Column("purpose", sa.String(length=256), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "purpose")
    op.drop_column("projects", "canonical_spec")
    op.drop_column("projects", "name")
