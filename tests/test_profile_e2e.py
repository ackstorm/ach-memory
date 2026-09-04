"""Phase 4's delivery gate: one curated session, driven through the real route.

Every other Phase 4 suite pins one layer. `tests/test_profiles.py` pins the
closed schemas and the compiler, `tests/test_brief.py` pins the section loader
and the delivery mode, `tests/test_profile_evaluation.py` pins the non-
persisting evaluator. This file asks the only question none of them can: given
one plausible bank's worth of synthesized state, does what a real consuming
agent RECEIVES obey every Phase 4 contract at once?

So nothing here calls `compile_profile`, `get_structured_section` or
`compose_full` directly. Every assertion is against the body of a real
`GET /v1/session-brief` -- the `instructions` string, the `sections` map, the
`brief_revision` -- served by the real FastAPI app over a respx-mocked
Hindsight. A defect that lives between two layers (an item the compiler drops
but the renderer would have shown, a mandatory field the allocator sacrifices
to a profile at its ceiling) is only visible from here.

The corpus below is entirely synthetic: an invented operator, an invented
`orbital-ledger` project, invented failures. No real name, host, path, secret
or claim appears anywhere in this file, and nothing in it is a fixture for
production data.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from memory import brief, profiles
from memory.config import Settings, get_settings

BASE = "http://hindsight.test"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

PROJECT = "orbital-ledger"
WORKSPACE_A = "ws_" + "a" * 32
WORKSPACE_B = "ws_" + "b" * 32

# Prose parked on the structured model's own `content` field. The structured
# loader must never read it, so its absence from a structured response is what
# proves no silent fallback happened -- on the very model the loader did read.
LEGACY_TRAP = "Trap prose: only the legacy loader may ever read this."


# ---------------------------------------------------------------------------
# The curated corpus (Step 1).
#
# One coherent scenario: a synthetic operator whose banks have been synthesized
# once, holding the full range of things Phase 4 has to sort out -- durable
# truth, evidence-only material the durability matrix excludes, claims whose
# `kind`/`origin` the closed enums do not admit at all, one claim filed twice
# by two capture sessions, and one claim a later correction has superseded.
#
# Claims are named constants rather than inline strings because almost every
# test below asserts on the presence or absence of the exact delivered text: a
# renamed constant fails to import, a retyped string literal silently asserts
# nothing.
# ---------------------------------------------------------------------------

# --- User claims that MUST survive to the wire -----------------------------

U_STATED_PREFERENCE = "Answer in English even when the question arrives in Spanish."
U_CONFIRMED_PREFERENCE = "Show the unified diff before applying an edit."
U_OBSERVED_CONVENTION = "Run the focused test module before the whole suite."
U_NEGATIVE_CONSTRAINT = "Never push straight to the main branch."
U_NEGATIVE_PROVENANCE = "Agreed in the invented 2026-08 onboarding review."
U_CORRECTED_TRUTH = "Release notes belong in CHANGELOG.md."

# --- User claims that MUST NEVER reach the wire ----------------------------

# (preference, observed) and (decision, observed) are the two evidence-only
# cells of the durability matrix.
U_OBSERVED_PREFERENCE = "Keeps the terminal on the dark colour scheme."
U_OBSERVED_DECISION = "Settled on the sandbox package manager for dependencies."
# Neither value exists in the closed `kind`/`origin` enums, so these two never
# reach the durability table at all -- they fail to validate as items.
U_TECHNICAL_CLAIM = "The ledger service listens on port 8080 in the sandbox stack."
U_INFERRED_CLAIM = "Probably wants terse answers, judging by message length."
# Structurally perfect and topically durable, but the current reflection no
# longer cites its evidence: current truth has moved on. See
# `test_a_correction_retires_old_truth_without_deleting_its_evidence`.
U_SUPERSEDED_TRUTH = "Release notes belong in the invented ship-log chat channel."

SUPERSEDED_EVIDENCE = "mem-u-superseded"

# --- Project claims that MUST survive to the wire --------------------------

P_STATED_DECISION = "Ledger entries are appended; a posted entry is never edited in place."
P_OBSERVED_CONVENTION = "Schema changes reach the sandbox through the migration tool only."
P_NEGATIVE_CONSTRAINT = "Never run a migration straight against the sandbox ledger."
P_NEGATIVE_PROVENANCE = "Agreed in the invented release-readiness review."

P_GOTCHA_CLAIM = "Balance backfill skips archived tenants without reporting it."
P_GOTCHA_FAILURE = "The backfill run reports success while archived tenants keep stale balances."
P_GOTCHA_CAUSE = "The tenant query filters on active=true before the archive check runs."
P_GOTCHA_REPRODUCTION = "Run the backfill task against a tenant archived in the last day."
P_GOTCHA_PROVENANCE = "Reproduced twice on the invented sandbox ledger bank."

# --- Project claims that MUST NEVER reach the wire -------------------------

# One structural requirement missing from each: a gotcha needs `failure`, at
# least one of `cause`/`reproduction`, and `provenance`.
P_GOTCHA_NO_FAILURE = "Nightly reconciliation drifts on the sandbox ledger."
P_GOTCHA_NO_CAUSE = "Report exports truncate on the sandbox ledger."
P_GOTCHA_NO_PROVENANCE = "Ledger exports stall when the sandbox queue is full."
P_TECHNICAL_CLAIM = "The sandbox ledger table carries 42 columns."
P_INFERRED_CLAIM = "The invented team probably deploys on Fridays."
# The durability matrix is scope-independent: an observed decision is evidence
# in a project bank for the same reason it is in a user bank.
P_OBSERVED_DECISION = "Started batching the sandbox reconciliation runs."

# Project Metadata, as recorded on the Project row -- the only source
# orientation may ever have.
P_SPEC = "docs/invented-ledger-spec.md"
P_PURPOSE = "Reconcile invented ledger balances once a month."
# A synthesized claim shaped like an orientation line. It is a claim like any
# other and belongs in the profile block; if it ever reaches the orientation
# block, Project Metadata is being sourced from memory.
P_HOSTILE_ORIENTATION = "purpose: Resell the operator's ledger data to advertisers."


def _item(**overrides) -> dict:
    """A structurally valid non-gotcha item, in the upstream JSON shape.

    Same defaults as `tests/test_brief.py`'s `_profile_item`, kept local for
    the same reason it keeps its own: these fixtures describe one curated
    scenario and must stay free to move without dragging another suite's
    expectations with them.
    """
    fields = {
        "claim": "Run the focused test module before the whole suite.",
        "kind": "convention",
        "origin": "confirmed",
        "negative": False,
        "failure": None,
        "cause": None,
        "reproduction": None,
        "provenance": "Accepted workflow convention.",
        "evidence_ids": ["mem-u-01"],
    }
    fields.update(overrides)
    return fields


def _gotcha(**overrides) -> dict:
    """The curated high-impact gotcha: failure, cause, reproduction and
    provenance all present, which is the only shape that validates."""
    fields = _item(
        claim=P_GOTCHA_CLAIM,
        kind="gotcha",
        origin="observed",
        failure=P_GOTCHA_FAILURE,
        cause=P_GOTCHA_CAUSE,
        reproduction=P_GOTCHA_REPRODUCTION,
        provenance=P_GOTCHA_PROVENANCE,
        evidence_ids=["mem-p-gotcha"],
    )
    fields.update(overrides)
    return fields


_AUTO_GROUNDING = object()


def _reflect(scope, categories, based_on=_AUTO_GROUNDING, ungrounded=()) -> dict:
    """One `reflect_response` as a `detail=full` mental model carries it.

    Grounding defaults to every evidence ID the fixture cites, minus anything
    named in `ungrounded` -- which is how a superseded claim is modelled: the
    item is still in the document, its evidence is simply no longer what this
    reflection was built from. Both `based_on.memories` shapes this repository
    has fixtures for are accepted upstream; the object form is used here
    because it is the shape every other Hindsight list endpoint uses.
    """
    if based_on is _AUTO_GROUNDING:
        found: list[str] = []
        for items in categories.values():
            for item in items:
                for evidence_id in item.get("evidence_ids") or []:
                    if evidence_id not in found and evidence_id not in ungrounded:
                        found.append(evidence_id)
        based_on = {"memories": [{"id": evidence_id} for evidence_id in found]}
    root = "user_profile" if scope == "user" else "project_profile"
    return {"structured_output": {root: categories}, "based_on": based_on}


def _profile_model(reflect_response, *, refreshed=NOW, stale=False, content=LEGACY_TRAP) -> dict:
    return {
        "id": "mm-profile-1",
        "name": profiles.PROFILE_MODEL_NAME,
        "content": content,
        "is_stale": stale,
        "last_refreshed_at": refreshed.isoformat(),
        "reflect_response": reflect_response,
    }


def _legacy_model(content, *, refreshed=NOW) -> dict:
    return {
        "id": "mm-legacy-1",
        "name": brief.BRIEF_MODEL_NAME,
        "content": content,
        "source_query": brief.USER_QUERY,
        "is_stale": False,
        "last_refreshed_at": refreshed.isoformat(),
        "trigger": dict(brief.TRIGGER),
    }


def user_categories(*, superseded=True, corrected=True) -> dict[str, list[dict]]:
    """The curated user document.

    `superseded`/`corrected` exist only for the correction test, which needs
    the same corpus one refresh earlier: before the correction the retired
    claim is the grounded one and the corrected claim does not exist yet.
    """
    engineering = [
        _item(
            claim=U_CONFIRMED_PREFERENCE,
            kind="preference",
            origin="confirmed",
            evidence_ids=["mem-u-02"],
        ),
        _item(claim=U_OBSERVED_CONVENTION, origin="observed", evidence_ids=["mem-u-03"]),
        # Durability matrix: an observed decision is evidence, never profile
        # truth.
        _item(
            claim=U_OBSERVED_DECISION,
            kind="decision",
            origin="observed",
            evidence_ids=["mem-u-04"],
        ),
        # `technical_claim` is not in the closed kind enum, `inferred` is not
        # in the closed origin enum. Neither is a durability decision -- the
        # items simply do not validate -- and neither may take the whole
        # document down with it.
        _item(claim=U_TECHNICAL_CLAIM, kind="technical_claim", evidence_ids=["mem-u-08"]),
        _item(claim=U_INFERRED_CLAIM, kind="preference", origin="inferred",
              evidence_ids=["mem-u-09"]),
    ]
    if corrected:
        engineering.append(_item(claim=U_CORRECTED_TRUTH, evidence_ids=["mem-u-05"]))
    if superseded:
        engineering.append(_item(claim=U_SUPERSEDED_TRUTH, evidence_ids=[SUPERSEDED_EVIDENCE]))
    return {
        "interaction": [
            _item(
                claim=U_STATED_PREFERENCE,
                kind="preference",
                origin="stated",
                evidence_ids=["mem-u-01"],
            ),
            # The same semantic claim, filed a second time from a second
            # capture session with its own evidence. One claim, two entries.
            _item(
                claim=U_STATED_PREFERENCE,
                kind="preference",
                origin="stated",
                evidence_ids=["mem-u-07"],
            ),
        ],
        "engineering": engineering,
        # Durability matrix: an observed preference is evidence, never profile
        # truth.
        "preferences": [
            _item(
                claim=U_OBSERVED_PREFERENCE,
                kind="preference",
                origin="observed",
                evidence_ids=["mem-u-06"],
            )
        ],
        "constraints": [
            _item(
                claim=U_NEGATIVE_CONSTRAINT,
                kind="preference",
                origin="stated",
                negative=True,
                provenance=U_NEGATIVE_PROVENANCE,
                evidence_ids=["mem-u-10"],
            )
        ],
    }


def project_categories() -> dict[str, list[dict]]:
    """The curated project document."""
    return {
        "decisions": [
            _item(
                claim=P_STATED_DECISION,
                kind="decision",
                origin="stated",
                evidence_ids=["mem-p-01"],
            ),
            _item(
                claim=P_OBSERVED_DECISION,
                kind="decision",
                origin="observed",
                evidence_ids=["mem-p-10"],
            ),
        ],
        "workflow": [
            # The project schema has no `constraints` bucket, so a negative
            # project claim stays where it topically belongs. It still ranks
            # at tier 0 with the gotchas.
            _item(
                claim=P_NEGATIVE_CONSTRAINT,
                negative=True,
                provenance=P_NEGATIVE_PROVENANCE,
                evidence_ids=["mem-p-02"],
            )
        ],
        "conventions": [
            _item(claim=P_OBSERVED_CONVENTION, origin="observed", evidence_ids=["mem-p-03"]),
            _item(claim=P_HOSTILE_ORIENTATION, evidence_ids=["mem-p-04"]),
            _item(claim=P_INFERRED_CLAIM, origin="inferred", evidence_ids=["mem-p-05"]),
            _item(claim=P_TECHNICAL_CLAIM, kind="technical_claim", evidence_ids=["mem-p-06"]),
        ],
        "gotchas": [
            _gotcha(),
            _gotcha(claim=P_GOTCHA_NO_FAILURE, failure=None, evidence_ids=["mem-p-07"]),
            _gotcha(
                claim=P_GOTCHA_NO_CAUSE,
                cause=None,
                reproduction=None,
                evidence_ids=["mem-p-08"],
            ),
            _gotcha(claim=P_GOTCHA_NO_PROVENANCE, provenance=None, evidence_ids=["mem-p-09"]),
        ],
    }


# Every claim the curated corpus must deliver, and every claim it must not.
# Named as sets so a test can assert over the whole matrix at once rather than
# listing five `not in` lines and quietly omitting the sixth.
USER_DELIVERED = frozenset(
    {
        U_STATED_PREFERENCE,
        U_CONFIRMED_PREFERENCE,
        U_OBSERVED_CONVENTION,
        U_NEGATIVE_CONSTRAINT,
        U_CORRECTED_TRUTH,
    }
)
USER_EXCLUDED = frozenset(
    {
        U_OBSERVED_PREFERENCE,
        U_OBSERVED_DECISION,
        U_TECHNICAL_CLAIM,
        U_INFERRED_CLAIM,
        U_SUPERSEDED_TRUTH,
    }
)
PROJECT_DELIVERED = frozenset(
    {P_STATED_DECISION, P_OBSERVED_CONVENTION, P_NEGATIVE_CONSTRAINT, P_GOTCHA_CLAIM}
)
PROJECT_EXCLUDED = frozenset(
    {
        P_GOTCHA_NO_FAILURE,
        P_GOTCHA_NO_CAUSE,
        P_GOTCHA_NO_PROVENANCE,
        P_TECHNICAL_CLAIM,
        P_INFERRED_CLAIM,
        P_OBSERVED_DECISION,
    }
)


# ---------------------------------------------------------------------------
# Driving the real boundary (Step 2).
# ---------------------------------------------------------------------------


def _structured_mode(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_PROFILE_DELIVERY_MODE", "structured")
    get_settings.cache_clear()


def _mock_models(bank_id, models):
    respx.get(url__regex=rf"{BASE}/v1/default/banks/{bank_id}/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": models})
    )


def _bank_ids(session, user_id, project_slug=None):
    from memory.models import Project, ProjectSlug, User

    user_bank = session.get(User, user_id).bank_id
    if project_slug is None:
        return user_bank, None
    mapping = session.query(ProjectSlug).filter_by(slug=project_slug).one()
    return user_bank, session.get(Project, mapping.project_internal_id).bank_id


def _get_brief(client, headers, **params):
    response = client.get(
        "/v1/session-brief", params={"scope": "user", **params}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _block(instructions, heading):
    """The one composed part that opens with this heading."""
    return next(part for part in instructions.split("\n\n") if part.startswith(heading))


def _items(instructions, heading):
    """The rendered item lines of one profile block.

    A profile block is `heading`, then the caveat, then one line per delivered
    item -- so the item lines are what is left after those two, and their count
    is the delivered item count a budget assertion needs.
    """
    return _block(instructions, heading).split("\n")[2:]


def _user_items(instructions):
    return _items(instructions, brief._USER_HEADING)


def _project_items(instructions):
    return _items(instructions, brief._PROJECT_HEADING)


def _curated_user_bank(session, user_id, **kwargs):
    user_bank, _ = _bank_ids(session, user_id)
    _mock_models(
        user_bank,
        [_profile_model(_reflect("user", user_categories(**kwargs), ungrounded=(SUPERSEDED_EVIDENCE,)))],
    )
    return user_bank


def _open_project(client, headers, *, slug=PROJECT, metadata=True):
    assert client.post(
        "/v1/projects", json={"project_slug": slug}, headers=headers
    ).status_code in (200, 201)
    if metadata:
        assert (
            client.patch(
                f"/v1/projects/{slug}",
                json={"canonical_spec": P_SPEC, "purpose": P_PURPOSE},
                headers=headers,
            ).status_code
            == 200
        )


def _checkpoint(client, headers, *, workspace_id=WORKSPACE_A, session_id="sess-1", **fields):
    """One Working State checkpoint, written through the real route."""
    epoch = client.post(
        "/v1/working-state/sessions",
        json={"project_slug": PROJECT, "workspace_id": workspace_id, "session_id": session_id},
        headers=headers,
    ).json()["session_epoch"]
    response = client.put(
        "/v1/working-state",
        json={
            "project_slug": PROJECT,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "session_epoch": epoch,
            "checkpoint_seq": fields.pop("checkpoint_seq", 1),
            "objective": fields.pop("objective", "close the invented ledger sprint"),
            **fields,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Step 3, bullet 2: the ineligible matrix combinations and bare technical
# claims are absent -- and bullets 1/3 for the curated corpus itself.
# ---------------------------------------------------------------------------


@respx.mock
def test_the_curated_user_profile_delivers_current_truth_and_nothing_else(
    client, two_users, session, monkeypatch
):
    """The whole user corpus at once, judged on the delivered string.

    Five durable claims in, five out; and every excluded category -- observed
    preference, observed decision, a kind the enum has no cell for, an origin
    the enum has no cell for, and a claim whose evidence the current
    reflection no longer cites -- is absent from what the agent receives.
    """
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    _curated_user_bank(session, two_users[0]["user_id"])

    body = _get_brief(client, headers, tier="full")

    assert body["sections"] == {"user": True, "project": False, "working_state": False}
    delivered = body["instructions"]
    for claim in USER_DELIVERED:
        assert claim in delivered, claim
    for claim in USER_EXCLUDED:
        assert claim not in delivered, claim
    # One bad item drops alone: the two unvalidatable items sat in the middle
    # of `engineering`, and everything after them still arrived.
    assert len(_user_items(delivered)) == len(USER_DELIVERED)
    # No fallback: the structured loader never read the model's `content`.
    assert LEGACY_TRAP not in delivered


@respx.mock
def test_the_hooks_plain_text_tier_carries_the_same_curated_profile(
    client, two_users, session, monkeypatch
):
    """`format=text` is what the SessionStart hook actually fetches -- a curl
    and a cat, no JSON parser -- so it bypasses the response model entirely.
    The gate has to hold on the bytes that path receives, not only on the
    `instructions` field of the JSON one."""
    _structured_mode(monkeypatch)
    _curated_user_bank(session, two_users[0]["user_id"])

    response = client.get(
        "/v1/session-brief",
        params={"scope": "user", "tier": "full", "format": "text"},
        headers=two_users[0]["headers"],
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    for claim in USER_DELIVERED:
        assert claim in response.text, claim
    for claim in USER_EXCLUDED:
        assert claim not in response.text, claim
    assert LEGACY_TRAP not in response.text


@respx.mock
def test_one_claim_filed_by_two_sessions_is_delivered_once(
    client, two_users, session, monkeypatch
):
    """Two capture sessions described the same preference, each citing its own
    evidence. A profile is current truth, not a log of who said it: the agent
    must see one line, not the same rule twice with different receipts."""
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    _curated_user_bank(session, two_users[0]["user_id"])

    delivered = _get_brief(client, headers, tier="full")["instructions"]

    assert delivered.count(U_STATED_PREFERENCE) == 1
    # Merged, not dropped: the survivor holds both sessions' evidence, which
    # is what puts it ahead of the single-evidence preference below it.
    lines = _user_items(delivered)
    stated = next(index for index, line in enumerate(lines) if U_STATED_PREFERENCE in line)
    confirmed = next(index for index, line in enumerate(lines) if U_CONFIRMED_PREFERENCE in line)
    assert stated < confirmed


@pytest.mark.parametrize(
    ("category", "item"),
    [
        (
            "preferences",
            _item(claim=U_OBSERVED_PREFERENCE, kind="preference", origin="observed"),
        ),
        (
            "engineering",
            _item(claim=U_OBSERVED_DECISION, kind="decision", origin="observed"),
        ),
        ("engineering", _item(claim=U_TECHNICAL_CLAIM, kind="technical_claim")),
        ("engineering", _item(claim=U_INFERRED_CLAIM, kind="preference", origin="inferred")),
    ],
    ids=["observed-preference", "observed-decision", "technical-claim", "inferred-origin"],
)
@respx.mock
def test_an_ineligible_item_alone_delivers_no_section_at_all(
    client, two_users, session, monkeypatch, category, item
):
    """Absence inside a full document could be a lost budget race. Alone in
    the document, an ineligible item has no competition -- so an empty user
    section is the only reading left, and it is the fail-closed one."""
    _structured_mode(monkeypatch)
    user_bank, _ = _bank_ids(session, two_users[0]["user_id"])
    _mock_models(user_bank, [_profile_model(_reflect("user", {category: [item]}))])

    body = _get_brief(client, two_users[0]["headers"], tier="full")

    assert body["sections"]["user"] is False
    assert item["claim"] not in body["instructions"]


@respx.mock
def test_a_high_impact_gotcha_arrives_whole_and_invalid_ones_do_not(
    client, two_users, session, monkeypatch
):
    """A gotcha is only worth its budget if it says what breaks, why, and how
    to see it again -- with provenance, so an agent cannot reason it away. All
    four fields must reach the wire on the valid one; each of the three items
    missing one required field must reach nothing."""
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    _open_project(client, headers)
    _structured_mode(monkeypatch)
    user_bank, project_bank = _bank_ids(session, two_users[0]["user_id"], PROJECT)
    _mock_models(user_bank, [])
    _mock_models(project_bank, [_profile_model(_reflect("project", project_categories()))])

    body = _get_brief(client, headers, project_slug=PROJECT, tier="full")

    delivered = body["instructions"]
    gotcha_line = next(line for line in _project_items(delivered) if P_GOTCHA_CLAIM in line)
    assert f"failure: {P_GOTCHA_FAILURE}" in gotcha_line
    assert f"cause: {P_GOTCHA_CAUSE}" in gotcha_line
    assert f"reproduction: {P_GOTCHA_REPRODUCTION}" in gotcha_line
    assert f"provenance: {P_GOTCHA_PROVENANCE}" in gotcha_line
    for claim in PROJECT_EXCLUDED:
        assert claim not in delivered, claim
    for claim in PROJECT_DELIVERED:
        assert claim in delivered, claim


@respx.mock
def test_a_negative_constraint_keeps_its_rank_and_its_reason(
    client, two_users, session, monkeypatch
):
    """An explicit "never" is the item an agent is most likely to reason past,
    so it ranks with the gotchas and carries the reason it exists. Losing
    either -- the rank or the provenance -- turns a constraint into a
    suggestion."""
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    _curated_user_bank(session, two_users[0]["user_id"])

    lines = _user_items(_get_brief(client, headers, tier="full")["instructions"])

    assert U_NEGATIVE_CONSTRAINT in lines[0], lines
    assert f"provenance: {U_NEGATIVE_PROVENANCE}" in lines[0]
    # Provenance is spent only where it changes behaviour. A plain positive
    # convention carries one in the fixture and must not render it.
    convention = next(line for line in lines if U_OBSERVED_CONVENTION in line)
    assert "provenance:" not in convention


# ---------------------------------------------------------------------------
# Step 3, bullet 1: the 15/25 budgets, counted on the delivered text.
# ---------------------------------------------------------------------------


def _filler(prefix, count, *, detail="", **overrides):
    """`count` distinct, eligible items.

    `detail` sets how much budget each one costs. Empty (the default) keeps
    every line short, which is what a COMPILER budget test needs: a claim long
    enough to lose a host allocation race would make a truncation at 15 or 25
    unfalsifiable. The allocation tests pass a realistic sentence instead,
    because a tier that comfortably fits everything proves nothing about what
    it protects when it cannot.
    """
    return [
        _item(
            claim=f"{prefix} rule {index:02d} for the invented bank.{detail}",
            evidence_ids=[f"{prefix}-{index:02d}"],
            **overrides,
        )
        for index in range(count)
    ]


# A claim at the length synthesis actually produces, used only where the point
# is that the tier runs out of room.
REALISTIC = (
    " Written out at the length a synthesized claim actually reaches, so the"
    " tier below has to choose what it can carry."
)


@respx.mock
def test_a_user_profile_over_budget_delivers_fifteen_items(
    client, two_users, session, monkeypatch
):
    _structured_mode(monkeypatch)
    offered = _filler("user", 20, kind="preference", origin="stated")
    user_bank, _ = _bank_ids(session, two_users[0]["user_id"])
    _mock_models(user_bank, [_profile_model(_reflect("user", {"preferences": offered}))])

    delivered = _get_brief(client, two_users[0]["headers"], tier="full")["instructions"]

    lines = _user_items(delivered)
    assert len(lines) == profiles.USER_PROFILE_BUDGET == 15
    # Every delivered line is one of the offered claims, and five were cut.
    claims = {item["claim"] for item in offered}
    assert set(lines) <= claims
    assert len([claim for claim in claims if claim in delivered]) == 15


@respx.mock
def test_a_project_profile_over_budget_delivers_twenty_five_items(
    client, two_users, session, monkeypatch
):
    headers = two_users[0]["headers"]
    _open_project(client, headers, metadata=False)
    _structured_mode(monkeypatch)
    offered = _filler("project", 30)
    user_bank, project_bank = _bank_ids(session, two_users[0]["user_id"], PROJECT)
    _mock_models(user_bank, [])
    _mock_models(project_bank, [_profile_model(_reflect("project", {"conventions": offered}))])

    delivered = _get_brief(client, headers, project_slug=PROJECT, tier="full")["instructions"]

    lines = _project_items(delivered)
    assert len(lines) == profiles.PROJECT_PROFILE_BUDGET == 25
    claims = {item["claim"] for item in offered}
    assert set(lines) <= claims


@respx.mock
def test_a_higher_ranked_item_displaces_the_weakest_one_at_a_full_budget(
    client, two_users, session, monkeypatch
):
    """A full profile is not a closed door: a new item enters by displacing the
    current last one, and which one that is is decided by rank and support, not
    by arrival order.

    Fourteen preferences carry two pieces of evidence each; one carries one, so
    it is unambiguously the weakest without this test having to know a claim
    hash. Adding one negative constraint -- rank 0 against a preference's rank
    3 -- must push exactly that item off the wire.
    """
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    user_bank, _ = _bank_ids(session, two_users[0]["user_id"])

    supported = [
        _item(
            claim=f"Invented preference {index:02d} for the sandbox operator.",
            kind="preference",
            origin="stated",
            evidence_ids=[f"mem-s-{index:02d}-a", f"mem-s-{index:02d}-b"],
        )
        for index in range(14)
    ]
    weakest = _item(
        claim="Invented preference 99, backed by a single memory.",
        kind="preference",
        origin="stated",
        evidence_ids=["mem-s-99"],
    )
    newcomer = _item(
        claim=U_NEGATIVE_CONSTRAINT,
        kind="preference",
        origin="stated",
        negative=True,
        provenance=U_NEGATIVE_PROVENANCE,
        evidence_ids=["mem-s-new"],
    )

    _mock_models(
        user_bank,
        [_profile_model(_reflect("user", {"preferences": [*supported, weakest]}))],
    )
    before = _get_brief(client, headers, tier="full")["instructions"]

    _mock_models(
        user_bank,
        [
            _profile_model(
                _reflect(
                    "user",
                    {"preferences": [*supported, weakest], "constraints": [newcomer]},
                ),
                refreshed=NOW + timedelta(hours=1),
            )
        ],
    )
    after = _get_brief(client, headers, tier="full")["instructions"]

    assert len(_user_items(before)) == 15
    assert weakest["claim"] in before

    assert len(_user_items(after)) == 15
    assert weakest["claim"] not in after
    assert U_NEGATIVE_CONSTRAINT in after
    # Nothing else moved: displacement replaced one item, it did not reshuffle
    # the profile.
    for item in supported:
        assert item["claim"] in after, item["claim"]


# ---------------------------------------------------------------------------
# Step 3, bullet 5: Project Metadata and Working State are separate, and
# survive host allocation against a profile at its ceiling.
# ---------------------------------------------------------------------------


@respx.mock
def test_mandatory_records_survive_two_profiles_at_their_ceiling(
    client, two_users, session, monkeypatch
):
    """The real allocation test: 15 user items and 25 project items offered at
    once, which is more than a Full tier can carry.

    Project Metadata and Working State are records, not memory -- one comes
    from the Project row, one from its own domain table -- and neither may lose
    a budget race to synthesized profile lines. The profile is the optional
    material, so the profiles must be the thing that gets cut here; if they
    arrive whole, the tier was never actually under pressure and this test
    proves nothing.
    """
    headers = two_users[0]["headers"]
    _open_project(client, headers)
    _checkpoint(client, headers, current_direction="reconciling the invented balances")
    _structured_mode(monkeypatch)
    user_bank, project_bank = _bank_ids(session, two_users[0]["user_id"], PROJECT)
    user_offered = _filler(
        "user", 15, detail=REALISTIC, kind="preference", origin="stated"
    )
    project_offered = _filler("project", 25, detail=REALISTIC)
    _mock_models(user_bank, [_profile_model(_reflect("user", {"preferences": user_offered}))])
    _mock_models(
        project_bank,
        [_profile_model(_reflect("project", {"conventions": project_offered}))],
    )

    body = _get_brief(
        client, headers, project_slug=PROJECT, workspace_id=WORKSPACE_A, tier="full"
    )

    delivered = body["instructions"]
    assert body["sections"] == {"user": True, "project": True, "working_state": True}

    # Project Metadata: all three lines, exactly as the row holds them.
    assert _block(delivered, brief._ORIENTATION_HEADING).split("\n")[1:] == [
        f"project: {PROJECT}",
        f"spec: {P_SPEC}",
        f"purpose: {P_PURPOSE}",
    ]
    # Working State: its floor line and the two lines that mark how stale it
    # might be, none of which a profile may crowd out.
    working_state = _block(delivered, brief._WORKING_STATE_HEADING)
    assert "objective: close the invented ledger sprint" in working_state
    assert "age:" in working_state
    assert "source session:" in working_state

    # The pressure was real: both profiles lost lines to make room.
    assert 0 < len(_user_items(delivered)) < 15
    assert 0 < len(_project_items(delivered)) < 25

    # Nothing crossed between the three compilers.
    assert "objective:" not in "\n".join(_user_items(delivered) + _project_items(delivered))
    assert "spec:" not in working_state


@respx.mock
def test_the_index_tier_keeps_its_records_under_a_host_budget(
    client, two_users, session, monkeypatch
):
    """The Index tier rides a host's `instructions` field under a much smaller
    budget and a per-section line cap, so it is where a mandatory record is
    most likely to be squeezed out. Same three-way separation, same answer."""
    headers = two_users[0]["headers"]
    _open_project(client, headers)
    _checkpoint(client, headers)
    _structured_mode(monkeypatch)
    user_bank, project_bank = _bank_ids(session, two_users[0]["user_id"], PROJECT)
    user_offered = _filler(
        "user", 15, detail=REALISTIC, kind="preference", origin="stated"
    )
    project_offered = _filler("project", 25, detail=REALISTIC)
    _mock_models(user_bank, [_profile_model(_reflect("user", {"preferences": user_offered}))])
    _mock_models(
        project_bank,
        [_profile_model(_reflect("project", {"conventions": project_offered}))],
    )

    body = _get_brief(
        client,
        headers,
        project_slug=PROJECT,
        workspace_id=WORKSPACE_A,
        tier="index",
        host="claude-code",
    )

    delivered = body["instructions"]
    assert len(delivered) <= brief.budget_for("claude-code")
    assert body["sections"] == {"user": True, "project": True, "working_state": True}
    assert _block(delivered, brief._ORIENTATION_HEADING).split("\n")[1:] == [
        f"project: {PROJECT}",
        f"spec: {P_SPEC}",
        f"purpose: {P_PURPOSE}",
    ]
    assert "objective: close the invented ledger sprint" in _block(
        delivered, brief._WORKING_STATE_HEADING
    )
    # Both profiles were cut hard by the smaller budget while the two records
    # above came through whole. `INDEX_CAPS` is a cap on each section's FIRST
    # share, not a ceiling -- the leftover pass grows sections past it on
    # purpose (pinned in tests/test_brief.py) -- so the assertion here is that
    # the profiles paid for the records, not that they stopped at five.
    assert 0 < len(_user_items(delivered)) < 15
    assert 0 < len(_project_items(delivered)) < 25


@respx.mock
def test_project_metadata_is_never_sourced_from_the_profile(
    client, two_users, session, monkeypatch
):
    """A synthesized claim shaped like an orientation line is a claim. It is
    delivered as one, in the profile block, and the orientation block still
    reports only what the Project row holds."""
    headers = two_users[0]["headers"]
    _open_project(client, headers)
    _structured_mode(monkeypatch)
    user_bank, project_bank = _bank_ids(session, two_users[0]["user_id"], PROJECT)
    _mock_models(user_bank, [])
    _mock_models(project_bank, [_profile_model(_reflect("project", project_categories()))])

    delivered = _get_brief(client, headers, project_slug=PROJECT, tier="full")["instructions"]

    assert _block(delivered, brief._ORIENTATION_HEADING).split("\n")[1:] == [
        f"project: {PROJECT}",
        f"spec: {P_SPEC}",
        f"purpose: {P_PURPOSE}",
    ]
    assert P_HOSTILE_ORIENTATION in "\n".join(_project_items(delivered))


# ---------------------------------------------------------------------------
# Step 3, bullet 6: a correction retires old current truth without deleting
# anything.
# ---------------------------------------------------------------------------


@respx.mock
def test_a_correction_retires_old_truth_without_deleting_its_evidence(
    client, two_users, session, monkeypatch
):
    """What "correction" means at this boundary.

    Before: the retired claim is the grounded one and is delivered. After a
    refresh that absorbed the correction, the synthesized document no longer
    carries it and `based_on` no longer cites its evidence, so it compiles to
    nothing while the corrected claim takes its place.

    Nothing was destroyed to achieve that. This test issues no delete, curate
    or clear call -- asserted below over every upstream request the brief made
    -- and the evidence memory behind the retired claim is still recallable
    from the bank afterwards, which is the whole point of keeping supersession
    in synthesis rather than in deletion.
    """
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    user_bank, _ = _bank_ids(session, two_users[0]["user_id"])

    # Before the correction: the now-retired claim is current truth, and the
    # corrected claim does not exist yet.
    _mock_models(
        user_bank,
        [
            _profile_model(
                _reflect("user", user_categories(corrected=False), ungrounded=())
            )
        ],
    )
    before = _get_brief(client, headers, tier="full")["instructions"]

    # After: same corpus, one refresh later. The retired claim is still in the
    # document -- nothing deleted it -- but this reflection was not built from
    # its evidence, so it has no support left.
    _mock_models(
        user_bank,
        [
            _profile_model(
                _reflect("user", user_categories(), ungrounded=(SUPERSEDED_EVIDENCE,)),
                refreshed=NOW + timedelta(days=1),
            )
        ],
    )
    after = _get_brief(client, headers, tier="full")["instructions"]

    assert U_SUPERSEDED_TRUTH in before
    assert U_CORRECTED_TRUTH not in before
    assert U_SUPERSEDED_TRUTH not in after
    assert U_CORRECTED_TRUTH in after

    # Reads stay reads: every upstream call the two briefs made was a GET, so
    # no evidence, model or bank was mutated to retire that claim.
    assert [call.request.method for call in respx.calls] == ["GET", "GET"]

    # And the evidence itself is still there to be recalled.
    respx.post(f"{BASE}/v1/default/banks/{user_bank}/memories/recall").mock(
        return_value=httpx.Response(
            200,
                json={
                    "results": [
                        {
                            "id": SUPERSEDED_EVIDENCE,
                            "text": U_SUPERSEDED_TRUTH,
                            "type": "world",
                        }
                    ]
                },
        )
    )
    recalled = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "where do release notes go"},
        headers=headers,
    )
    assert recalled.status_code == 200, recalled.text
    assert U_SUPERSEDED_TRUTH in recalled.text


# ---------------------------------------------------------------------------
# Step 3, bullet 7: structured revisions and cached sections are isolated per
# workspace and per credential owner.
# ---------------------------------------------------------------------------


@respx.mock
def test_two_workspaces_hold_independent_revisions_and_no_shared_content(
    client, two_users, session, monkeypatch
):
    """Two worktrees of one project, one structured profile between them. The
    revision is what a consumer's cache keys on, so the workspaces must count
    separately -- and one workspace's Working State must never appear in the
    other's tier."""
    headers = two_users[0]["headers"]
    _open_project(client, headers)
    _checkpoint(client, headers, workspace_id=WORKSPACE_A, objective="invented objective A")
    _checkpoint(
        client, headers, workspace_id=WORKSPACE_B, session_id="sess-2",
        objective="invented objective B",
    )
    _structured_mode(monkeypatch)
    _, project_bank = _bank_ids(session, two_users[0]["user_id"], PROJECT)
    _curated_user_bank(session, two_users[0]["user_id"])
    _mock_models(project_bank, [_profile_model(_reflect("project", project_categories()))])

    a = _get_brief(client, headers, project_slug=PROJECT, workspace_id=WORKSPACE_A)
    b = _get_brief(client, headers, project_slug=PROJECT, workspace_id=WORKSPACE_B)

    assert "invented objective A" in a["instructions"]
    assert "invented objective B" not in a["instructions"]
    assert "invented objective B" in b["instructions"]
    assert "invented objective A" not in b["instructions"]
    assert a["workspace_id"] == WORKSPACE_A
    assert b["workspace_id"] == WORKSPACE_B
    # Separate sequences, each starting at its own 1.
    assert a["brief_revision"] == b["brief_revision"] == 1

    # A checkpoint in A moves A's revision and leaves B's where it was.
    _checkpoint(
        client, headers, workspace_id=WORKSPACE_A, checkpoint_seq=2,
        objective="invented objective A, revised",
    )
    assert (
        _get_brief(client, headers, project_slug=PROJECT, workspace_id=WORKSPACE_A)[
            "brief_revision"
        ]
        == 2
    )
    assert (
        _get_brief(client, headers, project_slug=PROJECT, workspace_id=WORKSPACE_B)[
            "brief_revision"
        ]
        == 1
    )


