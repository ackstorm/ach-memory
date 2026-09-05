import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from memory import ids
from memory.auth import keys
from memory.models import ApiKey, User


def test_user_persists_with_its_bank_id(session, tenant):
    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    stored = session.get(User, user.id)
    assert stored.bank_id.startswith("user_")


def test_bank_id_is_unique(session, tenant):
    bank_id = ids.new_user_bank_id()
    session.add(User(id=ids.new_user_id(), tenant_id=tenant, bank_id=bank_id))
    session.flush()
    session.add(User(id=ids.new_user_id(), tenant_id=tenant, bank_id=bank_id))

    with pytest.raises(IntegrityError):
        session.flush()


def test_api_key_stores_only_a_hash(session, tenant):
    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    plaintext = keys.generate_key()
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=tenant,
            user_id=user.id,
            secret_hash=keys.hash_key(plaintext),
        )
    )
    session.flush()

    stored = session.query(ApiKey).one()
    assert stored.secret_hash != plaintext
    assert stored.status == "active"


def test_api_key_row_without_a_user_is_rejected(session, tenant):
    """The master key is configuration, never a row (SPEC §5.2). A user-less
    row must be impossible at the schema level, because principal resolution
    would otherwise have to decide what it means."""
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=tenant,
            user_id=None,
            secret_hash=keys.hash_key(keys.generate_key()),
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_project_slug_is_unique_per_tenant(session, tenant):
    from sqlalchemy import insert

    from memory.models import Project, ProjectSlug

    def _project(slug: str) -> Project:
        project = Project(
            internal_id=ids.new_project_internal_id(),
            tenant_id=tenant,
            owner_type="user",
            owner_id="usr_x",
            bank_id=ids.new_project_bank_id(),
        )
        project.slug_rows.append(
            ProjectSlug(tenant_id=tenant, slug=slug, is_canonical=True)
        )
        return project

    first = _project("payments-api")
    session.add(first)
    session.flush()
    second = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        owner_type="user",
        owner_id="usr_x",
        bank_id=ids.new_project_bank_id(),
    )
    session.add(second)
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            insert(ProjectSlug).values(
                tenant_id=tenant,
                slug="payments-api",
                project_internal_id=second.internal_id,
                is_canonical=True,
            )
        )


def test_the_same_slug_may_exist_in_two_tenants(session, tenant):
    """Proves the constraint is composite, not global."""
    from memory.models import Project, ProjectSlug, Tenant

    session.add(Tenant(id="ten_other"))
    session.flush()

    for tenant_id in (tenant, "ten_other"):
        project = Project(
            internal_id=ids.new_project_internal_id(),
            tenant_id=tenant_id,
            owner_type="user",
            owner_id="usr_x",
            bank_id=ids.new_project_bank_id(),
        )
        project.slug_rows.append(
            ProjectSlug(
                tenant_id=tenant_id, slug="payments-api", is_canonical=True
            )
        )
        session.add(project)
    session.flush()

    assert session.query(Project).count() == 2


def test_git_locator_is_not_unique(session, tenant):
    """Two projects may legitimately record the same locator (SPEC §17)."""
    from memory.models import Project, ProjectSlug

    for slug in ("one", "two"):
        project = Project(
            internal_id=ids.new_project_internal_id(),
            tenant_id=tenant,
            git_locator="github.com/acme/payments-api",
            owner_type="user",
            owner_id="usr_x",
            bank_id=ids.new_project_bank_id(),
        )
        project.slug_rows.append(
            ProjectSlug(tenant_id=tenant, slug=slug, is_canonical=True)
        )
        session.add(project)
    session.flush()

    assert session.query(Project).count() == 2


def test_alias_slug_points_at_a_project(session, tenant):
    from memory.models import Project, ProjectSlug

    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        owner_type="user",
        owner_id="usr_x",
        bank_id=ids.new_project_bank_id(),
    )
    project.slug_rows.append(
        ProjectSlug(tenant_id=tenant, slug="payments-service", is_canonical=True)
    )
    session.add(project)
    session.flush()
    session.add(
        ProjectSlug(
            tenant_id=tenant,
            slug="github.com-acme-payments-api",
            project_internal_id=project.internal_id,
            is_canonical=False,
        )
    )
    session.flush()

    stored = session.get(ProjectSlug, (tenant, "github.com-acme-payments-api"))
    assert stored.project_internal_id == project.internal_id
    assert stored.is_canonical is False


