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

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from memory import profiles

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

_WORKING_STATE_CAVEAT = (
    "(a prior session's checkpoint, not an agenda -- verify against the "
    "repository before acting on it)"
)

# Bumped when the shape of a tier changes in a way a consumer must notice.
# A cached tier keeps the protocol it was compiled under, so a brief holding
# instructions the current contract has replaced is visible instead of silent.
MEMORY_PROTOCOL = 2
FULL_MAX_TOKENS = 2500
_CACHE_AGE_WIDTH = 10
_CACHE_AGE_PREFIX = "cache-age "
_CACHE_AGE_RE = re.compile(r"cache-age [0-9]{10}s")

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
_INDEX_HEADING = "-- What else memory holds --"
INDEX_SECTION = (
    f"{_INDEX_HEADING}\n"
    "user profile: this user's standing working preferences.\n"
    "project profile: this project's conventions, constraints and gotchas.\n"
    "facts and observations: recall(scope, query) -- your host will ask you to "
    "confirm the call.\n"
)

_ORIENTATION_HEADING = "-- This project --"
_USER_HEADING = "-- What memory knows about you --"
_PROJECT_HEADING = "-- What memory knows about this project --"
_WORKING_STATE_HEADING = "-- Where the work was left --"

# Only the compiler writes a heading. Profile text is model-generated from
# content any project member can write, and a profile line reading
# "-- What else memory holds --" renders a second, earlier affordance list
# naming whatever tools it likes -- the highest-value forgery in the tier,
# because that block is the one thing never dropped and it is what tells the
# agent what it may call.
_HEADINGS = frozenset(
    {
        _INDEX_HEADING,
        _ORIENTATION_HEADING,
        _USER_HEADING,
        _PROJECT_HEADING,
        _WORKING_STATE_HEADING,
    }
)


@dataclass(frozen=True)
class Section:
    """A digest the endpoint is willing to show, with the freshness it can
    report. The mental model is out of reach by the time `generated_at` is
    assembled, so the timestamp travels with the text."""

    text: str
    refreshed_at: str | None
    # A digest of what this section SAYS, set only by the structured loader
    # (`get_structured_section`). `refreshed_at` is a wall clock: it moves on
    # every refresh, including one that re-derived a byte-identical profile,
    # so keying a revision on it invalidates a consumer's cache for no
    # change. The content digest moves only when the compiled, rendered
    # profile does.
    #
    # Defaulted to None so the legacy prose path -- which has no normalized
    # form to hash, only the markdown Hindsight happened to emit -- keeps
    # constructing a Section unchanged, and so `api/brief.py` can tell the
    # two apart without threading the delivery mode down to every call site.
    content_fingerprint: str | None = None


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


# ---------------------------------------------------------------------------
# The structured section loader (SPEC Phase 4; MEMORY_PROFILE_DELIVERY_MODE=
# structured).
#
# This is a second way to BUILD a Section, not a second way to compose a tier.
# It ends where `get_section` ends -- one `Section` of already-rendered lines
# -- and everything after it (`_lines`, `_fit`, `INDEX_CAPS`, `compose_index`,
# `compose_full`) is the same code the prose path runs, so mandatory
# orientation and Working State keep their reservations and a profile item is
# still the optional material that gets dropped first.
#
# One rendered line per compiled item is deliberate: a line is the unit the
# allocator counts, so an item that does not fit is dropped whole instead of
# cut mid-claim -- the same reason the legacy digest is served whole rather
# than hard-cut.
# ---------------------------------------------------------------------------

# Bumped by hand when the rendering below changes shape. It is mixed into
# every content fingerprint so a deploy that renders the same items
# differently invalidates consumer caches on purpose, instead of leaving them
# holding a tier that no longer matches what this code would produce. Not
# derived from the Pydantic schema: what a cache has to notice is a change to
# the TEXT, and two different schemas can render identically while one
# rendering change can alter every line.
# render-2: a gotcha's `failure` joined the line (render-1 dropped it, which
# lost the observable symptom on every gotcha whose claim states only the
# trigger condition).
# render-3: `_restates` compares by normalized equality, not containment
# (render-2's containment check let an unrelated failure whose normalized
# text happened to be a substring of the claim -- "Stall" inside "Install" --
# suppress a real symptom the claim never stated).
PROFILE_RENDER_VERSION = "profile-v1-render-3"


