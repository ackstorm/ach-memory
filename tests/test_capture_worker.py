import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
import respx

from memory import brief, ids, profiles, working_state
from memory.auth.principal import Principal
from memory.capture import repository, worker
from memory.errors import HindsightError
from memory.hindsight.client import HindsightClient
from memory.models import CaptureSlice, Project, ProjectSlug, User

BASE = "http://hindsight.test"
WS = "ws_" + "a" * 32


@pytest.fixture
def rig(session, tenant):
    user = User(id="usr_1", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        owner_type="user",
        owner_id="usr_1",
        bank_id=ids.new_project_bank_id(),
    )
    project.slug_rows.append(
        ProjectSlug(tenant_id=tenant, slug="acme-api", is_canonical=True)
    )
    session.add_all([user, project])
    session.flush()
    principal = Principal(
        tenant_id=tenant, user_id="usr_1", is_master=False, key_id="k", credential_id="k"
    )
    client = HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")
    return SimpleNamespace(user=user, project=project, principal=principal, client=client)


def _submit(session, rig, **overrides) -> CaptureSlice:
    content = overrides.pop("content", "user: hello there")
    fields = {
        "host": "claude-code",
        "session_id": "sess-1",
        "project_slug": "acme-api",
        "git_locator": None,
        "workspace_id": WS,
        "start_offset": 0,
        "end_offset": 100,
        "content_hash": "a" * 64,
        "sanitized_hash": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
    }
    fields.update(overrides)
    result = repository.accept_checkpoint(session, rig.principal, **fields)
    session.commit()
    return result.row


def _mock_extract(bank_id: str, *envelopes: dict):
    facts = [{"text": json.dumps(e)} for e in envelopes]
    return respx.post(f"{BASE}/v1/default/banks/{bank_id}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": facts})
    )


def _mock_retain(bank_id: str, operation_id: str):
    return respx.post(f"{BASE}/v1/default/banks/{bank_id}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": operation_id})
    )


def _mock_operation(bank_id: str, operation_id: str, status: str = "completed"):
    return respx.get(f"{BASE}/v1/default/banks/{bank_id}/operations/{operation_id}").mock(
        return_value=httpx.Response(200, json={"operation_id": operation_id, "status": status})
    )


def _hold_lease(session, row) -> str:
    """Put `row` in the state `acquire_lease` would leave it in, and return
    the token.

    Transitions are owner-fenced, so a lease is a precondition for every one
    of them. Stamped directly rather than acquired, so that a test driving
    one specific row is not affected by whatever else is leasable in the
    same session; `run_once`'s own tests below exercise the real
    acquisition, and the fencing regressions live in
    tests/test_phase3_review_closure.py.
    """
    owner = repository.new_lease_owner("test")
    row.lease_owner = owner
    row.lease_until = datetime.now(UTC) + timedelta(seconds=60)
    session.flush()
    return owner


def _process(session, rig, row, **kwargs):
    kwargs.setdefault("max_attempts", 8)
    kwargs.setdefault("lease_seconds", 60)
    kwargs.setdefault("correction_refresh_enabled", False)
    kwargs.setdefault("profile_delivery_mode", "legacy")
    kwargs.setdefault("owner", _hold_lease(session, row))
    worker.process_row(session, rig.client, row, **kwargs)
    session.commit()


PREFERENCE = {
    "record": "candidate",
    "text": "Prefers tabs over spaces.",
    "kind": "preference",
    "origin": "stated",
    "subject": "user",
}

CONVENTION = {
    "record": "candidate",
    "text": "The project uses black for formatting.",
    "kind": "convention",
    "origin": "stated",
    "subject": "project",
}

WORKING_STATE_UPDATE = {
    "record": "working_state",
    "objective": "Ship Phase 3",
    "next_steps": ["Wire the CLI"],
}


# ---------------------------------------------------------------------------
# Crash boundaries: restart the worker at each point, expect exactly one
# evidence set and one effective Working State advancement.
# ---------------------------------------------------------------------------


@respx.mock
def test_crash_after_lease_leaves_the_row_untouched_and_recoverable(session, rig):
    row = _submit(session, rig)
    leased = repository.acquire_lease(session, owner="w1", lease_seconds=60)
    session.commit()
    assert leased and leased[0].id == row.id
    # Simulate a crash: nothing else happens this cycle.

    _mock_extract(rig.project.bank_id, PREFERENCE)
    _mock_retain(rig.user.bank_id, worker.filer.operation_id(str(row.id), "user"))
    _mock_operation(rig.user.bank_id, worker.filer.operation_id(str(row.id), "user"))

    while row.status != "completed":
        _process(session, rig, row)

    assert row.status == "completed"


