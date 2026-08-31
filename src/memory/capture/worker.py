"""The durable capture queue state machine (SPEC Phase 3 §5-§6).

Each lease invocation (`process_row`) performs at most one externally
visible stage -- one Hindsight call or one Working State write -- persists
its result, and returns. `status` names the stage still to run:

    pending -> retaining -> applying -> completed
                                     \\-> failed (terminal, past max attempts)

("extracting" and "failed-as-a-retry-state" are in the schema's vocabulary
but never produced here: extraction is one atomic, idempotent, read-only
call with no partial state worth its own lease cycle, and
repository.record_failure() leaves `status` at whichever stage just failed
so a retry resumes there instead of restarting from pending -- see that
function's docstring.)

Crash safety is structural, not defensive: every write this module makes is
either read back and reused verbatim on the next lease (extraction,
operation IDs) or is itself idempotent to repeat (dry-run-extract, a retain
call keyed by its already-persisted operation ID, working_state.replace()
at an unchanged checkpoint).
"""

import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from memory import working_state
from memory.auth.principal import Principal
from memory.capture import filer, repository
from memory.capture.contracts import NormalizedCandidate, WorkingStateEnvelope
from memory.capture.extractor import ExtractionFailed, ExtractionResult, extract
from memory.config import Settings, get_settings
from memory.errors import HindsightError, WorkingStateConflict, WorkingStateStale
from memory.hindsight.client import HindsightClient
from memory.models import CaptureSlice, Project, User
from memory.working_state import WorkingStateWrite

_WORKER_PRINCIPAL_KEY_ID = "capture-worker"


def _serialize_extraction(result: ExtractionResult) -> dict[str, Any]:
    return {
        "candidates": [c.model_dump(mode="json") for c in result.candidates],
        "working_state": (
            result.working_state.model_dump(mode="json") if result.working_state else None
        ),
    }


def _deserialize_extraction(data: dict[str, Any] | None) -> ExtractionResult:
    data = data or {}
    candidates = [NormalizedCandidate.model_validate(c) for c in data.get("candidates") or []]
    raw_working_state = data.get("working_state")
    ws = WorkingStateEnvelope.model_validate(raw_working_state) if raw_working_state else None
    return ExtractionResult(candidates=candidates, working_state=ws)


def _bank_id_for(db: Session, row: CaptureSlice, bank_kind: str) -> str:
    if bank_kind == "user":
        return db.get(User, row.user_id).bank_id
    return db.get(Project, row.project_internal_id).bank_id


def _worker_principal(row: CaptureSlice) -> Principal:
    """A background service acting for the row's own owner -- there is no
    HTTP credential here, only the tenant/user this slice already belongs
    to (SPEC: `set_working_state` remains the explicit surface; the
    background worker is the only automatic writer)."""
    return Principal(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        is_master=False,
        key_id=_WORKER_PRINCIPAL_KEY_ID,
        credential_id=_WORKER_PRINCIPAL_KEY_ID,
    )


# --------------------------------------------------------------------------
# Stage handlers. Each persists its own result and returns -- never chains
# into the next stage within one call.
# --------------------------------------------------------------------------


def _extract_stage(
    db: Session, client: HindsightClient, row: CaptureSlice, *, max_attempts: int
) -> None:
    project = db.get(Project, row.project_internal_id)
    try:
        result = extract(client, project.bank_id, row.sanitized_content or "")
    except (ExtractionFailed, HindsightError) as exc:
        code = "EXTRACTION_FAILED" if isinstance(exc, ExtractionFailed) else "HINDSIGHT_UNAVAILABLE"
        repository.record_failure(db, row, error_code=code, max_attempts=max_attempts)
        db.commit()
        return

    repository.advance_stage(
        db, row, status="retaining", extraction=_serialize_extraction(result)
    )
    db.commit()


def _retain_stage(
    db: Session, client: HindsightClient, row: CaptureSlice, *, max_attempts: int
) -> None:
    extraction = _deserialize_extraction(row.extraction)

    if not extraction.candidates:
        repository.advance_stage(db, row, status="applying")
        db.commit()
        return

    if row.hindsight_operations is None:
        # Sub-stage 1: persist deterministic operation IDs before ever
        # calling Hindsight, so a crash here loses nothing -- the next
        # lease reuses the identical IDs rather than generating new ones.
        bank_kinds = {c.bank_kind for c in extraction.candidates}
        operations = {
            bank_kind: {
                "operation_id": filer.operation_id(str(row.id), bank_kind),
                "acknowledged": False,
            }
            for bank_kind in sorted(bank_kinds)
        }
        repository.advance_stage(db, row, status="retaining", hindsight_operations=operations)
        db.commit()
        return

    # Sub-stage 2: retain exactly one not-yet-acknowledged bank's items,
    # using the operation ID already persisted for it.
    operations = dict(row.hindsight_operations)
    pending_bank = next((bk for bk, op in operations.items() if not op["acknowledged"]), None)
    if pending_bank is None:
        repository.advance_stage(db, row, status="applying")
        db.commit()
        return

    doc_id = filer.document_id(row.session_id, row.start_offset, row.end_offset, row.content_hash)
    items_by_bank = filer.build_items(
        extraction.candidates,
        doc_id=doc_id,
        host=row.host,
        session_id=row.session_id,
        session_epoch=row.session_epoch,
        checkpoint_seq=row.end_offset,
        sanitized_hash=row.sanitized_hash,
    )
    bank_id = _bank_id_for(db, row, pending_bank)
    try:
        client.retain_items(
            bank_id,
            items_by_bank[pending_bank],
            operation_id=operations[pending_bank]["operation_id"],
        )
    except HindsightError:
        repository.record_failure(db, row, error_code="RETAIN_FAILED", max_attempts=max_attempts)
        db.commit()
        return

    operations[pending_bank] = {**operations[pending_bank], "acknowledged": True}
    all_acknowledged = all(op["acknowledged"] for op in operations.values())
    repository.advance_stage(
        db,
        row,
        status="applying" if all_acknowledged else "retaining",
        hindsight_operations=operations,
    )
    db.commit()


