import json

import httpx
import pytest
import respx

from memory.capture.extractor import ExtractionFailed, extract
from memory.hindsight.client import HindsightClient

BASE = "http://hindsight.test"
BANK = "user_11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")


def _mock(*envelopes: dict, route_kwargs: dict | None = None):
    facts = [{"text": json.dumps(envelope)} for envelope in envelopes]
    return respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": facts})
    )


# ---------------------------------------------------------------------------
# Curated happy-path fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_extracts_a_stated_user_preference(client):
    _mock(
        {
            "record": "candidate",
            "text": "Prefers tabs over spaces.",
            "kind": "preference",
            "origin": "stated",
            "subject": "user",
        }
    )

    result = extract(client, BANK, "user: I prefer tabs over spaces")

    assert len(result.candidates) == 1
    assert result.candidates[0].eligible == "profile_eligible"
    assert result.working_state is None


@respx.mock
def test_extracts_an_observed_project_convention_with_artifact(client):
    _mock(
        {
            "record": "candidate",
            "text": "The project uses black for formatting.",
            "kind": "convention",
            "origin": "observed",
            "subject": "project",
            "provenance": {"type": "transcript", "start": 0, "end": 40},
        }
    )

    result = extract(client, BANK, "tool_use: Bash\ntool_result: Bash ok all files formatted")

    assert result.candidates[0].origin == "observed"
    assert result.candidates[0].eligible == "profile_eligible"


@respx.mock
def test_extracts_an_inferred_technical_claim(client):
    _mock(
        {
            "record": "candidate",
            "text": "The service listens on port 8080.",
            "kind": "technical_claim",
            "origin": "inferred",
            "subject": "project",
        }
    )

    result = extract(client, BANK, "assistant: looks like it binds 8080")

    assert result.candidates[0].eligible == "evidence_only"


@respx.mock
def test_extracts_a_negative_constraint(client):
    _mock(
        {
            "record": "candidate",
            "text": "Do not squash commits.",
            "kind": "convention",
            "origin": "stated",
            "subject": "project",
            "negative": True,
        }
    )

    result = extract(client, BANK, "user: never squash our commits")

    assert result.candidates[0].negative is True


@respx.mock
def test_extracts_a_correction(client):
    _mock(
        {
            "record": "candidate",
            "text": "The project uses ruff, not flake8.",
            "kind": "convention",
            "origin": "stated",
            "subject": "project",
            "correction": True,
        }
    )

    result = extract(client, BANK, "user: actually we use ruff now, not flake8")

    assert result.candidates[0].correction is True
    assert result.candidates[0].correction_scope == ("project", "profile_eligible")


@respx.mock
def test_extracts_a_working_state_update(client):
    _mock(
        {
            "record": "working_state",
            "objective": "Ship Phase 3",
            "next_steps": ["Wire the worker"],
        }
    )

    result = extract(client, BANK, "assistant: next I'll wire the worker")

    assert result.working_state is not None
    assert result.working_state.objective == "Ship Phase 3"
    assert result.candidates == []


@respx.mock
def test_permission_content_becomes_working_state_never_a_decision_candidate(client):
    _mock(
        {
            "record": "working_state",
            "objective": "Prepare Phase 3",
            "next_steps": ["Get approval before running the test suite"],
        }
    )

    result = extract(client, BANK, "user: get my approval before running tests")

    assert result.working_state is not None
    assert result.candidates == []


@respx.mock
def test_multiple_candidates_in_one_slice(client):
    _mock(
        {
            "record": "candidate",
            "text": "Prefers tabs.",
            "kind": "preference",
            "origin": "stated",
            "subject": "user",
        },
        {
            "record": "candidate",
            "text": "CI runs on every push.",
            "kind": "convention",
            "origin": "observed",
            "subject": "project",
            "provenance": {"type": "transcript", "start": 0, "end": 10},
        },
    )

    result = extract(client, BANK, "user: I prefer tabs; CI runs on every push")

    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# Adversarial: fail the whole extraction, never file a partial result
# ---------------------------------------------------------------------------


