import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

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
    RateLimited,
    UserNotFound,
)
from memory.models import (
    AuditEvent,
    Group,
    Project,
    ProjectSlug,
    Tenant,
    User,
)


def _user(session, tenant, user_id: str) -> User:
    user = User(id=user_id, tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return user


def _principal(tenant: str, user_id: str | None, subject: str | None = None) -> Principal:
    return Principal(
        tenant_id=tenant, user_id=user_id, credential_id="ext_x", subject=subject
    )


OPERATOR_ID = "usr_operator"
OPERATOR_SUBJECT = "operator@example.com"


@pytest.fixture
def operator(session, tenant, monkeypatch) -> Principal:
    """An operator is an ordinary external identity that configuration also
    names. There is no identity-less credential any more, so an operator has
    a user id, a bank and projects of their own like anybody else -- the only
    difference is that MEMORY_MASTER_USERS names them.

    It names the SUBJECT, not the user id: `usr_operator` is minted locally
    on first sight, so an administrator could not write it into configuration
    before that operator had ever logged in.
    """
    from memory.config import get_settings

    _user(session, tenant, OPERATOR_ID)
    monkeypatch.setenv("MEMORY_MASTER_USERS", OPERATOR_SUBJECT)
    get_settings.cache_clear()
    yield _principal(tenant, OPERATOR_ID, OPERATOR_SUBJECT)
    get_settings.cache_clear()


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


def test_an_unauthorized_project_is_indistinguishable_from_missing(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    with pytest.raises(ProjectNotFound) as caught:
        projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert caught.value.details["project_slug"] == "payments-api"
    assert "owner_type" not in caught.value.details
    assert "owner_id" not in caught.value.details
    assert "usr_juan" not in str(caught.value.details)


def test_no_second_bank_is_created_for_the_denied_caller(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    with pytest.raises(ProjectNotFound):
        projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert session.query(Project).count() == 1


def test_group_access_comes_only_from_the_identity_provider(session, tenant):
    """A behaviour change to name, not a no-op. `authorize` used to grant on
    an asserted group OR a local `group_members` row, checked independently.
    The row is gone, so a caller their token does not vouch for is denied --
    there is no second place membership can come from, which is also what
    makes an IdP revocation take effect on the very next request."""
    _user(session, tenant, "usr_juan")
    alice = _user(session, tenant, "usr_alice")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()

    result = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    projects.transfer(
        session, _principal(tenant, "usr_juan"), result.project, "group", "grp_payments"
    )

    with pytest.raises(ProjectNotFound):
        projects.resolve(session, _external(tenant, alice.id, set()), "payments-api")

    for_alice = projects.resolve(
        session, _external(tenant, alice.id, {"grp_payments"}), "payments-api"
    )
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

    with pytest.raises(ProjectNotFound):
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

    with pytest.raises(ProjectNotFound):
        projects.resolve(session, juan, "payments-api")


def test_rename_leaves_a_forwarding_tombstone(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "github.com-acme-payments-api")
    bank_before = result.project.bank_id

    projects.rename(session, juan, result.project, "payments-api")

    forwarded = projects.resolve(session, juan, "github.com-acme-payments-api")

    assert forwarded.current_slug == "payments-api"
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
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "a")
    projects.rename(session, juan, result.project, "b")
    projects.rename(session, juan, result.project, "c")

    from_a = projects.resolve(session, juan, "a")
    alias = session.get(ProjectSlug, (tenant, "a"))

    assert from_a.current_slug == "c"
    assert alias.project_internal_id == result.project.internal_id
    assert alias.is_canonical is False


def test_a_retired_slug_cannot_be_reused(session, tenant):
    from memory.errors import ProjectSlugConflict

    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "a")
    projects.rename(session, juan, result.project, "b")
    second = projects.resolve(session, juan, "c")

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, juan, second.project, "a")


def test_alias_and_canonical_slug_share_one_namespace(session, tenant):
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    project = projects.resolve(session, principal, "ach-memory").project

    projects.rename(session, principal, project, "renamed")
    session.commit()

    with pytest.raises(ProjectSlugConflict):
        projects.create(
            session, principal, "ach-memory", "user", principal.user_id
        )


def test_rename_collision_rolls_back_canonical_change(session, tenant):
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    project = projects.resolve(session, principal, "ach-memory").project
    other_project = projects.resolve(session, principal, "other-project").project
    original = projects.canonical_slug(session, project)
    session.commit()

    with pytest.raises(ProjectSlugConflict):
        projects.rename(
            session,
            principal,
            project,
            projects.canonical_slug(session, other_project),
        )
    session.rollback()

    assert (
        projects.resolve(session, principal, original, create=False).project.internal_id
        == project.internal_id
    )


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


