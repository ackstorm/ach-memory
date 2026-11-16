from uuid import uuid4

import pytest

from memory import curation, read, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.errors import InvalidRequest, MemoryNotFound, UnsupportedCapability


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


def _seed(db, principal, backend, content: str, memory_type: str = "fact") -> str:
    result = retain.submit(
        db, principal,
        retain.RetainRequest(scope="user", title="T", content=content, memory_type=memory_type,
                              basis="human_explicit", operation_id=uuid4(), wait=True),
        backend=backend,
    )
    return result.memory_id


def test_recall_withholds_no_hit_however_low_it_scores(db, principal, backend):
    high_id = _seed(db, principal, backend, "alpha bravo charlie delta echo")
    low_id = _seed(db, principal, backend, "alpha zulu yankee xray")

    result = read.recall(
        db, principal, read.RecallRequest(scope="user", query="alpha bravo charlie delta"), backend=backend
    )

    assert [hit.memory_id for hit in result.items] == [high_id, low_id]
    assert result.items[1].score < 0.5 and result.truncated is False


def test_recall_filters_by_memory_type(db, principal, backend):
    fact_id = _seed(db, principal, backend, "A durable fact.", memory_type="fact")
    _seed(db, principal, backend, "A durable fact.", memory_type="preference")

    result = read.recall(
        db, principal,
        read.RecallRequest(scope="user", query="durable fact", memory_types=["fact"]),
        backend=backend,
    )

    assert [hit.memory_id for hit in result.items] == [fact_id]
    assert result.items[0].memory_type == "fact"


