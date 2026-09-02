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


class VariantScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blind_variant: str
    eligible: bool
    named_misses: tuple[str, ...]


class UnblindedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
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
        results.append(UnblindedItem(case_id=value["case_id"], variant=value["variant"], repetition=value["repetition"], required_units_met=item.required_units_met))
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
