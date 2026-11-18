from uuid import uuid4

import pytest

from memory import context, curation, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.config import get_settings
from memory.families import THRESHOLD
from memory.models import Memory, Project


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


def test_a_project_nobody_retained_into_asks_for_its_purpose(db, principal, backend):
    text = _load(db, principal, backend, slug="nope").text

    assert text.startswith("No purpose is stored for this project.")
    assert "Nothing is pinned in this project." in text
    assert text.endswith("0 project memories, 0 pinned; 0 of yours, 0 pinned.\n")


def test_without_a_project_the_user_part_is_delivered(db, principal, backend):
    _retain(db, principal, backend, "Answer in English", scope="user", pin=True,
            content="Always answer in English.")

    text = _load(db, principal, backend, slug=None).text

    assert "### Pinned by you\n- **Answer in English**: Always answer in English.\n" in text
    assert "1 memories of yours, 1 pinned." in text


def test_another_users_project_still_delivers_the_callers_own_memory(db, principal, backend):
    """The first person to retain into a slug owns the project. The next one opening the
    same repository still gets their own pins, and is told why the project part is absent."""
    _retain(db, Principal(user_id="usr_alice"), backend, "Hers", pin=True)
    _retain(db, principal, backend, "Answer in English", scope="user", pin=True,
            content="Always answer in English.")

    text = _load(db, principal, backend).text

    assert "- **Answer in English**: Always answer in English.\n" in text
    assert "Hers" not in text
    assert "belongs to another user" in text


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

    assert "- deploy (14): Deploy note 27; Deploy note 25; Deploy note 23; " in text
    assert "- testing (15): Deploy note 28; Deploy note 26; Deploy note 24; " in text
    assert "release" not in text  # a family with no memory has no line
    assert text.index("- testing (15)") < text.index("- other (1): Loose end")
    assert "30 project memories in 3 families" in text


def test_the_whole_stays_within_the_budget(db, principal, backend, monkeypatch):
    for i in range(10):
        _retain(db, principal, backend, f"A rather long title for memory number {i}")
    monkeypatch.setattr(get_settings(), "context_budget_chars", 900)

    result = _load(db, principal, backend)

    assert len(result.text) <= 900
    assert "more: `list_memories` lists them." in result.text
    assert result.truncated


def test_a_tight_budget_drops_sample_titles_before_any_family_line(db, principal, backend, monkeypatch):
    for i in range(THRESHOLD):
        _retain(db, principal, backend, f"A long enough title for memory number {i:02d}", family=f"family-{i % 6}")
    monkeypatch.setattr(get_settings(), "context_budget_chars", 1050)

    result = _load(db, principal, backend)

    assert all(f"- family-{i} (5)" in result.text for i in range(6))
    lines = [line for line in result.text.splitlines() if line.startswith("- family-")]
    assert len(lines) == 6 and all(line.count(";") < 2 for line in lines)
    assert len(result.text) <= 1050 and result.truncated


def test_pinned_memories_are_not_repeated_as_sample_titles(db, principal, backend):
    for i in range(THRESHOLD - 1):
        _retain(db, principal, backend, f"Note {i}", family="deploy")
    _retain(db, principal, backend, "Pinned rule", family="deploy", pin=True)

    text = _load(db, principal, backend).text

    line = next(x for x in text.splitlines() if x.startswith("- deploy (30): "))
    assert line == "- deploy (30): " + "; ".join(f"Note {i}" for i in range(28, -1, -1))


def _families(db, principal, backend, n, per_family, title="A long enough title for memory number"):
    for i in range(n):
        _retain(db, principal, backend, f"{title} {i:02d}", family=f"family-{i % per_family}")


def _titles_per_line(text):
    return [line.count(";") + 1 if ": " in line else 0 for line in text.splitlines() if line.startswith("- family-")]


def test_spare_room_shows_more_titles_on_every_line_and_stays_within_the_budget(db, principal, backend):
    _families(db, principal, backend, 60, 6)

    result = _load(db, principal, backend)

    shown = _titles_per_line(result.text)
    assert len(shown) == 6 and len(set(shown)) == 1 and shown[0] > 3
    assert len(result.text) <= get_settings().context_budget_chars and not result.truncated


def test_without_room_for_three_the_index_sheds_titles_as_before(db, principal, backend, monkeypatch):
    _families(db, principal, backend, THRESHOLD, 6)
    monkeypatch.setattr(get_settings(), "context_budget_chars", 1050)

    result = _load(db, principal, backend)

    assert all(n < 3 for n in _titles_per_line(result.text)) and len(_titles_per_line(result.text)) == 6
    assert len(result.text) <= 1050 and result.truncated


def test_a_family_with_fewer_memories_than_the_number_shows_them_all(db, principal, backend):
    _families(db, principal, backend, 40, 4)
    _retain(db, principal, backend, "The only small one", family="small")
    project = db.query(Project).one()
    project.families = ["family-0", "family-1", "family-2", "family-3", "small", "other"]

    text = _load(db, principal, backend).text

    assert "- small (1): The only small one\n" in text
    assert all(n == 10 for n in _titles_per_line(text))


def test_full_caps_and_a_long_purpose_keep_every_family_line_and_the_totals():
    def pins(prefix, n, chars):
        return [Memory(memory_id=f"{prefix}{i}", title=f"{prefix} pin {i}", family="family-0", pinned=True,
                       chars=chars, memory_type="constraint") for i in range(n)]

    user = context.Bank(pins("u", 10, get_settings().pin_cap_user_chars // 10), get_settings().pin_cap_user_chars,
                        lambda row: "u" * row.chars)
    rows = pins("p", 10, get_settings().pin_cap_project_chars // 10) + [
        Memory(memory_id=f"m{i}", title=f"A memory title of a realistic length, number {i:02d}",
               family=f"family-{i % 12}", pinned=False, chars=200, memory_type="fact") for i in range(60)]
    project = context.Bank(rows, get_settings().pin_cap_project_chars, lambda row: "p" * row.chars)
    purpose = " ".join(f"Sentence {i} says what the project does." for i in range(40))

    text = context.render(user=user, project=project, purpose=purpose).text

    shown = text.split("### Project purpose\n")[1].split("\n\n")[0]
    assert len(purpose) > 1500 and len(shown) <= context.PURPOSE_CHARS and shown.endswith("does.")
    assert all(f"- family-{i} (" in text for i in range(12))
    assert text.endswith("70 project memories in 12 families, 10 pinned; 10 of yours, 10 pinned.\n")
    assert "left out" not in text and len(text) <= get_settings().context_budget_chars
