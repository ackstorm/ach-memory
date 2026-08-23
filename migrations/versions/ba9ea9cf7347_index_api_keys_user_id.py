"""index api_keys.user_id

Revision ID: ba9ea9cf7347
Revises: b8ddcf824c02
Create Date: 2026-08-23 16:52:30.418135

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ba9ea9cf7347'
down_revision: str | Sequence[str] | None = 'b8ddcf824c02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # GET /v1/users/{id}/keys filters on api_keys.user_id, and every other
    # tenant-scoped FK in this schema already carries an index.
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)
    # One clock for the audit trail. The Python-side default stamps each
    # replica's own clock, and the Helm chart exposes replicaCount, so
    # list_audit's ordering depended on which pod inserted the row. Added by
    # hand: autogenerate does not detect server_default changes unless
    # compare_server_default is enabled.
    op.alter_column(
        "audit_events",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "audit_events",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
    )
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
