import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from memory import ids
from memory.auth.principal import Principal
from memory.context_service import ContextService
from memory.models import (
    BankCurrentness,
    MentalModelRegistration,
    Project,
    ProjectSlug,
    RetainedRecord,
    User,
    WorkingState,
)
from memory.v040_contracts import LoadContextRequest


class _StepClock:
    """A `clock: Callable[[], float]` test double: `advance()` moves it
    forward by a caller-controlled amount, independent of real wall time."""

    def __init__(self, start: float = 0.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _DeadlineProbeClient:
    """`get_mental_model` for the FAST model returns immediately and pushes
    the shared `_StepClock` well past the deadline, simulating "reaching
    this point consumed the whole budget" without any real delay. The SLOW
    model blocks briefly on a real event -- an over-budget call the deadline
    must not wait out."""

    def __init__(self, clock: _StepClock, *, slow_model_id: str, advance_seconds: float):
        self.clock = clock
        self.slow_model_id = slow_model_id
        self.advance_seconds = advance_seconds
        self.calls: list[tuple[str, str, float | None]] = []
        self._slow_gate = threading.Event()

    def get_mental_model(self, bank_id: str, model_id: str, *, timeout: float | None = None) -> dict:
        self.calls.append((bank_id, model_id, timeout))
        if model_id == self.slow_model_id:
            self._slow_gate.wait(timeout=0.3)
            return {"text": f"content:{model_id}"}
        self.clock.advance(self.advance_seconds)
        return {"text": f"content:{model_id}"}


class RecordingClient:
    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.calls: list[tuple[str, str, float | None]] = []

    def get_mental_model(
        self, bank_id: str, model_id: str, *, timeout: float | None = None
    ) -> dict:
        self.calls.append((bank_id, model_id, timeout))
        if self.delay:
            time.sleep(self.delay)
        return {"text": f"content:{model_id}"}


def _user(tenant_id: str, user_id: str) -> User:
    return User(id=user_id, tenant_id=tenant_id, bank_id=ids.new_user_bank_id())


def _project(tenant_id: str, user_id: str, slug: str) -> Project:
    row = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant_id,
        owner_type="user",
        owner_id=user_id,
        bank_id=ids.new_project_bank_id(),
    )
    row.slug_rows.append(ProjectSlug(tenant_id=tenant_id, slug=slug, is_canonical=True))
    return row


def _registration(
    tenant_id: str,
    *,
    model_key: str,
    model_id: str,
    user_id: str | None = None,
    project_internal_id: str | None = None,
    origin: str = "user",
) -> MentalModelRegistration:
    scope = "user" if user_id is not None else "project"
    return MentalModelRegistration(
        tenant_id=tenant_id,
        scope=scope,
        user_id=user_id,
        project_internal_id=project_internal_id,
        model_key=model_key,
        upstream_model_id=model_id,
        name=model_key,
        source_query="known context",
        source_tags=["schema:ach-retain-v1", "validity:indefinite"],
        tags_match="all",
        max_tokens=256,
        trigger={},
        origin=origin,
        builtin_key=model_key if origin == "builtin" else None,
        definition_version=1 if origin == "builtin" else None,
        lifecycle_state="active",
        delivery_state="ready",
    )


def _claim(
    tenant_id: str,
    *,
    content: str,
    user_id: str | None = None,
    project_internal_id: str | None = None,
    upstream_state: str = "completed",
) -> RetainedRecord:
    now = datetime.now(UTC)
    operation_id = str(uuid.uuid4())
    return RetainedRecord(
        tenant_id=tenant_id,
        scope="user" if user_id is not None else "project",
        user_id=user_id,
        project_internal_id=project_internal_id,
        operation_id=operation_id,
        payload_hash=uuid.uuid4().hex,
        document_id=f"ach-retain-{uuid.uuid4().hex}",
        source_memory_id=f"mem-{uuid.uuid4().hex}",
        canonical_content=content,
        memory_type="constraint",
        basis="human_explicit",
        trigger="user_requested",
        sanitized_evidence=[{"kind": "user_message", "raw": content, "source_ref": None}],
        recorded_at=now - timedelta(minutes=1),
        valid_from=now - timedelta(minutes=1),
        valid_until=now + timedelta(hours=1),
        lifecycle="active",
        upstream_state=upstream_state,
    )


