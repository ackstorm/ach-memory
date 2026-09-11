"""One claim, one slot in a recall response.

Measured on a live stack against a 10-claim bank (2026-09-11): 13 retains of
10 distinct sentences produced 26 upstream entries, and the worst query came
back with 8 hits carrying 1 distinct claim -- 7 of 8 slots spent restating one
sentence. Two independent mechanisms, both reproduced here:

* every retained claim exists upstream twice, as the `world` fact and as
  Hindsight's own `observation` of it with the same text, and
  `resolve_filters` asks for both types;
* a re-retain of identical content is a NEW claim, because `accept_retain`
  compares `payload_hash` only against a row it already found by
  `operation_id`.

Neither threshold can help: identical text scores identically, so the floor
and the relative cut keep or drop every copy alike.
"""

import pytest

from memory import read_service
from memory.read_service import _collapse_duplicate_claims, _normalize_hit

CLAIM = "Deployments to production require two approvals from the platform team."
OTHER = "All log lines are JSON and carry a trace_id field."


def _raw(text=CLAIM, *, memory_id="mem-1", fact_type="world", final=1.08, **overrides):
    raw = {
        "id": memory_id,
        "text": text,
        "type": fact_type,
        "tags": ["type:fact", "basis:human_explicit", "schema:ach-retain-v1"],
        "scores": {"final": final, "reranker": 0.98, "semantic": 0.72},
    }
    raw.update(overrides)
    return raw


def _hits(*raws):
    hits = []
    for raw in raws:
        hit = _normalize_hit(raw)
        assert hit is not None, raw
        hits.append(hit)
    return hits


class _SpyClient:
    def __init__(self):
        self.kwargs = None
        self.results = []

    def recall(self, bank_id, query, **kwargs):
        self.kwargs = kwargs
        return {"results": self.results}


@pytest.fixture
def spy(monkeypatch):
    client = _SpyClient()
    monkeypatch.setattr(read_service, "get_client", lambda: client)
    return client


def test_the_fact_and_its_observation_twin_take_one_slot():
    """The 2x every bank pays on every query, with nothing re-retained. The
    observation's text is the fact's text plus upstream's own provenance
    suffix, which is why the comparison strips it."""
    kept = _collapse_duplicate_claims(
        _hits(
            _raw(memory_id="fact-1"),
            _raw(
                f"{CLAIM} (mentioned_at=2026-09-11 10:14:31.026616+00:00)",
                memory_id="obs-1",
                fact_type="observation",
            ),
        )
    )

    assert [hit.memory_id for hit in kept] == ["fact-1"]


def test_four_re_retains_of_one_sentence_take_one_slot():
    """Four distinct `operation_id`s, four upstream memories, one claim."""
    kept = _collapse_duplicate_claims(
        _hits(*[
            _raw(memory_id=f"mem-{i}", document_id=f"ach-retain-{i}") for i in range(4)
        ])
    )

    assert [hit.memory_id for hit in kept] == ["mem-0"]


def test_the_best_ranked_copy_wins_when_both_are_traceable():
    """Upstream orders by `final`, so among equally traceable copies the
    first is the claim's best. Nothing is merged: a hit is returned exactly
    as it arrived, because inventing a score or grafting one copy's
    `document_id` onto another would report a record that never existed."""
    best = _raw(memory_id="best", final=1.09, document_id="ach-retain-aaa")
    worse = _raw(memory_id="worse", final=0.40, document_id="ach-retain-bbb")

    kept = _collapse_duplicate_claims(_hits(best, worse))

    assert len(kept) == 1
    assert kept[0].memory_id == "best"
    assert kept[0].score == pytest.approx(1.09)
    assert kept[0].document_id == "ach-retain-aaa"
    assert kept[0].text == CLAIM


