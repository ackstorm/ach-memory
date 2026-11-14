from uuid import uuid4

import pytest

from memory import audit, curation, families, read, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.config import get_settings
from memory.errors import InvalidRequest, PinCapExceeded
from memory.models import AuditEvent, Memory, Project


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


def _retain(db, principal, backend, title="A title", *, content="Some durable claim.", family=None,
            pin=False, scope="project", memory_type="fact"):
    request = retain.RetainRequest(
        scope=scope, project_slug="acme" if scope == "project" else None, title=title, content=content,
        memory_type=memory_type, basis="agent_verified", operation_id=uuid4(), family=family, pin=pin,
    )
    return retain.submit(db, principal, request, backend=backend).memory_id


def _project(db) -> Project:
    return db.query(Project).one()


def test_retain_records_title_family_and_pin(db, principal, backend):
    memory_id = _retain(db, principal, backend, "Deploy  with\nmake deploy", family="Release Process", pin=True)

    row = db.get(Memory, memory_id)
    assert (row.title, row.family, row.pinned, row.chars) == ("Deploy with make deploy", "release-process", True, 19)


def test_a_title_over_80_chars_is_refused(db, principal, backend):
    with pytest.raises(ValueError):
        _retain(db, principal, backend, "x" * 81)


def test_once_the_project_has_a_list_the_family_is_required_and_checked(db, principal, backend):
    _retain(db, principal, backend)
    _project(db).families = ["deploy", "testing", "other"]

    with pytest.raises(InvalidRequest, match="use one of: deploy, testing, other"):
        _retain(db, principal, backend)
    with pytest.raises(InvalidRequest, match="unknown family 'docs'; use one of: deploy, testing, other"):
        _retain(db, principal, backend, family="docs")
    assert db.get(Memory, _retain(db, principal, backend, family="Testing")).family == "testing"


