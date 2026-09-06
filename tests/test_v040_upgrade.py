"""Clean-install and released-v0.3.5 upgrade proofs for the v0.4.0 schema."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL, _ensure_database_exists

_ROOT = Path(__file__).resolve().parents[1]
_V035_HEAD = "b7d99d980665"
# The last schema state before `a7b8c9d0e1f2` drops `capture_slices` and
# `context_revisions` -- everything the pre-removal product actually wrote
# (Working Session/Working State, a capture slice, a context revision)
# still exists at this exact revision, in its exact predecessor shape.
_PRE_RETIREMENT_HEAD = "f6a7b8c9d0e1"


@pytest.fixture
def upgrade_database_url():
    target = make_url(TEST_DATABASE_URL).set(
        database=f"memory_v040_upgrade_{uuid.uuid4().hex[:12]}"
    )
    url = target.render_as_string(hide_password=False)
    _ensure_database_exists(url)
    try:
        yield url
    finally:
        admin = create_engine(
            make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            with admin.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": target.database},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}"'))
        finally:
            admin.dispose()


@contextmanager
def _preserve_logging():
    manager = logging.Logger.manager
    before = {
        name: logger.disabled
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    try:
        yield
    finally:
        for name, logger in manager.loggerDict.items():
            if isinstance(logger, logging.Logger):
                logger.disabled = before.get(name, logger.disabled)


def _upgrade(url: str, monkeypatch, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    with _preserve_logging():
        command.upgrade(Config(str(_ROOT / "alembic.ini")), revision)


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            }
    finally:
        engine.dispose()


def test_clean_install_reaches_the_v040_control_plane(
    upgrade_database_url, monkeypatch
):
    _upgrade(upgrade_database_url, monkeypatch, "head")

    assert {
        "project_slugs",
        "retained_records",
        "retained_record_revisions",
        "curation_operations",
        "bank_currentness",
        "mental_model_registrations",
        "mental_model_mutations",
        "working_sessions",
        "working_states",
    } <= _tables(upgrade_database_url)
    assert {"capture_slices", "context_revisions", "retired_slugs"}.isdisjoint(
        _tables(upgrade_database_url)
    )


def test_released_v035_data_survives_v040_upgrade(
    upgrade_database_url, monkeypatch
):
    _upgrade(upgrade_database_url, monkeypatch, _V035_HEAD)
    engine = create_engine(upgrade_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (id) VALUES ('tenant-upgrade')"))
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, bank_id, created_at) VALUES "
                    "('usr-upgrade', 'tenant-upgrade', 'bank-user-upgrade', now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO projects "
                    "(internal_id, tenant_id, project_slug, owner_type, owner_id, "
                    "bank_id, created_at, updated_at) VALUES "
                    "('prj-upgrade', 'tenant-upgrade', 'current-name', 'user', "
                    "'usr-upgrade', 'bank-project-upgrade', now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO retired_slugs "
                    "(tenant_id, retired_slug, project_internal_id, retired_at) VALUES "
                    "('tenant-upgrade', 'previous-name', 'prj-upgrade', now())"
                )
            )
    finally:
        engine.dispose()

    _upgrade(upgrade_database_url, monkeypatch, "head")

    engine = create_engine(upgrade_database_url)
    try:
        with engine.connect() as conn:
            user = conn.execute(
                text("SELECT id, bank_id FROM users WHERE id = 'usr-upgrade'")
            ).one()
            project = conn.execute(
                text(
                    "SELECT internal_id, bank_id FROM projects "
                    "WHERE internal_id = 'prj-upgrade'"
                )
            ).one()
            slugs = conn.execute(
                text(
                    "SELECT slug, is_canonical FROM project_slugs "
                    "WHERE project_internal_id = 'prj-upgrade' ORDER BY slug"
                )
            ).all()
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()

    assert tuple(user) == ("usr-upgrade", "bank-user-upgrade")
    assert tuple(project) == ("prj-upgrade", "bank-project-upgrade")
    assert [tuple(row) for row in slugs] == [
        ("current-name", True),
        ("previous-name", False),
    ]
    assert head == "c9d0e1f2a3b4"


def test_pre_retirement_state_survives_forward_removal(
    upgrade_database_url, monkeypatch
):
    """Working Session/Working State must survive `a7b8c9d0e1f2`'s removal
    of `capture_slices`/`context_revisions` untouched, only those two
    obsolete tables may disappear, and the removal itself must never reach
    Hindsight -- proven by running the upgrade under an HTTP deny-all
    transport rather than merely asserting no client method was invoked."""
    import httpx
    import respx

    _upgrade(upgrade_database_url, monkeypatch, _PRE_RETIREMENT_HEAD)
    workspace_id = "ws_" + "0" * 32
    engine = create_engine(upgrade_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (id) VALUES ('tenant-retire')"))
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, bank_id, created_at) VALUES "
                    "('usr-retire', 'tenant-retire', 'bank-user-retire', now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO projects "
                    "(internal_id, tenant_id, owner_type, owner_id, bank_id, created_at, updated_at) "
                    "VALUES ('prj-retire', 'tenant-retire', 'user', 'usr-retire', "
                    "'bank-project-retire', now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO project_slugs "
                    "(tenant_id, slug, project_internal_id, is_canonical, created_at) VALUES "
                    "('tenant-retire', 'retire-project', 'prj-retire', true, now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO working_sessions "
                    "(tenant_id, user_id, project_internal_id, workspace_id, session_id, started_at) "
                    "VALUES (:tenant_id, :user_id, :project_internal_id, :workspace_id, :session_id, now())"
                ),
                {
                    "tenant_id": "tenant-retire", "user_id": "usr-retire",
                    "project_internal_id": "prj-retire", "workspace_id": workspace_id,
                    "session_id": "s1",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO working_states "
                    "(tenant_id, user_id, project_internal_id, workspace_id, objective, "
                    "current_direction, recent_decisions, open_questions, next_steps, "
                    "updated_at, session_id, session_epoch, checkpoint_seq) "
                    "VALUES (:tenant_id, :user_id, :project_internal_id, :workspace_id, "
                    ":objective, NULL, '[]', '[]', '[]', now(), :session_id, 0, 0)"
                ),
                {
                    "tenant_id": "tenant-retire", "user_id": "usr-retire",
                    "project_internal_id": "prj-retire", "workspace_id": workspace_id,
                    "objective": "pre-retirement objective", "session_id": "s1",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO capture_slices "
                    "(id, tenant_id, user_id, project_internal_id, workspace_id, host, "
                    "session_id, session_epoch, start_offset, end_offset, content_hash, "
                    "sanitized_hash, sanitized_content, extraction, hindsight_operations, "
                    "status, attempt_count, available_at, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :user_id, :project_internal_id, :workspace_id, "
                    ":host, :session_id, 0, 0, 10, :content_hash, :sanitized_hash, "
                    ":sanitized_content, NULL, NULL, :status, 0, now(), now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()), "tenant_id": "tenant-retire",
                    "user_id": "usr-retire", "project_internal_id": "prj-retire",
                    "workspace_id": workspace_id, "host": "cli", "session_id": "s1",
                    "content_hash": "c" * 64, "sanitized_hash": "s" * 64,
                    "sanitized_content": "pre-retirement transcript slice",
                    "status": "pending",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO context_revisions "
                    "(tenant_id, user_id, project_slug, workspace_id, revision, fingerprint, updated_at) "
                    "VALUES (:tenant_id, :user_id, :project_slug, :workspace_id, 1, :fingerprint, now())"
                ),
                {
                    "tenant_id": "tenant-retire", "user_id": "usr-retire",
                    "project_slug": "retire-project", "workspace_id": workspace_id,
                    "fingerprint": "f" * 64,
                },
            )
    finally:
        engine.dispose()

    with respx.mock(assert_all_called=False) as router:
        router.route(url__regex=r".*").mock(
            side_effect=httpx.ConnectError("Hindsight must never be reached during a migration")
        )
        _upgrade(upgrade_database_url, monkeypatch, "head")

    engine = create_engine(upgrade_database_url)
    try:
        with engine.connect() as conn:
            remaining_tables = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name IN ('capture_slices', 'context_revisions')"
                    )
                )
            }
            session_row = conn.execute(
                text(
                    "SELECT session_id, workspace_id FROM working_sessions "
                    "WHERE tenant_id = 'tenant-retire'"
                )
            ).one()
            state_row = conn.execute(
                text(
                    "SELECT objective, session_id, session_epoch, checkpoint_seq "
                    "FROM working_states WHERE tenant_id = 'tenant-retire'"
                )
            ).one()
            # A tenant that never touched typed retain or governed models
            # must come up with an empty, but PRESENT, v0.4.0 control plane
            # -- not a table that fails to exist, and not phantom rows.
            new_table_counts = {
                table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in (
                    "retained_records",
                    "retained_record_revisions",
                    "curation_operations",
                    "bank_currentness",
                    "mental_model_registrations",
                    "mental_model_mutations",
                )
            }
    finally:
        engine.dispose()

    assert remaining_tables == set()
    assert tuple(session_row) == ("s1", workspace_id)
    assert tuple(state_row) == ("pre-retirement objective", "s1", 0, 0)
    assert new_table_counts == {table: 0 for table in new_table_counts}