def _find_profile(client, bank_id: str) -> dict | None:
    """Same list-and-match shape as `_find` above, for the profile model.

    A local finder per model name rather than a shared one: each loader owns
    the name it reads, and `get_structured_section` must be unable to answer
    with `ach-memory-session-brief`'s document by accident.
    """
    listed = client.list_mental_models(bank_id, detail="full")
    models = listed.get("mental_models") or listed.get("items") or []
    for model in models:
        if model.get("name") == profiles.PROFILE_MODEL_NAME:
            return model
    return None


def _restates(claim: str, failure: str) -> bool:
    """Whether `claim` already says exactly what `failure` says.

    The schema lets a synthesizing model file the same sentence in both
    fields, and a line that says the same thing twice spends scarce budget on
    nothing. This used to be containment, but containment is not a similarity
    measure: two different sentences can have one be a normalized substring of
    the other ("Stall" inside "Install"), or a negated claim can contain a
    plain restatement of the symptom it denies, and suppressing on that basis
    throws away what breaking actually looks like, which is the whole reason
    `failure` is delivered. Equality -- casefolded, whitespace-collapsed,
    terminal-punctuation-stripped -- only matches the one case the check
    exists for: the same sentence filed in both fields.
    """
    normalized_claim = " ".join(claim.split()).casefold().rstrip(".!?")
    normalized_failure = " ".join(failure.split()).casefold().rstrip(".!?")
    return bool(normalized_failure) and normalized_failure == normalized_claim


def _profile_line(compiled: profiles.CompiledProfileItem) -> str:
    """One compiled item as one line.

    The claim comes from the compiled wrapper, which is whitespace-normalized
    and merge-stable; `representative.claim` is one group member's raw text
    and would make the delivered line depend on which spelling won.

    What else the line carries:

    - A gotcha's `failure`, unless the claim already states it verbatim (see
      `_restates`). Claim and failure are two different facts -- WHEN it
      breaks and WHAT breaking looks like ("Deploy fails when DATABASE_URL is
      unset" / "The deploy script exits with a stack trace") -- and nothing in
      the schema makes one restate the other. The symptom is what lets an
      agent recognise the failure it is already looking at, so dropping it
      loses the half of a gotcha that fires at the moment it matters.
    - A gotcha's `cause`/`reproduction`. A bare warning is not actionable --
      "deploys sometimes fail" tells an agent nothing it can avoid -- and the
      schema already guarantees at least one of the two is present.
    - `provenance`, for a gotcha or for an explicit negative constraint only.
      Those are the items an agent is most likely to reason its way past --
      "never do X" with no reason attached invites exactly that -- so the
      "why it exists" is what keeps them from being misused. On a plain
      positive preference or convention the provenance changes no behaviour
      and would tax every line's budget to say so.

    Category is not rendered: it routes nothing here. A `Section` is a flat
    ordered list of lines per scope, and re-introducing category subheadings
    would spend budget on structure while colliding with the compiler's own
    heading rules. The order is `compile_profile`'s, unchanged.

    Every upstream string goes through `inert` -- claim, failure, cause,
    reproduction and provenance alike -- and so does the assembled line.
    `ProfileLine`
    already rejects C0 controls, but U+2028 is not one and `_lines` splits on
    it: a cause could otherwise open a line the allocator never charged
    budget for and forge a heading on it. Same defence, same reason, as
    `_orientation_lines`.
    """
    item = compiled.representative
    parts = [compiled.claim]
    if item.kind == "gotcha":
        if item.failure and not _restates(compiled.claim, item.failure):
            parts.append(f"failure: {item.failure}")
        if item.cause:
            parts.append(f"cause: {item.cause}")
        if item.reproduction:
            parts.append(f"reproduction: {item.reproduction}")
    if item.provenance and (item.kind == "gotcha" or item.negative):
        parts.append(f"provenance: {item.provenance}")
    return inert(" -- ".join(parts))