@respx.mock
def test_malformed_json_fails_the_whole_extraction(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": [{"text": "{not json"}]})
    )

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


@respx.mock
def test_an_unknown_field_fails_the_whole_extraction(client):
    envelope = {
        "record": "candidate",
        "text": "x",
        "kind": "preference",
        "origin": "stated",
        "subject": "user",
        "unexpected_field": "sneaky",
    }
    _mock(envelope)

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


@pytest.mark.parametrize("field", ["bank", "profile_eligible", "tags", "observation_scopes"])
@respx.mock
def test_a_model_supplied_eligibility_field_fails_the_whole_extraction(client, field):
    envelope = {
        "record": "candidate",
        "text": "x",
        "kind": "preference",
        "origin": "stated",
        "subject": "user",
        field: "anything",
    }
    _mock(envelope)

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


@respx.mock
def test_malformed_provenance_fails_the_whole_extraction(client):
    envelope = {
        "record": "candidate",
        "text": "The project uses black.",
        "kind": "convention",
        "origin": "observed",
        "subject": "project",
        "provenance": {"type": "transcript", "start": 40, "end": 10},
    }
    _mock(envelope)

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


@respx.mock
def test_out_of_range_stated_provenance_is_removed_without_losing_sibling(client):
    _mock(
        {
            "record": "candidate",
            "text": "Use concise updates.",
            "kind": "preference",
            "origin": "stated",
            "subject": "user",
            "provenance": {"type": "transcript", "start": 100, "end": 120},
        },
        {
            "record": "candidate",
            "text": "The project uses JSONL.",
            "kind": "convention",
            "origin": "stated",
            "subject": "project",
        },
    )

    result = extract(client, BANK, "user: concise updates; project uses JSONL")

    assert len(result.candidates) == 2
    assert result.candidates[0].origin == "stated"
    assert result.candidates[0].eligible == "profile_eligible"
    assert result.candidates[0].provenance is None
    assert result.candidates[1].text == "The project uses JSONL."


@respx.mock
def test_out_of_range_observed_provenance_degrades_to_inferred(client):
    _mock(
        {
            "record": "candidate",
            "text": "The project uses JSONL.",
            "kind": "convention",
            "origin": "observed",
            "subject": "project",
            "provenance": {"type": "transcript", "start": 100, "end": 120},
        }
    )

    result = extract(client, BANK, "tool_result: JSONL output observed")

    assert len(result.candidates) == 1
    assert result.candidates[0].origin == "inferred"
    assert result.candidates[0].eligible == "evidence_only"
    assert result.candidates[0].provenance is None


@respx.mock
def test_more_than_one_working_state_record_fails_the_whole_extraction(client):
    _mock(
        {"record": "working_state", "objective": "first"},
        {"record": "working_state", "objective": "second"},
    )

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


@respx.mock
def test_a_valid_envelope_followed_by_a_malformed_one_files_nothing(client):
    """The extractor raises rather than returning a partial ExtractionResult
    -- there is no result object for a caller to accidentally file half of."""
    _mock(
        {
            "record": "candidate",
            "text": "Prefers tabs.",
            "kind": "preference",
            "origin": "stated",
            "subject": "user",
        },
        {"record": "candidate", "text": "y", "kind": "bogus", "origin": "stated", "subject": "user"},
    )

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


@respx.mock
def test_a_personal_preference_routed_to_project_fails_the_whole_extraction(client):
    _mock(
        {
            "record": "candidate",
            "text": "I like tabs.",
            "kind": "preference",
            "origin": "stated",
            "subject": "project",
        }
    )

    with pytest.raises(ExtractionFailed):
        extract(client, BANK, "...")


# ---------------------------------------------------------------------------
# The extraction call itself
# ---------------------------------------------------------------------------


@respx.mock
def test_uses_the_custom_extraction_mode_and_excludes_noise_categories(client):
    route = _mock()

    extract(client, BANK, "some sanitized slice text")

    body = json.loads(route.calls.last.request.read())
    assert body["retain_extraction_mode"] == "custom"
    assert body["content"] == "some sanitized slice text"
    mission = body["retain_mission"].lower()
    for excluded in ("greeting", "logistics", "canaries", "repository"):
        assert excluded in mission
    assert "bank" in mission and "profile_eligible" in mission


@respx.mock
def test_the_prompt_never_asks_for_more_than_it_should(client):
    route = _mock()

    extract(client, BANK, "x")

    body = json.loads(route.calls.last.request.read())
    assert "at most one" in body["retain_mission"].lower() or "at most once" in (
        body["retain_mission"].lower()
    )