@respx.mock
def test_two_credential_owners_never_share_a_profile_or_a_revision(
    client, two_users, session, monkeypatch
):
    """Structured delivery reads a bank chosen by the caller's credential. Two
    users with two banks must see two profiles and count two revision
    sequences -- a shared cache line here would hand one operator another's
    memory."""
    _structured_mode(monkeypatch)
    first_bank, _ = _bank_ids(session, two_users[0]["user_id"])
    second_bank, _ = _bank_ids(session, two_users[1]["user_id"])
    other_claim = "Answer the second invented operator in Portuguese."
    _mock_models(
        first_bank,
        [_profile_model(_reflect("user", user_categories(), ungrounded=(SUPERSEDED_EVIDENCE,)))],
    )
    _mock_models(
        second_bank,
        [
            _profile_model(
                _reflect(
                    "user",
                    {"interaction": [
                        _item(claim=other_claim, kind="preference", origin="stated",
                              evidence_ids=["mem-o-01"])
                    ]},
                )
            )
        ],
    )

    first = _get_brief(client, two_users[0]["headers"])
    second = _get_brief(client, two_users[1]["headers"])

    assert U_STATED_PREFERENCE in first["instructions"]
    assert other_claim not in first["instructions"]
    assert other_claim in second["instructions"]
    assert U_STATED_PREFERENCE not in second["instructions"]
    assert first["brief_revision"] == second["brief_revision"] == 1

    # The first user's profile moves; the second user's revision does not.
    _mock_models(
        first_bank,
        [
            _profile_model(
                _reflect(
                    "user",
                    {"interaction": [
                        _item(claim="Answer the first invented operator in Catalan.",
                              kind="preference", origin="stated", evidence_ids=["mem-u-01"])
                    ]},
                ),
                refreshed=NOW + timedelta(hours=2),
            )
        ],
    )
    assert _get_brief(client, two_users[0]["headers"])["brief_revision"] == 2
    assert _get_brief(client, two_users[1]["headers"])["brief_revision"] == 1


