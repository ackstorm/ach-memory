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
        tenant_id=settings.tenant_id,
    )


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def _registration(
    tenant: str,
    *,
    key: str,
    upstream_id: str,
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
        source_query="Summarize current disposable test facts.",
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


def test_maximum_context_selection_is_live_parallel_and_bounded(session, tenant):
    """Nine live model GETs plus deterministic sections stay inside two seconds."""
    client = _client()
    suffix = uuid.uuid4().hex[:16]
    user_bank = f"v040-context-user-{suffix}"
    project_bank = f"v040-context-project-{suffix}"
    created_banks = [user_bank, project_bank]
    now = datetime.now(UTC)

    try:
        user = User(
            id=f"usr_v040context{suffix}", tenant_id=tenant, bank_id=user_bank
        )
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
            ProjectSlug(
                tenant_id=tenant,
                slug=f"context-{suffix}",
                is_canonical=True,
            )
        )
        session.add_all([user, project])
        session.flush()

        registrations = []
        for scope, bank, count in (
            ("user", user_bank, 4),
            ("project", project_bank, 5),
        ):
            for index in range(count):
                created = client.create_mental_model(
                    bank,
                    name=f"ach:context-{scope}-{index}",
                    source_query="Summarize current disposable test facts.",
                    max_tokens=256,
                    tags=["schema:ach-retain-v1", "validity:indefinite"],
                )
                upstream_id = created.get("mental_model_id") or created.get("id")
                assert isinstance(upstream_id, str)
                registrations.append(
                    _registration(
                        tenant,
                        key=f"{scope}-{index}",
                        upstream_id=upstream_id,
                        user_id=user.id if scope == "user" else None,
                        project_internal_id=(
                            project.internal_id if scope == "project" else None
                        ),
                    )
                )
        session.add_all(registrations)
        session.add(_expiring_claim(tenant, project.internal_id, now))
        workspace_id = "ws_" + "4" * 32
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
        request = LoadContextRequest(
            project_slug=f"context-{suffix}", workspace_id=workspace_id
        )
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
        assert not [
            item for item in last.omissions if item.reason == "model_unavailable"
        ]
    finally:
        for bank in created_banks:
            try:
                client.delete_bank(bank)
            except Exception as exc:  # noqa: BLE001 -- cleanup must not hide the gate
                print(f"ach-memory: disposable context cleanup failed: {exc}")
