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
import time
from collections.abc import Callable
from typing import Any, Literal

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from memory import metrics, working_state
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

StageOutcome = Literal["advanced", "waiting", "failed", "lease_lost"]


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


def _renew(db: Session, row: CaptureSlice, *, owner: str, lease_seconds: int) -> None:
    """Give this row a full lease window around one external call.

    Called immediately before and immediately after every call that leaves
    this process. Before, because a row queued behind a slow sibling in the
    same batch would otherwise start its own work on whatever lease time was
    left over; after, because a call that outran even a fresh lease must be
    caught before its result is written, not once it already has been.

    Committed rather than merely flushed: an uncommitted renewal is
    invisible to the worker deciding whether this row is stealable. Safe at
    every call site because each sits between two committed stage
    boundaries, so there is never other pending work to commit early.
    """
    repository.renew_lease(db, row, owner=owner, lease_seconds=lease_seconds)
    db.commit()


# --------------------------------------------------------------------------
# Stage handlers. Each persists its own result and returns -- never chains
# into the next stage within one call.
# --------------------------------------------------------------------------


def _extract_stage(
    db: Session,
    client: HindsightClient,
    row: CaptureSlice,
    *,
    owner: str,
    lease_seconds: int,
    max_attempts: int,
) -> StageOutcome:
    project = db.get(Project, row.project_internal_id)
    _renew(db, row, owner=owner, lease_seconds=lease_seconds)
    try:
        result = extract(client, project.bank_id, row.sanitized_content or "")
    except (ExtractionFailed, HindsightError) as exc:
        code = "EXTRACTION_FAILED" if isinstance(exc, ExtractionFailed) else "HINDSIGHT_UNAVAILABLE"
        repository.record_failure(
            db, row, owner=owner, error_code=code, max_attempts=max_attempts
        )
        db.commit()
        return "failed"

    _renew(db, row, owner=owner, lease_seconds=lease_seconds)
    repository.advance_stage(
        db, row, owner=owner, status="retaining", extraction=_serialize_extraction(result)
    )
    db.commit()
    return "advanced"


def _retain_stage(
    db: Session,
    client: HindsightClient,
    row: CaptureSlice,
    *,
    owner: str,
    lease_seconds: int,
    max_attempts: int,
) -> StageOutcome:
    extraction = _deserialize_extraction(row.extraction)

    if not extraction.candidates:
        repository.advance_stage(db, row, owner=owner, status="applying")
        db.commit()
        return "advanced"

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
        repository.advance_stage(
            db, row, owner=owner, status="retaining", hindsight_operations=operations
        )
        db.commit()
        return "advanced"

    # Sub-stage 2: retain exactly one not-yet-acknowledged bank's items,
    # using the operation ID already persisted for it.
    operations = dict(row.hindsight_operations)
    pending_bank = next((bk for bk, op in operations.items() if not op["acknowledged"]), None)
    if pending_bank is None:
        repository.advance_stage(db, row, owner=owner, status="applying")
        db.commit()
        return "advanced"

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
    _renew(db, row, owner=owner, lease_seconds=lease_seconds)
    try:
        client.retain_items(
            bank_id,
            items_by_bank[pending_bank],
            operation_id=operations[pending_bank]["operation_id"],
        )
    except HindsightError:
        repository.record_failure(
            db, row, owner=owner, error_code="RETAIN_FAILED", max_attempts=max_attempts
        )
        db.commit()
        return "failed"

    _renew(db, row, owner=owner, lease_seconds=lease_seconds)
    operations[pending_bank] = {**operations[pending_bank], "acknowledged": True}
    all_acknowledged = all(op["acknowledged"] for op in operations.values())
    repository.advance_stage(
        db,
        row,
        owner=owner,
        status="applying" if all_acknowledged else "retaining",
        hindsight_operations=operations,
    )
    db.commit()
    return "advanced"


