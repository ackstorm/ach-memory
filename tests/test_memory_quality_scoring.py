import pytest

from experiments.memory_quality.contracts import RunObservation
from experiments.memory_quality.scoring import (
    Adjudication,
    AdjudicationItem,
    build_blind_packet,
    decide,
    score_run,
)


def test_blind_packet_contains_only_adjudicable_semantic_artifacts():
    semantic = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="blind/outputs/o-1.json", hard_gate_flags={}, metric_values={})
    delivery = RunObservation(case_id="D01", variant="official_pages", repetition=1, artifact_relpath="delivery/D01/official_pages-1.json", hard_gate_flags={}, metric_values={})
    packet = build_blind_packet((semantic, delivery), b"a" * 32)
    assert len(packet.items) == 1
    assert "ach_semantic" not in packet.items[0].artifact_relpath


def test_blind_packet_rejects_variant_named_artifacts():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="semantic/S01/ach_semantic-1.json", hard_gate_flags={}, metric_values={})
    with pytest.raises(ValueError, match="opaque"):
        build_blind_packet((observation,), b"a" * 32)


def test_blind_labels_change_with_key_but_items_remain_sorted():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="a.json", hard_gate_flags={}, metric_values={})
    assert build_blind_packet((observation,), b"a" * 32).items[0].blind_variant != build_blind_packet((observation,), b"b" * 32).items[0].blind_variant


def test_decisions_always_cover_all_components():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="a.json", hard_gate_flags={}, metric_values={})
    packet = build_blind_packet((observation,), b"a" * 32)
    item = packet.items[0]
    adjudication = Adjudication(run_id="r", items=(AdjudicationItem(case_id=item.case_id, blind_variant=item.blind_variant, repetition=1, required_units_met=(), unsupported_current_claims=0, wrong_scope_claims=0, notes_code="NONE"),))
    assert len(decide(score_run(packet, adjudication))) == 8


def test_mutated_gate_is_ineligible():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="a.json", hard_gate_flags={"executed": False}, metric_values={})
    packet = build_blind_packet((observation,), b"a" * 32)
    assert packet.items
