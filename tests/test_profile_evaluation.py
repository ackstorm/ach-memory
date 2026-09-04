"""Non-persisting profile cost/quality measurement (SPEC Phase 4 Task 7).

`ach-memory profile-check` is the rollout gate's measurement instrument: it
reads a bank's already-provisioned `ach-memory-profile-v1`, asks Hindsight
for a NON-PERSISTING refresh preview, runs that preview through the same
normalizer real delivery uses, and reports counts, timings and error codes.

Three properties are load-bearing and each has its own section below.

1. It measures; it never mutates. Nothing on any path may create, update,
   refresh (the real refresh, as opposed to the upstream dry run) or retain
   anything, upstream or locally. The respx tests below fail if any request
   other than the mental-model listing and the dry-run-refresh preview is
   made at all.
2. It has no public surface. No FastAPI route, no MCP tool, no HTTP
   `dry-run-refresh` endpoint -- this is a local/admin CLI operation and the
   structural tests here keep it that way.
3. Everything it emits is content-free: counts, timings, modes and closed
   error/warning codes. The canary tests plant a marker in a claim, in
   evidence IDs, in the bank id and in the project slug, and assert it never
   reaches stdout, stderr, the log stream or the result object.
"""

import json
import subprocess
from dataclasses import asdict, fields
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from memory import profiles

ROOT = Path(__file__).resolve().parents[1]
REPO_SRC = ROOT / "src" / "memory"
CHART = ROOT / "deploy" / "helm" / "ach-memory"

BASE = "http://hindsight.test"
BANK = "user_11111111-1111-1111-1111-111111111111"
MM_ID = "mm-" + "1" * 32

# One marker planted in every piece of content and every identifier a test
# feeds in. Nothing this command writes may contain it.
CANARY = "zzcanaryzz"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _item(claim, *, kind="convention", origin="stated", negative=False, evidence=("m1",)):
    return {
        "claim": claim,
        "kind": kind,
        "origin": origin,
        "negative": negative,
        "evidence_ids": list(evidence),
    }


def _gotcha(claim, *, evidence=("m1",)):
    return {
        "claim": claim,
        "kind": "gotcha",
        "origin": "observed",
        "negative": False,
        "failure": "the deploy 500s",
        "cause": "the migration runs after the rollout",
        "provenance": "observed on 2026-08-01",
        "evidence_ids": list(evidence),
    }


def _preview(scope, document):
    return json.dumps({profiles._ROOT_KEY[scope]: document})


def _dry_run(preview, **overrides):
    """Exactly the shape Task 2's own respx test pinned for
    `dry_run_refresh_mental_model` -- usage, duration_ms, diff and a
    `preview_content` JSON STRING, with no `based_on` block."""
    response = {
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "duration_ms": 4321,
        "diff": {"added": 2, "removed": 1},
        "preview_content": preview,
    }
    response.update(overrides)
    return response


# ---------------------------------------------------------------------------
# Step 1: the content-free evaluation result
# ---------------------------------------------------------------------------


def test_the_result_carries_every_field_the_plan_names():
    assert {field.name for field in fields(profiles.EvaluationResult)} == {
        "scope",
        "requested_mode",
        "effective_mode",
        "outcome",
        "would_persist",
        "retrieved_fact_count",
        "used_fact_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "duration_ms",
        "schema_valid",
        "candidate_item_count",
        "delivered_item_count",
        "displacement_count",
        "curated_case_pass_count",
        "curated_case_fail_count",
        "warning_codes",
    }


def test_every_string_the_result_can_hold_comes_from_a_closed_set():
    """No field may carry free text: a result is scope, two modes, an
    outcome code, warning codes and numbers. Anything else would be a
    channel for claim text or an identifier."""
    result = profiles.evaluate_dry_run(
        "user", _dry_run(_preview("user", {"preferences": [_item("we use uv")]}))
    )

    for name, value in asdict(result).items():
        if name == "scope":
            assert value in {"user", "project"}
        elif name in {"requested_mode", "effective_mode"}:
            assert value in {"legacy", "structured"}
        elif name == "outcome":
            assert value in set(profiles.EvaluationOutcome.__args__)
        elif name == "warning_codes":
            assert set(value) <= profiles.WARNING_CODES
        else:
            assert isinstance(value, bool | int | type(None)), name


