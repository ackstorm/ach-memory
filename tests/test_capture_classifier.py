import pytest
from pydantic import ValidationError

from memory.capture.classifier import CandidateRejected, classify
from memory.capture.contracts import NormalizedCandidate, WorkingStateEnvelope

ARTIFACT = {"type": "transcript", "start": 10, "end": 20}


def _candidate(**overrides) -> dict:
    fields = {
        "record": "candidate",
        "text": "The project uses black for formatting.",
        "kind": "convention",
        "origin": "stated",
        "subject": "project",
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# The full kind x origin matrix (harness rule #7)
# ---------------------------------------------------------------------------

_ALL_KINDS = ["preference", "decision", "convention", "gotcha", "technical_claim"]
_ALL_ORIGINS = ["stated", "confirmed", "observed", "inferred"]

# SPEC §6.4, transcribed cell by cell. Written out in full rather than
# derived from a rule, because a derived expectation is what let the
# observed-preference and observed-decision cells stay wrong: the rule
# "neither a technical claim nor inferred, therefore eligible" agrees with
# the spec in eighteen cells out of twenty, and a test that encodes the rule
# cannot tell you which two it disagrees on.
_PROFILE = "profile_eligible"
_EVIDENCE = "evidence_only"
_SPEC_6_4 = {
    ("preference", "stated"): _PROFILE,
    ("preference", "confirmed"): _PROFILE,
    ("preference", "observed"): _EVIDENCE,
    ("preference", "inferred"): _EVIDENCE,
    ("decision", "stated"): _PROFILE,
    ("decision", "confirmed"): _PROFILE,
    ("decision", "observed"): _EVIDENCE,
    ("decision", "inferred"): _EVIDENCE,
    ("convention", "stated"): _PROFILE,
    ("convention", "confirmed"): _PROFILE,
    ("convention", "observed"): _PROFILE,
    ("convention", "inferred"): _EVIDENCE,
    ("gotcha", "stated"): _PROFILE,
    ("gotcha", "confirmed"): _PROFILE,
    ("gotcha", "observed"): _PROFILE,
    ("gotcha", "inferred"): _EVIDENCE,
    ("technical_claim", "stated"): _EVIDENCE,
    ("technical_claim", "confirmed"): _EVIDENCE,
    ("technical_claim", "observed"): _EVIDENCE,
    ("technical_claim", "inferred"): _EVIDENCE,
}


@pytest.mark.parametrize("kind", _ALL_KINDS)
@pytest.mark.parametrize("origin", _ALL_ORIGINS)
def test_the_kind_by_origin_eligibility_matrix(kind, origin):
    # gotcha needs evidence to stay a gotcha; supply it so this table
    # exercises the eligibility matrix, not the gotcha-downgrade rule.
    extra = {"failure": "it crashed", "cause": "null pointer"} if kind == "gotcha" else {}
    # A preference cannot route to the project bank at all (a separate
    # rule); use the user subject here so every cell is reachable.
    envelope = _candidate(kind=kind, origin=origin, subject="user", **extra)
    if origin == "observed":
        envelope["provenance"] = ARTIFACT

    result = classify(envelope)

    assert isinstance(result, NormalizedCandidate)
    assert result.eligible == _SPEC_6_4[(kind, origin)]
    assert result.tags == [f"kind:{result.kind}", result.eligible]
    assert result.observation_scopes == [[result.eligible]]


def test_every_kind_by_origin_pair_is_covered_by_the_spec_table():
    """The table above is exhaustive: no pair falls through to a default."""
    assert set(_SPEC_6_4) == {(k, o) for k in _ALL_KINDS for o in _ALL_ORIGINS}


# ---------------------------------------------------------------------------
# Named semantic cases from the plan
# ---------------------------------------------------------------------------


def test_stated_user_preference_is_user_and_profile_eligible():
    result = classify(
        _candidate(text="I prefer tabs over spaces.", kind="preference", origin="stated", subject="user")
    )

    assert result.bank_kind == "user"
    assert result.eligible == "profile_eligible"


def test_observed_project_convention_with_artifact_is_project_and_profile_eligible():
    result = classify(
        _candidate(
            text="The project uses black for formatting.",
            kind="convention",
            origin="observed",
            subject="project",
            provenance=ARTIFACT,
        )
    )

    assert result.bank_kind == "project"
    assert result.eligible == "profile_eligible"
    assert result.origin == "observed"


def test_inferred_convention_is_project_and_evidence_only():
    result = classify(
        _candidate(
            text="The project structures modules by feature.",
            kind="convention",
            origin="inferred",
            subject="project",
        )
    )

    assert result.bank_kind == "project"
    assert result.eligible == "evidence_only"


@pytest.mark.parametrize("origin", _ALL_ORIGINS)
def test_any_technical_claim_is_evidence_only(origin):
    provenance = ARTIFACT if origin == "observed" else None
    result = classify(
        _candidate(
            text="The service listens on port 8080.",
            kind="technical_claim",
            origin=origin,
            subject="project",
            provenance=provenance,
        )
    )

    assert result.eligible == "evidence_only"


def test_a_plan_or_next_step_is_working_state_not_a_decision():
    result = classify(
        {
            "record": "working_state",
            "objective": "Ship the capture pipeline",
            "next_steps": ["Write the classifier", "Wire the worker"],
        }
    )

    assert isinstance(result, WorkingStateEnvelope)
    assert result.next_steps == ["Write the classifier", "Wire the worker"]


def test_permission_to_test_is_working_state_never_a_confirmed_decision():
    """Working State is routed before candidate precedence (harness rule
    #2): permission to execute is captured there, or not at all -- it is
    never emitted as a `decision` candidate with eligibility attached."""
    result = classify(
        {
            "record": "working_state",
            "objective": "Prepare Phase 3",
            "next_steps": ["Get explicit approval before running the test suite"],
        }
    )

    assert isinstance(result, WorkingStateEnvelope)


@pytest.mark.parametrize("subject", ["user", "project"])
def test_an_explicit_negative_constraint_keeps_its_negation_and_scope(subject):
    text = "Do not squash commits."
    result = classify(
        _candidate(
            text=text,
            kind="convention",
            origin="stated",
            subject=subject,
            negative=True,
        )
    )

    assert result.text == text
    assert result.negative is True
    assert result.bank_kind == subject


def test_an_observed_gotcha_without_failure_or_cause_becomes_a_technical_claim():
    result = classify(
        _candidate(
            text="Retain sometimes hangs.",
            kind="gotcha",
            origin="observed",
            subject="project",
            provenance=ARTIFACT,
        )
    )

    assert result.kind == "technical_claim"
    assert result.eligible == "evidence_only"


def test_a_gotcha_with_failure_and_cause_stays_a_gotcha_and_is_eligible():
    result = classify(
        _candidate(
            text="Retain hangs under load.",
            kind="gotcha",
            origin="stated",
            subject="project",
            failure="the retain call times out",
            cause="the batch size exceeds the upstream limit",
        )
    )

    assert result.kind == "gotcha"
    assert result.eligible == "profile_eligible"


def test_a_gotcha_with_failure_and_only_reproduction_stays_a_gotcha():
    result = classify(
        _candidate(
            text="Retain hangs under load.",
            kind="gotcha",
            origin="stated",
            subject="project",
            failure="the retain call times out",
            reproduction="submit a 10MB payload",
        )
    )

    assert result.kind == "gotcha"


def test_a_gotcha_with_only_failure_becomes_a_technical_claim():
    result = classify(
        _candidate(
            text="Retain hangs under load.",
            kind="gotcha",
            origin="stated",
            subject="project",
            failure="the retain call times out",
        )
    )

    assert result.kind == "technical_claim"


def test_a_personal_preference_routed_to_project_is_rejected():
    with pytest.raises(CandidateRejected):
        classify(
            _candidate(
                text="I like tabs over spaces.",
                kind="preference",
                origin="stated",
                subject="project",
            )
        )


def test_personal_language_in_a_non_preference_project_candidate_is_rejected():
    with pytest.raises(CandidateRejected):
        classify(
            _candidate(
                text="My team always squashes before merging.",
                kind="convention",
                origin="stated",
                subject="project",
            )
        )


def test_impersonal_project_wording_is_preserved():
    result = classify(
        _candidate(
            text="Pull requests are squashed before merging.",
            kind="convention",
            origin="stated",
            subject="project",
        )
    )

    assert result.bank_kind == "project"


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subject", ["user", "project"])
def test_a_correction_is_fenced_to_its_own_resolved_bank_and_eligibility(subject):
    text = "Pull requests are squashed before merging." if subject == "project" else "I prefer tabs."
    result = classify(
        _candidate(
            text=text,
            kind="convention" if subject == "project" else "preference",
            origin="stated",
            subject=subject,
            correction=True,
        )
    )

    assert result.correction is True
    assert result.correction_scope == (subject, result.eligible)


def test_a_non_correction_has_no_correction_scope():
    result = classify(_candidate(correction=False))

    assert result.correction_scope is None


# ---------------------------------------------------------------------------
# Rejection of model-supplied structural fields, and malformed envelopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["bank", "profile_eligible", "tags", "observation_scopes"])
def test_a_model_supplied_bank_or_eligibility_field_is_rejected(field):
    envelope = _candidate()
    envelope[field] = "anything"

    with pytest.raises(ValidationError):
        classify(envelope)


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        classify(_candidate(kind="opinion"))


def test_an_unknown_origin_is_rejected():
    with pytest.raises(ValidationError):
        classify(_candidate(origin="assumed"))


def test_oversized_text_is_rejected():
    with pytest.raises(ValidationError):
        classify(_candidate(text="x" * 513))


def test_multiline_text_is_rejected():
    with pytest.raises(ValidationError):
        classify(_candidate(text="line one\nline two"))


def test_an_unrecognized_record_type_fails_closed():
    with pytest.raises(CandidateRejected):
        classify({"record": "summary", "text": "whatever"})
