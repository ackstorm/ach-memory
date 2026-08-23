import pytest
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
    from memory.models import Project

    def _project(slug: str) -> Project:
        return Project(
            internal_id=ids.new_project_internal_id(),
            tenant_id=tenant,
            project_slug=slug,
            owner_type="user",
            owner_id="usr_x",
            bank_id=ids.new_project_bank_id(),
        )

    session.add(_project("payments-api"))
    session.flush()
    session.add(_project("payments-api"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_slug_may_exist_in_two_tenants(session, tenant):
    """Proves the constraint is composite, not global."""
    from memory.models import Project, Tenant

    session.add(Tenant(id="ten_other"))
    session.flush()

    for tenant_id in (tenant, "ten_other"):
        session.add(
            Project(
                internal_id=ids.new_project_internal_id(),
                tenant_id=tenant_id,
                project_slug="payments-api",
                owner_type="user",
                owner_id="usr_x",
                bank_id=ids.new_project_bank_id(),
            )
        )
    session.flush()

    assert session.query(Project).count() == 2


def test_git_locator_is_not_unique(session, tenant):
    """Two projects may legitimately record the same locator (SPEC §17)."""
    from memory.models import Project

    for slug in ("one", "two"):
        session.add(
            Project(
                internal_id=ids.new_project_internal_id(),
                tenant_id=tenant,
                project_slug=slug,
                git_locator="github.com/acme/payments-api",
                owner_type="user",
                owner_id="usr_x",
                bank_id=ids.new_project_bank_id(),
            )
        )
    session.flush()

    assert session.query(Project).count() == 2


def test_retired_slug_points_at_a_project(session, tenant):
    from memory.models import Project, RetiredSlug

    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        project_slug="payments-service",
        owner_type="user",
        owner_id="usr_x",
        bank_id=ids.new_project_bank_id(),
    )
    session.add(project)
    session.flush()
    session.add(
        RetiredSlug(
            tenant_id=tenant,
            retired_slug="github.com-acme-payments-api",
            project_internal_id=project.internal_id,
        )
    )
    session.flush()

    stored = session.get(RetiredSlug, (tenant, "github.com-acme-payments-api"))
    assert stored.project_internal_id == project.internal_id


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
