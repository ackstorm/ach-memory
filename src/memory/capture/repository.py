"""Idempotent checkpoint acceptance and the durable lease/stage primitives
the Task 6 worker state machine is built on (SPEC Phase 3 §5-§6).

Acceptance never calls an LLM or Hindsight -- it is one bounded PostgreSQL
transaction that either returns the row already there (a lost-acknowledgement
retry) or inserts a new one. Leasing uses `FOR UPDATE SKIP LOCKED` so two
worker processes racing for the same batch never both claim one row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import projects, working_state
from memory.auth.principal import Principal
from memory.capture.contracts import CaptureStatus
from memory.errors import CaptureConflict
from memory.models import CaptureSlice
from memory.projects import Resolution

# A row stops being auto-leased after this many failed attempts (SPEC: "After
# eight failed attempts the row remains failed; a master-only operational
# retry may reset available_at"). Backoff is exponential, capped at five
# minutes.
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BACKOFF_CAP_SECONDS = 300

_ACTIVE_STATUSES = ("pending", "extracting", "retaining", "applying")


@dataclass(frozen=True)
class AcceptResult:
    row: CaptureSlice
    duplicate: bool
    resolution: Resolution


def _matching(
    principal: Principal,
    project_internal_id: str,
    workspace_id: str,
    session_id: str,
    start_offset: int,
    end_offset: int,
):
    return select(CaptureSlice).where(
        CaptureSlice.tenant_id == principal.tenant_id,
        CaptureSlice.user_id == principal.user_id,
        CaptureSlice.project_internal_id == project_internal_id,
        CaptureSlice.workspace_id == workspace_id,
        CaptureSlice.session_id == session_id,
        CaptureSlice.start_offset == start_offset,
        CaptureSlice.end_offset == end_offset,
    )


def accept_checkpoint(
    db: Session,
    principal: Principal,
    *,
    host: str,
    session_id: str,
    project_slug: str,
    git_locator: str | None,
    workspace_id: str,
    start_offset: int,
    end_offset: int,
    content_hash: str,
    sanitized_hash: str,
    content: str,
) -> AcceptResult:
    """Insert-or-fetch under a savepoint. A duplicate with the exact
    identity/payload returns the original row (`duplicate=True`). An
    overlapping identity (same offsets, a different content or sanitized
    hash) is `CaptureConflict` -- never last-write-wins."""
    resolution = projects.resolve(
        db, principal, project_slug, git_locator=git_locator, create=False
    )
    project = resolution.project

    session_row = working_state.start_session(
        db, principal, project.project_slug, workspace_id, session_id, git_locator
    )

    query = _matching(
        principal, project.internal_id, workspace_id, session_id, start_offset, end_offset
    )
    existing = db.scalar(query)
    if existing is not None:
        if existing.content_hash != content_hash or existing.sanitized_hash != sanitized_hash:
            raise CaptureConflict(
                "a different slice already exists for this offset range",
                session_id=session_id,
                start_offset=start_offset,
                end_offset=end_offset,
            )
        return AcceptResult(row=existing, duplicate=True, resolution=resolution)

    row = CaptureSlice(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        project_internal_id=project.internal_id,
        workspace_id=workspace_id,
        host=host,
        session_id=session_id,
        session_epoch=session_row.session_epoch,
        start_offset=start_offset,
        end_offset=end_offset,
        content_hash=content_hash,
        sanitized_hash=sanitized_hash,
        sanitized_content=content,
        status="pending",
        # The database's clock, not this process's: acquire_lease() compares
        # available_at against `func.now()`, and this app server's wall
        # clock can drift from the database's -- a row inserted with a
        # Python-side timestamp slightly ahead of the database was
        # unleasable until the database's own clock caught up to it.
        available_at=db.execute(select(func.now())).scalar_one(),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return AcceptResult(row=row, duplicate=False, resolution=resolution)
    except IntegrityError:
        # Lost the race for this exact identity; resolve like any other
        # concurrent duplicate against whoever won it.
        existing = db.scalar(query)
        if existing is not None and (
            existing.content_hash == content_hash and existing.sanitized_hash == sanitized_hash
        ):
            return AcceptResult(row=existing, duplicate=True, resolution=resolution)
        raise


# --------------------------------------------------------------------------
# Leases and stage persistence (Task 6 drives these; Task 2 only persists
# them durably).
# --------------------------------------------------------------------------


class LeaseLost(Exception):
    """This worker no longer holds the row it is transitioning.

    Raised, never swallowed: the work whose result was about to be written
    has already been redone (or is being redone) by whoever holds the lease
    now, and writing it anyway is how one slice retains twice.
    """


def new_lease_owner(prefix: str = "worker") -> str:
    """A lease token that is never reused.

    The generation half of owner/generation fencing. A token derived from
    something stable about the process -- its pid, the id() of its session
    -- repeats across cycles, so a worker whose lease expired mid-call would
    still match its own row after another worker had taken it and handed it
    back (Phase 3 review finding 6). A fresh token per acquisition makes
    "is this still my lease?" answerable.
    """
    return f"{prefix}-{uuid.uuid4().hex}"


def acquire_lease(
    db: Session,
    *,
    owner: str,
    lease_seconds: int,
    batch_size: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[CaptureSlice]:
    """Claim up to `batch_size` rows ready to run, skipping any another
    worker already holds. `status` names the stage still to run (pending,
    retaining, applying, ...) and never resets to a generic "failed" below
    `max_attempts` -- see record_failure() -- so a retried row resumes at
    the exact stage that failed rather than restarting from pending. Only
    once a row reaches `max_attempts` does it become the terminal `failed`
    and stop being claimable here at all."""
    now = db.execute(select(func.now())).scalar_one()
    rows = db.scalars(
        select(CaptureSlice)
        .where(
            CaptureSlice.status.in_(_ACTIVE_STATUSES),
            CaptureSlice.attempt_count < max_attempts,
            CaptureSlice.available_at <= now,
            or_(CaptureSlice.lease_until.is_(None), CaptureSlice.lease_until <= now),
        )
        .order_by(CaptureSlice.available_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.lease_owner = owner
        row.lease_until = now + timedelta(seconds=lease_seconds)
    db.flush()
    return list(rows)


def _hold_lease(db: Session, row: CaptureSlice, owner: str) -> None:
    """Refuse the caller's transition unless it still holds the row's lease.

    Every stage between two transitions makes an external call -- an
    extraction, a retain, an operation poll -- and any of them can outlast a
    60-second lease. When that happens the row is re-leased and redone by
    another worker, and the slow worker's write must not land on top of it.
    Re-reads `lease_owner` from the database rather than trusting the
    in-memory row, which is exactly as stale as the worker holding it.
    """
    current = db.execute(
        select(CaptureSlice.lease_owner).where(CaptureSlice.id == row.id).with_for_update()
    ).scalar_one_or_none()
    if current != owner:
        raise LeaseLost(f"lease on capture row {row.id} is no longer held by this worker")


def renew_lease(db: Session, row: CaptureSlice, *, owner: str, lease_seconds: int) -> None:
    """Push this worker's lease out by another `lease_seconds`.

    Called on both sides of every external call (see
    `memory.capture.worker`). The fence alone makes an overrun SAFE -- a
    stale worker's write is refused -- but not LIVE: a batch of five rows
    behind one slow extraction would routinely have expired leases by the
    time the worker reached them, so each would be stolen mid-flight and its
    finished LLM work thrown away, indefinitely. Renewing immediately before
    a call is what gives the row a full lease window for its own work rather
    than whatever was left after its siblings.

    Raises `LeaseLost` if the lease has already moved on -- there is nothing
    to renew, and the caller must not proceed.
    """
    _hold_lease(db, row, owner)
    row.lease_until = db.execute(select(func.now())).scalar_one() + timedelta(
        seconds=lease_seconds
    )
    db.flush()


def release_lease(db: Session, row: CaptureSlice, *, owner: str) -> None:
    _hold_lease(db, row, owner)
    row.lease_owner = None
    row.lease_until = None
    db.flush()


def advance_stage(
    db: Session,
    row: CaptureSlice,
    *,
    owner: str,
    status: CaptureStatus,
    extraction: dict | list | None = None,
    hindsight_operations: dict | list | None = None,
) -> None:
    """Persist one stage's result and relinquish the lease. A retry never
    reruns a successful stage: the caller checks
    `row.extraction`/`row.hindsight_operations` before redoing the work
    that would populate them.

    Always releases the lease: every call site is the end of one lease
    invocation's single externally-visible stage (worker.process_row's
    docstring), so the row must be immediately re-leasable for its next
    stage rather than sitting locked until the lease it already used
    expires.

    Refused if the lease has since moved on (`LeaseLost`): the stage result
    being persisted here was computed against a row someone else now owns.
    """
    _hold_lease(db, row, owner)
    row.status = status
    if extraction is not None:
        row.extraction = extraction
    if hindsight_operations is not None:
        row.hindsight_operations = hindsight_operations
    row.lease_owner = None
    row.lease_until = None
    db.flush()


def record_failure(
    db: Session,
    row: CaptureSlice,
    *,
    owner: str,
    error_code: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_cap_seconds: int = DEFAULT_BACKOFF_CAP_SECONDS,
) -> None:
    """Bump attempts and back off, but leave `status` at whichever stage
    just failed -- pending/retaining/applying -- so the next lease resumes
    there instead of restarting from pending. Only past `max_attempts` does
    the row become the terminal `failed`, at which point acquire_lease()
    stops claiming it and a master-only operational reset is what's left.

    Refused if the lease has since moved on (`LeaseLost`): charging an
    attempt to a row another worker is actively retrying would burn its
    budget for a failure that is no longer its own.
    """
    _hold_lease(db, row, owner)
    row.attempt_count += 1
    row.last_error_code = error_code
    row.lease_owner = None
    row.lease_until = None
    now = db.execute(select(func.now())).scalar_one()
    delay = min(backoff_cap_seconds, 2**row.attempt_count)
    row.available_at = now + timedelta(seconds=delay)
    if row.attempt_count >= max_attempts:
        row.status = "failed"
    db.flush()


def complete(db: Session, row: CaptureSlice, *, owner: str) -> None:
    """Clears sanitized_content and extraction on completion (SPEC): only
    identity, status, counters and operation state remain for replay/audit.

    Refused if the lease has since moved on (`LeaseLost`): completing a row
    another worker is mid-way through would strip the sanitized content out
    from under it.
    """
    _hold_lease(db, row, owner)
    now = db.execute(select(func.now())).scalar_one()
    row.status = "completed"
    row.sanitized_content = None
    row.extraction = None
    row.lease_owner = None
    row.lease_until = None
    row.completed_at = now
    db.flush()