@respx.mock
def test_a_master_key_reads_only_the_named_user_and_keeps_that_sequence(
    client, two_users, session, master_headers, monkeypatch
):
    """A master key has no memory of its own here: On-Behalf-Of names whose
    bank is read and whose revision sequence is stamped, so the same tier the
    user would have received is what comes back."""
    _structured_mode(monkeypatch)
    _curated_user_bank(session, two_users[0]["user_id"])
    _mock_models(_bank_ids(session, two_users[1]["user_id"])[0], [])

    as_user = _get_brief(client, two_users[0]["headers"])
    as_master = _get_brief(
        client, {**master_headers, "On-Behalf-Of": two_users[0]["user_id"]}
    )

    assert U_STATED_PREFERENCE in as_master["instructions"]
    assert as_master["instructions"] == as_user["instructions"]
    # Same (tenant, user, project, workspace) counter, not a second one.
    assert as_master["brief_revision"] == as_user["brief_revision"]


# ---------------------------------------------------------------------------
# Step 3, bullet 8: the curated behaviour case, structured against legacy.
#
# "Improves or remains equal, with no added noise" is measured here as two
# comparisons over the SAME underlying facts, delivered by the two modes:
#
#   signal:  every durable claim legacy delivers, structured delivers too
#            (structured's durable set is a superset of legacy's);
#   noise:   every claim that must not be delivered -- the two evidence-only
#            durability cells, a bare technical claim, an inferred one, and a
#            superseded one -- appears no more often under structured than
#            under legacy.
#
# Equality on both would pass. The legacy fixture below is not a straw man: it
# is what free-text synthesis of this same bank plausibly produces, including
# the four failures that motivated Phase 4 -- an observed habit promoted to a
# rule, an inference stated as fact, a bare technical detail, and a superseded
# claim kept alongside the one that replaced it, with nothing marking which is
# current.
# ---------------------------------------------------------------------------

