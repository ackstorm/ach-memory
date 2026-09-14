"""Bounded standing context assembled from built-in mental models (SPEC §2, §6)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import resolve_bank
from memory.builtin_models import BUILTIN_MODELS
from memory.config import get_settings
from memory.errors import ProjectNotFound, UnsupportedCapability


class LoadContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = None


class ContextEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"]
    key: str
    name: str
    chars: int


class LoadContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    entries: list[ContextEntry]
    omitted: list[str]


def load(
    db: Session, principal: Principal, request: LoadContextRequest, *, backend: Backend
) -> LoadContextResponse:
    """User bank always; project bank too when `project_slug` names a known project (an
    unknown one is silently omitted). Whole entries only, user then project: one that would
    exceed `context_budget_chars` is dropped into `omitted`, never truncated."""
    refs = [resolve_bank(db, principal, "user", None, create=False)[0]]
    if request.project_slug:
        try:
            refs.append(resolve_bank(db, principal, "project", request.project_slug, create=False)[0])
        except ProjectNotFound:
            pass

    budget = get_settings().context_budget_chars
    sections: list[str] = []
    entries: list[ContextEntry] = []
    omitted: list[str] = []
    used = 0
    try:
        for ref in refs:
            for builtin in BUILTIN_MODELS:
                if builtin.scope != ref.scope:
                    continue
                view = backend.get_mental_model(ref.bank_id, builtin.key)
                if view is None or not view.content:
                    continue
                section = f"### {view.name}\n{view.content}\n\n"
                if used + len(section) > budget:
                    omitted.append(builtin.key)
                    continue
                sections.append(section)
                entries.append(
                    ContextEntry(scope=ref.scope, key=builtin.key, name=view.name, chars=len(section))
                )
                used += len(section)
    except UnsupportedCapability:
        return LoadContextResponse(text="", entries=[], omitted=[])

    return LoadContextResponse(text="".join(sections), entries=entries, omitted=omitted)
