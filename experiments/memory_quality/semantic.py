"""Normalized semantic adapters; no worker or production persistence."""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import RunObservation, SemanticCase

_HINDSIGHT_COMPATIBLE_ACH_MISSION = """\
Extract every durable semantic claim from the input. Return exactly one JSON
object with a `facts` array. Each fact must use only the `text` field, whose
value is one minified JSON object matching the ACH envelope: `record` is
`candidate` or `working_state`; candidates use `text`, `kind`, `origin`, and
`subject`; working state uses `objective`, `current_direction`,
`recent_decisions`, `open_questions`, and `next_steps`. Use only candidate
kind values preference, decision, convention, gotcha, or technical_claim;
only origin values stated, confirmed, observed, or inferred; and only subject
values user or project. Emit no JSONL, markdown, or text outside the outer
object. Do not emit any canary or raw-span marker.
"""


class _AchHindsightAdapter:
    """Adapt the ACH envelope request to Hindsight's single JSON response schema.

    The production extractor deliberately asks its provider for JSONL. Hindsight
    0.9.2 parses the provider response as one JSON document before returning
    facts, so passing that mission verbatim makes multi-claim cases fail with
    ``JSONDecodeError: Extra data``. The input and production parser remain the
    same; only the transport-facing response envelope is adapted here.
    """

    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def dry_run_extract(self, bank_id, content, **options):
        options["retain_mission"] = _HINDSIGHT_COMPATIBLE_ACH_MISSION
        return self._delegate.dry_run_extract(bank_id, content, **options)


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


def _canonical(case: SemanticCase) -> str:
    return "\n".join(f"{turn['role']}: {turn['text']}" for turn in case.transcript)


def run_semantic_case(case: SemanticCase, variant: str, repetition: int, banks=None) -> RunObservation:
    if variant not in {"ach_semantic", "native_semantic", "hybrid_semantic"}:
        raise ValueError("unknown semantic variant")
    content = "\n".join(f"{turn['role']}: {turn['text']}" for turn in case.transcript)
    if any(canary in content for canary in case.secret_canaries):
        raise ValueError("semantic input contains a declared canary")
    if banks is None:
        raise ValueError("semantic variants require a fresh disposable bank client")
    bank = banks.create_bank(f"semantic-{case.id}-{variant}", repetition)
    if variant == "ach_semantic":
        from memory.capture.extractor import extract
        result = extract(_AchHindsightAdapter(banks), bank, content)
        count = len(result.candidates)
        scopes = tuple(sorted({candidate.bank_kind for candidate in result.candidates}))
    elif variant == "native_semantic":
        receipt = banks.retain_and_wait(bank, content, document_id=f"mq55:{case.id}:{repetition}")
        snapshot = banks.list_bank_objects(bank)
        count = sum(item.layer in {"memory", "observation"} for item in snapshot.objects)
        scopes = ("shared",) if receipt.terminal_state == "completed" else ()
    else:
        projection = banks.dry_run_extract(bank, content, retain_extraction_mode="custom", retain_mission="Return only {text, bank, provenance} claims.")
        count = len(projection.get("facts", []))
        scopes = ("user", "project") if count else ()
    return RunObservation(case_id=case.id, variant=variant, repetition=repetition, artifact_relpath=f"semantic/{case.id}/{variant}-{repetition}.json", hard_gate_flags={"no_canary": True, "no_shared_document": variant != "native_semantic"}, metric_values={"byte_count": len(content.encode()), "claim_count": count, "scope_count": len(scopes), "duration_ms": 0}, warning_codes=("NATIVE_SHARED_BASELINE",) if variant == "native_semantic" else ())
