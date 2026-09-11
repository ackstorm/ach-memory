"""What a recall is allowed to cut, and on which axis.

Three budgets sit between a bank and a caller, and two of them used to cut on
`final` -- the reranker's own ranking, which a measured false negative already
got wrong (0.000024 on a query that a fact with `semantic` 0.6135 plainly
answered).

Measured 2026-09-11 on a 129-claim bank:

* Upstream's `max_tokens` defaulted to 4096 because this service never sent
  one. That returned 161 of 258 entries and 109 of 129 claims -- 38% of the
  bank cut in reranker order, invisibly: Hindsight's recall response carries
  `results` and no truncation flag of any kind. The cut varied per query
  (122-167 entries) because a token budget is not a row count.
* This service then sliced the first 200 raw results and only afterwards
  applied the `semantic` floor, so a hit the floor would have admitted could
  be discarded for ranking badly among junk, without ever being judged. That
  half is insurance rather than a repair: slicing first left 58 of 258 entries
  unjudged on this bank and cost no answer, and it was unreachable at all
  while upstream's 4096 default kept responses under 200 entries. Raising the
  budget is what makes the bound live, which is why both are here.

The floor is the only one of the three on an axis that means the same thing on
every query, so it is the one that narrows now. The remaining bounds are on
work, not on relevance.
"""

import pytest

from memory import read_service
from memory.config import get_settings
from memory.read_service import (
    _MAX_HITS_NORMALIZED,
    _MAX_RAW_RESULTS_SCANNED,
    _RECALL_MAX_TOKENS,
)

ANSWER = "Alice pins Python dependencies with uv and never uses pip directly."


def _raw(text, *, memory_id, semantic, final=1.0):
    return {
        "id": memory_id,
        "text": text,
        "type": "world",
        "tags": ["type:fact", "basis:human_explicit", "schema:ach-retain-v1"],
        "document_id": f"ach-retain-{memory_id}",
        "scores": {"final": final, "reranker": 0.5, "semantic": semantic},
    }


def _junk(n, *, semantic=0.10):
    """Entries the floor refuses. Ranked above the answer on purpose: that is
    the whole failure -- upstream's order is not the floor's order."""
    return [
        _raw(f"In release {2000 + i}, the billing exporter runs nightly.",
             memory_id=f"junk-{i}", semantic=semantic, final=1.0 - i * 1e-6)
        for i in range(n)
    ]


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


def test_the_answer_survives_five_hundred_junk_entries_ranked_above_it(spy):
    """The defect, in one assertion. 500 entries the floor refuses used to
    fill the 200-slice first, so the floor never saw position 500 at all.
    Constructed, not measured: the calibration bank never ranked an answer
    that deep. The reranker false negative that scored a genuine answer at
    0.000024 is what says it can happen."""
    spy.results = [*_junk(500), _raw(ANSWER, memory_id="answer", semantic=0.72, final=0.0001)]

    hits = read_service._recall_hits(
        "bank-1", "how does Alice manage Python dependencies", "current", None
    )

    assert [hit.memory_id for hit in hits] == ["answer"]


def test_junk_does_not_consume_the_normalization_budget(spy):
    """`_MAX_HITS_NORMALIZED` counts hits built, not entries walked, so a bank
    that is mostly noise spends the budget on answers instead of on noise."""
    spy.results = [
        *_junk(900),
        *[_raw(f"{ANSWER} ({i})", memory_id=f"answer-{i}", semantic=0.72) for i in range(5)],
    ]

    hits = read_service._recall_hits("bank-1", "q", "current", None)

    assert len(hits) == 5


def test_the_number_of_hits_built_is_still_bounded(spy):
    """The bound the old constant existed for, now on the thing it was
    defending: a huge or hostile response cannot make this service build
    unbounded pydantic models."""
    spy.results = [
        _raw(f"claim number {i}", memory_id=f"mem-{i}", semantic=0.72)
        for i in range(_MAX_HITS_NORMALIZED + 50)
    ]

    hits = read_service._recall_hits("bank-1", "q", "current", None)

    assert len(hits) == _MAX_HITS_NORMALIZED


def test_the_scan_itself_is_bounded(spy):
    """An admissible hit past `_MAX_RAW_RESULTS_SCANNED` is not returned, and
    that is deliberate rather than unnoticed: iterating an arbitrarily long
    upstream array is the work this bound protects. The floor admits ~12.7
    hits per query (measured), so the bound is reached only by a response far
    larger than `_RECALL_MAX_TOKENS` can produce."""
    spy.results = [
        *_junk(_MAX_RAW_RESULTS_SCANNED),
        _raw(ANSWER, memory_id="too-far", semantic=0.72),
    ]

    assert read_service._recall_hits("bank-1", "q", "current", None) == []


def test_the_result_budget_is_sent_upstream(spy):
    """Never sent at all before, so upstream applied its own 4096 default and
    cut 38% of a 129-claim bank in reranker order."""
    read_service._recall_hits("bank-1", "q", "current", None)

    assert spy.kwargs["max_tokens"] == _RECALL_MAX_TOKENS


def test_the_budget_is_well_clear_of_upstreams_default():
    """4096 is what cut the bank. A value at or below it would leave the
    measured truncation exactly where it was."""
    assert _RECALL_MAX_TOKENS >= 4 * 4096


def test_the_scan_bound_exceeds_what_the_budget_can_return():
    """~27 tokens per entry measured, so the budget tops out near 1200
    entries. A scan bound below that would reintroduce the same silent cut
    this service just moved off `final`."""
    assert _MAX_RAW_RESULTS_SCANNED > _RECALL_MAX_TOKENS / 27


def test_the_floor_is_still_the_only_relevance_judgement(spy, monkeypatch):
    """Neither bound may stand in for the floor: with the floor switched off,
    entries the floor would have refused are returned, which proves nothing
    else is quietly filtering on relevance."""
    spy.results = _junk(3)

    assert read_service._recall_hits("bank-1", "q", "current", None) == []

    monkeypatch.setattr(get_settings(), "recall_min_semantic", 0.0)
    assert len(read_service._recall_hits("bank-1", "q", "current", None)) == 3
