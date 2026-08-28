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


def test_models_asks_for_the_content_and_renders_it(client):
    """GET /v1/mental-models omits `content` unless detail=full is asked for.

    Without it the tab fell back to `source_query` and drew the prompt in the
    place the synthesis belongs, so an operator reading the panel saw the
    question and concluded the summary was missing. brief.py already passes
    detail="full" for the same reason, which is why /v1/session-brief showed
    content while this panel never did.
    """
    body = client.get("/admin/ui").text

    assert 'detail: "full"' in body
    assert "model.content" in body


def test_models_does_not_render_the_upstream_placeholder_as_content(client):
    """Hindsight returns "Generating content..." for a model that exists but
    has never refreshed, and that string is truthy.

    Rendered as content it reads as work in progress, so a model whose refresh
    has been failing for a week looks like one that is busy. brief.py:130
    treats the placeholder as absent and tests/test_brief.py pins it; the
    console has to agree, or the two drift and only the server is tested.
    """
    body = client.get("/admin/ui").text

    assert 'PLACEHOLDER = "Generating content..."' in body
    assert "=== PLACEHOLDER" in body


def test_brief_shows_the_instructions_with_whitespace_intact(client):
    """The brief is a line-per-rule instruction list. Collapsing its whitespace
    would show something the agent never receives."""
    body = client.get("/admin/ui").text

    assert "brief-text" in body
    assert "white-space:pre-wrap" in body


def test_the_console_can_edit_project_metadata(client):
    """A metadata record nobody can set is a column, not a feature.

    The brief composes name, canonical_spec and purpose into a project's
    orientation, and until this panel existed nothing in the product could
    write them. All three ids are pinned because a form wired for two looks
    finished: the third input is simply never sent, the column stays null,
    and there is no error anywhere to show for it.
    """
    body = client.get("/admin/ui").text

    assert 'data-panel="projects"' in body
    assert '<section class="panel" id="projects" hidden>' in body
    assert "/v1/projects/" in body
    assert '"PATCH"' in body

    for field in ("name", "canonical_spec", "purpose"):
        assert f'id="pm-{field}"' in body
        assert f'{field}: metadataValue("pm-{field}")' in body

    # An emptied box is an explicit null, never "": the API's min_length=1
    # makes an empty string a 422 rather than a clear, so a form that sent
    # strings would fail the first time anyone retracted a field.
    assert 'value === "" ? null' in body

    # The 256 cap is visible while typing, not only in the 422 afterwards.
    assert "PURPOSE_MAX = 256" in body

    # Save is bound to the project the boxes were filled from, not to whatever
    # the picker happens to read. Pick A, pick B, let B's load fail: the form
    # hides but still holds A's values, and a save would write A's orientation
    # onto B with nothing on screen saying so.
    assert "loadedProject !== slug" in body

    # The picker lists every project, not the fleet summary the other tabs
    # use: that one groups activity over the last 24 hours, so a project
    # created a minute ago -- exactly when its purpose wants stating -- would
    # not be in it. The notice claims this in words; this pins it.
    assert 'api("/v1/projects")' in body