def test_the_traceable_copy_wins_over_a_better_ranked_untraceable_one():
    """The floor runs first and judges each copy separately, and the twins'
    scores sit within 0.002% of each other (1.0997851 vs 1.0997698, measured),
    so a fact can be withheld while its observation passes. First-wins then
    returned the observation -- 3 of 3 hits on one measured query, none with a
    `document_id` and each carrying upstream's `(mentioned_at=...)` suffix in
    the text the agent reads."""
    observation = _raw(
        f"{CLAIM} (mentioned_at=2026-09-11 10:14:31.026616+00:00)",
        memory_id="obs-1",
        fact_type="observation",
        final=1.0997698,
    )
    fact = _raw(memory_id="fact-1", final=1.0997851, document_id="ach-retain-aaa")

    kept = _collapse_duplicate_claims(_hits(observation, fact))

    assert [hit.memory_id for hit in kept] == ["fact-1"]
    assert kept[0].text == CLAIM


def test_an_untraceable_copy_is_still_returned_when_it_is_the_only_one():
    """Losing the claim would be worse than losing its provenance."""
    observation = _raw(
        f"{CLAIM} (mentioned_at=2026-09-11 10:14:31.026616+00:00)",
        memory_id="obs-1",
        fact_type="observation",
    )

    kept = _collapse_duplicate_claims(_hits(observation))

    assert [hit.memory_id for hit in kept] == ["obs-1"]


def test_distinct_claims_all_survive():
    kept = _collapse_duplicate_claims(
        _hits(
            _raw(memory_id="a"),
            _raw(OTHER, memory_id="b"),
            _raw(memory_id="c"),
        )
    )

    assert [hit.memory_id for hit in kept] == ["a", "b"]


def test_a_paraphrase_is_a_different_claim():
    """Three paraphrases of one fact measured as three distinct claims, and
    they are: collapsing them would decide on the caller's behalf that two
    sentences mean the same thing. Only identical text is a duplicate."""
    kept = _collapse_duplicate_claims(
        _hits(
            _raw("The staging database is reset every night at 03:00 UTC.", memory_id="a"),
            _raw("Staging's database gets wiped nightly at 3am UTC.", memory_id="b"),
        )
    )

    assert len(kept) == 2


def test_a_genuine_consolidation_is_never_dropped():
    """An observation that summarises several facts says something none of
    them says, so its text differs and it keeps its own slot."""
    kept = _collapse_duplicate_claims(
        _hits(
            _raw(memory_id="fact-1"),
            _raw(
                "Production changes are gated on platform-team review, which is "
                "why two approvals are required.",
                memory_id="obs-1",
                fact_type="observation",
            ),
        )
    )

    assert len(kept) == 2


def test_recall_collapses_before_the_relative_cut(spy):
    """Order matters only in one direction: the cut measures against the best
    score in the response, and duplicates of the best hit cannot change that
    maximum -- but they would otherwise be counted, compared and returned."""
    spy.results = [
        _raw(memory_id="fact-1", document_id="ach-retain-aaa"),
        _raw(
            f"{CLAIM} (mentioned_at=2026-09-11 10:14:31.026616+00:00)",
            memory_id="obs-1",
            fact_type="observation",
        ),
        _raw(OTHER, memory_id="other-1", final=1.02, document_id="ach-retain-bbb"),
    ]

    hits = read_service._recall_hits("bank-1", "how many approvals", "current", None)

    assert [hit.memory_id for hit in hits] == ["fact-1", "other-1"]


def test_the_worst_measured_query_returns_one_hit(spy):
    """The 2026-09-11 measurement, end to end: 8 hits, 1 claim. Four retains
    of one sentence, each with its observation twin."""
    spy.results = []
    for i in range(4):
        spy.results.append(_raw(memory_id=f"fact-{i}", document_id=f"ach-retain-{i}"))
        spy.results.append(
            _raw(
                f"{CLAIM} (mentioned_at=2026-09-11 10:14:3{i}.026616+00:00)",
                memory_id=f"obs-{i}",
                fact_type="observation",
            )
        )

    hits = read_service._recall_hits("bank-1", "how many approvals", "current", None)

    assert len(hits) == 1
    assert hits[0].memory_id == "fact-0"