@respx.mock
def test_crash_after_extraction_persistence_does_not_re_extract(session, rig):
    row = _submit(session, rig)
    extract_route = _mock_extract(rig.project.bank_id, PREFERENCE)
    _process(session, rig, row)  # pending -> retaining, extraction persisted

    assert row.status == "retaining"
    assert extract_route.call_count == 1

    # "Restart": drive the rest. The extraction route above is the only one
    # mocked for dry-run-extract, so a second call to it would raise
    # respx's "already matched" style error if the worker ever re-extracted
    # -- asserting call_count == 1 below is the direct proof either way.
    op_id = worker.filer.operation_id(str(row.id), "user")
    _mock_retain(rig.user.bank_id, op_id)
    _mock_operation(rig.user.bank_id, op_id)

    while row.status != "completed":
        _process(session, rig, row)

    assert extract_route.call_count == 1


@respx.mock
def test_crash_after_operation_id_persistence_reuses_the_same_id(session, rig):
    row = _submit(session, rig)
    _mock_extract(rig.project.bank_id, PREFERENCE)
    _process(session, rig, row)  # -> retaining, extraction saved
    _process(session, rig, row)  # -> retaining, operation id persisted (not yet called)

    assert row.hindsight_operations is not None
    first_op_id = row.hindsight_operations["user"]["operation_id"]
    assert first_op_id == worker.filer.operation_id(str(row.id), "user")

    # "Restart": the next stage actually calls retain -- it must use the
    # already-persisted operation id, not recompute one.
    retain_route = _mock_retain(rig.user.bank_id, first_op_id)
    _process(session, rig, row)

    assert row.hindsight_operations["user"]["operation_id"] == first_op_id
    sent = json.loads(retain_route.calls.last.request.read())
    assert sent["operation_id"] == first_op_id


@respx.mock
def test_crash_after_retain_acknowledgement_does_not_retain_twice(session, rig):
    row = _submit(session, rig)
    _mock_extract(rig.project.bank_id, PREFERENCE)
    op_id = worker.filer.operation_id(str(row.id), "user")
    retain_route = _mock_retain(rig.user.bank_id, op_id)
    _mock_operation(rig.user.bank_id, op_id)

    _process(session, rig, row)  # -> retaining, extraction saved
    _process(session, rig, row)  # -> retaining, operation id persisted
    _process(session, rig, row)  # -> applying, retain acknowledged
    assert row.status == "applying"
    assert retain_route.call_count == 1

    # "Restart": must not retain again.
    _process(session, rig, row)  # -> completed
    assert retain_route.call_count == 1
    assert row.status == "completed"


@respx.mock
def test_crash_after_hindsight_completion_is_recoverable_via_re_poll(session, rig):
    row = _submit(session, rig)
    _mock_extract(rig.project.bank_id, PREFERENCE)
    op_id = worker.filer.operation_id(str(row.id), "user")
    _mock_retain(rig.user.bank_id, op_id)
    operation_route = _mock_operation(rig.user.bank_id, op_id, status="completed")

    _process(session, rig, row)
    _process(session, rig, row)
    _process(session, rig, row)
    assert row.status == "applying"

    # The operation is already "completed" upstream; simulate a crash right
    # after this worker learns that, before it acts on it.
    with mock.patch.object(
        worker.repository, "complete", side_effect=RuntimeError("simulated crash")
    ):
        with pytest.raises(RuntimeError):
            worker.process_row(
                    session,
                    rig.client,
                    row,
                    owner=_hold_lease(session, row),
                    lease_seconds=60,
                    max_attempts=8,
                    correction_refresh_enabled=False,
                    profile_delivery_mode="legacy",
                )
        session.rollback()

    assert working_state.get_current(session, rig.principal, rig.project.internal_id, WS) is None

    # Retry: re-polls (cheap, idempotent) and completes.
    _process(session, rig, row)
    assert row.status == "completed"
    assert operation_route.call_count >= 2


