import pytest

from memory import projects
from memory.auth.principal import Principal
from memory.errors import Forbidden, InvalidRequest, ProjectNotFound


def _principal(user_id: str, **kw) -> Principal:
    return Principal(user_id=user_id, **kw)


def test_first_toucher_creates_and_owns_the_project(db):
    juan = _principal("usr_juan")

    result = projects.resolve(db, juan, "github.com-acme-payments-api", create=True)

    assert result.notice == "PROJECT_CREATED"
    assert result.project.owner_type == "user"
    assert result.project.owner_id == "usr_juan"
    assert result.project.bank_id.startswith("project_")


def test_second_resolution_reuses_the_same_project(db):
    juan = _principal("usr_juan")
    first = projects.resolve(db, juan, "payments-api", create=True)

    second = projects.resolve(db, juan, "payments-api", create=False)

    assert second.project.internal_id == first.project.internal_id
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


def test_rename_back_to_a_retired_slug_flips_the_tombstone(db):
    juan = _principal("usr_juan")
    result = projects.resolve(db, juan, "old-slug", create=True)
    projects.rename(db, juan, result.project, "new-slug")

    retired = projects.rename(db, juan, result.project, "old-slug")

    assert retired == "new-slug"
    assert projects.resolve(db, juan, "old-slug", create=False).notice is None
    assert projects.resolve(db, juan, "new-slug", create=False).notice == "PROJECT_RENAMED"


def test_rename_onto_another_projects_slug_is_rejected(db):
    juan = _principal("usr_juan")
    mine = projects.resolve(db, juan, "mine", create=True)
    projects.resolve(db, juan, "theirs", create=True)

    with pytest.raises(InvalidRequest):
        projects.rename(db, juan, mine.project, "theirs")

    assert projects.resolve(db, juan, "mine", create=False).notice is None


def test_rename_to_the_current_slug_is_rejected(db):
    juan = _principal("usr_juan")
    result = projects.resolve(db, juan, "payments-api", create=True)

    with pytest.raises(InvalidRequest):
        projects.rename(db, juan, result.project, "payments-api")
