"""v0.4.0 memory control plane

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SCOPE_IDENTITY = (
    "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
    "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)"
)


def _scope_columns() -> list[sa.Column]:
    return [
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("project_internal_id", sa.String(length=64), nullable=True),
    ]


def _scope_foreign_keys() -> list[sa.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_internal_id"], ["projects.internal_id"]),
    ]


def upgrade() -> None:
    op.create_table(
        "retained_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("source_memory_id", sa.String(length=128), nullable=True),
        sa.Column("canonical_content", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(length=16), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("sanitized_evidence", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle", sa.String(length=16), nullable=False),
        sa.Column("upstream_state", sa.String(length=16), nullable=False),
        sa.Column("calling_agent", sa.String(length=64), nullable=True),
        sa.Column("created_by_credential", sa.String(length=64), nullable=True),
        *_scope_foreign_keys(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_SCOPE_IDENTITY, name="ck_retained_records_scope_identity"),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "operation_id",
            name="uq_retained_records_bank_operation",
            postgresql_nulls_not_distinct=True,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "document_id",
            name="uq_retained_records_bank_document",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_retained_records_due",
        "retained_records",
        ["tenant_id", "scope", "valid_until"],
    )

    op.create_table(
        "curation_operations",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("retained_record_id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("desired_content", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("repair_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_scope_foreign_keys(),
        sa.ForeignKeyConstraint(["retained_record_id"], ["retained_records.id"]),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.CheckConstraint(_SCOPE_IDENTITY, name="ck_curation_operations_scope_identity"),
    )

    op.create_table(
        "bank_currentness",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("blocking_operation_id", sa.String(length=128), nullable=True),
        sa.Column("repair_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_scope_foreign_keys(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_SCOPE_IDENTITY, name="ck_bank_currentness_scope_identity"),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            name="uq_bank_currentness_bank",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "mental_model_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        *_scope_columns(),
        sa.Column("upstream_model_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("source_query", sa.Text(), nullable=False),
        sa.Column("source_tags", sa.JSON(), nullable=False),
        sa.Column("tags_match", sa.String(length=8), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("builtin_key", sa.String(length=64), nullable=True),
        sa.Column("definition_version", sa.Integer(), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("mutation_operation_id", sa.String(length=128), nullable=True),
        sa.Column("mutation_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("always_in_context", sa.Boolean(), nullable=False),
        sa.Column("delivery_state", sa.String(length=16), nullable=False),
        sa.Column("refresh_operation_id", sa.String(length=128), nullable=True),
        sa.Column("refresh_status", sa.String(length=16), nullable=True),
        sa.Column("repair_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_scope_foreign_keys(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            _SCOPE_IDENTITY,
            name="ck_mental_model_registrations_scope_identity",
        ),
        sa.CheckConstraint(
            "(origin = 'builtin' AND builtin_key IS NOT NULL "
            "AND definition_version IS NOT NULL) "
            "OR (origin = 'user' AND builtin_key IS NULL "
            "AND definition_version IS NULL)",
            name="ck_mental_model_registrations_origin_metadata",
        ),
        sa.CheckConstraint(
            "max_tokens > 0",
            name="ck_mental_model_registrations_positive_tokens",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "model_key",
            name="uq_mental_model_registrations_bank_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "uq_mental_model_registrations_bank_upstream",
        "mental_model_registrations",
        [
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "upstream_model_id",
        ],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("upstream_model_id IS NOT NULL"),
    )
    op.create_index(
        "uq_mental_model_registrations_bank_builtin",
        "mental_model_registrations",
        [
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "builtin_key",
        ],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("builtin_key IS NOT NULL"),
    )

    op.add_column(
        "working_sessions",
        sa.Column("completed_checkpoint_seq", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "working_sessions",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("working_sessions", "completed_at")
    op.drop_column("working_sessions", "completed_checkpoint_seq")
    op.drop_index(
        "uq_mental_model_registrations_bank_builtin",
        table_name="mental_model_registrations",
    )
    op.drop_index(
        "uq_mental_model_registrations_bank_upstream",
        table_name="mental_model_registrations",
    )
    op.drop_table("mental_model_registrations")
    op.drop_table("bank_currentness")
    op.drop_table("curation_operations")
    op.drop_index("ix_retained_records_due", table_name="retained_records")
    op.drop_table("retained_records")
