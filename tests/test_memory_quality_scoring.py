import hashlib
import hmac
import json

import pytest

from experiments.memory_quality import scoring as scoring_module
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
    score_semantic_repair,
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


def _v3_semantic_fixture(
    *,
    met_by_variant: dict[str, tuple[str, ...]],
    case_id: str = "S01",
    wrong_scope_by_variant: dict[str, int] | None = None,
    unsupported_by_variant: dict[str, int] | None = None,
    flags_by_variant: dict[str, dict[str, bool]] | None = None,
):
    labels = {
        "ach_semantic": "V-ach",
        "native_semantic": "V-native",
        "hybrid_semantic": "V-hybrid",
    }
    wrong_scope_by_variant = wrong_scope_by_variant or {}
    unsupported_by_variant = unsupported_by_variant or {}
    flags_by_variant = flags_by_variant or {}
    observations = []
    adjudication_items = []
    unblinded = []
    for variant, label in labels.items():
        for repetition in (1, 2, 3):
            artifact = f"blind/outputs/{case_id}-{variant}-{repetition}.json"
            required_units_met = met_by_variant[variant]
            observations.append(
                RunObservation(
                    case_id=case_id,
                    variant=variant,
                    repetition=repetition,
                    artifact_relpath=artifact,
                    hard_gate_flags=flags_by_variant.get(variant, {"executed": True}),
                    metric_values={},
                )
            )
            adjudication_items.append(
                AdjudicationItem(
                    case_id=case_id,
                    blind_variant=label,
                    repetition=repetition,
                    required_units_met=required_units_met,
                    unsupported_current_claims=unsupported_by_variant.get(variant, 0),
                    wrong_scope_claims=wrong_scope_by_variant.get(variant, 0),
                    notes_code="NONE",
                )
            )
            unblinded.append(
                UnblindedItem(
                    case_id=case_id,
                    blind_variant=label,
                    artifact_relpath=artifact,
                    variant=variant,
                    repetition=repetition,
                    required_units_met=required_units_met,
                )
            )
    return tuple(observations), tuple(unblinded), Adjudication(run_id="r", items=tuple(adjudication_items))


def test_v3_compares_named_miss_atoms_instead_of_case_boole():
    observations, unblinded, adjudication = _v3_semantic_fixture(
        met_by_variant={
            "ach_semantic": ("S01-U2",),
            "native_semantic": ("S01-U1",),
            "hybrid_semantic": ("S01-U1", "S01-U2"),
        }
    )

    score = scoring_module.score_run_v3(
        observations,
        unblinded,
        adjudication,
        expected_units={"S01": ("S01-U1", "S01-U2")},
        critical_units={},
        consumer_complete=False,
    )

    native = score.components["semantic_extractor"].challengers["native_semantic"]
    assert native.repeatable_regressions == ("S01:MISSING_S01-U2",)
    assert "S01:MISSING_S01-U1" not in native.quality_regressions


def test_v3_routes_scope_failures_only_to_scope_router():
    observations, unblinded, adjudication = _v3_semantic_fixture(
        met_by_variant={variant: ("S01-U1",) for variant in ("ach_semantic", "native_semantic", "hybrid_semantic")},
        wrong_scope_by_variant={"native_semantic": 1},
        flags_by_variant={
            "ach_semantic": {"executed": True, "no_shared_document": True},
            "native_semantic": {"executed": True, "no_shared_document": False},
            "hybrid_semantic": {"executed": True, "no_shared_document": True},
        },
    )

    score = scoring_module.score_run_v3(
        observations,
        unblinded,
        adjudication,
        expected_units={"S01": ("S01-U1",)},
        critical_units={"S01": ("S01-U1",)},
        consumer_complete=False,
    )

    extractor = score.components["semantic_extractor"].challengers["native_semantic"]
    router = score.components["scope_router"].challengers["native_semantic"]
    assert extractor.hard_gate_failures == ()
    assert extractor.quality_regressions == ()
    assert "native_semantic:S01:NO_SHARED_DOCUMENT" in router.hard_gate_failures
    assert "native_semantic:S01:WRONG_SCOPE" in router.hard_gate_failures


def test_v3_attributes_preprocessing_secret_gate_to_the_failed_variant():
    observations = tuple(
        RunObservation(
            case_id="claude",
            variant=variant,
            repetition=1,
            artifact_relpath=f"preprocessing/{variant}.json",
            hard_gate_flags={"executed": True, "no_canary": variant != "official_preprocess"},
            metric_values={},
        )
        for variant in ("ach_preprocess", "official_preprocess", "hybrid_preprocess")
    )

    score = scoring_module.score_run_v3(
        observations,
        (),
        Adjudication(run_id="r", items=()),
        expected_units={},
        critical_units={},
        consumer_complete=False,
    )

    preprocessing = score.components["preprocessing"]
    assert preprocessing.hard_gate_failures == ("official_preprocess:claude:NO_CANARY",)
    assert preprocessing.challengers["official_preprocess"].non_inferior is False
    assert preprocessing.challengers["hybrid_preprocess"].non_inferior is True


