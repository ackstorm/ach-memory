"""Normalized semantic adapters; no worker or production persistence."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from memory.capture import local
from memory.capture.extractor import ExtractionFailed, build_extraction_prompt

from .contracts import RunObservation, SemanticCase


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
        options["retain_mission"] = build_extraction_prompt("hindsight_object")
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
    raw_records = [
        {
            "type": turn["role"],
            "message": {
                "role": turn["role"],
                "content": [{"type": "text", "text": turn["text"]}],
            },
        }
        for turn in case.transcript
    ]
    sanitized = local.sanitize(raw_records)
    for canary in case.secret_canaries:
        sanitized = sanitized.replace(canary, "[redacted]")
    return sanitized


def run_semantic_case(
    case: SemanticCase,
    variant: str,
    repetition: int,
    banks=None,
    artifact_path: Path | None = None,
) -> RunObservation:
    if variant not in {"ach_semantic", "native_semantic", "hybrid_semantic"}:
        raise ValueError("unknown semantic variant")
    content = _canonical(case)
    if any(canary in content for canary in case.secret_canaries):
        raise ValueError("semantic input contains a declared canary")
    if banks is None:
        raise ValueError("semantic variants require a fresh disposable bank client")
    valid_output = True
    started = time.monotonic()
    user_bank_scope_clean = True
    project_bank_scope_clean = True
    no_shared_document = True
    if variant == "ach_semantic":
        from memory.capture.extractor import extract

        bank = banks.create_bank(f"semantic-{case.id}-{variant}", repetition)
        try:
            result = extract(_AchHindsightAdapter(banks), bank, content)
        except ExtractionFailed:
            # A provider response that violates the production contract is a
            # measured semantic failure, not permission to rerun until a
            # favourable sample appears. Infrastructure errors still abort.
            valid_output = False
            count = 0
            scopes = ()
            detail = {"error_code": "EXTRACTION_FAILED"}
        else:
            count = len(result.candidates)
            scopes = tuple(sorted({candidate.bank_kind for candidate in result.candidates}))
            detail = {
                "claims": [candidate.model_dump(mode="json") for candidate in result.candidates],
                "working_state": result.working_state.model_dump(mode="json") if result.working_state else None,
                "dropped": result.dropped,
            }
    elif variant == "native_semantic":
        bank = banks.create_bank(f"semantic-{case.id}-{variant}", repetition)
        receipt = banks.retain_and_wait(bank, content, document_id=f"mq55:{case.id}:{repetition}")
        snapshot = banks.list_bank_objects(bank)
        count = sum(item.layer in {"memory", "observation"} for item in snapshot.objects)
        scopes = ("shared",) if receipt.terminal_state == "completed" else ()
        detail = {
            "receipt": receipt.model_dump(mode="json"),
            "objects": [item.model_dump(mode="json") for item in snapshot.objects],
        }
        persisted_documents = tuple(
            item.original_text
            for item in snapshot.objects
            if item.layer == "document" and item.original_text is not None
        )
        no_shared_document = all(
            content not in document for document in persisted_documents
        )
        user_bank_scope_clean = not persisted_documents
        project_bank_scope_clean = not persisted_documents
    else:
        from .splitter import split_and_persist

        split = split_and_persist(case, repetition, banks)
        count = len(split.output.claims)
        scopes = split.output.document_scopes
        detail = {
            "claims": [claim.model_dump(mode="json") for claim in split.output.claims],
            "working_state": split.output.working_state,
        }
        user_bank_scope_clean = split.user_bank_scope_clean
        project_bank_scope_clean = split.project_bank_scope_clean
        no_shared_document = split.no_shared_document
    duration_ms = int((time.monotonic() - started) * 1000)
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(detail, sort_keys=True, separators=(",", ":")) + "\n")
    return RunObservation(case_id=case.id, variant=variant, repetition=repetition, artifact_relpath=f"semantic/{case.id}/{variant}-{repetition}.json", hard_gate_flags={"executed": True, "valid_output": valid_output, "no_canary": True, "no_shared_document": no_shared_document, "user_bank_scope_clean": user_bank_scope_clean, "project_bank_scope_clean": project_bank_scope_clean}, metric_values={"byte_count": len(content.encode()), "claim_count": count, "scope_count": len(scopes), "duration_ms": duration_ms}, warning_codes=("NATIVE_SHARED_BASELINE",) if variant == "native_semantic" else ())
