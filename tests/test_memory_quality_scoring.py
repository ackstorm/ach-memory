import hashlib
import hmac
import json

import pytest

from experiments.memory_quality.contracts import RunObservation
from experiments.memory_quality.scoring import (
    Adjudication,
    AdjudicationItem,
    UnblindedItem,
    build_blind_packet,
    decide,
    decide_v2,
    score_run,
    score_run_v2,
    unblind,
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
    item = packet.items[0]
    adjudication = Adjudication(run_id="r", items=(AdjudicationItem(case_id=item.case_id, blind_variant=item.blind_variant, repetition=1, required_units_met=(), unsupported_current_claims=0, wrong_scope_claims=0, notes_code="NONE"),))
    score = score_run(packet, adjudication)
    assert score.variant_scores[0].eligible is False


def test_missing_required_unit_is_a_named_variant_miss():
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="a.json", hard_gate_flags={"executed": True}, metric_values={})
    packet = build_blind_packet((observation,), b"a" * 32)
    item = packet.items[0]
    adjudication = Adjudication(run_id="r", items=(AdjudicationItem(case_id=item.case_id, blind_variant=item.blind_variant, repetition=1, required_units_met=(), unsupported_current_claims=0, wrong_scope_claims=0, notes_code="NONE"),))
    score = score_run(packet, adjudication, expected_units={"S01": ("S01-U1",)})
    assert score.variant_scores[0].eligible is False


def test_unblind_requires_a_valid_sealed_mapping(tmp_path):
    key = b"a" * 32
    observation = RunObservation(case_id="S01", variant="ach_semantic", repetition=1, artifact_relpath="blind/outputs/o-1.json", hard_gate_flags={}, metric_values={})
    packet = build_blind_packet((observation,), key)
    item = packet.items[0]
    adjudication = Adjudication(run_id="r", items=(AdjudicationItem(case_id="S01", blind_variant=item.blind_variant, repetition=1, required_units_met=(), unsupported_current_claims=0, wrong_scope_claims=0, notes_code="NONE"),))
    mapping = {item.artifact_relpath: {"case_id": "S01", "variant": "ach_semantic", "repetition": 1}}
    encoded = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / ".blind-mapping.json"
    path.write_text(json.dumps({"mapping": mapping, "seal": hmac.new(key, encoded, hashlib.sha256).hexdigest()}))
    assert unblind(packet, adjudication, path, key)[0].variant == "ach_semantic"
    path.write_text(json.dumps({"mapping": mapping, "seal": "bad"}))
    with pytest.raises(ValueError, match="seal"):
        unblind(packet, adjudication, path, key)


def test_v2_compares_challengers_against_ach_by_named_case_and_repetition():
    observations = tuple(
        RunObservation(case_id=case, variant=variant, repetition=rep, artifact_relpath=f"blind/outputs/o-{len(variant)}-{case}-{rep}.json", hard_gate_flags={"executed": True}, metric_values={})
        for variant in ("ach_semantic", "native_semantic", "hybrid_semantic")
        for case in ("S01", "S02")
        for rep in (1, 2, 3)
    )
    key = b"a" * 32
    packet = build_blind_packet(observations, key)
    items = tuple(AdjudicationItem(case_id=item.case_id, blind_variant=item.blind_variant, repetition=item.repetition, required_units_met=("U",) if not (item.case_id == "S02" and item.repetition in (1, 2) and item.blind_variant == packet.items[-1].blind_variant) else (), unsupported_current_claims=0, wrong_scope_claims=0, notes_code="NONE") for item in packet.items)
    adjudication = Adjudication(run_id="r", items=items)
    unblinded = tuple(UnblindedItem(case_id=i.case_id, blind_variant=i.blind_variant, variant="ach_semantic" if i.blind_variant == packet.items[0].blind_variant else "native_semantic", repetition=i.repetition, required_units_met=i.required_units_met) for i in items)
    score = score_run_v2(observations, unblinded, adjudication, expected_units={"S01": ("U",), "S02": ("U",)}, consumer_complete=False)
    assert score.components["semantic_extractor"].challengers
    assert score.components["semantic_extractor"].challengers["native_semantic"].approval_required


def test_v2_decisions_are_component_specific_and_have_evidence():
    score = score_run_v2((), (), Adjudication(run_id="r", items=()), expected_units={}, consumer_complete=False)
    decisions = decide_v2(score)
    assert len(decisions) == 8
    assert all(item.reason_codes for item in decisions)
    assert next(item for item in decisions if item.component == "profile_compiler").ruling == "insufficient_evidence"
