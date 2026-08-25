"""Unit tests for the read-tool payload reduction (src/memory/mcp/compact.py).

The wiring — which tool passes `verbose`, what goes on the wire — is covered in
test_mcp_tools.py. This file pins the rules themselves.
"""

from memory.mcp.compact import compact

# One recall hit carrying every field hindsight-api 0.9.1 puts on a fact.
FACT = {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "text": "The project pins its Python dependencies with uv, never with pip.",
    "type": "world",
    "entities": ["uv", "pip"],
    "context": "dependency management",
    "occurred_start": "2026-01-15T10:30:00Z",
    "occurred_end": "2026-01-15T10:30:00Z",
    "mentioned_at": "2026-01-15T10:30:00Z",
    "document_id": "550e8400-e29b-41d4-a716-446655440000",
    "chunk_id": "456e7890-e12b-34d5-a678-901234567890",
    "tags": [],
    "source_fact_ids": [],
    "metadata": {"source": "agent"},
    "scores": {"final": 0.81234, "reranker": 0.42, "semantic": 0.77},
}


def _recall(**overrides):
    fact = {**FACT, **overrides}
    return compact("memory.recall", {"results": [fact], "entities": {"uv": {}}})


def test_recall_drops_the_fields_no_tool_of_the_surface_can_consume():
    hit = _recall()["results"][0]

    for gone in ("chunk_id", "source_fact_ids", "entities", "tags"):
        assert gone not in hit


def test_recall_keeps_what_the_agent_acts_on():
    """document_id earns its place: it is the only handle get_document,
    delete_document and retain(update_mode=...) have, and a recall hit is the
    only place an agent learns one."""
    hit = _recall()["results"][0]

    for kept in ("id", "text", "type", "context", "document_id", "metadata"):
        assert kept in hit, kept


def test_recall_drops_the_entity_map_the_agent_never_asked_for():
    assert "entities" not in _recall()


def test_one_instant_repeated_three_times_collapses_to_one():
    hit = _recall()["results"][0]

    assert hit["occurred_start"] == "2026-01-15T10:30:00Z"
    assert "occurred_end" not in hit
    assert "mentioned_at" not in hit


def test_a_fact_that_genuinely_spans_time_keeps_both_ends():
    """The collapse means "same as the anchor", so it must not fire when the
    timestamps differ -- that is the only case where they carried information."""
    hit = _recall(occurred_end="2026-03-01T00:00:00Z")["results"][0]

    assert hit["occurred_start"] == "2026-01-15T10:30:00Z"
    assert hit["occurred_end"] == "2026-03-01T00:00:00Z"


def test_scores_keep_the_ranking_signal_and_drop_the_tuning_internals():
    scores = _recall()["results"][0]["scores"]

    assert scores == {"final": 0.81}


def test_a_hit_with_no_final_score_loses_the_scores_key_entirely():
    hit = _recall(scores={"reranker": 0.4})["results"][0]

    assert "scores" not in hit


def test_reflect_keeps_the_answer_and_drops_the_token_accounting():
    payload = compact(
        "memory.reflect",
        {
            "text": "This project uses uv.",
            "usage": {"input_tokens": 1500, "output_tokens": 500},
            "based_on": None,
            "trace": None,
        },
    )

    assert payload == {"text": "This project uses uv."}


def test_list_memories_keeps_the_curation_fields_on_an_invalidated_row():
    """state/invalidation_reason/invalidated_at are deliberately absent from
    the exclude set: they are what forget/restore/correct read back."""
    payload = compact(
        "memory.list",
        {
            "items": [
                {
                    "id": "m1",
                    "text": "old fact",
                    "date": "2026-01-15T10:30:00Z",
                    "state": "invalidated",
                    "invalidation_reason": "superseded",
                    "invalidated_at": "2026-02-01T00:00:00Z",
                    "proof_count": 3,
                    "consolidated_at": "2026-01-16T02:00:00Z",
                    "chunk_id": "c1",
                }
            ],
            "total": 150,
            "limit": 20,
            "offset": 0,
        },
    )
    row = payload["items"][0]

    assert row["state"] == "invalidated"
    assert row["invalidation_reason"] == "superseded"
    assert row["invalidated_at"] == "2026-02-01T00:00:00Z"
    assert "proof_count" not in row
    assert "consolidated_at" not in row
    assert "chunk_id" not in row


def test_the_paging_envelope_survives_so_the_rest_stays_reachable():
    payload = compact("memory.list", {"items": [], "total": 150, "limit": 20, "offset": 0})

    assert payload["total"] == 150
    assert payload["limit"] == 20
    assert payload["offset"] == 0


def test_an_empty_result_set_is_not_the_same_as_a_missing_one():
    """`results: []` means "searched, found nothing" and has to survive the
    null drop, which only ever removes null VALUES."""
    assert compact("memory.recall", {"results": []}) == {"results": []}


def test_a_missing_field_is_a_no_op_not_an_error():
    """Exclude semantics: Hindsight gating a field behind a request option, or
    dropping one in a future version, must not raise here."""
    assert compact("memory.recall", {"results": [{"text": "bare"}]}) == {
        "results": [{"text": "bare"}]
    }


def test_an_unknown_action_is_left_alone_apart_from_its_nulls():
    payload = compact("memory.retain", {"operation_id": "op1", "error": None})

    assert payload == {"operation_id": "op1"}


def test_a_field_hindsight_adds_later_reaches_the_agent():
    """The whole point of excluding rather than allowlisting: the failure mode
    points at "too much", which someone can see and fix, not at "too little",
    which is invisible."""
    payload = compact("memory.recall", {"results": [{"text": "x", "brand_new": "v"}]})

    assert payload["results"][0]["brand_new"] == "v"


def test_a_payload_that_is_not_an_object_is_returned_untouched():
    assert compact("memory.recall", ["not", "a", "dict"]) == ["not", "a", "dict"]


def test_documents_drop_the_internal_hash():
    payload = compact(
        "memory.documents.get",
        {"id": "d1", "content_hash": "abc123", "text_length": 5420, "tags": []},
    )

    assert payload == {"id": "d1", "text_length": 5420}


def test_operations_keep_the_status_an_agent_polls_for():
    payload = compact(
        "memory.operations.get",
        {
            "id": "op1",
            "status": "failed",
            "error_message": "extraction timed out",
            "retry_count": 2,
            "next_retry_at": "2026-01-15T11:00:00Z",
            "items_count": 1,
        },
    )

    assert payload == {
        "id": "op1",
        "status": "failed",
        "error_message": "extraction timed out",
    }
