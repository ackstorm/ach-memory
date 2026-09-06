import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path("evaluations/v040")


def _module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_retain_skill", "scripts/evaluate-retain-skill.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _perfect_rows(module):
    data = module.cases()
    policy = json.loads((ROOT / "policy.json").read_text())
    rows = []
    for family in policy["families"]:
        for repetition in range(policy["repetitions"]):
            for item in data:
                action = item["expected_action"]
                rows.append(
                    {
                        "family": family,
                        "repetition": repetition,
                        "case_id": item["case_id"],
                        "action": action,
                        "scope": item["expected_scope"],
                        "memory_type": item["expected_type"],
                        "basis": item["expected_basis"],
                        "trigger": item["expected_trigger"],
                        "claim_count": item["expected_claim_count"],
                        "has_expiry": item["expected_expiry"],
                        "evidence_shape": (
                            "none" if action in {"abstain", "working_state"} else "minimal_raw"
                        ),
                    }
                )
    return rows, data, policy


def test_coverage_names_every_scenario_and_each_required_polarity():
    module = _module()
    module.validate_inputs()

    coverage = json.loads((ROOT / "coverage.json").read_text())
    assert set(coverage["behaviors"]) == module._REQUIRED_BEHAVIORS
    assert all(
        matrix["positive"] and matrix["negative"]
        for matrix in coverage["behaviors"].values()
    )


def test_wrong_scope_mutation_fails_the_hard_gate():
    module = _module()
    rows, data, policy = _perfect_rows(module)
    target = next(row for row in rows if row["case_id"] == "case-01")
    target["scope"] = "user"

    score = module.score_rows(rows, data, policy)

    assert score["passed"] is False
    assert score["wrong_scope"] == 1
    assert "WRONG_SCOPE" in score["reason_codes"]


def test_duplicate_or_missing_decision_is_rejected():
    module = _module()
    rows, data, policy = _perfect_rows(module)
    rows[-1] = dict(rows[0])

    with pytest.raises(ValueError, match="each family/repetition/case tuple once"):
        module.score_rows(rows, data, policy)


def test_perfect_closed_decisions_pass_the_frozen_policy():
    module = _module()
    rows, data, policy = _perfect_rows(module)

    score = module.score_rows(rows, data, policy)

    perfect_metrics = {
        "aggregate_recall": 1.0,
        "critical_claim_recall": 1.0,
        "retention_precision": 1.0,
        "abstention_accuracy": 1.0,
        "memory_type_accuracy": 1.0,
    }
    assert score == {
        "passed": True,
        "raw_rows": 120,
        "majority_outcomes": 40,
        "wrong_scope": 0,
        "secret_retention": 0,
        **perfect_metrics,
        "per_family": {family: dict(perfect_metrics) for family in policy["families"]},
        "failing_case_ids": [],
        "reason_codes": [],
    }


def _one_outlier_rows(module, *, corrupt: str):
    """20 cases x 2 families x 3 repetitions, all correct, except repetition
    0 of family[0]/case-01 -- outnumbered 2-to-1 by the other two reps."""
    rows, data, policy = _perfect_rows(module)
    family = policy["families"][0]
    outlier = next(
        row
        for row in rows
        if row["family"] == family and row["repetition"] == 0 and row["case_id"] == "case-01"
    )
    if corrupt == "action":
        # A different, still-valid closed action -- outvoted 2-to-1 by the
        # other two repetitions' correct decision.
        outlier["action"] = "abstain" if outlier["action"] != "abstain" else "working_state"
        outlier["evidence_shape"] = "none"
    elif corrupt == "scope":
        expected_scope = next(item for item in data if item["case_id"] == "case-01")["expected_scope"]
        outlier["scope"] = "project" if expected_scope == "user" else "user"
    else:  # pragma: no cover -- defensive, test-only helper
        raise ValueError(corrupt)
    return rows, data, policy


def test_ordinary_metrics_use_per_family_case_majority():
    """One outvoted repetition must not move any ordinary metric: the
    (family, case_id) group's majority decision is what the other two
    repetitions agree on."""
    module = _module()
    rows, data, policy = _one_outlier_rows(module, corrupt="action")

    score = module.score_rows(rows, data, policy)

    assert score["aggregate_recall"] == 1.0
    assert score["raw_rows"] == 120
    assert score["majority_outcomes"] == 40
    assert score["failing_case_ids"] == []


def test_single_raw_wrong_scope_is_never_hidden_by_majority():
    """wrong_scope/secret_retention are raw-run scans across all 120 rows,
    never majority-adjusted -- a single bad repetition must still fail the
    hard gate even though the other two repetitions in its group agree on
    the correct scope."""
    module = _module()
    rows, data, policy = _one_outlier_rows(module, corrupt="scope")

    score = module.score_rows(rows, data, policy)

    assert score["wrong_scope"] == 1
    assert "WRONG_SCOPE" in score["reason_codes"]
    assert score["passed"] is False
    # The majority (2 of 3) decision for this group is still correct, so it
    # must not appear as a failing majority outcome.
    assert "case-01" not in score["failing_case_ids"]
