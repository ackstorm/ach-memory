"""Deterministic Hindsight items/operations and completion polling (SPEC
Phase 3 §6). One async retain call per resolved bank, one deterministic
UUIDv5 operation ID per (capture_id, bank_kind), and `update_mode="append"`
always -- a retry relies on the operation ID being identical every time,
never on document replacement as a casual retry.
"""

import uuid

from memory.capture.contracts import NormalizedCandidate
from memory.hindsight.client import HindsightClient, RetainItem

# Fixed, not secret: a stable root so operation/document IDs are
# reproducible across processes and restarts, the same way any UUIDv5 scheme
# is meant to work.
_NAMESPACE = uuid.UUID("6f1f9b2a-6b0e-4c9e-9f7d-2b7a6e6b9c2d")


def operation_id(capture_id: str, bank_kind: str) -> str:
    """One per (capture_id, bank_kind), always -- never regenerated on
    retry, so Hindsight's own idempotency (keyed by this id) is what makes
    resubmission safe."""
    return str(uuid.uuid5(_NAMESPACE, f"{capture_id}:{bank_kind}"))


def document_id(session_id: str, start_offset: int, end_offset: int, content_hash: str) -> str:
    """The content-addressed slice key every bank's retain operation for
    this slice shares. The same slice identity always names the same
    document, across every retry and across both banks."""
    key = f"{session_id}:{start_offset}:{end_offset}:{content_hash}"
    return str(uuid.uuid5(_NAMESPACE, key))


def build_items(
    candidates: list[NormalizedCandidate],
    *,
    doc_id: str,
    host: str,
    session_id: str,
    session_epoch: int,
    checkpoint_seq: int,
    sanitized_hash: str,
) -> dict[str, list[RetainItem]]:
    """Group candidates into RetainItems by resolved bank_kind. Metadata
    carries origin/kind/provenance/host/session/session_epoch/checkpoint/
    slice hash -- user, session and origin never become tags: tags are
    exactly `kind:<kind>` plus the eligibility, nothing else.
    """
    grouped: dict[str, list[RetainItem]] = {}
    for candidate in candidates:
        metadata: dict[str, object] = {
            "origin": candidate.origin,
            "kind": candidate.kind,
            "host": host,
            "session_id": session_id,
            "session_epoch": session_epoch,
            "checkpoint_seq": checkpoint_seq,
            "slice_hash": sanitized_hash,
        }
        if candidate.provenance is not None:
            metadata["provenance"] = {
                "type": candidate.provenance.type,
                "start": candidate.provenance.start,
                "end": candidate.provenance.end,
            }
        if candidate.negative:
            metadata["negative"] = True
        if candidate.correction:
            metadata["correction"] = True

        item = RetainItem(
            content=candidate.text,
            document_id=doc_id,
            metadata=metadata,
            tags=list(candidate.tags),
            observation_scopes=[list(scope) for scope in candidate.observation_scopes],
            strategy="candidate_verbatim",
            update_mode="append",
        )
        grouped.setdefault(candidate.bank_kind, []).append(item)
    return grouped


def file_candidates(
    client: HindsightClient,
    *,
    capture_id: str,
    candidates: list[NormalizedCandidate],
    bank_ids: dict[str, str],
    doc_id: str,
    host: str,
    session_id: str,
    session_epoch: int,
    checkpoint_seq: int,
    sanitized_hash: str,
) -> dict[str, str]:
    """File every candidate, one async retain call per resolved bank whose
    candidates survived classification. Returns {bank_kind: operation_id}
    for whichever banks had candidates -- the caller persists this before
    polling (memory.capture.repository.advance_stage)."""
    grouped = build_items(
        candidates,
        doc_id=doc_id,
        host=host,
        session_id=session_id,
        session_epoch=session_epoch,
        checkpoint_seq=checkpoint_seq,
        sanitized_hash=sanitized_hash,
    )
    operations: dict[str, str] = {}
    for bank_kind, items in grouped.items():
        op_id = operation_id(capture_id, bank_kind)
        client.retain_items(bank_ids[bank_kind], items, operation_id=op_id)
        operations[bank_kind] = op_id
    return operations


def is_complete(operation: dict) -> bool:
    """True only when Hindsight reports the operation durably done. Pending
    or failed never reports complete -- a caller must not advance Working
    State on a maybe."""
    return operation.get("status") == "completed"


# The only two statuses that mean "ask again later". Everything else --
# `failed`, `not_found`, or a status this client has never heard of -- is
# terminal, because an operation id Hindsight no longer recognizes will not
# start being recognized on the next poll.
_PENDING_STATUSES = frozenset({"pending", "running"})


def is_pending(operation: dict) -> bool:
    """True while the operation may still complete.

    Deliberately an allowlist rather than `not is_complete(...)`: a failed
    or forgotten operation read as "still pending" is a row that re-polls a
    dead id every cycle and never reaches a terminal state at all (SPEC
    Phase 3 review finding 5).
    """
    return operation.get("status") in _PENDING_STATUSES