def test_the_result_serializes_to_json():
    result = profiles.evaluate_dry_run("user", _dry_run(_preview("user", {})))

    assert json.loads(json.dumps(result.to_dict()))["scope"] == "user"


def test_curated_case_counts_are_absent_for_a_real_bank_measurement():
    """`profile-check` measures one real bank's real synthesis output, which
    has no relationship to a curated adversarial corpus. Those two fields
    exist for the delivery gate that runs curated fixtures through this same
    result shape; this caller must not invent numbers for them."""
    result = profiles.evaluate_dry_run("user", _dry_run(_preview("user", {})))

    assert result.curated_case_pass_count is None
    assert result.curated_case_fail_count is None


# ---------------------------------------------------------------------------
# Step 2 (pure half): reading one dry-run-refresh response
# ---------------------------------------------------------------------------


def test_task_2s_own_fixture_is_measured_without_crashing():
    """The exact response shape the client test pins: an empty but valid
    user document, no `based_on`."""
    result = profiles.evaluate_dry_run("user", _dry_run('{"user_profile": {}}'))

    assert result.schema_valid is True
    assert result.input_tokens == 1000
    assert result.output_tokens == 200
    assert result.total_tokens == 1200
    assert result.duration_ms == 4321
    assert result.candidate_item_count == 0
    assert result.delivered_item_count == 0
    assert result.outcome == "empty_profile"
    assert "EMPTY_PREVIEW" in result.warning_codes
    assert "NO_BASED_ON" in result.warning_codes


def test_a_reported_total_token_count_wins_over_the_computed_sum():
    result = profiles.evaluate_dry_run(
        "user",
        _dry_run(
            _preview("user", {}),
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 99},
        ),
    )

    assert result.total_tokens == 99


def test_a_missing_usage_block_reports_no_tokens_rather_than_zero():
    response = _dry_run(_preview("user", {}))
    del response["usage"]

    result = profiles.evaluate_dry_run("user", response)

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.total_tokens is None
    assert "NO_USAGE" in result.warning_codes


def test_a_healthy_preview_is_ok_and_counts_its_items():
    document = {
        "preferences": [_item("we use uv", kind="preference")],
        "engineering": [_item("we pin every dependency")],
    }

    result = profiles.evaluate_dry_run("user", _dry_run(_preview("user", document)))

    assert result.outcome == "ok"
    assert result.schema_valid is True
    assert result.candidate_item_count == 2
    assert result.delivered_item_count == 2
    assert result.displacement_count == 0


def test_a_preview_that_is_not_json_is_schema_invalid_and_does_not_crash():
    result = profiles.evaluate_dry_run("user", _dry_run("not json at all"))

    assert result.schema_valid is False
    assert result.outcome == "schema_invalid"
    assert "PREVIEW_NOT_JSON" in result.warning_codes


def test_a_missing_preview_content_key_is_schema_invalid():
    response = _dry_run(_preview("user", {}))
    del response["preview_content"]

    result = profiles.evaluate_dry_run("user", response)

    assert result.schema_valid is False
    assert result.outcome == "schema_invalid"
    assert "PREVIEW_NOT_JSON" in result.warning_codes


def test_the_other_scopes_document_is_never_partially_salvaged():
    preview = _preview("project", {"conventions": [_item("we squash merges")]})

    result = profiles.evaluate_dry_run("user", preview and _dry_run(preview))

    assert result.schema_valid is False
    assert result.outcome == "schema_invalid"
    assert result.candidate_item_count == 0
    assert "PREVIEW_ROOT_MISSING" in result.warning_codes


