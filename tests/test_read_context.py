"""`resolve_read_bank` must be existing-only and non-enriching: every case
here snapshots every domain table except the two tolerated writes (audit,
activity) before and after the call and asserts the snapshot is unchanged.

Fixtures are copied from tests/test_projects.py's `_user`/`_principal`
pattern rather than imported, matching this codebase's established
one-fixture-set-per-file convention (see tests/test_brief.py's own
`_profile_item` docstring for the same reasoning).
"""

import inspect

import pytest
from sqlalchemy import inspect as sa_inspect

from memory import ids, projects, read_context
from memory.auth.principal import Principal
from memory.errors import (
    Forbidden,
    ProjectContextUnavailable,
    ProjectNotFound,
    UserNotFound,
)
from memory.models import (
    AuditEvent,
    ExternalIdentity,
    Group,
    Project,
    ProjectSlug,
    User,
    WorkingSession,
    WorkingState,
)
from tests.conftest import OPERATOR_SUBJECT

# Every domain table EXCEPT the two tolerated writes around a read: audit
# events (mandatory delegated-master audit, SPEC §20 MUST) and activity
# events (content-free operational telemetry, and in any case never written
# by a bare domain-level call here -- activity.finish(), the actual writer,
# runs only at a REST/MCP edge this test never reaches).
_DOMAIN_MODELS = [
    User,
    ExternalIdentity,
    Group,
    Project,
    ProjectSlug,
    WorkingSession,
    WorkingState,
]


@pytest.fixture(autouse=True)
def _operator_config(monkeypatch):
    """Operator authority is configuration, and `Principal.is_master` reads it
    through the module-level settings cache. These tests call the resolver
    directly, with no `app` fixture to name the operator for them."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MASTER_USERS", OPERATOR_SUBJECT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _row_tuple(row) -> tuple:
    mapper = sa_inspect(type(row)).mapper
    return tuple(getattr(row, column.key) for column in mapper.columns)


def _snapshot(session) -> dict[str, list[tuple]]:
    return {
        model.__tablename__: sorted(
            (_row_tuple(row) for row in session.query(model).all()),
            key=lambda values: tuple(repr(value) for value in values),
        )
        for model in _DOMAIN_MODELS
    }


def _user(session, tenant, user_id: str) -> User:
    user = User(id=user_id, tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return user


def _principal(tenant: str, user_id: str, master: bool = False) -> Principal:
    """`master` is no longer carried by the credential: it is the subject the
    `_operator_config` fixture named in MEMORY_MASTER_USERS. An operator is an
    ordinary user who happens to be named there, so they still have a user_id
    of their own."""
    return Principal(
        tenant_id=tenant,
        user_id=user_id,
        credential_id="ext_x",
        subject=OPERATOR_SUBJECT if master else f"{user_id}@test",
    )


def test_the_resolver_never_imports_hindsight():
    """Structural, not behavioral: a read resolver that had no way to reach
    Hindsight at all cannot someday reach it before authorization finishes."""
    assert not any(
        "hindsight" in line.lower()
        for line in inspect.getsource(read_context).splitlines()
        if line.startswith(("import ", "from "))
    )


def test_an_existing_user_resolves_with_no_domain_writes(session, tenant):
    juan = _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    before = _snapshot(session)

    result = read_context.resolve_read_bank(
        session, principal, None, "read.recall", "user"
    )

    assert result.bank_id == juan.bank_id
    assert result.scope == "user"
    assert result.user_id == "usr_juan"
    assert result.project_internal_id is None
    assert _snapshot(session) == before


def test_an_existing_project_resolves_with_no_domain_writes_and_no_enrichment(
    session, tenant
):
    """`git_locator=NULL` going in must still be `NULL` coming out: the read
    contract has no parameter to feed `projects.resolve`'s enrichment branch
    a value, so there is nothing for it to bind even implicitly."""
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    created = projects.resolve(session, principal, "payments-api")
    assert created.project.git_locator is None
    before = _snapshot(session)

    result = read_context.resolve_read_bank(
        session, principal, None, "read.recall", "project", project_slug="payments-api"
    )

    assert result.bank_id == created.project.bank_id
    assert result.scope == "project"
    assert result.project_internal_id == created.project.internal_id
    assert result.current_slug == "payments-api"
    assert result.resolved_from is None
    assert _snapshot(session) == before
    assert session.get(Project, created.project.internal_id).git_locator is None


def test_an_absent_project_is_not_found_with_no_domain_writes(session, tenant):
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    before = _snapshot(session)

    with pytest.raises(ProjectNotFound):
        read_context.resolve_read_bank(
            session, principal, None, "read.recall", "project", project_slug="no-such-project"
        )

    assert _snapshot(session) == before


def test_a_retired_slug_still_forwards_with_no_domain_writes(session, tenant):
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    created = projects.resolve(session, principal, "old-slug")
    projects.rename(session, principal, created.project, "new-slug")
    before = _snapshot(session)

    result = read_context.resolve_read_bank(
        session, principal, None, "read.recall", "project", project_slug="old-slug"
    )

    assert result.current_slug == "new-slug"
    assert result.resolved_from == "old-slug"
    assert _snapshot(session) == before


def test_an_unauthorized_project_is_hidden_with_no_domain_writes(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    owner = _principal(tenant, "usr_juan")
    stranger = _principal(tenant, "usr_alice")
    projects.resolve(session, owner, "payments-api")
    before = _snapshot(session)

    with pytest.raises(ProjectNotFound):
        read_context.resolve_read_bank(
            session, stranger, None, "read.recall", "project", project_slug="payments-api"
        )

    assert _snapshot(session) == before


def test_no_project_slug_is_a_typed_error_with_no_domain_writes(session, tenant):
    _user(session, tenant, "usr_juan")
    principal = _principal(tenant, "usr_juan")
    before = _snapshot(session)

    with pytest.raises(ProjectContextUnavailable):
        read_context.resolve_read_bank(session, principal, None, "read.recall", "project")

    assert _snapshot(session) == before


def test_a_delegated_master_read_writes_only_the_audit_row(session, tenant):
    """The one tolerated write: everything else, including the target
    user's own row, must be untouched."""
    juan = _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_operator")
    master = _principal(tenant, "usr_operator", master=True)
    before = _snapshot(session)
    assert session.query(AuditEvent).count() == 0

    result = read_context.resolve_read_bank(
        session, master, "on-call-investigation", "read.recall", "user", user_id="usr_juan"
    )

    assert result.bank_id == juan.bank_id
    assert _snapshot(session) == before
    audit_rows = session.query(AuditEvent).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].resource == "usr_juan"
    assert audit_rows[0].on_behalf_of == "on-call-investigation"


