"""Kubernetes probe endpoints.

Both are unauthenticated by design -- a kubelet carries no bearer token --
so the first thing each test asserts is that no credential was needed.
"""

import pytest


def test_health_is_unauthenticated_and_touches_no_dependency(client, monkeypatch):
    """Liveness must not depend on the database. A liveness probe wired to a
    dependency restarts every replica during a database blip, which converts
    a recoverable outage into a crash loop."""
    from memory import db

    def explode():
        raise AssertionError("liveness must not reach the database")

    monkeypatch.setattr(db, "get_engine", explode)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ready_when_the_database_answers(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_reports_503_when_the_database_is_unreachable(client, monkeypatch):
    from memory import db

    def unreachable():
        raise OSError("connection refused")

    monkeypatch.setattr(db, "get_engine", unreachable)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_probe_bodies_never_carry_failure_detail(client, monkeypatch, path):
    """A probe endpoint is reachable by anything that can open the port. The
    reason for a failure belongs in the log, not in a body a scanner reads."""
    from memory import db

    def unreachable():
        raise OSError("could not connect to postgresql://memory:hunter2@db:5432/memory")

    monkeypatch.setattr(db, "get_engine", unreachable)

    body = client.get(path).text

    assert "postgresql://" not in body
    assert "hunter2" not in body
    assert "connection" not in body.lower()


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_probes_are_absent_from_the_openapi_schema(client, path):
    """include_in_schema=False: operational routes are not part of the API
    contract and must not appear to consumers as if they were."""
    assert path not in client.get("/openapi.json").json()["paths"]
