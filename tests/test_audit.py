import httpx
import respx

from memory import audit
from memory.api.app import current_on_behalf_of
from memory.auth.principal import Principal
from memory.models import AuditEvent, GroupMember
from tests.test_memory_api import BASE, _mock_hindsight, juan  # noqa: F401


def _master(tenant: str) -> Principal:
    return Principal(tenant_id=tenant, user_id=None, is_master=True, key_id=None)


def _user(tenant: str) -> Principal:
    # Both, exactly as `local_key.authenticate` sets them for a real key:
    # `key_id` is the api_keys row, `credential_id` is what audit and the rate
    # limiter account against, and for a local key they are the same value.
    return Principal(
        tenant_id=tenant, user_id="usr_juan", is_master=False, key_id="key_juan",
        credential_id="key_juan",
    )


def test_a_user_action_records_the_key_and_no_delegation(session, tenant):
    audit.record(session, _user(tenant), "project.rename", "a -> b")
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id == "key_juan"
    assert event.on_behalf_of is None
    assert event.action == "project.rename"
    assert event.resource == "a -> b"


def test_a_master_action_without_delegation_records_no_subject(session, tenant):
    audit.record(session, _master(tenant), "project.transfer", "payments-api")
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id is None
    assert event.on_behalf_of is None


def test_a_delegated_master_action_records_the_subject(session, tenant):
    """The case the derived version could never express: a master key has no
    identity of its own, so the subject has to be supplied (SPEC §5.2)."""
    audit.record(
        session,
        _master(tenant),
        "project.transfer",
        "payments-api",
        on_behalf_of="usr_alice",
    )
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id is None
    assert event.on_behalf_of == "usr_alice"


def test_the_event_is_scoped_to_the_principal_tenant(session, tenant):
    audit.record(session, _user(tenant), "project.rename", "a -> b")
    session.flush()

    assert session.query(AuditEvent).one().tenant_id == tenant


def test_master_key_user_and_key_creation_are_audited(
    client, master_headers, tenant, session
):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers)

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "user.create" in actions
    assert "key.create" in actions


def test_group_membership_changes_are_audited(client, master_headers, tenant, session):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post("/v1/groups", json={"id": "grp_pay"}, headers=master_headers)
    client.put(f"/v1/groups/grp_pay/members/{user_id}", headers=master_headers)
    client.delete(f"/v1/groups/grp_pay/members/{user_id}", headers=master_headers)

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "group.create" in actions
    assert "group.add_member" in actions
    assert "group.remove_member" in actions


@respx.mock
def test_a_master_key_reading_a_users_bank_is_audited(
    client, master_headers, tenant, session
):
    """SPEC §20.3. A master key can reach any user's private memory; that must
    not be traceless."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]

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
def test_a_master_key_reaching_a_project_bank_is_audited(
    client, juan, master_headers, tenant, session  # noqa: F811
):
    """The other half of SPEC §20.3: a master key reaching a PROJECT's shared
    bank is at least as sensitive as reaching a user's, and used to be
    traceless -- projects.authorize() returns early for a master key with
    nothing recorded, and projects.create()'s own audit call is unreachable
    from the data plane (resolve() refuses to create for a master)."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    # juan creates and owns the project first (lazy first-touch creation).
    client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "juans-secret", "query": "x"},
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
        if e.action == "memory.recall" and e.actor_key_id is None
    ]
    assert len(events) == 1
    assert events[0].resource == "juans-secret"


