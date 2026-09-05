import json
from pathlib import Path

def test_coverage_names_every_scenario_and_policy_is_frozen():
    root = Path("evaluations/v040")
    cases = {json.loads(line)["case_id"] for line in (root / "retain-scenarios.jsonl").read_text().splitlines()}
    coverage = json.loads((root / "coverage.json").read_text())
    assert cases == set(coverage["cases"])
    assert json.loads((root / "policy.json").read_text())["wrong_scope"] == 0
