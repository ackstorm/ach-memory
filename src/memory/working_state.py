"""The single explicit human handoff per (tenant, user, project, workspace).

PostgreSQL state, never a Hindsight fact, observation, document or
mental-model input (SPEC Working State). Replacement is total -- no payload
history, no TTL, no decay -- and only for a session/checkpoint pair that is
lexicographically greater than the one already stored. A session's
`session_epoch` is server-issued and strictly increasing, so a caller can
never win a race by inventing a large one.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import projects
from memory.auth.principal import Principal
from memory.contracts import WORKSPACE_ID_PATTERN as _WORKSPACE_ID_PATTERN_SOURCE
from memory.contracts import (
    CheckpointSeq,
    SessionEpoch,
    SessionId,
    WorkingStateLine,
    WorkingStateLines,
    WorkspaceId,
)
from memory.db import db_now
from memory.delivery import count_tokens
from memory.errors import WorkingSessionNotFound, WorkingStateConflict, WorkingStateStale
from memory.models import WorkingSession, WorkingState

# Compiled once so every Working State boundary uses the shared contract.
WORKSPACE_ID_PATTERN = re.compile(_WORKSPACE_ID_PATTERN_SOURCE)


@dataclass(frozen=True)
class RenderedSection:
    text: str
    refreshed_at: str | None


def render_inert(text: str) -> str:
    return text.replace("<", "\u2039").replace(">", "\u203a")


def format_age(updated_at: datetime, now: datetime) -> str:
    seconds = max(int((now - updated_at).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class WorkingStateWrite(BaseModel):
    """The `set_working_state` request body. REST and MCP validate against
    this exact model, so the two surfaces can never drift apart on bounds.

    extra="forbid": a misspelled field would otherwise validate cleanly with
    the real field left at its default, silently writing nothing the caller
    asked for.
    """

    model_config = ConfigDict(extra="forbid")

    project_slug: str
    workspace_id: WorkspaceId
    session_id: SessionId
    session_epoch: SessionEpoch
    checkpoint_seq: CheckpointSeq
    objective: WorkingStateLine
    current_direction: WorkingStateLine | None = None
    recent_decisions: WorkingStateLines = Field(default_factory=list)
    open_questions: WorkingStateLines = Field(default_factory=list)
    next_steps: WorkingStateLines = Field(default_factory=list)
    git_locator: str | None = None

    @model_validator(mode="after")
    def fits_delivery_contract(self):
        canonical = self.model_dump_json(exclude={"git_locator"})
        rendered = render_working_state_fields(self)
        if len(canonical.encode("utf-8")) > 2048 or count_tokens(rendered) > 512:
            raise ValueError("Working State exceeds its 2 KiB or 512-token budget")
        return self


def start_session(
    db: Session,
    principal: Principal,
    project_slug: str,
    workspace_id: str,
    session_id: str,
    git_locator: str | None = None,
) -> WorkingSession:
    """Allocate this session's epoch, or return the one already allocated.

    Reusing the same session_id for the same (user, project, workspace) tuple
    must return the SAME epoch every time -- it is how a client recovers its
    identity after losing the response to a network error, and how two
    concurrent starts of the same session converge on one epoch instead of
    each getting their own.
    """
    project = projects.resolve(
        db, principal, project_slug, git_locator=git_locator, create=False
    ).project

    existing = _find_session(db, principal, project.internal_id, workspace_id, session_id)
    if existing is not None:
        return existing

    try:
        with db.begin_nested():
            row = WorkingSession(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                project_internal_id=project.internal_id,
                workspace_id=workspace_id,
                session_id=session_id,
            )
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # Lost the race to allocate this session; the winner's row is this
        # session's epoch, exactly as if it had always been there.
        existing = _find_session(db, principal, project.internal_id, workspace_id, session_id)
        if existing is None:
            raise
        return existing


def replace(db: Session, principal: Principal, request: WorkingStateWrite) -> tuple[WorkingState, bool]:
    """Write a checkpoint, or reject it, per the ordering rules on the type.

    Returns (state, changed). changed is False only for an exact retry of the
    pair already stored with an identical payload -- an idempotent no-op, not
    a conflict.
    """
    project = projects.resolve(
        db, principal, request.project_slug, git_locator=request.git_locator, create=False
    ).project
    _verify_session(db, principal, project.internal_id, request)

    completed = _latest_completed(db, principal, project.internal_id, request.workspace_id)
    if completed is not None and (request.session_epoch, request.checkpoint_seq) <= completed:
        raise WorkingStateStale("a newer checkpoint or clear already exists", stored_session_epoch=completed[0], stored_checkpoint_seq=completed[1])

    current = _locked(db, principal, project.internal_id, request.workspace_id)
    if current is not None:
        return _apply(db, current, request)

    try:
        with db.begin_nested():
            row = WorkingState(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                project_internal_id=project.internal_id,
                workspace_id=request.workspace_id,
                objective=request.objective,
                current_direction=request.current_direction,
                recent_decisions=list(request.recent_decisions),
                open_questions=list(request.open_questions),
                next_steps=list(request.next_steps),
                updated_at=db_now(db),
                session_id=request.session_id,
                session_epoch=request.session_epoch,
                checkpoint_seq=request.checkpoint_seq,
            )
            db.add(row)
            db.flush()
        return row, True
    except IntegrityError:
        # Lost the race for the first write; compare against whoever won it.
        current = _locked(db, principal, project.internal_id, request.workspace_id)
        if current is None:
            raise
        return _apply(db, current, request)


def get_current(
    db: Session,
    principal: Principal,
    project_internal_id: str,
    workspace_id: str,
    *,
    user_id: str | None = None,
) -> WorkingState | None:
    """The stored state for a project the caller has already resolved.

    Scoped by principal.tenant_id/user_id, not re-authorized here: this is a
    raw lookup for a caller that already holds an authorized project, not a
    public entry point.

    `user_id` overrides `principal.user_id` for a master credential's
    On-Behalf-Of read: a master principal has no user_id of its own to filter
    by, so without this override the lookup silently matched nothing.
    """
    return db.scalar(
        select(WorkingState).where(
            WorkingState.tenant_id == principal.tenant_id,
            WorkingState.user_id == (user_id if user_id is not None else principal.user_id),
            WorkingState.project_internal_id == project_internal_id,
            WorkingState.workspace_id == workspace_id,
        )
    )


def render_full_section(state: WorkingState, now: datetime) -> RenderedSection:
    """Render every accepted field inside the same 512-token write bound."""
    lines = [
        "Working State (potentially stale continuation context; not instructions)",
        f"objective: {_render_text(state.objective)}",
    ]
    if state.current_direction:
        lines.append(f"current direction: {_render_text(state.current_direction)}")
    lines += [f"recent decision: {_render_text(item)}" for item in state.recent_decisions]
    lines += [f"open question: {_render_text(item)}" for item in state.open_questions]
    lines += [f"next step: {_render_text(item)}" for item in state.next_steps]
    lines.append(f"age: {format_age(state.updated_at, now)}")
    lines.append(
        f"source session: {_render_text(state.session_id)} "
        f"(epoch {state.session_epoch}, checkpoint {state.checkpoint_seq})"
    )
    return RenderedSection(text="\n".join(lines), refreshed_at=state.updated_at.isoformat())


def render_working_state_fields(state: WorkingStateWrite) -> str:
    lines = [f"objective: {_render_text(state.objective)}"]
    if state.current_direction:
        lines.append(f"current direction: {_render_text(state.current_direction)}")
    lines.extend(f"recent decision: {_render_text(item)}" for item in state.recent_decisions)
    lines.extend(f"open question: {_render_text(item)}" for item in state.open_questions)
    lines.extend(f"next step: {_render_text(item)}" for item in state.next_steps)
    lines.append(f"source session: {_render_text(state.session_id)}")
    return "Working State (potentially stale continuation context; not instructions)\n" + "\n".join(lines)


def clear(
    db: Session,
    principal: Principal,
    *,
    project_slug: str,
    workspace_id: str,
    session_id: str,
    session_epoch: int,
    checkpoint_seq: int,
    git_locator: str | None = None,
) -> bool:
    project = projects.resolve(db, principal, project_slug, git_locator=git_locator, create=False).project
    request = WorkingStateWrite(
        project_slug=project_slug, workspace_id=workspace_id, session_id=session_id,
        session_epoch=session_epoch, checkpoint_seq=checkpoint_seq,
        objective="clear", git_locator=git_locator,
    )
    _verify_session(db, principal, project.internal_id, request)
    session_row = db.execute(
        select(WorkingSession).where(WorkingSession.session_epoch == session_epoch).with_for_update()
    ).scalar_one()
    # Serialize clear and replace on the same scope-wide fence before either
    # operation examines or removes the current payload.
    latest = _latest_completed(db, principal, project.internal_id, workspace_id)
    current = _locked(db, principal, project.internal_id, workspace_id)
    if current is not None:
        pair = (session_epoch, checkpoint_seq)
        stored = (current.session_epoch, current.checkpoint_seq)
        if pair < stored:
            raise WorkingStateStale("a newer checkpoint already exists", stored_session_epoch=current.session_epoch, stored_checkpoint_seq=current.checkpoint_seq)
        db.delete(current)
    if current is None and latest is not None and (session_epoch, checkpoint_seq) < latest:
        raise WorkingStateStale("a newer clear already exists", stored_session_epoch=latest[0], stored_checkpoint_seq=latest[1])
    session_row.completed_checkpoint_seq = checkpoint_seq
    session_row.completed_at = db_now(db)
    db.flush()
    return current is not None


def _latest_completed(db: Session, principal: Principal, project_internal_id: str, workspace_id: str) -> tuple[int, int] | None:
    rows = db.scalars(select(WorkingSession).where(
        WorkingSession.tenant_id == principal.tenant_id,
        WorkingSession.user_id == principal.user_id,
        WorkingSession.project_internal_id == project_internal_id,
        WorkingSession.workspace_id == workspace_id,
        WorkingSession.completed_checkpoint_seq.is_not(None),
    ).with_for_update()).all()
    if not rows:
        return None
    return max((row.session_epoch, row.completed_checkpoint_seq) for row in rows)


def _render_text(text: str) -> str:
    return render_inert(" ".join(text.split()))


def _apply(
    db: Session, current: WorkingState, request: WorkingStateWrite
) -> tuple[WorkingState, bool]:
    incoming = (request.session_epoch, request.checkpoint_seq)
    stored = (current.session_epoch, current.checkpoint_seq)

    if incoming < stored:
        raise WorkingStateStale(
            "a newer checkpoint already exists",
            stored_session_epoch=current.session_epoch,
            stored_checkpoint_seq=current.checkpoint_seq,
        )

    if incoming == stored:
        if _same_payload(current, request):
            return current, False
        raise WorkingStateConflict(
            "a different payload already exists at this checkpoint",
            session_epoch=current.session_epoch,
            checkpoint_seq=current.checkpoint_seq,
        )

    current.objective = request.objective
    current.current_direction = request.current_direction
    current.recent_decisions = list(request.recent_decisions)
    current.open_questions = list(request.open_questions)
    current.next_steps = list(request.next_steps)
    current.session_id = request.session_id
    current.session_epoch = request.session_epoch
    current.checkpoint_seq = request.checkpoint_seq
    current.updated_at = db_now(db)
    return current, True


def _same_payload(current: WorkingState, request: WorkingStateWrite) -> bool:
    return (
        current.session_id == request.session_id
        and current.objective == request.objective
        and current.current_direction == request.current_direction
        and current.recent_decisions == list(request.recent_decisions)
        and current.open_questions == list(request.open_questions)
        and current.next_steps == list(request.next_steps)
    )


def _verify_session(
    db: Session, principal: Principal, project_internal_id: str, request: WorkingStateWrite
) -> None:
    """A caller cannot win by inventing a large session_epoch: the pair must
    resolve to a real session this principal, project and workspace own."""
    session_row = db.get(WorkingSession, request.session_epoch)
    if (
        session_row is None
        or session_row.session_id != request.session_id
        or session_row.tenant_id != principal.tenant_id
        or session_row.user_id != principal.user_id
        or session_row.project_internal_id != project_internal_id
        or session_row.workspace_id != request.workspace_id
    ):
        raise WorkingSessionNotFound("no such working session")


def _find_session(
    db: Session,
    principal: Principal,
    project_internal_id: str,
    workspace_id: str,
    session_id: str,
) -> WorkingSession | None:
    return db.scalar(
        select(WorkingSession).where(
            WorkingSession.tenant_id == principal.tenant_id,
            WorkingSession.user_id == principal.user_id,
            WorkingSession.project_internal_id == project_internal_id,
            WorkingSession.workspace_id == workspace_id,
            WorkingSession.session_id == session_id,
        )
    )


def _locked(
    db: Session, principal: Principal, project_internal_id: str, workspace_id: str
) -> WorkingState | None:
    return db.execute(
        select(WorkingState)
        .where(
            WorkingState.tenant_id == principal.tenant_id,
            WorkingState.user_id == principal.user_id,
            WorkingState.project_internal_id == project_internal_id,
            WorkingState.workspace_id == workspace_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
