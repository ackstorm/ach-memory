from pathlib import Path

import pytest
from pydantic import ValidationError

from experiments.memory_quality.contracts import load_delivery_cases, load_semantic_cases

ROOT = Path(__file__).parents[1]
SEMANTIC = ROOT / "experiments/memory_quality/corpus/semantic.jsonl"
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
