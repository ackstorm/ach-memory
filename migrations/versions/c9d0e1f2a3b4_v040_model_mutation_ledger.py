"""v0.4.0 model mutation ledger

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SCOPE_IDENTITY = (
    "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
    "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "mental_model_mutations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("project_internal_id", sa.String(length=64), nullable=True),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("upstream_operation_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_SCOPE_IDENTITY, name="ck_mental_model_mutations_scope_identity"),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "operation_id",
            name="uq_mental_model_mutations_bank_operation",
            postgresql_nulls_not_distinct=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("mental_model_mutations")
