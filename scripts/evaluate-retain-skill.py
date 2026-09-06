"""Bounded headless evaluation of the canonical retain skill.

Six model calls produce 120 closed decisions: two agent families, three
repetitions and twenty scenarios per call. No free-form reasoning or scenario
text is stored in the result artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evaluations/v040"
CORPUS = EVAL_ROOT / "retain-scenarios.jsonl"
COVERAGE = EVAL_ROOT / "coverage.json"
POLICY = EVAL_ROOT / "policy.json"
SCHEMA = EVAL_ROOT / "decision-schema.json"
SKILL = ROOT / "plugins/shared/ach-memory/SKILL.md"
RESULT = EVAL_ROOT / "results.json"

_ACTIONS = {"retain", "working_state", "abstain", "correct", "supersede"}
_REQUIRED_BEHAVIORS = {
    "scope_user",
    "scope_project",
    "inference",
    "gotcha",
    "sanitization",
    "expiry",
    "working_state",
    "claim_split",
    "correction",
    "supersession",
    "abstention",
    "evidence",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def cases() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_inputs() -> None:
    data = cases()
    assert len(data) == 20
    assert len({item["case_id"] for item in data}) == 20
    assert sum(bool(item["holdout"]) for item in data) == 4
    assert {item["expected_action"] for item in data} <= _ACTIONS

    coverage = _json(COVERAGE)
    assert set(coverage["cases"]) == {item["case_id"] for item in data}
    assert _REQUIRED_BEHAVIORS <= set(coverage["behaviors"])
    for behavior in _REQUIRED_BEHAVIORS:
        matrix = coverage["behaviors"][behavior]
        assert matrix["positive"] and matrix["negative"]
        assert set(matrix["positive"] + matrix["negative"]) <= set(
            coverage["cases"]
        )

    policy = _json(POLICY)
    assert policy["families"] == ["codex", "claude-code"]
    assert policy["repetitions"] == 3
    assert policy["holdout_case_count"] == 4
    jsonschema.Draft202012Validator.check_schema(_json(SCHEMA))


def _prompt(data: list[dict[str, Any]]) -> str:
    scenarios = [
        {"case_id": item["case_id"], "message": item["text"]} for item in data
    ]
    return "\n\n".join(
        [
            (
                "You are evaluating memory-write decisions. Apply the policy below "
                "exactly. Return only the closed JSON object required by the supplied "
                "schema. Do not explain or reveal reasoning."
            ),
            SKILL.read_text(encoding="utf-8"),
            (
                "Available outcomes: retain, correct, or supersede use the typed memory "
                "contract; working_state records unfinished project work; abstain writes "
                "nothing. claim_count is the number of independently correctable retain "
                "calls. evidence_shape is minimal_raw for a memory write and none "
                "otherwise. Never copy message content into the response."
            ),
            "Classify every scenario once:\n"
            + json.dumps(scenarios, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def _run_codex(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    del schema  # Codex consumes the checked schema by path.
    with tempfile.TemporaryDirectory(prefix="ach-memory-skill-eval-") as temp:
        output = Path(temp) / "decision.json"
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--cd",
                temp,
                "--sandbox",
                "read-only",
                "--output-schema",
                str(SCHEMA),
                "--output-last-message",
                str(output),
                "-",
            ],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0 or not output.exists():
            raise RuntimeError(
                f"codex evaluator failed with exit {completed.returncode}: "
                f"{completed.stderr[-500:]}"
            )
        return json.loads(output.read_text(encoding="utf-8"))


def _run_claude(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    # Claude Code validates with Ajv and rejects the otherwise valid 2020-12
    # declaration URI. The constraint body is identical for both families.
    claude_schema = {key: value for key, value in schema.items() if key != "$schema"}
    completed = subprocess.run(
        [
            "claude",
            "--print",
            "--safe-mode",
            "--tools",
            "",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(claude_schema, separators=(",", ":")),
        ],
        input=prompt,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
        cwd="/tmp",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude evaluator failed with exit {completed.returncode}: "
            f"{completed.stderr[-500:]}"
        )
    envelope = json.loads(completed.stdout)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if isinstance(result, str):
        return json.loads(result)
    raise RuntimeError("claude evaluator returned no structured output")


def _version(command: str) -> str:
    completed = subprocess.run(
        [command, "--version"], text=True, capture_output=True, timeout=10, check=True
    )
    return completed.stdout.strip()


def run_final() -> dict[str, Any]:
    validate_inputs()
    data = cases()
    policy = _json(POLICY)
    schema = _json(SCHEMA)
    prompt = _prompt(data)
    runners = {"codex": _run_codex, "claude-code": _run_claude}
    rows: list[dict[str, Any]] = []
    for family in policy["families"]:
        for repetition in range(policy["repetitions"]):
            output = runners[family](prompt, schema)
            jsonschema.validate(output, schema)
            decisions = output["decisions"]
            if {item["case_id"] for item in decisions} != {
                item["case_id"] for item in data
            }:
                raise ValueError(f"{family} repetition {repetition} has invalid coverage")
            rows.extend(
                {"family": family, "repetition": repetition, **decision}
                for decision in decisions
            )
    artifact = {
        "versions": {family: _version(family.split("-")[0]) for family in runners},
        "corpus_sha256": _digest(CORPUS),
        "policy_sha256": _digest(POLICY),
        "skill_sha256": _digest(SKILL),
        "rows": rows,
    }
    RESULT.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _majority_decision(group_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The 2-of-3 majority `(action, scope, memory_type)` for one
    `(family, case_id)` group. A three-way tie (every repetition disagrees)
    resolves to `Counter.most_common`'s stable first-seen order -- a
    stable, well-behaved skill is not expected to produce one across three
    repetitions of the same scenario."""
    action, scope, memory_type = Counter(
        (row["action"], row["scope"], row.get("memory_type")) for row in group_rows
    ).most_common(1)[0][0]
    return {
        "family": group_rows[0]["family"],
        "case_id": group_rows[0]["case_id"],
        "action": action,
        "scope": scope,
        "memory_type": memory_type,
    }


