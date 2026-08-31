"""Focused regressions for the seven Phase 2 review findings (Task 0 of the
Phase 3 capture-quality plan). Findings with a natural existing home live
there instead:

- #1 (Full payload floor)            -> tests/test_working_state.py
- #2 (MCP schema bounds)              -> tests/test_mcp_surface_honesty.py
- #3 (workspace fullmatch, REST side) -> tests/test_working_state_api.py
- #5 (On-Behalf-Of Working State)     -> tests/test_brief.py
- #6 (retired slug, REST side)        -> tests/test_working_state_api.py
- #7 (proxy locator pairing)          -> tests/test_mcp_proxy.py

This file covers #4 (migration downgrade collapse), and the MCP-side halves
of #3 and #6, which have no other natural home.
"""

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL, _ensure_database_exists

WS = "ws_" + "a" * 32

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Finding #3 (MCP side): session_id had no bound at all on the MCP surface
# either, since both tools validate through the same WorkingStateWrite /
# StartSessionRequest models as REST.
# ---------------------------------------------------------------------------


def test_mcp_rejects_a_blank_session_id_before_touching_the_database(
    client, master_headers
):
    from memory.mcp.tools import REGISTRY, MCPToolError

    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    secret = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    client.post(
        "/v1/projects",
        json={"project_slug": "acme-api"},
        headers={"Authorization": f"Bearer {secret}"},
    )

    class Ctx:
        headers: ClassVar[dict[str, str]] = {"Authorization": f"Bearer {secret}"}

    with pytest.raises(MCPToolError) as excinfo:
        REGISTRY["start_working_session"](
            project_slug="acme-api", workspace_id=WS, session_id="   ", ctx=Ctx()
        )

    assert excinfo.value.code == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# Finding #6 (MCP side): both working-state tools echoed the request's
# project_slug verbatim instead of the resolved project's current slug.
# ---------------------------------------------------------------------------


def test_mcp_retired_slug_returns_the_current_slug_and_rename_metadata(
    client, master_headers
):
    from memory.mcp.tools import REGISTRY

    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    secret = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    headers = {"Authorization": f"Bearer {secret}"}
    client.post("/v1/projects", json={"project_slug": "acme-api"}, headers=headers)
    rename = client.patch(
        "/v1/projects/acme-api", json={"project_slug": "acme-api-v2"}, headers=headers
    )
    assert rename.status_code == 200, rename.text

    class Ctx:
        headers: ClassVar[dict[str, str]] = {"Authorization": f"Bearer {secret}"}

    result = REGISTRY["start_working_session"](
        project_slug="acme-api", workspace_id=WS, session_id="sess-1", ctx=Ctx()
    )

    assert result.project_slug == "acme-api-v2"
    assert result.resolved_from == "acme-api"
    assert result.notice == "PROJECT_RENAMED"
    assert result.result["project_slug"] == "acme-api-v2"


# ---------------------------------------------------------------------------
# Finding #4: the downgrade recreates the old three-column primary key on
# context_revisions, which collides once workspace revisions have multiplied
# a (tenant, user, project_slug) tuple into several rows.
# ---------------------------------------------------------------------------


@pytest.fixture
def migration_database_url():
    """A throwaway database, separate from the pytest suite's own
    `memory_test` (which never runs alembic -- its schema comes from
    `Base.metadata.create_all`). Dropped again in the finally block.

    `render_as_string(hide_password=False)`, never `str(url)`: URL.__str__
    deliberately masks the password as "***" for safe printing, and that
    literal string is not a usable credential.
    """
    target = make_url(TEST_DATABASE_URL).set(database="memory_migration_test")
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
    """migrations/env.py calls `logging.config.fileConfig()`, whose default
    `disable_existing_loggers=True` silently disables every logger that
    already exists -- including ones pytest's `caplog` depends on elsewhere
    in this same session (memory.mcp, for one). Snapshot and restore each
    logger's `disabled` flag around an alembic command call."""
    manager = logging.Logger.manager
    before = {
        name: lg.disabled
        for name, lg in manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    try:
        yield
    finally:
        for name, lg in manager.loggerDict.items():
            if isinstance(lg, logging.Logger):
                lg.disabled = before.get(name, lg.disabled)


def test_downgrade_collapses_multiple_workspace_revisions_deterministically(
    migration_database_url, monkeypatch
):
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv("MEMORY_DATABASE_URL", migration_database_url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))

    with _logging_state_preserved():
        command.upgrade(cfg, "c3d4e5f6a7b8")

    engine = create_engine(migration_database_url)
    try:
        with engine.begin() as conn:
            # Tuple A has a legacy (workspace_id="") row plus one workspace
            # revision: the legacy row must be the one that survives.
            conn.execute(
                text(
                    "INSERT INTO context_revisions "
                    "(tenant_id, user_id, project_slug, workspace_id, revision, "
                    "fingerprint, updated_at) VALUES "
                    "('t1', 'u1', 'proj-a', '', 1, :fp, now()), "
                    "('t1', 'u1', 'proj-a', :ws_a, 99, :fp, now())"
                ),
                {"fp": "f" * 64, "ws_a": "ws_" + "a" * 32},
            )
            # Tuple B has no legacy row, only two workspace revisions at
            # different times: the newest must survive.
            conn.execute(
                text(
                    "INSERT INTO context_revisions "
                    "(tenant_id, user_id, project_slug, workspace_id, revision, "
                    "fingerprint, updated_at) VALUES "
                    "('t1', 'u1', 'proj-b', :ws_b, 5, :fp, now() - interval '1 hour'), "
                    "('t1', 'u1', 'proj-b', :ws_c, 7, :fp, now())"
                ),
                {"fp": "f" * 64, "ws_b": "ws_" + "b" * 32, "ws_c": "ws_" + "c" * 32},
            )
    finally:
        engine.dispose()

    with _logging_state_preserved():
        command.downgrade(cfg, "b2c3d4e5f6a7")

    engine = create_engine(migration_database_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT project_slug, revision FROM context_revisions "
                    "WHERE tenant_id = 't1' AND user_id = 'u1' "
                    "ORDER BY project_slug"
                )
            ).all()
    finally:
        engine.dispose()

    assert [tuple(row) for row in rows] == [("proj-a", 1), ("proj-b", 7)]

    # Must be able to re-upgrade cleanly afterward -- this is the sequence a
    # real rollback-then-fix-forward performs.
    with _logging_state_preserved():
        command.upgrade(cfg, "c3d4e5f6a7b8")
