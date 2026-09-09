from unittest import mock

import httpx
import pytest
import respx
from sqlalchemy import text

from memory import ids, model_registry
from memory.auth.principal import Principal
from memory.bootstrap import BootstrapRequest, bootstrap
from memory.builtin_models import USER_CONTEXT
from memory.errors import CurationNeedsOperator, ProjectNotFound
from memory.hindsight.client import HindsightClient
from memory.mental_model_service import reconcile_builtin
from memory.model_registry import register_model
from memory.models import Project, ProjectSlug, User
from memory.retained_records import LogicalBankRef

BASE = "http://hindsight.test"


def _make_user_key(client, master_headers) -> str:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    return client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]


def _project_by_slug(session, slug: str) -> Project:
    mapping = session.query(ProjectSlug).filter(ProjectSlug.slug == slug).one()
    return session.get(Project, mapping.project_internal_id)


def _project_bank_ref(session, project: Project) -> LogicalBankRef:
    return LogicalBankRef(
        project.tenant_id, "project", None, project.internal_id, project.bank_id
    )


def _user_bank_ref(session, user_id: str) -> LogicalBankRef:
    user = session.get(User, user_id)
    return LogicalBankRef(user.tenant_id, "user", user.id, None, user.bank_id)

EXACT_STRATEGY = {
    "retain_extraction_mode": "chunks",
    "retain_chunk_size": 4096,
    "retain_structured_chunk_size": 4096,
}