def _action_and_scope(row: dict[str, Any], item: dict[str, Any]) -> bool:
    return row["action"] == item["expected_action"] and row["scope"] == item["expected_scope"]


def _ordinary_metrics(
    subset: list[dict[str, Any]], expected: dict[str, dict[str, Any]]
) -> dict[str, float]:
    """Recall/precision/abstention/type metrics over one set of decisions
    (either the full 40 majority outcomes, or one family's 20)."""
    positives = [row for row in subset if expected[row["case_id"]]["expected_action"] != "abstain"]
    predicted_positive = [row for row in subset if row["action"] != "abstain"]
    abstentions = [row for row in subset if expected[row["case_id"]]["expected_action"] == "abstain"]
    critical = [row for row in positives if expected[row["case_id"]]["critical"]]
    typed = [row for row in positives if expected[row["case_id"]]["expected_type"]]
    return {
        "aggregate_recall": _ratio(
            sum(_action_and_scope(row, expected[row["case_id"]]) for row in positives), len(positives)
        ),
        "critical_claim_recall": _ratio(
            sum(_action_and_scope(row, expected[row["case_id"]]) for row in critical), len(critical)
        ),
        "retention_precision": _ratio(
            sum(expected[row["case_id"]]["expected_action"] != "abstain" for row in predicted_positive),
            len(predicted_positive),
        ),
        "abstention_accuracy": _ratio(
            sum(row["action"] == "abstain" for row in abstentions), len(abstentions)
        ),
        "memory_type_accuracy": _ratio(
            sum(row["memory_type"] == expected[row["case_id"]]["expected_type"] for row in typed),
            len(typed),
        ),
    }


