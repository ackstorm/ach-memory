"""Normalized semantic adapters; no worker or production persistence."""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import RunObservation, SemanticCase


class NormalizedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    scope: Literal["user", "project"]
    current: bool
    source_ids: tuple[str, ...]


class SemanticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: tuple[NormalizedClaim, ...]
    working_state: dict[str, object] | None
    document_scopes: tuple[Literal["user", "project", "shared"], ...]
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int


class SemanticVariant(Protocol):
    name: Literal["ach_semantic", "native_semantic", "hybrid_semantic"]

    def run(self, *, case: SemanticCase, repetition: int, banks) -> SemanticOutput: ...


def split_minimal(content: str, client=None) -> dict:
    """Return a deliberately minimal projection envelope for adapter tests."""
    claims = []
    for line in content.splitlines():
        if line.startswith("user:"):
            claims.append({"text": line[5:].strip(), "bank": "user", "provenance": "raw-span"})
        elif line.startswith("project:"):
            claims.append({"text": line[8:].strip(), "bank": "project", "provenance": "raw-span"})
    return {"claims": claims, "working_state": None}


def run_semantic_case(case: SemanticCase, variant: str, repetition: int) -> RunObservation:
    content = "\n".join(f"{turn['role']}: {turn['text']}" for turn in case.transcript)
    flags = {"no_canary": not any(canary in content for canary in case.secret_canaries), "no_shared_document": variant != "native_semantic"}
    return RunObservation(case_id=case.id, variant=variant, repetition=repetition, artifact_relpath=f"semantic/{case.id}/{variant}-{repetition}.json", hard_gate_flags=flags, metric_values={"byte_count": len(content.encode()), "duration_ms": 0})
