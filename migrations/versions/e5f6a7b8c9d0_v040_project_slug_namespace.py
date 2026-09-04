"""v0.4.0 project slug namespace

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scalar(statement: str) -> int:
    return int(op.get_bind().execute(sa.text(statement)).scalar_one())


def _abort_if_rows(statement: str, message: str) -> None:
    """Keep data validation executable in online and offline migrations.

    Offline Alembic has no result-bearing bind, so Python cannot count rows.
    Emit the equivalent PostgreSQL validation for the generated script; a
    connected migration retains the existing RuntimeError before destructive
    DDL runs.
    """
    if context.is_offline_mode():
        escaped_message = message.replace("'", "''")
        op.execute(
            f"""
            DO $ach_memory$
            BEGIN
                IF EXISTS (
                    {statement}
                ) THEN
                    RAISE EXCEPTION '{escaped_message}';
                END IF;
            END
            $ach_memory$
            """
        )
        return

    if _scalar(f"SELECT count(*) FROM ({statement}) invalid"):
        raise RuntimeError(message)


def upgrade() -> None:
    """Move live and retired names into one tenant-global namespace."""
    # SHARE ROW EXCLUSIVE conflicts with the ROW EXCLUSIVE lock taken by
    # INSERT/UPDATE/DELETE while still allowing ordinary reads. Acquire both
    # legacy namespace sources before the first duplicate snapshot so an
    # admin alias release or project write cannot disappear between checking,
    # copying and dropping the old storage.
    op.execute(
        "LOCK TABLE projects, retired_slugs IN SHARE ROW EXCLUSIVE MODE"
    )
    _abort_if_rows(
        """
        SELECT tenant_id, slug
        FROM (
            SELECT tenant_id, project_slug AS slug FROM projects
            UNION ALL
            SELECT tenant_id, retired_slug AS slug FROM retired_slugs
        ) names
        GROUP BY tenant_id, slug
        HAVING count(*) > 1
        """,
        "cannot migrate duplicate tenant project slugs; resolve them before upgrade",
    )

    op.create_table(
        "project_slugs",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("project_internal_id", sa.String(length=64), nullable=False),
        sa.Column("is_canonical", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "slug"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_project_slugs_tenant_slug"),
    )
    op.create_index(
        "uq_project_slugs_canonical_project",
        "project_slugs",
        ["tenant_id", "project_internal_id"],
        unique=True,
        postgresql_where=sa.text("is_canonical"),
    )

    op.execute(
        """
        INSERT INTO project_slugs
            (tenant_id, slug, project_internal_id, is_canonical, created_at)
        SELECT tenant_id, project_slug, internal_id, true, created_at
        FROM projects
        """
    )
    op.execute(
        """
        INSERT INTO project_slugs
            (tenant_id, slug, project_internal_id, is_canonical, created_at)
        SELECT tenant_id, retired_slug, project_internal_id, false, retired_at
        FROM retired_slugs
        """
    )

    _abort_if_rows(
        """
        SELECT p.internal_id
        FROM projects p
        LEFT JOIN project_slugs ps
          ON ps.tenant_id = p.tenant_id
         AND ps.project_internal_id = p.internal_id
         AND ps.is_canonical
        GROUP BY p.internal_id
        HAVING count(ps.slug) <> 1
        """,
        "cannot migrate projects without exactly one canonical slug",
    )

    op.drop_table("retired_slugs")
    op.drop_index(op.f("ix_projects_project_slug"), table_name="projects")
    op.drop_constraint("projects_tenant_id_project_slug_key", "projects", type_="unique")
    op.drop_column("projects", "project_slug")


def downgrade() -> None:
    """Restore the legacy live column and retired-slug table."""
    # Fence unified-namespace writes before the first canonical snapshot.
    # Lock projects in the same statement so a concurrent create/delete
    # cannot split the project row from the mapping being copied back.
    op.execute(
        "LOCK TABLE projects, project_slugs IN SHARE ROW EXCLUSIVE MODE"
    )
    _abort_if_rows(
        """
        SELECT p.internal_id
        FROM projects p
        LEFT JOIN project_slugs ps
          ON ps.tenant_id = p.tenant_id
         AND ps.project_internal_id = p.internal_id
         AND ps.is_canonical
        GROUP BY p.internal_id
        HAVING count(ps.slug) <> 1
        """,
        "cannot downgrade projects without exactly one canonical slug",
    )

    op.add_column("projects", sa.Column("project_slug", sa.String(length=128), nullable=True))
    op.execute(
        """
        UPDATE projects p
        SET project_slug = ps.slug
        FROM project_slugs ps
        WHERE ps.tenant_id = p.tenant_id
          AND ps.project_internal_id = p.internal_id
          AND ps.is_canonical
        """
    )
    op.alter_column("projects", "project_slug", nullable=False)
    op.create_unique_constraint(
        "projects_tenant_id_project_slug_key",
        "projects",
        ["tenant_id", "project_slug"],
    )
    op.create_index(
        op.f("ix_projects_project_slug"),
        "projects",
        ["project_slug"],
        unique=False,
    )

    op.create_table(
        "retired_slugs",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("retired_slug", sa.String(length=128), nullable=False),
        sa.Column("project_internal_id", sa.String(length=64), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "retired_slug"),
    )
    op.execute(
        """
        INSERT INTO retired_slugs
            (tenant_id, retired_slug, project_internal_id, retired_at)
        SELECT tenant_id, slug, project_internal_id, created_at
        FROM project_slugs
        WHERE NOT is_canonical
        """
    )

    op.drop_index("uq_project_slugs_canonical_project", table_name="project_slugs")
    op.drop_table("project_slugs")
