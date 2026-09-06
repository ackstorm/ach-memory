"""Disposable live latency proof for bounded standing-context delivery."""

from __future__ import annotations

import math
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from memory.auth.principal import Principal
from memory.builtin_models import PROJECT_CONTEXT_V1, USER_CONTEXT_V1
from memory.context_service import ContextService
from memory.hindsight.client import HindsightClient
from memory.models import (
    MentalModelRegistration,
    Project,
    ProjectSlug,
    RetainedRecord,
    User,
    WorkingState,
)
from memory.retained_records import LogicalBankRef
from memory.v040_contracts import LoadContextRequest

pytestmark = pytest.mark.integration

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_CALLS = 30


def _client() -> HindsightClient:
    if os.environ.get("HINDSIGHT_V040_CONFIRM") != "disposable-banks-only":
        pytest.skip("set HINDSIGHT_V040_CONFIRM=disposable-banks-only")
    from memory.config import get_settings

    settings = get_settings()
    host = urlsplit(settings.hindsight_url).hostname
    if host not in _LOOPBACK:
        pytest.fail("live context gate only accepts a loopback Hindsight target")
    return HindsightClient(
        base_url=settings.hindsight_url,
        api_key=settings.hindsight_api_key,
    )


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def _wait_for_synthesis(
    client: HindsightClient, bank_id: str, operation_id: str, *, timeout: float = 30.0
) -> None:
    """A freshly created or refreshed mental model answers a GET with a
    "Generating content..." placeholder until Hindsight's own synthesis
    finishes -- wait for the exact operation this model's creation returned
    before trusting `get_mental_model`'s content in an assertion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get_operation(bank_id, operation_id).get("status")
        if status == "completed":
            return
        if status == "failed":
            raise AssertionError(f"model synthesis operation {operation_id} failed")
        time.sleep(0.5)
    raise AssertionError(f"model synthesis operation {operation_id} did not complete in {timeout}s")


def _registration(
    tenant: str,
    *,
    key: str,
    upstream_id: str,
    marker: str,
    user_id: str | None = None,
    project_internal_id: str | None = None,
) -> MentalModelRegistration:
    return MentalModelRegistration(
        tenant_id=tenant,
        scope="user" if user_id else "project",
        user_id=user_id,
        project_internal_id=project_internal_id,
        model_key=key,
        upstream_model_id=upstream_id,
        name=key,
        # A unique harmless marker per model, embedded in its own source
        # query -- lets a live assertion tie a specific delivered section
        # back to the exact registration that produced it.
        source_query=f"Summarize current disposable test facts. Marker: {marker}.",
        source_tags=["schema:ach-retain-v1", "validity:indefinite"],
        tags_match="all",
        max_tokens=256,
        trigger={},
        origin="user",
        builtin_key=None,
        definition_version=None,
        lifecycle_state="active",
        always_in_context=True,
        delivery_state="ready",
    )


def _expiring_claim(
    tenant: str, project_internal_id: str, now: datetime
) -> RetainedRecord:
    marker = uuid.uuid4().hex
    return RetainedRecord(
        tenant_id=tenant,
        scope="project",
        user_id=None,
        project_internal_id=project_internal_id,
        operation_id=f"context-live-{marker}",
        payload_hash=marker,
        document_id=f"ach-retain-{marker}",
        source_memory_id=f"mem-{marker}",
        canonical_content="The disposable context latency probe is active.",
        memory_type="constraint",
        basis="human_explicit",
        trigger="user_requested",
        sanitized_evidence=[],
        recorded_at=now,
        valid_from=now,
        valid_until=now + timedelta(hours=1),
        lifecycle="active",
        upstream_state="completed",
    )


def _register_builtin(
    session, tenant: str, client: HindsightClient, *, bank_id: str, user_id: str | None, project_internal_id: str | None
) -> MentalModelRegistration:
    """Create a real upstream builtin model, registered exactly as
    `mental_model_service.reconcile_builtin` would for a fresh bank."""
    from memory import mental_model_service

    scope = "user" if user_id else "project"
    definition = USER_CONTEXT_V1 if scope == "user" else PROJECT_CONTEXT_V1
    bank = LogicalBankRef(tenant, scope, user_id, project_internal_id, bank_id)
    mental_model_service.reconcile_builtin(session, bank, definition, client=client)
    session.commit()
    from memory import model_registry

    row = model_registry.get_registered_model(session, bank, definition.key)
    assert row is not None
    assert row.refresh_operation_id is not None
    _wait_for_synthesis(client, bank_id, row.refresh_operation_id)
    # A builtin starts `delivery_state="withheld"` exactly like a fresh
    # custom model -- observe the now-completed operation to transition it
    # to "ready", the same way a real GET /v1/mental-models/{key} would.
    mental_model_service.observe_model_refresh(session, bank, definition.key, client=client)
    session.commit()
    row = model_registry.get_registered_model(session, bank, definition.key)
    assert row is not None
    return row


def _build_context_probe(
    session,
    tenant: str,
    client: HindsightClient,
    *,
    suffix: str,
    user_custom_count: int,
    project_custom_count: int,
    include_builtins: bool,
) -> tuple[Principal, LoadContextRequest, list[str], dict[str, str]]:
    """Seed one disposable user+project bank with the requested model mix.

    Returns (principal, request, created_bank_ids, heading_by_model_key) --
    a model's `source_query` is a synthesis PROMPT over the bank's actual
    retained content, never text echoed verbatim into its output, so the
    delivered proof per model is that its OWN section heading is present
    (proving its content actually reached the response), not that some
    caller-chosen marker string survived LLM synthesis.
    """
    user_bank = f"v040-context-user-{suffix}"
    project_bank = f"v040-context-project-{suffix}"
    now = datetime.now(UTC)

    user = User(id=f"usr_v040context{suffix}", tenant_id=tenant, bank_id=user_bank)
    project = Project(
        internal_id=f"prj_v040context{suffix}",
        tenant_id=tenant,
        owner_type="user",
        owner_id=user.id,
        bank_id=project_bank,
        name="Disposable context probe",
        purpose="Measure parallel standing-context delivery.",
    )
    project.slug_rows.append(
        ProjectSlug(tenant_id=tenant, slug=f"context-{suffix}", is_canonical=True)
    )
    session.add_all([user, project])
    session.flush()

    headings: dict[str, str] = {}
    registrations = []
    if include_builtins:
        for scope, bank_id, user_id, project_internal_id in (
            ("user", user_bank, user.id, None),
            ("project", project_bank, None, project.internal_id),
        ):
            row = _register_builtin(
                session, tenant, client, bank_id=bank_id, user_id=user_id, project_internal_id=project_internal_id
            )
            headings[row.model_key] = f"{scope.title()} · {row.model_key}"

    for scope, bank_id, count in (
        ("user", user_bank, user_custom_count),
        ("project", project_bank, project_custom_count),
    ):
        for index in range(count):
            key = f"{scope}-{index}-{suffix}"
            created = client.create_mental_model(
                bank_id,
                name=f"ach:context-{scope}-{index}-{suffix}",
                source_query="Summarize current disposable test facts.",
                max_tokens=256,
                tags=["schema:ach-retain-v1", "validity:indefinite"],
            )
            upstream_id = created.get("mental_model_id") or created.get("id")
            operation_id = created.get("operation_id")
            assert isinstance(upstream_id, str)
            assert isinstance(operation_id, str)
            _wait_for_synthesis(client, bank_id, operation_id)
            registrations.append(
                _registration(
                    tenant,
                    key=key,
                    upstream_id=upstream_id,
                    marker=key,
                    user_id=user.id if scope == "user" else None,
                    project_internal_id=project.internal_id if scope == "project" else None,
                )
            )
            headings[key] = f"{scope.title()} · {key}"
    session.add_all(registrations)
    session.add(_expiring_claim(tenant, project.internal_id, now))
    workspace_id = "ws_" + suffix[:8].ljust(32, "0")
    session.add(
        WorkingState(
            tenant_id=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id=workspace_id,
            objective="Verify live standing-context latency.",
            current_direction=None,
            recent_decisions=[],
            open_questions=[],
            next_steps=["Record the measured p95."],
            updated_at=now,
            session_id="context-live",
            session_epoch=1,
            checkpoint_seq=1,
        )
    )
    session.flush()

    principal = Principal(
        tenant_id=tenant,
        user_id=user.id,
        is_master=False,
        key_id="key_v040context",
        credential_id="key_v040context",
    )
    request = LoadContextRequest(project_slug=f"context-{suffix}", workspace_id=workspace_id)
    return principal, request, [user_bank, project_bank], headings


def _delete_banks(client: HindsightClient, banks: list[str]) -> None:
    for bank in banks:
        last_error = None
        for _ in range(5):
            try:
                client.delete_bank(bank)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 -- bounded disposable cleanup retry
                last_error = exc
                time.sleep(0.5)
        if last_error is not None:
            raise AssertionError(
                "could not delete a disposable context-test bank"
            ) from last_error


def _assert_bounded_and_delivered(session, principal, request, client, headings: dict[str, str]) -> None:
    service = ContextService(session, principal, client=client)

    service.load(request)
    durations = []
    last = None
    for _ in range(_CALLS):
        started = time.monotonic()
        last = service.load(request)
        durations.append(time.monotonic() - started)

    p95 = _p95(durations)
    print(f"context_live_p95_seconds={p95:.6f}")
    assert p95 <= 2.0
    assert last is not None
    assert last.total_tokens <= 4608
    assert "Project Metadata" in last.headings
    assert "Active Time-Bounded Claims" in last.headings
    assert "Working State" in last.headings
    assert not [item for item in last.omissions if item.reason == "model_unavailable"]
    for heading in headings.values():
        assert heading in last.headings


def test_maximum_context_selection_is_live_parallel_and_bounded(session, tenant):
    """Nine live model GETs plus deterministic sections stay inside two seconds."""
    client = _client()
    suffix = uuid.uuid4().hex[:16]
    principal, request, banks, headings = _build_context_probe(
        session, tenant, client, suffix=suffix,
        user_custom_count=4, project_custom_count=5, include_builtins=False,
    )
    try:
        _assert_bounded_and_delivered(session, principal, request, client, headings)
    finally:
        _delete_banks(client, banks)


def test_default_like_context_selection_is_live_parallel_and_bounded(session, tenant):
    """The realistic mix -- both built-ins plus a couple of custom models
    per bank, not the maxed-out quota -- stays inside two seconds too."""
    client = _client()
    suffix = uuid.uuid4().hex[:16]
    principal, request, banks, headings = _build_context_probe(
        session, tenant, client, suffix=suffix,
        user_custom_count=2, project_custom_count=4, include_builtins=True,
    )
    try:
        _assert_bounded_and_delivered(session, principal, request, client, headings)
    finally:
        _delete_banks(client, banks)


class _OneSlowModelClient:
    """Delegates every call to the REAL disposable Hindsight client except
    `get_mental_model` for one chosen upstream id, which sleeps well past
    the two-second deadline -- proving the deadline fails this ONE peer open
    without hiding or delaying anyone else."""

    def __init__(self, real: HindsightClient, *, slow_upstream_id: str):
        self._real = real
        self._slow_upstream_id = slow_upstream_id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get_mental_model(self, bank_id: str, model_id: str, *, timeout: float | None = None):
        if model_id == self._slow_upstream_id:
            time.sleep(3.0)
        return self._real.get_mental_model(bank_id, model_id, timeout=timeout)


def test_a_controlled_slow_model_fails_open_without_hiding_peers(session, tenant):
    """One model's GET is wrapped to sleep past the deadline. Peers and
    Project Metadata (no I/O of its own) must still return; a single peer
    genuinely exceeding the shared two-second budget correctly consumes it
    entirely (SPEC's one shared deadline, not one per phase) -- Active
    Claims and Working State are only ever entitled to whatever the model
    wait phase leaves behind, so this proves they are cleanly OMITTED with
    a `deadline_exceeded` reason rather than silently missing or the whole
    request blowing past its bound."""
    real_client = _client()
    suffix = uuid.uuid4().hex[:16]
    principal, request, banks, headings = _build_context_probe(
        session, tenant, real_client, suffix=suffix,
        user_custom_count=2, project_custom_count=2, include_builtins=False,
    )
    try:
        from memory import model_registry
        from memory.retained_records import LogicalBankRef

        user_bank_id = banks[0]
        slow_key = f"user-0-{suffix}"
        slow_bank = LogicalBankRef(tenant, "user", principal.user_id, None, user_bank_id)
        slow_row = model_registry.get_registered_model(session, slow_bank, slow_key)
        assert slow_row is not None and slow_row.upstream_model_id is not None
        slow_client = _OneSlowModelClient(real_client, slow_upstream_id=slow_row.upstream_model_id)

        started = time.monotonic()
        result = ContextService(session, principal, client=slow_client).load(request)
        elapsed = time.monotonic() - started

        assert elapsed <= 2.5
        assert "Project Metadata" in result.headings
        for key, heading in headings.items():
            if key != slow_key:
                assert heading in result.headings
        assert headings[slow_key] not in result.headings
        omissions_by_key = {item.key: item.reason for item in result.omissions}
        assert omissions_by_key["user-0-" + suffix] == "model_unavailable"
        assert sum(reason == "model_unavailable" for reason in omissions_by_key.values()) == 1
        # Whatever budget the slow peer left behind is 0 by construction (it
        # blocked for the shared deadline's full duration) -- both later
        # phases must say so explicitly, never appear to have just been
        # skipped for no recorded reason.
        assert omissions_by_key.get("active-claims") == "deadline_exceeded"
        assert omissions_by_key.get("working-state") == "deadline_exceeded"
    finally:
        _delete_banks(real_client, banks)