def test_an_item_the_schema_forbids_fails_the_document_but_still_counts():
    """Whole-document validity is the rollout gate's `schema_valid`: Hindsight
    was handed this exact JSON Schema, so one item it cannot satisfy is a
    finding. The per-item counts stay meaningful anyway -- the normalizer
    drops a bad item alone, never the response."""
    document = {
        "preferences": [
            _item("we use uv", kind="preference"),
            # An observed preference is evidence-only per SPEC 6.4.
            _item("we use pnpm", kind="preference", origin="observed"),
        ]
    }

    result = profiles.evaluate_dry_run("user", _dry_run(_preview("user", document)))

    assert result.schema_valid is False
    assert result.outcome == "schema_invalid"
    assert result.candidate_item_count == 2
    assert result.delivered_item_count == 1
    assert "DOCUMENT_INVALID" in result.warning_codes
    assert "INELIGIBLE_ITEMS_DROPPED" in result.warning_codes


def test_an_over_budget_preview_reports_displacement_and_never_over_delivers():
    document = {
        "conventions": [
            _item(f"convention number {index}", evidence=(f"m{index}",))
            for index in range(profiles.PROJECT_PROFILE_BUDGET + 4)
        ]
    }

    result = profiles.evaluate_dry_run("project", _dry_run(_preview("project", document)))

    assert result.candidate_item_count == profiles.PROJECT_PROFILE_BUDGET + 4
    assert result.delivered_item_count == profiles.PROJECT_PROFILE_BUDGET
    assert result.displacement_count == 4
    assert "BUDGET_TRUNCATED" in result.warning_codes


def test_displacement_counts_only_the_budget_cut_not_every_dropped_item():
    """An item dropped for ineligibility never made it to the ranked list, so
    it was never displaced by anything -- displacement is the budget prefix
    cut alone."""
    document = {
        "conventions": [_item(f"convention number {index}") for index in range(3)],
        # Rejected outright: a project category cannot hold a preference.
        "workflow": [_item("we like tabs", kind="preference")],
    }

    result = profiles.evaluate_dry_run("project", _dry_run(_preview("project", document)))

    assert result.candidate_item_count == 4
    assert result.delivered_item_count == 3
    assert result.displacement_count == 0


def test_duplicate_claims_merge_and_say_so():
    document = {
        "conventions": [
            _item("we squash merges", evidence=("m1",)),
            _item("we  squash   merges", evidence=("m2",)),
        ]
    }

    result = profiles.evaluate_dry_run("project", _dry_run(_preview("project", document)))

    assert result.candidate_item_count == 2
    assert result.delivered_item_count == 1
    assert "DUPLICATE_ITEMS_MERGED" in result.warning_codes


def test_a_response_that_carries_based_on_grounds_against_it_and_counts_facts():
    document = {
        "conventions": [
            _item("we squash merges", evidence=("m1",)),
            _item("we deploy on fridays", evidence=("ghost",)),
        ]
    }
    response = _dry_run(
        _preview("project", document), based_on={"memories": ["m1", "m2", "m3"]}
    )

    result = profiles.evaluate_dry_run("project", response)

    assert result.retrieved_fact_count == 3
    assert result.used_fact_count == 1
    assert result.delivered_item_count == 1
    assert "UNGROUNDED_ITEMS_DROPPED" in result.warning_codes
    assert "NO_BASED_ON" not in result.warning_codes


def test_without_based_on_fact_accounting_is_absent_rather_than_invented():
    document = {"conventions": [_item("we squash merges", evidence=("m1",))]}

    result = profiles.evaluate_dry_run("project", _dry_run(_preview("project", document)))

    assert result.retrieved_fact_count is None
    assert result.used_fact_count is None
    assert "NO_BASED_ON" in result.warning_codes
    # Grounding is the one gate a preview cannot verify, so it is declared
    # unmeasured instead of failing every item and reporting an empty
    # profile that real delivery would never produce.
    assert result.delivered_item_count == 1


def test_output_tokens_over_the_provisioned_ceiling_are_a_budget_failure():
    document = {"conventions": [_item("we squash merges")]}
    response = _dry_run(
        _preview("project", document),
        usage={"input_tokens": 10, "output_tokens": profiles.PROJECT_PROFILE_MAX_TOKENS + 1},
    )

    result = profiles.evaluate_dry_run("project", response)

    assert result.outcome == "budget_exceeded"


