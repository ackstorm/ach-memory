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
        context, "get_settings", lambda: SimpleNamespace(context_budget_chars=first_section_len)
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
