import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL, _ensure_database_exists

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migration_database_url():
    target = make_url(TEST_DATABASE_URL).set(
        database="memory_project_slug_migration_test"
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
def _logging_state_preserved():
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
    config = Config(str(REPO_ROOT / "alembic.ini"))
    with _logging_state_preserved():
        command.upgrade(config, revision)


def _downgrade(url: str, monkeypatch, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    with _logging_state_preserved():
        command.downgrade(config, revision)


def _with_application_name(url: str, application_name: str) -> str:
    return (
        make_url(url)
        .update_query_dict({"application_name": application_name})
        .render_as_string(hide_password=False)
    )


def _wait_for_lock_query(
    url: str, application_name: str, source_table: str
) -> str:
    """Return the real SQL currently waiting on a PostgreSQL lock."""
    engine = create_engine(url)
    deadline = time.monotonic() + 5
    last_state = None
    try:
        with engine.connect() as conn:
            while time.monotonic() < deadline:
                conn.execute(text("SELECT pg_stat_clear_snapshot()"))
                row = conn.execute(
                    text(
                        "SELECT pid, state, wait_event_type, wait_event, query, "
                        "pg_blocking_pids(pid) AS blocking_pids "
                        "FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND application_name = :application_name"
                    ),
                    {"application_name": application_name},
                ).one_or_none()
                last_state = tuple(row) if row is not None else None
                if (
                    row is not None
                    and row.wait_event_type == "Lock"
                    and source_table in row.query.lower()
                ):
                    return row.query
                time.sleep(0.02)
    finally:
        engine.dispose()
    raise AssertionError(
        f"migration connection did not wait on a lock; last state={last_state!r}"
    )


def _seed_legacy_slug_rows(url: str, *, duplicate: bool = False) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (id) VALUES ('tenant-a')"))
            conn.execute(
                text(
                    "INSERT INTO projects "
                    "(internal_id, tenant_id, project_slug, owner_type, owner_id, "
                    "bank_id, created_at, updated_at) VALUES "
                    "('project-a', 'tenant-a', 'current-name', 'user', 'user-a', "
                    "'bank-a', now(), now()), "
                    "('project-b', 'tenant-a', 'other-project', 'user', 'user-a', "
                    "'bank-b', now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO retired_slugs "
                    "(tenant_id, retired_slug, project_internal_id, retired_at) "
                    "VALUES ('tenant-a', :slug, 'project-a', now())"
                ),
                {"slug": "other-project" if duplicate else "old-name"},
            )
    finally:
        engine.dispose()


def test_upgrade_unifies_live_and_retired_slugs(
    migration_database_url, monkeypatch
):
    _upgrade(migration_database_url, monkeypatch, "d4e5f6a7b8c9")
    _seed_legacy_slug_rows(migration_database_url)

    _upgrade(migration_database_url, monkeypatch, "head")

    engine = create_engine(migration_database_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT slug, project_internal_id, is_canonical "
                    "FROM project_slugs ORDER BY slug"
                )
            ).all()
            canonical_counts = conn.execute(
                text(
                    "SELECT project_internal_id, count(*) "
                    "FROM project_slugs WHERE is_canonical "
                    "GROUP BY project_internal_id ORDER BY project_internal_id"
                )
            ).all()
            legacy_columns = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'projects' "
                    "AND column_name = 'project_slug'"
                )
            ).all()
            retired_table = conn.execute(
                text("SELECT to_regclass('public.retired_slugs')")
            ).scalar_one()
    finally:
        engine.dispose()

    assert [tuple(row) for row in rows] == [
        ("current-name", "project-a", True),
        ("old-name", "project-a", False),
        ("other-project", "project-b", True),
    ]
    assert [tuple(row) for row in canonical_counts] == [
        ("project-a", 1),
        ("project-b", 1),
    ]
    assert legacy_columns == []
    assert retired_table is None


def test_upgrade_aborts_when_a_retired_slug_duplicates_a_live_slug(
    migration_database_url, monkeypatch
):
    _upgrade(migration_database_url, monkeypatch, "d4e5f6a7b8c9")
    _seed_legacy_slug_rows(migration_database_url, duplicate=True)

    with pytest.raises(RuntimeError, match="duplicate tenant project slugs"):
        _upgrade(migration_database_url, monkeypatch, "head")

    engine = create_engine(migration_database_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('public.retired_slugs')")
            ).scalar_one() == "retired_slugs"
            assert conn.execute(
                text("SELECT to_regclass('public.project_slugs')")
            ).scalar_one() is None
    finally:
        engine.dispose()


