"""read_events.relevance: the best decision-model relevance of each recall

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16 00:00:00
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("read_events", sa.Column("relevance", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("read_events", "relevance")
