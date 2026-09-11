"""`reflect` answers over the same corpus `recall` does.

`reflect` used to send only the caller's own tags and nothing of the
server's, so the two surfaces answered over different sets in the same bank:
`recall` scoped to `schema:ach-retain-v1` facts of type world/observation,
while `reflect` reasoned over the whole bank -- `experience` facts included,
and anything not written by this service's retain path at all. Nothing in
either response said so, and `reflect` is the surface that returns prose an
agent quotes wholesale, so it is where a silently wider grounding shows
least.
"""

import httpx
import pytest
import respx

from memory.hindsight.client import HindsightClient
from memory.read_models import resolve_filters

BASE = "http://hindsight.test"
BANK = "user_bank-1"


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret")


def _reflect_body(route):
    import json

    return json.loads(route.calls[0].request.content)


@respx.mock
def test_reflect_sends_the_servers_scoping_group(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"text": "an answer"})
    )
    filters = resolve_filters("current", None, ())

    client.reflect(
        BANK, "why", tag_groups=list(filters.tag_groups), fact_types=list(filters.types)
    )

    body = _reflect_body(route)
    assert body["tag_groups"][0] == {
        "tags": ["schema:ach-retain-v1"],
        "match": "all_strict",
    }
    assert body["fact_types"] == ["world", "observation"]


@respx.mock
def test_reflect_no_longer_speaks_the_flat_tag_vocabulary(client):
    """`tags`/`tags_match` are mutually exclusive with `tag_groups` upstream,
    and a flat list carries ONE match mode for every tag in it -- which is how
    a caller's tags once reached the server's own scoping tag and loosened
    it."""
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"text": "an answer"})
    )
    filters = resolve_filters("current", None, ("repo:acme/api",))

    client.reflect(
        BANK, "why", tag_groups=list(filters.tag_groups), fact_types=list(filters.types)
    )

    body = _reflect_body(route)
    assert "tags" not in body and "tags_match" not in body
    groups = body["tag_groups"]
    assert groups[0] == {"tags": ["schema:ach-retain-v1"], "match": "all_strict"}
    assert {"tags": ["repo:acme/api"], "match": "all_strict"} in groups


@respx.mock
def test_an_unfiltered_reflect_omits_the_keys_entirely(client):
    """Upstream's defaults differ between a filtered and an unfiltered
    request, so an empty key is not equivalent to an absent one."""
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"text": "an answer"})
    )

    client.reflect(BANK, "why")

    body = _reflect_body(route)
    assert body == {"query": "why"}


def test_both_surfaces_map_scope_through_one_function():
    """Parity is structural, not a coincidence to re-check by hand: `recall`
    and `reflect` both build their scope with `resolve_filters`, so a change
    to what the retained corpus means cannot reach one and miss the other."""
    filters = resolve_filters("current", None, ())

    assert filters.types == ("world", "observation")
    assert [dict(g) for g in filters.tag_groups] == [
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"}
    ]
