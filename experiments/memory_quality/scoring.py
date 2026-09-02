"""Deterministic blind packet and component decision scoring."""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .contracts import ComponentDecision, GateResult, RunObservation


class BlindItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    blind_variant: str
    repetition: int
    artifact_relpath: str


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
    variant_scores: tuple[VariantScore, ...] = ()


class VariantScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blind_variant: str
    eligible: bool
    named_misses: tuple[str, ...]


def build_blind_packet(observations: Sequence[RunObservation], key: bytes) -> BlindPacket:
    items = tuple(sorted((BlindItem(case_id=o.case_id, blind_variant="V-" + hmac.new(key, o.variant.encode(), hashlib.sha256).hexdigest()[:4], repetition=o.repetition, artifact_relpath=o.artifact_relpath) for o in observations), key=lambda x: (x.case_id, x.blind_variant, x.repetition, x.artifact_relpath)))
    digest = hashlib.sha256("\n".join(item.model_dump_json() for item in items).encode()).hexdigest()
    return BlindPacket(corpus_digest=digest, items=items)


def score_run(packet: BlindPacket, adjudication: Adjudication) -> Scorecard:
    expected = {(item.case_id, item.blind_variant, item.repetition) for item in packet.items}
    actual = {(item.case_id, item.blind_variant, item.repetition) for item in adjudication.items}
    if expected != actual:
        raise ValueError("adjudication is incomplete or contains duplicates")
    failures = tuple(sorted(item.case_id for item in adjudication.items if item.notes_code == "ADJUDICATION_BLOCKED"))
    by_variant: dict[str, list[str]] = {}
    for item in adjudication.items:
        misses = by_variant.setdefault(item.blind_variant, [])
        if item.wrong_scope_claims:
            misses.append(f"{item.case_id}:WRONG_SCOPE")
        if item.unsupported_current_claims:
            misses.append(f"{item.case_id}:UNSUPPORTED_CURRENT")
    variants = tuple(VariantScore(blind_variant=name, eligible=not misses and not failures, named_misses=tuple(sorted(misses))) for name, misses in sorted(by_variant.items()))
    return Scorecard(gates=(GateResult(gate="adjudication_complete", passed=not failures, failing_case_ids=failures),), named_misses=failures, median_latency_ms=None, p95_latency_ms=None, total_input_tokens=0, total_output_tokens=0, non_inferior=not failures and all(item.eligible for item in variants), approval_required=bool(failures or any(item.named_misses for item in variants)), variant_scores=variants)


COMPONENTS = ("host_adapters", "preprocessing", "semantic_extractor", "scope_router", "profile_compiler", "delivery_protocol", "capture_reliability", "working_state_ordering")


def decide(scorecard: Scorecard) -> tuple[ComponentDecision, ...]:
    decisions = []
    has_delivery_evidence = any(item.blind_variant.startswith("V-") for item in scorecard.variant_scores)
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
