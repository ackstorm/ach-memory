"""Deterministic blind packet and component decision scoring."""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .contracts import ComponentDecision, GateResult, RunObservation


class BlindItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    blind_variant: str
    repetition: int
    artifact_relpath: str
    automatic_gates: dict[str, bool] = {}


class BlindPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_digest: str
    items: tuple[BlindItem, ...]


class AdjudicationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    blind_variant: str
    repetition: int
    required_units_met: tuple[str, ...]
    unsupported_current_claims: int
    wrong_scope_claims: int
    notes_code: Literal["NONE", "PARAPHRASE_ACCEPTED", "AMBIGUOUS_OUTPUT", "ADJUDICATION_BLOCKED"]


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    items: tuple[AdjudicationItem, ...]


class Scorecard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gates: tuple[GateResult, ...]
    named_misses: tuple[str, ...]
    median_latency_ms: float | None
    p95_latency_ms: float | None
    total_input_tokens: int
    total_output_tokens: int
    non_inferior: bool
    approval_required: bool
    consumer_complete: bool = False
    variant_scores: tuple[VariantScore, ...] = ()


class ChallengerScoreV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: str
    non_inferior: bool
    approval_required: bool
    hard_gate_failures: tuple[str, ...]
    quality_regressions: tuple[str, ...]
    repeatable_regressions: tuple[str, ...]
    evidence_case_ids: tuple[str, ...]
    repetitions_observed: int
    latency_ms: tuple[float, ...] = ()
    input_tokens: tuple[int, ...] = ()
    output_tokens: tuple[int, ...] = ()


class ComponentScoreV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: str
    observation_count: int
    evidence_case_ids: tuple[str, ...]
    hard_gate_failures: tuple[str, ...]
    quality_misses: tuple[str, ...]
    median_latency_ms: float | None
    p95_latency_ms: float | None
    total_input_tokens: int | None
    total_output_tokens: int | None
    evidence_available: bool
    challengers: dict[str, ChallengerScoreV2] = {}


class ScorecardV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    observation_count: int
    consumer_complete: bool
    components: dict[str, ComponentScoreV2]


class VariantScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blind_variant: str
    eligible: bool
    named_misses: tuple[str, ...]


class UnblindedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    blind_variant: str
    artifact_relpath: str = ""
    variant: str
    repetition: int
    required_units_met: tuple[str, ...]


ADJUDICABLE_VARIANTS = {"ach_semantic", "native_semantic", "hybrid_semantic"}


def build_blind_packet(observations: Sequence[RunObservation], key: bytes) -> BlindPacket:
    if not key or len(key) < 32 or key == b"development-only":
        raise ValueError("blind packet requires a random HMAC key of at least 32 bytes")
    selected = (o for o in observations if o.variant in ADJUDICABLE_VARIANTS)
    items = []
    for observation in selected:
        if observation.variant in observation.artifact_relpath:
            raise ValueError("blind packet requires opaque artifact paths")
        label = "V-" + hmac.new(key, observation.variant.encode(), hashlib.sha256).hexdigest()[:16]
        items.append(BlindItem(case_id=observation.case_id, blind_variant=label, repetition=observation.repetition, artifact_relpath=observation.artifact_relpath, automatic_gates=dict(observation.hard_gate_flags)))
    items = tuple(sorted(items, key=lambda x: (x.case_id, x.blind_variant, x.repetition, x.artifact_relpath)))
    digest = hashlib.sha256("\n".join(item.model_dump_json() for item in items).encode()).hexdigest()
    return BlindPacket(corpus_digest=digest, items=items)


