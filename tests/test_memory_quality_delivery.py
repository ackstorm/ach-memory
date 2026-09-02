import sys

from experiments.memory_quality.contracts import DeliveryCase
from experiments.memory_quality.delivery import build_delivery, run_consumer


def case():
    return DeliveryCase(id="D01", task="task", user_profile={}, project_profile={}, project_metadata=("metadata",), working_state=None, history_evidence=(), expected_units=(), secret_canaries=("SECRET",))


def test_delivery_artifact_is_closed_and_redacted():
    artifact = build_delivery(case(), "ach_index")
    assert artifact.case_id == "D01"
    assert "SECRET" not in artifact.context


def test_consumer_boundary_accepts_closed_json(tmp_path):
    script = tmp_path / "consumer.py"
    script.write_text("import json,sys; print(json.dumps({'answer':'ok','used_source_ids':[],'abstained':False}))")
    answer = run_consumer(build_delivery(case(), "ach_full"), (sys.executable, str(script)))
    assert answer.answer == "ok"


def test_live_delivery_variants_refuse_simulated_fallback():
    import pytest
    with pytest.raises(ValueError, match="live delivery"):
        build_delivery(case(), "official_reflect")


def test_live_delivery_repetition_is_part_of_bank_identity():
    class Banks:
        def __init__(self):
            self.calls = []

        def create_bank(self, purpose, ordinal):
            self.calls.append((purpose, ordinal))
            return "mq55-test"

    banks = Banks()
    try:
        build_delivery(case(), "official_reflect", banks, repetition=3)
    except AttributeError:
        pass
    assert banks.calls == [("delivery-D01-official_reflect", 3)]
