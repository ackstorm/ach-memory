from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from memory import currentness, ids, model_registry
from memory.errors import MentalModelNotFound
from memory.hindsight.client import HindsightClient
from memory.mental_model_service import observe_model_refresh, repair_one_model
from memory.models import User
from memory.retained_records import LogicalBankRef

REQUIRED_TAGS = ("schema:ach-retain-v1", "validity:indefinite")
TRIGGER = {"mode": "manual"}


@pytest.fixture
def bank(session, tenant):
    user = User(id="usr_refresh", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return LogicalBankRef(tenant, "user", user.id, None, user.bank_id)


@pytest.fixture
def hindsight():
    return mock.MagicMock(spec=HindsightClient)


def _register(session, bank, *, model_key, refresh_operation_id, **overrides):
    fields = {
        "origin": "user",
        "name": "m",
        "source_query": "q",
        "source_tags": list(REQUIRED_TAGS),
        "tags_match": "all",
        "max_tokens": 256,
        "trigger": TRIGGER,
        "delivery_state": "withheld",
        "refresh_status": "pending",
        "upstream_model_id": f"mm-upstream-{model_key}",
    }
    fields.update(overrides)
    return model_registry.register_model(
        session, bank, model_key=model_key, refresh_operation_id=refresh_operation_id, **fields
    )


@pytest.fixture
def withheld_model(session, bank):
    row = _register(session, bank, model_key=f"mm_{'0' * 32}", refresh_operation_id="op-real")
    session.commit()
    return row


# ---------------------------------------------------------------------------
# observe_model_refresh: exact operation-identity completion
# ---------------------------------------------------------------------------


def test_only_recorded_refresh_operation_can_make_model_ready(session, bank, withheld_model, hindsight):
    hindsight.get_operation.return_value = {"operation_id": "different", "status": "completed"}

    result = observe_model_refresh(session, bank, withheld_model.model_key, client=hindsight)

    assert result.delivery_state == "withheld"


def test_matching_completed_operation_makes_the_model_ready(session, bank, withheld_model, hindsight):
    hindsight.get_operation.return_value = {"operation_id": "op-real", "status": "completed"}

    result = observe_model_refresh(session, bank, withheld_model.model_key, client=hindsight)

    assert result.delivery_state == "ready"
    assert result.refresh_status == "succeeded"


def test_a_pending_operation_leaves_the_model_withheld(session, bank, withheld_model, hindsight):
    hindsight.get_operation.return_value = {"operation_id": "op-real", "status": "pending"}

    result = observe_model_refresh(session, bank, withheld_model.model_key, client=hindsight)

    assert result.delivery_state == "withheld"
    assert result.refresh_status == "pending"


def test_a_processing_operation_leaves_the_model_withheld_not_failed(session, bank, withheld_model, hindsight):
    """Hindsight 0.9.2 measured live: an in-progress refresh reports
    "processing" (not "pending" the whole time, and never "running") --
    the terminal-status whitelist must not mistake it for a failure."""
    hindsight.get_operation.return_value = {"operation_id": "op-real", "status": "processing"}

    result = observe_model_refresh(session, bank, withheld_model.model_key, client=hindsight)

    assert result.delivery_state == "withheld"
    assert result.refresh_status == "pending"


def test_a_failed_operation_sets_refresh_status_failed_with_a_backoff(session, bank, withheld_model, hindsight):
    hindsight.get_operation.return_value = {"operation_id": "op-real", "status": "failed"}

    result = observe_model_refresh(session, bank, withheld_model.model_key, client=hindsight)

    assert result.delivery_state == "withheld"
    assert result.refresh_status == "failed"
    row = model_registry.get_registered_model(session, bank, withheld_model.model_key)
    assert row.repair_not_before is not None


def test_observe_a_ready_model_does_not_touch_hindsight(session, bank, hindsight):
    row = _register(
        session, bank, model_key=f"mm_{'9' * 32}", refresh_operation_id=None,
        delivery_state="ready", refresh_status="succeeded",
    )
    session.commit()

    result = observe_model_refresh(session, bank, row.model_key, client=hindsight)

    assert result.delivery_state == "ready"
    hindsight.get_operation.assert_not_called()


def test_observe_an_unregistered_key_raises(session, bank, hindsight):
    with pytest.raises(MentalModelNotFound):
        observe_model_refresh(session, bank, "mm_missing", client=hindsight)


# ---------------------------------------------------------------------------
# repair_one_model: at-most-one bounded repair, oldest failed first
# ---------------------------------------------------------------------------


@pytest.fixture
def failed_models(session, bank):
    now = datetime.now(UTC)
    rows = [
        _register(
            session, bank, model_key=f"mm_{str(index) * 32}", refresh_operation_id=f"op-{index}",
            refresh_status="failed", repair_not_before=now - timedelta(seconds=30),
        )
        for index in (1, 2)
    ]
    rows[0].updated_at = now - timedelta(seconds=120)
    rows[1].updated_at = now - timedelta(seconds=60)
    session.flush()
    session.commit()
    return rows


def test_failed_refresh_repair_is_one_action_after_backoff(session, bank, failed_models, hindsight):
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-repair-1"}
    oldest = min(failed_models, key=lambda row: row.updated_at)

    result = repair_one_model(session, bank, now=datetime.now(UTC), client=hindsight)

    assert result.model_key == oldest.model_key
    assert hindsight.refresh_mental_model.call_count == 1
    assert result.delivery_state == "withheld"
    assert result.refresh_status == "pending"


def test_repair_ignores_a_model_still_inside_its_backoff(session, bank, hindsight):
    now = datetime.now(UTC)
    _register(
        session, bank, model_key=f"mm_{'3' * 32}", refresh_operation_id="op-3",
        refresh_status="failed", repair_not_before=now + timedelta(seconds=30),
    )
    session.commit()

    result = repair_one_model(session, bank, now=now, client=hindsight)

    assert result is None
    hindsight.refresh_mental_model.assert_not_called()


def test_repair_also_finds_a_required_model_with_no_operation_id(session, bank, hindsight):
    """`require_model_refresh` withholds with `refresh_status='required'` and
    no invented operation id (e.g. a submission was never attempted or was
    lost) -- the repair selector must pick this up exactly like a failed one,
    even though `refresh_operation_id IS NULL`."""
    now = datetime.now(UTC)
    row = model_registry.register_model(
        session, bank, origin="user", model_key=f"mm_{'7' * 32}", name="m",
        source_query="q", source_tags=list(REQUIRED_TAGS), tags_match="all",
        max_tokens=256, trigger=TRIGGER,
        upstream_model_id="mm-upstream-required",
    )
    model_registry.require_model_refresh(session, bank, row.model_key, repair_not_before=now)
    session.commit()
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-required-repair"}

    result = repair_one_model(session, bank, now=now, client=hindsight)

    assert result.model_key == row.model_key
    assert result.refresh_status == "pending"
    hindsight.refresh_mental_model.assert_called_once_with(bank.bank_id, "mm-upstream-required")


def test_repair_finds_nothing_when_no_model_is_failed(session, bank, hindsight):
    result = repair_one_model(session, bank, now=datetime.now(UTC), client=hindsight)

    assert result is None
    hindsight.refresh_mental_model.assert_not_called()


def test_repair_defers_entirely_while_the_bank_barrier_is_active(session, bank, failed_models, hindsight):
    currentness.withhold_bank(session, bank, "some-safety-operation")
    session.commit()

    result = repair_one_model(session, bank, now=datetime.now(UTC), client=hindsight)

    assert result is None
    hindsight.refresh_mental_model.assert_not_called()


def test_a_second_repair_failure_resets_the_backoff(session, bank, failed_models, hindsight):
    from memory.errors import HindsightError

    hindsight.refresh_mental_model.side_effect = HindsightError("backend unreachable")
    oldest = min(failed_models, key=lambda row: row.updated_at)

    result = repair_one_model(session, bank, now=datetime.now(UTC), client=hindsight)

    assert result.model_key == oldest.model_key
    assert result.delivery_state == "withheld"
    assert result.refresh_status == "failed"
    row = model_registry.get_registered_model(session, bank, oldest.model_key)
    assert row.repair_not_before > datetime.now(UTC)