def test_upgrade_locks_legacy_slug_sources_before_reading_them(
    migration_database_url, monkeypatch
):
    _upgrade(migration_database_url, monkeypatch, "d4e5f6a7b8c9")
    _seed_legacy_slug_rows(migration_database_url)
    blocker_engine = create_engine(migration_database_url)
    application_name = f"slug-upgrade-{uuid4().hex[:12]}"
    migration_url = _with_application_name(migration_database_url, application_name)

    try:
        with blocker_engine.connect() as blocker:
            transaction = blocker.begin()
            deleted = blocker.execute(
                text(
                    "DELETE FROM retired_slugs "
                    "WHERE tenant_id = 'tenant-a' AND retired_slug = 'old-name'"
                )
            )
            assert deleted.rowcount == 1
            with ThreadPoolExecutor(max_workers=1) as pool:
                migration = pool.submit(_upgrade, migration_url, monkeypatch, "head")
                try:
                    waiting_query = _wait_for_lock_query(
                        migration_database_url, application_name, "retired_slugs"
                    )
                finally:
                    transaction.rollback()
                migration.result(timeout=10)
    finally:
        blocker_engine.dispose()

    assert " ".join(waiting_query.split()) == (
        "LOCK TABLE projects, retired_slugs IN SHARE ROW EXCLUSIVE MODE"
    )


def test_downgrade_locks_the_unified_namespace_before_reading_it(
    migration_database_url, monkeypatch
):
    _upgrade(migration_database_url, monkeypatch, "d4e5f6a7b8c9")
    _seed_legacy_slug_rows(migration_database_url)
    _upgrade(migration_database_url, monkeypatch, "head")
    blocker_engine = create_engine(migration_database_url)
    application_name = f"slug-downgrade-{uuid4().hex[:12]}"
    migration_url = _with_application_name(migration_database_url, application_name)

    try:
        with blocker_engine.connect() as blocker:
            transaction = blocker.begin()
            updated = blocker.execute(
                text(
                    "UPDATE project_slugs SET created_at = created_at "
                    "WHERE tenant_id = 'tenant-a' AND slug = 'old-name'"
                )
            )
            assert updated.rowcount == 1
            with ThreadPoolExecutor(max_workers=1) as pool:
                migration = pool.submit(
                    _downgrade, migration_url, monkeypatch, "d4e5f6a7b8c9"
                )
                try:
                    waiting_query = _wait_for_lock_query(
                        migration_database_url, application_name, "project_slugs"
                    )
                finally:
                    transaction.rollback()
                migration.result(timeout=10)
    finally:
        blocker_engine.dispose()

    assert " ".join(waiting_query.split()) == (
        "LOCK TABLE projects, project_slugs IN SHARE ROW EXCLUSIVE MODE"
    )


def test_downgrade_restores_live_and_retired_slug_storage(
    migration_database_url, monkeypatch
):
    _upgrade(migration_database_url, monkeypatch, "d4e5f6a7b8c9")
    _seed_legacy_slug_rows(migration_database_url)
    _upgrade(migration_database_url, monkeypatch, "head")

    _downgrade(migration_database_url, monkeypatch, "d4e5f6a7b8c9")

    engine = create_engine(migration_database_url)
    try:
        with engine.connect() as conn:
            projects = conn.execute(
                text("SELECT internal_id, project_slug FROM projects ORDER BY internal_id")
            ).all()
            aliases = conn.execute(
                text(
                    "SELECT retired_slug, project_internal_id "
                    "FROM retired_slugs ORDER BY retired_slug"
                )
            ).all()
            namespace = conn.execute(
                text("SELECT to_regclass('public.project_slugs')")
            ).scalar_one()
    finally:
        engine.dispose()

    assert [tuple(row) for row in projects] == [
        ("project-a", "current-name"),
        ("project-b", "other-project"),
    ]
    assert [tuple(row) for row in aliases] == [("old-name", "project-a")]
    assert namespace is None
