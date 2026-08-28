"""The session brief: what memory already knows, composed for a session start.

Reading a mental model is a SELECT; the LLM cost is paid by its refresh. That
is what makes this affordable to send on every connect.

Every rule in here is about a digest being WRONG rather than missing. Measured
while designing this (2026-08-27): a loose query over this user's own memories
inverted one of his rules -- "after each change run a full test gate" where the
stored fact forbids exactly that -- and invented a role and a toolchain for him
that no memory contains. A missing section costs a session some context. A
confidently wrong one steers the work.
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
    once, the first time a bank is used, and nothing else ever revisits them.
    Only the query was reconciled here at first, which meant a changed TRIGGER
    silently applied to new banks alone: the two models already provisioned in
    production would have kept a trigger no source file described.

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


def ensure_section(
    client, bank_id: str, source_query: str, now: datetime
) -> Section | None:
    """The bank's digest, or None when there is nothing worth showing.

    Provisioning lives here rather than in a migration or a CLI step because a
    bank appears the first time somebody uses it: there is no earlier moment
    at which to create anything.
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
        return None

    _reconcile(client, bank_id, model, source_query)

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