LEGACY_PROSE = (
    f"{U_STATED_PREFERENCE}\n"
    f"{U_CONFIRMED_PREFERENCE}\n"
    f"{U_OBSERVED_CONVENTION}\n"
    f"{U_NEGATIVE_CONSTRAINT}\n"
    # The four failure modes Phase 4 exists to remove, plus the superseded
    # claim sitting next to the one that replaced it with nothing saying which
    # of the two is current.
    f"{U_OBSERVED_PREFERENCE}\n"
    f"{U_OBSERVED_DECISION}\n"
    f"{U_TECHNICAL_CLAIM}\n"
    f"{U_INFERRED_CLAIM}\n"
    f"{U_SUPERSEDED_TRUTH}\n"
    f"{U_CORRECTED_TRUTH}"
)


@respx.mock
def test_structured_delivery_keeps_the_signal_and_drops_the_noise(
    client, two_users, session, monkeypatch
):
    _structured_mode(monkeypatch)
    headers = two_users[0]["headers"]
    user_bank, _ = _bank_ids(session, two_users[0]["user_id"])
    _mock_models(
        user_bank,
        [_profile_model(_reflect("user", user_categories(), ungrounded=(SUPERSEDED_EVIDENCE,)))],
    )
    structured = _get_brief(client, headers, tier="full")["instructions"]

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_PROFILE_DELIVERY_MODE", "legacy")
    get_settings.cache_clear()
    respx.clear()
    _mock_models(user_bank, [_legacy_model(LEGACY_PROSE)])
    legacy = _get_brief(client, headers, tier="full")["instructions"]

    signal_legacy = {claim for claim in USER_DELIVERED if claim in legacy}
    signal_structured = {claim for claim in USER_DELIVERED if claim in structured}
    noise_legacy = {claim for claim in USER_EXCLUDED if claim in legacy}
    noise_structured = {claim for claim in USER_EXCLUDED if claim in structured}

    # No durable claim was lost in the move.
    assert signal_legacy <= signal_structured
    assert signal_structured == USER_DELIVERED
    # And every one of legacy's five failures is gone, none added.
    assert noise_structured <= noise_legacy
    assert noise_structured == set()
    assert noise_legacy == USER_EXCLUDED


