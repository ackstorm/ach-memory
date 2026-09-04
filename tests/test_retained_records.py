import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from memory import ids
from memory.auth.principal import Principal
from memory.errors import IdempotencyConflict
from memory.models import RetainedRecord, Tenant, User
from memory.retained_records import LogicalBankRef, accept_retain, get_by_operation
from memory.v040_contracts import RetainEvidence, TypedRetainRequest


def _request(*, operation_id=None, content="claim") -> TypedRetainRequest:
    return TypedRetainRequest(
        scope="user",
        content=content,
        memory_type="fact",
        basis="human_explicit",
        trigger="user_requested",
        evidence=[RetainEvidence(kind="user_quote", raw="quote")],
        operation_id=operation_id or uuid4(),
    )


@pytest.fixture
def retained_bank(session, tenant):
    user = User(id="usr_retained", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    principal = Principal(
        tenant_id=tenant,
        user_id=user.id,
        is_master=False,
        key_id="key_retained",
        credential_id="key_retained",
    )
    bank = LogicalBankRef(
        tenant_id=tenant,
        scope="user",
        user_id=user.id,
        project_internal_id=None,
        bank_id=user.bank_id,
    )
    return principal, bank


def test_accept_retain_is_idempotent_and_rejects_payload_conflict(session, retained_bank):
    principal, bank = retained_bank
    request = _request()
    evidence = [{"kind": "user_quote", "raw": "quote", "source_ref": None}]

    first, created = accept_retain(
        session,
        principal,
        request,
        bank=bank,
        canonical_content="claim",
        sanitized_evidence=evidence,
    )
    again, repeated = accept_retain(
        session,
        principal,
        request,
        bank=bank,
        canonical_content="claim",
        sanitized_evidence=evidence,
    )

    assert created is True and repeated is False and again.id == first.id
    assert first.document_id == f"ach-retain-{request.operation_id.hex}"
    assert first.created_by_credential == principal.credential_id
    assert get_by_operation(session, bank, str(request.operation_id)).id == first.id

    with pytest.raises(IdempotencyConflict):
        accept_retain(
            session,
            principal,
            request.model_copy(update={"content": "different"}),
            bank=bank,
            canonical_content="different",
            sanitized_evidence=evidence,
        )


def test_payload_hash_excludes_transport_identity_and_server_time(session, retained_bank):
    principal, bank = retained_bank
    first, _ = accept_retain(
        session,
        principal,
        _request(),
        bank=bank,
        canonical_content="claim",
        sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}],
    )
    second, _ = accept_retain(
        session,
        principal,
        _request(),
        bank=bank,
        canonical_content="claim",
        sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}],
    )

    assert first.id != second.id
    assert first.operation_id != second.operation_id
    assert first.document_id != second.document_id
    assert first.payload_hash == second.payload_hash
    assert len(first.payload_hash) == 64


def test_payload_hash_canonicalizes_equivalent_expiry_offsets(session, retained_bank):
    principal, bank = retained_bank
    instant = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    offset = datetime.fromisoformat("2027-01-02T04:04:05+01:00")

    first, _ = accept_retain(
        session,
        principal,
        _request().model_copy(update={"valid_until": instant}),
        bank=bank,
        canonical_content="claim",
        sanitized_evidence=[{"raw": "quote", "kind": "user_quote"}],
    )
    second, _ = accept_retain(
        session,
        principal,
        _request().model_copy(update={"valid_until": offset}),
        bank=bank,
        canonical_content="claim",
        sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}],
    )

    assert first.payload_hash == second.payload_hash


def _wait_for_lock(engine, application_name: str, query_fragment: str) -> str:
    deadline = time.monotonic() + 5
    last_state = None
    with engine.connect() as observer:
        while time.monotonic() < deadline:
            observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            row = observer.execute(
                text(
                    "SELECT state, wait_event_type, query FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND application_name = :application_name"
                ),
                {"application_name": application_name},
            ).one_or_none()
            last_state = tuple(row) if row is not None else None
            if row is not None and row.wait_event_type == "Lock" and query_fragment in row.query:
                return row.query
            time.sleep(0.02)
    raise AssertionError(f"connection did not wait on a lock; last state={last_state!r}")


def test_concurrent_exact_retry_creates_one_record(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = f"retain-race-{uuid4().hex[:12]}"
    user_id = f"usr_{uuid4().hex[:12]}"
    bank_id = ids.new_user_bank_id()
    with factory.begin() as db:
        db.add(Tenant(id=tenant_id))
        db.add(User(id=user_id, tenant_id=tenant_id, bank_id=bank_id))

    principal = Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        is_master=False,
        key_id="key_race",
        credential_id="key_race",
    )
    bank = LogicalBankRef(tenant_id, "user", user_id, None, bank_id)
    request = _request()
    winner_ready = Event()
    release_winner = Event()
    loser_application = f"retain-race-loser-{uuid4().hex[:8]}"

    def _winner():
        with factory() as db:
            row, created = accept_retain(
                db,
                principal,
                request,
                bank=bank,
                canonical_content="claim",
                sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}],
            )
            winner_ready.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("winner was not released")
            db.commit()
            return row.id, created

    def _loser():
        if not winner_ready.wait(timeout=10):
            raise AssertionError("winner did not insert")
        with factory() as db:
            db.execute(
                text("SELECT set_config('application_name', :name, true)"),
                {"name": loser_application},
            )
            row, created = accept_retain(
                db,
                principal,
                request,
                bank=bank,
                canonical_content="claim",
                sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}],
            )
            db.commit()
            return row.id, created

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(_winner)
            assert winner_ready.wait(timeout=10)
            loser = pool.submit(_loser)
            try:
                waiting_query = _wait_for_lock(engine, loser_application, "FROM users")
            finally:
                release_winner.set()
            winner_result = winner.result(timeout=10)
            loser_result = loser.result(timeout=10)

        assert "FOR UPDATE" in waiting_query
        assert winner_result[0] == loser_result[0]
        assert {winner_result[1], loser_result[1]} == {True, False}
        with factory() as db:
            assert db.query(RetainedRecord).filter_by(tenant_id=tenant_id).count() == 1
    finally:
        release_winner.set()
        with factory.begin() as db:
            db.query(RetainedRecord).filter_by(tenant_id=tenant_id).delete()
            db.query(User).filter_by(id=user_id).delete()
            db.query(Tenant).filter_by(id=tenant_id).delete()