@respx.mock
def test_master_actions_across_routes_are_distinguishable(
    client, juan, master_headers, tenant, session  # noqa: F811
):
    """SPEC §20.3 asks for auditable master actions, not just a record that
    *some* bank was touched. Before this fix all 15 master->user-bank routes
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
        if e.actor_key_id is None
        and e.resource == user_id
        and e.action.startswith("memory.")
    }
    assert actions == {"memory.recall", "memory.documents.delete"}


@respx.mock
def test_a_user_key_reading_its_own_bank_is_not_audited(
    client, juan, tenant, session  # noqa: F811 -- fixture reused from test_memory_api
):
    """Audit records delegated and privileged access, not ordinary use. A user
    reading their own memory on every agent start would drown the log."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    # Not a bare count() before/after: the `juan` fixture itself calls the
    # now-audited user.create/key.create routes with the master key, so
    # events already exist by this point. What matters is that THIS call
    # (a user reading its own bank) adds none of its own.
    before = session.query(AuditEvent).count()

    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "anything"},
        headers=juan["headers"],
    )

    assert session.query(AuditEvent).count() == before


def test_the_on_behalf_of_header_is_recorded(client, master_headers, tenant, session):
    client.post(
        "/v1/users",
        json={},
        headers={**master_headers, "On-Behalf-Of": "usr_alice"},
    )

    event = session.query(AuditEvent).filter_by(action="user.create").one()
    assert event.on_behalf_of == "usr_alice"


def test_current_on_behalf_of_discards_a_user_principals_header():
    """The trust boundary itself, tested directly rather than through a route.

    The old version of this test (below, renamed) posted to /v1/projects,
    which for a non-master principal records no audit event at all -- so
    `all(e.on_behalf_of is None for e in events)` ranged over events that
    never carried a header in the first place and would pass no matter what
    current_on_behalf_of did. Deleting the `is_master` guard in
    memory/api/app.py left every one of the 268 prior tests passing."""
    assert current_on_behalf_of(_user("default"), "usr_alice") is None


def test_current_on_behalf_of_keeps_a_master_principals_header():
    assert current_on_behalf_of(_master("default"), "usr_alice") == "usr_alice"


def test_a_user_key_cannot_claim_to_act_on_behalf_of_someone(
    client, juan, master_headers, tenant, session  # noqa: F811 -- fixture reused from test_memory_api
):
    """on_behalf_of is delegation, and only the master key delegates. A user
    key sending the header must not have it recorded as fact -- exercised here
    on project.rename, which (unlike project.create) audits ANY principal, so
    the assertion is no longer vacuous over zero matching events."""
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
    client, master_headers, tenant, session
):
    """SPEC §20.3 + the "ownership changes and renames" half of §20 MUST: a
    master key delegating a rename on a human's behalf must not record NULL.
    Before this fix, project.create/rename/transfer ignored on_behalf_of
    entirely."""
    client.post("/v1/users", json={"id": "usr_someone"}, headers=master_headers)
    client.post(
        "/v1/projects",
        json={
            "project_slug": "payments-api",
            "owner": {"type": "user", "id": "usr_someone"},
        },
        headers=master_headers,
    )

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-api-2"},
        headers={**master_headers, "On-Behalf-Of": "usr_alice"},
    )

    event = session.query(AuditEvent).filter_by(action="project.rename").one()
    assert event.on_behalf_of == "usr_alice"


def test_an_oversize_on_behalf_of_header_is_a_422_not_a_500(client, master_headers):
    """AuditEvent.on_behalf_of is String(128); the header itself is
    unbounded. An external subject id from ACH is an email or a DN -- over
    128 characters is not exotic, and should be a typed 422 at the boundary,
    not a 500 from the database."""
    response = client.post(
        "/v1/users",
        json={},
        headers={**master_headers, "On-Behalf-Of": "x" * 200},
    )
    assert response.status_code == 422


def test_group_member_ids_at_the_128_char_bound_do_not_500(
    client, master_headers, tenant, session
):
    """AuditEvent.resource was String(256); group.add_member's composed
    f"{group_id}/{user_id}" reaches 257 chars at User.id/Group.id's own
    String(128) bound -- a 500, and the membership was not persisted because
    the commit that carries it is the one that fails."""
    user_id = "u" * 128
    group_id = "g" * 128
    client.post("/v1/users", json={"id": user_id}, headers=master_headers)
    client.post("/v1/groups", json={"id": group_id}, headers=master_headers)

    put_resp = client.put(
        f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers
    )
    assert put_resp.status_code == 204
    assert (
        session.get(GroupMember, (group_id, user_id)) is not None
    ), "membership must be persisted, not lost to the failed commit"

    del_resp = client.delete(
        f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers
    )
    assert del_resp.status_code == 204


