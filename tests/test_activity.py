"""The recorder, tested directly. Its wiring into the two surfaces is
covered by tests/test_activity_api.py."""

from datetime import UTC, datetime, timedelta

from memory import activity, ids
from memory.models import ActivityEvent


def test_fingerprint_is_stable_and_hides_the_bank_id():
    bank = "project_ba378411-348d-4eb2-9c74-ef0c9da982cc"

    fp = activity.fingerprint(bank)

    assert fp == activity.fingerprint(bank)
    assert len(fp) == 12
    assert bank not in fp
    assert fp not in bank


def test_finish_without_an_action_writes_nothing(app, session):
    """A request that never reached _resolve_bank -- a rejected credential,
    a scrape of /metrics -- has no bank to report. Recording those would also
    hand an unauthenticated caller an unbounded INSERT on a public ingress."""
    activity.new_call()

    activity.finish("rest")

    assert session.query(ActivityEvent).count() == 0


def test_finish_writes_one_row(app, session, tenant):
    activity.new_call()
    activity.describe(
        action="memory.retain",
        scope="user",
        tenant_id=tenant,
        credential_id="key_1",
        user_id="usr_1",
        bank_fingerprint="a" * 12,
        content_bytes=41,
    )

    activity.finish("mcp")

    row = session.query(ActivityEvent).one()
    assert (row.action, row.surface, row.outcome) == ("memory.retain", "mcp", "ok")
    assert row.content_bytes == 41
    assert row.duration_ms >= 0


def test_an_error_code_makes_the_outcome_an_error(app, session, tenant):
    activity.new_call()
    activity.describe(
        action="memory.recall", scope="user", tenant_id=tenant,
        bank_fingerprint="b" * 12,
    )
    activity.set_error("HINDSIGHT_ERROR")

    activity.finish("rest")

    row = session.query(ActivityEvent).one()
    assert (row.outcome, row.error_code) == ("error", "HINDSIGHT_ERROR")


def test_an_over_long_agent_is_truncated_not_rejected(app, session, tenant):
    """agent is client-supplied and the column is String(64). Unscreened, an
    over-long value is a psycopg DataError inside the finalizer -- telemetry
    turning a served request into a 500 is the one thing it may never do."""
    activity.new_call()
    activity.describe(
        action="memory.retain", scope="user", tenant_id=tenant,
        bank_fingerprint="c" * 12, agent="x" * 500,
    )

    activity.finish("mcp")

    assert len(session.query(ActivityEvent).one().agent) == 64


def test_prune_drops_rows_past_the_horizon(app, session, tenant, monkeypatch):
    old = ActivityEvent(
        id=ids.new_activity_id(), tenant_id=tenant, action="memory.recall",
        surface="rest", scope="user", bank_fingerprint="d" * 12,
        outcome="ok", duration_ms=1,
        created_at=datetime.now(UTC) - timedelta(days=90),
    )
    session.add(old)
    session.flush()
    monkeypatch.setattr(activity, "_last_prune", 0.0)

    activity.new_call()
    activity.describe(
        action="memory.recall", scope="user", tenant_id=tenant,
        bank_fingerprint="e" * 12,
    )
    activity.finish("rest")

    remaining = session.query(ActivityEvent).all()
    assert len(remaining) == 1
    assert remaining[0].bank_fingerprint == "e" * 12
