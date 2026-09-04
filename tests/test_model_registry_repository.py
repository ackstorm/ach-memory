import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from memory import ids
from memory.errors import ModelQuotaExceeded
from memory.model_registry import (
    list_registered_models,
    ready_model,
    register_model,
    withhold_model,
)
from memory.models import MentalModelRegistration, Tenant, User
from memory.retained_records import LogicalBankRef

BUILTIN = {
    "source_query": "Summarize durable user context.",
    "source_tags": ["schema:ach-retain-v1", "validity:indefinite"],
    "tags_match": "all",
    "max_tokens": 512,
    "trigger": {
        "mode": "delta",
        "refresh_after_consolidation": True,
        "min_refresh_interval_seconds": 300,
    },
    "builtin_key": "user-context",
    "definition_version": 1,
    "always_in_context": True,
    "delivery_state": "ready",
}

CUSTOM = {
    "source_query": "Summarize review conventions.",
    "source_tags": ["schema:ach-retain-v1", "validity:indefinite"],
    "tags_match": "all",
    "max_tokens": 256,
    "trigger": {"mode": "manual"},
    "always_in_context": False,
    "delivery_state": "ready",
}


@pytest.fixture
def model_bank(session, tenant):
    user = User(id="usr_models", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return LogicalBankRef(tenant, "user", user.id, None, user.bank_id)


def test_builtin_does_not_consume_five_custom_slots(session, model_bank):
    register_model(
        session,
        model_bank,
        origin="builtin",
        model_key="user-context",
        name="User context",
        **BUILTIN,
    )
    for index in range(5):
        register_model(
            session,
            model_bank,
            origin="user",
            model_key=f"mm_{index:032x}",
            name=f"m{index}",
            **CUSTOM,
        )

    with pytest.raises(ModelQuotaExceeded):
        register_model(
            session,
            model_bank,
            origin="user",
            model_key=f"mm_{99:032x}",
            name="overflow",
            **CUSTOM,
        )

    assert [row.model_key for row in list_registered_models(session, model_bank)] == [
        "user-context",
        *(f"mm_{index:032x}" for index in range(5)),
    ]


def test_a_bank_has_at_most_one_builtin(session, model_bank):
    register_model(
        session,
        model_bank,
        origin="builtin",
        model_key="user-context",
        name="User context",
        **BUILTIN,
    )

    with pytest.raises(ModelQuotaExceeded):
        register_model(
            session,
            model_bank,
            origin="builtin",
            model_key="another-builtin",
            name="Another",
            **{**BUILTIN, "builtin_key": "another-builtin"},
        )


def test_model_delivery_requires_the_exact_refresh_operation(session, model_bank):
    model = register_model(
        session,
        model_bank,
        origin="user",
        model_key=f"mm_{uuid4().hex}",
        name="Review context",
        **CUSTOM,
    )

    withheld = withhold_model(session, model_bank, model.model_key, "refresh-new")
    assert withheld.delivery_state == "withheld"
    assert withheld.refresh_status == "pending"

    stale = ready_model(session, model_bank, model.model_key, "refresh-old")
    assert stale.delivery_state == "withheld"
    assert stale.refresh_status == "pending"

    ready = ready_model(session, model_bank, model.model_key, "refresh-new")

    assert ready.delivery_state == "ready"
    assert ready.refresh_status == "succeeded"
    assert ready.last_refreshed_at is not None


def _wait_for_lock(engine, application_name: str) -> str:
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
            if row is not None and row.wait_event_type == "Lock" and "FROM users" in row.query:
                return row.query
            time.sleep(0.02)
    raise AssertionError(f"connection did not wait on a lock; last state={last_state!r}")


def test_concurrent_custom_registration_cannot_exceed_five(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = f"model-race-{uuid4().hex[:12]}"
    user_id = f"usr_{uuid4().hex[:12]}"
    bank_id = ids.new_user_bank_id()
    bank = LogicalBankRef(tenant_id, "user", user_id, None, bank_id)
    with factory.begin() as db:
        db.add(Tenant(id=tenant_id))
        db.add(User(id=user_id, tenant_id=tenant_id, bank_id=bank_id))
        db.flush()
        for index in range(4):
            register_model(
                db,
                bank,
                origin="user",
                model_key=f"mm_{index:032x}",
                name=f"m{index}",
                **CUSTOM,
            )

    winner_ready = Event()
    release_winner = Event()
    loser_application = f"model-race-loser-{uuid4().hex[:8]}"

    def _winner():
        with factory() as db:
            register_model(
                db,
                bank,
                origin="user",
                model_key=f"mm_{4:032x}",
                name="winner",
                **CUSTOM,
            )
            winner_ready.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("winner was not released")
            db.commit()

    def _loser():
        if not winner_ready.wait(timeout=10):
            raise AssertionError("winner did not register")
        with factory() as db:
            db.execute(
                text("SELECT set_config('application_name', :name, true)"),
                {"name": loser_application},
            )
            try:
                register_model(
                    db,
                    bank,
                    origin="user",
                    model_key=f"mm_{5:032x}",
                    name="loser",
                    **CUSTOM,
                )
            except ModelQuotaExceeded:
                db.rollback()
                return "quota"
            db.commit()
            return "created"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(_winner)
            assert winner_ready.wait(timeout=10)
            loser = pool.submit(_loser)
            try:
                waiting_query = _wait_for_lock(engine, loser_application)
            finally:
                release_winner.set()
            winner.result(timeout=10)
            assert loser.result(timeout=10) == "quota"

        assert "FOR UPDATE" in waiting_query
        with factory() as db:
            assert (
                db.query(MentalModelRegistration)
                .filter_by(tenant_id=tenant_id, origin="user")
                .count()
                == 5
            )
    finally:
        release_winner.set()
        with factory.begin() as db:
            db.query(MentalModelRegistration).filter_by(tenant_id=tenant_id).delete()
            db.query(User).filter_by(id=user_id).delete()
            db.query(Tenant).filter_by(id=tenant_id).delete()
