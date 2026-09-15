"""drop working_states

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15 06:10:00

Working State was never read by anything: `load_context` did not deliver it
and the host's own compaction summary covers the same ground.
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("working_states")


def downgrade() -> None:
    op.create_table(
        "working_states",
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.String(length=35), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "workspace_id"),
    )