def test_project_transfer_with_long_owner_ids_does_not_500(
    client, master_headers, tenant, session
):
    """Same defect as group membership, pre-existing here too: transfer()'s
    f"{slug}: {owner_type}:{owner_id}" can reach 264+ characters."""
    slug = "s" * 128
    old_owner = "o" * 128
    new_owner = "n" * 128
    client.post("/v1/users", json={"id": old_owner}, headers=master_headers)
    client.post("/v1/users", json={"id": new_owner}, headers=master_headers)
    client.post(
        "/v1/projects",
        json={"project_slug": slug, "owner": {"type": "user", "id": old_owner}},
        headers=master_headers,
    )

    resp = client.patch(
        f"/v1/projects/{slug}/owner",
        json={"type": "user", "id": new_owner},
        headers=master_headers,
    )
    assert resp.status_code == 200


def test_repeat_add_member_logs_no_event(client, master_headers, tenant, session):
    """"Log changes, not requests": a repeat idempotent PUT must add no event.
    Moving audit.record outside the `if db.get(GroupMember, ...) is None:`
    branch in groups.py leaves every prior test passing."""
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post("/v1/groups", json={"id": "grp_repeat"}, headers=master_headers)
    client.put(f"/v1/groups/grp_repeat/members/{user_id}", headers=master_headers)

    before = session.query(AuditEvent).count()
    response = client.put(
        f"/v1/groups/grp_repeat/members/{user_id}", headers=master_headers
    )

    assert response.status_code == 204
    assert session.query(AuditEvent).count() == before


def test_remove_absent_member_logs_no_event(client, master_headers, tenant, session):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post("/v1/groups", json={"id": "grp_absent"}, headers=master_headers)

    before = session.query(AuditEvent).count()
    response = client.delete(
        f"/v1/groups/grp_absent/members/{user_id}", headers=master_headers
    )

    assert response.status_code == 204
    assert session.query(AuditEvent).count() == before


def test_key_create_records_the_key_id_not_the_user_id(
    client, master_headers, tenant, session
):
    """Two keys minted for one user must produce distinguishable rows. The
    key id is the thing created and is not sensitive."""
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key_id = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key_id"]

    event = session.query(AuditEvent).filter_by(action="key.create").one()
    assert event.resource == key_id


def test_key_revoke_is_audited_with_the_key_id(
    client, master_headers, tenant, session
):
    """The twin of test_key_create_records_the_key_id_not_the_user_id, for the
    other half of the lifecycle. Revocation is the security event this task
    exists to make possible; nothing else would fail if `audit.record` moved
    below `db.commit()` (lost in the never-committing session teardown),
    above the `if row is None` guard (auditing a no-op), or recorded the user
    id instead of the key id."""
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key_id = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key_id"]

    response = client.delete(
        f"/v1/users/{user_id}/keys/{key_id}", headers=master_headers
    )

    assert response.status_code == 204
    event = session.query(AuditEvent).filter_by(action="key.revoke").one()
    assert event.resource == key_id


def test_an_external_actor_is_recorded_in_the_audit_trail(session, tenant):
    """`ext_`-prefixed so a reader can tell it apart from a `key_` api_keys.id,
    and `external_identities.credential_id` resolves it back to a human. Before
    this it was NULL -- indistinguishable from a master-key action."""
    principal = Principal(
        tenant_id=tenant, user_id="usr_alice", is_master=False, key_id=None,
        credential_id="ext_alice",
    )

    audit.record(session, principal, "project.rename", "a -> b")
    session.flush()

    event = session.query(AuditEvent).one()
    assert event.actor_key_id == "ext_alice"
    assert event.on_behalf_of is None
