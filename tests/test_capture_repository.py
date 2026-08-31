from datetime import timedelta

import pytest

from memory import ids
from memory.auth.principal import Principal
from memory.capture import repository
from memory.errors import CaptureConflict, ProjectNotFound
from memory.models import CaptureSlice, Project, User

WS = "ws_" + "a" * 32


def _user_and_project(session, tenant, *, user_id: str = "usr_cap", slug: str = "acme-api"):
    user = User(id=user_id, tenant_id=tenant, bank_id=ids.new_user_bank_id())
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        project_slug=slug,
        owner_type="user",
        owner_id=user_id,
        bank_id=ids.new_project_bank_id(),
    )
    session.add_all([user, project])
    session.flush()
    return user, project


def _principal(user, tenant) -> Principal:
    return Principal(
        tenant_id=tenant, user_id=user.id, is_master=False, key_id="key_x",
        credential_id="key_x",
    )


def _submit(session, principal, **overrides):
    fields = {
        "host": "claude-code",
        "session_id": "sess-1",
        "project_slug": "acme-api",
        "git_locator": None,
        "workspace_id": WS,
        "start_offset": 0,
        "end_offset": 100,
        "content_hash": "a" * 64,
        "sanitized_hash": "b" * 64,
        "content": "user: hello",
    }
    fields.update(overrides)
    return repository.accept_checkpoint(session, principal, **fields)


def test_accept_checkpoint_creates_a_new_row_and_allocates_a_session(session, tenant):
    user, project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)

    result = _submit(session, principal)

    assert result.duplicate is False
    assert result.row.status == "pending"
    assert result.row.session_epoch >= 0
    assert result.row.project_internal_id == project.internal_id
    assert result.resolution.project.project_slug == "acme-api"


def test_accept_checkpoint_reuses_the_working_session_across_two_slices(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)

    first = _submit(session, principal, start_offset=0, end_offset=100, content_hash="a" * 64)
    second = _submit(
        session, principal, start_offset=100, end_offset=200, content_hash="c" * 64
    )

    assert first.row.session_epoch == second.row.session_epoch


def test_an_exact_duplicate_returns_the_original_row(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)

    first = _submit(session, principal)
    second = _submit(session, principal)

    assert second.duplicate is True
    assert second.row.id == first.row.id
    assert session.query(CaptureSlice).count() == 1


def test_the_same_offsets_with_a_different_hash_is_a_conflict(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)

    _submit(session, principal, content_hash="a" * 64, sanitized_hash="b" * 64)

    with pytest.raises(CaptureConflict):
        _submit(session, principal, content_hash="z" * 64, sanitized_hash="b" * 64)


def test_the_same_offsets_with_a_different_sanitized_hash_is_a_conflict(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)

    _submit(session, principal, content_hash="a" * 64, sanitized_hash="b" * 64)

    with pytest.raises(CaptureConflict):
        _submit(session, principal, content_hash="a" * 64, sanitized_hash="z" * 64)


def test_an_unknown_project_is_not_found_and_creates_no_project(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    before = session.query(Project).count()

    with pytest.raises(ProjectNotFound):
        _submit(session, principal, project_slug="no-such-project")

    assert session.query(Project).count() == before


def test_two_tenants_with_identical_scope_fields_do_not_collide(session, tenant):
    from memory.models import Tenant

    other_tenant = "tenant-b"
    session.add(Tenant(id=other_tenant))
    session.flush()
    user_a, _ = _user_and_project(session, tenant, user_id="usr_a1")
    user_b, _ = _user_and_project(session, other_tenant, user_id="usr_a2", slug="acme-api")

    result_a = _submit(session, _principal(user_a, tenant))
    result_b = _submit(session, _principal(user_b, other_tenant))

    assert result_a.row.id != result_b.row.id
    assert session.query(CaptureSlice).count() == 2


# ---------------------------------------------------------------------------
# Leases and stage persistence
# ---------------------------------------------------------------------------


def test_two_workers_cannot_acquire_the_same_row(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    _submit(session, principal)
    session.commit()

    leased = repository.acquire_lease(session, owner="worker-a", lease_seconds=60)
    session.commit()

    assert len(leased) == 1
    still_available = repository.acquire_lease(session, owner="worker-b", lease_seconds=60)
    assert still_available == []


def test_an_expired_lease_is_recoverable(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    _submit(session, principal)
    session.commit()

    first = repository.acquire_lease(session, owner="worker-a", lease_seconds=60)
    session.commit()
    assert len(first) == 1
    row = first[0]
    # Simulate a crashed worker: its lease is already in the past.
    row.lease_until = row.lease_until - timedelta(seconds=120)
    session.commit()

    recovered = repository.acquire_lease(session, owner="worker-b", lease_seconds=60)

    assert len(recovered) == 1
    assert recovered[0].id == row.id
    assert recovered[0].lease_owner == "worker-b"


def test_stage_payloads_survive_a_process_boundary(session, tenant, connection):
    from sqlalchemy.orm import Session as SASession

    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    result = _submit(session, principal)
    session.commit()

    repository.advance_stage(
        session, result.row, status="retaining", extraction=[{"text": "a fact"}]
    )
    session.commit()

    # A fresh Session bound to the same connection stands in for "a
    # different worker process reading the row back."
    other_session = SASession(bind=connection)
    reloaded = other_session.get(CaptureSlice, result.row.id)

    assert reloaded.status == "retaining"
    assert reloaded.extraction == [{"text": "a fact"}]


def test_record_failure_increments_attempts_and_backs_off(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    result = _submit(session, principal)
    session.commit()

    repository.record_failure(session, result.row, error_code="HINDSIGHT_UNAVAILABLE")
    session.commit()

    assert result.row.attempt_count == 1
    # Stays at the stage that failed (still "pending") -- only max_attempts
    # failures make it the terminal "failed"; see the dedicated test below.
    assert result.row.status == "pending"
    assert result.row.last_error_code == "HINDSIGHT_UNAVAILABLE"
    assert result.row.available_at > result.row.updated_at - timedelta(seconds=1)
    assert result.row.lease_owner is None


def test_backoff_never_exceeds_the_cap(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    result = _submit(session, principal)
    session.commit()

    for _ in range(10):
        repository.record_failure(
            session, result.row, error_code="HINDSIGHT_UNAVAILABLE", backoff_cap_seconds=300
        )
    session.commit()

    delay = (result.row.available_at - result.row.updated_at).total_seconds()
    assert delay <= 301


def test_a_row_past_max_attempts_is_not_auto_leased(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    result = _submit(session, principal)
    session.commit()

    for _ in range(8):
        repository.record_failure(session, result.row, error_code="E", backoff_cap_seconds=0)
    session.commit()
    result.row.available_at = result.row.available_at - timedelta(days=1)
    session.commit()

    assert result.row.status == "failed"

    leased = repository.acquire_lease(session, owner="worker-a", lease_seconds=60, max_attempts=8)

    assert leased == []


def test_complete_clears_sanitized_content_and_extraction(session, tenant):
    user, _project = _user_and_project(session, tenant)
    principal = _principal(user, tenant)
    result = _submit(session, principal, content="user: something sensitive")
    session.commit()
    assert result.row.sanitized_content == "user: something sensitive"

    repository.advance_stage(session, result.row, status="applying", extraction=[{"x": 1}])
    repository.complete(session, result.row)
    session.commit()

    assert result.row.status == "completed"
    assert result.row.sanitized_content is None
    assert result.row.extraction is None
    assert result.row.completed_at is not None
