import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from memory import brief, ids, projects, working_state
from memory.auth.principal import Principal
from memory.errors import (
    ProjectAccessDenied,
    ProjectNotFound,
    WorkingSessionNotFound,
    WorkingStateConflict,
    WorkingStateStale,
)
from memory.models import Project, ProjectSlug, Tenant, User, WorkingSession, WorkingState
from memory.working_state import WorkingStateWrite


def _project(tenant: str, *, slug: str = "acme-api", owner_id: str = "usr_x") -> Project:
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        owner_type="user",
        owner_id=owner_id,
        bank_id=ids.new_project_bank_id(),
    )
    project.slug_rows.append(
        ProjectSlug(tenant_id=tenant, slug=slug, is_canonical=True)
    )
    return project


def _user(tenant: str, *, user_id: str | None = None) -> User:
    return User(
        id=user_id or ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id()
    )


def _principal(tenant: str, user_id: str | None, master: bool = False) -> Principal:
    return Principal(tenant_id=tenant, user_id=user_id, is_master=master, key_id="key_x")


def _state(*, tenant: str, user_id: str, project_internal_id: str, workspace_id: str) -> WorkingState:
    return WorkingState(
        tenant_id=tenant,
        user_id=user_id,
        project_internal_id=project_internal_id,
        workspace_id=workspace_id,
        objective="ship the feature",
        current_direction=None,
        recent_decisions=["use approach A"],
        open_questions=["is B in scope?"],
        next_steps=["write tests"],
        updated_at=datetime.now(UTC),
        session_id="sess-1",
        session_epoch=1,
        checkpoint_seq=1,
    )