def score_run(packet: BlindPacket, adjudication: Adjudication, *, expected_units: Mapping[str, Sequence[str]] | None = None, consumer_complete: bool = False) -> Scorecard:
    expected = {(item.case_id, item.blind_variant, item.repetition) for item in packet.items}
    actual = {(item.case_id, item.blind_variant, item.repetition) for item in adjudication.items}
    if expected != actual:
        raise ValueError("adjudication is incomplete or contains duplicates")
    failures = tuple(sorted(item.case_id for item in adjudication.items if item.notes_code == "ADJUDICATION_BLOCKED"))
    by_variant: dict[str, list[str]] = {}
    item_by_key = {(item.case_id, item.blind_variant, item.repetition): item for item in packet.items}
    for item in adjudication.items:
        misses = by_variant.setdefault(item.blind_variant, [])
        packet_item = item_by_key[(item.case_id, item.blind_variant, item.repetition)]
        for gate, passed in packet_item.automatic_gates.items():
            if not passed:
                misses.append(f"{item.case_id}:{gate.upper()}")
        if expected_units:
            missing = set(expected_units.get(item.case_id, ())) - set(item.required_units_met)
            misses.extend(f"{item.case_id}:MISSING_{unit}" for unit in sorted(missing))
        if item.wrong_scope_claims:
            misses.append(f"{item.case_id}:WRONG_SCOPE")
        if item.unsupported_current_claims:
            misses.append(f"{item.case_id}:UNSUPPORTED_CURRENT")
    variants = tuple(VariantScore(blind_variant=name, eligible=not misses and not failures, named_misses=tuple(sorted(misses))) for name, misses in sorted(by_variant.items()))
    return Scorecard(gates=(GateResult(gate="adjudication_complete", passed=not failures, failing_case_ids=failures),), named_misses=failures, median_latency_ms=None, p95_latency_ms=None, total_input_tokens=0, total_output_tokens=0, non_inferior=not failures and all(item.eligible for item in variants), approval_required=bool(failures or any(item.named_misses for item in variants)), consumer_complete=consumer_complete, variant_scores=variants)


def unblind(packet: BlindPacket, adjudication: Adjudication, mapping_path: Path, key: bytes) -> tuple[UnblindedItem, ...]:
    if not key or len(key) < 32 or key == b"development-only":
        raise ValueError("unblind requires the external HMAC key")
    payload = json.loads(mapping_path.read_text())
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        raise TypeError("blind mapping is malformed")
    encoded = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    expected_seal = hmac.new(key, encoded, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(payload.get("seal", "")), expected_seal):
        raise ValueError("blind mapping seal mismatch")
    by_label = {}
    for opaque, value in mapping.items():
        if not isinstance(value, dict) or set(value) != {"case_id", "variant", "repetition"}:
            raise ValueError("blind mapping entry is malformed")
        by_label[opaque] = value
    results = []
    for item in adjudication.items:
        packet_item = next((candidate for candidate in packet.items if (candidate.case_id, candidate.blind_variant, candidate.repetition) == (item.case_id, item.blind_variant, item.repetition)), None)
        if packet_item is None or packet_item.artifact_relpath not in by_label:
            raise ValueError("adjudication does not match sealed blind mapping")
        value = by_label[packet_item.artifact_relpath]
        results.append(UnblindedItem(case_id=value["case_id"], blind_variant=item.blind_variant, artifact_relpath=packet_item.artifact_relpath, variant=value["variant"], repetition=value["repetition"], required_units_met=item.required_units_met))
    return tuple(results)


COMPONENTS = ("host_adapters", "preprocessing", "semantic_extractor", "scope_router", "profile_compiler", "delivery_protocol", "capture_reliability", "working_state_ordering")


def decide(scorecard: Scorecard) -> tuple[ComponentDecision, ...]:
    decisions = []
    has_delivery_evidence = scorecard.consumer_complete
    for component in COMPONENTS:
        if component in {"profile_compiler", "delivery_protocol"} and not has_delivery_evidence:
            ruling = "insufficient_evidence"
            reason = "NO_CONSUMER_EVIDENCE"
        elif not scorecard.non_inferior:
            ruling = "keep"
            reason = "CHALLENGER_GATE_FAILURE"
        elif component == "scope_router" and any("WRONG_SCOPE" in miss for item in scorecard.variant_scores for miss in item.named_misses):
            ruling = "add_follow_up_guard"
            reason = "WRONG_SCOPE"
        else:
            ruling = "keep"
            reason = "BASELINE_ONLY"
        decisions.append(ComponentDecision(component=component, ruling=ruling, approval_required=scorecard.approval_required, evidence_case_ids=scorecard.gates[0].failing_case_ids, reason_codes=(reason,)))
    return tuple(decisions)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