def test_context_selects_only_the_callers_user_and_authorized_project(session, tenant):
    juan = _user(tenant, "usr_juan")
    maria = _user(tenant, "usr_maria")
    alpha = _project(tenant, juan.id, "alpha")
    alpha.name = "Alpha"
    beta = _project(tenant, maria.id, "beta")
    session.add_all([juan, maria, alpha, beta])
    session.flush()
    session.add_all(
        [
            _registration(
                tenant, model_key="juan", model_id="mm-juan", user_id=juan.id, origin="builtin"
            ),
            _registration(
                tenant, model_key="maria", model_id="mm-maria", user_id=maria.id, origin="builtin"
            ),
            _registration(
                tenant,
                model_key="alpha",
                model_id="mm-alpha",
                project_internal_id=alpha.internal_id,
                origin="builtin",
            ),
            _registration(
                tenant,
                model_key="beta",
                model_id="mm-beta",
                project_internal_id=beta.internal_id,
                origin="builtin",
            ),
        ]
    )
    session.flush()
    client = RecordingClient()

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=client,
    ).load(LoadContextRequest(project_slug="alpha"))

    assert {(bank_id, model_id) for bank_id, model_id, _ in client.calls} == {
        (juan.bank_id, "mm-juan"),
        (alpha.bank_id, "mm-alpha"),
    }
    assert "content:mm-juan" in result.text
    assert "content:mm-alpha" in result.text
    assert "mm-maria" not in result.text
    assert "mm-beta" not in result.text
    assert result.headings == ["User · juan", "Project Metadata", "Project · alpha"]


class ObservingClient(RecordingClient):
    """A backend whose refresh operation has already finished upstream."""

    def __init__(self, *, status: str = "completed"):
        super().__init__()
        self.status = status
        self.observed: list[tuple[str, str]] = []

    def get_operation(self, bank_id: str, operation_id: str) -> dict:
        self.observed.append((bank_id, operation_id))
        return {"operation_id": operation_id, "status": self.status}


def test_a_withheld_model_whose_refresh_finished_is_observed_and_delivered(
    session, tenant
):
    """The hook's own path has to be able to turn a model ready.

    `ach-memory context load` is all the SessionStart hook runs, and until
    now nothing on this path observed a finished refresh -- `get_mental_model`
    was the only caller that did. So a bank whose built-ins bootstrap had
    just registered delivered empty standing context for ever: measured
    2026-09-07, both built-ins sat withheld for 40 minutes after their
    operations had completed upstream, and a single `get_mental_model` call
    flipped each to ready at once.
    """
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    registration = _registration(
        tenant, model_key="juan", model_id="mm-juan", user_id=juan.id, origin="builtin"
    )
    registration.delivery_state = "withheld"
    registration.refresh_status = "pending"
    registration.refresh_operation_id = str(uuid.uuid4())
    session.add(registration)
    session.flush()
    client = ObservingClient()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=client
    ).load(LoadContextRequest())

    assert client.observed == [(juan.bank_id, registration.refresh_operation_id)]
    assert "content:mm-juan" in result.text
    assert registration.delivery_state == "ready"


def test_a_withheld_model_whose_refresh_is_unfinished_stays_out_of_context(
    session, tenant
):
    """Observing is not the same as delivering: a still-running refresh
    leaves the model withheld and its content unread, exactly as before."""
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    registration = _registration(
        tenant, model_key="juan", model_id="mm-juan", user_id=juan.id, origin="builtin"
    )
    registration.delivery_state = "withheld"
    registration.refresh_status = "pending"
    registration.refresh_operation_id = str(uuid.uuid4())
    session.add(registration)
    session.flush()
    client = ObservingClient(status="pending")

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=client
    ).load(LoadContextRequest())

    assert client.calls == []
    assert "content:mm-juan" not in result.text
    assert registration.delivery_state == "withheld"


def test_context_emits_only_current_claims_from_the_selected_banks(session, tenant):
    juan = _user(tenant, "usr_juan")
    maria = _user(tenant, "usr_maria")
    alpha = _project(tenant, juan.id, "alpha")
    beta = _project(tenant, maria.id, "beta")
    session.add_all([juan, maria, alpha, beta])
    session.flush()
    session.add_all(
        [
            _claim(tenant, content="JUAN_CURRENT", user_id=juan.id),
            _claim(tenant, content="JUAN_PENDING", user_id=juan.id, upstream_state="pending"),
            _claim(tenant, content="MARIA_CURRENT", user_id=maria.id),
            _claim(tenant, content="ALPHA_CURRENT", project_internal_id=alpha.internal_id),
            _claim(tenant, content="BETA_CURRENT", project_internal_id=beta.internal_id),
        ]
    )
    session.flush()

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="alpha"))

    assert "User · JUAN_CURRENT" in result.text
    assert "Project · ALPHA_CURRENT" in result.text
    for forbidden in ("JUAN_PENDING", "MARIA_CURRENT", "BETA_CURRENT"):
        assert forbidden not in result.text


