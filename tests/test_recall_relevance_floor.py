"""Recall withholds noise instead of padding the answer with it.

Hindsight ranks but never abstains: it returns its whole candidate set
however badly it scores. Measured against benchmarks/corpus.jsonl on a live
stack, one query came back with 68 hits whose expected answers scored a
median `final` of 1.08 while the rest sat at 0.00001 -- and `RecallHit` was
`extra="forbid"` with no score field, so all 68 reached the caller looking
equally confident.

Two filters, each on the score that can answer its question. The absolute
one is on `semantic`, a cosine similarity that means the same thing on every
query; the relative one is on `final`, which orders hits within one response
and is meaningless as a constant. Putting the absolute floor on `final`
first is what `make smoke` caught: a fact scoring 0.000024 on one query and
0.98 on another was withheld from a query it genuinely answered.
"""

import pytest

from memory import read_service
from memory.config import get_settings
from memory.read_models import RecallRequest
from memory.read_service import _normalize_hit, _passes_semantic_floor, _score


def _raw(**overrides):
    raw = {
        "id": "mem-1",
        "text": "The auth module signs EdDSA, never RS256.",
        "type": "world",
        "tags": ["type:convention", "basis:agent_verified", "schema:ach-retain-v1"],
        "scores": {"final": 1.0839, "reranker": 0.985, "semantic": 0.661},
    }
    raw.update(overrides)
    return raw


class _SpyClient:
    """Records the kwargs recall was called with; returns nothing."""

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


def test_a_hit_carries_the_score_it_was_ranked_by():
    hit = _normalize_hit(_raw())

    assert hit is not None
    assert hit.score == pytest.approx(1.0839)


def test_a_score_above_one_is_kept():
    """`final` combines reranker relevance with recency/temporal/proof
    boosts and is not normalised: 1.0997 is a real measured value, so a
    model that capped at 1.0 would reject the best hits there are."""
    assert _normalize_hit(_raw(scores={"final": 1.0997})).score == pytest.approx(1.0997)


@pytest.mark.parametrize(
    "scores",
    [None, {}, {"final": None}, {"final": "1.08"}, {"reranker": 0.9}, "not-a-dict"],
)
def test_a_missing_or_malformed_score_degrades_to_none(scores):
    """`scores` is nullable in upstream's own contract and absent entirely
    for source facts. Upstream shape is not ours to trust inside a read."""
    hit = _normalize_hit(_raw(scores=scores))

    assert hit is not None
    assert hit.score is None


def test_a_boolean_is_not_accepted_as_a_score():
    """`True` is an `int` to Python and would otherwise land as 1.0 --
    a perfect score invented out of a malformed payload."""
    assert _score({"scores": {"final": True}}, "final") is None


def test_the_absolute_floor_is_on_semantic_not_on_the_ranking_score():
    """The whole point of the correction `make smoke` forced. `final` is
    excellent at ordering and unusable as a constant: this hit's cross-encoder
    score is at the noise floor, but its cosine similarity says it is plainly
    about the query, and cosine means the same thing on every query."""
    weak_rank_real_match = {"scores": {"final": 0.000024, "semantic": 0.6135}}

    assert _passes_semantic_floor(weak_rank_real_match, 0.55) is True


def test_a_hit_about_nothing_is_withheld():
    assert _passes_semantic_floor({"scores": {"final": 0.9, "semantic": 0.48}}, 0.55) is False


def test_a_keyword_only_hit_survives_the_semantic_floor():
    """Upstream reports no semantic score for a hit its vector arm never
    surfaced. Dropping those would silently narrow recall to one arm."""
    assert _passes_semantic_floor({"scores": {"final": 0.9, "semantic": None}}, 0.55) is True
    assert _passes_semantic_floor({"scores": {"final": 0.9}}, 0.55) is True


def test_a_floor_of_zero_admits_everything():
    assert _passes_semantic_floor({"scores": {"semantic": 0.01}}, 0.0) is True


def test_the_floor_is_applied_here_and_never_pushed_upstream(spy):
    """Upstream's own `semantic` floor prunes the vector arm alone, so a
    keyword-surfaced hit would bypass it and the setting would mean two
    different things depending on which arm found the hit."""
    read_service._recall_hits("bank-1", "anything", "current", None)

    assert "min_scores" not in spy.kwargs


def _hit(score):
    from memory.read_models import RecallHit

    return RecallHit(memory_id="m", text="t", fact_type="world", state="valid", score=score)


def test_the_tail_below_a_fraction_of_the_best_hit_is_dropped():
    """The absolute floor answers "is anything relevant at all"; this answers
    "how much of what came back is tail". A real measured response ran
    1.08, 1.08, 0.75, 0.50 and then fell off a cliff to 0.013 and 0.003 --
    every one of those clearing the absolute floor by two orders of magnitude.
    """
    hits = [_hit(1.08), _hit(0.75), _hit(0.013), _hit(0.003)]

    kept = read_service._apply_relative_cut(hits, 0.01)

    assert [h.score for h in kept] == [1.08, 0.75, 0.013]


def test_an_unscored_hit_survives_the_relative_cut():
    """Unjudged is not judged badly, and upstream may send no scores."""
    kept = read_service._apply_relative_cut([_hit(1.0), _hit(None), _hit(0.0001)], 0.01)

    assert [h.score for h in kept] == [1.0, None]


