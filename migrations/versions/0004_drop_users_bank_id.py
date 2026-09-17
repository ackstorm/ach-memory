"""drop users.bank_id

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17 00:00:00

A user's bank is `user_<user_id>`, derived in `resolve_bank` on every request
and never read back from this column -- SPEC §7 says as much. Only a project
needs its bank id stored, that one being a random uuid4.
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Postgres drops the column's unique constraint with it.
    op.drop_column("users", "bank_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("bank_id", sa.String(length=64), nullable=True))
    op.execute("UPDATE users SET bank_id = 'user_' || id")
    op.alter_column("users", "bank_id", nullable=False)
    op.create_unique_constraint("users_bank_id_key", "users", ["bank_id"])