def test_audit_event_records_the_actor(session, tenant):
    from memory.models import AuditEvent

    session.add(
        AuditEvent(
            id=ids.new_audit_id(),
            tenant_id=tenant,
            actor_key_id=None,
            on_behalf_of="usr_alice",
            action="project.transfer",
            resource="payments-api",
        )
    )
    session.flush()

    assert session.query(AuditEvent).one().on_behalf_of == "usr_alice"


def test_audit_event_created_at_is_a_db_default_not_a_python_default(
    session, connection, tenant
):
    """2026-08-23 review, finding 2: `default=utcnow` and `server_default=
    func.now()` were both set on this column. SQLAlchemy always prefers the
    Python-side `default` when both are present, so it stamped the
    INSERTING POD's clock on every row and the DDL default never fired --
    the migration that added server_default was decorative.

    Pinned directly on the compiled INSERT, the same way the review found
    it: reintroducing `default=utcnow` puts `created_at` back in the
    column list below with a bound parameter, turning this red.
    """
    from memory.models import AuditEvent

    captured = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO audit_events" in statement:
            captured.append(statement)

    event.listen(connection, "before_cursor_execute", _capture)
    try:
        session.add(
            AuditEvent(
                id=ids.new_audit_id(),
                tenant_id=tenant,
                actor_key_id=None,
                on_behalf_of=None,
                action="project.transfer",
                resource="payments-api",
            )
        )
        session.flush()
    finally:
        event.remove(connection, "before_cursor_execute", _capture)

    assert captured, "no INSERT INTO audit_events observed"
    # `created_at` legitimately appears in the ORM's own RETURNING clause
    # (how it reads a DB-generated default back onto the object) -- it's a
    # bound VALUES parameter, `%(created_at)s`, that means the Python-side
    # default won and the DDL default never fired.
    assert "%(created_at)s" not in captured[0], captured[0]


def test_external_identity_is_unique_per_issuer_and_subject(session, tenant):
    from sqlalchemy.exc import IntegrityError

    from memory import ids
    from memory.models import ExternalIdentity, User

    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    def _row(credential_id):
        return ExternalIdentity(
            issuer="https://ach.example.com",
            subject="alice@example.com",
            tenant_id=tenant,
            user_id=user.id,
            credential_id=credential_id,
        )

    session.add(_row("ext_aaa"))
    session.flush()
    session.add(_row("ext_bbb"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_subject_from_two_issuers_stays_two_identities(session, tenant):
    from memory import ids
    from memory.models import ExternalIdentity, User

    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    for issuer, credential in (
        ("https://ach.example.com", "ext_a"),
        ("https://auth.example.com", "ext_b"),
    ):
        session.add(
            ExternalIdentity(
                issuer=issuer,
                subject="alice@example.com",
                tenant_id=tenant,
                user_id=user.id,
                credential_id=credential,
            )
        )
    session.flush()  # no constraint violation




def test_activity_event_stamps_created_at_from_the_database(session, tenant):
    """server_default only, no Python-side default -- the same reasoning
    AuditEvent.created_at records: one clock, the database's, or ordering
    across replicas means nothing."""
    from memory import ids
    from memory.models import ActivityEvent

    row = ActivityEvent(
        id=ids.new_activity_id(),
        tenant_id="default",
        action="memory.retain",
        surface="mcp",
        scope="user",
        user_id="usr_1",
        bank_fingerprint="a" * 12,
        outcome="ok",
        duration_ms=12,
    )
    session.add(row)
    session.flush()

    assert row.created_at is not None
    assert row.id.startswith("act_")


def _capture_user_and_project(session, tenant, *, user_id: str = "usr_cap", slug: str = "acme-api"):
    from memory.models import Project, ProjectSlug, User

    user = User(id=user_id, tenant_id=tenant, bank_id=ids.new_user_bank_id())
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        owner_type="user",
        owner_id=user_id,
        bank_id=ids.new_project_bank_id(),
    )
    project.slug_rows.append(
        ProjectSlug(tenant_id=tenant, slug=slug, is_canonical=True)
    )
    session.add_all([user, project])
    session.flush()
    return user, project






