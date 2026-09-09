"""widen mental_model_registrations.tags_match for all_strict/any_strict

Revision ID: 73e00e0dd20c
Revises: 918c1a7a37de
Create Date: 2026-09-09 22:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '73e00e0dd20c'
down_revision: str | Sequence[str] | None = '918c1a7a37de'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('mental_model_registrations', 'tags_match',
               existing_type=sa.VARCHAR(length=8),
               type_=sa.String(length=16),
               existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('mental_model_registrations', 'tags_match',
               existing_type=sa.String(length=16),
               type_=sa.VARCHAR(length=8),
               existing_nullable=False)
