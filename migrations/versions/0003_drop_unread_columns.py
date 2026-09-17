"""drop external_identities.credential_id and projects.git_locator

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17 00:00:00

Both were written on insert and never read back. `(issuer, subject)` is
already the external identity's primary key, and `git_locator` was hardcoded
to NULL at the only site that built a Project.
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("ix_external_identities_credential_id", table_name="external_identities")
    op.drop_column("external_identities", "credential_id")
    op.drop_column("projects", "git_locator")


def downgrade() -> None:
    op.add_column("projects", sa.Column("git_locator", sa.String(length=512), nullable=True))
    # Nullable on the way back: the values it used to hold are gone.
    op.add_column(
        "external_identities", sa.Column("credential_id", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "ix_external_identities_credential_id",
        "external_identities",
        ["credential_id"],
        unique=True,
    )
