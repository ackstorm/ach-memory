import pytest

from experiments.memory_quality.contracts import SemanticCase
from experiments.memory_quality.semantic import (
    _AchHindsightAdapter,
    _canonical,
    run_semantic_case,
    split_minimal,
)
from memory.capture.extractor import build_extraction_prompt


def test_minimal_projection_has_only_scope_and_provenance():
    value = split_minimal("user: concise replies\nproject: use JSONL")
    assert value["claims"][0] == {"text": "concise replies", "bank": "user", "provenance": "raw-span"}
    assert set(value) == {"claims", "working_state"}


def test_semantic_variant_mutation_without_banks_fails_closed():
    case = SemanticCase(id="S99", transcript=({"role": "user", "text": "safe"},), expected_units=(), secret_canaries=("SECRET",))
    with pytest.raises(ValueError, match="fresh disposable"):
        run_semantic_case(case, "native_semantic", 1)


def test_ach_adapter_uses_hindsight_single_document_envelope():
    class Delegate:
        def dry_run_extract(self, bank_id, content, **options):
            assert options["retain_extraction_mode"] == "custom"
            assert "one outer object" in options["retain_mission"]
            assert "facts" in options["retain_mission"]
            assert "permission to execute, test or explore" in options["retain_mission"].casefold()
            assert "repository-local wording" in options["retain_mission"].casefold()
            return {"facts": []}

    _AchHindsightAdapter(Delegate()).dry_run_extract(
        "mq55-test-bank", "user: safe", retain_extraction_mode="custom", retain_mission="ignored"
    )


@pytest.mark.parametrize(
    "rule",
    (
        "Plans, proposals, research output and next steps are Working State",
        "Permission to execute, test or explore a proposal is authorization",
        "Repository-local wording",
        "Preserve a negative constraint",
        "provenance names the exact transcript span",
        "At most one working_state object total",
    ),
)
def test_both_extraction_transports_share_every_semantic_rule(rule):
    assert rule.casefold() in build_extraction_prompt("jsonl").casefold()
    assert rule.casefold() in build_extraction_prompt("hindsight_object").casefold()


def test_transport_envelopes_do_not_leak_into_each_other():
    jsonl = build_extraction_prompt("jsonl")
    hindsight = build_extraction_prompt("hindsight_object")
    assert "one minified JSON object per line" in jsonl
    assert 'one outer object shaped like {"facts"' not in jsonl
    assert 'one outer object shaped like {"facts"' in hindsight
    assert '\\"record\\":\\"candidate\\"' in hindsight
    assert "replace the example claim" in hindsight.casefold()
    assert "one minified JSON object per line" not in hindsight


def test_semantic_canonical_input_redacts_declared_fixture_canaries():
    case = SemanticCase(
        id="S13",
        transcript=({"role": "user", "text": "Bearer TOKEN and private-key header"},),
        expected_units=(),
        secret_canaries=("Bearer TOKEN", "private-key header"),
    )
    content = _canonical(case)
    assert "Bearer TOKEN" not in content
    assert "private-key header" not in content
    assert "[redacted]" in content


def test_ach_model_contract_failure_is_measured_instead_of_aborting_matrix(tmp_path):
    case = SemanticCase(
        id="S02",
        transcript=(
            {
                "role": "user",
                "text": "The accepted project decision is to use the staged rollout.",
            },
        ),
        expected_units=(),
        secret_canaries=("CANARY-S02",),
    )

    class Banks:
        def create_bank(self, purpose, repetition):
            return "mq55-test"

        def dry_run_extract(self, bank_id, content, **options):
            return {
                "facts": [
                    {
                        "text": '{"record":"candidate","text":"staged rollout",'
                        '"kind":"decision","origin":"stated","subject":"project",'
                        '"provenance":{"type":"transcript","start":47,"end":104}}'
                    }
                ]
            }

    artifact = tmp_path / "S02.json"
    observation = run_semantic_case(
        case, "ach_semantic", 1, Banks(), artifact
    )

    assert observation.hard_gate_flags["executed"] is True
    assert observation.hard_gate_flags["valid_output"] is False
    assert observation.metric_values["claim_count"] == 0
    assert artifact.read_text() == '{"error_code":"EXTRACTION_FAILED"}\n'
