"""The brief's trust rules and composition.

Every rule here exists because a digest that is wrong is worse than one that
is missing: it arrives with no citation and nothing to check it against.
"""

from datetime import UTC, datetime, timedelta

import pytest

from memory import brief

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class FakeClient:
    """Records calls; returns whatever the test seeded."""

    def __init__(self, models=None):
        self.models = models if models is not None else []
        self.created = []
        self.updated = []

    def list_mental_models(self, bank_id, **kwargs):
        return {"mental_models": self.models}

    def create_mental_model(self, bank_id, **kwargs):
        self.created.append((bank_id, kwargs))
        return {"mental_model_id": "mm-new"}

    def update_mental_model(self, bank_id, mental_model_id, **kwargs):
        self.updated.append((bank_id, mental_model_id, kwargs))
        return {"mental_model_id": mental_model_id}


def _model(content, *, refreshed=NOW, stale=False, query=None):
    return {
        "id": "mm-1",
        "name": brief.BRIEF_MODEL_NAME,
        "content": content,
        "source_query": query if query is not None else brief.USER_QUERY,
        "is_stale": stale,
        "last_refreshed_at": refreshed.isoformat(),
    }


def test_a_missing_model_is_created_and_yields_no_section_yet():
    """First contact provisions and returns nothing: upstream fills `content`
    with a placeholder until the first refresh completes, and showing a
    placeholder to a model is worse than showing it nothing."""
    client = FakeClient(models=[])

    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None

    (bank_id, kwargs) = client.created[0]
    assert bank_id == "user_1"
    assert kwargs["name"] == brief.BRIEF_MODEL_NAME
    assert kwargs["source_query"] == brief.USER_QUERY
    assert kwargs["max_tokens"] == 400
    assert kwargs["trigger"] == {"mode": "delta", "refresh_cron": "0 3 * * *"}


def test_the_upstream_placeholder_is_not_a_section():
    client = FakeClient(models=[_model("Generating content...")])
    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None


@pytest.mark.parametrize("content", ["", "   \n  "])
def test_an_empty_digest_is_not_a_section(content):
    """An empty heading is an invitation to invent one."""
    client = FakeClient(models=[_model(content)])
    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None


def test_a_digest_whose_refreshes_are_failing_is_dropped():
    """Stale AND old together mean refreshes are failing, which is otherwise
    invisible: a failed refresh keeps serving the previous content and does
    not set is_stale. Measured against production 2026-08-27."""
    client = FakeClient(
        models=[_model("real content", refreshed=NOW - timedelta(days=8), stale=True)]
    )
    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None


def test_an_old_but_current_digest_is_kept():
    """Age alone is not failure: a user who wrote nothing for a week has a
    legitimately old digest, and the cron skips ticks when nothing is stale."""
    refreshed = NOW - timedelta(days=30)
    client = FakeClient(models=[_model("real content", refreshed=refreshed, stale=False)])

    section = brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW)

    assert section.text == "real content"
    assert section.refreshed_at == refreshed.isoformat()


def test_a_changed_source_query_updates_the_model_in_place():
    """The query is code. A deploy that improves it must reach the next
    refresh with no manual step -- upstream falls back from delta to a full
    regeneration by itself when the query changed."""
    client = FakeClient(models=[_model("real content", query="an older query")])

    brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW)

    (bank_id, model_id, kwargs) = client.updated[0]
    assert (bank_id, model_id) == ("user_1", "mm-1")
    assert kwargs["source_query"] == brief.USER_QUERY


def test_a_long_digest_is_served_whole():
    """max_tokens is advisory upstream, so a digest routinely runs longer than
    asked for. It still goes out intact: cutting it left the last line severed
    mid-word, and an unmarked half sentence can read as the opposite of the
    rule it came from."""
    content = "* a rule that must not be cut\n" + "x" * 5000
    client = FakeClient(models=[_model(content)])

    section = brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW)

    assert section.text == content


def _section(text):
    return brief.Section(text=text, refreshed_at="2026-08-27T03:00:00+00:00")


def test_compose_states_the_briefs_status_and_keeps_the_policy_first():
    text = brief.compose("POLICY", _section("user facts"), _section("project facts"), "acme-api")

    assert text.startswith("POLICY")
    assert "user facts" in text and "project facts" in text
    assert "acme-api" in text
    # The one clause that makes a wrong digest survivable.
    assert "verify with recall" in text


def test_compose_omits_a_section_it_has_no_material_for():
    text = brief.compose("POLICY", _section("user facts"), None, "acme-api")

    assert "user facts" in text
    assert "acme-api" not in text


def test_compose_with_nothing_is_exactly_the_policy():
    """The failure path must be indistinguishable from today's behaviour."""
    assert brief.compose("POLICY", None, None, None) == "POLICY"


import httpx
import respx

BASE = "http://hindsight.test"


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


@respx.mock
def test_the_brief_carries_the_policy_and_the_user_section(client, two_users):
    """The endpoint returns the WHOLE instructions payload, not the brief
    alone: the proxy replaces the server's instructions with whatever it
    advertises, so composing anywhere else would drop the policy."""
    from memory.mcp.server import INSTRUCTIONS

    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {
                        "id": "mm-1",
                        "name": "ach-memory-session-brief",
                        "content": "Ask before planning.",
                        "source_query": __import__(
                            "memory.brief", fromlist=["x"]
                        ).USER_QUERY,
                        "is_stale": False,
                        "last_refreshed_at": "2026-08-27T03:00:00+00:00",
                    }
                ]
            },
        )
    )

    response = client.get(
        "/v1/session-brief?scope=user", headers=_headers(two_users[0]["key"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["instructions"].startswith(INSTRUCTIONS)
    assert "Ask before planning." in body["instructions"]
    assert body["sections"] == {"user": True, "project": False}
    assert body["generated_at"] == "2026-08-27T03:00:00+00:00"


@respx.mock
def test_a_project_that_does_not_exist_is_not_created_by_asking_for_a_brief(
    client, two_users
):
    """create=False. A session start must never mint a project -- an agent
    opening any directory would otherwise squat a slug."""
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )
    # The user bank's own model does not exist yet either -- ensure_section
    # provisions it in the same call, unrelated to the project assertion.
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(201, json={"id": "mm-new"})
    )

    response = client.get(
        "/v1/session-brief?scope=user&project_slug=never-created",
        headers=_headers(two_users[0]["key"]),
    )

    assert response.status_code == 200
    assert response.json()["sections"]["project"] is False
