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


def test_bound_parameters_never_reach_an_exception_string(configured_env):
    """A DataError's str() renders [parameters: {...}] unless the engine is
    built with hide_parameters=True -- and api/app.py logs the whole traceback
    of any unhandled exception. bank_id and internal_id are bound parameters
    on the projects INSERT, so without this the invariant "bank_id never
    crosses the boundary" (inv. 29) holds for responses and fails for logs.

    Reproduced live against the dev database before this test was written:
    hide_parameters=False -> the value appears in str(exc); True -> it does not.

    `get_engine()` reads settings directly and isn't otherwise exercised by
    this suite (every other test bypasses it via a patched
    `_session_factory`), so it needs the `configured_env` fixture -- the same
    minimum MEMORY_DATABASE_URL/MASTER_KEY_HASH/HINDSIGHT_URL config the
    `app` fixture supplies -- to build at all.
    """
    from sqlalchemy import text

    from memory.db import get_engine

    get_engine.cache_clear()
    engine = get_engine()
    assert engine.hide_parameters is True, (
        "create_engine must set hide_parameters=True"
    )

    # And prove it end to end, not just via the flag. A cast-to-int failure
    # was tried first and rejected: psycopg's own driver error ("invalid
    # input syntax for type integer: ...") echoes the value itself,
    # independent of hide_parameters, so it can't distinguish the two
    # states. A NUL byte raises sqlalchemy.exc.DataError with a generic
    # driver message instead -- confirmed live against the dev database:
    # hide_parameters=False leaks the value, True does not.
    secret = "project_00000000-0000-0000-0000-00000000dead\x00tail"
    with engine.connect() as conn:
        try:
            conn.execute(text("select :bank_id"), {"bank_id": secret})
        except Exception as exc:  # noqa: BLE001 -- the string is the assertion
            assert secret not in str(exc)
        else:
            raise AssertionError("expected the NUL byte to be rejected")