def test_context_delivers_user_time_bounded_claims_without_a_project(
    session, tenant
):
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    session.add(_claim(tenant, content="USER_CURRENT", user_id=juan.id))
    session.flush()

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=RecordingClient(),
    ).load(LoadContextRequest())

    assert "User · USER_CURRENT" in result.text


def test_context_reports_a_withheld_bank_without_delivering_its_content(
    session, tenant
):
    juan = _user(tenant, "usr_juan")
    alpha = _project(tenant, juan.id, "alpha")
    session.add_all([juan, alpha])
    session.flush()
    session.add_all(
        [
            _registration(
                tenant, model_key="user", model_id="mm-user", user_id=juan.id, origin="builtin"
            ),
            _registration(
                tenant,
                model_key="project",
                model_id="mm-project",
                project_internal_id=alpha.internal_id,
                origin="builtin",
            ),
            _claim(tenant, content="PROJECT_CURRENT", project_internal_id=alpha.internal_id),
            BankCurrentness(
                tenant_id=tenant,
                scope="project",
                user_id=None,
                project_internal_id=alpha.internal_id,
                state="withheld",
                blocking_operation_id="op-unknown",
            ),
        ]
    )
    session.flush()
    client = RecordingClient()

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=client,
    ).load(LoadContextRequest(project_slug="alpha"))

    assert {(bank_id, model_id) for bank_id, model_id, _ in client.calls} == {
        (juan.bank_id, "mm-user")
    }
    assert "PROJECT_CURRENT" not in result.text
    assert ("project-bank", "bank_currentness_unavailable") in {
        (item.key, item.reason) for item in result.omissions
    }


def test_active_claims_are_included_whole_with_a_visible_omission_count(
    session, tenant
):
    juan = _user(tenant, "usr_juan")
    alpha = _project(tenant, juan.id, "alpha")
    session.add_all([juan, alpha])
    session.flush()
    claims = [
        _claim(
            tenant,
            content=f"CLAIM_{index} " + (f"word{index} " * 40),
            project_internal_id=alpha.internal_id,
        )
        for index in range(8)
    ]
    session.add_all(claims)
    session.flush()

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="alpha"))

    omission = next(item for item in result.omissions if item.key == "active-claims")
    assert omission.reason == "section_budget"
    assert omission.omitted_count
    assert f"[{omission.omitted_count} more active claims omitted; use recall]" in result.text
    for claim in claims:
        marker = claim.canonical_content.split()[0]
        if marker in result.text:
            assert claim.canonical_content.strip() in result.text


def test_model_reads_receive_the_remaining_deadline_and_do_not_hold_startup(
    session, tenant, monkeypatch
):
    from memory import context_service

    monkeypatch.setattr(context_service, "DEADLINE_SECONDS", 0.05)
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    session.add(
        _registration(
            tenant, model_key="slow", model_id="mm-slow", user_id=juan.id, origin="builtin"
        )
    )
    session.flush()
    client = RecordingClient(delay=0.2)

    started = time.monotonic()
    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=client,
    ).load(LoadContextRequest())
    elapsed = time.monotonic() - started

    assert elapsed < 0.15
    assert result.text == ""
    assert len(client.calls) == 1
    assert client.calls[0][2] is not None
    assert 0 < client.calls[0][2] <= 0.05
    # The active-claims phase runs AFTER the model-wait phase and must not
    # spend any of its own time once the shared deadline is already gone.
    assert {(item.key, item.reason) for item in result.omissions} == {
        ("slow", "model_unavailable"),
        ("active-claims", "deadline_exceeded"),
        # This request carries no project_slug, so the project half is
        # genuinely absent and now says so.
        ("project", "no_project_resolved"),
    }