def _content_fingerprint(
    scope: profiles.ProfileScope, items: list[profiles.CompiledProfileItem]
) -> str:
    """A digest of the canonical typed truth this profile compiles.

    Over the compiled items, not the rendered text: `_profile_line` never
    renders `origin` or `evidence_ids` (a gotcha's failure/cause/reproduction
    and a negative's provenance are the only representative fields that reach
    a line), so a `stated -> confirmed` correction or an evidence swap left
    the old text-keyed fingerprint -- and the revision it feeds -- unmoved
    even though what memory actually asserts had changed. Hashing the
    compiled items means anything that changes the underlying truth moves the
    fingerprint, whether or not it happens to change a rendered character.

    `compile_profile` already puts the items in a canonical order, so this is
    stable under an upstream permutation. `PROFILE_SCHEMA_VERSION` and
    `PROFILE_RENDER_VERSION` are mixed in too: either can change what a given
    set of compiled fields is entitled to say without any field itself
    moving.
    """
    payload = {
        "scope": scope,
        "schema_version": profiles.PROFILE_SCHEMA_VERSION,
        "render_version": PROFILE_RENDER_VERSION,
        "items": [
            {
                "category": item.category,
                "claim_key": item.claim_key,
                "claim": item.claim,
                "kind": item.representative.kind,
                "origin": item.representative.origin,
                "negative": item.representative.negative,
                "failure": item.representative.failure,
                "cause": item.representative.cause,
                "reproduction": item.representative.reproduction,
                "provenance": item.representative.provenance,
                "evidence_ids": sorted(item.evidence_ids),
                "support_count": item.support_count,
            }
            for item in items
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_structured_section(
    client, bank_id: str, scope: profiles.ProfileScope, now: datetime
) -> Section | None:
    """The bank's structured profile as a Section, or None.

    Reads only, exactly like `get_section`: no create, no reconcile, no
    refresh. The profile model exists solely because somebody called
    `POST /v1/admin/profile/{scope}/provision`.

    `content` is never read here, at any point, including as a fallback.
    That field is the legacy model's contract; on a profile model it is at
    best a rendering of an older synthesis a correction may already have
    superseded. Anything wrong with the structured output -- absent,
    malformed, the other scope's document, every item ungrounded -- fails
    closed to None, and the caller does NOT then try the prose model.
    """
    model = _find_profile(client, bank_id)
    if model is None:
        return None

    reflect_response = model.get("reflect_response")
    if not isinstance(reflect_response, dict):
        return None

    refreshed_at = model.get("last_refreshed_at")
    # Fail closed on ANY staleness, not after a grace period: `get_section`'s
    # `_older_than` grace period exists for legacy prose because a slightly
    # stale narrative summary degrades gracefully. A structured item is
    # delivered as current typed truth with no room for the agent to hedge
    # it, so a stale one -- even seconds stale -- must not be served as
    # current at all.
    if model.get("is_stale"):
        return None

    items = profiles.compile_profile(scope, reflect_response)
    if not items:
        return None

    text = "\n".join(_profile_line(item) for item in items)
    return Section(
        text=text,
        refreshed_at=refreshed_at,
        content_fingerprint=_content_fingerprint(scope, items),
    )


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

# Floor RESERVATION, not emission, order. Working State is last in
# `_FILL_ORDER` (it renders after both profiles), but its floor must be
# promised before either profile's: a profile is optional context, Working
# State is what the agent is doing right now, and "may disappear to honour
# the budget" applies to the former, never the latter. With floors promised
# in `_FILL_ORDER` instead, two near-maximum profile floors could each fit
# on their own and together leave no room for Working State's -- both
# profiles present, the one section that must not disappear, gone.
_FLOOR_ORDER = ("working_state", "user", "project")


def budget_for(host: str | None) -> int:
    """Characters a host will carry, keyed on how it names itself."""
    return HOST_BUDGETS.get(host or "", SMALLEST_BUDGET)


def _header(revision: int, project_slug: str | None) -> str:
    """The revision is one counter per (user, project), so the tier has to name
    which counter its number came from.

    An MCP proxy that starts with no git locator caches a revision from the
    no-project counter while a hook fetches the full tier with a slug, from a
    different one: "INDEX rev 42 / FULL rev 39" then compares two unrelated
    sequences and the consumer keeps the wrong tier -- the failure the revision
    exists to prevent.
    """
    scope = f"project {project_slug}" if project_slug else "no project"
    return (
        f"-- ach-memory brief rev {revision} / protocol {MEMORY_PROTOCOL} / "
        f"{_cache_age_field(0)} / {scope} --"
    )


def _cache_age_field(age_seconds: int) -> str:
    bounded = min(max(age_seconds, 0), (10**_CACHE_AGE_WIDTH) - 1)
    return f"{_CACHE_AGE_PREFIX}{bounded:0{_CACHE_AGE_WIDTH}d}s"


def stamp_cache_age(instructions: str, age_seconds: int) -> str:
    """Stamp a compiled payload without changing its budgeted length."""
    return _CACHE_AGE_RE.sub(_cache_age_field(age_seconds), instructions, count=1)


def carries_cache_age(text: str) -> bool:
    """Whether a compiled payload reserves a cache-age slot to stamp.

    False for anything compiled before protocol 2 introduced the field --
    stamp_cache_age's substitution is then a silent no-op, and the caller
    must fall back to making the age visible some other way.
    """
    return bool(_CACHE_AGE_RE.search(text))


def token_upper_bound(text: str) -> int:
    """Safe upper bound for byte-level host tokenizers."""
    return len(text.encode("utf-8"))


def inert(value: str) -> str:
    """One line, always: a line break inside `purpose` would forge a section
    heading, and the forged section would read as one of ours.

    The projects API already rejects C0 controls in these three fields, but it
    is one validator away from this string reaching an agent, and U+2028 is
    not a C0 control.
    """
    return " ".join(value.split())


def _lines(section: Section | None) -> list[str]:
    """Free text as the lines the budget is spent in.

    The delivery contract describes both tiers in ITEMS ("at most 5"), but a
    legacy prose profile is free text, so a line is the smallest unit the
    compiler can drop without cutting a sentence in half. Structured delivery
    keeps that unit rather than replacing it: `get_structured_section` renders
    exactly one line per compiled item, so here a line and an item are the
    same thing and nothing below has to know which loader ran.

    Blank lines go: they cost budget and say nothing. Trailing whitespace goes
    with them -- a CRLF digest would otherwise pay for a carriage return on
    every line and render one too.

    `splitlines`, not `split("\\n")`: it also breaks on U+2028 and friends,
    which several hosts render as a line break. A "line" the compiler cannot
    see is a line the heading check below cannot reject.

    A line that IS one of our headings is dropped rather than shown: forgery
    or coincidence, it cannot be rendered as itself, and dropping is the one
    response that cannot be misread by whoever reads the tier next.
    """
    if section is None:
        return []
    stripped = (line.rstrip() for line in section.text.splitlines())
    return [line for line in stripped if line.strip() and line.strip() not in _HEADINGS]


def _orientation_lines(orientation: Orientation | None) -> list[str]:
    if orientation is None:
        return []
    labelled = (
        ("project", orientation.name),
        ("spec", orientation.canonical_spec),
        ("purpose", orientation.purpose),
    )
    return [f"{label}: {inert(text)}" for label, text in labelled if text and text.strip()]


def _sections(
    user: Section | None,
    orientation: Orientation | None,
    project: Section | None,
    working_state: Section | None,
) -> list[tuple[str, list[str], list[str]]]:
    """(name, heading lines, body lines) in composition order.

    Working State is item 3 of both tiers: absent (empty body, never
    emitted) when the caller has no workspace_id or nothing stored there,
    present as a rendered Section from working_state.py otherwise -- see
    render_index_headline() and render_full_section().
    """
    return [
        ("orientation", [_ORIENTATION_HEADING], _orientation_lines(orientation)),
        ("user", [_USER_HEADING, _CAVEAT], _lines(user)),
        ("project", [_PROJECT_HEADING, _CAVEAT], _lines(project)),
        (
            "working_state",
            [_WORKING_STATE_HEADING, _WORKING_STATE_CAVEAT],
            _lines(working_state),
        ),
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
    project_slug: str | None,
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
       tier (`INDEX_CAPS`) -- and a FLOOR held back for each, because a cap in
       lines is not a cap in characters: five 300-character user lines spent
       the project's room before the project was looked at, which put the
       measured defect back with 171 characters left unspent. Floors are
       promised in `_FLOOR_ORDER`, Working State first: a profile may lose
       its floor to the budget, Working State may not.
    3. The leftover, one line per section per lap, so a user with three lines
       of profile does not get a half-empty tier while the project has thirty
       more lines to give -- and so the surplus is not all handed to whoever
       comes first in `_FILL_ORDER`.
    """
    header = _header(revision, project_slug)
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

    # A heading and one line for every section that has something to say, held
    # back before any section spends. Promised in `_FLOOR_ORDER` -- Working
    # State first, then the profiles -- and only while the budget covers them:
    # a section that cannot be promised a floor is not promised one, rather
    # than taking it from a section ahead of it.
    floors: dict[str, int] = {}
    room = remaining
    for name in _FLOOR_ORDER:
        if not bodies[name]:
            continue
        floor = headings[name] + len(bodies[name][0]) + 1
        if floor > room:
            break
        floors[name] = floor
        room -= floor

    reserved = sum(floors.values())
    for name in _FILL_ORDER:
        if not bodies[name]:
            continue
        # Everyone else's floor is off limits while this section spends.
        held = reserved - floors.get(name, 0)
        kept, left = _fit(
            bodies[name][: INDEX_CAPS[name]], remaining - headings[name] - held
        )
        if kept:
            chosen[name] = kept
            remaining = left + held
        reserved -= floors.get(name, 0)

    while True:
        spent = False
        for name in _FILL_ORDER:
            taken = len(chosen.get(name, []))
            if not taken or taken >= len(bodies[name]):
                continue
            kept, remaining = _fit(bodies[name][taken : taken + 1], remaining)
            if kept:
                chosen[name] += kept
                spent = True
        if not spent:
            break

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
    project_slug: str | None,
    user: Section | None,
    orientation: Orientation | None,
    project: Section | None,
    working_state: Section | None,
    max_tokens: int = FULL_MAX_TOKENS,
) -> str:
    """Compile a bounded Full tier, dropping only whole semantic lines."""
    header = _header(revision, project_slug)
    tail = INDEX_SECTION.rstrip("\n")
    separator_cost = token_upper_bound(_SEPARATOR)

    def part_cost(lines: list[str]) -> int:
        return token_upper_bound("\n".join(lines)) + separator_cost

    remaining = max_tokens - part_cost([header]) - part_cost([tail])
    sections = _sections(user, orientation, project, working_state)
    chosen: dict[str, list[str]] = {}

    orientation_section = next(item for item in sections if item[0] == "orientation")
    _, orientation_prefix, orientation_body = orientation_section
    orientation_lines = [*orientation_prefix, *orientation_body]
    orientation_cost = part_cost(orientation_lines)
    if orientation_body and orientation_cost <= remaining:
        chosen["orientation"] = list(orientation_body)
        remaining -= orientation_cost

    dynamic = [item for item in sections if item[0] != "orientation" and item[2]]

    # For every dynamic section, body[0] is the mandatory floor line. For
    # "working_state" specifically, body[-2:] (age, source session -- see
    # working_state.render_full_section) is ALSO mandatory: the caller's
    # only signal that a checkpoint might be stale. Split each body into its
    # optional middle and (for working_state) a protected tail, so the
    # round-robin below can grow the middle without that tail ever losing a
    # budget race to unrelated project/user content or to Working State's
    # own optional lines (current direction, decisions, questions, next
    # steps).
    middles: dict[str, list[str]] = {}
    tails: dict[str, list[str]] = {}
    for name, _, body in dynamic:
        if name == "working_state":
            middles[name] = body[1:-2]
            tails[name] = body[-2:]
        else:
            middles[name] = body[1:]
            tails[name] = []

    # Reserved in `_FLOOR_ORDER`, not `dynamic`'s emission order: each floor
    # here is evaluated independently against whatever `remaining` is left
    # over from the ones tried before it, so trying user/project first could
    # spend the budget two near-maximum profile floors could each cover on
    # their own before Working State's short floor was ever considered.
    for name, prefix, body in sorted(dynamic, key=lambda item: _FLOOR_ORDER.index(item[0])):
        floor_lines = [body[0], *tails[name]]
        floor_cost = part_cost([*prefix, *floor_lines])
        if floor_cost > remaining:
            continue
        chosen[name] = floor_lines
        remaining -= floor_cost

    grown: dict[str, int] = dict.fromkeys(chosen, 0)
    while True:
        spent = False
        for name, _, _ in dynamic:
            if name not in chosen:
                continue
            middle = middles[name]
            taken = grown[name]
            if taken >= len(middle):
                continue
            line_cost = token_upper_bound("\n" + middle[taken])
            if line_cost <= remaining:
                protected = tails[name]
                head = chosen[name][: len(chosen[name]) - len(protected)]
                chosen[name] = [*head, middle[taken], *protected]
                remaining -= line_cost
                grown[name] = taken + 1
                spent = True
        if not spent:
            break

    parts = [header]
    parts += [
        "\n".join([*prefix, *chosen[name]])
        for name, prefix, _ in sections
        if name in chosen
    ]
    parts.append(tail)
    return _SEPARATOR.join(parts)


def survived(text: str) -> dict[str, bool]:
    """Which sections a composed tier actually carries.

    Read back off the tier rather than taken from what the compiler was
    handed: a budget drops sections, so an index tier can be compiled from a
    project digest and arrive with none of it. Reporting `project: true` there
    tells a consumer -- and, from the next task, a disk cache -- that it holds
    a project half it does not have.

    Exact line equality is sound because `_lines` drops any profile line that
    matches one of our headings, so the only headings in a tier are ours.
    """
    lines = text.split("\n")
    return {
        "user": _USER_HEADING in lines,
        "project": _PROJECT_HEADING in lines,
        "working_state": _WORKING_STATE_HEADING in lines,
    }
