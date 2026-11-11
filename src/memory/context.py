"""Standing context for a session start (SPEC §2, §6): the project's purpose, its briefing, and
the on-demand models an agent loads with `load_context(model=...)`."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import resolve_bank
from memory.builtin_models import BRIEFING, BUILTIN_MODELS
from memory.config import get_settings
from memory.errors import InvalidRequest, ProjectNotFound, UnsupportedCapability

PURPOSE_TAG = "purpose"
ON_DEMAND = tuple(m for m in BUILTIN_MODELS if m.load_when)

_NO_PURPOSE = (
    "No purpose is stored for this project. Read its README or AGENTS.md and retain one project "
    "`fact` tagged `purpose`: two or three sentences on what it is and does, who uses it, its "
    "main parts and where it runs.\n\n"
)
_EMPTY_BRIEFING = (
    "The briefing is empty. When you learn something every session in this project needs before "
    "doing anything, retain it with `memory_type` `briefing`.\n\n"
)
_BRIEFING_HEAD = (
    "### Briefing\nNotes agents saved to know at the start of every session in this project. "
    "Read them before doing anything.\n"
)
_BRIEFING_CUT = (
    "The briefing continues: these are notes saved to know before doing anything in this "
    "project. You MUST call `load_context` with `model` \"briefing\" to read the rest.\n\n"
)


class LoadContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = None
    model: str | None = None


class LoadContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    truncated: bool = False


def load(
    db: Session, principal: Principal, request: LoadContextRequest, *, backend: Backend
) -> LoadContextResponse:
    """Project bank only; no project, or an unknown one, loads nothing. With `model`, that one
    model in full. Otherwise purpose, briefing and index within `context_budget_chars`: the
    briefing is cut at a whole line, never mid-sentence, and says how to load the rest."""
    if request.model and not request.project_slug:
        raise InvalidRequest("models belong to a project: pass project_slug")
    if not request.project_slug:
        return LoadContextResponse(text="")
    try:
        ref, _ = resolve_bank(db, principal, "project", request.project_slug, create=False)
    except ProjectNotFound:
        if request.model:
            raise
        return LoadContextResponse(text="")
    if request.model:
        return LoadContextResponse(text=_model(backend, ref.bank_id, request.model))
    head, tail = _purpose(backend, ref.bank_id), _index()
    body, truncated = _briefing(
        backend, ref.bank_id, get_settings().context_budget_chars - len(head) - len(tail)
    )
    return LoadContextResponse(text=head + body + tail, truncated=truncated)


def _model(backend: Backend, bank_id: str, key: str) -> str:
    builtin = next((m for m in BUILTIN_MODELS if m.key == key), None)
    if builtin is None:
        raise InvalidRequest(f"unknown model; use one of: {', '.join(m.key for m in BUILTIN_MODELS)}")
    view = backend.get_mental_model(bank_id, key)
    return f"### {builtin.name}\n{view.content.strip()}\n" if view and view.content else ""


def _purpose(backend: Backend, bank_id: str) -> str:
    page = backend.list(bank_id, tag_groups=({"tags": [PURPOSE_TAG], "match": "any"},),
                        state=None, limit=1, offset=0)
    if not page.items:
        return _NO_PURPOSE
    return f"### Project purpose\n{page.items[0].text}\n\n"


def _briefing(backend: Backend, bank_id: str, budget: int) -> tuple[str, bool]:
    notes = backend.list(bank_id, tag_groups=({"tags": ["type:briefing"], "match": "any"},),
                         state=None, limit=1, offset=0)
    if not notes.total:
        return _EMPTY_BRIEFING, False
    try:
        view = backend.get_mental_model(bank_id, BRIEFING.key)
    except UnsupportedCapability:
        return "", False
    if view is None or not view.content:
        return "", False
    text = f"{_BRIEFING_HEAD}{view.content.strip()}\n\n"
    if len(text) <= budget:
        return text, False
    cut = text.rfind("\n", 0, budget - len(_BRIEFING_CUT))
    return text[:cut + 1] + _BRIEFING_CUT, True


def _index() -> str:
    lines = "".join(f"- `{m.key}`: {m.load_when}\n" for m in ON_DEMAND)
    return f"### More context\nCall `load_context` with `model` set to one of these when it applies:\n{lines}"