def test_deadline_is_computed_once_and_never_resets_across_later_phases(
    session, tenant, monkeypatch
):
    """One shared deadline for the whole request, not one per phase: a slow
    peer that exhausts it must not let a LATER phase (Working State) start
    fresh with its own full budget."""
    from memory import context_service

    monkeypatch.setattr(context_service, "DEADLINE_SECONDS", 0.05)
    juan = _user(tenant, "usr_juan")
    alpha = _project(tenant, juan.id, "alpha")
    session.add_all([juan, alpha])
    session.flush()
    session.add_all(
        [
            _registration(
                tenant, model_key="fast", model_id="mm-fast", user_id=juan.id, origin="builtin"
            ),
            _registration(
                tenant,
                model_key="slow",
                model_id="mm-slow",
                project_internal_id=alpha.internal_id,
                origin="builtin",
            ),
        ]
    )
    workspace_id = "ws_" + "0" * 32
    session.add(
        WorkingState(
            tenant_id=tenant,
            user_id=juan.id,
            project_internal_id=alpha.internal_id,
            workspace_id=workspace_id,
            objective="should never be read once the deadline is gone",
            current_direction=None,
            recent_decisions=[],
            open_questions=[],
            next_steps=[],
            updated_at=datetime.now(UTC),
            session_id="s1",
            session_epoch=0,
            checkpoint_seq=0,
        )
    )
    session.flush()
    clock = _StepClock()
    client = _DeadlineProbeClient(clock, slow_model_id="mm-slow", advance_seconds=10.0)

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=client,
        clock=clock,
    ).load(LoadContextRequest(project_slug="alpha", workspace_id=workspace_id))

    assert "content:mm-fast" in result.text
    assert "User · fast" in result.headings
    assert "Working State" not in result.headings
    reasons = {(item.key, item.reason) for item in result.omissions}
    assert ("slow", "model_unavailable") in reasons
    assert ("working-state", "deadline_exceeded") in reasons


def test_no_budget_left_for_the_registry_query_still_emits_a_machine_readable_omission(
    session, tenant, monkeypatch
):
    """Every phase the deadline forces a skip on must say so -- the
    always-in-context registry query is no exception, even when it is
    skipped so early that no candidate model was ever identified."""
    from memory import context_service

    monkeypatch.setattr(context_service, "DEADLINE_SECONDS", 0.0)
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    session.add(_registration(tenant, model_key="m", model_id="mm-m", user_id=juan.id))
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest())

    assert ("always-in-context-models", "deadline_exceeded") in [
        (item.key, item.reason) for item in result.omissions
    ]


def test_a_request_with_no_project_says_the_project_half_is_absent(
    session, tenant
):
    """An empty `omissions` asserts nothing is missing, so a bare request that
    resolves no project must not answer with the user half alone and say
    nothing. The caller cannot otherwise tell an unscoped workspace from a
    project section that was dropped."""
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest())

    assert ("project", "no_project_resolved") in [
        (item.key, item.reason) for item in result.omissions
    ]


def test_a_request_for_an_absent_project_says_the_project_half_is_absent(
    session, tenant
):
    """load_context is one of the twelve read tools that map an absent
    project to empty (lazy-provisioning plan, decision 3): a project_slug
    that names nothing must not raise PROJECT_NOT_FOUND, only omit the
    project half exactly like a bare request does -- an agent does not know
    whether today is its first day, and its first call is this one, never
    retain."""
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="does-not-exist"))

    assert ("project", "no_project_resolved") in [
        (item.key, item.reason) for item in result.omissions
    ]


def test_a_request_for_a_foreign_project_says_the_project_half_is_absent(
    session, tenant
):
    """The oracle guard: a foreign project and an absent one must be
    indistinguishable here, exactly as everywhere else the twelve read tools
    reach a project through slug resolution."""
    juan = _user(tenant, "usr_juan")
    alice = _user(tenant, "usr_alice")
    session.add_all([juan, alice, _project(tenant, juan.id, "payments")])
    session.flush()

    result = ContextService(
        session, Principal(tenant, alice.id, False, "key_alice"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="payments"))

    assert ("project", "no_project_resolved") in [
        (item.key, item.reason) for item in result.omissions
    ]


def test_project_status_is_none_without_a_project_slug(session, tenant):
    """The agent stays blind either way (decision 4); a facade has nothing
    to log when no project was ever asked for, so this is None, not
    "absent"."""
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest())

    assert result.project_status is None


def test_project_status_is_ready_when_the_project_resolves(session, tenant):
    juan = _user(tenant, "usr_juan")
    session.add_all([juan, _project(tenant, juan.id, "payments")])
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="payments"))

    assert result.project_status == "ready"


def test_project_status_is_absent_for_an_unknown_project(session, tenant):
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="does-not-exist"))

    assert result.project_status == "absent"


