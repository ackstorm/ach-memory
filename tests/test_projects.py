import pytest

from memory import projects
from memory.auth.principal import Principal
from memory.errors import Forbidden, ProjectNotFound


def _principal(user_id: str, **kw) -> Principal:
    return Principal(user_id=user_id, **kw)


def test_first_toucher_creates_and_owns_the_project(db):
    juan = _principal("usr_juan")

    result = projects.resolve(db, juan, "github.com-acme-payments-api", create=True)

    assert result.created is True
    assert result.notice == "PROJECT_CREATED"
    assert result.project.owner_type == "user"
    assert result.project.owner_id == "usr_juan"
    assert result.project.bank_id.startswith("project_")


def test_second_resolution_reuses_the_same_project(db):
    juan = _principal("usr_juan")
    first = projects.resolve(db, juan, "payments-api", create=True)

    second = projects.resolve(db, juan, "payments-api", create=False)

    assert second.project.internal_id == first.project.internal_id
    assert second.created is False
    assert second.notice is None


def test_unknown_slug_without_create_is_not_found(db):
    juan = _principal("usr_juan")

    with pytest.raises(ProjectNotFound):
        projects.resolve(db, juan, "missing", create=False)


def test_unauthorized_caller_is_forbidden(db):
    juan = _principal("usr_juan")
    alice = _principal("usr_alice")
    projects.resolve(db, juan, "payments-api", create=True)

    with pytest.raises(Forbidden):
        projects.resolve(db, alice, "payments-api", create=False)


def test_an_operator_reaches_any_project(db):
    juan = _principal("usr_juan")
    operator = _principal("usr_op", is_operator=True)
    projects.resolve(db, juan, "payments-api", create=True)

    result = projects.resolve(db, operator, "payments-api", create=False)

    assert result.project.owner_id == "usr_juan"


def test_group_owned_project_is_reachable_by_a_member(db):
    juan = _principal("usr_juan")
    result = projects.resolve(db, juan, "payments-api", create=True)
    result.project.owner_type = "group"
    result.project.owner_id = "grp_payments"
    db.flush()
    alice = _principal("usr_alice", groups=frozenset({"grp_payments"}))

    reached = projects.resolve(db, alice, "payments-api", create=False)

    assert reached.project.internal_id == result.project.internal_id


def test_rename_leaves_a_forwarding_tombstone(db):
    juan = _principal("usr_juan")
    result = projects.resolve(db, juan, "old-slug", create=True)

    projects.rename(db, juan, result.project, "new-slug")

    forwarded = projects.resolve(db, juan, "old-slug", create=False)
    assert forwarded.notice == "PROJECT_RENAMED"
    assert forwarded.project.internal_id == result.project.internal_id
    current = projects.resolve(db, juan, "new-slug", create=False)
    assert current.notice is None


def test_rename_denies_an_unauthorized_caller(db):
    juan = _principal("usr_juan")
    alice = _principal("usr_alice")
    result = projects.resolve(db, juan, "payments-api", create=True)

    with pytest.raises(Forbidden):
        projects.rename(db, alice, result.project, "new-slug")
