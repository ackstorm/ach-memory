from uuid import uuid4

import pytest

from memory import read, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.errors import MemoryNotFound, UnsupportedCapability


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


def _seed(db, principal, backend, content: str, memory_type: str = "fact") -> str:
    result = retain.submit(
        db, principal,
        retain.RetainRequest(scope="user", content=content, memory_type=memory_type,
                              basis="human_explicit", operation_id=uuid4(), wait=True),
        backend=backend,
    )
    return result.memory_id


def test_recall_drops_hits_below_the_floor(db, principal, backend, monkeypatch):
    high_id = _seed(db, principal, backend, "alpha bravo charlie delta echo")
    _seed(db, principal, backend, "alpha zulu yankee xray")

    monkeypatch.setattr(read.get_settings(), "recall_min_semantic", 0.5)

    result = read.recall(
        db, principal, read.RecallRequest(scope="user", query="alpha bravo charlie delta"), backend=backend
    )

    assert [hit.memory_id for hit in result.items] == [high_id]
    assert result.truncated is False


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
    backend.invalidate("user_usr_juan", memory_id, reason="test")

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


def test_reads_on_a_project_nobody_retained_into_are_empty(db, principal):
    from memory.backend.fake import fake_backend

    empty = read.recall(db, principal, read.RecallRequest(scope="project", project_slug="github.com-x-never", query="anything"), backend=fake_backend)
    assert empty.items == () and empty.total == 0
    listed = read.list_memories(db, principal, read.ListRequest(scope="project", project_slug="github.com-x-never"), backend=fake_backend)
    assert listed.items == () and listed.total == 0