@respx.mock
def test_crash_between_working_state_write_and_completion_mark_is_recoverable(session, rig):
    row = _submit(session, rig, content="assistant: next I'll wire the CLI")
    _mock_extract(rig.project.bank_id, WORKING_STATE_UPDATE)

    _process(session, rig, row)  # -> retaining (extraction: working_state only)
    _process(session, rig, row)  # no candidates -> straight to applying
    assert row.status == "applying"

    with mock.patch.object(
        worker.repository, "complete", side_effect=RuntimeError("simulated crash")
    ):
        with pytest.raises(RuntimeError):
            worker.process_row(
                    session,
                    rig.client,
                    row,
                    owner=_hold_lease(session, row),
                    lease_seconds=60,
                    max_attempts=8,
                    correction_refresh_enabled=False,
                    profile_delivery_mode="legacy",
                )
        session.rollback()

    # Working State write and the completion mark are one transaction (they
    # share this call's db.commit()): a crash before that commit persists
    # neither.
    assert working_state.get_current(session, rig.principal, rig.project.internal_id, WS) is None
    assert row.status == "applying"

    _process(session, rig, row)

    assert row.status == "completed"
    state = working_state.get_current(session, rig.principal, rig.project.internal_id, WS)
    assert state is not None
    assert state.objective == "Ship Phase 3"


# ---------------------------------------------------------------------------
# Working State semantics
# ---------------------------------------------------------------------------


@respx.mock
def test_a_slice_with_no_candidates_can_still_update_working_state(session, rig):
    row = _submit(session, rig, content="assistant: next I'll wire the CLI")
    _mock_extract(rig.project.bank_id, WORKING_STATE_UPDATE)

    while row.status != "completed":
        _process(session, rig, row)

    state = working_state.get_current(session, rig.principal, rig.project.internal_id, WS)
    assert state is not None
    assert state.next_steps == ["Wire the CLI"]


@respx.mock
def test_no_extracted_working_state_leaves_the_prior_row_unchanged(session, rig):
    row = _submit(session, rig)
    op_id = worker.filer.operation_id(str(row.id), "user")
    _mock_extract(rig.project.bank_id, PREFERENCE)
    _mock_retain(rig.user.bank_id, op_id)
    _mock_operation(rig.user.bank_id, op_id)

    while row.status != "completed":
        _process(session, rig, row)

    assert working_state.get_current(session, rig.principal, rig.project.internal_id, WS) is None


@respx.mock
def test_a_stale_earlier_slice_cannot_overwrite_a_later_offset(session, rig):
    later = _submit(
        session, rig, start_offset=100, end_offset=200, content="assistant: later state",
        content_hash="c" * 64,
    )
    earlier = _submit(
        session, rig, start_offset=0, end_offset=100, content="assistant: earlier state",
        content_hash="d" * 64,
    )

    later_envelope = {"record": "working_state", "objective": "later objective"}
    earlier_envelope = {"record": "working_state", "objective": "earlier objective"}

    # Process the later offset first.
    respx.post(f"{BASE}/v1/default/banks/{rig.project.bank_id}/memories/dry-run-extract").mock(
        side_effect=[httpx.Response(200, json={"facts": [{"text": json.dumps(later_envelope)}]})]
    )
    while later.status != "completed":
        _process(session, rig, later)

    state_after_later = working_state.get_current(
        session, rig.principal, rig.project.internal_id, WS
    )
    assert state_after_later.objective == "later objective"

    # Now process the earlier, now-stale offset -- must not raise, and must
    # not overwrite the newer state.
    respx.post(f"{BASE}/v1/default/banks/{rig.project.bank_id}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": [{"text": json.dumps(earlier_envelope)}]})
    )
    while earlier.status != "completed":
        _process(session, rig, earlier)

    state_after_earlier = working_state.get_current(
        session, rig.principal, rig.project.internal_id, WS
    )
    assert state_after_earlier.objective == "later objective"
    assert earlier.status == "completed"


@respx.mock
def test_exact_replay_does_not_change_updated_at(session, rig):
    row = _submit(session, rig, content="assistant: next I'll wire the CLI")
    _mock_extract(rig.project.bank_id, WORKING_STATE_UPDATE)

    while row.status != "completed":
        _process(session, rig, row)

    state = working_state.get_current(session, rig.principal, rig.project.internal_id, WS)
    first_updated_at = state.updated_at

    # Force a replay of the applying stage on the same, already-completed
    # data.
    row.status = "applying"
    session.commit()
    _process(session, rig, row)

    replayed = working_state.get_current(session, rig.principal, rig.project.internal_id, WS)
    assert replayed.updated_at == first_updated_at


# ---------------------------------------------------------------------------
# Correction fencing
# ---------------------------------------------------------------------------


@respx.mock
def test_correction_refresh_stays_off_by_default(session, rig):
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    mm_route = respx.get(f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": [{"id": "mm-1"}]})
    )

    while row.status != "completed":
        _process(session, rig, row, correction_refresh_enabled=False)

    assert not mm_route.called


