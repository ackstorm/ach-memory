"""capture slices

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-31 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | Sequence[str] | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # One durable, replay-safe checkpoint job. Identity is the full unique
    # constraint below -- resubmitting the exact same slice after a lost
    # acknowledgement must resolve to the row already there, never create a
    # second one or re-run a completed stage.
    op.create_table(
        "capture_slices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("project_internal_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=35), nullable=False),
        sa.Column("host", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("session_epoch", sa.BigInteger(), nullable=False),
        sa.Column("start_offset", sa.BigInteger(), nullable=False),
        sa.Column("end_offset", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("sanitized_hash", sa.String(length=64), nullable=False),
        sa.Column("sanitized_content", sa.Text(), nullable=True),
        sa.Column("extraction", sa.JSON(), nullable=True),
        sa.Column("hindsight_operations", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "project_internal_id",
            "workspace_id",
            "session_id",
            "start_offset",
            "end_offset",
            "content_hash",
            name="uq_capture_slices_identity",
        ),
        sa.CheckConstraint(
            "start_offset >= 0", name="ck_capture_slices_start_offset_non_negative"
        ),
        sa.CheckConstraint("end_offset > start_offset", name="ck_capture_slices_end_after_start"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_capture_slices_attempt_count_non_negative"
        ),
    )
    op.create_index(
        "ix_capture_slices_status_available_at",
        "capture_slices",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_capture_slices_lease_until",
        "capture_slices",
        ["lease_until"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_capture_slices_lease_until", table_name="capture_slices")
    op.drop_index("ix_capture_slices_status_available_at", table_name="capture_slices")
    op.drop_table("capture_slices")
