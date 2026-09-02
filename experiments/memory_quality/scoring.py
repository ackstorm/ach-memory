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
    return Scorecard(gates=(GateResult(gate="adjudication_complete", passed=not failures, failing_case_ids=failures),), named_misses=failures, median_latency_ms=None, p95_latency_ms=None, total_input_tokens=0, total_output_tokens=0, non_inferior=not failures, approval_required=bool(failures))


COMPONENTS = ("host_adapters", "preprocessing", "semantic_extractor", "scope_router", "profile_compiler", "delivery_protocol", "capture_reliability", "working_state_ordering")


def decide(scorecard: Scorecard) -> tuple[ComponentDecision, ...]:
    ruling = "keep" if scorecard.non_inferior else "insufficient_evidence"
    return tuple(ComponentDecision(component=component, ruling=ruling, approval_required=scorecard.approval_required, evidence_case_ids=scorecard.gates[0].failing_case_ids, reason_codes=("NO_COMPLETE_EVIDENCE" if not scorecard.non_inferior else "BASELINE_ONLY",)) for component in COMPONENTS)
