import httpx
import respx

from memory.capture import configuration
from memory.hindsight.client import HindsightClient

BASE = "http://hindsight.test"
BANK = "user_11111111-1111-1111-1111-111111111111"


def _client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")


def test_desired_user_config_has_the_15_item_limit_and_canonical_labels():
    desired = configuration.desired_user_config().as_dict()

    assert desired["observation_scope_limits"] == [{"scope": ["profile_eligible"], "limit": 15}]
    assert desired["entity_labels"] == ["user", "agent"]
    assert desired["entities_allow_free_form"] is False
    assert desired["retain_default_strategy"] == "candidate_verbatim"
    assert desired["retain_strategies"]["candidate_verbatim"]["retain_extraction_mode"] == (
        "verbatim"
    )


def test_desired_project_config_has_the_25_item_limit_and_no_entity_vocabulary():
    desired = configuration.desired_project_config().as_dict()

    assert desired["observation_scope_limits"] == [{"scope": ["profile_eligible"], "limit": 25}]
    assert "entity_labels" not in desired
    assert "entities_allow_free_form" not in desired


def test_desired_config_never_touches_mental_model_source_filters():
    for desired in (configuration.desired_user_config(), configuration.desired_project_config()):
        body = desired.as_dict()
        assert "mental_model" not in str(body).lower()
        assert "source_filter" not in body


def test_diff_config_reports_only_differing_keys():
    desired = {"a": 1, "b": 2}
    current = {"a": 1, "b": 99}

    drift = configuration.diff_config(desired, current)

    assert drift == {"b": {"desired": 2, "current": 99}}


def test_diff_config_ignores_extra_current_only_keys():
    desired = {"a": 1}
    current = {"a": 1, "unrelated": "whatever"}

    drift = configuration.diff_config(desired, current)

    assert drift == {}


def test_diff_config_is_empty_when_everything_matches():
    desired = configuration.desired_project_config().as_dict()

    drift = configuration.diff_config(desired, desired)

    assert drift == {}


def test_redact_for_display_replaces_the_bank_id_wherever_it_appears():
    from memory import activity

    value = {"a": BANK, "b": [1, BANK, {"c": BANK}], "d": "unrelated"}

    redacted = configuration.redact_for_display(value, BANK)

    fingerprint = activity.fingerprint(BANK)
    assert redacted["a"] == fingerprint
    assert redacted["b"][1] == fingerprint
    assert redacted["b"][2]["c"] == fingerprint
    assert redacted["d"] == "unrelated"
    assert BANK not in str(redacted)


@respx.mock
def test_verify_bank_passes_when_verbatim_returns_the_exact_claim_once():
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(
            200, json={"facts": [{"text": configuration.VERBATIM_CHECK_CLAIM}]}
        )
    )
    respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json=configuration.desired_project_config().as_dict())
    )

    result = configuration.verify_bank(_client(), BANK, configuration.desired_project_config())

    assert result.extraction_ok is True
    assert result.config_drift == {}
    assert result.ok is True


@respx.mock
def test_verify_bank_fails_when_extraction_splits_the_claim():
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(
            200,
            json={
                "facts": [
                    {"text": "Do not run destructive Git commands"},
                    {"text": "without approval."},
                ]
            },
        )
    )
    respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json=configuration.desired_project_config().as_dict())
    )

    result = configuration.verify_bank(_client(), BANK, configuration.desired_project_config())

    assert result.extraction_ok is False
    assert result.ok is False


@respx.mock
def test_verify_bank_fails_when_extraction_rephrases_the_claim():
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(
            200, json={"facts": [{"text": "Avoid destructive git operations."}]}
        )
    )
    respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json=configuration.desired_project_config().as_dict())
    )

    result = configuration.verify_bank(_client(), BANK, configuration.desired_project_config())

    assert result.extraction_ok is False


@respx.mock
def test_verify_bank_reports_config_drift():
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(
            200, json={"facts": [{"text": configuration.VERBATIM_CHECK_CLAIM}]}
        )
    )
    respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json={"retain_default_strategy": "concise"})
    )

    result = configuration.verify_bank(_client(), BANK, configuration.desired_project_config())

    assert result.extraction_ok is True
    assert "retain_default_strategy" in result.config_drift
    assert result.ok is False


@respx.mock
def test_verify_bank_never_patches_config_or_retains():
    """No route is mocked for PATCH .../config or POST .../memories (only
    .../dry-run-extract and GET .../config): either call would raise on the
    unmocked request and fail this test."""
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(
            200, json={"facts": [{"text": configuration.VERBATIM_CHECK_CLAIM}]}
        )
    )
    respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json=configuration.desired_project_config().as_dict())
    )

    configuration.verify_bank(_client(), BANK, configuration.desired_project_config())
