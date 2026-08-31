import json

import httpx
import pytest
import respx

from memory.capture import filer
from memory.capture.contracts import NormalizedCandidate, Provenance
from memory.hindsight.client import HindsightClient

BASE = "http://hindsight.test"
USER_BANK = "user_11111111-1111-1111-1111-111111111111"
PROJECT_BANK = "project_22222222-2222-2222-2222-222222222222"


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")


def _candidate(**overrides) -> NormalizedCandidate:
    fields = {
        "text": "Prefers tabs over spaces.",
        "kind": "preference",
        "origin": "stated",
        "bank_kind": "user",
        "eligible": "profile_eligible",
        "tags": ["kind:preference", "profile_eligible"],
        "observation_scopes": [["profile_eligible"]],
        "negative": False,
        "correction": False,
        "correction_scope": None,
        "provenance": None,
    }
    fields.update(overrides)
    return NormalizedCandidate(**fields)


# ---------------------------------------------------------------------------
# Deterministic IDs
# ---------------------------------------------------------------------------


def test_operation_id_is_deterministic_per_capture_and_bank_kind():
    first = filer.operation_id("cap_1", "user")
    second = filer.operation_id("cap_1", "user")

    assert first == second


def test_operation_id_differs_by_bank_kind():
    user_op = filer.operation_id("cap_1", "user")
    project_op = filer.operation_id("cap_1", "project")

    assert user_op != project_op


def test_operation_id_differs_by_capture_id():
    assert filer.operation_id("cap_1", "user") != filer.operation_id("cap_2", "user")


def test_document_id_is_deterministic_for_the_same_slice_identity():
    first = filer.document_id("sess-1", 0, 100, "a" * 64)
    second = filer.document_id("sess-1", 0, 100, "a" * 64)

    assert first == second


def test_document_id_differs_for_a_different_slice():
    a = filer.document_id("sess-1", 0, 100, "a" * 64)
    b = filer.document_id("sess-1", 100, 200, "b" * 64)

    assert a != b


# ---------------------------------------------------------------------------
# build_items: grouping, metadata, tags, scopes
# ---------------------------------------------------------------------------


def _build(*candidates, **kwargs):
    fields = {
        "doc_id": "doc-1",
        "host": "claude-code",
        "session_id": "sess-1",
        "session_epoch": 3,
        "checkpoint_seq": 4812,
        "sanitized_hash": "b" * 64,
    }
    fields.update(kwargs)
    return filer.build_items(list(candidates), **fields)


def test_build_items_groups_by_bank_kind():
    grouped = _build(
        _candidate(bank_kind="user"),
        _candidate(bank_kind="project", tags=["kind:convention", "evidence_only"]),
    )

    assert set(grouped) == {"user", "project"}
    assert len(grouped["user"]) == 1
    assert len(grouped["project"]) == 1


def test_every_item_shares_the_slice_document_id_and_host():
    grouped = _build(_candidate(), _candidate(text="Also prefers dark mode."))

    for item in grouped["user"]:
        assert item.document_id == "doc-1"
        assert item.metadata["host"] == "claude-code"


def test_metadata_contains_the_expected_fields():
    candidate = _candidate(
        origin="observed",
        provenance=Provenance(type="transcript", start=1, end=10),
    )
    grouped = _build(candidate)

    metadata = grouped["user"][0].metadata
    assert metadata["origin"] == "observed"
    assert metadata["kind"] == "preference"
    assert metadata["host"] == "claude-code"
    assert metadata["session_id"] == "sess-1"
    assert metadata["session_epoch"] == 3
    assert metadata["checkpoint_seq"] == 4812
    assert metadata["slice_hash"] == "b" * 64
    assert metadata["provenance"] == {"type": "transcript", "start": 1, "end": 10}


def test_tags_are_exactly_kind_and_eligibility():
    candidate = _candidate(tags=["kind:preference", "profile_eligible"])
    grouped = _build(candidate)

    assert grouped["user"][0].tags == ["kind:preference", "profile_eligible"]


