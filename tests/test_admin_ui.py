def test_the_console_is_served(client):
    response = client.get("/admin/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ach-memory" in response.text


def test_the_console_loads_no_third_party_code(client):
    """The page holds the master key. A CDN script tag would put that key one
    supply-chain compromise away from an attacker."""
    body = client.get("/admin/ui").text

    assert "http://" not in body
    assert "https://" not in body


def test_peek_reads_the_field_names_hindsight_actually_returns(client):
    """Reading a field that does not exist fails silently, and did.

    Peek asked each item for `type`, `created_at`, `metadata.agent` and
    `metadata.git_locator`. Hindsight returns `fact_type` and `date`, and
    returns no agent and no git_locator at all -- so every row rendered
    "memory" with two em-dashes while the payload underneath held the type,
    the timestamp, the state and the proof count. No error, no empty page,
    just a panel that quietly said nothing.
    """
    body = client.get("/admin/ui").text

    assert "item.fact_type" in body
    assert "item.date" in body
    assert "item.state" in body
    assert "item.memory_type" not in body
    assert "item.created_at" not in body
    assert "metadata?.agent" not in body
    assert "metadata?.git_locator" not in body


def test_the_console_can_be_turned_off(configured_env, monkeypatch):
    from fastapi.testclient import TestClient

    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_ADMIN_UI_ENABLED", "false")
    get_settings.cache_clear()

    assert TestClient(create_app()).get("/admin/ui").status_code == 404


def test_models_reads_freshness_from_history_not_from_last_refreshed_at(client):
    """last_refreshed_at is what a model claims; history is what happened.

    Measured in production on 2026-08-27: session-brief-user reported
    last_refreshed_at equal to its created_at while its content had actually
    been rewritten ten minutes later, and on another bank the two agreed to
    170ms. A panel that read the claim would have been confidently wrong about
    the one model anybody cared about, and right everywhere else -- which is
    the shape of a bug nobody finds.
    """
    body = client.get("/admin/ui").text

    assert "history[0].changed_at" in body, "the docstring must match the code"
    assert "function freshness(" in body
    # The divergence is surfaced, not silently resolved in favour of either.
    assert "diverges" in body
    assert "Trust the history." in body


def test_models_makes_no_op_success_and_failure_visually_distinct(client):
    """The reflect outage hid for six days behind "took 0.003s", which is what
    a no-op looks like when nothing renders it differently from a success. All
    four outcomes get their own class, including the one this console cannot
    resolve at all."""
    body = client.get("/admin/ui").text

    for cls in ("mm-fresh", "mm-stale", "mm-blind", "mm-bad"):
        assert f".{cls}{{" in body, f"{cls} has no colour of its own"
    assert "wrote content" in body
    assert "last refresh failed" in body
    # Absence of a trace is an answer, not a blank cell.
    assert "No trace kept." in body


def test_models_never_folds_reasoning_tokens_into_a_total(client):
    """Reasoning tokens bill at the output rate on the Gemini family and never
    appear in the visible response. A total that absorbs them under-reports
    precisely the cost keep_trace was switched on to expose."""
    body = client.get("/admin/ui").text

    assert "thoughts_tokens" in body
    assert "thinking" in body
    # input/output stay separate columns rather than being summed.
    assert "u.input_tokens" in body and "u.output_tokens" in body
    assert "total_tokens" not in body, "a total here would hide the thinking tokens"


def test_models_states_the_two_limits_of_keep_trace(client):
    """A trace answers why the LAST refresh did what it did, and only where the
    flag was already on. Neither limit is obvious, and a console that implies
    otherwise re-creates the false confidence it exists to remove."""
    body = client.get("/admin/ui").text

    assert "not retroactive" in body
    assert "most recent refresh" in body


def test_brief_reports_both_sections_from_the_response_not_from_the_prose(client):
    """A missing section leaves no marker in the composed text.

    /v1/session-brief treats a project the caller cannot reach as a missing
    section rather than an error, so a brief that lost half its content still
    returns 200 with prose that reads perfectly well. The booleans are the only
    honest source; inferring presence from the text would guess.
    """
    body = client.get("/admin/ui").text

    assert "response?.sections" in body
    assert "sections.user" in body and "sections.project" in body
    assert "The user section is missing." in body


def test_brief_sends_the_scope_query_param_it_cannot_omit(client):
    """`scope` is required on GET /v1/session-brief even though the user half
    is resolved from on-behalf-of and never from a param. Calling it with no
    query string at all is a 422, not a user-only brief."""
    body = client.get("/admin/ui").text

    assert '{ scope: "user" }' in body
    assert '"on-behalf-of"' in body, "the subject travels as a header (SPEC §16.5)"


def test_brief_shows_the_instructions_with_whitespace_intact(client):
    """The brief is a line-per-rule instruction list. Collapsing its whitespace
    would show something the agent never receives."""
    body = client.get("/admin/ui").text

    assert "brief-text" in body
    assert "white-space:pre-wrap" in body
