import pytest
from sqlalchemy import select

from memory import projects
from memory.auth.principal import Principal
from memory.errors import Forbidden, InvalidRequest, ProjectNotFound
from memory.models import AuditEvent


def _principal(user_id: str, **kw) -> Principal:
    kw.setdefault("subject", f"{user_id.removeprefix('usr_')}@acme.test")
    return Principal(user_id=user_id, **kw)


def test_first_toucher_creates_and_owns_the_project(db):
    juan = _principal("usr_juan", subject="Juan@Acme.test")

    result = projects.resolve(db, juan, "github.com-acme-payments-api", create=True)

    assert result.notice == "PROJECT_CREATED"
    assert result.project.access == {"users": ["juan@acme.test"], "groups": []}
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

    assert result.project.access["users"] == ["juan@acme.test"]


def test_group_on_the_list_reaches_the_project(db):
    juan = _principal("usr_juan", groups=frozenset({"payments"}))
    result = projects.resolve(db, juan, "payments-api", create=True)
    projects.share(db, juan, result.project, [], ["payments"])
    alice = _principal("usr_alice", groups=frozenset({"payments"}))

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


def test_a_shared_user_is_a_co_owner_and_can_remove_the_creator(db):
    juan = _principal("usr_juan")
    alice = _principal("usr_alice", subject="Alice@Acme.test")
    project = projects.resolve(db, juan, "payments-api", create=True).project

    assert projects.share(db, juan, project, ["ALICE@acme.test"], []) == {
        "users": ["alice@acme.test", "juan@acme.test"], "groups": []}
    projects.rename(db, alice, project, "payments")
    projects.unshare(db, alice, project, ["juan@acme.test"], [])

    with pytest.raises(Forbidden):
        projects.resolve(db, juan, "payments", create=False)
    assert projects.resolve(db, alice, "payments", create=False).project.internal_id == project.internal_id


def test_sharing_a_group_the_caller_is_not_in_is_refused(db):
    juan = _principal("usr_juan", groups=frozenset({"dream"}))
    project = projects.resolve(db, juan, "payments-api", create=True).project

    with pytest.raises(InvalidRequest, match="run"):
        projects.share(db, juan, project, [], ["dream", "run"])
    assert project.access["groups"] == []


def test_unsharing_a_group_the_caller_is_not_in_is_allowed(db):
    juan = _principal("usr_juan", groups=frozenset({"dream"}))
    alice = _principal("usr_alice")
    project = projects.resolve(db, juan, "payments-api", create=True).project
    projects.share(db, juan, project, ["alice@acme.test"], ["dream"])

    assert projects.unshare(db, alice, project, [], ["dream"])["groups"] == []


def test_emptying_the_list_is_refused(db):
    juan = _principal("usr_juan")
    project = projects.resolve(db, juan, "payments-api", create=True).project

    with pytest.raises(InvalidRequest):
        projects.unshare(db, juan, project, ["juan@acme.test"], [])
    assert project.access["users"] == ["juan@acme.test"]


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "x" * 257])
def test_a_bad_entry_is_refused(db, bad):
    juan = _principal("usr_juan")
    project = projects.resolve(db, juan, "payments-api", create=True).project

    with pytest.raises(InvalidRequest):
        projects.share(db, juan, project, [bad], [])


def test_share_and_unshare_are_journaled(db):
    juan = _principal("usr_juan")
    project = projects.resolve(db, juan, "payments-api", create=True).project
    projects.share(db, juan, project, ["alice@acme.test"], [])
    projects.unshare(db, juan, project, ["alice@acme.test"], [])

    actions = db.scalars(select(AuditEvent.action).where(AuditEvent.resource == project.internal_id)).all()
    assert sorted(actions) == ["project.share", "project.unshare"]
