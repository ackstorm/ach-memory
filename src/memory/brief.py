"""The session brief: what memory already knows, composed for a session start.

Reading a mental model is a SELECT; the LLM cost is paid by its refresh. That
is what makes this affordable to send on every connect.

Every rule in here is about a digest being WRONG rather than missing. Measured
while designing this (2026-08-27): a loose query over this user's own memories
inverted one of his rules -- "after each change run a full test gate" where the
stored fact forbids exactly that -- and invented a role and a toolchain for him
that no memory contains. A missing section costs a session some context. A
confidently wrong one steers the work.

Reads never write. `get_section` only reads; `provision_section` is the only
thing here that creates or patches a model, and nothing on the GET path calls
it. The cost is that a deploy changing USER_QUERY, PROJECT_QUERY or TRIGGER no
longer repairs live models by itself -- somebody has to call
`POST /v1/admin/brief/{scope}/provision`. That is worth paying: repairing on
read is the same mechanism that spent an LLM generation minting a model on a
bank one exploratory GET happened to name.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

BRIEF_MODEL_NAME = "ach-memory-session-brief"
MAX_TOKENS = 400
# keep_trace is what makes a failed refresh visible. Without it a refresh that
# fails keeps serving the previous document and sets nothing -- measured
# upstream, four unrelated mental models went six days without refreshing while
# /health stayed green. Hindsight documents the flag as "the only way to
# diagnose a cron- or consolidation-driven refresh after the fact, since no
# human sees those run", which is this failure exactly. Only the latest
# refresh's trace is kept, so it answers "why did the last one do that", never
# "what has been failing all week".
TRIGGER = {"mode": "delta", "refresh_cron": "0 3 * * *", "keep_trace": True}
# Stale AND older than this means refreshes are failing, not that the user
# went quiet.
STALE_AFTER = timedelta(days=7)
PLACEHOLDER = "Generating content..."

# Every clause below was earned against real memories. The anti-inference
# sentence removed an invented biography; the formatting rules took the output
# from 3364 characters to 1438 and un-inverted a rule. Change them with
# evidence, not taste.
USER_QUERY = (
    "List only the standing instructions this user has explicitly stated "
    "about how an agent must work with them: process rules, communication and "
    "language, and tools they named. Write each as one short imperative line "
    "an agent can follow. State only what the memories say. Do not infer "
    "their role, employer, seniority or any tooling they did not name. Omit "
    "colour, styling and theme preferences entirely. No headings, no tables, "
    "no summary paragraph."
)
PROJECT_QUERY = (
    "List only what has been learned about working in this codebase: "
    "conventions that are followed, constraints that hold, and gotchas "
    "together with their cause. Write each as one short line an agent can act "
    "on. State only what the memories say. Do not infer the project's "
    "purpose, architecture or technology from its name, and do not describe "
    "what the repository's files would already show. No headings, no tables, "
    "no summary paragraph."
)

_CAVEAT = (
    "(orientation, generated from stored facts -- verify with recall before "
    "acting on it)"
)

# Bumped when the shape of a tier changes in a way a consumer must notice.
# A cached tier keeps the protocol it was compiled under, so a brief holding
# instructions the current contract has replaced is visible instead of silent.
MEMORY_PROTOCOL = 1

# Per host, as the client names itself. Claude Code truncates MCP
# `instructions` at 2048 characters -- measured against ours: the composed
# brief was 6115 characters, 626 arrived, and the project half was discarded
# every session. 1800 leaves headroom for a host that counts characters
# differently than we do (tokens, UTF-16 units) before it cuts.
HOST_BUDGETS = {"claude-code": 1800}
# An unknown host gets the smallest known budget rather than the benefit of
# the doubt: the overflow is silent, and what it drops is the end of the brief.
SMALLEST_BUDGET = 1800

# Reserved: never dropped to make room, in either tier. An agent cannot call
# what it does not know exists -- and must not be told to call what it cannot,
# so `recall` is named with the confirmation its host will ask for, while the
# profiles are named as things memory holds and not as a call to fetch them.
INDEX_SECTION = (
    "-- What else memory holds --\n"
    "user profile: this user's standing working preferences.\n"
    "project profile: this project's conventions, constraints and gotchas.\n"
    "facts and observations: recall(scope, query) -- your host will ask you to "
    "confirm the call.\n"
)

_ORIENTATION_HEADING = "-- This project --"
_USER_HEADING = "-- What memory knows about you --"
_PROJECT_HEADING = "-- What memory knows about this project --"
_WORKING_STATE_HEADING = "-- Where the work was left --"


@dataclass(frozen=True)
class Section:
    """A digest the endpoint is willing to show, with the freshness it can
    report. The mental model is out of reach by the time `generated_at` is
    assembled, so the timestamp travels with the text."""

    text: str
    refreshed_at: str | None


def _find(client, bank_id: str) -> dict | None:
    listed = client.list_mental_models(bank_id, detail="full")
    models = listed.get("mental_models") or listed.get("items") or []
    for model in models:
        if model.get("name") == BRIEF_MODEL_NAME:
            return model
    return None


def _reconcile(client, bank_id: str, model: dict, source_query: str) -> None:
    """Bring an existing model back in line with the constants above.

    Both the query and the trigger are versioned in code, so a deploy that
    changes either must reach the models that already exist -- they are created
    once, by an explicit provision call, and nothing else ever revisits them.
    Only the query was reconciled here at first, which meant a changed TRIGGER
    silently applied to new banks alone: the two models already provisioned in
    production would have kept a trigger no source file described.

    Reached only through `provision_section`. Reads used to arrive here on
    every brief, which repaired deployed models for free -- and was also how
    one exploratory GET minted a model on an unrelated bank. Since that path
    closed, carrying a changed constant to live models is an explicit
    post-deploy call to `POST /v1/admin/brief/{scope}/provision`.

    The trigger is merged rather than replaced, and compared only on the keys
    this module sets. Hindsight puts its own fields in there, and overwriting
    the whole object would quietly drop whatever we do not model.
    """
    changed: dict[str, object] = {}
    if model.get("source_query") != source_query:
        changed["source_query"] = source_query

    stored = model.get("trigger") or {}
    if any(stored.get(key) != value for key, value in TRIGGER.items()):
        changed["trigger"] = {**stored, **TRIGGER}

    if changed:
        client.update_mental_model(bank_id, model["id"], **changed)


def get_section(client, bank_id: str, now: datetime) -> Section | None:
    """The bank's digest, or None when there is nothing worth showing.

    Reads only. Provisioning used to live here, which meant one exploratory
    GET against an unrelated project minted a mental model there and spent a
    generation on it -- a read creating state, the same class as the
    readOnlyHint slug-squat. Creation moved to `provision_section`.
    """
    model = _find(client, bank_id)
    if model is None:
        return None

    content = (model.get("content") or "").strip()
    if not content or content == PLACEHOLDER:
        return None

    refreshed_at = model.get("last_refreshed_at")
    if model.get("is_stale") and _older_than(refreshed_at, now):
        return None

    # Served whole. A digest was hard-cut at 2000 characters here, which
    # measured live meant every section lost its last line mid-word: both
    # banks returned just over the cap (2018 and 2397 characters for a
    # max_tokens: 400 request, so the cut was the normal path, not an edge
    # case) and ended on "...omit tests entirely for trivi". A half sentence
    # is worse than a missing one: nothing marks it as incomplete to the
    # model reading it, so a rule can arrive meaning the opposite of what it
    # says. `max_tokens` already bounds this upstream.
    return Section(text=content, refreshed_at=refreshed_at)


def provision_section(client, bank_id: str, source_query: str) -> str:
    """Create the bank's brief model, or bring an existing one back in line.

    Returns "created" or "reconciled" so the admin route can say which.
    """
    model = _find(client, bank_id)
    if model is None:
        client.create_mental_model(
            bank_id,
            name=BRIEF_MODEL_NAME,
            source_query=source_query,
            max_tokens=MAX_TOKENS,
            trigger=dict(TRIGGER),
        )
        return "created"

    _reconcile(client, bank_id, model, source_query)
    return "reconciled"


def _older_than(timestamp: str | None, now: datetime) -> bool:
    if not timestamp:
        # Never refreshed and already stale: nothing to trust.
        return True
    try:
        refreshed = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    return now - refreshed > STALE_AFTER


def compose(
    policy: str,
    user: Section | None,
    project: Section | None,
    project_slug: str | None,
) -> str:
    """Policy first, sections after, and nothing at all when there is nothing.

    With no sections this returns the policy byte-for-byte, so a memory
    service that is down leaves the model with exactly what it gets today.
    """
    parts = [policy]
    if user:
        parts.append(f"-- What memory knows about you --\n{_CAVEAT}\n{user.text}")
    if project and project_slug:
        parts.append(
            f"-- What memory knows about {project_slug} --\n{_CAVEAT}\n{project.text}"
        )
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Orientation:
    """Deterministic project facts: a record, not memory.

    All three are unconstrained free text set by any authorised project
    member, so this is where untrusted content enters agent context. They are
    composed as one labelled value per line, never as prose that could read as
    an instruction, and `canonical_spec` is a pointer that is never fetched --
    following a path a project member wrote would turn a metadata field into
    a file read.
    """

    name: str | None
    canonical_spec: str | None
    purpose: str | None


_SEPARATOR = "\n\n"

# Lines each section may claim before any other one is looked at again.
# Measured: with no caps and a priority fill, a 2.4 KB user profile took the
# whole budget and the index tier carried no project half at all -- the exact
# failure this tier exists to fix, reproduced by the compiler meant to fix it.
#
# Both profiles get the same five: neither half of memory outranks the other,
# and five lines is what the digests actually lead with before they drift into
# the older, lower-value material. Working State gets its headline alone.
#
# Caps are LINE counts. Per-section ITEM budgets are a later phase and operate
# on schema items; a line is all this tier can count today.
INDEX_CAPS = {"user": 5, "project": 5, "working_state": 1}

# Who gets the leftover once every section has had its capped share. The
# user's standing rules lead because nothing else in the session shows them:
# the repository in front of the agent already carries the project's spec and
# layout. Orientation is not here -- it is filled first and whole, below.
#
# Both tiers EMIT in the order `_sections` returns, which is authority order.
_FILL_ORDER = ("user", "project", "working_state")


def budget_for(host: str | None) -> int:
    """Characters a host will carry, keyed on how it names itself."""
    return HOST_BUDGETS.get(host or "", SMALLEST_BUDGET)


def _header(revision: int) -> str:
    return f"-- ach-memory brief rev {revision} / protocol {MEMORY_PROTOCOL} --"


def _inert(value: str) -> str:
    """One line, always: a line break inside `purpose` would forge a section
    heading, and the forged section would read as one of ours.

    The projects API already rejects C0 controls in these three fields, but it
    is one validator away from this string reaching an agent, and U+2028 is
    not a C0 control.
    """
    return " ".join(value.split())


def _lines(section: Section | None) -> list[str]:
    """Free text as the lines the budget is spent in.

    The delivery contract describes both tiers in ITEMS ("at most 5"), but
    item structure arrives with a response schema in a later phase: today
    these profiles are free text, so a line is the smallest unit the compiler
    can drop without cutting a sentence in half.

    Blank lines go: they cost budget and say nothing.
    """
    if section is None:
        return []
    return [line for line in section.text.split("\n") if line.strip()]


def _orientation_lines(orientation: Orientation | None) -> list[str]:
    if orientation is None:
        return []
    labelled = (
        ("project", orientation.name),
        ("spec", orientation.canonical_spec),
        ("purpose", orientation.purpose),
    )
    return [f"{label}: {_inert(text)}" for label, text in labelled if text and text.strip()]


def _sections(
    user: Section | None,
    orientation: Orientation | None,
    project: Section | None,
    working_state: Section | None,
) -> list[tuple[str, list[str], list[str]]]:
    """(name, heading lines, body lines) in composition order.

    Working State is item 3 of both tiers and belongs to a later phase: with
    nothing writing it, its body is always empty and the section is never
    emitted. The seam is here so the tier does not have to be re-cut later.
    """
    return [
        ("orientation", [_ORIENTATION_HEADING], _orientation_lines(orientation)),
        ("user", [_USER_HEADING, _CAVEAT], _lines(user)),
        ("project", [_PROJECT_HEADING, _CAVEAT], _lines(project)),
        ("working_state", [_WORKING_STATE_HEADING], _lines(working_state)),
    ]


def _cost(part: str) -> int:
    """What a part costs assembled: itself plus the separator before it. Two
    characters high for the last part, which is slack in the safe direction."""
    return len(part) + len(_SEPARATOR)


def _fit(lines: list[str], remaining: int) -> tuple[list[str], int]:
    """As many whole lines as the remaining budget takes.

    Whole lines, never part of one: the digest hard-cut at 2000 characters
    ended every section mid-word, on "...omit tests entirely for trivi", and
    nothing marks a half sentence as incomplete to the model reading it.

    Stops at the first line that does not fit instead of skipping ahead to a
    shorter one -- a contiguous prefix is "the first rules", while a sieve is
    an unmarked selection.
    """
    kept: list[str] = []
    for line in lines:
        if len(line) + 1 > remaining:
            break
        kept.append(line)
        remaining -= len(line) + 1
    return kept, remaining


def compose_index(
    revision: int,
    user: Section | None,
    orientation: Orientation | None,
    project: Section | None,
    working_state: Section | None,
    budget: int,
) -> str:
    """The tier that rides a host's `instructions` field, under its budget.

    The header and `INDEX_SECTION` are reserved: everything else competes for
    what is left. A budget smaller than those two is honoured as far as it
    goes -- the affordance list is never traded away to fit, because an agent
    that is told half of what memory holds asks for the other half from the
    user.

    Three passes, because one greedy pass in priority order gives the whole
    budget to whichever section is longest and leaves the tier with one half
    of the brief, measured:

    1. Orientation, whole. Three deterministic lines, the only content here
       that is not model-generated, and effectively reserved next to
       `INDEX_SECTION`. Whole or not at all -- a metadata record quietly
       missing a field reads as a project that does not have one.
    2. A capped share for every other section, so no one of them can take the
       tier (`INDEX_CAPS`).
    3. The leftover, in `_FILL_ORDER` priority, so a user with three lines of
       profile does not get a half-empty tier while the project has thirty
       more lines to give.
    """
    header = _header(revision)
    tail = INDEX_SECTION.rstrip("\n")
    remaining = budget - _cost(header) - _cost(tail)

    sections = _sections(user, orientation, project, working_state)
    bodies = {name: body for name, _, body in sections}
    # A section's heading is charged once, the first time the section takes a
    # line, and dropped with it when no line fits: a heading over nothing says
    # memory is empty, a different claim than "this did not fit".
    headings = {name: _cost("\n".join(prefix)) for name, prefix, _ in sections}
    chosen: dict[str, list[str]] = {}

    kept, left = _fit(bodies["orientation"], remaining - headings["orientation"])
    if kept and len(kept) == len(bodies["orientation"]):
        chosen["orientation"] = kept
        remaining = left

    for name in _FILL_ORDER:
        kept, left = _fit(bodies[name][: INDEX_CAPS[name]], remaining - headings[name])
        if kept:
            chosen[name] = kept
            remaining = left

    for name in _FILL_ORDER:
        taken = len(chosen.get(name, []))
        if taken and taken < len(bodies[name]):
            kept, remaining = _fit(bodies[name][taken:], remaining)
            chosen[name] += kept

    parts = [header]
    parts += [
        "\n".join([*prefix, *chosen[name]])
        for name, prefix, _ in sections
        if name in chosen
    ]
    parts.append(tail)
    return _SEPARATOR.join(parts)


def compose_full(
    revision: int,
    user: Section | None,
    orientation: Orientation | None,
    project: Section | None,
    working_state: Section | None,
) -> str:
    """Every section whole, for the channel that has no cap.

    A strict superset of the index tier compiled from the same snapshot,
    `INDEX_SECTION` included: a consumer that keeps only whichever tier is
    newer must never lose the affordance list by keeping this one.
    """
    parts = [_header(revision)]
    parts += [
        "\n".join([*prefix, *body])
        for _, prefix, body in _sections(user, orientation, project, working_state)
        if body
    ]
    parts.append(INDEX_SECTION.rstrip("\n"))
    return _SEPARATOR.join(parts)
