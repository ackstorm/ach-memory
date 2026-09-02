from pathlib import Path

import pytest
from pydantic import ValidationError

from experiments.memory_quality.contracts import (
    SEMANTIC_V2_CORPUS_VERSION,
    load_delivery_cases,
    load_semantic_cases,
)

ROOT = Path(__file__).parents[1]
SEMANTIC = ROOT / "experiments/memory_quality/corpus/semantic.jsonl"
SEMANTIC_V2 = ROOT / "experiments/memory_quality/corpus/semantic-v2.jsonl"
DELIVERY = ROOT / "experiments/memory_quality/corpus/delivery.jsonl"


def test_the_committed_corpus_is_closed_and_complete():
    semantic = load_semantic_cases(SEMANTIC)
    delivery = load_delivery_cases(DELIVERY)
    assert len(semantic) == 16
    assert len(delivery) == 10
    assert all(case.repetitions == 3 for case in (*semantic, *delivery))
    assert all(case.secret_canaries for case in (*semantic, *delivery))


def test_unknown_case_fields_fail_closed(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('{"id":"S99","unknown":true}\n')
    with pytest.raises(ValidationError):
        load_semantic_cases(path)


def test_semantic_v2_replaces_meta_working_state_with_concrete_state():
    cases = load_semantic_cases(SEMANTIC_V2)

    assert SEMANTIC_V2_CORPUS_VERSION == "semantic-v2"
    assert tuple(case.id for case in cases) == tuple(f"S{index:02d}" for index in range(1, 17))
    s09 = next(case for case in cases if case.id == "S09")
    transcript = "\n".join(turn["text"] for turn in s09.transcript)
    assert "Transient progress is not durable memory" not in transcript
    assert "Current objective:" in transcript
    assert "Next step:" in transcript
    assert len(s09.expected_units) == 1
    unit = s09.expected_units[0]
    assert unit.scope == "working_state"
    assert unit.critical is True
    assert unit.current is True
    assert unit.required_literals == (
        "finish transcript replay",
        "run the delivery gate",
    )
