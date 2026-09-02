import pytest

from experiments.memory_quality.contracts import SemanticCase
from experiments.memory_quality.semantic import run_semantic_case, split_minimal


def test_minimal_projection_has_only_scope_and_provenance():
    value = split_minimal("user: concise replies\nproject: use JSONL")
    assert value["claims"][0] == {"text": "concise replies", "bank": "user", "provenance": "raw-span"}
    assert set(value) == {"claims", "working_state"}


def test_semantic_variant_mutation_without_banks_fails_closed():
    case = SemanticCase(id="S99", transcript=({"role": "user", "text": "safe"},), expected_units=(), secret_canaries=("SECRET",))
    with pytest.raises(ValueError, match="fresh disposable"):
        run_semantic_case(case, "native_semantic", 1)