def test_list_valid_and_invalid_after_invalidate(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Will be forgotten.")
    curation.forget(db, principal, curation.CurationRequest(scope="user", memory_id=memory_id, reason="test"),
                    backend=backend)

    valid = read.list_memories(db, principal, read.ListRequest(scope="user"), backend=backend)
    invalid = read.list_memories(db, principal, read.ListRequest(scope="user", state="invalid"), backend=backend)

    assert memory_id not in [item.memory_id for item in valid.items]
    assert [item.memory_id for item in invalid.items] == [memory_id]
    assert invalid.items[0].state == "invalid"


def test_get_memory_found_and_not_found(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Findable content.")

    item = read.get_memory(db, principal, read.GetRequest(scope="user", memory_id=memory_id), backend=backend)
    assert item.content == "Findable content."
    assert item.state == "valid"

    with pytest.raises(MemoryNotFound):
        read.get_memory(db, principal, read.GetRequest(scope="user", memory_id="mem_unknown"), backend=backend)


def test_history_shows_the_retain_journal_row(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Historic content.")

    result = read.history(db, principal, read.HistoryRequest(scope="user", memory_id=memory_id), backend=backend)

    assert result.current is not None
    assert result.current.memory_id == memory_id
    assert [entry.action for entry in result.journal] == ["memory.retain"]


def test_reflect_raises_unsupported_when_the_backend_lacks_synthesis(db, principal, backend, monkeypatch):
    monkeypatch.setattr(backend, "capabilities", lambda: frozenset())

    with pytest.raises(UnsupportedCapability):
        read.reflect(db, principal, read.ReflectRequest(scope="user", query="anything"), backend=backend)


def test_reflect_passes_the_type_and_basis_filter_to_the_backend(db, principal, backend, monkeypatch):
    seen = {}

    def fake_reflect(bank_id, query, *, tag_groups):
        seen["groups"] = tag_groups
        return "an answer"

    monkeypatch.setattr(backend, "capabilities", lambda: frozenset({"synthesis"}))
    monkeypatch.setattr(backend, "reflect", fake_reflect, raising=False)

    read.reflect(
        db, principal,
        read.ReflectRequest(scope="user", query="what do we do about retries",
                             memory_types=["convention"], basis=["human_explicit"]),
        backend=backend,
    )

    assert seen["groups"] == (
        {"tags": ["type:convention"], "match": "any"},
        {"tags": ["basis:human_explicit"], "match": "any"},
    )


def test_reads_on_a_project_nobody_retained_into_are_empty(db, principal):
    from memory.backend.fake import fake_backend

    empty = read.recall(db, principal, read.RecallRequest(scope="project", project_slug="github.com-x-never", query="anything"), backend=fake_backend)
    assert empty.items == () and empty.total == 0
    listed = read.list_memories(db, principal, read.ListRequest(scope="project", project_slug="github.com-x-never"), backend=fake_backend)
    assert listed.items == () and listed.total == 0


def _seed_project(db, principal, backend, slug: str, content: str) -> str:
    result = retain.submit(
        db, principal,
        retain.RetainRequest(scope="project", project_slug=slug, title="T", content=content,
                              memory_type="decision", basis="human_explicit",
                              operation_id=uuid4(), wait=True),
        backend=backend,
    )
    return result.memory_id


def test_recall_scope_all_merges_both_banks_project_first_on_a_tie(db, principal, backend):
    """scope="all" is the whole point: the agent no longer has to guess which bank holds
    the answer. Equal scores keep the documented order, so the project-specific claim
    outranks the general user one."""
    user_id = _seed(db, principal, backend, "alpine images")
    project_id = _seed_project(db, principal, backend, "github.com-x-alpine", "alpine images")

    result = read.recall(
        db, principal,
        read.RecallRequest(scope="all", project_slug="github.com-x-alpine",
                            query="alpine images"),
        backend=backend,
    )

    assert [hit.memory_id for hit in result.items] == [project_id, user_id]
    assert [hit.memory_type for hit in result.items] == ["decision", "fact"]


def test_recall_scope_all_alternates_the_banks_by_rank_project_first(db, principal, backend):
    user_id = _seed(db, principal, backend, "alpine images")
    project_id = _seed_project(db, principal, backend, "github.com-x-alpine",
                                "alpine images pinned")

    result = read.recall(
        db, principal,
        read.RecallRequest(scope="all", project_slug="github.com-x-alpine",
                            query="alpine images"),
        backend=backend,
    )

    assert [hit.memory_id for hit in result.items] == [project_id, user_id]


def test_recall_keeps_the_engine_order_not_the_semantic_score(db, principal, backend, monkeypatch):
    first, second = (_seed(db, principal, backend, text) for text in ("alpha one", "alpha two"))
    hits = backend.recall("user_usr_juan", "alpha", tag_groups=(), limit=5)
    by_id = {h.memory_id: h for h in hits}
    reranked = [by_id[second].__class__(**{**by_id[second].__dict__, "score": 0.61}),
                by_id[first].__class__(**{**by_id[first].__dict__, "score": 0.99})]
    monkeypatch.setattr(backend, "recall", lambda *a, **kw: reranked)

    result = read.recall(db, principal, read.RecallRequest(scope="user", query="alpha"), backend=backend)

    assert [hit.memory_id for hit in result.items] == [second, first]


def _decide(probs):
    calls = []

    def fake(settings, text, questions):
        calls.append((text, questions))
        if isinstance(probs, Exception):
            raise probs
        return probs[: len(questions)]
    return fake, calls


@pytest.fixture
def decision(monkeypatch):
    monkeypatch.setattr(read.get_settings(), "llm_url", "http://gateway.test/v1")
    monkeypatch.setattr(read.get_settings(), "llm_decision_model", "ackstorm.decision")

    def use(probs):
        fake, calls = _decide(probs)
        monkeypatch.setattr(read.llm, "decide", fake)
        return calls
    return use


def test_recall_scores_the_first_ten_hits_without_dropping_or_reordering(db, principal, backend, decision):
    ids = [_seed(db, principal, backend, f"deploy step {i}") for i in range(12)]
    calls = decision([0.9, *[0.1] * 9])

    result = read.recall(db, principal, read.RecallRequest(scope="user", query="deploy step", max_results=12),
                         backend=backend)

    assert len(result.items) == 12 and {h.memory_id for h in result.items} == set(ids)
    assert [h.relevance for h in result.items] == [0.9, *[0.1] * 9, None, None]
    assert result.note is None
    text, questions = calls[0]
    assert "deploy step" in text and len(questions) == 10 and questions[0] == "Does memory [1] help answer the query?"


def test_recall_says_first_when_no_hit_clearly_answers(db, principal, backend, decision):
    _seed(db, principal, backend, "deploy step")
    decision([0.4])

    result = read.recall(db, principal, read.RecallRequest(scope="user", query="deploy"), backend=backend)

    assert result.note == read.NOTHING_CLEAR and len(result.items) == 1
    assert next(iter(result.model_dump())) == "note"


@pytest.mark.parametrize("failure", [RuntimeError("gateway down"), "slow"])
def test_recall_answers_without_relevance_when_the_model_fails_or_is_slow(db, principal, backend, decision,
                                                                          monkeypatch, failure):
    _seed(db, principal, backend, "deploy step")
    if failure == "slow":
        import time
        monkeypatch.setattr(read.get_settings(), "recall_relevance_timeout_seconds", 0.05)
        monkeypatch.setattr(read.llm, "decide", lambda *a: time.sleep(0.5) or [0.9])
    else:
        decision(failure)

    result = read.recall(db, principal, read.RecallRequest(scope="user", query="deploy"), backend=backend)

    assert [h.relevance for h in result.items] == [None] and result.note is None


def test_recall_scope_all_survives_a_project_nobody_retained_into(db, principal, backend):
    """The user half must still answer; an unknown project drops out silently."""
    user_id = _seed(db, principal, backend, "alpine images")

    result = read.recall(
        db, principal,
        read.RecallRequest(scope="all", project_slug="github.com-x-never",
                            query="alpine images"),
        backend=backend,
    )

    assert [hit.memory_id for hit in result.items] == [user_id]


def test_recall_scope_all_without_a_slug_searches_the_user_bank(db, principal, backend):
    """scope="project" without a slug is still an error; scope="all" just skips it."""
    user_id = _seed(db, principal, backend, "alpine images")

    result = read.recall(
        db, principal, read.RecallRequest(scope="all", query="alpine images"), backend=backend
    )

    assert [hit.memory_id for hit in result.items] == [user_id]


def test_recall_scope_project_without_a_slug_is_still_an_error(db, principal, backend):
    with pytest.raises(InvalidRequest):
        read.recall(db, principal, read.RecallRequest(scope="project", query="alpine images"),
                     backend=backend)
