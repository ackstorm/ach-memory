import pytest

from memory import ids, projects
from memory.auth.principal import Principal
from memory.errors import (
    GroupNotFound,
    InvalidOwnerType,
    ProjectAccessDenied,
    ProjectInvalidSlug,
    ProjectLocatorMismatch,
    ProjectNotFound,
    ProjectSlugConflict,
    UserNotFound,
)
from memory.models import Group, GroupMember, Project, User


def _user(session, tenant, user_id: str) -> User:
    user = User(id=user_id, tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return user


def _principal(tenant: str, user_id: str | None, master: bool = False) -> Principal:
    return Principal(
        tenant_id=tenant, user_id=user_id, is_master=master, key_id="key_x"
    )


def test_first_toucher_creates_and_owns_the_project(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    result = projects.resolve(session, juan, "github.com-acme-payments-api")

    assert result.project.owner_type == "user"
    assert result.project.owner_id == "usr_juan"
    assert result.project.bank_id.startswith("project_")
    assert result.resolved_from is None


def test_second_resolution_reuses_the_same_bank(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    first = projects.resolve(session, juan, "payments-api")
    second = projects.resolve(session, juan, "payments-api")

    assert first.project.bank_id == second.project.bank_id


def test_another_user_is_denied_and_told_only_the_owner_type(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    with pytest.raises(ProjectAccessDenied) as caught:
        projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert caught.value.details["project_slug"] == "payments-api"
    assert caught.value.details["owner_type"] == "user"
    assert "owner_id" not in caught.value.details
    assert "usr_juan" not in str(caught.value.details)


def test_no_second_bank_is_created_for_the_denied_caller(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert session.query(Project).count() == 1


def test_group_member_reaches_a_group_owned_project(session, tenant):
    _user(session, tenant, "usr_juan")
    alice = _user(session, tenant, "usr_alice")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    session.add(GroupMember(group_id="grp_payments", user_id=alice.id))
    session.flush()

    result = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    projects.transfer(
        session, _principal(tenant, "usr_juan"), result.project, "group", "grp_payments"
    )

    for_alice = projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert for_alice.project.bank_id == result.project.bank_id


def test_non_member_is_denied_a_group_owned_project(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_bob")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()

    result = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    projects.transfer(
        session, _principal(tenant, "usr_juan"), result.project, "group", "grp_payments"
    )

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(session, _principal(tenant, "usr_bob"), "payments-api")


def test_transfer_between_users_moves_access(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    juan = _principal(tenant, "usr_juan")

    result = projects.resolve(session, juan, "payments-api")
    bank_before = result.project.bank_id
    projects.transfer(session, juan, result.project, "user", "usr_alice")

    for_alice = projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")
    assert for_alice.project.bank_id == bank_before

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(session, juan, "payments-api")


def test_rename_leaves_a_forwarding_tombstone(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "github.com-acme-payments-api")
    bank_before = result.project.bank_id

    projects.rename(session, juan, result.project, "payments-api")

    forwarded = projects.resolve(session, juan, "github.com-acme-payments-api")

    assert forwarded.project.project_slug == "payments-api"
    assert forwarded.project.bank_id == bank_before
    assert forwarded.resolved_from == "github.com-acme-payments-api"


def test_rename_does_not_create_an_empty_project(session, tenant):
    """The failure this tombstone exists to prevent (SPEC §8.6)."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "old-slug")
    projects.rename(session, juan, result.project, "new-slug")

    projects.resolve(session, juan, "old-slug")

    assert session.query(Project).count() == 1


def test_chained_rename_still_resolves_in_one_hop(session, tenant):
    from memory.models import RetiredSlug

    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "a")
    projects.rename(session, juan, result.project, "b")
    projects.rename(session, juan, result.project, "c")

    from_a = projects.resolve(session, juan, "a")
    tombstone = session.get(RetiredSlug, (tenant, "a"))

    assert from_a.project.project_slug == "c"
    assert tombstone.project_internal_id == result.project.internal_id


def test_a_retired_slug_cannot_be_reused(session, tenant):
    from memory.errors import ProjectSlugConflict

    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "a")
    projects.rename(session, juan, result.project, "b")
    second = projects.resolve(session, juan, "c")

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, juan, second.project, "a")


def test_locator_mismatch_refuses_rather_than_merging(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    with pytest.raises(ProjectLocatorMismatch):
        projects.resolve(
            session,
            juan,
            "payments-api",
            git_locator="gitlab.com/customer/payments-api",
        )


def test_a_caller_without_a_locator_is_unaffected(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    result = projects.resolve(session, juan, "payments-api")

    assert result.project.git_locator == "github.com/acme/payments-api"


def test_an_absent_locator_is_filled_in_for_an_authorized_caller(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(session, juan, "payments-api")

    result = projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    assert result.project.git_locator == "github.com/acme/payments-api"


def test_locator_spelling_variants_resolve_to_one_project(session, tenant):
    """github.com/acme/payments-api and https://github.com/acme/payments-api.git
    are the same repository; the mismatch check must canonicalize before
    comparing or it treats one repo, spelled two ways, as two (SPEC §8.4)."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    created = projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    result = projects.resolve(
        session,
        juan,
        "payments-api",
        git_locator="https://github.com/acme/payments-api.git",
    )

    assert result.project.internal_id == created.project.internal_id
    assert result.project.git_locator == "github.com/acme/payments-api"


def test_genuinely_different_repos_still_conflict(session, tenant):
    """The mismatch check must still catch what it exists to catch: two
    actually different repositories, even when spelled in different styles."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(
        session,
        juan,
        "payments-api",
        git_locator="git@github.com:acme/payments-api.git",
    )

    with pytest.raises(ProjectLocatorMismatch):
        projects.resolve(
            session,
            juan,
            "payments-api",
            git_locator="https://gitlab.com/customer/payments-api",
        )


def test_a_malformed_locator_is_a_typed_error_not_a_crash(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    with pytest.raises(ProjectInvalidSlug):
        projects.resolve(session, juan, "payments-api", git_locator="not-a-url")


def test_master_key_reaches_any_project_in_its_tenant(session, tenant):
    _user(session, tenant, "usr_juan")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    result = projects.resolve(
        session, _principal(tenant, None, master=True), "payments-api"
    )

    assert result.project.project_slug == "payments-api"


def test_master_key_does_not_lazily_create(session, tenant):
    """A master key has no identity, so there is no owner to assign (§8.1)."""
    with pytest.raises(ProjectNotFound):
        projects.resolve(session, _principal(tenant, None, master=True), "nope")


def test_an_unknown_owner_type_is_rejected(session, tenant):
    """A bad owner_type would make every future authorize() deny, orphaning
    the project — so it is refused at the domain boundary, not just at the
    API edge."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    with pytest.raises(InvalidOwnerType):
        projects.transfer(session, juan, result.project, "team", "grp_x")

    assert result.project.owner_type == "user"


def test_slug_is_normalized_on_the_way_in(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    created = projects.resolve(session, juan, "Payments API")
    found = projects.resolve(session, juan, "payments-api")

    assert created.project.internal_id == found.project.internal_id


def test_a_slug_in_another_tenant_is_invisible(session, tenant):
    """The tenant filter is the whole isolation story; nothing tested it.

    Deleting the tenant clause from _live used to leave every test green.
    """
    from memory.models import Tenant

    session.add(Tenant(id="ten_other"))
    session.flush()
    _user(session, tenant, "usr_juan")
    other = User(
        id="usr_other", tenant_id="ten_other", bank_id=ids.new_user_bank_id()
    )
    session.add(other)
    session.flush()

    mine = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    theirs = projects.resolve(
        session, _principal("ten_other", "usr_other"), "payments-api"
    )

    assert mine.project.internal_id != theirs.project.internal_id
    assert mine.project.bank_id != theirs.project.bank_id


def test_master_key_create_rejects_a_nonexistent_user_owner(session, tenant):
    """Gutting the existence check in _validate_owner would let this through
    and permanently orphan the project: authorize() then denies everyone,
    since no real user ever matches owner_id, and only a master key could
    even attempt a transfer to fix it."""
    master = _principal(tenant, None, master=True)

    with pytest.raises(UserNotFound):
        projects.create(session, master, "payments-api", "user", "usr_ghost")

    assert session.query(Project).count() == 0


def test_master_key_create_rejects_a_nonexistent_group_owner(session, tenant):
    from memory.errors import GroupNotFound

    master = _principal(tenant, None, master=True)

    with pytest.raises(GroupNotFound):
        projects.create(session, master, "payments-api", "group", "grp_ghost")

    assert session.query(Project).count() == 0


def test_transfer_rejects_a_nonexistent_owner(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    with pytest.raises(UserNotFound):
        projects.transfer(session, juan, result.project, "user", "usr_ghost")

    assert result.project.owner_id == "usr_juan"


def test_create_rejects_an_owner_from_another_tenant(session, tenant):
    """The existence check alone is not enough: an owner id that resolves to
    a real row in someone ELSE's tenant must be rejected just as hard as one
    that does not exist at all."""
    from memory.models import Tenant

    session.add(Tenant(id="ten_other"))
    session.flush()
    session.add(
        User(id="usr_other", tenant_id="ten_other", bank_id=ids.new_user_bank_id())
    )
    session.flush()
    master = _principal(tenant, None, master=True)

    with pytest.raises(UserNotFound):
        projects.create(session, master, "payments-api", "user", "usr_other")

    assert session.query(Project).count() == 0


def test_transfer_rejects_an_owner_from_another_tenant(session, tenant):
    from memory.models import Tenant

    session.add(Tenant(id="ten_other"))
    session.flush()
    session.add(
        User(id="usr_other", tenant_id="ten_other", bank_id=ids.new_user_bank_id())
    )
    session.flush()
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    with pytest.raises(UserNotFound):
        projects.transfer(session, juan, result.project, "user", "usr_other")

    assert result.project.owner_id == "usr_juan"


def test_rename_denies_an_unauthorized_caller(session, tenant):
    """Pins authorize() inside rename() itself, independent of resolve()'s own
    authorize() call: the route only reaches rename() with an already-
    authorized project, so this is the ONLY thing that would catch
    rename()'s own authorize() call being deleted."""
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    with pytest.raises(ProjectAccessDenied):
        projects.rename(
            session, _principal(tenant, "usr_alice"), result.project, "new-slug"
        )

    assert result.project.project_slug == "payments-api"


def test_transfer_denies_an_unauthorized_caller(session, tenant):
    """Same reasoning as test_rename_denies_an_unauthorized_caller, for
    transfer()'s own authorize() call."""
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    with pytest.raises(ProjectAccessDenied):
        projects.transfer(
            session, _principal(tenant, "usr_alice"), result.project, "user", "usr_alice"
        )

    assert result.project.owner_id == "usr_juan"


def _force_race(
    monkeypatch, session, tenant: str, slug: str, owner_type: str, owner_id: str
) -> Project:
    """Deterministically reproduces SPEC §9's creation race without real
    concurrency: the winner's row is already committed to the session when
    the loser's INSERT runs, but the loser's slug-taken pre-check is forced to
    miss — exactly as it would if it ran before the winner's project existed."""
    winner = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        project_slug=slug,
        owner_type=owner_type,
        owner_id=owner_id,
        bank_id=ids.new_project_bank_id(),
    )
    session.add(winner)
    session.flush()
    monkeypatch.setattr(projects, "_slug_taken", lambda *a, **k: False)
    return winner


def test_race_loser_unauthorized_is_denied(session, tenant, monkeypatch):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_bob")
    _force_race(monkeypatch, session, tenant, "payments-api", "user", "usr_juan")

    with pytest.raises(ProjectAccessDenied):
        projects._create(session, _principal(tenant, "usr_bob"), "payments-api", None)

    assert session.query(Project).count() == 1


def test_race_loser_authorized_gets_the_winners_project(session, tenant, monkeypatch):
    _user(session, tenant, "usr_juan")
    alice = _user(session, tenant, "usr_alice")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    session.add(GroupMember(group_id="grp_payments", user_id=alice.id))
    session.flush()
    winner = _force_race(
        monkeypatch, session, tenant, "payments-api", "group", "grp_payments"
    )

    result = projects._create(
        session, _principal(tenant, "usr_alice"), "payments-api", None
    )

    assert result.internal_id == winner.internal_id
    assert session.query(Project).count() == 1


def test_earlier_write_survives_the_race_savepoint_rollback(session, tenant, monkeypatch):
    """The entire reason create() uses db.begin_nested() instead of
    db.rollback(): a bare rollback would discard this earlier, still-
    uncommitted write along with the losing INSERT."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    session.add(Group(id="grp_earlier_write", tenant_id=tenant))
    session.flush()
    _force_race(monkeypatch, session, tenant, "payments-api", "user", "usr_juan")

    projects._create(session, juan, "payments-api", None)

    assert session.get(Group, "grp_earlier_write") is not None


def test_a_denial_after_a_forward_does_not_disclose_the_new_slug(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "old-slug")
    projects.rename(session, juan, result.project, "secret-new-slug")

    with pytest.raises(ProjectAccessDenied) as caught:
        projects.resolve(session, _principal(tenant, "usr_alice"), "old-slug")

    assert caught.value.details["project_slug"] == "old-slug"
    assert "secret-new-slug" not in str(caught.value.details)


def test_a_lost_rename_race_is_a_conflict_not_a_500(session, tenant, monkeypatch):
    """create() got a savepoint and an IntegrityError -> ProjectSlugConflict
    mapping; rename() got neither, though it mutates through TWO unique
    constraints (projects and retired_slugs). SPEC §18 names "rename to an
    existing live or retired slug" as PROJECT_SLUG_CONFLICT, and it was a 500
    (2026-08-23 review, R1-#1)."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    # Another caller already took "payments" -- _force_race also patches
    # _slug_taken so rename()'s own pre-check misses it, exactly as it would
    # if the winner's commit landed between the check and the write.
    _force_race(monkeypatch, session, tenant, "payments", "user", "usr_juan")

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, juan, result.project, "payments")


def test_a_lost_race_on_the_tombstone_is_also_a_conflict(session, tenant):
    """rename() mutates through TWO unique constraints, and the suite only
    drove one of them.

    `_slug_taken` checks the NEW slug; nothing checks whether the OLD slug's
    tombstone already exists. Two concurrent renames of the same project both
    insert RetiredSlug(tenant, old_slug), and the loser violates that composite
    primary key -- a different constraint from the `projects` one
    `_force_race` exercises, reaching the same savepoint. Unguarded it was a
    500; §18 requires PROJECT_SLUG_CONFLICT for both.
    """
    from memory.models import RetiredSlug

    _user(session, tenant, "usr_owner")
    principal = _principal(tenant, "usr_owner")
    project = projects.resolve(session, principal, "alpha").project
    session.flush()

    # The winner of the race already retired "alpha" and committed.
    session.add(
        RetiredSlug(
            tenant_id=tenant,
            retired_slug="alpha",
            project_internal_id=project.internal_id,
        )
    )
    session.flush()

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, principal, project, "beta")


def _external(tenant, user_id, groups):
    """A principal as an identity provider produces it: a real user id, no
    api-key row, and group membership asserted by the token rather than
    stored in group_members."""
    return Principal(
        tenant_id=tenant,
        user_id=user_id,
        is_master=False,
        key_id=None,
        groups=frozenset(groups),
        credential_id="ext_test",
    )


def _group_owned(session, tenant, group_id="grp_payments"):
    """A group-owned project, reached the way the existing tests reach one:
    lazily created by its first toucher, then transferred to the group."""
    _user(session, tenant, "usr_juan")
    session.add(Group(id=group_id, tenant_id=tenant))
    session.flush()
    result = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    projects.transfer(
        session, _principal(tenant, "usr_juan"), result.project, "group", group_id
    )
    return result.project


def test_an_asserted_group_authorizes_without_a_membership_row(session, tenant):
    """The whole point of the external path: the IdP asserts membership, so no
    group_members row has to exist for the caller to reach the project."""
    project = _group_owned(session, tenant)
    alice = _user(session, tenant, "usr_alice")

    reached = projects.resolve(
        session, _external(tenant, alice.id, {"grp_payments"}), "payments-api"
    )

    assert reached.project.bank_id == project.bank_id
    assert session.get(GroupMember, ("grp_payments", alice.id)) is None


def test_a_group_the_token_does_not_assert_is_denied(session, tenant):
    _group_owned(session, tenant)
    alice = _user(session, tenant, "usr_alice")

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(
            session, _external(tenant, alice.id, {"grp_something-else"}), "payments-api"
        )


def test_owning_a_project_materializes_the_asserted_group(session, tenant):
    """authorize() needs no row, but _validate_owner does -- and the group only
    has to exist at the moment someone hands it a project."""
    alice = _user(session, tenant, "usr_alice")
    principal = _external(tenant, alice.id, {"grp_platform"})

    project = projects.create(session, principal, "acme-api", "group", "grp_platform")

    assert session.get(Group, "grp_platform") is not None
    assert project.owner_id == "grp_platform"


def test_a_group_the_token_does_not_assert_is_not_materialized(session, tenant):
    """Lazy creation is bounded by the assertion: it must not become a way to
    conjure an arbitrary group id."""
    alice = _user(session, tenant, "usr_alice")
    principal = _external(tenant, alice.id, {"grp_platform"})

    with pytest.raises(GroupNotFound):
        projects.create(session, principal, "acme-api", "group", "grp_other")

    assert session.get(Group, "grp_other") is None