@respx.mock
def test_correction_refresh_project_correction_never_touches_the_user_bank(session, rig):
    """A project correction is fenced to the project bank alone -- the user
    bank's mental-models list is never even requested."""
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    project_mm_route = respx.get(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models"
    ).mock(
        return_value=httpx.Response(
            200, json={"mental_models": [{"id": "mm-1", "name": brief.BRIEF_MODEL_NAME}]}
        )
    )
    refresh_route = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-1/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    user_mm_route = respx.get(f"{BASE}/v1/default/banks/{rig.user.bank_id}/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )

    while row.status != "completed":
        _process(session, rig, row, correction_refresh_enabled=True)

    assert project_mm_route.called
    assert refresh_route.called
    assert not user_mm_route.called


@respx.mock
def test_correction_refresh_user_correction_never_touches_the_project_bank(session, rig):
    """The reverse direction: a user correction is fenced to the user bank
    alone -- the project bank's mental-models list is never requested."""
    row = _submit(session, rig)
    correction_envelope = {**PREFERENCE, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "user")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.user.bank_id, op_id)
    _mock_operation(rig.user.bank_id, op_id)
    user_mm_route = respx.get(f"{BASE}/v1/default/banks/{rig.user.bank_id}/mental-models").mock(
        return_value=httpx.Response(
            200, json={"mental_models": [{"id": "mm-1", "name": brief.BRIEF_MODEL_NAME}]}
        )
    )
    refresh_route = respx.post(
        f"{BASE}/v1/default/banks/{rig.user.bank_id}/mental-models/mm-1/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    project_mm_route = respx.get(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models"
    ).mock(return_value=httpx.Response(200, json={"mental_models": []}))

    while row.status != "completed":
        _process(session, rig, row, correction_refresh_enabled=True)

    assert user_mm_route.called
    assert refresh_route.called
    assert not project_mm_route.called


@respx.mock
def test_correction_refresh_legacy_mode_targets_only_the_brief_model(session, rig):
    """`profile_delivery_mode="legacy"` refreshes only
    `brief.BRIEF_MODEL_NAME` -- neither the structured profile model nor any
    operator-created model in the same bank, even though all three are
    listed side by side (the exact bug this task fixes: the old code
    refreshed every model `list_mental_models` returned)."""
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    respx.get(f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {"id": "mm-brief", "name": brief.BRIEF_MODEL_NAME},
                    {"id": "mm-profile", "name": profiles.PROFILE_MODEL_NAME},
                    {"id": "mm-ops", "name": "operator-dashboard-digest"},
                ]
            },
        )
    )
    brief_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-brief/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    profile_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-profile/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    ops_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-ops/refresh"
    ).mock(return_value=httpx.Response(200, json={}))

    while row.status != "completed":
        _process(
            session, rig, row, correction_refresh_enabled=True, profile_delivery_mode="legacy"
        )

    assert brief_refresh.called
    assert not profile_refresh.called
    assert not ops_refresh.called


@respx.mock
def test_correction_refresh_structured_mode_targets_only_the_profile_model(session, rig):
    """The mirror of the legacy-mode test: `profile_delivery_mode="structured"`
    refreshes only `profiles.PROFILE_MODEL_NAME` out of the same three-model
    bank -- never the brief model, never the operator model."""
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    respx.get(f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {"id": "mm-brief", "name": brief.BRIEF_MODEL_NAME},
                    {"id": "mm-profile", "name": profiles.PROFILE_MODEL_NAME},
                    {"id": "mm-ops", "name": "operator-dashboard-digest"},
                ]
            },
        )
    )
    brief_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-brief/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    profile_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-profile/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    ops_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-ops/refresh"
    ).mock(return_value=httpx.Response(200, json={}))

    while row.status != "completed":
        _process(
            session,
            rig,
            row,
            correction_refresh_enabled=True,
            profile_delivery_mode="structured",
        )

    assert profile_refresh.called
    assert not brief_refresh.called
    assert not ops_refresh.called


@respx.mock
def test_correction_refresh_missing_target_is_a_bounded_noop(session, rig):
    """A bank that lists models but none named for the active mode's target
    is a silent no-op: no exception, no refresh call -- and the capture
    still completes normally rather than being charged a failure."""
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    respx.get(f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models").mock(
        return_value=httpx.Response(
            200, json={"mental_models": [{"id": "mm-ops", "name": "operator-dashboard-digest"}]}
        )
    )
    refresh_route = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-ops/refresh"
    ).mock(return_value=httpx.Response(200, json={}))

    while row.status != "completed":
        _process(
            session, rig, row, correction_refresh_enabled=True, profile_delivery_mode="legacy"
        )

    assert row.status == "completed"
    assert not refresh_route.called