def _challenger_score(
    variant: str,
    baseline_misses: Mapping[str, set[int]],
    misses: Mapping[str, set[int]],
    hard_gate_failures: Sequence[str],
    observations: Sequence[RunObservation],
) -> ChallengerScoreV2:
    additional = {case: repetitions - baseline_misses.get(case, set()) for case, repetitions in misses.items()}
    additional = {case: repetitions for case, repetitions in additional.items() if repetitions}
    quality = set(additional)
    repeatable = {case for case, repetitions in additional.items() if len(repetitions) >= 2}
    variant_observations = [item for item in observations if item.variant == variant]
    latencies = [float(item.metric_values["duration_ms"]) for item in variant_observations if item.metric_values.get("duration_ms") is not None]
    input_tokens = [int(item.metric_values["input_tokens"]) for item in variant_observations if item.metric_values.get("input_tokens") is not None]
    output_tokens = [int(item.metric_values["output_tokens"]) for item in variant_observations if item.metric_values.get("output_tokens") is not None]
    return ChallengerScoreV2(
        variant=variant,
        non_inferior=not repeatable and not hard_gate_failures,
        approval_required=bool(quality or hard_gate_failures),
        hard_gate_failures=tuple(sorted(hard_gate_failures)),
        quality_regressions=tuple(sorted(quality)),
        repeatable_regressions=tuple(sorted(repeatable)),
        evidence_case_ids=tuple(sorted(set(misses) | set(baseline_misses))),
        repetitions_observed=len(variant_observations),
        latency_ms=tuple(latencies), input_tokens=tuple(input_tokens), output_tokens=tuple(output_tokens),
    )


def score_run_v2(
    observations: Sequence[RunObservation],
    unblinded: Sequence[UnblindedItem],
    adjudication: Adjudication,
    *,
    expected_units: Mapping[str, Sequence[str]],
    consumer_complete: bool,
    run_id: str | None = None,
) -> ScorecardV2:
    """Rescore frozen evidence by component without changing the experiment."""
    adjudication_by_key = {(item.case_id, item.blind_variant, item.repetition): item for item in adjudication.items}
    semantic_obs = [item for item in observations if item.variant in ADJUDICABLE_VARIANTS]
    by_variant_case: dict[str, dict[str, set[int]]] = {}
    hard_by_variant: dict[str, list[str]] = {}
    for item in unblinded:
        adjudication_item = adjudication_by_key[(item.case_id, item.blind_variant, item.repetition)]
        missing = set(expected_units.get(item.case_id, ())) - set(adjudication_item.required_units_met)
        bad = bool(missing or adjudication_item.unsupported_current_claims or adjudication_item.wrong_scope_claims or adjudication_item.notes_code == "ADJUDICATION_BLOCKED")
        if bad:
            by_variant_case.setdefault(item.variant, {}).setdefault(item.case_id, set()).add(item.repetition)
    for item in semantic_obs:
        # no_shared_document is an explicit native-baseline characteristic,
        # not a failed gate. Only execution/sanitization gates are failures.
        failed_gates = {
            gate: passed
            for gate, passed in item.hard_gate_flags.items()
            if gate != "no_shared_document" and not passed
        }
        if failed_gates:
            mapped = next((candidate.variant for candidate in unblinded if candidate.artifact_relpath == item.artifact_relpath), item.variant)
            hard_by_variant.setdefault(mapped, []).extend(f"{item.case_id}:{gate}" for gate in failed_gates)
    baseline = by_variant_case.get("ach_semantic", {})
    challengers = {
        variant: _challenger_score(variant, baseline, by_variant_case.get(variant, {}), hard_by_variant.get(variant, ()), semantic_obs)
        for variant in ADJUDICABLE_VARIANTS - {"ach_semantic"}
    }
    components: dict[str, ComponentScoreV2] = {}
    preprocessing = [item for item in observations if item.variant.endswith("preprocess")]
    preprocessing_latencies = [float(item.metric_values["duration_ms"]) for item in preprocessing if item.metric_values.get("duration_ms") is not None]
    components["preprocessing"] = ComponentScoreV2(component="preprocessing", observation_count=len(preprocessing), evidence_case_ids=tuple(sorted({item.case_id for item in preprocessing})), hard_gate_failures=tuple(sorted(f"{item.case_id}:{gate}" for item in preprocessing for gate, passed in item.hard_gate_flags.items() if not passed)), quality_misses=(), median_latency_ms=_percentile(preprocessing_latencies, 0.5), p95_latency_ms=_percentile(preprocessing_latencies, 0.95), total_input_tokens=None, total_output_tokens=None, evidence_available=len(preprocessing) == 3)
    semantic_cases = tuple(sorted({item.case_id for item in semantic_obs}))
    semantic_hard = tuple(sorted(failure for values in hard_by_variant.values() for failure in values))
    semantic_latencies = [float(item.metric_values["duration_ms"]) for item in semantic_obs if item.metric_values.get("duration_ms") is not None]
    semantic_quality_misses = tuple(sorted({case for value in by_variant_case.values() for case in value}))
    components["semantic_extractor"] = ComponentScoreV2(component="semantic_extractor", observation_count=len(semantic_obs), evidence_case_ids=semantic_cases, hard_gate_failures=semantic_hard, quality_misses=semantic_quality_misses, median_latency_ms=_percentile(semantic_latencies, 0.5), p95_latency_ms=_percentile(semantic_latencies, 0.95), total_input_tokens=None, total_output_tokens=None, evidence_available=len(semantic_obs) == 144, challengers=challengers)
    components["scope_router"] = components["semantic_extractor"].model_copy(update={"component": "scope_router"})
    delivery = [item for item in observations if item.variant in {"ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"}]
    delivery_score = ComponentScoreV2(component="delivery_protocol", observation_count=len(delivery), evidence_case_ids=tuple(sorted({item.case_id for item in delivery})), hard_gate_failures=tuple(sorted(f"{item.case_id}:{gate}" for item in delivery for gate, passed in item.hard_gate_flags.items() if not passed)), quality_misses=(), median_latency_ms=None, p95_latency_ms=None, total_input_tokens=None, total_output_tokens=None, evidence_available=consumer_complete and len(delivery) == 150)
    components["delivery_protocol"] = delivery_score
    components["profile_compiler"] = delivery_score.model_copy(update={"component": "profile_compiler"})
    reliability = [item for item in observations if item.variant in {"ach_reliability", "official_reliability"}]
    reliability_score = ComponentScoreV2(component="capture_reliability", observation_count=len(reliability), evidence_case_ids=tuple(sorted({item.case_id for item in reliability})), hard_gate_failures=(), quality_misses=(), median_latency_ms=None, p95_latency_ms=None, total_input_tokens=None, total_output_tokens=None, evidence_available=False)
    components["capture_reliability"] = reliability_score
    components["working_state_ordering"] = reliability_score.model_copy(update={"component": "working_state_ordering"})
    components["host_adapters"] = ComponentScoreV2(component="host_adapters", observation_count=len(preprocessing), evidence_case_ids=tuple(sorted({item.case_id for item in preprocessing})), hard_gate_failures=(), quality_misses=(), median_latency_ms=None, p95_latency_ms=None, total_input_tokens=None, total_output_tokens=None, evidence_available=bool(preprocessing))
    return ScorecardV2(run_id=run_id or adjudication.run_id, observation_count=len(observations), consumer_complete=consumer_complete, components=components)


