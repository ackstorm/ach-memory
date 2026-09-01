"""Shapes shared by the capture pipeline's stages: the checkpoint submission
body and its acceptance response (Task 1/2), and the extractor's envelope
and normalized-candidate shapes (Task 3, SPEC Phase 3 §7-§10).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memory.contracts import SessionId, WorkingStateLine, WorkingStateLines, WorkspaceId

_HEX64 = r"^[0-9a-f]{64}$"

CaptureStatus = Literal[
    "pending", "extracting", "retaining", "applying", "completed", "failed"
]

CandidateKind = Literal["preference", "decision", "convention", "gotcha", "technical_claim"]
CandidateOrigin = Literal["stated", "confirmed", "observed", "inferred"]
CandidateSubject = Literal["user", "project"]
Eligibility = Literal["profile_eligible", "evidence_only"]

# WorkingStateLine already bounds length (1-512), rejects blank text, and
# rejects control characters -- which includes \n and \r, so a candidate is
# already one line with no extra check needed: a newline inside it could
# otherwise forge a second, structurally-unvalidated line.
CandidateText = WorkingStateLine


class CheckpointSubmission(BaseModel):
    """The exact wire body for `POST /v1/capture/checkpoints`. Built once
    the local client has already sanitized the slice -- nothing on this
    model is raw transcript content.

    This is the ONE definition of that body: `memory.api.capture` accepts
    this same model, so a field the local client leaves optional cannot
    differ from what the route requires. The Phase 3 review found the two
    had drifted -- the shipped hook omitted `project_slug` and every 422 it
    earned was invisible, because the hook swallows failures by design and
    the delivery gate hand-repaired the body before replaying it.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=32)
    session_id: SessionId
    project_slug: str
    git_locator: str | None = None
    workspace_id: WorkspaceId
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    content_hash: str = Field(pattern=_HEX64)
    sanitized_hash: str = Field(pattern=_HEX64)
    content: str

    @model_validator(mode="after")
    def _end_after_start(self) -> "CheckpointSubmission":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class CheckpointAccepted(BaseModel):
    """The `202` acceptance response. `extra="ignore"`: the server may add
    fields the local client has no use for, and a forward-compatible client
    should not break on them."""

    model_config = ConfigDict(extra="ignore")

    capture_id: str
    status: CaptureStatus
    duplicate: bool
    session_epoch: int | None = None
    checkpoint_seq: int
    project_slug: str | None = None
    resolved_from: str | None = None


# --------------------------------------------------------------------------
# Extractor envelopes (SPEC Phase 3 §7-§10). Each `ExtractedFact.text` from
# `dry-run-extract` must be one minified JSON object matching one of these
# two discriminated shapes.
# --------------------------------------------------------------------------


class Provenance(BaseModel):
    """The one artifact anchor an `observed` claim needs to stay `observed`
    instead of downgrading to `inferred` (harness rule #3)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["transcript"]
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _end_after_start(self) -> "Provenance":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class CandidateEnvelope(BaseModel):
    """What the model may propose for one claim. It may not set bank,
    profile_eligible, eligibility tags/scopes, operation IDs or document IDs
    -- `extra="forbid"` makes any of those a validation failure rather than
    a value classify() would have to notice and discard.
    """

    model_config = ConfigDict(extra="forbid")

    record: Literal["candidate"]
    text: CandidateText
    kind: CandidateKind
    origin: CandidateOrigin
    subject: CandidateSubject
    provenance: Provenance | None = None
    negative: bool = False
    correction: bool = False
    # gotcha-only: harness rule #4 requires failure plus cause and/or
    # reproduction to keep `kind="gotcha"`, else it downgrades to
    # `technical_claim`. Meaningless, and left unset, for every other kind.
    failure: CandidateText | None = None
    cause: CandidateText | None = None
    reproduction: CandidateText | None = None


class WorkingStateEnvelope(BaseModel):
    """The Working State half of the envelope union. Routed before candidate
    precedence (harness rule #2): this shape carries no kind/origin/subject
    and never touches eligibility at all -- a plan or next step is Working
    State or noise, never a confirmed durable decision.
    """

    model_config = ConfigDict(extra="forbid")

    record: Literal["working_state"]
    objective: WorkingStateLine
    current_direction: WorkingStateLine | None = None
    recent_decisions: WorkingStateLines = Field(default_factory=list)
    open_questions: WorkingStateLines = Field(default_factory=list)
    next_steps: WorkingStateLines = Field(default_factory=list)


class NormalizedCandidate(BaseModel):
    """The classifier's output: only resolved bank_kind, derived eligibility,
    exact tags/scopes, validated provenance and correction scope survive --
    never a bank ID, and never anything the model supplied directly for any
    of those fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    kind: CandidateKind
    origin: CandidateOrigin
    bank_kind: CandidateSubject
    eligible: Eligibility
    tags: list[str]
    observation_scopes: list[list[str]]
    negative: bool
    correction: bool
    correction_scope: tuple[CandidateSubject, Eligibility] | None
    provenance: Provenance | None
