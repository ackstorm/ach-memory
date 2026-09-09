import httpx
import pytest
import respx

from memory import audit
from memory.api.app import current_on_behalf_of
from memory.auth.principal import Principal
from memory.models import AuditEvent
from tests.conftest import OPERATOR_SUBJECT, RESOLVER_URL

BASE = "http://hindsight.test"


@pytest.fixture
def juan(new_user) -> dict:
    """An ordinary (non-operator) external user. Defined here rather than
    imported from tests/test_memory_api.py so this file's audit assertions do
    not ride on another module's fixture shape."""
    return new_user()


def _mock_hindsight() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


def operator_credential_id() -> str:
    """What `ext_` id the suite's operator authenticates as.

    Derived, not hardcoded: it is a hash of (issuer, subject), and the whole
    point of the new model is that an operator IS an ordinary external
    identity -- so their audit rows carry a real, resolvable actor instead of
    the NULL the master key used to leave.
    """
    from memory.auth.provisioning import credential_id_for

    return credential_id_for(RESOLVER_URL, OPERATOR_SUBJECT)


def _operator(tenant: str) -> Principal:
    """An operator principal, built directly: an ordinary external identity
    whose `subject` configuration names in MEMORY_MASTER_USERS."""
    return Principal(
        tenant_id=tenant,
        user_id="usr_operator",
        credential_id=operator_credential_id(),
        subject=OPERATOR_SUBJECT,
    )


def _user(tenant: str) -> Principal:
    return Principal(
        tenant_id=tenant, user_id="usr_juan", credential_id="ext_juan",
        subject="juan@test",
    )


def test_a_user_action_records_the_credential_and_no_delegation(session, tenant):
    audit.record(session, _user(tenant), "project.rename", "a -> b")
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id == "ext_juan"
    assert event.on_behalf_of is None
    assert event.action == "project.rename"
    assert event.resource == "a -> b"


def test_an_operator_action_without_delegation_records_the_operator(session, tenant):
    """An operator used to be traceless here -- `actor_key_id` was NULL,
    because the master key was configuration and never a row. It is an
    ordinary external identity now, so the row names the credential that
    acted, and `on_behalf_of` stays NULL when nobody was acted for."""
    audit.record(session, _operator(tenant), "project.transfer", "payments-api")
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id == operator_credential_id()
    assert event.on_behalf_of is None


def test_a_delegated_operator_action_records_the_subject(session, tenant):
    """SPEC §5.2: on_behalf_of is passed in, never derived. An operator now
    HAS an identity of its own, which is exactly why deriving it would be
    wrong -- the derived value would name the operator, not the human they
    are acting for."""
    audit.record(
        session,
        _operator(tenant),
        "project.transfer",
        "payments-api",
        on_behalf_of="usr_alice",
    )
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id == operator_credential_id()
    assert event.on_behalf_of == "usr_alice"


def test_the_event_is_scoped_to_the_principal_tenant(session, tenant):
    audit.record(session, _user(tenant), "project.rename", "a -> b")
    session.flush()

    assert session.query(AuditEvent).one().tenant_id == tenant


@respx.mock
def test_an_operator_reading_a_users_bank_is_audited(
    client, juan, master_headers, tenant, session
):
    """SPEC §20.3. An operator can reach any user's private memory; that must
    not be traceless."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    user_id = juan["user_id"]

    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": user_id, "query": "anything"},
        headers=master_headers,
    )

    # "memory.recall", not the old generic "memory.read_as_user": the action
    # names the route, so an admin can tell it apart from e.g.
    # documents.delete on the same principal (see the granularity test below).
    events = [e for e in session.query(AuditEvent).all() if e.action == "memory.recall"]
    assert len(events) == 1
    assert events[0].resource == user_id


@respx.mock
def test_an_operator_reaching_a_project_bank_is_audited(
    client, juan, master_headers, tenant, session
):
    """The other half of SPEC §20.3: an operator reaching a PROJECT's shared
    bank is at least as sensitive as reaching a user's, and used to be
    traceless -- projects.authorize() returns early for an operator with
    nothing recorded."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    # juan creates and owns the project first through the project-management
    # surface; recall itself is existing-only.
    client.post(
        "/v1/projects",
        json={"project_slug": "juans-secret"},
        headers=juan["headers"],
    )

    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "juans-secret", "query": "y"},
        headers=master_headers,
    )

    assert response.status_code == 200
    events = [
        e
        for e in session.query(AuditEvent).all()
        if e.action == "memory.recall"
        and e.actor_key_id == operator_credential_id()
    ]
    assert len(events) == 1
    assert events[0].resource == "juans-secret"


