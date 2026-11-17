"""read_events: session id, unresolved project slug, query length

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 00:00:00
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Fail rather than queue every read behind a lock held by a long transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("read_events", sa.Column("project_slug", sa.String(128), nullable=True))
    op.add_column("read_events", sa.Column("session_id", sa.String(128), nullable=True))
    op.add_column("read_events", sa.Column("query_chars", sa.Integer(), nullable=True))
    op.create_index("ix_read_events_session_id", "read_events", ["session_id"])
    op.execute("UPDATE read_events SET query_chars = length(query) "
               "WHERE query IS NOT NULL AND query NOT LIKE '[withheld:%'")


def downgrade() -> None:
    op.drop_index("ix_read_events_session_id", table_name="read_events")
    op.drop_column("read_events", "query_chars")
    op.drop_column("read_events", "session_id")
    op.drop_column("read_events", "project_slug")
