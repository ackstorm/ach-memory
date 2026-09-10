"""The `tags_match` widening has to be reversible on a database that ran it.

`73e00e0dd20c` widens the column from VARCHAR(8) to String(16) so `all_strict`
fits. Every built-in written since then holds that 10-character value, so a
downgrade that narrows the column without first shortening the data raises
StringDataRightTruncation and leaves the schema mid-migration.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL, _ensure_database_exists
from tests.test_project_slug_migration import _downgrade, _upgrade

REPO_ROOT = Path(__file__).resolve().parents[1]

WIDENED = "73e00e0dd20c"
PREVIOUS = "918c1a7a37de"


@pytest.fixture
def migration_database_url():
    target = make_url(TEST_DATABASE_URL).set(database="memory_tags_match_migration_test")
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


def test_downgrade_shortens_all_strict_before_narrowing_the_column(
    migration_database_url, monkeypatch
):
    _upgrade(migration_database_url, monkeypatch, WIDENED)

    engine = create_engine(migration_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (id) VALUES ('tnt_mig')"))
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, bank_id, created_at) "
                    "VALUES ('usr_mig', 'tnt_mig', 'user_mig', now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO mental_model_registrations ("
                    "  id, tenant_id, scope, user_id, project_internal_id,"
                    "  model_key, origin, name, source_query, source_tags,"
                    "  tags_match, max_tokens, trigger, lifecycle_state,"
                    "  delivery_state, builtin_key, definition_version"
                    ") VALUES ("
                    "  gen_random_uuid(), 'tnt_mig', 'user', 'usr_mig', NULL,"
                    "  'user-context', 'builtin', 'User Context', 'q', '[]',"
                    "  'all_strict', 2048, '{}', 'active', 'ready',"
                    "  'user-context', 3"
                    ")"
                )
            )

        _downgrade(migration_database_url, monkeypatch, PREVIOUS)

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT tags_match FROM mental_model_registrations")
            ).scalar_one() == "all"
    finally:
        engine.dispose()
