"""Submit one durable, sanitized, idempotent typed retain to Hindsight.

The only semantic write path for `0.4.0`: a caller submits one claim plus
bounded evidence, ACH persists sanitized provenance first, then represents
only the canonical claim upstream under the frozen `ach-exact-v1` strategy.
Evidence never reaches Hindsight (SPEC §5.5).
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic, sleep

from sqlalchemy.orm import Session

from memory import projects
from memory.auth.principal import Principal
from memory.banks import resolve_user_bank
from memory.errors import HindsightError, ProjectContextUnavailable
from memory.hindsight.client import HindsightClient, RetainItem
from memory.models import RetainedRecord
from memory.retained_records import (
    LogicalBankRef,
    accept_retain,
    document_id_for,  # noqa: F401 -- re-exported: the deterministic identity Task 3 cites.
    get_by_operation,
)
from memory.sanitization import normalize_claim, sanitize_evidence
from memory.v040_contracts import TypedRetainRequest, TypedRetainResponse

_POLL_INTERVAL_SECONDS = 0.25
_POLL_TIMEOUT_SECONDS = 30.0
_TERMINAL_STATUSES = ("completed", "failed")


def _resolve_bank(db: Session, principal: Principal, request: TypedRetainRequest) -> LogicalBankRef:
    """Existing-only resolution (create=False): ordinary retain never mints
    an unknown project."""
    if request.scope == "user":
        bank_id = resolve_user_bank(db, principal, request.user_id)
        target_id = request.user_id or principal.user_id
        return LogicalBankRef(principal.tenant_id, "user", target_id, None, bank_id)

    if not request.project_slug:
        raise ProjectContextUnavailable("scope=project needs a project: pass project_slug")
    resolution = projects.resolve(db, principal, request.project_slug, create=False)
    return LogicalBankRef(
        principal.tenant_id,
        "project",
        None,
        resolution.project.internal_id,
        resolution.project.bank_id,
    )


def _tags(request: TypedRetainRequest) -> list[str]:
    validity = "expiring" if request.valid_until is not None else "indefinite"
    return [
        f"type:{request.memory_type}",
        f"basis:{request.basis}",
        "schema:ach-retain-v1",
        f"validity:{validity}",
    ]


def _extract_source_memory_id(operation: dict) -> str | None:
    """Read the created memory's stable id off a terminal operation, if the
    upstream response carries it. Never guessed from `document_id`."""
    result = operation.get("result")
    if isinstance(result, dict):
        for key in ("memory_id", "source_memory_id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        many = result.get("memory_ids") or result.get("created_memory_ids")
        if isinstance(many, list) and many and isinstance(many[0], str):
            return many[0]
    for child in operation.get("child_operations") or []:
        if not isinstance(child, dict):
            continue
        for key in ("memory_id", "result_memory_id"):
            value = child.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _read_back_source_memory_id(
    client: HindsightClient, bank: LogicalBankRef, document_id: str
) -> str | None:
    """Fall back to the stable document when the operation body names no
    memory id -- never invent one from `document_id` itself.

    Best-effort: the retain itself already proved `completed`, so a failure
    reading the id back (transport, unexpected shape) must not turn a proven
    success into an error. A later authorized access can retry hydration.
    """
    try:
        listing = client.list_memories(bank.bank_id, document_id=document_id, limit=1)
    except Exception:  # noqa: BLE001 -- see docstring: hydration is best-effort
        return None
    items = listing.get("items") if isinstance(listing, dict) else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        candidate = items[0].get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _response(row: RetainedRecord, *, status: str) -> TypedRetainResponse:
    return TypedRetainResponse(
        record_id=str(row.id),
        operation_id=row.operation_id,
        document_id=row.document_id,
        status=status,
        recorded_at=row.recorded_at,
        valid_until=row.valid_until,
        lifecycle=row.lifecycle,
    )


def submit_retain(
    db: Session,
    principal: Principal,
    request: TypedRetainRequest,
    *,
    client: HindsightClient,
    wait: bool,
    clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], None] = sleep,
) -> TypedRetainResponse:
    bank = _resolve_bank(db, principal, request)
    canonical_content = normalize_claim(request.content)
    sanitized_evidence = [item.model_dump() for item in sanitize_evidence(request.evidence)]

    row, _created = accept_retain(
        db,
        principal,
        request,
        bank=bank,
        canonical_content=canonical_content,
        sanitized_evidence=sanitized_evidence,
    )
    # Committed BEFORE the network call: a transport failure after this point
    # leaves a pending, retry-safe record rather than an orphaned upstream
    # write with no local trace (SPEC §6.1).
    db.commit()

    item = RetainItem(
        content=row.canonical_content,
        document_id=row.document_id,
        metadata={"ach_record_id": str(row.id)},
        tags=_tags(request),
        strategy="ach-exact-v1",
        update_mode="replace",
    )
    result = client.retain_items(bank.bank_id, [item], operation_id=row.operation_id, is_async=True)
    returned_operation_id = result.get("operation_id")
    if returned_operation_id is not None and str(returned_operation_id) != row.operation_id:
        raise HindsightError("memory backend acknowledged a different operation")

    row.upstream_state = "accepted"
    db.commit()

    if not wait:
        return _response(row, status="accepted")

    return _poll_until_terminal(db, bank, row, client=client, clock=clock, sleeper=sleeper)


def _poll_until_terminal(
    db: Session,
    bank: LogicalBankRef,
    row: RetainedRecord,
    *,
    client: HindsightClient,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> TypedRetainResponse:
    deadline = clock() + _POLL_TIMEOUT_SECONDS
    while clock() < deadline:
        operation = client.get_operation(bank.bank_id, row.operation_id)
        if operation.get("status") in _TERMINAL_STATUSES:
            return refresh_operation_state(db, bank, row.operation_id, operation=operation, client=client)
        sleeper(min(_POLL_INTERVAL_SECONDS, max(deadline - clock(), 0)))
    # Deadline expired after upstream acceptance: an accepted async write is
    # never reinterpreted as failed (SPEC §6.2).
    return _response(row, status="pending")


def refresh_operation_state(
    db: Session,
    bank: LogicalBankRef,
    operation_id: str,
    *,
    operation: dict | None = None,
    client: HindsightClient | None = None,
) -> TypedRetainResponse:
    """Reconcile one retained record against a (possibly already-fetched)
    Hindsight operation. Only a terminal status changes `upstream_state`."""
    row = get_by_operation(db, bank, operation_id)
    if row is None:
        raise ValueError(f"no retained record for operation {operation_id}")

    if operation is None:
        if client is None:
            raise ValueError("client is required when operation is not already known")
        operation = client.get_operation(bank.bank_id, operation_id)

    status = operation.get("status")
    if status == "completed":
        source_memory_id = _extract_source_memory_id(operation)
        if source_memory_id is None and client is not None:
            source_memory_id = _read_back_source_memory_id(client, bank, row.document_id)
        if source_memory_id:
            row.source_memory_id = source_memory_id
        row.upstream_state = "completed"
        db.commit()
        return _response(row, status="completed")

    if status == "failed":
        row.upstream_state = "failed"
        db.commit()
        return _response(row, status="failed")

    return _response(row, status="pending")
