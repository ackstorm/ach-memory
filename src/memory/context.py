"""Bounded standing context: stored rules verbatim, then built-in mental models (SPEC §2, §6)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import resolve_bank
from memory.builtin_models import BUILTIN_MODELS
from memory.config import get_settings
from memory.errors import ProjectNotFound, UnsupportedCapability

# Delivery order. Rules skip the LLM: a 1,000-token summary kept 1-4 of 19 constraints on
# Hindsight 0.9.2, and the constraints alone do not fit in one.
_RULES = (
    ("project", "constraint", "Project Constraints"),
    ("user", "constraint", "User Constraints"),
    ("project", "convention", "Project Conventions"),
)
# ponytail: one page, since ~30 claims fill the budget; `Page.total` still counts the rest.
_RULES_PAGE = 100


def _left_out_line(rules: int, models: list[str]) -> str:
    items = [f"{rules} stored rule{'' if rules == 1 else 's'}"] if rules else []
    return f"Left out for size: {', '.join(items + models)}. Use `recall`.\n\n"


_LEFT_OUT_ROOM = len(_left_out_line(999, [m.name for m in BUILTIN_MODELS]))


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
    unknown one is silently omitted). Rules come first, newest first, then models, user before
    project. Whole items only: one that would exceed `context_budget_chars` goes to `omitted`,
    never truncated, and a closing line in the text names what was left out."""
    refs = [resolve_bank(db, principal, "user", None, create=False)[0]]
    if request.project_slug:
        try:
            refs.append(resolve_bank(db, principal, "project", request.project_slug, create=False)[0])
        except ProjectNotFound:
            pass

    budget = get_settings().context_budget_chars - _LEFT_OUT_ROOM
    sections: list[str] = []
    entries: list[ContextEntry] = []
    omitted: list[str] = []
    used = 0

    banks = {ref.scope: ref.bank_id for ref in refs}
    rules_left_out = 0
    models_left_out: list[str] = []
    for scope, memory_type, heading in _RULES:
        if scope not in banks:
            continue
        page = backend.list(banks[scope], tag_groups=({"tags": [f"type:{memory_type}"], "match": "any"},),
                            state=None, limit=_RULES_PAGE, offset=0)
        rules_left_out += page.total - len(page.items)
        section = f"### {heading}\n"
        kept = False
        for view in page.items:
            line = f"- {view.text}\n"
            if used + len(section) + len(line) + 1 > budget:
                rules_left_out += 1
                omitted.append(view.memory_id)
                continue
            section += line
            kept = True
        if kept:
            section += "\n"
            sections.append(section)
            entries.append(ContextEntry(scope=scope, key=memory_type, name=heading, chars=len(section)))
            used += len(section)

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
                    models_left_out.append(view.name)
                    continue
                sections.append(section)
                entries.append(
                    ContextEntry(scope=ref.scope, key=builtin.key, name=view.name, chars=len(section))
                )
                used += len(section)
    except UnsupportedCapability:
        pass

    if rules_left_out or models_left_out:
        sections.append(_left_out_line(rules_left_out, models_left_out))
    return LoadContextResponse(text="".join(sections), entries=entries, omitted=omitted)