def _applying_stage(
    db: Session,
    client: HindsightClient,
    row: CaptureSlice,
    *,
    owner: str,
    lease_seconds: int,
    max_attempts: int,
    correction_refresh_enabled: bool,
) -> StageOutcome:
    operations = row.hindsight_operations or {}

    for bank_kind, op in operations.items():
        bank_id = _bank_id_for(db, row, bank_kind)
        _renew(db, row, owner=owner, lease_seconds=lease_seconds)
        try:
            status_result = client.get_operation(bank_id, op["operation_id"])
        except HindsightError:
            repository.record_failure(
                db, row, owner=owner, error_code="OPERATION_POLL_FAILED", max_attempts=max_attempts
            )
            db.commit()
            return "failed"
        _renew(db, row, owner=owner, lease_seconds=lease_seconds)
        if filer.is_complete(status_result):
            continue
        if filer.is_pending(status_result):
            # Not ready. Not a failure either -- release the lease and let
            # the next poll cycle check again.
            repository.release_lease(db, row, owner=owner)
            db.commit()
            return "waiting"
        # Terminal, or a status this client does not recognize. Either way
        # the operation is never going to complete, and treating it as
        # pending parked the row on this stage forever, re-polling a dead
        # operation id every cycle (Phase 3 review finding 5). Charge an
        # attempt so it reaches the terminal `failed` and stops being
        # claimable.
        repository.record_failure(
            db, row, owner=owner, error_code="OPERATION_FAILED", max_attempts=max_attempts
        )
        db.commit()
        return "failed"

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
                db, row, owner=owner, error_code="WORKING_STATE_CONFLICT", max_attempts=max_attempts
            )
            db.commit()
            return "failed"

    if correction_refresh_enabled:
        _renew(db, row, owner=owner, lease_seconds=lease_seconds)
        _refresh_corrected_banks(db, client, row, extraction.candidates)
        _renew(db, row, owner=owner, lease_seconds=lease_seconds)

    repository.complete(db, row, owner=owner)
    db.commit()
    return "advanced"


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
    owner: str,
    lease_seconds: int,
    max_attempts: int,
    correction_refresh_enabled: bool,
) -> None:
    stage_name = {"pending": "extract", "retaining": "retain", "applying": "apply"}.get(
        row.status
    )
    if stage_name is None:
        # "completed"/"failed" rows are never leased
        # (repository.acquire_lease); nothing to do if one somehow reaches
        # here, and nothing worth a metric either -- there was no stage.
        return

    started = time.monotonic()
    try:
        if row.status == "pending":
            outcome = _extract_stage(
                db, client, row, owner=owner, lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
        elif row.status == "retaining":
            outcome = _retain_stage(
                db, client, row, owner=owner, lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
        else:
            outcome = _applying_stage(
                db,
                client,
                row,
                owner=owner,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
                correction_refresh_enabled=correction_refresh_enabled,
            )
    except repository.LeaseLost:
        # The lease expired during this stage's external call and another
        # worker took the row. It has already redone, or is redoing, this
        # work; the correct action is to drop everything computed here.
        db.rollback()
        outcome = "lease_lost"

    metrics.CAPTURE_STAGE_DURATION.labels(stage=stage_name).observe(time.monotonic() - started)
    metrics.CAPTURE_STAGE.labels(stage=stage_name, outcome=outcome).inc()
    if outcome == "failed":
        metrics.CAPTURE_RETRY.labels(
            stage=stage_name, error_code=row.last_error_code or "UNKNOWN"
        ).inc()


def _record_unexpected_failure(
    db: Session, row: CaptureSlice, *, owner: str, settings: Settings
) -> None:
    """Charge an attempt for a failure no stage handler anticipated.

    Runs after a rollback, so the row object in hand is expired; it is
    re-fetched by id. The lease itself survives the rollback -- it was
    committed when the row was leased -- so the fence still applies, and a
    row that has since moved to another worker is simply left alone.
    """
    # The primary key straight off the identity map. Reading `row.id` here
    # could touch a session the failure above may have left unusable; the
    # identity key never does.
    row_id = inspect(row).identity_key[1][0]
    db.rollback()
    try:
        current = db.get(CaptureSlice, row_id)
        if current is None:
            return
        stage_name = {"pending": "extract", "retaining": "retain", "applying": "apply"}.get(
            current.status, "extract"
        )
        repository.record_failure(
            db,
            current,
            owner=owner,
            error_code="UNEXPECTED_ERROR",
            max_attempts=settings.capture_worker_max_attempts,
        )
        db.commit()
    except (repository.LeaseLost, SQLAlchemyError):
        # The row is another worker's now, or the database itself is
        # unhappy. Either way the next cycle is the place to find out.
        db.rollback()
        return
    metrics.CAPTURE_STAGE.labels(stage=stage_name, outcome="failed").inc()
    metrics.CAPTURE_RETRY.labels(stage=stage_name, error_code="UNEXPECTED_ERROR").inc()


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

    # A fresh token per cycle, never one derived from the process or the
    # session: reusing a token defeats the owner fence, because a worker
    # whose lease expired mid-call would still match a row that had been
    # re-leased and handed back to it (Phase 3 review finding 6).
    owner = owner or repository.new_lease_owner()
    rows = repository.acquire_lease(
        db,
        owner=owner,
        lease_seconds=settings.capture_worker_lease_seconds,
        batch_size=settings.capture_worker_batch_size,
        max_attempts=settings.capture_worker_max_attempts,
    )
    db.commit()

    for row in rows:
        try:
            process_row(
                db,
                client,
                row,
                owner=owner,
                lease_seconds=settings.capture_worker_lease_seconds,
                max_attempts=settings.capture_worker_max_attempts,
                correction_refresh_enabled=settings.capture_correction_refresh_enabled,
            )
        except Exception:  # noqa: BLE001 -- one row must not kill the loop
            # One malformed row must not take the queue down with it. Every
            # expected failure is already handled inside the stage handlers,
            # so reaching here means something unforeseen -- a domain error,
            # a row referencing a deleted project, a bug. Charge it an
            # attempt and move on: the alternative is a poison row that
            # kills the loop on every cycle and blocks every good row behind
            # it (Phase 3 review finding 5).
            _record_unexpected_failure(db, row, owner=owner, settings=settings)

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