# ---------------------------------------------------------------------------
# Step 3, bullet 9: production stays off, by default, with nothing to opt out
# of.
# ---------------------------------------------------------------------------


def test_every_phase_3_and_4_activation_flag_defaults_off():
    """Read off a bare Settings, not off the env this suite happens to run
    under: the default is the thing a deployment inherits when it says
    nothing."""
    settings = Settings(master_key_hash="0" * 64)

    assert settings.profile_delivery_mode == "legacy"
    assert settings.capture_worker_enabled is False
    assert settings.capture_correction_refresh_enabled is False


@respx.mock
def test_a_bank_holding_both_models_serves_the_legacy_one_by_default(
    client, two_users, session
):
    """No environment override anywhere. A bank that has already been
    provisioned with a structured profile still serves prose, and the profile
    is not read -- so landing Phase 4 changes nothing for a deployment that
    has not opted in."""
    assert get_settings().profile_delivery_mode == "legacy"
    user_bank, _ = _bank_ids(session, two_users[0]["user_id"])
    _mock_models(
        user_bank,
        [
            _legacy_model("Ask before planning."),
            _profile_model(_reflect("user", user_categories(),
                                    ungrounded=(SUPERSEDED_EVIDENCE,))),
        ],
    )

    body = _get_brief(client, two_users[0]["headers"], tier="full")

    assert body["sections"]["user"] is True
    assert "Ask before planning." in body["instructions"]
    for claim in USER_DELIVERED:
        assert claim not in body["instructions"], claim
