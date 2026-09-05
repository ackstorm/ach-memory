"""Remove retired capture and context-revision storage.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""
from collections.abc import Sequence
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS capture_slices")
    op.execute("DROP TABLE IF EXISTS context_revisions")


def downgrade() -> None:
    # Retired queue/profile data is not reconstructable; downgrade restores
    # empty compatibility tables only.
    op.execute("CREATE TABLE IF NOT EXISTS capture_slices (id uuid PRIMARY KEY)")
    op.execute("CREATE TABLE IF NOT EXISTS context_revisions (tenant_id varchar(64) NOT NULL, user_id varchar(128) NOT NULL, project_slug varchar(128) NOT NULL, workspace_id varchar(35) NOT NULL, revision integer NOT NULL, fingerprint varchar(64) NOT NULL, updated_at timestamptz NOT NULL, PRIMARY KEY (tenant_id, user_id, project_slug, workspace_id))")
