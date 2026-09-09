from unittest import mock

import httpx
import pytest
import respx

from memory import ids, mental_model_service, model_registry, projects
from memory.auth.principal import Principal
from memory.bootstrap import BootstrapRequest, bootstrap
from memory.builtin_models import USER_CONTEXT
from memory.errors import CurationNeedsOperator
from memory.hindsight.client import HindsightClient
from memory.mental_model_service import reconcile_builtin
from memory.model_registry import register_model
from memory.models import ExternalIdentity, Project, ProjectSlug, User
from memory.retained_records import LogicalBankRef
from tests.conftest import IDENTITY_HEADER, RESOLVER_URL, identity_token

BASE = "http://hindsight.test"


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
    return Principal(tenant_id=tenant, user_id=user.id, credential_id="ext_boot")


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


def test_project_bootstrap_pre_warms_an_existing_project(session, principal, hindsight):
    projects.create(session, principal, "Pepe", "user", principal.user_id)

    result = bootstrap(session, principal, BootstrapRequest(project_slug="Pepe"), client=hindsight)

    assert result.project_slug == "pepe"
    assert result.project_owner.model_dump() == {"type": "user", "id": principal.user_id}
    assert not hasattr(result, "project_bank_id")
    assert result.project_status == "ready"
    assert result.project_model.model_key == "project-context"


def test_project_bootstrap_never_creates_a_project(session, principal, hindsight, caplog):
    """A pre-warm warms what exists. `retain` is the one place allowed to mint
    an unknown slug, and it is the place with the audited hourly ceiling --
    creating here spent one of those every time an MCP session started, for a
    configured slug the agent might never write to."""
    result = bootstrap(session, principal, BootstrapRequest(project_slug="never-seen"), client=hindsight)

    assert session.query(ProjectSlug).filter_by(slug="never-seen").first() is None
    assert result.project_slug is None
    assert result.project_status is None
    assert "creation_source=mcp_bootstrap" not in caplog.text
    # The user bank is still warmed: that is what this call is for.
    assert result.user_model.model_key == "user-context"


def test_project_bootstrap_is_idempotent_and_reuses_the_project(session, principal, hindsight):
    projects.create(session, principal, "acme", "user", principal.user_id)

    first = bootstrap(session, principal, BootstrapRequest(project_slug="acme"), client=hindsight)
    second = bootstrap(session, principal, BootstrapRequest(project_slug="acme"), client=hindsight)

    assert first.project_slug == second.project_slug == "acme"
    # user-context + project-context, created once each across both calls.
    assert hindsight.create_mental_model.call_count == 2


def test_project_bootstrap_on_an_unauthorized_slug_reports_nothing(session, principal, hindsight, tenant):
    """A foreign project and an absent one must look identical here. Raising
    on the foreign one would make bootstrap an existence oracle; it would also
    make the proxy poison every project-scoped tool call at startup, including
    the `retain` that is supposed to create the absent one."""
    other = User(id="usr_other", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(other)
    session.flush()
    other_principal = Principal(
        tenant_id=tenant, user_id=other.id, credential_id="ext_other"
    )
    projects.create(session, other_principal, "private-proj", "user", other.id)

    foreign = bootstrap(session, principal, BootstrapRequest(project_slug="private-proj"), client=hindsight)
    absent = bootstrap(session, principal, BootstrapRequest(project_slug="no-such-proj"), client=hindsight)

    assert foreign.model_dump() == absent.model_dump()
    assert foreign.project_status is None


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
    client, session, new_user, monkeypatch
):
    """A project is usable when created, not when someone remembers to
    bootstrap it. Before this, POST /v1/projects minted a bank id and
    nothing else: no retain strategy, no project-context model, so
    load_context delivered empty standing context for that project for ever."""
    # The app fixture stubs reconcile_builtin out by default for unrelated
    # route tests -- this test is specifically about what it does, so restore
    # the real function.
    monkeypatch.setattr(mental_model_service, "reconcile_builtin", reconcile_builtin)
    # Bank-id-agnostic on purpose: the only real upstream call here is the
    # project bank's builtin registration below.
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201, json={"mental_model_id": "mm-upstream-1", "operation_id": "op-upstream-1"}
        )
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    headers = new_user()["headers"]

    response = client.post(
        "/v1/projects", json={"project_slug": "acme-app"}, headers=headers
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
def test_a_failed_provisioning_fails_the_create(
    client, session, new_user, monkeypatch
):
    """A 201 means the bank is usable. Provisioning is what makes it usable,
    so a caller must never be told a project is ready when its bank has no
    retain strategy and no built-in model -- they would write into a bank
    that delivers nothing and never learn why."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201, json={"mental_model_id": "mm-upstream-1", "operation_id": "op-upstream-1"}
        )
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    headers = new_user()["headers"]

    def fail(db, principal, project, *, client):
        raise RuntimeError("hindsight is down")

    # The route binds the name at import, so this is the target that matters.
    monkeypatch.setattr("memory.api.projects.provision_project_bank", fail)

    response = client.post(
        "/v1/projects", json={"project_slug": "acme-app"}, headers=headers
    )

    assert response.status_code >= 500
    assert (
        session.query(ProjectSlug).filter_by(slug="acme-app").first() is None
    ), "the row must not survive"


@respx.mock
def test_a_new_user_is_linked_but_not_provisioned_until_bootstrap(
    client, session, monkeypatch
):
    """The opposite rule to projects above, and deliberately so.

    `link_identity` runs on the authentication path of EVERY externally
    authenticated request, so it creates the `User` row on first sight but
    never provisions its bank -- a Hindsight round trip there would put
    upstream latency and failure modes on ordinary auth. `POST /v1/bootstrap`
    is the only thing that provisions a caller's own bank, which is why the
    pre-warm survived the removal of the internal identity system: without it
    a brand-new user's first `load_context` finds no `user-context` at all.
    """
    monkeypatch.setattr(mental_model_service, "reconcile_builtin", reconcile_builtin)
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201, json={"mental_model_id": "mm-upstream-1", "operation_id": "op-upstream-1"}
        )
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    subject = "newcomer@test"
    headers = {IDENTITY_HEADER: identity_token(subject)}

    # A first authenticated write that is NOT bootstrap: enough to link the
    # identity and commit it, never enough to provision the user's own bank.
    created = client.post(
        "/v1/projects", json={"project_slug": "acme-app"}, headers=headers
    )
    assert created.status_code == 201, created.text

    identity = session.get(ExternalIdentity, (RESOLVER_URL, subject))
    assert identity is not None, "first sight must create the User row"
    bank = _user_bank_ref(session, identity.user_id)
    assert model_registry.get_registered_model(session, bank, "user-context") is None

    warmed = client.post("/v1/bootstrap", json={}, headers=headers)

    assert warmed.status_code == 200, warmed.text
    assert model_registry.get_registered_model(session, bank, "user-context") is not None