def test_project_status_is_absent_for_a_forbidden_project(session, tenant):
    """The whole reason this field is safe: a facade may see "absent" for a
    project that actually exists and belongs to someone else, but never
    anything that would let it (or, through it, the agent) tell the two
    cases apart."""
    juan = _user(tenant, "usr_juan")
    alice = _user(tenant, "usr_alice")
    session.add_all([juan, alice, _project(tenant, juan.id, "payments")])
    session.flush()

    result = ContextService(
        session, Principal(tenant, alice.id, False, "key_alice"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="payments"))

    assert result.project_status == "absent"


def test_a_custom_model_is_never_delivered_even_with_the_flag_set(session, tenant):
    """Standing context is built-ins only. A custom model -- registered here
    exactly like an ordinary always-in-context row was before the flag was
    removed -- must not appear in a context load."""
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    session.add_all(
        [
            _registration(
                tenant,
                model_key="user-context",
                model_id="mm-builtin",
                user_id=juan.id,
                origin="builtin",
            ),
            _registration(
                tenant, model_key="custom", model_id="mm-custom", user_id=juan.id
            ),
        ]
    )
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest())

    assert "User · user-context" in result.headings
    assert "User · custom" not in result.headings


def test_a_disabled_builtin_is_not_delivered(session, tenant):
    """`disabled` is the only opt-out left once the flag is gone."""
    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    registration = _registration(
        tenant,
        model_key="user-context",
        model_id="mm-builtin",
        user_id=juan.id,
        origin="builtin",
    )
    registration.lifecycle_state = "disabled"
    session.add(registration)
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest())

    assert result.headings == []


def test_scope_project_omits_the_user_section(session, tenant):
    juan = _user(tenant, "usr_juan")
    alpha = _project(tenant, juan.id, "alpha")
    alpha.name = "Alpha"
    session.add_all([juan, alpha])
    session.flush()
    session.add_all(
        [
            _registration(
                tenant, model_key="user-context", model_id="mm-user",
                user_id=juan.id, origin="builtin",
            ),
            _registration(
                tenant, model_key="project-context", model_id="mm-project",
                project_internal_id=alpha.internal_id, origin="builtin",
            ),
        ]
    )
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="alpha", scope="project"))

    assert not any(heading.startswith("User") for heading in result.headings)
    assert "Project Metadata" in result.headings
    assert "Project · project-context" in result.headings


def test_scope_user_omits_the_project_section(session, tenant):
    juan = _user(tenant, "usr_juan")
    alpha = _project(tenant, juan.id, "alpha")
    alpha.name = "Alpha"
    session.add_all([juan, alpha])
    session.flush()
    session.add_all(
        [
            _registration(
                tenant, model_key="user-context", model_id="mm-user",
                user_id=juan.id, origin="builtin",
            ),
            _registration(
                tenant, model_key="project-context", model_id="mm-project",
                project_internal_id=alpha.internal_id, origin="builtin",
            ),
        ]
    )
    session.flush()

    result = ContextService(
        session, Principal(tenant, juan.id, False, "key_juan"), client=RecordingClient(),
    ).load(LoadContextRequest(project_slug="alpha", scope="user"))

    assert "User · user-context" in result.headings
    assert "Project Metadata" not in result.headings
    assert not any(heading.startswith("Project") for heading in result.headings)


def test_scope_defaults_to_both():
    """The default must not change what an existing caller receives."""
    assert LoadContextRequest().scope == "both"


def test_active_claims_fetches_a_bounded_prefix_not_the_whole_ledger(session, tenant):
    """1,000 active expiring claims must never be materialized whole: the
    repository query stays within the named bounded prefix, the response
    stays within its 4,608-token ceiling, and the reported omission counts
    every row not rendered -- fetched-but-trimmed AND never-fetched alike."""
    from memory.context_service import ACTIVE_CLAIMS_FETCH_LIMIT

    juan = _user(tenant, "usr_juan")
    session.add(juan)
    session.flush()
    session.add_all(
        [
            _claim(
                tenant,
                content=f"claim number {index:04d}",
                user_id=juan.id,
            )
            for index in range(1000)
        ]
    )
    session.flush()
    assert session.query(RetainedRecord).filter_by(tenant_id=tenant).count() == 1000

    result = ContextService(
        session,
        Principal(tenant, juan.id, False, "key_juan"),
        client=RecordingClient(),
    ).load(LoadContextRequest())

    assert result.total_tokens <= 4608
    omission = next(item for item in result.omissions if item.key == "active-claims")
    assert omission.reason == "section_budget"
    rendered_claims = sum(
        1 for index in range(1000) if f"claim number {index:04d}" in result.text
    )
    assert rendered_claims < ACTIVE_CLAIMS_FETCH_LIMIT
    assert omission.omitted_count == 1000 - rendered_claims
