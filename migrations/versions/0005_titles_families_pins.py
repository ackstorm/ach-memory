"""titles, families, pins and the read log

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14 00:00:00
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("families", sa.JSON(), nullable=True))
    op.create_table(
        "memories",
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=True),
        sa.Column("family", sa.String(length=48), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("memory_type", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("ix_memories_owner_state", "memories", ["owner_id", "state"])
    op.create_table(
        "read_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=True),
        sa.Column("project_internal_id", sa.String(length=64), nullable=True),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("family", sa.String(length=48), nullable=True),
        sa.Column("results", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_read_events_created_at", "read_events", ["created_at"])
    op.create_index("ix_read_events_project_internal_id", "read_events", ["project_internal_id"])
    # Reads per day, project and tool: `SELECT * FROM read_daily WHERE day > now() - interval '7 days'`.
    op.execute("""
        CREATE VIEW read_daily AS
        SELECT date_trunc('day', r.created_at)::date AS day,
               s.slug AS project,
               r.tool,
               count(*) AS calls,
               count(DISTINCT r.actor_user_id) AS users,
               count(*) FILTER (WHERE r.results = 0) AS empty,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms) AS p50_ms
        FROM read_events r
        LEFT JOIN project_slugs s ON s.project_internal_id = r.project_internal_id AND s.is_canonical
        GROUP BY 1, 2, 3
    """)


def downgrade() -> None:
    op.execute("DROP VIEW read_daily")
    op.drop_table("read_events")
    op.drop_table("memories")
    op.drop_column("projects", "families")
