"""v0.4.0 retained record revisions

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retained_record_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("retained_record_id", sa.Uuid(), nullable=False),
        sa.Column("curation_operation_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("canonical_content", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("memory_type", sa.String(length=16), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("sanitized_evidence", sa.JSON(), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["retained_record_id"], ["retained_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "curation_operation_id",
            name="uq_retained_record_revisions_operation",
        ),
        sa.UniqueConstraint(
            "retained_record_id",
            "revision",
            name="uq_retained_record_revisions_record_revision",
        ),
    )


def downgrade() -> None:
    op.drop_table("retained_record_revisions")