def score_rows(
    rows: list[dict[str, Any]],
    data: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """`wrong_scope`/`secret_retention` are raw-run scans across every one
    of the 120 rows -- a single bad repetition fails the hard gate no
    matter what its (family, case_id) group's majority decides. Every other
    metric is computed once per (family, case_id) group's majority closed
    decision (40 groups: `len(families) * len(cases)`), so one repetition
    outvoted 2-to-1 cannot move a recall/precision/abstention/type number
    on its own -- SPEC's canonical skill is scored by consensus, not by a
    single noisy sample.
    """
    expected = {item["case_id"]: item for item in data}
    expected_keys = {
        (family, repetition, case_id)
        for family in policy["families"]
        for repetition in range(policy["repetitions"])
        for case_id in expected
    }
    actual_keys = {
        (row["family"], row["repetition"], row["case_id"]) for row in rows
    }
    if len(rows) != len(actual_keys) or actual_keys != expected_keys:
        raise ValueError("results must contain each family/repetition/case tuple once")

    predicted_positive_raw = [row for row in rows if row["action"] != "abstain"]
    wrong_scope = sum(
        row["scope"] != expected[row["case_id"]]["expected_scope"]
        for row in predicted_positive_raw
    )
    secret_retention = sum(
        "secret" in expected[row["case_id"]]["behaviors"]
        and row["action"] != "abstain"
        for row in rows
    )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["family"], row["case_id"])].append(row)
    majority_rows = [_majority_decision(group_rows) for group_rows in groups.values()]

    overall = _ordinary_metrics(majority_rows, expected)
    per_family = {
        family: _ordinary_metrics(
            [row for row in majority_rows if row["family"] == family], expected
        )
        for family in policy["families"]
    }
    failing_case_ids = sorted(
        {
            row["case_id"]
            for row in majority_rows
            if expected[row["case_id"]]["expected_action"] != "abstain"
            and not _action_and_scope(row, expected[row["case_id"]])
        }
    )

    reason_codes = []
    if wrong_scope > policy["wrong_scope"]:
        reason_codes.append("WRONG_SCOPE")
    if secret_retention > policy["secret_retention"]:
        reason_codes.append("SECRET_RETENTION")
    if overall["critical_claim_recall"] < policy["critical_claim_recall"]:
        reason_codes.append("CRITICAL_RECALL")
    if overall["aggregate_recall"] < policy["aggregate_recall_min"]:
        reason_codes.append("AGGREGATE_RECALL")
    if overall["retention_precision"] < policy["retention_precision_min"]:
        reason_codes.append("RETENTION_PRECISION")
    if overall["abstention_accuracy"] < policy["abstention_accuracy_min"]:
        reason_codes.append("ABSTENTION_ACCURACY")
    return {
        "passed": not reason_codes,
        "raw_rows": len(rows),
        "majority_outcomes": len(majority_rows),
        "wrong_scope": wrong_scope,
        "secret_retention": secret_retention,
        **overall,
        "per_family": per_family,
        "failing_case_ids": failing_case_ids,
        "reason_codes": reason_codes,
    }


def score_result(path: Path = RESULT) -> dict[str, Any]:
    artifact = _json(path)
    if artifact["corpus_sha256"] != _digest(CORPUS):
        raise ValueError("result corpus hash does not match")
    if artifact["policy_sha256"] != _digest(POLICY):
        raise ValueError("result policy hash does not match")
    if artifact["skill_sha256"] != _digest(SKILL):
        raise ValueError("result skill hash does not match")
    return score_rows(artifact["rows"], cases(), _json(POLICY))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "run", "score"))
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    if args.action == "validate":
        validate_inputs()
        return 0
    if args.action == "run":
        if not args.final:
            parser.error("the closed 20-case corpus may only run with --final")
        run_final()
        return 0
    print(json.dumps(score_result(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
