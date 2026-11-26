"""projects: one access list of users and groups replaces the single owner

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-26 00:00:00
"""
import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    orphans = op.get_bind().execute(sa.text(
        "SELECT internal_id FROM projects p WHERE owner_type = 'user' AND NOT EXISTS "
        "(SELECT 1 FROM external_identities e WHERE e.user_id = p.owner_id) ORDER BY internal_id"
    )).scalars().all()
    if orphans:
        # Converting these would leave projects nobody but an operator can reach.
        raise RuntimeError(f"projects whose owner has no identity: {', '.join(orphans)}")
    op.add_column("projects", sa.Column("access", sa.JSON(), nullable=True))
    op.execute("""
        UPDATE projects p SET access = json_build_object(
            'users', COALESCE((
                SELECT json_agg(s ORDER BY s) FROM (
                    SELECT DISTINCT lower(e.subject) AS s FROM external_identities e
                    WHERE p.owner_type = 'user' AND e.user_id = p.owner_id) subjects
            ), '[]'::json),
            'groups', CASE WHEN p.owner_type = 'group' THEN json_build_array(p.owner_id) ELSE '[]'::json END)
    """)
    op.alter_column("projects", "access", nullable=False)
    op.drop_column("projects", "owner_type")
    op.drop_column("projects", "owner_id")


def downgrade() -> None:
    op.add_column("projects", sa.Column("owner_type", sa.String(8), nullable=True))
    op.add_column("projects", sa.Column("owner_id", sa.String(128), nullable=True))
    # The first user on the list becomes the owner; with no user, the first group.
    op.execute("""
        UPDATE projects p SET
            owner_type = CASE WHEN json_array_length(p.access->'users') > 0 THEN 'user' ELSE 'group' END,
            owner_id = COALESCE(
                (SELECT e.user_id FROM external_identities e
                 WHERE lower(e.subject) = p.access->'users'->>0 LIMIT 1),
                p.access->'groups'->>0)
    """)
    op.alter_column("projects", "owner_type", nullable=False)
    op.alter_column("projects", "owner_id", nullable=False)
    op.drop_column("projects", "access")
