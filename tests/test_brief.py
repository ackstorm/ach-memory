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


def _model(content, *, refreshed=NOW, stale=False, query=None, trigger=None):
    return {
        "id": "mm-1",
        "name": brief.BRIEF_MODEL_NAME,
        "content": content,
        "source_query": query if query is not None else brief.USER_QUERY,
        "is_stale": stale,
        "last_refreshed_at": refreshed.isoformat(),
        # Already in line with the constants unless a test says otherwise, so
        # reconciliation only fires where it is the subject.
        "trigger": dict(brief.TRIGGER) if trigger is None else trigger,
    }


def test_reading_a_bank_with_no_model_creates_nothing():
    """A GET that provisions is a read minting state -- the same class as the
    readOnlyHint slug-squat. One exploratory brief read against an unrelated
    project created a model there and spent a generation on it."""
    client = FakeClient(models=[])

    assert brief.get_section(client, "user_1", NOW) is None
    assert client.created == []
    assert client.updated == []


def test_a_read_never_reconciles_a_drifted_model():
    """Reconciliation left the read path together with provisioning, and this
    is the half that regresses silently: a stale `source_query` shows up
    nowhere in the response, so only a test says the GET stopped repairing it.
    A changed constant now reaches deployed models through `provision_section`
    alone."""
    client = FakeClient(models=[_model("real content", query="an older query")])

    section = brief.get_section(client, "user_1", NOW)

    assert section.text == "real content"
    assert client.updated == []
    assert client.created == []


def test_provisioning_is_explicit_and_creates_the_model():
    """Creation moved out of the read path, not out of the system: the query
    and trigger are versioned in code and a deploy still has to reach models
    that already exist. `max_tokens` and the trigger are pinned here because
    both reached production once by accident, and the trigger assertion is the
    only thing standing between a deploy and a silently changed refresh
    schedule."""
    client = FakeClient(models=[])

    assert brief.provision_section(client, "user_1", brief.USER_QUERY) == "created"

    (bank_id, kwargs) = client.created[0]
    assert bank_id == "user_1"
    assert kwargs["name"] == brief.BRIEF_MODEL_NAME
    assert kwargs["source_query"] == brief.USER_QUERY
    assert kwargs["max_tokens"] == 400
    assert kwargs["trigger"] == {
        "mode": "delta",
        "refresh_cron": "0 3 * * *",
        "keep_trace": True,
    }


def test_the_upstream_placeholder_is_not_a_section():
    """Upstream fills `content` with a placeholder until the first refresh
    completes, and showing a placeholder to a model is worse than showing it
    nothing."""
    client = FakeClient(models=[_model("Generating content...")])
    assert brief.get_section(client, "user_1", NOW) is None


@pytest.mark.parametrize("content", ["", "   \n  "])
def test_an_empty_digest_is_not_a_section(content):
    """An empty heading is an invitation to invent one."""
    client = FakeClient(models=[_model(content)])
    assert brief.get_section(client, "user_1", NOW) is None


def test_a_digest_whose_refreshes_are_failing_is_dropped():
    """Stale AND old together mean refreshes are failing, which is otherwise
    invisible: a failed refresh keeps serving the previous content and does
    not set is_stale. Measured against production 2026-08-27."""
    client = FakeClient(
        models=[_model("real content", refreshed=NOW - timedelta(days=8), stale=True)]
    )
    assert brief.get_section(client, "user_1", NOW) is None


def test_an_old_but_current_digest_is_kept():
    """Age alone is not failure: a user who wrote nothing for a week has a
    legitimately old digest, and the cron skips ticks when nothing is stale."""
    refreshed = NOW - timedelta(days=30)
    client = FakeClient(models=[_model("real content", refreshed=refreshed, stale=False)])

    section = brief.get_section(client, "user_1", NOW)

    assert section.text == "real content"
    assert section.refreshed_at == refreshed.isoformat()


def test_a_changed_source_query_updates_the_model_in_place():
    """The query is code. A deploy that improves it must reach the next
    refresh -- upstream falls back from delta to a full regeneration by itself
    when the query changed -- and since the read path stopped provisioning,
    the deploy step that carries it there is this call."""
    client = FakeClient(models=[_model("real content", query="an older query")])

    assert brief.provision_section(client, "user_1", brief.USER_QUERY) == "reconciled"

    (bank_id, model_id, kwargs) = client.updated[0]
    assert (bank_id, model_id) == ("user_1", "mm-1")
    assert kwargs["source_query"] == brief.USER_QUERY
    assert "trigger" not in kwargs