def test_would_persist_follows_the_diff_the_preview_reports():
    document = {"conventions": [_item("we squash merges")]}
    changed = profiles.evaluate_dry_run(
        "project", _dry_run(_preview("project", document), diff={"added": 1, "removed": 0})
    )
    unchanged = profiles.evaluate_dry_run(
        "project", _dry_run(_preview("project", document), diff={"added": 0, "removed": 0})
    )

    assert changed.would_persist is True
    assert unchanged.would_persist is False
    assert "NO_DIFF" not in changed.warning_codes


def test_without_a_diff_would_persist_falls_back_to_the_outcome():
    document = {"conventions": [_item("we squash merges")]}
    response = _dry_run(_preview("project", document))
    del response["diff"]

    result = profiles.evaluate_dry_run("project", response)

    assert result.would_persist is True
    assert "NO_DIFF" in result.warning_codes


def test_profile_check_evaluates_the_structured_path_in_both_mode_fields():
    """`profile-check` has only one path to measure: Task 5 wired no fallback
    from structured delivery to the prose section, so nothing here can
    degrade from one mode to another. Both fields are still reported because
    the delivery gate that shares this result shape compares a legacy run
    against a structured run of the same case."""
    result = profiles.evaluate_dry_run("user", _dry_run(_preview("user", {})))

    assert result.requested_mode == "structured"
    assert result.effective_mode == "structured"


def test_every_warning_code_emitted_anywhere_is_in_the_closed_set():
    responses = [
        _dry_run("not json"),
        _dry_run(_preview("project", {})),
        _dry_run(_preview("user", {"preferences": [_item("x", kind="preference")]})),
        _dry_run(
            _preview("project", {"gotchas": [_gotcha("the deploy breaks")]}),
            based_on={"memories": []},
        ),
    ]

    for response in responses:
        for scope in ("user", "project"):
            result = profiles.evaluate_dry_run(scope, response)
            assert set(result.warning_codes) <= profiles.WARNING_CODES


def test_compile_profile_still_returns_exactly_what_it_did_before():
    """The evaluator needs the ranked list before truncation, so
    `compile_profile`'s body was split into a candidate pass and a ranking
    pass. Same inputs, same output, same budget prefix."""
    document = {
        "conventions": [
            _item(f"convention number {index}", evidence=(f"m{index}",))
            for index in range(profiles.PROJECT_PROFILE_BUDGET + 2)
        ]
    }
    reflect_response = {
        "structured_output": {"project_profile": document},
        "based_on": {"memories": [f"m{index}" for index in range(50)]},
    }

    items = profiles.compile_profile("project", reflect_response)

    assert len(items) == profiles.PROJECT_PROFILE_BUDGET
    assert [item.claim for item in items] == [
        item.claim
        for item in profiles._rank_candidates(
            profiles._profile_candidates("project", reflect_response)
        )[: profiles.PROJECT_PROFILE_BUDGET]
    ]


# ---------------------------------------------------------------------------
# Step 2 (CLI half): `ach-memory profile-check`
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_bank(monkeypatch):
    """The same direct SQLAlchemy resolution `capture-check` uses, faked at
    the session boundary so this file needs no database."""

    class _FakeProject:
        bank_id = BANK
        owner_type = "user"
        owner_id = "usr_1"

    class _FakeUser:
        bank_id = BANK

    class _FakeQuery:
        def join(self, *args):
            return self

        def filter(self, *args):
            return self

        def filter_by(self, **kwargs):
            return self

        def first(self):
            return _FakeProject()

    class _FakeDB:
        def query(self, model):
            return _FakeQuery()

        def get(self, model, identifier):
            return _FakeUser()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("memory.db.session_scope", lambda: _FakeDB())


def _mock_listing(models):
    return respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": models}))


def _mock_dry_run(response):
    return respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/dry-run-refresh$"
    ).mock(return_value=httpx.Response(200, json=response))


def _profile_model():
    return {"id": MM_ID, "name": profiles.PROFILE_MODEL_NAME}


