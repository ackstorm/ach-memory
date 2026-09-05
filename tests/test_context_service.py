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
)
from memory.v040_contracts import LoadContextRequest


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
        origin="user",
        builtin_key=None,
        definition_version=None,
        lifecycle_state="active",
        always_in_context=True,
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
            _registration(tenant, model_key="juan", model_id="mm-juan", user_id=juan.id),
            _registration(tenant, model_key="maria", model_id="mm-maria", user_id=maria.id),
            _registration(
                tenant,
                model_key="alpha",
                model_id="mm-alpha",
                project_internal_id=alpha.internal_id,
            ),
            _registration(
                tenant,
                model_key="beta",
                model_id="mm-beta",
                project_internal_id=beta.internal_id,
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
            _registration(tenant, model_key="user", model_id="mm-user", user_id=juan.id),
            _registration(
                tenant,
                model_key="project",
                model_id="mm-project",
                project_internal_id=alpha.internal_id,
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
        _registration(tenant, model_key="slow", model_id="mm-slow", user_id=juan.id)
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
    assert [(item.key, item.reason) for item in result.omissions] == [
        ("slow", "model_unavailable")
    ]
