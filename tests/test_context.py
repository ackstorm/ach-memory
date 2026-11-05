from types import SimpleNamespace

import pytest

from memory import context, projects
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT
from memory.errors import UnsupportedCapability


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


def _seed(backend: FakeBackend, bank_id: str, builtin, content: str | None) -> None:
    backend.provision_mental_models(bank_id, (builtin,))
    backend.mental_model_content.setdefault(bank_id, {})[builtin.key] = content


def test_user_and_project_entries_both_fit(db, principal, backend):
    bank_id = projects.resolve(db, principal, "acme", create=True).project.bank_id
    _seed(backend, "user_usr_juan", USER_CONTEXT, "user stuff")
    _seed(backend, bank_id, PROJECT_CONTEXT, "project stuff")

    result = context.load(
        db, principal, context.LoadContextRequest(project_slug="acme"), backend=backend
    )

    assert [e.key for e in result.entries] == [USER_CONTEXT.key, PROJECT_CONTEXT.key]
    assert result.omitted == []
    assert "user stuff" in result.text
    assert "project stuff" in result.text


def test_budget_forces_the_second_entry_into_omitted(db, principal, backend, monkeypatch):
    bank_id = projects.resolve(db, principal, "acme", create=True).project.bank_id
    _seed(backend, "user_usr_juan", USER_CONTEXT, "user stuff")
    _seed(backend, bank_id, PROJECT_CONTEXT, "project stuff")
    first_section_len = len(f"### {USER_CONTEXT.name}\nuser stuff\n\n")
    monkeypatch.setattr(
        context, "get_settings",
        lambda: SimpleNamespace(context_budget_chars=first_section_len + context._LEFT_OUT_ROOM),
    )

    result = context.load(
        db, principal, context.LoadContextRequest(project_slug="acme"), backend=backend
    )

    assert [e.key for e in result.entries] == [USER_CONTEXT.key]
    assert result.omitted == [PROJECT_CONTEXT.key]


def test_unknown_project_slug_is_omitted_silently(db, principal, backend):
    _seed(backend, "user_usr_juan", USER_CONTEXT, "user stuff")

    result = context.load(
        db, principal, context.LoadContextRequest(project_slug="does-not-exist"), backend=backend
    )

    assert [e.key for e in result.entries] == [USER_CONTEXT.key]
    assert result.omitted == []


def test_empty_content_is_skipped(db, principal, backend):
    _seed(backend, "user_usr_juan", USER_CONTEXT, None)

    result = context.load(db, principal, context.LoadContextRequest(), backend=backend)

    assert result.entries == []
    assert result.text == ""


def test_engine_without_mental_models_capability_returns_empty_context(db, principal):
    class _NoMentalModelsBackend(FakeBackend):
        def get_mental_model(self, bank_id, key):
            raise UnsupportedCapability("mental_models")

    result = context.load(
        db, principal, context.LoadContextRequest(), backend=_NoMentalModelsBackend()
    )

    assert result == context.LoadContextResponse(text="", entries=[], omitted=[])


def _retain(backend: FakeBackend, bank_id: str, memory_id: str, memory_type: str, text: str) -> None:
    backend.retain(bank_id, memory_id, text, tags=(f"type:{memory_type}",), metadata={}, wait=True)


def _budget(monkeypatch, chars: int) -> None:
    monkeypatch.setattr(context, "get_settings", lambda: SimpleNamespace(context_budget_chars=chars))


def test_rules_are_delivered_verbatim_before_any_model(db, principal, backend):
    bank_id = projects.resolve(db, principal, "acme", create=True).project.bank_id
    _retain(backend, bank_id, "mem_pc", "constraint", "Never touch `prod`.")
    _retain(backend, "user_usr_juan", "mem_uc", "constraint", "Ask before coding.")
    _retain(backend, bank_id, "mem_pv", "convention", "Tag releases.")
    _retain(backend, bank_id, "mem_pg", "gotcha", "Not standing context.")
    _retain(backend, "user_usr_juan", "mem_up", "preference", "Not standing context either.")
    _seed(backend, "user_usr_juan", USER_CONTEXT, "user stuff")

    result = context.load(
        db, principal, context.LoadContextRequest(project_slug="acme"), backend=backend
    )

    assert result.text == (
        "### Project Constraints\n- Never touch `prod`.\n\n"
        "### User Constraints\n- Ask before coding.\n\n"
        "### Project Conventions\n- Tag releases.\n\n"
        "### User Context\nuser stuff\n\n"
    )
    assert [(e.scope, e.key) for e in result.entries] == [
        ("project", "constraint"), ("user", "constraint"), ("project", "convention"),
        ("user", USER_CONTEXT.key),
    ]
    assert result.omitted == []


def test_claims_that_do_not_fit_are_counted_oldest_first(db, principal, backend, monkeypatch):
    _retain(backend, "user_usr_juan", "mem_old", "constraint", "x" * 200)
    _retain(backend, "user_usr_juan", "mem_new", "constraint", "keep this one")
    _budget(monkeypatch, 150)

    result = context.load(db, principal, context.LoadContextRequest(), backend=backend)

    assert "- keep this one\n" in result.text
    assert "x" * 200 not in result.text
    assert result.text.endswith("Left out for size: 1 stored rule. Use `recall`.\n\n")
    assert len(result.text) <= 150
    assert result.omitted == ["mem_old"]


def test_model_too_big_for_what_the_rules_leave_is_omitted(db, principal, backend, monkeypatch):
    _retain(backend, "user_usr_juan", "mem_uc", "constraint", "Ask before coding.")
    _seed(backend, "user_usr_juan", USER_CONTEXT, "u" * 200)
    _budget(monkeypatch, 150)

    result = context.load(db, principal, context.LoadContextRequest(), backend=backend)

    assert "- Ask before coding.\n" in result.text
    assert result.text.endswith("Left out for size: User Context. Use `recall`.\n\n")
    assert result.omitted == [USER_CONTEXT.key]


def test_engine_without_mental_models_still_delivers_rules(db, principal):
    class _NoMentalModelsBackend(FakeBackend):
        def get_mental_model(self, bank_id, key):
            raise UnsupportedCapability("mental_models")

    no_models = _NoMentalModelsBackend()
    _retain(no_models, "user_usr_juan", "mem_uc", "constraint", "Ask before coding.")

    result = context.load(db, principal, context.LoadContextRequest(), backend=no_models)

    assert result.text == "### User Constraints\n- Ask before coding.\n\n"