def test_a_delegated_master_project_read_writes_only_the_audit_row(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_operator")
    owner = _principal(tenant, "usr_juan")
    master = _principal(tenant, "usr_operator", master=True)
    created = projects.resolve(session, owner, "payments-api")
    # The creation itself is now audited too (every creation is, not only a
    # master key's) -- baseline includes that one row, not zero.
    assert session.query(AuditEvent).count() == 1
    before = _snapshot(session)

    result = read_context.resolve_read_bank(
        session,
        master,
        "on-call-investigation",
        "read.recall",
        "project",
        project_slug="payments-api",
    )

    assert result.bank_id == created.project.bank_id
    assert _snapshot(session) == before
    read_rows = session.query(AuditEvent).filter_by(action="read.recall").all()
    assert len(read_rows) == 1
    assert read_rows[0].resource == "payments-api"


def test_a_user_key_naming_another_user_is_forbidden_with_no_domain_writes(
    session, tenant
):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    principal = _principal(tenant, "usr_juan")
    before = _snapshot(session)

    with pytest.raises(Forbidden):
        read_context.resolve_read_bank(
            session, principal, None, "read.recall", "user", user_id="usr_alice"
        )

    assert _snapshot(session) == before


def test_a_master_key_naming_a_missing_user_is_user_not_found(session, tenant):
    _user(session, tenant, "usr_operator")
    master = _principal(tenant, "usr_operator", master=True)
    before = _snapshot(session)

    with pytest.raises(UserNotFound):
        read_context.resolve_read_bank(
            session, master, None, "read.recall", "user", user_id="usr_ghost"
        )

    assert _snapshot(session) == before
