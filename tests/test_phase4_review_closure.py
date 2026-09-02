"""Regressions for the four Phase 4 final-review findings, closed before
Phase 5 recall work adds another consumer of `brief.py`/`profiles.py`.

See docs/superpowers/plans/2026-09-02-memory-quality-phase-5-read-only-recall.md
Task 0. Each finding gets its own test(s) here rather than folding into
tests/test_brief.py, so the closure this file proves stays a single,
reviewable unit distinct from the ongoing brief/profile suites.

Fixture helpers are copied, not imported, from tests/test_brief.py -- same
rationale as that file's own `_profile_item` docstring: importing another
suite's fixtures couples two files that must stay free to change
independently.
"""

from datetime import UTC, datetime, timedelta

from memory import brief, profiles

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class FakeClient:
    """Records nothing; only ever read from in this file."""

    def __init__(self, models):
        self.models = models

    def list_mental_models(self, bank_id, **kwargs):
        return {"mental_models": self.models}


def _profile_item(**overrides) -> dict:
    fields = {
        "claim": "Run the focused tests before the full suite.",
        "kind": "convention",
        "origin": "confirmed",
        "negative": False,
        "failure": None,
        "cause": None,
        "reproduction": None,
        "provenance": "Accepted workflow convention.",
        "evidence_ids": ["mem-1"],
    }
    fields.update(overrides)
    return fields


def _reflect(scope, categories) -> dict:
    found: list[str] = []
    for items in categories.values():
        for item in items:
            for evidence_id in item.get("evidence_ids") or []:
                if evidence_id not in found:
                    found.append(evidence_id)
    based_on = {"memories": [{"id": evidence_id} for evidence_id in found]}
    root = "user_profile" if scope == "user" else "project_profile"
    return {"structured_output": {root: categories}, "based_on": based_on}


def _profile_model(reflect_response, *, refreshed=NOW, stale=False):
    return {
        "id": "mm-profile-1",
        "name": profiles.PROFILE_MODEL_NAME,
        "content": "Trap prose: only a legacy loader may ever read this.",
        "is_stale": stale,
        "last_refreshed_at": refreshed.isoformat() if refreshed is not None else None,
        "reflect_response": reflect_response,
    }


def _structured_section(models, scope="user", now=NOW):
    return brief.get_structured_section(FakeClient(models), "bank-1", scope, now)


# -- Finding 1: valid maximum structured lines can drop mandatory Working
# -- State while both optional profiles survive.

_MAX_CLAIM = "x" * 320  # profiles.ProfileClaim's own max_length


def _near_max_items(prefix: str, count: int) -> list[dict]:
    return [
        _profile_item(claim=f"{prefix}{n} {_MAX_CLAIM}"[:320], evidence_ids=[f"{prefix}{n}"])
        for n in range(count)
    ]


def _near_max_user_section() -> brief.Section:
    items = {"interaction": _near_max_items("u", profiles.USER_PROFILE_BUDGET)}
    return _structured_section([_profile_model(_reflect("user", items))])


def _near_max_project_section() -> brief.Section:
    items = {"conventions": _near_max_items("p", profiles.PROJECT_PROFILE_BUDGET)}
    return _structured_section([_profile_model(_reflect("project", items))], scope="project")


# A budget generous enough that two near-maximum profile floors each fit on
# their own, but not generous enough for both floors plus Working State's --
# measured empirically against the pre-fix allocator, which put both
# profiles in and Working State nowhere.
_STARVING_BUDGET = 1300

_WORKING_STATE_TEXT = "objective: ship the feature\ncurrent: refactor brief\nage: 1h\nsource: sess-1"


def test_a_maximal_index_profile_pair_cannot_starve_working_states_floor():
    """Two near-maximum structured profiles must not be able to leave
    Working State, the one section describing what the agent is doing right
    now, out of the index tier entirely."""
    instructions = brief.compose_index(
        1,
        "ach-memory",
        _near_max_user_section(),
        None,
        _near_max_project_section(),
        brief.Section(_WORKING_STATE_TEXT, NOW.isoformat()),
        _STARVING_BUDGET,
    )

    survived = brief.survived(instructions)
    assert survived["working_state"], survived
    assert not (survived["user"] and survived["project"]), (
        "a profile should have yielded its floor to Working State, not the other way round: "
        f"{survived}"
    )


def test_a_maximal_full_profile_pair_cannot_starve_working_states_floor():
    """Same defect, full tier: `compose_full`'s floor loop tried every
    section's floor independently in emission order, so two profiles ahead
    of Working State in that order could each fit their own floor and leave
    nothing for Working State's."""
    instructions = brief.compose_full(
        1,
        "ach-memory",
        _near_max_user_section(),
        None,
        _near_max_project_section(),
        brief.Section(_WORKING_STATE_TEXT, NOW.isoformat()),
        _STARVING_BUDGET,
    )

    survived = brief.survived(instructions)
    assert survived["working_state"], survived
    assert not (survived["user"] and survived["project"]), (
        "a profile should have yielded its floor to Working State, not the other way round: "
        f"{survived}"
    )


# -- Finding 2: `is_stale=true` structured output was served for up to the
# -- legacy prose grace period instead of failing closed immediately.


