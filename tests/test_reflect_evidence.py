"""A reflect answer says what it stood on.

`reflect` returns prose an agent quotes wholesale. Upstream can name the
memories that prose was grounded on (`based_on`), but only when asked
(`include.facts`), and this service never asked -- so every answer arrived
with no way to tell a grounded claim from an invented one.

Now requested on both surfaces, and reduced to the memories alone before a
caller sees it: `mental_models` carries upstream's own model ids, which every
surface here keeps internal behind a caller-facing `model_key`, and
`directives` is not a surface this service exposes.
"""

import httpx
import pytest
import respx

from memory.hindsight.client import HindsightClient
from memory.read_service import whitelist_reflect_evidence

BASE = "http://hindsight.test"
BANK = "user_bank-1"

UPSTREAM = {
    "text": "Dependencies are pinned with uv.",
    "based_on": {
        "memories": [
            {
                "id": "mem-1",
                "text": "This project pins its Python dependencies with uv, never with pip.",
                "type": "world",
                "context": "tooling",
                "occurred_start": "2026-09-11T10:14:31Z",
                "occurred_end": None,
            },
            {"id": "mem-2", "text": "All log lines are JSON.", "type": "observation"},
        ],
        "mental_models": [{"id": "mm-0123abcd", "text": "Alice prefers uv.", "context": None}],
        "directives": [{"id": "dir-1", "name": "tone", "content": "be terse"}],
    },
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
}


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret")


@respx.mock
def test_the_client_asks_for_the_grounding_when_told_to(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"text": "an answer"})
    )

    client.reflect(BANK, "why", include_facts=True)

    import json

    assert json.loads(route.calls[0].request.content)["include"] == {"facts": {}}


@respx.mock
def test_the_client_sends_no_include_key_by_default(client):
    """Omitted, not sent as null: upstream's defaults differ between a request
    that names `include` and one that does not."""
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"text": "an answer"})
    )

    client.reflect(BANK, "why")

    import json

    assert "include" not in json.loads(route.calls[0].request.content)


def test_only_the_memories_reach_the_caller_and_only_three_fields_of_each():
    result = whitelist_reflect_evidence(UPSTREAM)

    assert result["based_on"] == {
        "memories": [
            {
                "id": "mem-1",
                "text": "This project pins its Python dependencies with uv, never with pip.",
                "type": "world",
            },
            {"id": "mem-2", "text": "All log lines are JSON.", "type": "observation"},
        ]
    }
    assert "mm-0123abcd" not in str(result)
    assert "directives" not in str(result)


def test_the_answer_and_its_accounting_are_left_alone():
    result = whitelist_reflect_evidence(UPSTREAM)

    assert result["text"] == UPSTREAM["text"]
    assert result["usage"] == UPSTREAM["usage"]


def test_an_answer_with_no_grounding_is_not_given_an_empty_one():
    """Absent and empty answer different questions: "was this grounded on
    nothing" is upstream's to say, and it says it with an empty list."""
    assert whitelist_reflect_evidence({"text": "x"}) == {"text": "x"}
    assert whitelist_reflect_evidence({"text": "x", "based_on": {"memories": []}}) == {
        "text": "x",
        "based_on": {"memories": []},
    }


def test_upstream_shape_is_not_trusted():
    """A malformed entry is dropped, not raised on: reflect already spent the
    tokens, and a KeyError here would throw the answer away with them."""
    result = whitelist_reflect_evidence(
        {
            "text": "x",
            "based_on": {"memories": ["not-a-dict", {"id": "no-text"}, {"text": "ok"}]},
        }
    )

    assert result["based_on"] == {"memories": [{"id": None, "text": "ok", "type": None}]}
    # Present but not the documented shape: nothing in it is known to be safe.
    assert whitelist_reflect_evidence({"text": "x", "based_on": "junk"}) == {"text": "x"}
