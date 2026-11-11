import pytest

from memory import context, projects
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.builtin_models import BRIEFING, BUILTIN_MODELS, TRAPS
from memory.config import get_settings
from memory.errors import InvalidRequest


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


@pytest.fixture
def bank(db, principal, backend) -> str:
    bank_id = projects.resolve(db, principal, "acme", create=True).project.bank_id
    backend.provision_mental_models(bank_id, BUILTIN_MODELS)
    return bank_id


def _note(backend, bank, memory_id, text, *tags):
    backend.retain(bank, memory_id, text, tags=tags, metadata={}, wait=True)


def _load(db, principal, backend, **request):
    return context.load(db, principal, context.LoadContextRequest(**request), backend=backend)


def test_no_project_loads_nothing(db, principal, backend):
    assert _load(db, principal, backend).text == ""


def test_an_unknown_project_loads_nothing(db, principal, backend):
    assert _load(db, principal, backend, project_slug="nope").text == ""


def test_the_newest_purpose_is_printed_verbatim(db, principal, backend, bank):
    _note(backend, bank, "mem_old", "Old purpose.", "type:fact", "purpose")
    _note(backend, bank, "mem_new", "New purpose.", "type:fact", "purpose")

    text = _load(db, principal, backend, project_slug="acme").text

    assert "### Project purpose\nNew purpose.\n" in text
    assert "Old purpose." not in text


def test_a_missing_purpose_asks_the_agent_to_write_one(db, principal, backend, bank):
    assert context._NO_PURPOSE in _load(db, principal, backend, project_slug="acme").text


def test_an_empty_briefing_asks_for_notes(db, principal, backend, bank):
    assert context._EMPTY_BRIEFING in _load(db, principal, backend, project_slug="acme").text


def test_purpose_then_briefing_then_the_index(db, principal, backend, bank):
    _note(backend, bank, "mem_p", "Purpose.", "type:fact", "purpose")
    _note(backend, bank, "mem_b", "A note.", "type:briefing")
    backend.mental_model_content[bank] = {BRIEFING.key: "- Rule one.\n- Rule two."}

    result = _load(db, principal, backend, project_slug="acme")

    assert not result.truncated
    text = result.text
    assert text.index("Purpose.") < text.index("- Rule one.") < text.index("### More context")
    for model in context.ON_DEMAND:
        assert f"`{model.key}`: {model.load_when}" in text
    assert BRIEFING not in context.ON_DEMAND


def test_a_long_briefing_is_cut_at_a_whole_line(db, principal, backend, bank):
    _note(backend, bank, "mem_b", "A note.", "type:briefing")
    lines = [f"- Note {i:03d} " + "x" * 80 for i in range(200)]
    backend.mental_model_content[bank] = {BRIEFING.key: "\n".join(lines)}

    result = _load(db, principal, backend, project_slug="acme")

    assert result.truncated
    assert len(result.text) <= get_settings().context_budget_chars
    assert context._BRIEFING_CUT in result.text
    kept = [line for line in result.text.splitlines() if line.startswith("- Note")]
    assert kept and all(line in lines for line in kept)


def test_model_loads_one_model_in_full(db, principal, backend, bank):
    backend.mental_model_content[bank] = {TRAPS.key: "- a trap"}

    text = _load(db, principal, backend, project_slug="acme", model=TRAPS.key).text

    assert text == f"### {TRAPS.name}\n- a trap\n"


def test_an_unknown_model_is_rejected(db, principal, backend, bank):
    with pytest.raises(InvalidRequest):
        _load(db, principal, backend, project_slug="acme", model="nope")


def test_a_model_needs_a_project(db, principal, backend):
    with pytest.raises(InvalidRequest):
        _load(db, principal, backend, model=TRAPS.key)


def test_a_briefing_with_no_room_left_is_dropped_not_overflowed(db, principal, backend, bank):
    _note(backend, bank, "mem_p", "p" * 8600, "type:fact", "purpose")
    _note(backend, bank, "mem_b", "A note.", "type:briefing")
    backend.mental_model_content[bank] = {BRIEFING.key: "\n".join(["- note"] * 500)}

    result = _load(db, principal, backend, project_slug="acme")

    assert result.truncated
    assert "- note" not in result.text
