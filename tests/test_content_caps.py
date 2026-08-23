"""SPEC §20's content cap, on every field that carries caller text.

It was implemented for `retain` and later `correct` and nowhere else. A
directive's `content` is the worst gap: for project scope that text is a
standing rule prepended to every reflect for everyone on the project (§14.1),
and it was unbounded (2026-08-23 review, R2-I5). `provenance.build` forwarded
unbounded metadata straight to the extraction LLM (R1-#4).
"""

import pytest

OVERSIZE = "x" * 300_000  # MEMORY_MAX_CONTENT_BYTES defaults to 256_000


@pytest.mark.parametrize(
    "path,body",
    [
        ("/v1/directives", {"scope": "user", "name": "n", "content": OVERSIZE}),
        ("/v1/directives", {"scope": "user", "name": OVERSIZE, "content": "c"}),
        (
            "/v1/mental-models",
            {"scope": "user", "name": "n", "source_query": OVERSIZE},
        ),
        (
            "/v1/mental-models",
            {"scope": "user", "name": OVERSIZE, "source_query": "q"},
        ),
        (
            # finding 5 (2026-08-23): MentalModelTrigger is extra="allow"
            # (SPEC §14.5, deliberate) with no size bound, forwarded
            # verbatim -- the last uncapped caller-authored blob.
            "/v1/mental-models",
            {
                "scope": "user",
                "name": "n",
                "source_query": "q",
                "trigger": {"note": OVERSIZE},
            },
        ),
    ],
)
def test_oversize_governance_text_is_refused(client, master_headers, tenant, path, body):
    response = client.post(path, json=body, headers=master_headers)
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/v1/directives/11111111-1111-1111-1111-111111111111",
            {"scope": "user", "content": OVERSIZE},
        ),
        (
            "/v1/mental-models/mm-1234567890abcdef1234567890abcdef",
            {"scope": "user", "source_query": OVERSIZE},
        ),
        (
            "/v1/mental-models/mm-1234567890abcdef1234567890abcdef",
            {"scope": "user", "trigger": {"note": OVERSIZE}},
        ),
    ],
)
def test_oversize_governance_text_is_refused_on_update(client, master_headers, tenant, path, body):
    """update routes guard with `if body.x is not None` since an update may
    legitimately omit the field -- so the field must be supplied here to
    exercise the check at all. Rejected at validation before the id (which
    does not need to exist) is ever looked up."""
    response = client.patch(path, json=body, headers=master_headers)
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_an_oversize_recall_query_is_refused(client, master_headers, tenant):
    """reflect spends model tokens on a server-level key with no per-user cost
    attribution (§19.4). retain is capped; the token-spending route was not."""
    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "user", "user_id": "nobody", "query": OVERSIZE},
        headers=master_headers,
    )
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_an_oversize_recall_query_is_also_refused_on_recall(client, master_headers, tenant):
    """recall shares the same RecallRequest.query field as reflect -- capped
    at the same choke point rather than leaving one of the two siblings
    unbounded."""
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": "nobody", "query": OVERSIZE},
        headers=master_headers,
    )
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_an_oversize_forget_reason_is_refused(client, master_headers, tenant):
    """reason is caller free text forwarded verbatim to Hindsight; rejected
    before the memory_id (which does not need to exist) is ever looked up."""
    response = client.post(
        "/v1/memory/forget",
        json={
            "scope": "user",
            "user_id": "nobody",
            "memory_id": "11111111-1111-1111-1111-111111111111",
            "reason": OVERSIZE,
        },
        headers=master_headers,
    )
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_an_oversize_list_memories_q_is_refused(client, master_headers, tenant):
    """q carries the same embedding-spend risk class as recall's query."""
    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "user_id": "nobody", "q": OVERSIZE},
        headers=master_headers,
    )
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_an_oversize_list_documents_q_is_refused(client, master_headers, tenant):
    """q carries the same embedding-spend risk class as recall's query."""
    response = client.post(
        "/v1/memory/documents/list",
        json={"scope": "user", "user_id": "nobody", "q": OVERSIZE},
        headers=master_headers,
    )
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_oversize_metadata_is_refused_even_when_content_is_tiny():
    """_check_content_size sees 1 byte; the 8 MB rides alongside it."""
    from memory.errors import ContentTooLarge
    from memory.provenance import build

    with pytest.raises(ContentTooLarge):
        build({"note": "y" * 300_000}, project_slug=None)


def test_ordinary_metadata_still_passes():
    from memory.provenance import build

    assert build({"source": "cli"}, project_slug="p") == {
        "source": "cli",
        "project_slug": "p",
    }
