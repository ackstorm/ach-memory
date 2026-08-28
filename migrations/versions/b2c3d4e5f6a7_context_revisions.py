"""context revisions

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-28 16:41:05.113927

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: str | Sequence[str] | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # One monotonic revision per compiled-context snapshot, so a consumer
    # holding a cached index tier and a freshly fetched full tier can tell
    # which is newer without reconciling their contents.
    op.create_table(
        "context_revisions",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        # "" for the snapshot with no project: this is part of the primary
        # key, and NULL never equals NULL.
        sa.Column("project_slug", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "project_slug"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("context_revisions")