@respx.mock
def test_operator_actions_across_routes_are_distinguishable(
    client, juan, master_headers, tenant, session
):
    """SPEC §20.3 asks for auditable operator actions, not just a record that
    *some* bank was touched. Before this fix all 15 operator->user-bank routes
    recorded the byte-identical action "memory.read_as_user"."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/[^/]+").mock(
        return_value=httpx.Response(200, json={})
    )
    user_id = juan["user_id"]

    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": user_id, "query": "x"},
        headers=master_headers,
    )
    client.post(
        "/v1/memory/documents/delete",
        json={"scope": "user", "user_id": user_id, "document_id": "doc-1"},
        headers=master_headers,
    )

    actions = {
        e.action
        for e in session.query(AuditEvent).all()
        if e.actor_key_id == operator_credential_id()
        and e.resource == user_id
        and e.action.startswith("memory.")
    }
    assert actions == {"memory.recall", "memory.documents.delete"}


@respx.mock
def test_a_user_reading_its_own_bank_is_not_audited(client, juan, tenant, session):
    """Audit records delegated and privileged access, not ordinary use. A user
    reading their own memory on every agent start would drown the log."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    # Still a before/after count rather than an absolute zero: this file's
    # other fixtures and the caller's own provisioning may have written rows.
    # What matters is that THIS call -- a user reading its own bank -- adds
    # none of its own.
    before = session.query(AuditEvent).count()

    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "anything"},
        headers=juan["headers"],
    )

    assert session.query(AuditEvent).count() == before


def test_the_on_behalf_of_header_is_recorded(client, master_headers, tenant, session):
    """Exercised on project.create -- the `user.create` route it used to use is
    gone with the internal identity system, but the delegation header it was
    pinning survives untouched."""
    response = client.post(
        "/v1/projects",
        json={"project_slug": "payments-api"},
        headers={**master_headers, "On-Behalf-Of": "usr_alice"},
    )

    assert response.status_code == 201, response.text
    event = session.query(AuditEvent).filter_by(action="project.create").one()
    assert event.on_behalf_of == "usr_alice"


def _name_the_operator(monkeypatch) -> None:
    """Grant `OPERATOR_SUBJECT` authority for a test that builds its principal
    by hand instead of going through the `app` fixture (which already sets
    this). Authority is configuration read at call time now, so
    `principal.is_master` is False without it however the Principal is built.
    """
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MASTER_USERS", OPERATOR_SUBJECT)
    get_settings.cache_clear()


def test_current_on_behalf_of_discards_a_user_principals_header(monkeypatch):
    """The trust boundary itself, tested directly rather than through a route.

    An earlier version posted to /v1/projects, which for a non-operator
    principal records no audit event at all -- so `all(e.on_behalf_of is None
    for e in events)` ranged over events that never carried a header in the
    first place and would pass no matter what current_on_behalf_of did.
    Deleting the `is_master` guard in memory/api/app.py left every one of the
    268 prior tests passing."""
    _name_the_operator(monkeypatch)

    assert current_on_behalf_of(_user("default"), "usr_alice") is None


def test_current_on_behalf_of_keeps_an_operator_principals_header(monkeypatch):
    _name_the_operator(monkeypatch)

    assert current_on_behalf_of(_operator("default"), "usr_alice") == "usr_alice"


