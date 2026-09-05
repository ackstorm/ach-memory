from dataclasses import replace
from unittest import mock

import pytest

from memory import ids
from memory.auth.principal import Principal
from memory.bootstrap import BootstrapRequest, bootstrap
from memory.builtin_models import USER_CONTEXT_V1
from memory.errors import CurationNeedsOperator, ProjectNotFound
from memory.hindsight.client import HindsightClient
from memory.mental_model_service import reconcile_builtin
from memory.model_registry import register_model
from memory.models import User
from memory.retained_records import LogicalBankRef

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


class _BankHolder:
    def __init__(self, bank: LogicalBankRef) -> None:
        self.bank = bank


@pytest.fixture
def disabled_builtin(session, principal):
    user = session.get(User, principal.user_id)
    bank = LogicalBankRef(principal.tenant_id, "user", user.id, None, user.bank_id)
    register_model(
        session,
        bank,
        origin="builtin",
        model_key=USER_CONTEXT_V1.key,
        name=USER_CONTEXT_V1.name,
        source_query=USER_CONTEXT_V1.source_query,
        source_tags=list(USER_CONTEXT_V1.source_tags),
        tags_match=USER_CONTEXT_V1.tags_match,
        max_tokens=USER_CONTEXT_V1.max_tokens,
        trigger=dict(USER_CONTEXT_V1.trigger),
        builtin_key=USER_CONTEXT_V1.key,
        definition_version=1,
        always_in_context=False,
        delivery_state="ready",
        upstream_model_id="mm-existing-1",
    )
    session.commit()
    return _BankHolder(bank)


@pytest.fixture
def v2_definition():
    return replace(
        USER_CONTEXT_V1, version=2, source_query="Summarize durable user context, v2."
    )


def test_builtin_upgrade_preserves_disabled_delivery(session, disabled_builtin, v2_definition, hindsight):
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-upgrade-1"}

    result = reconcile_builtin(session, disabled_builtin.bank, v2_definition, client=hindsight)

    assert result.definition_version == 2
    assert result.always_in_context is False
    assert result.source_query == v2_definition.source_query
    hindsight.update_mental_model.assert_called_once()
    hindsight.refresh_mental_model.assert_called_once()


def test_a_lifecycle_disabled_builtin_is_never_recreated_or_reconciled(session, principal, hindsight):
    user = session.get(User, principal.user_id)
    bank = LogicalBankRef(principal.tenant_id, "user", user.id, None, user.bank_id)
    register_model(
        session,
        bank,
        origin="builtin",
        model_key=USER_CONTEXT_V1.key,
        name=USER_CONTEXT_V1.name,
        source_query=USER_CONTEXT_V1.source_query,
        source_tags=list(USER_CONTEXT_V1.source_tags),
        tags_match=USER_CONTEXT_V1.tags_match,
        max_tokens=USER_CONTEXT_V1.max_tokens,
        trigger=dict(USER_CONTEXT_V1.trigger),
        builtin_key=USER_CONTEXT_V1.key,
        definition_version=1,
        always_in_context=True,
        delivery_state="ready",
        lifecycle_state="disabled",
    )
    session.commit()

    result = reconcile_builtin(session, bank, USER_CONTEXT_V1, client=hindsight)

    assert result.model_key == USER_CONTEXT_V1.key
    hindsight.create_mental_model.assert_not_called()
    hindsight.update_mental_model.assert_not_called()


def test_reconcile_builtin_is_a_noop_when_already_current(session, principal, hindsight):
    first = reconcile_builtin(
        session,
        LogicalBankRef(
            principal.tenant_id, "user", principal.user_id, None,
            session.get(User, principal.user_id).bank_id,
        ),
        USER_CONTEXT_V1,
        client=hindsight,
    )
    second = reconcile_builtin(
        session,
        LogicalBankRef(
            principal.tenant_id, "user", principal.user_id, None,
            session.get(User, principal.user_id).bank_id,
        ),
        USER_CONTEXT_V1,
        client=hindsight,
    )

    assert first.model_key == second.model_key
    hindsight.create_mental_model.assert_called_once()
    hindsight.update_mental_model.assert_not_called()
    hindsight.refresh_mental_model.assert_not_called()