@respx.mock
def test_correction_refresh_retry_repeats_the_same_scope_not_a_widened_one(session, rig):
    """A retried applying stage re-derives bank scope and target selection
    from the same persisted candidates every time. Here the upstream
    refresh call itself fails once (a 500), so the capture cannot complete;
    retrying must land on the exact same single corrected bank and the same
    single target model -- never additionally touch the user bank, never
    refresh a second model."""
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    project_mm_route = respx.get(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models"
    ).mock(
        return_value=httpx.Response(
            200, json={"mental_models": [{"id": "mm-1", "name": brief.BRIEF_MODEL_NAME}]}
        )
    )
    refresh_route = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-1/refresh"
    ).mock(side_effect=[httpx.Response(500), httpx.Response(200, json={})])
    user_mm_route = respx.get(f"{BASE}/v1/default/banks/{rig.user.bank_id}/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )

    while row.status != "applying":
        _process(session, rig, row, correction_refresh_enabled=False)

    with pytest.raises(HindsightError):
        _process(session, rig, row, correction_refresh_enabled=True)
    session.rollback()
    session.refresh(row)
    assert row.status == "applying"

    _process(session, rig, row, correction_refresh_enabled=True)

    assert row.status == "completed"
    assert project_mm_route.call_count == 2
    assert refresh_route.call_count == 2
    assert not user_mm_route.called


@respx.mock
def test_correction_refresh_ignores_unrelated_operator_models(session, rig):
    """A bank holding the target model plus two unrelated operator-created
    models refreshes only the target -- never the operator models, and
    never more than one refresh call for this bank."""
    row = _submit(session, rig)
    correction_envelope = {**CONVENTION, "correction": True}
    op_id = worker.filer.operation_id(str(row.id), "project")
    _mock_extract(rig.project.bank_id, correction_envelope)
    _mock_retain(rig.project.bank_id, op_id)
    _mock_operation(rig.project.bank_id, op_id)
    respx.get(f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {"id": "mm-ops-1", "name": "operator-first"},
                    {"id": "mm-1", "name": brief.BRIEF_MODEL_NAME},
                    {"id": "mm-ops-2", "name": "operator-second"},
                ]
            },
        )
    )
    target_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-1/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    ops1_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-ops-1/refresh"
    ).mock(return_value=httpx.Response(200, json={}))
    ops2_refresh = respx.post(
        f"{BASE}/v1/default/banks/{rig.project.bank_id}/mental-models/mm-ops-2/refresh"
    ).mock(return_value=httpx.Response(200, json={}))

    while row.status != "completed":
        _process(
            session, rig, row, correction_refresh_enabled=True, profile_delivery_mode="legacy"
        )

    assert target_refresh.call_count == 1
    assert not ops1_refresh.called
    assert not ops2_refresh.called


# ---------------------------------------------------------------------------
# run_once(): the enabled gate, batching, and disabled-means-no-lease
# ---------------------------------------------------------------------------


def test_run_once_returns_false_and_leases_nothing_when_disabled(session, rig, monkeypatch):
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_CAPTURE_WORKER_ENABLED", "false")
    get_settings.cache_clear()
    _submit(session, rig)

    found = worker.run_once(session, rig.client)

    assert found is False
    assert session.query(CaptureSlice).filter(CaptureSlice.lease_owner.isnot(None)).count() == 0
    get_settings.cache_clear()


@respx.mock
def test_run_once_processes_one_stage_when_enabled(session, rig, monkeypatch):
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_CAPTURE_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    row = _submit(session, rig)
    _mock_extract(rig.project.bank_id, PREFERENCE)

    found = worker.run_once(session, rig.client)

    assert found is True
    session.refresh(row)
    assert row.status == "retaining"
    get_settings.cache_clear()


@respx.mock
def test_run_once_drives_a_row_to_completion_across_repeated_calls(session, rig, monkeypatch):
    """Exercises the real acquire_lease() path, not the direct process_row()
    shortcut the crash-boundary tests use above: each stage must actually
    release its lease so the very next run_once() call can re-lease the
    same row for its next stage, instead of sitting locked until the lease
    it already used expires."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_CAPTURE_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    row = _submit(session, rig)
    _mock_extract(rig.project.bank_id, PREFERENCE)
    op_id = worker.filer.operation_id(str(row.id), "user")
    _mock_retain(rig.user.bank_id, op_id)
    _mock_operation(rig.user.bank_id, op_id)

    attempts = 0
    while worker.run_once(session, rig.client) and attempts < 10:
        attempts += 1

    session.refresh(row)
    assert row.status == "completed"
    assert row.lease_owner is None
    get_settings.cache_clear()
