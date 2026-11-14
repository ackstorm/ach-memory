"""A project's closed family list (SPEC §2): drafted once when the project first holds
THRESHOLD valid memories, then required on every project retain; editable by the owner."""

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import audit, catalog, llm
from memory.auth.principal import Principal
from memory.config import get_settings
from memory.errors import InvalidRequest
from memory.models import Memory, Project

logger = logging.getLogger("memory.families")

THRESHOLD = 30
MAX_FAMILIES = 12
OTHER = "other"

PROMPT = """Below are the {n} memories of a software project's shared memory, one per line as "number. [type] title".
Group them into at most 12 families: the broad areas of this project an agent would browse by, named as the project names them. Each family name is lowercase, words joined by hyphens, at most 3 words. Every family should hold several memories; do not make a family for one or two. Do not include "other": anything that fits no family goes there.
Then assign every memory number to one family, or to "other".
Answer with JSON only: {{"families": ["..."], "assign": {{"1": "family", "2": "other", ...}}}}

Memories:
{memories}"""


def normalize(name: str) -> str:
    family = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not family or len(family) > 48:
        raise InvalidRequest("a family is 1 to 48 lowercase letters, digits and hyphens")
    return family


def clean(names: list[str]) -> list[str]:
    """Normalized, deduplicated, at most MAX_FAMILIES, with OTHER last."""
    out: list[str] = []
    for name in names:
        family = normalize(name)
        if family != OTHER and family not in out:
            out.append(family)
    if len(out) > MAX_FAMILIES:
        raise InvalidRequest(f"at most {MAX_FAMILIES} families plus {OTHER}")
    return out + [OTHER]


def resolve(families: list[str] | None, family: str | None) -> str | None:
    """The family a project retain stores: optional until the project has a list, then required
    and checked against it. The error carries the list so the agent can pick from it."""
    if families is None:
        return normalize(family) if family else None
    choices = ", ".join(families)
    if not family:
        raise InvalidRequest(f"family is required in this project; use one of: {choices}")
    if (normalized := re.sub(r"[^a-z0-9]+", "-", family.strip().lower()).strip("-")) not in families:
        raise InvalidRequest(f"unknown family {family!r}; use one of: {choices}")
    return normalized


def propose(lines: list[str]) -> tuple[list[str], list[str]]:
    """A family list for these memory lines and one family per line, from the configured LLM."""
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    answer = llm.complete_json(get_settings(), PROMPT.format(n=len(lines), memories=numbered))
    names = []
    for name in answer.get("families") or []:
        try:
            names.append(normalize(str(name)))
        except InvalidRequest:
            continue
    families = clean(list(dict.fromkeys(n for n in names if n != OTHER))[:MAX_FAMILIES])
    assign = answer.get("assign") or {}
    picked = []
    for i in range(1, len(lines) + 1):
        raw = str(assign.get(str(i)) or "")
        family = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
        picked.append(family if family in families else OTHER)
    return families, picked


def maybe_draft(db: Session, principal: Principal, project_id: str) -> None:
    """Draft the list the first time the project holds THRESHOLD valid memories, filing every
    memory into it. Needs MEMORY_LLM_URL; a failure leaves the project without a list, and the
    next retain tries again."""
    if not llm.configured(get_settings()):
        return
    if catalog.count_valid(db, project_id) < THRESHOLD:
        return
    project = db.get(Project, project_id, with_for_update=True)
    if project is None or project.families is not None:
        return
    rows = catalog.valid(db, project_id)
    try:
        families, picked = propose([f"[{row.memory_type}] {row.title or ''}" for row in rows])
    except Exception as exc:  # noqa: BLE001 -- a failed draft must not fail the retain
        logger.warning("family list draft failed: %s", type(exc).__name__)
        return
    for row, family in zip(rows, picked, strict=True):
        if row.family not in families:
            row.family = family
    project.families = families
    audit.record(db, principal=principal, action="project.families", resource=project_id,
                 details={"before": None, "after": families, "drafted": True})


def replace(db: Session, principal: Principal, project: Project, names: list[str],
            rename: dict[str, str]) -> list[str]:
    """Set the list. `rename` moves memories from an old family to a new one first; a memory
    whose family leaves the list goes to OTHER. Caller has authorized and commits."""
    families = clean(names)
    moves = {normalize(old): normalize(new) for old, new in rename.items()}
    if unknown := sorted(set(moves.values()) - set(families)):
        raise InvalidRequest(f"rename targets not in the new list: {', '.join(unknown)}")
    for row in db.scalars(select(Memory).where(Memory.owner_id == project.internal_id)):
        if row.family is None:
            continue
        family = moves.get(row.family, row.family)
        row.family = family if family in families else OTHER
    before, project.families = project.families, families
    audit.record(db, principal=principal, action="project.families", resource=project.internal_id,
                 details={"before": before, "after": families, "rename": moves})
    return families
