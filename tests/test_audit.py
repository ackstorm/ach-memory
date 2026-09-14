from datetime import UTC, datetime, timedelta

from memory import audit
from memory.auth.principal import Principal
from memory.models import AuditEvent


def _user(user_id: str, on_behalf_of: str | None = None) -> Principal:
    return Principal(user_id=user_id, on_behalf_of=on_behalf_of)


def test_record_carries_actor_and_on_behalf_of(db):
    audit.record(db, principal=_user("usr_juan"), action="memory.retain", resource="mem_1")
    db.flush()

    event = db.query(AuditEvent).one()
    assert event.actor_user_id == "usr_juan"
    assert event.on_behalf_of is None
    assert event.action == "memory.retain"
    assert event.resource == "mem_1"


def test_a_delegated_action_records_the_subject(db):
    audit.record(
        db, principal=_user("usr_op", on_behalf_of="usr_alice"),
        action="memory.retain", resource="mem_1",
    )
    db.flush()

    assert db.query(AuditEvent).one().on_behalf_of == "usr_alice"


def test_record_round_trips_operation_id_and_details(db):
    details = {"scope": "user", "status": "completed"}

    returned = audit.record(
        db, principal=_user("usr_juan"), action="memory.retain", resource="mem_1",
        operation_id="op_1", details=details,
    )
    db.flush()
    db.expire_all()

    event = db.query(AuditEvent).one()
    assert event.id == returned.id
    assert event.operation_id == "op_1"
    assert event.details == details


def test_find_retain_returns_the_latest_matching_row(db):
    # Postgres freezes now() at transaction start, so two rows written here
    # would otherwise tie on created_at; backdate the older one explicitly.
    older = audit.record(
        db, principal=_user("usr_juan"), action="memory.retain", resource="mem_1",
        operation_id="op_1",
    )
    db.flush()
    older.created_at = datetime.now(UTC) - timedelta(seconds=10)
    db.flush()
    latest = audit.record(
        db, principal=_user("usr_juan"), action="memory.retain", resource="mem_1",
        operation_id="op_1",
    )
    db.flush()

    found = audit.find_retain(db, "op_1")

    assert found is not None
    assert found.id == latest.id


def test_find_retain_ignores_other_actions(db):
    audit.record(
        db, principal=_user("usr_juan"), action="memory.forget", resource="mem_1",
        operation_id="op_1",
    )
    db.flush()

    assert audit.find_retain(db, "op_1") is None


def test_find_retain_returns_none_when_absent(db):
    assert audit.find_retain(db, "op_missing") is None
