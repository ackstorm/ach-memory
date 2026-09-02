"""Closed contracts for the non-production memory-quality bake-off."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

SEMANTIC_V2_CORPUS_VERSION = "semantic-v2"

Scope = Literal["user", "project", "working_state", "ignore"]
Variant = Literal[
    "ach_preprocess", "official_preprocess", "hybrid_preprocess",
    "ach_semantic", "native_semantic", "hybrid_semantic",
    "ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery",
    "ach_reliability", "official_reliability",
]


class ExpectedUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: str
    scope: Scope
    critical: bool
    current: bool
    required_literals: tuple[str, ...]
    forbidden_literals: tuple[str, ...] = ()


class SemanticCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    transcript: tuple[dict[str, str], ...]
    expected_units: tuple[ExpectedUnit, ...]
    secret_canaries: tuple[str, ...]
    repetitions: Literal[3] = 3


class DeliveryCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    task: str
    user_profile: dict[str, object]
    project_profile: dict[str, object]
    project_metadata: tuple[str, ...]
    working_state: dict[str, object] | None
    history_evidence: tuple[str, ...]
    expected_units: tuple[ExpectedUnit, ...]
    secret_canaries: tuple[str, ...]
    repetitions: Literal[3] = 3


class RunObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    variant: Variant
    repetition: int
    artifact_relpath: str
    hard_gate_flags: dict[str, bool]
    metric_values: dict[str, int | float | None]
    warning_codes: tuple[str, ...] = ()


class SemanticRepairManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    mode: Literal["semantic_repair"] = "semantic_repair"
    hindsight_version: Literal["0.9.2"]
    official_package_version: Literal["0.5.1"]
    semantic_digest: str
    observation_count: Literal[144]
    disposable_bank_count: Literal[144]
    native_retain_count: Literal[48]
    cleanup_complete: bool
    mutating_requests: Literal[336]


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate: str
    passed: bool
    failing_case_ids: tuple[str, ...] = ()


class ComponentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: Literal[
        "host_adapters", "preprocessing", "semantic_extractor", "scope_router",
        "profile_compiler", "delivery_protocol", "capture_reliability",
        "working_state_ordering",
    ]
    ruling: Literal[
        "keep", "replace_with_official", "simplify_to_hybrid",
        "add_follow_up_guard", "insufficient_evidence",
    ]
    approval_required: bool
    evidence_case_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


class RetainReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    operation_id: str
    terminal_state: Literal["completed", "failed"]
    duration_ms: int


class SnapshotObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Literal["document", "memory", "observation", "mental_model", "page"]
    object_id: str
    text: str
    source_ids: tuple[str, ...] = ()


class BankSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank_id: str
    objects: tuple[SnapshotObject, ...]


def _load(path: Path, model: type[BaseModel]) -> tuple[BaseModel, ...]:
    values = []
    for line in path.read_text().splitlines():
        if line.strip():
            values.append(model.model_validate(json.loads(line)))
    return tuple(values)


def load_semantic_cases(path: Path) -> tuple[SemanticCase, ...]:
    return _load(path, SemanticCase)  # type: ignore[return-value]


def load_delivery_cases(path: Path) -> tuple[DeliveryCase, ...]:
    cases = _load(path, DeliveryCase)
    # Validate the fixture-facing dictionaries against the production profile
    # documents without importing any database or service boundary.
    from memory.profiles import ProjectProfileDocument, UserProfileDocument

    for case in cases:
        UserProfileDocument.model_validate(case.user_profile)
        ProjectProfileDocument.model_validate(case.project_profile)
    return cases  # type: ignore[return-value]


def corpus_digest(cases: Sequence[BaseModel]) -> str:
    payload = [case.model_dump(mode="json") for case in cases]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