def test_a_freshly_refreshed_but_stale_structured_profile_is_never_served():
    """A structured item is delivered as current typed truth with no room
    for an agent to hedge it. Staleness must fail closed the instant Hindsight
    marks it, not after `get_section`'s multi-day prose grace period."""
    response = _reflect("user", {"interaction": [_profile_item()]})
    model = _profile_model(response, refreshed=NOW - timedelta(minutes=1), stale=True)

    section = _structured_section([model])

    assert section is None


def test_the_legacy_prose_grace_period_is_unaffected():
    """The fix is scoped to the structured loader; `get_section`'s own
    grace-period policy for legacy markdown prose must survive unchanged."""
    client = FakeClient(
        models=[
            {
                "id": "mm-legacy",
                "name": brief.BRIEF_MODEL_NAME,
                "content": "still-fresh-enough prose",
                "source_query": brief.USER_QUERY,
                "is_stale": True,
                "last_refreshed_at": (NOW - timedelta(minutes=1)).isoformat(),
                "trigger": dict(brief.TRIGGER),
            }
        ]
    )

    section = brief.get_section(client, "bank-1", NOW)

    assert section is not None
    assert section.text == "still-fresh-enough prose"


# -- Finding 3: gotcha failure dedup used substring containment, so an
# -- unrelated failure whose normalized text happened to sit inside the
# -- claim -- or a claim that merely mentions the failure while denying it --
# -- was silently dropped.


def test_an_unrelated_failure_is_not_suppressed_by_a_substring_match():
    """"Stall" is a literal substring of "Install"; the old containment
    check let a claim about installation swallow an unrelated stall
    failure. Gotchas only validate in a project's `gotchas` category (see
    profiles.py's category table), so this exercises project scope."""
    response = _reflect(
        "project",
        {
            "gotchas": [
                _profile_item(
                    claim="Install fails intermittently on cold start.",
                    kind="gotcha",
                    origin="observed",
                    failure="Stall",
                    cause="Cold start races the dependency cache.",
                    provenance="Observed twice in CI logs.",
                    evidence_ids=["mem-1"],
                )
            ]
        },
    )

    section = _structured_section([_profile_model(response)], scope="project")

    assert "failure: Stall" in section.text


def test_a_negated_mention_does_not_suppress_the_actual_failure():
    """A claim that denies a symptom in passing must not suppress the
    failure line reporting the real thing."""
    response = _reflect(
        "project",
        {
            "gotchas": [
                _profile_item(
                    claim="Never observed a stall during install.",
                    kind="gotcha",
                    origin="observed",
                    failure="stall during install",
                    cause="Network contention during dependency fetch.",
                    provenance="Observed once in CI logs.",
                    evidence_ids=["mem-1"],
                )
            ]
        },
    )

    section = _structured_section([_profile_model(response)], scope="project")

    assert "failure: stall during install" in section.text


def test_the_same_sentence_filed_in_both_fields_is_still_suppressed():
    """Equality, not containment, must still catch the one case the check
    exists for: a synthesizing model filing the identical sentence into both
    `claim` and `failure`."""
    response = _reflect(
        "project",
        {
            "gotchas": [
                _profile_item(
                    claim="Deploy fails after the migration step.",
                    kind="gotcha",
                    origin="observed",
                    failure="Deploy fails after the migration step.",
                    cause="The migration leaves the schema half applied.",
                    provenance="Observed in CI logs.",
                    evidence_ids=["mem-1"],
                )
            ]
        },
    )

    section = _structured_section([_profile_model(response)], scope="project")

    assert "failure:" not in section.text


# -- Finding 4: revision fingerprints hashed rendered text plus a render
# -- version, not canonical typed truth, so a `stated -> confirmed`
# -- correction or an evidence swap left a stale revision.


def test_a_correction_from_stated_to_confirmed_moves_the_content_fingerprint():
    """`origin` never reaches a rendered line for a plain convention (only a
    gotcha or explicit negative renders provenance, and none render origin
    at all), so the old text-keyed fingerprint could not distinguish a
    stated claim from the same claim freshly confirmed."""
    stated = _reflect("user", {"interaction": [_profile_item(origin="stated")]})
    confirmed = _reflect("user", {"interaction": [_profile_item(origin="confirmed")]})

    before = _structured_section([_profile_model(stated)])
    after = _structured_section([_profile_model(confirmed)])

    assert before.text == after.text
    assert before.content_fingerprint != after.content_fingerprint


def test_an_evidence_swap_moves_the_content_fingerprint_even_with_the_same_claim():
    """`evidence_ids` never reaches a rendered line either, so a claim that
    lost its only supporting memory and gained a different one was
    byte-identical to the old text-keyed fingerprint."""
    original = _reflect("user", {"interaction": [_profile_item(evidence_ids=["mem-a"])]})
    swapped = _reflect("user", {"interaction": [_profile_item(evidence_ids=["mem-b"])]})

    before = _structured_section([_profile_model(original)])
    after = _structured_section([_profile_model(swapped)])

    assert before.text == after.text
    assert before.content_fingerprint != after.content_fingerprint
