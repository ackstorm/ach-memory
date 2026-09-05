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
        "curation_operations",
        "bank_currentness",
        "mental_model_registrations",
        "working_sessions",
        "working_states",
    } <= _tables(upgrade_database_url)


def test_released_v035_data_survives_upgrade_to_v040(
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
    assert head == "a7b8c9d0e1f2"