def test_a_changed_trigger_reaches_a_model_that_already_exists():
    """The trigger is code too, and the models are provisioned once. Without
    this, adding keep_trace to TRIGGER would apply to banks nobody has used
    yet and to nothing else -- the two models already in production would keep
    a trigger no source file describes."""
    stale_trigger = {"mode": "delta", "refresh_cron": "0 3 * * *"}
    client = FakeClient(models=[_model("real content", trigger=stale_trigger)])

    brief.provision_section(client, "user_1", brief.USER_QUERY)

    (_, _, kwargs) = client.updated[0]
    assert kwargs["trigger"] == brief.TRIGGER
    assert "source_query" not in kwargs


def test_reconciling_a_trigger_keeps_fields_this_module_does_not_set():
    """Hindsight puts its own keys in the trigger. Replacing the object
    wholesale would drop them silently, so only the keys set here are compared
    and the rest are carried through."""
    client = FakeClient(
        models=[_model("real content", trigger={"mode": "full", "upstream_knob": 7})]
    )

    brief.provision_section(client, "user_1", brief.USER_QUERY)

    (_, _, kwargs) = client.updated[0]
    assert kwargs["trigger"]["upstream_knob"] == 7
    assert kwargs["trigger"]["mode"] == "delta"


def test_a_model_already_in_line_is_not_patched():
    """Provisioning is idempotent, so re-running it after a deploy that
    changed nothing is not one write per bank."""
    client = FakeClient(models=[_model("real content")])

    brief.provision_section(client, "user_1", brief.USER_QUERY)

    assert client.updated == []


def test_a_long_digest_is_served_whole():
    """max_tokens is advisory upstream, so a digest routinely runs longer than
    asked for. It still goes out intact: cutting it left the last line severed
    mid-word, and an unmarked half sentence can read as the opposite of the
    rule it came from."""
    content = "* a rule that must not be cut\n" + "x" * 5000
    client = FakeClient(models=[_model(content)])

    section = brief.get_section(client, "user_1", NOW)

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
                        # In line with TRIGGER, so this test exercises the
                        # read path and not reconciliation.
                        "trigger": dict(brief.TRIGGER),
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
def test_a_master_key_reads_a_brief_for_the_user_it_names(
    client, master_headers, two_users, session
):
    """The console's Brief tab was dead for every operator: scope=user was
    resolved with user_id=None, so a master key -- which has no identity of its
    own -- got InvalidScope on the one route whose whole purpose is reading
    somebody else's memory on their behalf."""
    from memory.models import User

    # Pinned to the named user's own bank: a `banks/[^/]+/` regex answers for
    # any bank, so it proves a brief was read and not whose.
    bank_id = session.get(User, two_users[0]["user_id"]).bank_id
    respx.get(url__regex=rf"{BASE}/v1/default/banks/{bank_id}/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {
                        "id": "mm-1",
                        "name": brief.BRIEF_MODEL_NAME,
                        "content": "Ask before planning.",
                        "source_query": brief.USER_QUERY,
                        "is_stale": False,
                        "last_refreshed_at": "2026-08-27T03:00:00+00:00",
                        "trigger": dict(brief.TRIGGER),
                    }
                ]
            },
        )
    )

    response = client.get(
        "/v1/session-brief",
        params={"scope": "user"},
        headers={**master_headers, "On-Behalf-Of": two_users[0]["user_id"]},
    )

    assert response.status_code == 200
    assert "Ask before planning." in response.json()["instructions"]


@respx.mock
def test_a_project_that_does_not_exist_is_not_created_by_asking_for_a_brief(
    client, two_users
):
    """create=False. A session start must never mint a project -- an agent
    opening any directory would otherwise squat a slug.

    Neither bank has a brief model, and no POST is mocked: a read that went
    back to provisioning would fail here on an unmocked request."""
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )

    response = client.get(
        "/v1/session-brief?scope=user&project_slug=never-created",
        headers=_headers(two_users[0]["key"]),
    )

    assert response.status_code == 200
    assert response.json()["sections"]["project"] is False