def test_observation_scopes_match_the_eligibility():
    candidate = _candidate(
        eligible="evidence_only",
        tags=["kind:technical_claim", "evidence_only"],
        observation_scopes=[["evidence_only"]],
    )
    grouped = _build(candidate)

    assert grouped["user"][0].observation_scopes == [["evidence_only"]]


def test_user_session_and_origin_never_become_tags():
    candidate = _candidate(origin="observed", bank_kind="user")
    grouped = _build(candidate)

    tags = grouped["user"][0].tags
    assert "user" not in tags
    assert "sess-1" not in tags
    assert "observed" not in tags


def test_every_item_uses_the_candidate_verbatim_strategy_and_append():
    grouped = _build(_candidate())

    item = grouped["user"][0]
    assert item.strategy == "candidate_verbatim"
    assert item.update_mode == "append"


# ---------------------------------------------------------------------------
# file_candidates: one call per bank, deterministic replay
# ---------------------------------------------------------------------------


@respx.mock
def test_file_candidates_calls_retain_items_once_per_resolved_bank(client):
    user_route = respx.post(f"{BASE}/v1/default/banks/{USER_BANK}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": "op-user"})
    )
    project_route = respx.post(f"{BASE}/v1/default/banks/{PROJECT_BANK}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": "op-project"})
    )

    operations = filer.file_candidates(
        client,
        capture_id="cap_1",
        candidates=[
            _candidate(bank_kind="user"),
            _candidate(bank_kind="project", tags=["kind:convention", "evidence_only"]),
        ],
        bank_ids={"user": USER_BANK, "project": PROJECT_BANK},
        doc_id="doc-1",
        host="claude-code",
        session_id="sess-1",
        session_epoch=3,
        checkpoint_seq=100,
        sanitized_hash="b" * 64,
    )

    assert user_route.call_count == 1
    assert project_route.call_count == 1
    assert set(operations) == {"user", "project"}


@respx.mock
def test_a_lost_retain_acknowledgement_retries_the_same_operation_id(client):
    route = respx.post(f"{BASE}/v1/default/banks/{USER_BANK}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": "op-1"})
    )
    kwargs = {
        "capture_id": "cap_1",
        "candidates": [_candidate()],
        "bank_ids": {"user": USER_BANK},
        "doc_id": "doc-1",
        "host": "claude-code",
        "session_id": "sess-1",
        "session_epoch": 3,
        "checkpoint_seq": 100,
        "sanitized_hash": "b" * 64,
    }

    filer.file_candidates(client, **kwargs)
    filer.file_candidates(client, **kwargs)

    first = json.loads(route.calls[0].request.read())
    second = json.loads(route.calls[1].request.read())
    assert first["operation_id"] == second["operation_id"]


@respx.mock
def test_replay_never_uses_document_replacement(client):
    """update_mode is "append" on every call, first attempt or retry --
    Hindsight's own idempotency (keyed by operation_id) is what makes
    resubmission safe, not a replace-on-retry special case."""
    route = respx.post(f"{BASE}/v1/default/banks/{USER_BANK}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": "op-1"})
    )
    kwargs = {
        "capture_id": "cap_1",
        "candidates": [_candidate()],
        "bank_ids": {"user": USER_BANK},
        "doc_id": "doc-1",
        "host": "claude-code",
        "session_id": "sess-1",
        "session_epoch": 3,
        "checkpoint_seq": 100,
        "sanitized_hash": "b" * 64,
    }

    filer.file_candidates(client, **kwargs)
    filer.file_candidates(client, **kwargs)

    for call in route.calls:
        body = json.loads(call.request.read())
        assert all(item["update_mode"] == "append" for item in body["items"])


# ---------------------------------------------------------------------------
# Completion polling
# ---------------------------------------------------------------------------


def test_is_complete_true_only_for_completed_status():
    assert filer.is_complete({"status": "completed"}) is True


@pytest.mark.parametrize("status", ["pending", "failed", "not_found", None])
def test_is_complete_false_for_pending_or_failed_or_missing(status):
    assert filer.is_complete({"status": status}) is False
