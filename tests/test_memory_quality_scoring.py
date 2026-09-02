from experiments.memory_quality.contracts import RunObservation
from experiments.memory_quality.scoring import (
    Adjudication,
    AdjudicationItem,
    build_blind_packet,
    decide,
    score_run,
)


def test_blind_labels_change_with_key_but_items_remain_sorted():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="a.json", hard_gate_flags={}, metric_values={})
    assert build_blind_packet((observation,), b"a").items[0].blind_variant != build_blind_packet((observation,), b"b").items[0].blind_variant


def test_decisions_always_cover_all_components():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="a.json", hard_gate_flags={}, metric_values={})
    packet = build_blind_packet((observation,), b"a")
    item = packet.items[0]
    adjudication = Adjudication(run_id="r", items=(AdjudicationItem(case_id=item.case_id, blind_variant=item.blind_variant, repetition=1, required_units_met=(), unsupported_current_claims=0, wrong_scope_claims=0, notes_code="NONE"),))
    assert len(decide(score_run(packet, adjudication))) == 8