@pytest.fixture
def principal(session, tenant):
    user = User(id="usr_boot", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return Principal(
        tenant_id=tenant, user_id=user.id, is_master=False,
        key_id="key_boot", credential_id="key_boot",
    )


@pytest.fixture
def hindsight():
    client = mock.MagicMock(spec=HindsightClient)
    counter = iter(range(1, 1000))
    client.create_mental_model.side_effect = lambda *a, **k: {
        "mental_model_id": f"mm-upstream-{next(counter)}",
        "operation_id": f"op-upstream-{next(counter)}",
    }
    client.list_mental_models.return_value = {"items": []}
    client.get_bank_config.return_value = {
        "config": {"retain_strategies": {"ach-exact-v1": EXACT_STRATEGY}},
        "overrides": {},
    }
    return client


def test_bootstrap_provisions_exact_strategy_when_builtins_are_disabled(
    session, principal, hindsight
):
    hindsight.get_bank_config.side_effect = [
        {"config": {"retain_strategies": None}, "overrides": {}},
        {"config": {"retain_strategies": {"ach-exact-v1": EXACT_STRATEGY}}, "overrides": {}},
    ]

    result = bootstrap(
        session, principal, BootstrapRequest(builtins_enabled=False), client=hindsight
    )

    assert result.user_model is None
    hindsight.update_bank_config.assert_called_once_with(
        hindsight.ensure_bank.call_args.args[0],
        {"retain_strategies": {"ach-exact-v1": EXACT_STRATEGY}},
    )


def test_user_bootstrap_creates_one_builtin_idempotently(session, principal, hindsight):
    first = bootstrap(session, principal, BootstrapRequest(), client=hindsight)
    second = bootstrap(session, principal, BootstrapRequest(), client=hindsight)

    assert first.user_model.model_key == second.user_model.model_key == "user-context"
    assert hindsight.create_mental_model.call_count == 1


def test_project_bootstrap_creates_user_owned_project_and_warns(session, principal, hindsight, caplog):
    result = bootstrap(session, principal, BootstrapRequest(project_slug="Pepe"), client=hindsight)

    assert result.project_slug == "pepe"
    assert result.project_owner.model_dump() == {"type": "user", "id": principal.user_id}
    assert "creation_source=mcp_bootstrap" in caplog.text
    assert not hasattr(result, "project_bank_id")
    assert result.project_status == "ready"
    assert result.project_model.model_key == "project-context"


def test_project_bootstrap_is_idempotent_and_reuses_the_project(session, principal, hindsight):
    first = bootstrap(session, principal, BootstrapRequest(project_slug="acme"), client=hindsight)
    second = bootstrap(session, principal, BootstrapRequest(project_slug="acme"), client=hindsight)

    assert first.project_slug == second.project_slug == "acme"
    # user-context + project-context, created once each across both calls.
    assert hindsight.create_mental_model.call_count == 2


def test_project_bootstrap_on_an_unauthorized_existing_slug_is_a_404(session, principal, hindsight, tenant):
    other = User(id="usr_other", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(other)
    session.flush()
    other_principal = Principal(
        tenant_id=tenant, user_id=other.id, is_master=False,
        key_id="key_other", credential_id="key_other",
    )
    bootstrap(session, other_principal, BootstrapRequest(project_slug="private-proj"), client=hindsight)

    with pytest.raises(ProjectNotFound):
        bootstrap(session, principal, BootstrapRequest(project_slug="private-proj"), client=hindsight)


def test_builtins_disabled_creates_no_models(session, principal, hindsight):
    result = bootstrap(session, principal, BootstrapRequest(builtins_enabled=False), client=hindsight)

    assert result.user_model is None
    hindsight.create_mental_model.assert_not_called()


def test_builtin_create_refuses_a_colliding_unknown_upstream_model(session, principal, hindsight):
    hindsight.list_mental_models.return_value = {
        "items": [{"id": "mm-mystery", "name": "ach:user-context"}]
    }

    with pytest.raises(CurationNeedsOperator):
        bootstrap(session, principal, BootstrapRequest(), client=hindsight)
    hindsight.create_mental_model.assert_not_called()


def test_a_lifecycle_disabled_builtin_is_never_recreated_or_reconciled(session, principal, hindsight):
    user = session.get(User, principal.user_id)
    bank = LogicalBankRef(principal.tenant_id, "user", user.id, None, user.bank_id)
    register_model(
        session,
        bank,
        origin="builtin",
        model_key=USER_CONTEXT.key,
        name=USER_CONTEXT.name,
        source_query=USER_CONTEXT.source_query,
        source_tags=list(USER_CONTEXT.source_tags),
        tags_match=USER_CONTEXT.tags_match,
        max_tokens=USER_CONTEXT.max_tokens,
        trigger=dict(USER_CONTEXT.trigger),
        builtin_key=USER_CONTEXT.key,
        definition_version=1,
        delivery_state="ready",
        lifecycle_state="disabled",
    )
    session.commit()

    result = reconcile_builtin(session, bank, USER_CONTEXT, client=hindsight)

    assert result.model_key == USER_CONTEXT.key
    hindsight.create_mental_model.assert_not_called()
    hindsight.update_mental_model.assert_not_called()


def test_a_builtin_stuck_in_creating_is_repaired_by_the_next_reconcile(
    session, principal, hindsight
):
    """_create_builtin commits the row before calling Hindsight, so a failed
    or lost create leaves it in `creating` with no upstream id. Nothing used
    to move it: the definition version already matched, so reconcile returned
    the stuck row unchanged for ever -- and standing delivery requires
    `active`, so that bank served no standing context at all."""
    user = session.get(User, principal.user_id)
    bank = LogicalBankRef(principal.tenant_id, "user", user.id, None, user.bank_id)
    register_model(
        session,
        bank,
        origin="builtin",
        model_key=USER_CONTEXT.key,
        name=USER_CONTEXT.name,
        source_query=USER_CONTEXT.source_query,
        source_tags=list(USER_CONTEXT.source_tags),
        tags_match=USER_CONTEXT.tags_match,
        max_tokens=USER_CONTEXT.max_tokens,
        trigger=dict(USER_CONTEXT.trigger),
        builtin_key=USER_CONTEXT.key,
        definition_version=USER_CONTEXT.version,
        delivery_state="ready",
        lifecycle_state="creating",
    )
    session.commit()

    result = reconcile_builtin(session, bank, USER_CONTEXT, client=hindsight)

    assert result.model_key == USER_CONTEXT.key
    row = model_registry.get_registered_model(session, bank, USER_CONTEXT.key)
    assert row.lifecycle_state == "active"
    # The fixture lists no upstream models, so the name is free and the
    # repair recreates rather than adopting.
    assert row.upstream_model_id
    hindsight.create_mental_model.assert_called_once()


def test_reconcile_builtin_is_a_noop_when_already_current(session, principal, hindsight):
    first = reconcile_builtin(
        session,
        LogicalBankRef(
            principal.tenant_id, "user", principal.user_id, None,
            session.get(User, principal.user_id).bank_id,
        ),
        USER_CONTEXT,
        client=hindsight,
    )
    second = reconcile_builtin(
        session,
        LogicalBankRef(
            principal.tenant_id, "user", principal.user_id, None,
            session.get(User, principal.user_id).bank_id,
        ),
        USER_CONTEXT,
        client=hindsight,
    )

    assert first.model_key == second.model_key
    hindsight.create_mental_model.assert_called_once()
    hindsight.update_mental_model.assert_not_called()
    hindsight.refresh_mental_model.assert_not_called()


@respx.mock
def test_a_project_created_through_the_control_plane_is_fully_provisioned(
    client, session, master_headers
):
    """A project is usable when created, not when someone remembers to
    bootstrap it. Before this, POST /v1/projects minted a bank id and
    nothing else: no retain strategy, no project-context model, so
    load_context delivered empty standing context for that project for ever."""
    key = _make_user_key(client, master_headers)
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201, json={"mental_model_id": "mm-upstream-1", "operation_id": "op-upstream-1"}
        )
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    response = client.post(
        "/v1/projects",
        json={"project_slug": "acme-app"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 201, response.text

    project = _project_by_slug(session, "acme-app")
    registration = model_registry.get_registered_model(
        session, _project_bank_ref(session, project), "project-context"
    )
    assert registration is not None, "project-context must exist at creation"
    assert registration.origin == "builtin"
    assert registration.lifecycle_state in {"creating", "active"}


@respx.mock
def test_a_failed_provisioning_still_commits_the_project(
    client, session, master_headers, monkeypatch
):
    """The route promises "the project row is real and the caller gets its
    201". A DB-level failure inside provisioning -- an IntegrityError from a
    concurrent bootstrap registering the same built-in -- used to poison the
    session, so the db.commit() after the except raised PendingRollbackError
    and the caller got a 500 with nothing committed: the exact opposite of
    the promise. The savepoint is what makes the comment true."""
    from sqlalchemy.exc import IntegrityError

    key = _make_user_key(client, master_headers)

    def poison(db, principal, project, *, client):
        db.execute(text("SELECT 1"))  # a real statement, so the session is live
        raise IntegrityError("duplicate built-in", None, Exception())

    # The route binds the name at import, so this is the target that matters.
    monkeypatch.setattr("memory.api.projects.provision_project_bank", poison)

    response = client.post(
        "/v1/projects",
        json={"project_slug": "acme-unprovisioned"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 201, response.text
    assert _project_by_slug(session, "acme-unprovisioned") is not None


@respx.mock
def test_a_new_user_gets_user_context_without_a_bootstrap_call(client, session, master_headers):
    """Same rule as projects: a bank is provisioned when it is created."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201, json={"mental_model_id": "mm-upstream-1", "operation_id": "op-upstream-1"}
        )
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    registration = model_registry.get_registered_model(
        session, _user_bank_ref(session, user_id), "user-context"
    )
    assert registration is not None
