"""external identities

Revision ID: 557ed1ef53de
Revises: ba9ea9cf7347
Create Date: 2026-08-25 13:46:07.345633

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '557ed1ef53de'
down_revision: str | Sequence[str] | None = 'ba9ea9cf7347'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # (issuer, subject) is the primary key because it is the only globally
    # unique name an IdP gives us, and neither half works alone as a User.id.
    # Without this table the same human arriving through two routes mints a
    # second User -- and a second bank_id -- splitting their memory in half.
    op.create_table(
        "external_identities",
        sa.Column("issuer", sa.String(length=256), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("credential_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("issuer", "subject"),
    )
    op.create_index(
        op.f("ix_external_identities_credential_id"),
        "external_identities",
        ["credential_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_external_identities_tenant_id"),
        "external_identities",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_identities_user_id"),
        "external_identities",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_external_identities_user_id"), table_name="external_identities")
    op.drop_index(op.f("ix_external_identities_tenant_id"), table_name="external_identities")
    op.drop_index(
        op.f("ix_external_identities_credential_id"), table_name="external_identities"
    )
    op.drop_table("external_identities")