def test_a_wholly_unscored_response_passes_untouched():
    """No scores means no reference to measure a ratio against."""
    hits = [_hit(None), _hit(None)]

    assert read_service._apply_relative_cut(hits, 0.5) == hits


def test_the_two_thresholds_are_read_independently(spy, monkeypatch):
    """Each answers a different question about a hit, so switching the floor
    off must NOT also switch the relative cut off. They were coupled while a
    request could override the floor; with that gone the coupling would just
    be one setting silently disabling another."""
    settings = get_settings()
    calls = []
    monkeypatch.setattr(
        read_service, "_apply_relative_cut", lambda hits, ratio: calls.append(ratio) or hits
    )
    monkeypatch.setattr(settings, "recall_min_semantic", 0.0)

    read_service._recall_hits("bank-1", "q", "current", None)

    assert calls == [settings.recall_relative_cut]


def test_the_configured_floor_is_the_one_applied(spy, monkeypatch):
    """The floor is not a constant in `_recall_hits`: a deployment that moves
    `MEMORY_RECALL_MIN_SEMANTIC` must actually change what recall withholds."""
    settings = get_settings()
    spy.results = [_raw(scores={"final": 1.0, "semantic": 0.58})]

    monkeypatch.setattr(settings, "recall_min_semantic", 0.55)
    assert len(read_service._recall_hits("bank-1", "q", "current", None)) == 1

    monkeypatch.setattr(settings, "recall_min_semantic", 0.60)
    assert read_service._recall_hits("bank-1", "q", "current", None) == []


def test_the_shipped_floor_is_the_decided_value():
    """0.60 answers every question in the calibration corpus while cutting
    the reply from 68 hits to 12.7 and removing 98.7% of what nonsense
    queries return. The room above it is thin and measured: 0.62 breaks
    `make smoke` (whose fact scores 0.6135) and 0.65 blinds a real question
    (best expected hit 0.6313). Pinned so neither ceiling is crossed without
    reading the table in `config` first."""
    assert get_settings().recall_min_semantic == 0.60


def test_the_smoke_regression_would_be_caught_here(spy):
    """`make smoke` retains one fact and recalls it. Under the first design
    the fact was withheld -- the cross-encoder scored it at 0.000024 while
    cosine said 0.6135 -- and `recall` answered with an empty set for a bank
    holding exactly the answer."""
    spy.results = [
        {
            "id": "mem-1",
            "text": "The mcp-smoke script pins its Python tooling with uv, never with pip.",
            "type": "world",
            "tags": ["schema:ach-retain-v1"],
            "scores": {"final": 0.000026, "reranker": 0.000024, "semantic": 0.6135},
        }
    ]

    hits = read_service._recall_hits("bank-1", "how are Python dependencies managed", "current", None)

    assert len(hits) == 1
    assert hits[0].score == pytest.approx(0.000026)


def test_relevance_is_never_caller_input():
    """A quality contract a caller can switch off is not a contract: the one
    caller that reads "too few results" and sets the floor to 0 puts the
    padding back for everything downstream of it. So neither our own former
    `min_score` override nor upstream's `min_scores` syntax is accepted --
    `extra="forbid"` turns both into a 422 rather than a silent no-op."""
    with pytest.raises(ValueError):
        RecallRequest(scope="user", query="q", min_score=0)
    with pytest.raises(ValueError):
        RecallRequest(scope="user", query="q", min_scores={"final": 0.5})


def test_a_keyword_only_hit_the_reranker_dismissed_is_withheld():
    """QA F-12: keyword-arm hits with no semantic score sailed past the floor
    at final 0.003-0.013 whenever nothing relevant existed."""
    raw = _raw(scores={"final": 0.0052, "reranker": 0.006, "keyword": 3.1})
    assert not _passes_semantic_floor(raw, 0.60, keyword_only_min_reranker=0.10)


def test_a_keyword_only_hit_the_reranker_endorsed_survives():
    raw = _raw(scores={"final": 0.71, "reranker": 0.68, "keyword": 3.1})
    assert _passes_semantic_floor(raw, 0.60, keyword_only_min_reranker=0.10)


def test_a_keyword_only_hit_with_no_reranker_score_is_still_unjudged():
    """RRF passthrough deployments report no reranker score; nothing judged
    the hit, so nothing withholds it."""
    raw = _raw(scores={"final": 0.0164, "keyword": 3.1})
    assert _passes_semantic_floor(raw, 0.60, keyword_only_min_reranker=0.10)


def test_the_reranker_floor_never_touches_a_semantically_surfaced_hit():
    raw = _raw(scores={"final": 0.000024, "reranker": 0.00002, "semantic": 0.66})
    assert _passes_semantic_floor(raw, 0.60, keyword_only_min_reranker=0.10)


def test_the_keyword_only_floor_reaches_recall_from_configuration(spy, monkeypatch):
    """The setting is read, not just declared. Mirrors QA F-12: when nothing
    relevant exists the relative cut has no good hit to measure against, so
    this floor is all that stands between keyword-only noise and the caller."""
    settings = get_settings()
    spy.results = [_raw(id="mem-junk", scores={"final": 0.0052, "reranker": 0.006, "keyword": 3.1})]

    assert read_service._recall_hits("bank", "query", "current", None, ()) == []

    monkeypatch.setattr(settings, "recall_keyword_only_min_reranker", 0.0)
    hits = read_service._recall_hits("bank", "query", "current", None, ())
    assert [hit.memory_id for hit in hits] == ["mem-junk"]