def test_an_operator_reaches_any_project_in_its_tenant(session, tenant, operator):
    _user(session, tenant, "usr_juan")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    result = projects.resolve(session, operator, "payments-api")

    assert result.current_slug == "payments-api"


def test_an_operator_lazily_creates_a_project_they_own(session, tenant, operator):
    """The old refusal existed because a master key had no identity to assign
    as the owner. An operator has one, so the exception disappears rather
    than being ported: they create like anybody else, and own what they
    create."""
    result = projects.resolve(session, operator, "brand-new")

    assert result.project.owner_type == "user"
    assert result.project.owner_id == OPERATOR_ID


def test_every_project_creation_is_audited(session, tenant):
    """Not just master-key creations. The audit trail is the only durable
    record of who created what, and from here it is also the counting source
    for the creation rate limit -- an unaudited path is an unlimited path."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    projects.create(session, juan, "acme/app", "user", "usr_juan")
    session.flush()

    events = session.query(AuditEvent).filter(AuditEvent.action == "project.create").all()
    assert len(events) == 1


def test_a_user_cannot_create_more_than_the_hourly_limit(session, tenant):
    """A hallucinated project_slug on a lazy retain creates a real project,
    a Hindsight bank, a retain strategy and a built-in model. Banks are not
    free, so a typo storm must not become a bank storm."""
    _user(session, tenant, "usr_juan")
    juan = Principal(
        tenant_id=tenant, user_id="usr_juan", credential_id="ext_juan"
    )

    for n in range(10):
        projects.create(session, juan, f"acme/app-{n}", "user", "usr_juan")
    session.flush()

    with pytest.raises(RateLimited):
        projects.create(session, juan, "acme/app-11", "user", "usr_juan")


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


def test_create_rejects_a_nonexistent_user_owner(session, tenant, operator):
    """Gutting the existence check in _validate_owner would let this through
    and permanently orphan the project: authorize() then denies everyone,
    since no real user ever matches owner_id, and only an operator could
    even attempt a transfer to fix it."""
    with pytest.raises(UserNotFound):
        projects.create(session, operator, "payments-api", "user", "usr_ghost")

    assert session.query(Project).count() == 0


def test_create_rejects_a_nonexistent_group_owner(session, tenant, operator):
    from memory.errors import GroupNotFound

    with pytest.raises(GroupNotFound):
        projects.create(session, operator, "payments-api", "group", "grp_ghost")

    assert session.query(Project).count() == 0


def test_transfer_rejects_a_nonexistent_owner(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    with pytest.raises(UserNotFound):
        projects.transfer(session, juan, result.project, "user", "usr_ghost")

    assert result.project.owner_id == "usr_juan"


def test_create_rejects_an_owner_from_another_tenant(session, tenant, operator):
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

    with pytest.raises(UserNotFound):
        projects.create(session, operator, "payments-api", "user", "usr_other")

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

    assert projects.canonical_slug(session, result.project) == "payments-api"


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
        owner_type=owner_type,
        owner_id=owner_id,
        bank_id=ids.new_project_bank_id(),
    )
    session.add(winner)
    session.flush()
    session.add(
        ProjectSlug(
            tenant_id=tenant,
            slug=slug,
            project_internal_id=winner.internal_id,
            is_canonical=True,
        )
    )
    session.flush()
    monkeypatch.setattr(projects, "_slug_taken", lambda *a, **k: False)
    return winner


def _committed_concurrency_scope(engine, *slugs: str):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = f"slug-race-{uuid4().hex[:12]}"
    user_id = f"usr_{uuid4().hex[:12]}"
    principal = _principal(tenant_id, user_id)
    project_ids = []
    with factory.begin() as db:
        db.add(Tenant(id=tenant_id))
        db.add(User(id=user_id, tenant_id=tenant_id, bank_id=ids.new_user_bank_id()))
        db.flush()
        for slug in slugs:
            project_ids.append(
                projects.create(db, principal, slug, "user", user_id).internal_id
            )
    return factory, principal, project_ids


def _cleanup_concurrency_scope(factory, tenant_id: str) -> None:
    with factory.begin() as db:
        db.query(AuditEvent).filter_by(tenant_id=tenant_id).delete()
        db.query(ProjectSlug).filter_by(tenant_id=tenant_id).delete()
        db.query(Project).filter_by(tenant_id=tenant_id).delete()
        db.query(User).filter_by(tenant_id=tenant_id).delete()
        db.query(Tenant).filter_by(id=tenant_id).delete()


def _set_application_name(db, application_name: str) -> None:
    db.execute(
        text("SELECT set_config('application_name', :application_name, true)"),
        {"application_name": application_name},
    )


def _wait_for_application_lock(
    engine, application_name: str, query_fragment: str
) -> str:
    deadline = time.monotonic() + 5
    last_state = None
    with engine.connect() as observer:
        while time.monotonic() < deadline:
            observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            row = observer.execute(
                text(
                    "SELECT state, wait_event_type, query FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND application_name = :application_name"
                ),
                {"application_name": application_name},
            ).one_or_none()
            last_state = tuple(row) if row is not None else None
            if (
                row is not None
                and row.wait_event_type == "Lock"
                and query_fragment in row.query
            ):
                return row.query
            time.sleep(0.02)
    raise AssertionError(f"connection did not wait on a lock; last state={last_state!r}")


def test_concurrent_creates_have_one_winner_and_one_typed_conflict(engine):
    factory, principal, _ = _committed_concurrency_scope(engine)
    winner_ready = Event()
    release_winner = Event()
    loser_application = f"slug-create-loser-{uuid4().hex[:8]}"

    def _winner() -> str:
        with factory() as db:
            project = projects.create(
                db, principal, "contested", "user", principal.user_id
            )
            winner_ready.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("winner was not released")
            db.commit()
            return project.internal_id

    def _loser() -> str:
        if not winner_ready.wait(timeout=10):
            raise AssertionError("winner did not insert its namespace row")
        with factory() as db:
            _set_application_name(db, loser_application)
            try:
                projects.create(
                    db, principal, "contested", "user", principal.user_id
                )
            except ProjectSlugConflict:
                db.rollback()
                return "conflict"
            db.commit()
            return "created"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(_winner)
            assert winner_ready.wait(timeout=10)
            loser = pool.submit(_loser)
            try:
                waiting_query = _wait_for_application_lock(
                    engine, loser_application, "INSERT INTO project_slugs"
                )
            finally:
                release_winner.set()
            winner_id = winner.result(timeout=10)
            assert loser.result(timeout=10) == "conflict"

        assert "INSERT INTO project_slugs" in waiting_query
        with factory() as db:
            assert db.query(Project).filter_by(tenant_id=principal.tenant_id).count() == 1
            mapping = db.get(ProjectSlug, (principal.tenant_id, "contested"))
            assert mapping.project_internal_id == winner_id
            assert mapping.is_canonical is True
    finally:
        release_winner.set()
        _cleanup_concurrency_scope(factory, principal.tenant_id)


def test_concurrent_renames_to_one_slug_preserve_the_loser_canonical(engine):
    factory, principal, project_ids = _committed_concurrency_scope(
        engine, "winner-old", "loser-old"
    )
    winner_ready = Event()
    release_winner = Event()
    loser_application = f"slug-rename-loser-{uuid4().hex[:8]}"

    def _winner() -> None:
        with factory() as db:
            project = db.get(Project, project_ids[0])
            projects.rename(db, principal, project, "contested")
            winner_ready.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("winner was not released")
            db.commit()

    def _loser() -> str:
        if not winner_ready.wait(timeout=10):
            raise AssertionError("winner did not insert its namespace row")
        with factory() as db:
            _set_application_name(db, loser_application)
            project = db.get(Project, project_ids[1])
            try:
                projects.rename(db, principal, project, "contested")
            except ProjectSlugConflict:
                db.rollback()
                return "conflict"
            db.commit()
            return "renamed"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(_winner)
            assert winner_ready.wait(timeout=10)
            loser = pool.submit(_loser)
            try:
                waiting_query = _wait_for_application_lock(
                    engine, loser_application, "INSERT INTO project_slugs"
                )
            finally:
                release_winner.set()
            winner.result(timeout=10)
            assert loser.result(timeout=10) == "conflict"

        assert "INSERT INTO project_slugs" in waiting_query
        with factory() as db:
            winner_mapping = db.get(ProjectSlug, (principal.tenant_id, "contested"))
            loser_mapping = db.get(ProjectSlug, (principal.tenant_id, "loser-old"))
            assert winner_mapping.project_internal_id == project_ids[0]
            assert winner_mapping.is_canonical is True
            assert loser_mapping.project_internal_id == project_ids[1]
            assert loser_mapping.is_canonical is True
    finally:
        release_winner.set()
        _cleanup_concurrency_scope(factory, principal.tenant_id)


def test_concurrent_renames_of_one_project_serialize(engine):
    factory, principal, project_ids = _committed_concurrency_scope(engine, "original")
    winner_ready = Event()
    release_winner = Event()
    follower_application = f"slug-rename-follow-{uuid4().hex[:8]}"

    def _winner() -> None:
        with factory() as db:
            project = db.get(Project, project_ids[0])
            projects.rename(db, principal, project, "first")
            winner_ready.set()
            if not release_winner.wait(timeout=10):
                raise AssertionError("winner was not released")
            db.commit()

    def _follower() -> None:
        if not winner_ready.wait(timeout=10):
            raise AssertionError("winner did not insert its canonical row")
        with factory() as db:
            _set_application_name(db, follower_application)
            project = db.get(Project, project_ids[0])
            projects.rename(db, principal, project, "second")
            db.commit()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(_winner)
            assert winner_ready.wait(timeout=10)
            follower = pool.submit(_follower)
            try:
                waiting_query = _wait_for_application_lock(
                    engine, follower_application, "FROM projects"
                )
            finally:
                release_winner.set()
            winner.result(timeout=10)
            follower.result(timeout=10)

        assert "FROM projects" in waiting_query
        assert "FOR UPDATE" in waiting_query
        with factory() as db:
            mappings = db.query(ProjectSlug).filter_by(
                tenant_id=principal.tenant_id,
                project_internal_id=project_ids[0],
            )
            assert sorted(
                (mapping.slug, mapping.is_canonical) for mapping in mappings
            ) == [("first", False), ("original", False), ("second", True)]
    finally:
        release_winner.set()
        _cleanup_concurrency_scope(factory, principal.tenant_id)


def test_a_project_cannot_have_two_canonical_slug_rows(session, tenant):
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    project = projects.resolve(session, principal, "canonical").project
    session.add(
        ProjectSlug(
            tenant_id=tenant,
            slug="also-canonical",
            project_internal_id=project.internal_id,
            is_canonical=True,
        )
    )

    with pytest.raises(IntegrityError) as caught:
        session.flush()

    assert "uq_project_slugs_canonical_project" in str(caught.value.orig)


def test_race_loser_unauthorized_is_hidden(session, tenant, monkeypatch):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_bob")
    _force_race(monkeypatch, session, tenant, "payments-api", "user", "usr_juan")

    with pytest.raises(ProjectNotFound):
        projects._create(session, _principal(tenant, "usr_bob"), "payments-api", None)

    assert session.query(Project).count() == 1


def test_race_loser_authorized_gets_the_winners_project(session, tenant, monkeypatch):
    _user(session, tenant, "usr_juan")
    alice = _user(session, tenant, "usr_alice")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    winner = _force_race(
        monkeypatch, session, tenant, "payments-api", "group", "grp_payments"
    )

    result = projects._create(
        session, _external(tenant, alice.id, {"grp_payments"}), "payments-api", None
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


def test_an_unauthorized_alias_is_indistinguishable_from_missing(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "old-slug")
    projects.rename(session, juan, result.project, "secret-new-slug")

    with pytest.raises(ProjectNotFound) as caught:
        projects.resolve(session, _principal(tenant, "usr_alice"), "old-slug")

    assert caught.value.details["project_slug"] == "old-slug"
    assert "owner_type" not in caught.value.details
    assert "secret-new-slug" not in str(caught.value.details)


def test_a_lost_rename_race_is_a_conflict_not_a_500(session, tenant, monkeypatch):
    """A namespace INSERT race is a conflict and preserves the old canonical."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "payments-api")

    # Another caller already took "payments" -- _force_race also patches
    # _slug_taken so rename()'s own pre-check misses it, exactly as it would
    # if the winner's commit landed between the check and the write.
    _force_race(monkeypatch, session, tenant, "payments", "user", "usr_juan")

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, juan, result.project, "payments")

    assert projects.canonical_slug(session, result.project) == "payments-api"
    assert projects.resolve(session, juan, "payments-api", create=False).project == (
        result.project
    )


def _external(tenant, user_id, groups):
    """A principal as an identity provider produces it: a real user id and
    group membership asserted by the token, which is now the only place
    membership can come from."""
    return Principal(
        tenant_id=tenant,
        user_id=user_id,
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


def test_a_group_the_token_does_not_assert_is_denied(session, tenant):
    _group_owned(session, tenant)
    alice = _user(session, tenant, "usr_alice")

    with pytest.raises(ProjectNotFound):
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
