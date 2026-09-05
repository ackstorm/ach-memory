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

    assert score == {
        "passed": True,
        "rows": 120,
        "wrong_scope": 0,
        "secret_retention": 0,
        "critical_claim_recall": 1.0,
        "aggregate_recall": 1.0,
        "retention_precision": 1.0,
        "abstention_accuracy": 1.0,
        "memory_type_accuracy": 1.0,
        "reason_codes": [],
    }
