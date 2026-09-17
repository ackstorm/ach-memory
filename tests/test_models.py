from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from memory import models as m


def test_one_row_per_table_round_trips(db):
    db.add(m.User(id="usr_1"))
    db.flush()
    db.add(m.ExternalIdentity(issuer="dex", subject="s_1", user_id="usr_1"))
    db.add(m.Group(id="grp_1", name="team"))
    db.add(m.Project(internal_id="prj_1", owner_type="user", owner_id="usr_1", bank_id="proj_1"))
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