@respx.mock
def test_profile_check_reports_a_healthy_profile_and_exits_zero(
    fake_bank, configured_env, capsys
):
    from memory import cli

    _mock_listing([_profile_model()])
    _mock_dry_run(
        _dry_run(_preview("user", {"preferences": [_item("we use uv", kind="preference")]}))
    )

    exit_code = cli.main(["profile-check", "--scope", "user", "--project", "acme-api"])

    assert exit_code == 0
    assert "outcome: ok" in capsys.readouterr().out


@respx.mock
def test_an_unverified_grounding_run_says_so_in_words(fake_bank, configured_env, capsys):
    """A preview with no `based_on` still exits 0 and prints `outcome: ok` --
    deliberately, since failing on it could fail every run there will ever
    be. So the one thing an operator recording seven runs against the
    rollout gate must not miss is that the grounding gate was a no-op, and
    a bare NO_BASED_ON token on a warnings line does not communicate that.
    """
    from memory import cli

    _mock_listing([_profile_model()])
    _mock_dry_run(
        _dry_run(_preview("user", {"preferences": [_item("we use uv", kind="preference")]}))
    )

    exit_code = cli.main(["profile-check", "--scope", "user", "--project", "acme-api"])

    report = capsys.readouterr().out
    assert exit_code == 0
    assert "outcome: ok" in report
    assert "grounding: NOT VERIFIED" in report
    assert "based_on" in report


@respx.mock
def test_a_verified_grounding_run_says_that_too(fake_bank, configured_env, capsys):
    from memory import cli

    _mock_listing([_profile_model()])
    _mock_dry_run(
        _dry_run(
            _preview("user", {"preferences": [_item("we use uv", kind="preference")]}),
            based_on={"memories": ["m1"]},
        )
    )

    cli.main(["profile-check", "--scope", "user", "--project", "acme-api"])

    report = capsys.readouterr().out
    assert "grounding: verified" in report
    assert "NOT VERIFIED" not in report


