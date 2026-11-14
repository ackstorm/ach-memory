"""Standing context for a session start (SPEC §2, §6), in this order: the project's purpose, the
user's pinned memories in full, the project's pinned memories in full, an index of the project's
memories, and one line of totals. Without a project, the user's pinned memories alone.
Everything but the purpose and the pinned text comes from ACH's own record, not the engine."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from memory import catalog
from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import LogicalBankRef, resolve_bank
from memory.config import get_settings
from memory.errors import ProjectNotFound
from memory.families import OTHER, THRESHOLD
from memory.models import Memory

PURPOSE_TAG = "purpose"
TITLES_PER_FAMILY = 3

_NO_PURPOSE = (
    "No purpose is stored for this project. Read its README or AGENTS.md and retain one project "
    "`fact` tagged `purpose`, titled \"Project purpose\": two or three sentences on what it is and "
    "does, who uses it, its main parts and where it runs.\n\n"
)
_NOTHING_PINNED = (
    "Nothing is pinned in this project. Pin only what every session here needs before doing "
    "anything.\n\n"
)
_INDEX_HEAD = (
    "### Memory index\nWhat this project's memory holds, by family: count, then the newest titles. "
    "`list_memories` with `family` lists one; `recall` searches them all.\n"
)
_RECENT_HEAD = (
    "### Memory index\nThe titles this project's memory holds, newest first. `recall` searches "
    "them; `list_memories` with `full` reads them.\n"
)


class LoadContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = None


class LoadContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    truncated: bool = False
    #: Pinned memories delivered plus index lines kept: the read log's result count.
    shown: int = 0


def load(
    db: Session, principal: Principal, request: LoadContextRequest, *, backend: Backend
) -> LoadContextResponse:
    """Creates nothing. The engine is read for the purpose and the pinned texts only."""
    user_ref, _ = resolve_bank(db, principal, "user", None, create=False)
    project_ref = None
    if request.project_slug:
        try:
            project_ref, _ = resolve_bank(db, principal, "project", request.project_slug, create=False)
        except ProjectNotFound:
            pass

    def text_of(ref: LogicalBankRef):
        return lambda row: (view.text if (view := backend.get(ref.bank_id, row.memory_id)) else None)

    user = Bank(catalog.valid(db, user_ref.owner_id), catalog.pin_cap("user"), text_of(user_ref))
    if project_ref is None:
        return render(user=user)
    project = Bank(catalog.valid(db, project_ref.owner_id), catalog.pin_cap("project"), text_of(project_ref))
    return render(user=user, project=project, purpose=_purpose(backend, project_ref.bank_id))


@dataclass
class Bank:
    """What `render` needs of one bank: its valid memories newest first, its pin cap, and a way
    to read a pinned memory's text."""
    rows: list[Memory]
    cap: int
    text_of: Callable[[Memory], str | None]


def render(*, user: Bank, project: Bank | None = None, purpose: str | None = None,
           budget: int | None = None) -> LoadContextResponse:
    """The session start. Kept under `budget` (default `context_budget_chars`): pinned text stops
    at each bank's cap, newest first, and index lines go from the bottom when the whole would not
    fit. `purpose` is the purpose memory's text, None when there is none."""
    budget = budget or get_settings().context_budget_chars
    user_pins, user_shown, user_cut = _pinned(user, "Pinned by you")
    if project is None:
        if not user.rows:
            return LoadContextResponse(text="")
        totals = f"{len(user.rows)} memories of yours, {sum(r.pinned for r in user.rows)} pinned.\n"
        return LoadContextResponse(text=user_pins + totals, truncated=user_cut, shown=user_shown)

    project_pins, project_shown, project_cut = _pinned(project, "Pinned in this project")
    rows = project.rows
    head = (f"### Project purpose\n{purpose}\n\n" if purpose else _NO_PURPOSE) + user_pins + (
        project_pins or _NOTHING_PINNED)
    totals = (
        f"{len(rows)} project memories"
        + (f" in {len({r.family or OTHER for r in rows})} families" if len(rows) >= THRESHOLD else "")
        + f", {sum(r.pinned for r in rows)} pinned; {len(user.rows)} of yours, "
        f"{sum(r.pinned for r in user.rows)} pinned.\n"
    )
    room = budget - len(head) - len(totals) - 80
    index, cut = _index(rows, room)
    text = head + index + totals
    if len(text) > budget:
        text = text[: text.rfind("\n", 0, budget) + 1]
    return LoadContextResponse(
        text=text, truncated=user_cut or project_cut or cut,
        shown=user_shown + project_shown + index.count("\n- "),
    )


def _purpose(backend: Backend, bank_id: str) -> str | None:
    page = backend.list(bank_id, tag_groups=({"tags": [PURPOSE_TAG], "match": "any"},),
                        state=None, limit=1, offset=0)
    return page.items[0].text if page.items else None


def _pinned(bank: Bank, heading: str) -> tuple[str, int, bool]:
    """Pinned memories in full, newest first, until the bank's cap; says how many were left out."""
    rows = [row for row in bank.rows if row.pinned]
    lines, used = [], 0
    for row in rows:
        if used + row.chars > bank.cap:
            break
        text = bank.text_of(row)
        if text is None:
            continue
        lines.append(f"- **{row.title or row.memory_id}**: {text}\n")
        used += row.chars
    left = len(rows) - len(lines)
    if not lines and not left:
        return "", 0, False
    note = (f"{left} more pinned memories are over the {bank.cap}-char cap and left out: "
            "`list_memories` with `pinned` true lists them.\n") if left else ""
    return f"### {heading}\n" + "".join(lines) + note + "\n", len(lines), bool(left)


def _index(rows: list[Memory], room: int) -> tuple[str, bool]:
    """The index section within `room` chars, and whether anything was cut. Below THRESHOLD
    memories, the flat list of titles, cut from the bottom. From there, one line per family with
    a memory, its count and its newest unpinned titles (pinned ones are printed above): every
    family line stays, and the sample titles go first, 3 per line, then 2, 1, none."""
    if not rows:
        return "", False
    if len(rows) < THRESHOLD:
        lines = [f"- {row.title}\n" for row in rows if row.title]
        kept = list(lines)
        while kept and len(_RECENT_HEAD) + sum(map(len, kept)) > room:
            kept.pop()
        more = f"... {len(lines) - len(kept)} more: `list_memories` lists them.\n" if len(kept) < len(lines) else ""
        return _RECENT_HEAD + "".join(kept) + more + "\n", len(kept) < len(lines)
    counts = Counter(row.family or OTHER for row in rows)
    order = sorted(counts, key=lambda f: (f == OTHER, -counts[f], f))
    samples = {f: [r.title for r in rows if (r.family or OTHER) == f and r.title and not r.pinned] for f in order}
    for n in range(TITLES_PER_FAMILY, -1, -1):
        lines = [f"- {f} ({counts[f]})" + (": " + "; ".join(samples[f][:n]) if n and samples[f] else "") + "\n"
                 for f in order]
        text = _INDEX_HEAD + "".join(lines) + "\n"
        if len(text) <= room:
            break
    return text, n < TITLES_PER_FAMILY