def test_an_ordinary_user_cannot_claim_to_act_on_behalf_of_someone(
    client, juan, tenant, session
):
    """on_behalf_of is delegation, and only an operator delegates. An ordinary
    caller sending the header must not have it recorded as fact -- exercised
    here on project.rename, which audits ANY principal, so the assertion is
    not vacuous over zero matching events."""
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api"},
        headers=juan["headers"],
    )

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-api-2"},
        headers={**juan["headers"], "On-Behalf-Of": "usr_alice"},
    )

    event = session.query(AuditEvent).filter_by(action="project.rename").one()
    assert event.on_behalf_of is None


def test_a_delegated_project_rename_records_the_subject(
    client, new_user, master_headers, tenant, session
):
    """SPEC §20.3 + the "ownership changes and renames" half of §20 MUST: an
    operator delegating a rename on a human's behalf must not record NULL.
    Before this fix, project.create/rename/transfer ignored on_behalf_of
    entirely.

    Also pins the surviving half of the old master-key create rule: naming
    somebody ELSE as owner is still the authority part, so an operator may do
    it -- what went away is the refusal to create WITHOUT naming one."""
    someone = new_user()
    created = client.post(
        "/v1/projects",
        json={
            "project_slug": "payments-api",
            "owner": {"type": "user", "id": someone["user_id"]},
        },
        headers=master_headers,
    )
    assert created.status_code == 201, created.text

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-api-2"},
        headers={**master_headers, "On-Behalf-Of": "usr_alice"},
    )

    event = session.query(AuditEvent).filter_by(action="project.rename").one()
    assert event.on_behalf_of == "usr_alice"


def test_an_oversize_on_behalf_of_header_is_a_422_not_a_500(
    client, master_headers, tenant
):
    """AuditEvent.on_behalf_of is String(128); the header itself is
    unbounded. An external subject id from ACH is an email or a DN -- over
    128 characters is not exotic, and should be a typed 422 at the boundary,
    not a 500 from the database.

    Exercised on an admin route that takes the header: the bound is enforced
    by `current_on_behalf_of` before the route body, so it answers 422 rather
    than the 404 this unknown slug would otherwise get.
    """
    response = client.post(
        "/v1/admin/slugs/whatever/release",
        headers={**master_headers, "On-Behalf-Of": "x" * 200},
    )
    assert response.status_code == 422


def test_project_transfer_with_long_owner_ids_does_not_500(
    client, new_user, tenant, session
):
    """AuditEvent.resource is String(512) because call sites compose it from
    already-bounded external ids: transfer()'s
    f"{slug}: {owner_type}:{owner_id} -> ..." can reach 400+ characters at
    Project slug / Group.id's own String(128) bounds, and at the old 256 that
    was a psycopg DataError, i.e. a 500.

    A group owner, not a user one: user ids are minted locally now
    (`ids.new_user_id()`), so a caller can no longer supply a 128-character
    one -- but a GROUP id arrives from the identity provider and still can.
    """
    slug = "s" * 128
    group_id = "g" * 128
    owner = new_user(groups=(group_id,))
    created = client.post(
        "/v1/projects", json={"project_slug": slug}, headers=owner["headers"]
    )
    assert created.status_code == 201, created.text

    resp = client.patch(
        f"/v1/projects/{slug}/owner",
        json={"type": "group", "id": group_id},
        headers=owner["headers"],
    )

    assert resp.status_code == 200, resp.text
    event = session.query(AuditEvent).filter_by(action="project.transfer").one()
    assert event.resource.startswith(slug)
    assert event.resource.endswith(f"group:{group_id}")


def test_an_external_actor_is_recorded_in_the_audit_trail(session, tenant):
    """`ext_`-prefixed and resolvable: `external_identities.credential_id`
    turns it back into a human. Before this it was NULL -- indistinguishable
    from the master key's own actions, back when those were NULL too."""
    principal = Principal(
        tenant_id=tenant, user_id="usr_alice", credential_id="ext_alice",
        subject="alice@test",
    )

    audit.record(session, principal, "project.rename", "a -> b")
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id == "ext_alice"
    assert event.on_behalf_of is None
