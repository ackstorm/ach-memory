from uuid import uuid4

import pytest

from memory import context, curation, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.config import get_settings
from memory.families import THRESHOLD
from memory.models import Project


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


def _retain(db, principal, backend, title, *, scope="project", content=None, pin=False,
            family=None, memory_type="fact", tags=()):
    request = retain.RetainRequest(
        scope=scope, project_slug="acme" if scope == "project" else None, title=title,
        content=content or f"{title}: details.", memory_type=memory_type, basis="agent_verified",
        operation_id=uuid4(), family=family, pin=pin, tags=list(tags), wait=True,
    )
    return retain.submit(db, principal, request, backend=backend).memory_id


def _load(db, principal, backend, slug="acme"):
    return context.load(db, principal, context.LoadContextRequest(project_slug=slug), backend=backend)


def test_nothing_anywhere_loads_nothing(db, principal, backend):
    assert _load(db, principal, backend, slug=None).text == ""
    assert _load(db, principal, backend, slug="nope").text == ""


def test_without_a_project_the_user_part_is_delivered(db, principal, backend):
    _retain(db, principal, backend, "Answer in English", scope="user", pin=True,
            content="Always answer in English.")

    text = _load(db, principal, backend, slug=None).text

    assert "### Pinned by you\n- **Answer in English**: Always answer in English.\n" in text
    assert "1 memories of yours, 1 pinned." in text


def test_sections_come_in_order(db, principal, backend):
    _retain(db, principal, backend, "Answer in English", scope="user", pin=True)
    _retain(db, principal, backend, "Project purpose", content="Acme ships widgets.", tags=["purpose"])
    _retain(db, principal, backend, "Deploy with make deploy", pin=True)

    text = _load(db, principal, backend).text

    order = [text.index(s) for s in ("### Project purpose\nAcme ships widgets.", "### Pinned by you",
                                     "### Pinned in this project", "### Memory index", "2 project memories")]
    assert order == sorted(order)


def test_the_newest_purpose_is_printed_verbatim(db, principal, backend):
    _retain(db, principal, backend, "Old purpose", content="Old purpose.", tags=["purpose"])
    _retain(db, principal, backend, "New purpose", content="New purpose.", tags=["purpose"])

    text = _load(db, principal, backend).text

    assert "### Project purpose\nNew purpose.\n" in text
    assert "Old purpose." not in text


def test_a_missing_purpose_and_no_pins_say_what_to_do(db, principal, backend):
    _retain(db, principal, backend, "Something")

    text = _load(db, principal, backend).text

    assert context._NO_PURPOSE in text and context._NOTHING_PINNED in text


def test_pinned_text_past_the_cap_keeps_the_newest_and_counts_the_rest(db, principal, backend, monkeypatch):
    monkeypatch.setattr(get_settings(), "pin_cap_project_chars", 25)
    _retain(db, principal, backend, "Oldest", pin=True, content="o" * 10)
    monkeypatch.setattr(get_settings(), "pin_cap_project_chars", 10_000)
    _retain(db, principal, backend, "Middle", pin=True, content="m" * 10)
    _retain(db, principal, backend, "Newest", pin=True, content="n" * 10)
    monkeypatch.setattr(get_settings(), "pin_cap_project_chars", 25)

    result = _load(db, principal, backend)

    assert "**Newest**" in result.text and "**Middle**" in result.text and "**Oldest**" not in result.text
    assert "1 more pinned memories are over the 25-char cap" in result.text
    assert result.truncated


def test_below_the_threshold_the_index_is_the_flat_list_of_titles(db, principal, backend):
    _retain(db, principal, backend, "First")
    _retain(db, principal, backend, "Second")
    forgotten = _retain(db, principal, backend, "Forgotten")
    curation.forget(db, principal, curation.CurationRequest(memory_id=forgotten, project_slug="acme", reason="wrong"),
                    backend=backend)

    text = _load(db, principal, backend).text

    assert "- Second\n- First\n" in text
    assert "Forgotten" not in text


def test_from_the_threshold_the_index_has_one_line_per_family(db, principal, backend):
    for i in range(THRESHOLD - 1):
        _retain(db, principal, backend, f"Deploy note {i}", family="deploy" if i % 2 else "testing")
    project = db.query(Project).one()
    project.families = ["deploy", "testing", "release", "other"]
    _retain(db, principal, backend, "Loose end", family="other")

    text = _load(db, principal, backend).text

    assert "- deploy (14): Deploy note 27; Deploy note 25; Deploy note 23\n" in text
    assert "- testing (15): Deploy note 28; Deploy note 26; Deploy note 24\n" in text
    assert "- release (0)\n" in text
    assert text.index("- release (0)") < text.index("- other (1): Loose end")
    assert "30 project memories in 3 families" in text


def test_the_whole_stays_within_the_budget(db, principal, backend, monkeypatch):
    for i in range(10):
        _retain(db, principal, backend, f"A rather long title for memory number {i}")
    monkeypatch.setattr(get_settings(), "context_budget_chars", 900)

    result = _load(db, principal, backend)

    assert len(result.text) <= 900
    assert "more lines: `list_memories` lists them." in result.text
    assert result.truncated
