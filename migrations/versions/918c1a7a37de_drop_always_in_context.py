"""drop always_in_context

Revision ID: 918c1a7a37de
Revises: c9d0e1f2a3b4
Create Date: 2026-09-09 08:00:21.449456

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '918c1a7a37de'
down_revision: str | Sequence[str] | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("mental_model_registrations", "always_in_context")


def downgrade() -> None:
    """Downgrade schema."""
    # Restored non-null with a server default so an existing row is valid:
    # under the old rule every built-in was standing and every custom was
    # not, which is exactly what this default cannot express -- a downgrade
    # therefore needs the follow-up UPDATE below, not just the column.
    op.add_column(
        "mental_model_registrations",
        sa.Column(
            "always_in_context",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        "UPDATE mental_model_registrations "
        "SET always_in_context = true WHERE origin = 'builtin'"
    )
    op.alter_column(
        "mental_model_registrations", "always_in_context", server_default=None
    )