def test_the_list_is_drafted_once_at_the_threshold(db, principal, backend, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_url", "http://llm.test/v1")
    calls = []

    def fake_llm(settings, prompt):
        calls.append(prompt)
        return {"families": ["Deploy", "testing", "other", "deploy"],
                "assign": {str(i): "deploy" if i % 2 else "nonsense" for i in range(1, 40)}}

    monkeypatch.setattr(families.llm, "complete_json", fake_llm)
    for i in range(families.THRESHOLD - 1):
        _retain(db, principal, backend, f"Note {i}")
    assert _project(db).families is None and not calls

    _retain(db, principal, backend, "Note 29")
    _retain(db, principal, backend, "Note 30", family="testing")

    assert _project(db).families == ["deploy", "testing", "other"]
    assert len(calls) == 1 and "30. [fact] Note 0" in calls[0]
    assert {row.family for row in db.query(Memory)} == {"deploy", "other", "testing"}
    assert db.query(AuditEvent).filter_by(action="project.families").count() == 1


def test_a_failed_draft_leaves_the_project_without_a_list(db, principal, backend, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_url", "http://llm.test/v1")
    monkeypatch.setattr(families.llm, "complete_json", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    for i in range(families.THRESHOLD):
        _retain(db, principal, backend, f"Note {i}")
    assert _project(db).families is None


def test_replacing_the_list_renames_and_sends_dropped_families_to_other(db, principal, backend):
    _retain(db, principal, backend)
    project = _project(db)
    project.families = ["deploy", "testing", "docs", "other"]
    deploy = _retain(db, principal, backend, family="deploy")
    docs = _retain(db, principal, backend, family="docs")

    result = families.replace(db, principal, project, ["release", "testing"], {"deploy": "release"})

    assert result == ["release", "testing", "other"]
    assert db.get(Memory, deploy).family == "release" and db.get(Memory, docs).family == "other"
    with pytest.raises(InvalidRequest, match="rename targets not in the new list"):
        families.replace(db, principal, project, ["testing"], {"release": "ops"})
    with pytest.raises(InvalidRequest, match="at most 12"):
        families.replace(db, principal, project, [f"f{i}" for i in range(13)], {})


def test_a_pin_past_the_cap_is_refused_with_what_is_pinned(db, principal, backend, monkeypatch):
    monkeypatch.setattr(get_settings(), "pin_cap_project_chars", 30)
    first = _retain(db, principal, backend, "First pin", content="a" * 20, pin=True)

    with pytest.raises(PinCapExceeded, match=f'{first} "First pin" \\(20 chars\\)'):
        _retain(db, principal, backend, "Second pin", content="b" * 20, pin=True)
    assert db.query(Memory).count() == 1

    second = _retain(db, principal, backend, "Second", content="b" * 20)
    with pytest.raises(PinCapExceeded):
        curation.pin(db, principal, curation.PinRequest(memory_id=second, project_slug="acme"), backend=backend)
    curation.pin(db, principal, curation.PinRequest(memory_id=first, project_slug="acme", pinned=False),
                 backend=backend)
    curation.pin(db, principal, curation.PinRequest(memory_id=second, project_slug="acme"), backend=backend)

    actions = [e.action for e in db.query(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
               if e.action.startswith("memory.") and e.action != "memory.retain"]
    assert sorted(actions) == ["memory.pin", "memory.unpin"]


def test_user_pins_have_their_own_cap(db, principal, backend, monkeypatch):
    monkeypatch.setattr(get_settings(), "pin_cap_user_chars", 10)
    with pytest.raises(PinCapExceeded):
        _retain(db, principal, backend, scope="user", content="c" * 11, pin=True)


def test_correcting_a_pinned_memory_past_the_cap_is_refused(db, principal, backend, monkeypatch):
    monkeypatch.setattr(get_settings(), "pin_cap_project_chars", 30)
    memory_id = _retain(db, principal, backend, content="a" * 20, pin=True)

    with pytest.raises(PinCapExceeded):
        curation.correct(db, principal, curation.CorrectRequest(
            memory_id=memory_id, project_slug="acme", content="a" * 31, reason="longer"), backend=backend)
    curation.correct(db, principal, curation.CorrectRequest(
        memory_id=memory_id, project_slug="acme", content="a" * 25, reason="longer"), backend=backend)
    assert db.get(Memory, memory_id).chars == 25


def test_curation_keeps_the_record_in_step(db, principal, backend):
    memory_id = _retain(db, principal, backend)
    request = curation.CurationRequest(memory_id=memory_id, project_slug="acme", reason="r")

    curation.forget(db, principal, request, backend=backend)
    assert db.get(Memory, memory_id).state == "invalid"
    curation.restore(db, principal, request, backend=backend)
    assert db.get(Memory, memory_id).state == "valid"
    curation.delete(db, principal, request, backend=backend)
    assert db.get(Memory, memory_id) is None


def test_list_filters_by_family_and_pin_and_reads_text_only_when_asked(db, principal, backend):
    _retain(db, principal, backend, "Deploy", family="deploy", content="Deploy text.", pin=True)
    _retain(db, principal, backend, "Test", family="testing", memory_type="gotcha")

    def listed(**kw):
        return read.list_memories(db, principal, read.ListRequest(scope="project", project_slug="acme", **kw),
                                  backend=backend).items

    assert [(i.title, i.content) for i in listed(family="deploy")] == [("Deploy", None)]
    assert [i.content for i in listed(pinned=True, full=True)] == ["Deploy text."]
    assert [i.title for i in listed(memory_types=["gotcha"])] == ["Test"]


def test_recall_hits_carry_the_title(db, principal, backend):
    _retain(db, principal, backend, "Deploy rule", content="deploy with make deploy")

    hits = read.recall(db, principal, read.RecallRequest(scope="project", project_slug="acme",
                                                         query="deploy with make deploy"), backend=backend)

    assert [h.title for h in hits.items] == ["Deploy rule"]


def test_a_logged_query_shaped_like_a_secret_is_withheld(db, principal):
    audit.log_read(db, principal=principal, tool="recall", scope="user", project_slug=None,
                   query="is sk-1234567890abcdef1234 valid", results=0, latency_ms=3)
    from memory.models import ReadEvent

    assert db.query(ReadEvent).one().query == "[withheld: looks like a secret]"