@respx.mock
def test_profile_check_emits_the_result_as_json(fake_bank, configured_env, capsys):
    from memory import cli

    _mock_listing([_profile_model()])
    _mock_dry_run(
        _dry_run(_preview("user", {"preferences": [_item("we use uv", kind="preference")]}))
    )

    exit_code = cli.main(
        ["profile-check", "--scope", "user", "--project", "acme-api", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "ok"
    assert payload["scope"] == "user"
    assert payload["delivered_item_count"] == 1
    assert set(payload) == {field.name for field in fields(profiles.EvaluationResult)}


@respx.mock
def test_profile_check_exits_nonzero_when_the_bank_has_no_profile_model(
    fake_bank, configured_env, capsys
):
    from memory import cli

    _mock_listing([{"id": "mm-other", "name": "ach-memory-session-brief"}])

    exit_code = cli.main(
        ["profile-check", "--scope", "user", "--project", "acme-api", "--json"]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "no_model"


@respx.mock
def test_profile_check_exits_nonzero_on_an_unparsable_preview(
    fake_bank, configured_env, capsys
):
    from memory import cli

    _mock_listing([_profile_model()])
    _mock_dry_run(_dry_run("{not json"))

    exit_code = cli.main(
        ["profile-check", "--scope", "user", "--project", "acme-api", "--json"]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "schema_invalid"


@respx.mock
def test_profile_check_reports_an_upstream_failure_instead_of_a_traceback(
    fake_bank, configured_env, capsys
):
    from memory import cli

    _mock_listing([_profile_model()])
    respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/dry-run-refresh$"
    ).mock(return_value=httpx.Response(404, json={"detail": "gone"}))

    exit_code = cli.main(
        ["profile-check", "--scope", "user", "--project", "acme-api", "--json"]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "upstream_error"


def test_profile_check_rejects_an_unknown_project(monkeypatch, configured_env, capsys):
    from memory import cli

    class _FakeQuery:
        def join(self, *args):
            return self

        def filter(self, *args):
            return self

        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

    class _FakeDB:
        def query(self, model):
            return _FakeQuery()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("memory.db.session_scope", lambda: _FakeDB())

    exit_code = cli.main(["profile-check", "--scope", "user", "--project", "nope"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no such project" in captured.err


def test_profile_check_needs_a_user_owned_project_for_the_user_scope(
    monkeypatch, configured_env, capsys
):
    from memory import cli

    class _FakeProject:
        bank_id = BANK
        owner_type = "group"
        owner_id = "grp_1"

    class _FakeQuery:
        def join(self, *args):
            return self

        def filter(self, *args):
            return self

        def filter_by(self, **kwargs):
            return self

        def first(self):
            return _FakeProject()

    class _FakeDB:
        def query(self, model):
            return _FakeQuery()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("memory.db.session_scope", lambda: _FakeDB())

    exit_code = cli.main(["profile-check", "--scope", "user", "--project", "acme-api"])

    assert exit_code == 2
    assert "user-owned project" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Step 3: no mutation, no public surface, no content
# ---------------------------------------------------------------------------


@respx.mock
def test_profile_check_makes_only_a_listing_and_a_dry_run_request(
    fake_bank, configured_env
):
    """A catch-all route stands behind the two the command is allowed to
    make: any create, update, refresh or retain call -- upstream or on a
    path this test did not anticipate -- lands there and fails the test."""
    from memory import cli

    listing = _mock_listing([_profile_model()])
    dry_run = _mock_dry_run(
        _dry_run(_preview("user", {"preferences": [_item("we use uv", kind="preference")]}))
    )
    catch_all = respx.route().mock(return_value=httpx.Response(500))

    cli.main(["profile-check", "--scope", "user", "--project", "acme-api", "--json"])

    assert listing.called
    assert dry_run.called
    assert not catch_all.called
    methods_and_paths = {
        (call.request.method, call.request.url.path) for call in respx.calls
    }
    assert methods_and_paths == {
        ("GET", f"/v1/default/banks/{BANK}/mental-models"),
        ("POST", f"/v1/default/banks/{BANK}/mental-models/{MM_ID}/dry-run-refresh"),
    }


def test_profile_check_names_no_mutating_client_method():
    import inspect

    from memory import cli

    source = inspect.getsource(cli._profile_check)

    for forbidden in (
        "create_mental_model(",
        "update_mental_model(",
        "delete_mental_model(",
        # The REAL refresh, which persists. Never to be confused with the
        # dry run: anchored on the leading dot so
        # `dry_run_refresh_mental_model(` does not satisfy it.
        ".refresh_mental_model(",
        ".retain(",
        "retain_items(",
        ".curate(",
        ".append_",
        "provision_profile(",
        # Bank resolution must never create one.
        "create=True",
    ):
        assert forbidden not in source, forbidden
    assert "dry_run_refresh_mental_model(" in source


def test_the_evaluator_is_reachable_from_the_cli_alone():
    """No FastAPI route and no MCP tool: cost/quality evaluation is an
    explicit local/admin operation, and adding an HTTP dry-run-refresh
    endpoint is exactly what the plan forbids."""
    for directory in ("api", "mcp"):
        for path in (REPO_SRC / directory).rglob("*.py"):
            source = path.read_text()
            assert "dry_run_refresh_mental_model" not in source, path
            assert "evaluate_dry_run" not in source, path
            assert "profile-check" not in source, path
            assert "EvaluationResult" not in source, path


def test_the_dry_run_preview_is_called_from_exactly_one_place():
    callers = [
        path.relative_to(REPO_SRC).as_posix()
        for path in REPO_SRC.rglob("*.py")
        if "dry_run_refresh_mental_model(" in path.read_text()
    ]

    assert sorted(callers) == ["cli.py", "hindsight/client.py"]


@respx.mock
def test_no_content_or_identifier_reaches_anything_profile_check_writes(
    monkeypatch, configured_env, capsys, caplog
):
    import logging

    from memory import cli

    canary_bank = f"user_{CANARY}-bank"

    class _FakeProject:
        bank_id = canary_bank
        owner_type = "user"
        owner_id = "usr_1"

    class _FakeUser:
        bank_id = canary_bank

    class _FakeQuery:
        def join(self, *args):
            return self

        def filter(self, *args):
            return self

        def filter_by(self, **kwargs):
            return self

        def first(self):
            return _FakeProject()

    class _FakeDB:
        def query(self, model):
            return _FakeQuery()

        def get(self, model, identifier):
            return _FakeUser()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("memory.db.session_scope", lambda: _FakeDB())

    document = {
        "preferences": [
            _item(
                f"we use {CANARY} for everything",
                kind="preference",
                evidence=(f"mem-{CANARY}-1", f"mem-{CANARY}-2"),
            )
        ]
    }
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{canary_bank}/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": [_profile_model()]}))
    respx.post(
        url__regex=(
            rf"{BASE}/v1/default/banks/{canary_bank}/mental-models/{MM_ID}/dry-run-refresh$"
        )
    ).mock(
        return_value=httpx.Response(
            200,
            json=_dry_run(
                _preview("user", document), based_on={"memories": [f"mem-{CANARY}-1"]}
            ),
        )
    )

    # Un-mute httpx first, or this asserts nothing. httpx logs the full
    # request URL -- bank id and all -- at INFO, and `_profile_check` mutes
    # that logger process-wide. So does `create_app()`, and in a full-suite
    # run some earlier module has already built an app and left the level at
    # WARNING, which would make the caplog assertion below pass even with
    # the muting line in `_profile_check` deleted outright. Resetting to
    # NOTSET (restored by monkeypatch afterwards) makes the leak reachable
    # again, so the assertion depends only on the command's own muting.
    # Done before `caplog.at_level`, whose own setLevel clears logging's
    # per-logger effective-level cache.
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)

    with caplog.at_level(logging.DEBUG):
        for extra in ([], ["--json"]):
            cli.main(
                ["profile-check", "--scope", "user", "--project", f"{CANARY}-slug", *extra]
            )
            captured = capsys.readouterr()
            assert CANARY not in captured.out
            assert CANARY not in captured.err
    assert CANARY not in caplog.text


def test_the_result_object_itself_holds_no_content_or_identifier():
    document = {
        "preferences": [
            _item(
                f"we use {CANARY} for everything",
                kind="preference",
                evidence=(f"mem-{CANARY}-1",),
            )
        ]
    }

    result = profiles.evaluate_dry_run("user", _dry_run(_preview("user", document)))

    assert CANARY not in json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Step 4: the opt-in nightly evaluator, disabled by default
# ---------------------------------------------------------------------------


REQUIRED_SET = [
    "--set", "config.databaseUrl=postgresql://x/x",
    "--set", "config.hindsight.url=http://hindsight.test",
    "--set", "masterKeySecret.value=abcd",
]

ENABLE_WITH_TARGET = [
    "--set", "profileEvaluator.enabled=true",
    "--set", "profileEvaluator.targets[0].scope=user",
    "--set", "profileEvaluator.targets[0].project=acme-api",
]


def _render(*extra_args: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "test", str(CHART), *REQUIRED_SET, *extra_args],
        capture_output=True, text=True, check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _cronjob(docs: list[dict]) -> dict | None:
    for doc in docs:
        if doc.get("kind") == "CronJob":
            return doc
    return None


def test_helm_renders_no_profile_evaluator_by_default():
    assert _cronjob(_render()) is None


def test_helm_renders_the_profile_evaluator_only_with_explicit_enablement():
    cronjob = _cronjob(_render(*ENABLE_WITH_TARGET))

    assert cronjob is not None
    containers = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert containers[0]["command"] == [
        "ach-memory", "profile-check", "--scope", "user", "--project", "acme-api", "--json",
    ]


def test_the_second_gate_suspends_the_schedule_until_it_is_turned_on():
    """`enabled` renders the CronJob so an operator can review image,
    schedule and targets in the cluster; `evaluatorEnabled` is what lets
    Kubernetes create a Job from it. Staged, not running."""
    staged = _cronjob(_render(*ENABLE_WITH_TARGET))
    running = _cronjob(
        _render(*ENABLE_WITH_TARGET, "--set", "profileEvaluator.evaluatorEnabled=true")
    )

    assert staged["spec"]["suspend"] is True
    assert running["spec"]["suspend"] is False


def test_helm_refuses_to_render_an_enabled_evaluator_with_no_targets():
    result = subprocess.run(
        ["helm", "template", "test", str(CHART), *REQUIRED_SET,
         "--set", "profileEvaluator.enabled=true"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "profileEvaluator.targets" in result.stderr


def test_the_profile_evaluator_serves_nothing():
    docs = _render(
        *ENABLE_WITH_TARGET,
        "--set", "ingress.enabled=true",
        "--set", "ingress.host=ach.example.com",
    )

    for doc in docs:
        component = doc.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/component"
        )
        if component == "profile-evaluator":
            assert doc["kind"] == "CronJob"
    containers = _cronjob(docs)["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ]
    assert all("ports" not in container for container in containers)


def test_one_container_renders_per_target():
    cronjob = _cronjob(
        _render(
            *ENABLE_WITH_TARGET,
            "--set", "profileEvaluator.targets[1].scope=project",
            "--set", "profileEvaluator.targets[1].project=acme-api",
        )
    )

    containers = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 2
    assert [container["name"] for container in containers] == ["check-0", "check-1"]


def test_helm_lint_passes_with_the_evaluator_enabled():
    result = subprocess.run(
        ["helm", "lint", str(CHART), *REQUIRED_SET, *ENABLE_WITH_TARGET],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_chart_ships_both_gates_off_and_no_target():
    values = yaml.safe_load((CHART / "values.yaml").read_text())

    assert values["profileEvaluator"]["enabled"] is False
    assert values["profileEvaluator"]["evaluatorEnabled"] is False
    assert values["profileEvaluator"]["targets"] == []


def _compose_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.setdefault("MEMORY_MASTER_KEY_HASH", "unused")
    env.setdefault("HINDSIGHT_LLM_BASE_URL", "http://x")
    env.setdefault("HINDSIGHT_LLM_API_KEY", "x")
    return env


def _compose_services(*extra_args: str) -> list[str]:
    for binary in (["docker", "compose"], ["docker-compose"]):
        result = subprocess.run(
            [*binary, "-f", str(ROOT / "docker-compose.yml"), *extra_args,
             "config", "--services"],
            capture_output=True, text=True, check=False, env=_compose_env(), cwd=ROOT,
        )
        if result.returncode == 0:
            return result.stdout.split()
    pytest.skip("no working docker compose CLI available")


def test_compose_excludes_the_profile_evaluator_by_default():
    assert "profile-evaluator" not in _compose_services()


def test_compose_includes_the_profile_evaluator_under_its_own_profile():
    assert "profile-evaluator" in _compose_services("--profile", "profile-evaluation")


def test_compose_ships_no_target_for_the_evaluator():
    """The default Compose stack must still interpolate cleanly, so the two
    target variables carry empty defaults rather than `:?` -- an unset target
    fails in argparse at run time instead of breaking `docker compose config`
    for every service in the file."""
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "${MEMORY_PROFILE_CHECK_SCOPE:-}" in compose
    assert "${MEMORY_PROFILE_CHECK_PROJECT:-}" in compose


def test_an_unset_compose_target_is_rejected_by_the_cli():
    from memory import cli

    assert cli.main(["profile-check", "--scope", "", "--project", ""]) == 2


def test_the_env_example_ships_the_targets_unset():
    env_example = (ROOT / ".env.example").read_text()

    assert "# MEMORY_PROFILE_CHECK_SCOPE=" in env_example
    assert "# MEMORY_PROFILE_CHECK_PROJECT=" in env_example


def test_the_seven_run_minimum_is_recorded_where_the_evaluator_is_wired():
    """A process requirement for the humans running this job, not something
    the code enforces -- so it has to be written down next to the wiring."""
    for path in (CHART / "values.yaml", ROOT / "docker-compose.yml"):
        text = path.read_text().lower()
        assert "seven" in text, path
        assert "token" in text, path


def test_the_gate_comment_warns_that_grounding_may_be_unmeasured():
    """The counting rule has to sit with the seven-run rule, or an operator
    tallying `ok` runs never learns that some of them checked one gate
    fewer than the rest."""
    for path in (CHART / "values.yaml", ROOT / "docker-compose.yml"):
        text = path.read_text()
        assert "NO_BASED_ON" in text, path
        assert "NOT VERIFIED" in text, path
