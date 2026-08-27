"""activity events

Revision ID: b7d99d980665
Revises: 557ed1ef53de
Create Date: 2026-08-26 14:53:53.738836

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7d99d980665'
down_revision: str | Sequence[str] | None = '557ed1ef53de'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "activity_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("credential_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("surface", sa.String(length=4), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("project_slug", sa.String(length=128), nullable=True),
        sa.Column("bank_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=True),
        sa.Column("content_bytes", sa.Integer(), nullable=True),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_activity_events_tenant_id"), "activity_events", ["tenant_id"])
    op.create_index(op.f("ix_activity_events_action"), "activity_events", ["action"])
    op.create_index(op.f("ix_activity_events_created_at"), "activity_events", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_activity_events_created_at"), table_name="activity_events")
    op.drop_index(op.f("ix_activity_events_action"), table_name="activity_events")
    op.drop_index(op.f("ix_activity_events_tenant_id"), table_name="activity_events")
    op.drop_table("activity_events")
