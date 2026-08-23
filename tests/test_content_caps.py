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
    ],
)
def test_oversize_governance_text_is_refused(client, master_headers, tenant, path, body):
    response = client.post(path, json=body, headers=master_headers)
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
