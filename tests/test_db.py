def test_ensure_tenant_survives_a_concurrent_first_insert(session, monkeypatch):
    """Two provisioning calls racing on a fresh tenant: the loser used to get
    an uncaught IntegrityError and a bare 500 (review finding I9). Every other
    uniqueness race in this codebase is handled with a savepoint.

    Reproduced deterministically, the same pattern test_projects.py's
    `_force_race` uses: the winner's row is already committed to the session
    when the loser's INSERT runs, but the loser's own pre-check (`db.get`) is
    forced to miss -- exactly as it would if it ran before the winner's row
    existed.
    """
    from memory.db import ensure_tenant
    from memory.models import Tenant

    session.add(Tenant(id="fresh-tenant"))
    session.flush()
    monkeypatch.setattr(session, "get", lambda *a, **k: None)

    ensure_tenant(session, "fresh-tenant")  # must not raise IntegrityError

    assert session.query(Tenant).filter_by(id="fresh-tenant").count() == 1
