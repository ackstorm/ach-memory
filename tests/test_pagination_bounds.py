"""An out-of-range page size must be a 422, never a 502.

curation.py's own comment claimed this was already true -- only the low side
(ge=0) was bounded. Verified live: limit=10**20 answered 502 HINDSIGHT_ERROR
(a code whose whole meaning to an agent is "retry"), and limit=10**9 answered
200 with an unbounded page, on routes that are is_write=False and therefore
unmetered (2026-08-23 review, R2-I3).
"""

import pytest

HUGE = 100000000000000000000
BIG = 1000000000


@pytest.mark.parametrize(
    "method,path,body,params",
    [
        ("POST", "/v1/memory/list", {"scope": "user"}, None),
        ("POST", "/v1/memory/documents/list", {"scope": "user"}, None),
        ("POST", "/v1/memory/operations/list", {"scope": "user"}, None),
        ("GET", "/v1/directives", None, {"scope": "user"}),
        ("GET", "/v1/mental-models", None, {"scope": "user"}),
    ],
)
@pytest.mark.parametrize("value", [HUGE, BIG])
def test_an_oversize_limit_is_refused_at_the_boundary(
    client, master_headers, tenant, method, path, body, params, value
):
    payload = dict(body or {}, limit=value) if body is not None else None
    query = dict(params or {}, limit=value) if params is not None else None
    response = client.request(
        method, path, json=payload, params=query, headers=master_headers
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "method,path,body,params",
    [
        ("POST", "/v1/memory/list", {"scope": "user"}, None),
        ("GET", "/v1/directives", None, {"scope": "user"}),
    ],
)
def test_a_zero_limit_is_refused(
    client, master_headers, tenant, method, path, body, params
):
    """limit=0 is meaningless and was forwarded upstream verbatim."""
    payload = dict(body or {}, limit=0) if body is not None else None
    query = dict(params or {}, limit=0) if params is not None else None
    response = client.request(
        method, path, json=payload, params=query, headers=master_headers
    )
    assert response.status_code == 422, response.text