def test_two_workspaces_of_one_user_project_each_hold_their_own_state(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    session.add(
        _state(
            tenant=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "a" * 32,
        )
    )
    session.add(
        _state(
            tenant=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "b" * 32,
        )
    )
    session.flush()

    assert session.query(WorkingState).count() == 2


def test_a_duplicate_session_scope_cannot_allocate_a_second_row(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    def _row() -> WorkingSession:
        return WorkingSession(
            tenant_id=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "a" * 32,
            session_id="sess-1",
        )

    session.add(_row())
    session.flush()
    session.add(_row())

    with pytest.raises(IntegrityError):
        session.flush()


def test_session_epochs_increase_across_distinct_session_ids(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    first = WorkingSession(
        tenant_id=tenant,
        user_id=user.id,
        project_internal_id=project.internal_id,
        workspace_id="ws_" + "a" * 32,
        session_id="sess-1",
    )
    session.add(first)
    session.flush()

    second = WorkingSession(
        tenant_id=tenant,
        user_id=user.id,
        project_internal_id=project.internal_id,
        workspace_id="ws_" + "a" * 32,
        session_id="sess-2",
    )
    session.add(second)
    session.flush()

    assert second.session_epoch > first.session_epoch


def test_working_state_json_fields_round_trip_and_updated_at_is_aware(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    session.add(
        _state(
            tenant=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "a" * 32,
        )
    )
    session.flush()
    session.expire_all()

    stored = session.query(WorkingState).one()
    assert stored.recent_decisions == ["use approach A"]
    assert stored.open_questions == ["is B in scope?"]
    assert stored.next_steps == ["write tests"]
    assert stored.updated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# working_state.start_session / replace / get_current
# ---------------------------------------------------------------------------

WS = "ws_" + "a" * 32


def _write(**overrides) -> WorkingStateWrite:
    fields = {
        "project_slug": "acme-api",
        "workspace_id": WS,
        "session_id": "sess-1",
        "session_epoch": 1,
        "checkpoint_seq": 1,
        "objective": "ship the feature",
    }
    fields.update(overrides)
    return WorkingStateWrite(**fields)


def test_starting_the_same_session_twice_is_idempotent(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)

    first = working_state.start_session(session, juan, "acme-api", WS, "sess-1")
    second = working_state.start_session(session, juan, "acme-api", WS, "sess-1")

    assert first.session_epoch == second.session_epoch


def test_a_second_session_gets_a_higher_epoch(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)

    first = working_state.start_session(session, juan, "acme-api", WS, "sess-1")
    second = working_state.start_session(session, juan, "acme-api", WS, "sess-2")

    assert second.session_epoch > first.session_epoch


def test_checkpoint_two_replaces_checkpoint_one_within_one_session(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)
    epoch = working_state.start_session(session, juan, "acme-api", WS, "sess-1").session_epoch

    working_state.replace(
        session, juan, _write(session_epoch=epoch, checkpoint_seq=1, objective="first")
    )
    state, changed = working_state.replace(
        session, juan, _write(session_epoch=epoch, checkpoint_seq=2, objective="second")
    )

    assert changed is True
    assert state.objective == "second"
    assert state.checkpoint_seq == 2


def test_checkpoint_one_cannot_replace_checkpoint_two(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)
    epoch = working_state.start_session(session, juan, "acme-api", WS, "sess-1").session_epoch

    working_state.replace(
        session, juan, _write(session_epoch=epoch, checkpoint_seq=2, objective="second")
    )

    with pytest.raises(WorkingStateStale):
        working_state.replace(
            session, juan, _write(session_epoch=epoch, checkpoint_seq=1, objective="first")
        )


def test_an_older_session_cannot_overwrite_a_newer_session_even_with_a_larger_checkpoint(
    session, tenant
):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)
    older = working_state.start_session(session, juan, "acme-api", WS, "sess-older")
    newer = working_state.start_session(session, juan, "acme-api", WS, "sess-newer")

    working_state.replace(
        session,
        juan,
        _write(
            session_id="sess-newer",
            session_epoch=newer.session_epoch,
            checkpoint_seq=1,
            objective="from the newer session",
        ),
    )

    with pytest.raises(WorkingStateStale):
        working_state.replace(
            session,
            juan,
            _write(
                session_id="sess-older",
                session_epoch=older.session_epoch,
                checkpoint_seq=999,
                objective="from the older session",
            ),
        )


def test_an_identical_retry_at_the_same_pair_does_not_change_updated_at(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)
    epoch = working_state.start_session(session, juan, "acme-api", WS, "sess-1").session_epoch
    request = _write(session_epoch=epoch, checkpoint_seq=1, objective="steady state")

    first, first_changed = working_state.replace(session, juan, request)
    session.flush()
    stamped = first.updated_at
    second, second_changed = working_state.replace(session, juan, request)

    assert first_changed is True
    assert second_changed is False
    assert second.updated_at == stamped


def test_different_content_at_the_same_pair_is_a_conflict(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)
    epoch = working_state.start_session(session, juan, "acme-api", WS, "sess-1").session_epoch
    working_state.replace(
        session, juan, _write(session_epoch=epoch, checkpoint_seq=1, objective="first version")
    )

    with pytest.raises(WorkingStateConflict):
        working_state.replace(
            session,
            juan,
            _write(session_epoch=epoch, checkpoint_seq=1, objective="a different version"),
        )


def test_two_users_of_the_same_project_and_workspace_hold_separate_state(session, tenant):
    juan_user = _user(tenant, user_id="usr_juan")
    alice_user = _user(tenant, user_id="usr_alice")
    session.add_all([juan_user, alice_user])
    project = _project(tenant, owner_id=juan_user.id)
    session.add(project)
    session.flush()
    # Alice needs her own authorized project row; projects are owned by one
    # user/group, so give her a second project at a different slug and prove
    # isolation is by (user, workspace), not merely by project.
    alice_project = _project(tenant, slug="alice-api", owner_id=alice_user.id)
    session.add(alice_project)
    session.flush()
    juan = _principal(tenant, juan_user.id)
    alice = _principal(tenant, alice_user.id)

    juan_epoch = working_state.start_session(session, juan, "acme-api", WS, "sess-juan").session_epoch
    alice_epoch = working_state.start_session(
        session, alice, "alice-api", WS, "sess-alice"
    ).session_epoch
    working_state.replace(
        session,
        juan,
        _write(session_id="sess-juan", session_epoch=juan_epoch, checkpoint_seq=1, objective="juan's work"),
    )
    working_state.replace(
        session,
        alice,
        _write(
            project_slug="alice-api",
            session_id="sess-alice",
            session_epoch=alice_epoch,
            checkpoint_seq=1,
            objective="alice's work",
        ),
    )

    juan_state = working_state.get_current(session, juan, project.internal_id, WS)
    alice_state = working_state.get_current(session, alice, alice_project.internal_id, WS)
    assert juan_state.objective == "juan's work"
    assert alice_state.objective == "alice's work"


def test_two_workspaces_via_replace_hold_separate_state(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    project = _project(tenant, owner_id=user.id)
    session.add(project)
    session.flush()
    juan = _principal(tenant, user.id)
    ws_a, ws_b = "ws_" + "a" * 32, "ws_" + "b" * 32

    epoch_a = working_state.start_session(session, juan, "acme-api", ws_a, "sess-a").session_epoch
    epoch_b = working_state.start_session(session, juan, "acme-api", ws_b, "sess-b").session_epoch
    working_state.replace(
        session,
        juan,
        _write(workspace_id=ws_a, session_id="sess-a", session_epoch=epoch_a, checkpoint_seq=1, objective="workspace A"),
    )
    working_state.replace(
        session,
        juan,
        _write(workspace_id=ws_b, session_id="sess-b", session_epoch=epoch_b, checkpoint_seq=1, objective="workspace B"),
    )

    state_a = working_state.get_current(session, juan, project.internal_id, ws_a)
    state_b = working_state.get_current(session, juan, project.internal_id, ws_b)
    assert state_a.objective == "workspace A"
    assert state_b.objective == "workspace B"


def test_two_tenants_with_the_same_slug_hold_separate_state(session):
    tenant_a, tenant_b = "tenant-a", "tenant-b"
    session.add_all([Tenant(id=tenant_a), Tenant(id=tenant_b)])
    session.flush()
    user_a = _user(tenant_a, user_id="usr_a")
    user_b = _user(tenant_b, user_id="usr_b")
    session.add_all([user_a, user_b])
    session.flush()
    project_a = _project(tenant_a, owner_id=user_a.id)
    project_b = _project(tenant_b, owner_id=user_b.id)
    session.add_all([project_a, project_b])
    session.flush()
    principal_a = _principal(tenant_a, user_a.id)
    principal_b = _principal(tenant_b, user_b.id)

    epoch_a = working_state.start_session(session, principal_a, "acme-api", WS, "sess-1").session_epoch
    epoch_b = working_state.start_session(session, principal_b, "acme-api", WS, "sess-1").session_epoch
    working_state.replace(
        session, principal_a, _write(session_epoch=epoch_a, checkpoint_seq=1, objective="tenant a's work")
    )
    working_state.replace(
        session, principal_b, _write(session_epoch=epoch_b, checkpoint_seq=1, objective="tenant b's work")
    )

    state_a = working_state.get_current(session, principal_a, project_a.internal_id, WS)
    state_b = working_state.get_current(session, principal_b, project_b.internal_id, WS)
    assert state_a.objective == "tenant a's work"
    assert state_b.objective == "tenant b's work"


def test_project_rename_preserves_state(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)
    project = projects.resolve(session, juan, "acme-api").project
    epoch = working_state.start_session(session, juan, "acme-api", WS, "sess-1").session_epoch
    working_state.replace(
        session, juan, _write(session_epoch=epoch, checkpoint_seq=1, objective="before the rename")
    )

    projects.rename(session, juan, project, "acme-api-v2")

    state = working_state.get_current(session, juan, project.internal_id, WS)
    assert state.objective == "before the rename"


def test_starting_a_session_for_an_absent_project_creates_no_project(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.flush()
    juan = _principal(tenant, user.id)

    with pytest.raises(ProjectNotFound):
        working_state.start_session(session, juan, "no-such-project", WS, "sess-1")

    assert session.query(Project).count() == 0


def test_setting_state_for_an_unauthorized_project_creates_no_project(session, tenant):
    owner = _user(tenant, user_id="usr_owner")
    stranger = _user(tenant, user_id="usr_stranger")
    session.add_all([owner, stranger])
    session.add(_project(tenant, owner_id=owner.id))
    session.flush()
    stranger_principal = _principal(tenant, stranger.id)

    with pytest.raises(ProjectAccessDenied):
        working_state.start_session(session, stranger_principal, "acme-api", WS, "sess-1")

    assert session.query(Project).count() == 1


def test_replace_rejects_an_unknown_session_epoch(session, tenant):
    user = _user(tenant, user_id="usr_juan")
    session.add(user)
    session.add(_project(tenant, owner_id=user.id))
    session.flush()
    juan = _principal(tenant, user.id)

    with pytest.raises(WorkingSessionNotFound):
        working_state.replace(
            session, juan, _write(session_id="sess-never-started", session_epoch=999999, checkpoint_seq=1)
        )


def test_first_writes_from_two_sessions_race_and_the_greater_pair_survives(engine):
    """Two real connections attempt the FIRST write for the same (user,
    project, workspace) at once. Whichever INSERT physically lands first,
    the loser's retry compares pairs against the winner's row and either
    overwrites it (its pair is greater) or is rejected (its pair is lower)
    -- so the stored row always ends up holding the lexicographically
    greater pair, regardless of arrival order."""
    Session = sessionmaker(bind=engine)
    setup = Session()
    tenant_id = f"race-{uuid.uuid4().hex[:12]}"
    workspace_id = "ws_" + "c" * 32
    try:
        setup.add(Tenant(id=tenant_id))
        user = _user(tenant_id, user_id=f"usr_{uuid.uuid4().hex[:12]}")
        setup.add(user)
        setup.flush()
        project = _project(tenant_id, owner_id=user.id)
        setup.add(project)
        setup.flush()
        principal = _principal(tenant_id, user.id)

        low = working_state.start_session(setup, principal, "acme-api", workspace_id, "sess-low")
        high = working_state.start_session(setup, principal, "acme-api", workspace_id, "sess-high")
        setup.commit()
        assert high.session_epoch > low.session_epoch

        low_request = _write(
            workspace_id=workspace_id,
            session_id="sess-low",
            session_epoch=low.session_epoch,
            checkpoint_seq=1,
            objective="low arrives",
        )
        high_request = _write(
            workspace_id=workspace_id,
            session_id="sess-high",
            session_epoch=high.session_epoch,
            checkpoint_seq=1,
            objective="high arrives",
        )

        barrier = threading.Barrier(2)
        results: dict[str, tuple[str, object]] = {}

        def _race(name: str, request: WorkingStateWrite) -> None:
            barrier.wait(timeout=5)
            racer = Session()
            try:
                _, changed = working_state.replace(racer, principal, request)
                racer.commit()
                results[name] = ("ok", changed)
            except Exception as exc:  # noqa: BLE001 -- captured across threads
                racer.rollback()
                results[name] = ("error", exc)
            finally:
                racer.close()

        threads = [
            threading.Thread(target=_race, args=("low", low_request)),
            threading.Thread(target=_race, args=("high", high_request)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results["high"][0] == "ok"
        if results["low"][0] == "error":
            assert isinstance(results["low"][1], WorkingStateStale)

        verify = Session()
        try:
            stored = (
                verify.query(WorkingState)
                .filter_by(
                    tenant_id=tenant_id,
                    user_id=user.id,
                    project_internal_id=project.internal_id,
                    workspace_id=workspace_id,
                )
                .one()
            )
            assert stored.session_id == "sess-high"
            assert stored.objective == "high arrives"
        finally:
            verify.close()
    finally:
        cleanup = Session()
        cleanup.query(WorkingState).filter_by(tenant_id=tenant_id).delete()
        cleanup.query(WorkingSession).filter_by(tenant_id=tenant_id).delete()
        cleanup.query(ProjectSlug).filter_by(tenant_id=tenant_id).delete()
        cleanup.query(Project).filter_by(tenant_id=tenant_id).delete()
        cleanup.query(User).filter_by(tenant_id=tenant_id).delete()
        cleanup.query(Tenant).filter_by(id=tenant_id).delete()
        cleanup.commit()
        cleanup.close()


# ---------------------------------------------------------------------------
# render_index_headline / render_full_section
# ---------------------------------------------------------------------------


def _rendered_state(**overrides) -> WorkingState:
    fields = {
        "objective": "ship the feature",
        "current_direction": None,
        "recent_decisions": [],
        "open_questions": [],
        "next_steps": [],
        "updated_at": datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        "session_id": "sess-1",
        "session_epoch": 3,
        "checkpoint_seq": 2,
    }
    fields.update(overrides)
    return WorkingState(**fields)


def test_render_index_headline_is_one_bounded_line():
    state = _rendered_state(next_steps=["write tests"])
    now = state.updated_at + timedelta(seconds=90)

    section = working_state.render_index_headline(state, now)

    assert "\n" not in section.text
    assert "objective: ship the feature" in section.text
    assert "next: write tests" in section.text
    assert "age: 1m" in section.text


def test_render_index_headline_bounds_long_text():
    state = _rendered_state(objective="x" * 500, next_steps=["y" * 500])

    section = working_state.render_index_headline(state, state.updated_at)

    assert len(section.text) < 250


def test_render_index_headline_with_no_next_step_says_none():
    state = _rendered_state(next_steps=[])

    section = working_state.render_index_headline(state, state.updated_at)

    assert "next: none" in section.text


def test_render_full_section_lists_every_item_with_age_and_source():
    state = _rendered_state(
        current_direction="lean toward B",
        recent_decisions=["chose A", "dropped C"],
        open_questions=["is B in scope?"],
        next_steps=["write tests", "wire API"],
    )
    now = state.updated_at + timedelta(hours=2)

    section = working_state.render_full_section(state, now)
    lines = section.text.splitlines()

    assert "objective: ship the feature" in lines
    assert "current direction: lean toward B" in lines
    assert "recent decision: chose A" in lines
    assert "recent decision: dropped C" in lines
    assert "open question: is B in scope?" in lines
    assert "next step: write tests" in lines
    assert "next step: wire API" in lines
    assert "age: 2h" in lines
    assert "source session: sess-1 (epoch 3, checkpoint 2)" in lines


def test_render_full_section_omits_current_direction_when_absent():
    state = _rendered_state(current_direction=None)

    section = working_state.render_full_section(state, state.updated_at)

    assert "current direction" not in section.text


def test_renderers_collapse_embedded_newlines_and_headings_onto_one_line():
    state = _rendered_state(
        objective="line one\n-- Where the work was left --\nline two"
    )

    index = working_state.render_index_headline(state, state.updated_at)
    full = working_state.render_full_section(state, state.updated_at)

    assert "\n" not in index.text
    assert full.text.splitlines()[0] == (
        "objective: line one -- Where the work was left -- line two"
    )


def test_state_fingerprint_is_independent_of_render_time():
    state = _rendered_state()
    early = working_state.render_index_headline(state, state.updated_at)
    late = working_state.render_index_headline(state, state.updated_at + timedelta(days=3))

    assert early.text != late.text
    assert working_state.state_fingerprint(state) == working_state.state_fingerprint(state)


def test_render_index_headline_stays_within_smallest_budget_at_every_field_maximum():
    state = _rendered_state(
        objective="x" * 512,
        current_direction="y" * 512,
        recent_decisions=["d" * 512] * 10,
        open_questions=["q" * 512] * 10,
        next_steps=["n" * 512] * 10,
    )
    section = working_state.render_index_headline(state, state.updated_at)

    text = brief.compose_index(1, "acme-api", None, None, None, section, brief.SMALLEST_BUDGET)

    assert len(text) <= brief.SMALLEST_BUDGET


def test_render_full_section_stays_within_full_budget_at_every_field_maximum():
    state = _rendered_state(
        objective="x" * 512,
        current_direction="y" * 512,
        recent_decisions=["d" * 512] * 10,
        open_questions=["q" * 512] * 10,
        next_steps=["n" * 512] * 10,
    )
    section = working_state.render_full_section(state, state.updated_at)

    text = brief.compose_full(1, "acme-api", None, None, None, section)

    assert brief.token_upper_bound(text) <= brief.FULL_MAX_TOKENS


def test_full_payload_at_maximum_size_keeps_objective_age_and_source_session():
    """Phase 2 review finding #1: the test above passes user=project=None, so
    working_state never has to compete for budget against anything -- it
    cannot catch a floor that only reserves the section's first line. With
    crowded user/project sections, a maximum-size Working State could spend
    the whole round-robin on its own optional middle (current direction,
    decisions, questions, next steps) and never reach age/source session,
    which sit at the tail of the same body list.
    """
    state = _rendered_state(
        objective="x" * 512,
        current_direction="y" * 512,
        recent_decisions=["d" * 512] * 10,
        open_questions=["q" * 512] * 10,
        next_steps=["n" * 512] * 10,
    )
    now = state.updated_at + timedelta(hours=2)
    section = working_state.render_full_section(state, now)
    crowded = brief.Section(
        text="\n".join(f"line {i}: " + "p" * 200 for i in range(200)),
        refreshed_at="2026-08-31T00:00:00+00:00",
    )

    text = brief.compose_full(1, "acme-api", crowded, None, crowded, section)

    assert brief.token_upper_bound(text) <= brief.FULL_MAX_TOKENS
    lines = text.splitlines()
    assert "objective: " + "x" * 512 in lines
    assert "age: 2h" in lines
    assert "source session: sess-1 (epoch 3, checkpoint 2)" in lines