def test_v3_requires_comparative_host_adapter_evidence():
    score = scoring_module.score_run_v3(
        (),
        (),
        Adjudication(run_id="r", items=()),
        expected_units={},
        critical_units={},
        consumer_complete=False,
    )

    host_score = score.components["host_adapters"]
    host_decision = next(item for item in scoring_module.decide_v3(score) if item.component == "host_adapters")
    assert host_score.evidence_available is False
    assert host_decision.ruling == "insufficient_evidence"
    assert host_decision.reason_codes == ("NO_COMPARATIVE_HOST_ADAPTER_EVIDENCE",)


def test_v3_treats_a_missing_critical_unit_as_an_absolute_extractor_gate():
    observations, unblinded, adjudication = _v3_semantic_fixture(
        met_by_variant={
            "ach_semantic": ("S01-U1",),
            "native_semantic": (),
            "hybrid_semantic": ("S01-U1",),
        }
    )

    score = scoring_module.score_run_v3(
        observations,
        unblinded,
        adjudication,
        expected_units={"S01": ("S01-U1",)},
        critical_units={"S01": ("S01-U1",)},
        consumer_complete=False,
    )

    native = score.components["semantic_extractor"].challengers["native_semantic"]
    assert native.hard_gate_failures == ("native_semantic:S01:MISSING_S01-U1",)
    assert native.non_inferior is False


def test_v3_treats_unsupported_current_claim_in_a_critical_rejection_case_as_a_gate():
    observations, unblinded, adjudication = _v3_semantic_fixture(
        case_id="S16",
        met_by_variant={variant: ("S16-U1",) for variant in ("ach_semantic", "native_semantic", "hybrid_semantic")},
        unsupported_by_variant={"native_semantic": 1},
    )

    score = scoring_module.score_run_v3(
        observations,
        unblinded,
        adjudication,
        expected_units={"S16": ("S16-U1",)},
        critical_units={"S16": ("S16-U1",)},
        critical_rejection_cases=("S16",),
        consumer_complete=False,
    )

    native = score.components["semantic_extractor"].challengers["native_semantic"]
    assert native.hard_gate_failures == ("native_semantic:S16:UNSUPPORTED_CURRENT",)


def test_v3_does_not_require_an_ignore_scope_unit_to_be_emitted():
    observations, unblinded, adjudication = _v3_semantic_fixture(
        case_id="S16",
        met_by_variant={variant: () for variant in ("ach_semantic", "native_semantic", "hybrid_semantic")},
    )

    score = scoring_module.score_run_v3(
        observations,
        unblinded,
        adjudication,
        expected_units={"S16": ("S16-U1",)},
        critical_units={"S16": ("S16-U1",)},
        ignored_units={"S16": ("S16-U1",)},
        critical_rejection_cases=("S16",),
        consumer_complete=False,
    )

    extractor = score.components["semantic_extractor"]
    assert "S16:MISSING_S16-U1" not in extractor.quality_misses
    assert not any("MISSING_S16-U1" in failure for failure in extractor.hard_gate_failures)


def test_v3_keeps_unmeasured_delivery_and_reliability_insufficient():
    score = scoring_module.score_run_v3(
        (),
        (),
        Adjudication(run_id="r", items=()),
        expected_units={},
        critical_units={},
        consumer_complete=False,
    )
    decisions = {item.component: item for item in scoring_module.decide_v3(score)}

    assert decisions["profile_compiler"].ruling == "insufficient_evidence"
    assert decisions["delivery_protocol"].ruling == "insufficient_evidence"
    assert decisions["capture_reliability"].ruling == "insufficient_evidence"
    assert decisions["working_state_ordering"].ruling == "insufficient_evidence"


def test_semantic_repair_score_contains_only_the_two_measured_components():
    observations, unblinded, adjudication = _v3_semantic_fixture(
        case_id="S16",
        met_by_variant={
            variant: ()
            for variant in ("ach_semantic", "native_semantic", "hybrid_semantic")
        },
        unsupported_by_variant={"native_semantic": 1},
        wrong_scope_by_variant={"hybrid_semantic": 1},
    )

    score = score_semantic_repair(
        observations,
        unblinded,
        adjudication,
        expected_units={"S16": ("S16-U1",)},
        critical_units={"S16": ("S16-U1",)},
        ignored_units={"S16": ("S16-U1",)},
        critical_rejection_cases=("S16",),
        run_id="repair-run",
    )

    assert set(score.components) == {"semantic_extractor", "scope_router"}
    assert not any(
        "MISSING_S16-U1" in failure
        for failure in score.components["semantic_extractor"].hard_gate_failures
    )
    assert score.components["semantic_extractor"].challengers[
        "native_semantic"
    ].hard_gate_failures == ("native_semantic:S16:UNSUPPORTED_CURRENT",)
    assert score.components["semantic_extractor"].challengers[
        "hybrid_semantic"
    ].hard_gate_failures == ()
    assert score.components["scope_router"].challengers[
        "hybrid_semantic"
    ].hard_gate_failures == ("hybrid_semantic:S16:WRONG_SCOPE",)
