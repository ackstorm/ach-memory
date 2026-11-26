from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from memory import models as m


def test_one_row_per_table_round_trips(db):
    db.add(m.User(id="usr_1"))
    db.flush()
    db.add(m.ExternalIdentity(issuer="dex", subject="s_1", user_id="usr_1"))
    db.add(m.Group(id="grp_1", name="team"))
    db.add(m.Project(internal_id="prj_1", access={"users": ["s_1"], "groups": []}, bank_id="proj_1"))
    db.flush()
    db.add(m.ProjectSlug(slug="acme-api", project_internal_id="prj_1"))
    db.add(m.AuditEvent(id="aud_1", action="retain", resource="mem_1", operation_id="op_1"))
    db.flush()

    assert db.get(m.ExternalIdentity, ("dex", "s_1")).user_id == "usr_1"
    assert db.get(m.Group, "grp_1").name == "team"
    assert db.get(m.Project, "prj_1").bank_id == "proj_1"
    assert db.get(m.ProjectSlug, "acme-api").project_internal_id == "prj_1"
    assert db.get(m.AuditEvent, "aud_1").resource == "mem_1"


def test_migrations_produce_the_same_tables_as_the_models(engine, monkeypatch):
    m.Base.metadata.drop_all(engine)
    monkeypatch.setenv("MEMORY_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    inspector = inspect(engine)
    inspected = set(inspector.get_table_names()) - {"alembic_version"}
    assert inspected == set(m.Base.metadata.tables)
    # Table names alone would not catch a column a migration forgot to add or drop.
    for name, table in m.Base.metadata.tables.items():
        assert {c["name"] for c in inspector.get_columns(name)} == set(table.columns.keys()), name


def _fresh(engine, monkeypatch) -> Config:
    """An empty database at no revision, and the alembic config that targets it."""
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS read_daily"))
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    m.Base.metadata.drop_all(engine)
    monkeypatch.setenv("MEMORY_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    return Config(Path(__file__).resolve().parents[1] / "alembic.ini")


def test_the_access_migration_turns_owners_into_the_list(engine, monkeypatch):
    config = _fresh(engine, monkeypatch)
    command.upgrade(config, "0007")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, created_at) VALUES ('usr_1', now())"))
        conn.execute(text(
            "INSERT INTO external_identities (issuer, subject, user_id, created_at) VALUES "
            "('dex', 'Juan@Acme.test', 'usr_1', now()), ('jwt', 'juan@acme.test', 'usr_1', now())"))
        conn.execute(text(
            "INSERT INTO projects (internal_id, owner_type, owner_id, bank_id, created_at, updated_at) VALUES "
            "('prj_u', 'user', 'usr_1', 'b_u', now(), now()), ('prj_g', 'group', 'dream', 'b_g', now(), now())"))

    command.upgrade(config, "head")

    with engine.connect() as conn:
        access = dict(conn.execute(text("SELECT internal_id, access FROM projects")).all())
    assert access == {"prj_u": {"users": ["juan@acme.test"], "groups": []},
                      "prj_g": {"users": [], "groups": ["dream"]}}


def test_the_access_migration_refuses_an_owner_with_no_identity(engine, monkeypatch):
    config = _fresh(engine, monkeypatch)
    command.upgrade(config, "0007")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, created_at) VALUES ('usr_ghost', now())"))
        conn.execute(text(
            "INSERT INTO projects (internal_id, owner_type, owner_id, bank_id, created_at, updated_at) VALUES "
            "('prj_x', 'user', 'usr_ghost', 'b_x', now(), now())"))

    with pytest.raises(Exception, match="prj_x"):
        command.upgrade(config, "head")

    command.downgrade(config, "base")
    command.upgrade(config, "head")
