"""Checks for the benchmark scripts' own logic.

The benchmark harnesses talk to a live two-arm stack, so their scoring
decisions are the part that can silently go wrong without anything failing
loudly: a matcher that never matches reports 0% recall for every arm and
looks like a finding rather than a bug. These pin the pure functions.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load(name: str, monkeypatch):
    monkeypatch.setenv("MEMORY_OPERATOR_TOKEN", "unit-test-operator-token")
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"bench_target_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: these modules use `from __future__ import
    # annotations`, and dataclasses resolves a field's annotation through
    # `sys.modules[cls.__module__]`, which is None for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_table_renders_aligned_columns(monkeypatch):
    benchlib = _load("benchlib", monkeypatch)
    out = benchlib.table(["A", "Bee"], [["long-cell", "x"]])
    lines = out.splitlines()
    assert len(lines) == 3
    assert len({len(line) for line in lines}) == 1, out


def test_samples_reports_spread_only_when_there_is_more_than_one_run(monkeypatch):
    benchlib = _load("benchlib", monkeypatch)
    one = benchlib.Samples()
    one.add(0.5)
    assert one.render(pct=True) == "50.0%"

    many = benchlib.Samples()
    for value in (0.4, 0.6):
        many.add(value)
    assert "+/-" in many.render(pct=True)


def test_out_of_scope_is_a_distinct_verdict(monkeypatch):
    """The differential must be able to say "absent by design" rather than
    "failed". Collapsing the two is what makes a comparison rigged."""
    benchlib = _load("benchlib", monkeypatch)
    assert benchlib.MARK["OUT_OF_SCOPE"] != benchlib.MARK["NOT_ENFORCED"]
    assert set(benchlib.MARK) == {"ENFORCED", "NOT_ENFORCED", "OUT_OF_SCOPE", "ERROR"}


def test_extracted_text_still_matches_its_corpus_fact(monkeypatch):
    """Hindsight rewrites what it stores. The matcher has to survive that,
    or every arm scores zero and the benchmark reports a false finding."""
    quality = _load("bench_quality", monkeypatch)
    fact = {"match": ["idempotency", "24"]}
    extracted = "Payments stores idempotency keys for 24 hours."
    assert quality.is_same_fact(fact, extracted)


def test_the_contradicting_project_fact_does_not_count_as_a_match(monkeypatch):
    """p01 (24 hours) and s01 (7 days) differ in few words. If the matcher
    conflates them, contamination reads as recall and the headline number is
    backwards."""
    quality = _load("bench_quality", monkeypatch)
    facts, _ = quality.load_corpus()
    by_id = {f["id"]: f for f in facts}
    # The real corpus rows, not hand-written stand-ins: this is the pairing
    # a similarity threshold got wrong (0.625 containment, scored as a match).
    assert not quality.is_same_fact(by_id["p01"], by_id["s01"]["text"])
    assert not quality.is_same_fact(by_id["s01"], by_id["p01"]["text"])
    assert quality.is_same_fact(by_id["p01"], by_id["p01"]["text"])
    assert quality.is_same_fact(by_id["s01"], by_id["s01"]["text"])


def test_corpus_questions_reference_facts_that_exist(monkeypatch):
    quality = _load("bench_quality", monkeypatch)
    facts, questions = quality.load_corpus()
    ids = {f["id"] for f in facts}
    assert len(questions) >= 20
    for q in questions:
        for ref in list(q["expect"]) + list(q.get("must_not", [])):
            assert ref in ids, f"{q['id']} references unknown fact {ref}"
        assert q["expect"], f"{q['id']} expects nothing"


def test_every_question_scope_is_seeded_by_some_fact(monkeypatch):
    """A question aimed at a scope nothing was written to would score 0%
    for every arm and look like a retrieval failure."""
    quality = _load("bench_quality", monkeypatch)
    facts, questions = quality.load_corpus()
    seeded = {(f["scope"], f.get("project")) for f in facts}
    for q in questions:
        assert (q["scope"], q.get("project")) in seeded, q["id"]


@pytest.mark.parametrize(
    "arm,expect_shared",
    [("vanilla-shared", True), ("vanilla-sharded", False)],
)
def test_shared_arm_collapses_every_scope_into_one_bank(arm, expect_shared, monkeypatch):
    quality = _load("bench_quality", monkeypatch)
    payments = {"scope": "project", "project": "payments", "owner": "alice"}
    search = {"scope": "project", "project": "search", "owner": "alice"}
    same = quality.bank_for(arm, payments, "t") == quality.bank_for(arm, search, "t")
    assert same is expect_shared


def test_items_finds_the_result_list_in_every_real_envelope(monkeypatch):
    """Each shape here was observed live. An unrecognised envelope returns []
    which reads as a finding ("0 audit rows") rather than a harness bug, so
    every one of them is pinned."""
    bench = _load("bench", monkeypatch)
    assert bench._items({"result": {"items": [1, 2]}}) == [1, 2]
    assert bench._items({"result": {"hits": [{"a": 1}]}}) == [{"a": 1}]
    assert bench._items([{"action": "memory.recall"}]) == [{"action": "memory.recall"}]
    assert bench._items({"changes": [{"at": "now"}]}) == [{"at": "now"}]
    assert bench._items({"results": [1]}) == [1]
    assert bench._items({"nothing": 1}) == []
    assert bench._items("a string") == []


def test_a_replay_that_stored_nothing_is_inconclusive_not_a_pass(monkeypatch):
    """Zero rows reads as "no duplicate" and would score as enforced while
    actually meaning the arm stored nothing -- which is what the vanilla arm
    did for a whole run while its retain was returning 400."""
    bench = _load("bench", monkeypatch)
    assert bench._replay_verdict(0, "a replay").verdict == "ERROR"
    assert bench._replay_verdict(1, "a replay").verdict == "ENFORCED"
    assert bench._replay_verdict(2, "a replay").verdict == "NOT_ENFORCED"


def test_no_corpus_fact_matches_a_different_corpus_fact(monkeypatch):
    """The whole benchmark rests on telling paired facts apart. If any fact's
    match terms also fire on another fact's text, contamination and recall
    become the same measurement."""
    quality = _load("bench_quality", monkeypatch)
    facts, _ = quality.load_corpus()
    collisions = [
        (a["id"], b["id"])
        for a in facts
        for b in facts
        if a["id"] != b["id"] and quality.is_same_fact(a, b["text"])
    ]
    assert not collisions, collisions


def test_every_fact_matches_its_own_text(monkeypatch):
    quality = _load("bench_quality", monkeypatch)
    facts, _ = quality.load_corpus()
    assert [f["id"] for f in facts if not quality.is_same_fact(f, f["text"])] == []


def test_spread_is_measured_across_repeats_not_across_questions(monkeypatch):
    """A 0/1 metric pooled per question reports its own Bernoulli spread:
    96% recall printed as "96.0 +/- 19.7%", where 19.7 is sqrt(.96*.04) and
    says nothing about run-to-run stability. Two repeats that each scored
    exactly 96% must therefore report +/- 0.0, not +/- 19.7."""
    quality = _load("bench_quality", monkeypatch)
    benchlib = _load("benchlib", monkeypatch)

    tallies = []
    for _ in range(2):
        tally = quality.RepeatTally()
        for i in range(25):
            tally.recall.append(0.0 if i == 0 else 1.0)  # 24/25 = 96%
        tallies.append(tally)

    samples = benchlib.Samples()
    import statistics
    for tally in tallies:
        samples.add(statistics.fmean(tally.recall))

    assert samples.mean == pytest.approx(0.96)
    assert samples.stdev == pytest.approx(0.0), "identical repeats must show no spread"
