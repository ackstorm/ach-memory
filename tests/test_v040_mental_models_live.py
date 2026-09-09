"""SPEC §14.2/§14.3: governed mental-model lifecycle proof against a real,
disposable Hindsight instance.

Skipped by default. Set HINDSIGHT_V040_CONFIRM=disposable-banks-only to run
it, and only against a loopback Hindsight URL unless the host is explicitly
allow-listed via HINDSIGHT_V040_ALLOW_HOSTS. Every bank this file creates is
unique to the run and is registered for cleanup before use; `live_bank`
deletes it in `finally` whether or not the test's assertions passed. No
production bank or model is ever touched.
"""

import os
import time
import uuid
from urllib.parse import urlsplit

import pytest

from memory.auth.principal import Principal
from memory.bootstrap import BootstrapRequest, bootstrap
from memory.builtin_models import USER_CONTEXT
from memory.errors import MentalModelNotFound, MentalModelQuotaExceeded
from memory.hindsight.client import HindsightClient
from memory.mental_model_service import (
    CustomModelCreateRequest,
    CustomModelUpdateRequest,
    create_custom_model,
    delete_model,
    get_model,
    list_models,
    refresh_model,
    update_model,
)
from memory.models import User
from memory.retained_records import LogicalBankRef

CONFIRM_ENV = "HINDSIGHT_V040_CONFIRM"
ALLOW_HOSTS_ENV = "HINDSIGHT_V040_ALLOW_HOSTS"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
REQUIRED_TAGS = ("schema:ach-retain-v1", "validity:indefinite")
# An empty ACH trigger means manual refresh and is omitted from the upstream
# create request. Hindsight 0.9.2 has no literal ``manual`` trigger mode.
TRIGGER = {}


def _confirmed_settings():
    if os.environ.get(CONFIRM_ENV) != "disposable-banks-only":
        pytest.skip(f"set {CONFIRM_ENV}=disposable-banks-only to run live Hindsight governance tests")

    from memory.config import get_settings

    settings = get_settings()
    host = urlsplit(settings.hindsight_url).hostname
    allowlisted = {
        h.strip() for h in os.environ.get(ALLOW_HOSTS_ENV, "").split(",") if h.strip()
    }
    if host not in LOOPBACK_HOSTS and host not in allowlisted:
        pytest.fail(
            f"refusing to run live governance tests against non-loopback host {host!r}; "
            f"set {ALLOW_HOSTS_ENV} to explicitly allow it"
        )
    return settings


@pytest.fixture
def live_client():
    settings = _confirmed_settings()
    return HindsightClient(
        base_url=settings.hindsight_url,
        api_key=settings.hindsight_api_key,
    )


@pytest.fixture
def live_bank(session, tenant, live_client):
    run = uuid.uuid4().hex[:12]
    user = User(id=f"usr_v040live{run}", tenant_id=tenant, bank_id=f"v040livebank{run}")
    session.add(user)
    session.flush()
    session.commit()
    bank = LogicalBankRef(tenant, "user", user.id, None, user.bank_id)
    try:
        yield bank
    finally:
        last_error = None
        for _ in range(5):
            try:
                live_client.delete_bank(bank.bank_id)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 -- bounded disposable cleanup retry
                last_error = exc
                time.sleep(0.5)
        if last_error is not None:
            raise AssertionError(
                "could not delete a disposable governance-test bank"
            ) from last_error


def _principal_for(bank: LogicalBankRef) -> Principal:
    return Principal(
        tenant_id=bank.tenant_id, user_id=bank.user_id, is_master=False,
        key_id="key_v040live", credential_id="key_v040live",
    )


def _custom_request(**overrides) -> CustomModelCreateRequest:
    body = {
        "name": "live-custom",
        "source_query": "Summarize durable ach-memory v0.4.0 live-governance test content.",
        "source_tags": REQUIRED_TAGS,
        "tags_match": "all",
        "max_tokens": 256,
        "trigger": TRIGGER,
        "operation_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return CustomModelCreateRequest(**body)


def test_governed_mental_model_lifecycle_against_disposable_hindsight(
    session, live_bank, live_client
):
    principal = _principal_for(live_bank)

    # bootstrap creates exactly one built-in and a second bootstrap is idempotent
    first = bootstrap(session, principal, BootstrapRequest(), client=live_client)
    second = bootstrap(session, principal, BootstrapRequest(), client=live_client)
    assert first.user_model.model_key == second.user_model.model_key == USER_CONTEXT.key
    assert first.user_model.delivery_state in {"ready", "withheld"}

    # five custom models succeed while the sixth fails before an upstream request
    created = [
        create_custom_model(session, live_bank, _custom_request(name=f"live-{i}"), client=live_client)
        for i in range(5)
    ]
    assert len({view.model_key for view in created}) == 5
    with pytest.raises(MentalModelQuotaExceeded):
        create_custom_model(session, live_bank, _custom_request(name="live-overflow"), client=live_client)

    target = created[0]
    updated = update_model(
        session, live_bank, target.model_key,
        CustomModelUpdateRequest(source_query="Updated live source query.", operation_id=str(uuid.uuid4())),
        client=live_client,
    )
    assert updated.source_query == "Updated live source query."

    update_model(
        session,
        live_bank,
        target.model_key,
        CustomModelUpdateRequest(
            trigger={"mode": "delta", "refresh_after_consolidation": True},
            operation_id=str(uuid.uuid4()),
        ),
        client=live_client,
    )
    manual = update_model(
        session,
        live_bank,
        target.model_key,
        CustomModelUpdateRequest(trigger={}, operation_id=str(uuid.uuid4())),
        client=live_client,
    )
    assert manual.trigger == {}
    upstream_models = live_client.list_mental_models(live_bank.bank_id, detail="full")
    upstream_target = next(
        item
        for item in (upstream_models.get("mental_models") or upstream_models.get("items") or [])
        if item.get("name") == f"ach:{target.model_key}"
    )
    assert upstream_target["trigger"]["mode"] == "full"
    assert upstream_target["trigger"]["refresh_after_consolidation"] is False
    assert not upstream_target["trigger"].get("refresh_cron")

    # refresh output is withheld until the exact operation completes
    refreshed = refresh_model(
        session, live_bank, target.model_key, operation_id=str(uuid.uuid4()), client=live_client
    )
    assert refreshed.delivery_state == "withheld"
    assert refreshed.refresh_status == "pending"

    # delete by ACH model_key removes only its mapped upstream model
    delete_model(
        session, live_bank, created[4].model_key, operation_id=str(uuid.uuid4()), client=live_client
    )
    with pytest.raises(MentalModelNotFound):
        get_model(session, live_bank, created[4].model_key)
    # its sibling remains untouched
    assert get_model(session, live_bank, created[2].model_key).model_key == created[2].model_key


def test_an_externally_created_model_is_reported_but_never_adopted(session, live_bank, live_client):
    """An unrecognized model created directly against Hindsight (bypassing
    ACH entirely) must be counted in `unknown_upstream_count` and never
    registered, mutated or deleted by ACH."""
    live_client.create_mental_model(
        live_bank.bank_id,
        name="not-an-ach-model",
        source_query="An operator-created model outside ACH governance.",
        max_tokens=256,
    )

    result = list_models(session, live_bank, client=live_client)

    assert result.unknown_upstream_count >= 1
    assert all(view.name != "not-an-ach-model" for view in result.models)
