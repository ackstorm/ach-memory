"""working state

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-31 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: str | Sequence[str] | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # A session's ordering identity within one (user, project, workspace)
    # scope -- never its content. session_epoch is server-issued and
    # strictly increasing by creation order, so no caller can inflate one.
    op.create_table(
        "working_sessions",
        sa.Column("session_epoch", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("project_internal_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=35), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
        sa.PrimaryKeyConstraint("session_epoch"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "project_internal_id",
            "workspace_id",
            "session_id",
            name="uq_working_sessions_scope_session",
        ),
    )

    # The one live explicit handoff per (tenant, user, project, workspace).
    # PostgreSQL state, never a Hindsight fact/observation/document. Total
    # replacement: no payload history, no TTL, no decay.
    op.create_table(
        "working_states",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("project_internal_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=35), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("current_direction", sa.Text(), nullable=True),
        sa.Column("recent_decisions", sa.JSON(), nullable=False),
        sa.Column("open_questions", sa.JSON(), nullable=False),
        sa.Column("next_steps", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("session_epoch", sa.BigInteger(), nullable=False),
        sa.Column("checkpoint_seq", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "project_internal_id", "workspace_id"),
        sa.CheckConstraint("session_epoch >= 0", name="ck_working_states_session_epoch_non_negative"),
        sa.CheckConstraint("checkpoint_seq >= 0", name="ck_working_states_checkpoint_seq_non_negative"),
    )

    # Extend context_revisions to be workspace-scoped. "" for a snapshot with
    # no workspace, same convention project_slug already uses -- this is part
    # of the primary key and NULL never equals NULL. The server_default
    # backfills every existing row in the same statement, then is dropped so
    # every future write must name its workspace explicitly.
    op.add_column(
        "context_revisions",
        sa.Column("workspace_id", sa.String(length=35), nullable=False, server_default=""),
    )
    op.drop_constraint("context_revisions_pkey", "context_revisions", type_="primary")
    op.create_primary_key(
        "context_revisions_pkey",
        "context_revisions",
        ["tenant_id", "user_id", "project_slug", "workspace_id"],
    )
    op.alter_column("context_revisions", "workspace_id", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("context_revisions_pkey", "context_revisions", type_="primary")
    op.create_primary_key(
        "context_revisions_pkey",
        "context_revisions",
        ["tenant_id", "user_id", "project_slug"],
    )
    op.drop_column("context_revisions", "workspace_id")

    op.drop_table("working_states")
    op.drop_table("working_sessions")
