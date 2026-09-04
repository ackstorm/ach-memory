import logging
from contextlib import contextmanager
from pathlib import Path

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
