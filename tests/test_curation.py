from uuid import uuid4

import pytest

from memory import curation, read, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.errors import Forbidden, InvalidRequest, MemoryNotFound


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


def _seed(db, principal, backend, content: str, *, scope="user", project_slug=None, tags=()) -> str:
    result = retain.submit(
        db, principal,
        retain.RetainRequest(scope=scope, project_slug=project_slug, content=content, memory_type="fact",
                              basis="human_explicit", operation_id=uuid4(), tags=list(tags), wait=True),
        backend=backend,
    )
    return result.memory_id


def test_forget_then_list_shows_invalid(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Will be forgotten.")

    result = curation.forget(
        db, principal, curation.CurationRequest(scope="user", memory_id=memory_id, reason="stale"), backend=backend
    )

    assert result.status == "invalidated"
    listed = read.list_memories(db, principal, read.ListRequest(scope="user", state="invalid"), backend=backend)
    assert [item.memory_id for item in listed.items] == [memory_id]


def test_restore_makes_it_valid_again(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Will be restored.")
    curation.forget(db, principal, curation.CurationRequest(scope="user", memory_id=memory_id, reason="stale"),
                     backend=backend)

    result = curation.restore(
        db, principal, curation.CurationRequest(scope="user", memory_id=memory_id, reason="undo"), backend=backend
    )

    assert result.status == "valid"
    item = read.get_memory(db, principal, read.GetRequest(scope="user", memory_id=memory_id), backend=backend)
    assert item.state == "valid"


def test_correct_replaces_text_keeps_id_and_tags_and_journals_before_after(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Old text.", tags=("repo:group/app",))

    result = curation.correct(
        db, principal,
        curation.CorrectRequest(scope="user", memory_id=memory_id, reason="fix", content="New text."),
        backend=backend,
    )

    assert result.status == "valid"
    item = read.get_memory(db, principal, read.GetRequest(scope="user", memory_id=memory_id), backend=backend)
    assert item.memory_id == memory_id
    assert item.content == "New text."
    assert "repo:group/app" in item.tags

    history = read.history(db, principal, read.HistoryRequest(scope="user", memory_id=memory_id), backend=backend)
    correct_entry = next(e for e in history.journal if e.action == "memory.correct")
    assert correct_entry.details["before"] == "Old text."
    assert correct_entry.details["after"] == "New text."


def test_delete_then_get_not_found(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Will be deleted.")

    result = curation.delete(
        db, principal, curation.CurationRequest(scope="user", memory_id=memory_id, reason="gone"), backend=backend
    )

    assert result.status == "deleted"
    with pytest.raises(MemoryNotFound):
        read.get_memory(db, principal, read.GetRequest(scope="user", memory_id=memory_id), backend=backend)


def test_forget_unknown_id_raises_memory_not_found(db, principal, backend):
    with pytest.raises(MemoryNotFound):
        curation.forget(
            db, principal, curation.CurationRequest(scope="user", memory_id="mem_unknown", reason="x"),
            backend=backend,
        )


def test_non_owner_principal_is_forbidden_in_project_scope(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Project fact.", scope="project", project_slug="widgets")
    intruder = Principal(user_id="usr_intruder")

    with pytest.raises(Forbidden):
        curation.forget(
            db, intruder,
            curation.CurationRequest(scope="project", project_slug="widgets", memory_id=memory_id, reason="steal"),
            backend=backend,
        )


def test_curation_without_scope_infers_it_from_the_retain_journal(db, principal, backend):
    user_id = _seed(db, principal, backend, "Personal fact.")
    project_id = _seed(db, principal, backend, "Project fact.", scope="project", project_slug="widgets")

    curation.correct(db, principal, curation.CorrectRequest(memory_id=user_id, reason="fix", content="Fixed."),
                     backend=backend)
    curation.forget(db, principal,
                    curation.CurationRequest(project_slug="widgets", memory_id=project_id, reason="stale"),
                    backend=backend)

    assert read.get_memory(db, principal, read.GetRequest(memory_id=user_id), backend=backend).content == "Fixed."
    item = read.get_memory(db, principal, read.GetRequest(project_slug="widgets", memory_id=project_id),
                           backend=backend)
    assert item.state == "invalid"
    journal = read.history(db, principal, read.HistoryRequest(memory_id=user_id), backend=backend).journal
    assert next(e for e in journal if e.action == "memory.correct").details["scope"] == "user"


def test_inferred_scope_never_reaches_another_users_memory(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Mine.")
    intruder = Principal(user_id="usr_intruder")

    with pytest.raises(MemoryNotFound):
        curation.forget(db, intruder, curation.CurationRequest(memory_id=memory_id, reason="steal"),
                        backend=backend)


def test_inferred_project_scope_without_slug_is_invalid(db, principal, backend):
    memory_id = _seed(db, principal, backend, "Project fact.", scope="project", project_slug="widgets")

    with pytest.raises(InvalidRequest):
        curation.forget(db, principal, curation.CurationRequest(memory_id=memory_id, reason="x"), backend=backend)


def test_unknown_id_without_scope_raises_memory_not_found(db, principal, backend):
    with pytest.raises(MemoryNotFound):
        curation.forget(db, principal, curation.CurationRequest(memory_id="mem_unknown", reason="x"),
                        backend=backend)