def _applying_stage(
    db: Session,
    client: HindsightClient,
    row: CaptureSlice,
    *,
    max_attempts: int,
    correction_refresh_enabled: bool,
) -> None:
    operations = row.hindsight_operations or {}

    for bank_kind, op in operations.items():
        bank_id = _bank_id_for(db, row, bank_kind)
        try:
            status_result = client.get_operation(bank_id, op["operation_id"])
        except HindsightError:
            repository.record_failure(
                db, row, error_code="OPERATION_POLL_FAILED", max_attempts=max_attempts
            )
            db.commit()
            return
        if not filer.is_complete(status_result):
            # Not ready. Not a failure either -- release the lease and let
            # the next poll cycle check again.
            repository.release_lease(db, row)
            db.commit()
            return

    extraction = _deserialize_extraction(row.extraction)
    principal = _worker_principal(row)

    if extraction.working_state is not None:
        project = db.get(Project, row.project_internal_id)
        ws = extraction.working_state
        write = WorkingStateWrite(
            project_slug=project.project_slug,
            workspace_id=row.workspace_id,
            session_id=row.session_id,
            session_epoch=row.session_epoch,
            checkpoint_seq=row.end_offset,
            objective=ws.objective,
            current_direction=ws.current_direction,
            recent_decisions=ws.recent_decisions,
            open_questions=ws.open_questions,
            next_steps=ws.next_steps,
        )
        try:
            working_state.replace(db, principal, write)
        except WorkingStateStale:
            # A later slice already advanced past this one. This slice's
            # evidence is still filed; only the Working State pointer lost
            # the race, which is the designed outcome, not an error.
            pass
        except WorkingStateConflict:
            repository.record_failure(
                db, row, error_code="WORKING_STATE_CONFLICT", max_attempts=max_attempts
            )
            db.commit()
            return

    if correction_refresh_enabled:
        _refresh_corrected_banks(db, client, row, extraction.candidates)

    repository.complete(db, row)
    db.commit()


def _refresh_corrected_banks(
    db: Session, client: HindsightClient, row: CaptureSlice, candidates: list[NormalizedCandidate]
) -> None:
    """Request a mental-model refresh only for a bank an explicit correction
    actually touched -- fenced to that bank alone, never a blanket refresh.
    Guarded by the caller on capture_correction_refresh_enabled; this
    function assumes that gate already passed."""
    corrected_banks = sorted({c.bank_kind for c in candidates if c.correction})
    for bank_kind in corrected_banks:
        bank_id = _bank_id_for(db, row, bank_kind)
        models = client.list_mental_models(bank_id)
        for model in models.get("mental_models") or []:
            model_id = model.get("id")
            if model_id:
                client.refresh_mental_model(bank_id, model_id)


def process_row(
    db: Session,
    client: HindsightClient,
    row: CaptureSlice,
    *,
    max_attempts: int,
    correction_refresh_enabled: bool,
) -> None:
    if row.status == "pending":
        _extract_stage(db, client, row, max_attempts=max_attempts)
    elif row.status == "retaining":
        _retain_stage(db, client, row, max_attempts=max_attempts)
    elif row.status == "applying":
        _applying_stage(
            db,
            client,
            row,
            max_attempts=max_attempts,
            correction_refresh_enabled=correction_refresh_enabled,
        )
    # "completed"/"failed" rows are never leased (repository.acquire_lease);
    # nothing to do if one somehow reaches here.


def run_once(
    db: Session, client: HindsightClient, *, owner: str | None = None, settings: Settings | None = None
) -> bool:
    """Lease and process up to `capture_worker_batch_size` rows, one stage
    each. Returns whether any row was leased, so a polling loop knows
    whether to skip its sleep. Disabled means no lease acquisition at all --
    never "lease then skip", which would hold rows another worker could
    otherwise claim."""
    settings = settings or get_settings()
    if not settings.capture_worker_enabled:
        return False

    owner = owner or f"worker-{id(db):x}"
    rows = repository.acquire_lease(
        db,
        owner=owner,
        lease_seconds=settings.capture_worker_lease_seconds,
        batch_size=settings.capture_worker_batch_size,
        max_attempts=settings.capture_worker_max_attempts,
    )
    db.commit()

    for row in rows:
        process_row(
            db,
            client,
            row,
            max_attempts=settings.capture_worker_max_attempts,
            correction_refresh_enabled=settings.capture_correction_refresh_enabled,
        )

    return bool(rows)


def run_forever(
    db_factory: Callable[[], Session], client: HindsightClient, *, stop: threading.Event
) -> None:
    """Interruptible polling loop for `capture-worker` (no `--once`): each
    cycle opens its own session (a lease must not outlive the transaction
    that acquired it), calls run_once(), and sleeps the configured poll
    interval unless it found work -- in which case it loops immediately to
    drain the queue."""
    while not stop.is_set():
        settings = get_settings()
        with db_factory() as db:
            found_work = run_once(db, client, settings=settings)
        if not found_work:
            stop.wait(settings.capture_worker_poll_interval_seconds)