def decide_v2(scorecard: ScorecardV2) -> tuple[ComponentDecision, ...]:
    decisions = []
    for component in COMPONENTS:
        evidence = scorecard.components[component]
        if component in {"profile_compiler", "delivery_protocol"} and not scorecard.consumer_complete:
            ruling, reason = "insufficient_evidence", "NO_CONSUMER_EVIDENCE"
        elif not evidence.evidence_available:
            ruling, reason = "insufficient_evidence", f"INSUFFICIENT_{component.upper()}_EVIDENCE"
        elif evidence.hard_gate_failures:
            ruling, reason = "keep", "HARD_GATE_FAILURE"
        elif any(not challenger.non_inferior for challenger in evidence.challengers.values()):
            ruling, reason = "keep", "REPEATABLE_NAMED_CASE_REGRESSION"
        elif any(challenger.approval_required for challenger in evidence.challengers.values()):
            ruling, reason = "keep", "APPROVAL_REQUIRED_ONE_NAMED_CASE"
        else:
            ruling, reason = "keep", f"{component.upper()}_BASELINE_COMPARISON"
        decisions.append(ComponentDecision(component=component, ruling=ruling, approval_required=any(challenger.approval_required for challenger in evidence.challengers.values()) or ruling == "insufficient_evidence", evidence_case_ids=evidence.evidence_case_ids, reason_codes=(reason,)))
    return tuple(decisions)
