import pytest


def test_master_creates_a_user_with_a_generated_id(client, master_headers, tenant):
    response = client.post("/v1/users", json={}, headers=master_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"].startswith("usr_")


def test_master_creates_a_user_with_an_explicit_id(client, master_headers, tenant):
    response = client.post(
        "/v1/users", json={"id": "ach-user-82f"}, headers=master_headers
    )

    assert response.status_code == 201
    assert response.json()["user_id"] == "ach-user-82f"


def test_user_response_never_exposes_the_bank_id(client, master_headers, tenant):
    body = client.post("/v1/users", json={}, headers=master_headers).json()

    assert "bank_id" not in body
    assert not any("user_" in str(v) for k, v in body.items() if k != "user_id")


def test_creating_a_user_requires_the_master_key(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_key_creation_returns_the_plaintext_once(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    created = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()
    listed = client.get(f"/v1/users/{user_id}", headers=master_headers).json()

    assert created["key"].startswith("mem_")
    assert "key" not in str(listed)


def test_unauthenticated_request_is_401(client, tenant):
    assert client.post("/v1/users", json={}).status_code == 401


def test_an_oversize_explicit_user_id_is_a_422_not_a_500(client, master_headers, tenant):
    """User.id is String(128); an overflow is a DataError, not an
    IntegrityError, so create_user's db.begin_nested()/except IntegrityError
    guard never catches it -- it must be rejected at the boundary instead."""
    response = client.post(
        "/v1/users", json={"id": "u" * 200}, headers=master_headers
    )

    assert response.status_code == 422


def test_duplicate_explicit_user_id_is_a_conflict(client, master_headers, tenant):
    client.post("/v1/users", json={"id": "ach-user-dup"}, headers=master_headers)

    response = client.post(
        "/v1/users", json={"id": "ach-user-dup"}, headers=master_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "USER_ALREADY_EXISTS"


def test_key_creation_commits_before_returning(client, master_headers, tenant, monkeypatch):
    """A caller that receives a plaintext key must be holding a key that exists.

    If the commit happened in the dependency teardown it would run after the
    response was already sent, and this failure would surface as a 201 with a
    key that authenticates nowhere.
    """
    from memory.api import users

    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    committed = []
    original = users.Session.commit

    def _record(self):
        committed.append(True)
        return original(self)

    monkeypatch.setattr(users.Session, "commit", _record, raising=False)

    response = client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers)

    assert response.status_code == 201
    assert committed, "the handler returned without committing"


def test_each_request_gets_its_own_session(client, master_headers, tenant):
    """Two requests must not share a Session.

    Sharing one hides every commit and rollback bug: an uncommitted write is
    still visible to the next request through the identity map, so a handler
    that forgets to commit looks correct in tests and loses data in production.

    This wraps the fixture's OWN override rather than the real
    memory.db.get_session. Calling the real one would open a second physical
    connection outside the test transaction: it would commit rows the rollback
    never undoes, and it would deadlock against the `tenant` fixture's
    uncommitted row.

    Keeps the SESSION OBJECTS themselves (not `id(session)`): CPython reuses
    a freed object's address, so once the first request's session is garbage
    collected -- which `gen.close()` allows -- the second request's session
    can land at the exact same address and `id()` compares equal even though
    they are two different objects. This test was intermittently green on a
    broken implementation for that reason. Holding a real reference in `seen`
    keeps both alive for the `is not` check below, which cannot be fooled by
    address reuse.
    """
    from memory import db

    seen = []
    override = client.app.dependency_overrides[db.get_session]

    def _recording():
        gen = override()
        session = next(gen)
        seen.append(session)
        try:
            yield session
        finally:
            gen.close()

    client.app.dependency_overrides[db.get_session] = _recording

    client.post("/v1/users", json={}, headers=master_headers)
    client.post("/v1/users", json={}, headers=master_headers)

    assert len(seen) == 2
    assert seen[0] is not seen[1], "both requests shared one Session"


def test_unexpected_failure_keeps_the_error_envelope(
    client, master_headers, tenant, monkeypatch
):
    """Any non-DomainError must still produce {"error": {...}}, never plain text."""
    from memory.api import users

    def _boom(*_args, **_kwargs):
        raise RuntimeError("something internal broke")

    monkeypatch.setattr(users.ids, "new_user_id", _boom)

    response = client.post("/v1/users", json={}, headers=master_headers)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "something internal broke" not in response.text


def test_unhandled_error_log_line_carries_the_real_exception(
    client, master_headers, tenant, monkeypatch, caplog
):
    """`logger.exception()` reads `sys.exc_info()`, which is only populated
    inside an active `except` block. The catch-all handler runs from the
    exception-middleware's call site, not from one, so it logged
    "NoneType: None" instead of a traceback -- the one log line meant to
    survive everything else was carrying nothing useful."""
    from memory.api import users

    def _boom(*_args, **_kwargs):
        raise RuntimeError("something internal broke")

    monkeypatch.setattr(users.ids, "new_user_id", _boom)

    with caplog.at_level("ERROR", logger="memory.api"):
        client.post("/v1/users", json={}, headers=master_headers)

    records = [r for r in caplog.records if r.name == "memory.api"]
    assert records, "the catch-all handler must log the unhandled error"
    exc_info = records[-1].exc_info
    assert exc_info is not None and exc_info[1] is not None, (
        "the log record's exc_info must carry the real exception, not the "
        "empty (None, None, None) sys.exc_info() returns outside an except "
        "block -- which formats as 'NoneType: None'"
    )
    assert isinstance(exc_info[1], RuntimeError)
    assert str(exc_info[1]) == "something internal broke"


@pytest.fixture
def user_key(client, master_headers, tenant) -> tuple[str, str]:
    """(user_id, key) -- matches the established idiom in test_memory_api.py.

    Not a bare string: callers that only need the key unpack `_, key = user_key`.
    """
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return user_id, key


def test_revoking_a_key_stops_it_authenticating(client, master_headers, tenant):
    """The whole point: a leaked key must die on request.

    Before Plan 6 the only way was `UPDATE api_keys SET status='revoked'` in
    Postgres -- the status column was read on every auth and written by
    nothing (review Critical C2, SPEC §5.3).
    """
    user_id = client.post(
        "/v1/users", json={}, headers=master_headers
    ).json()["user_id"]
    minted = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers=master_headers,
    ).json()

    before = client.get(
        "/v1/projects", headers={"Authorization": f"Bearer {minted['key']}"}
    )
    assert before.status_code == 200

    revoked = client.delete(
        f"/v1/users/{user_id}/keys/{minted['key_id']}",
        headers=master_headers,
    )
    assert revoked.status_code == 204

    after = client.get(
        "/v1/projects", headers={"Authorization": f"Bearer {minted['key']}"}
    )
    assert after.status_code == 401


def test_listing_keys_never_returns_the_secret(client, master_headers, tenant):
    """A list route that leaks the plaintext would be worse than no route.

    The plaintext exists exactly once, in the mint response (SPEC §5.3), and
    only the hash is stored -- so this pins that nothing added a secret or
    secret_hash field to the summary.
    """
    user_id = client.post(
        "/v1/users", json={}, headers=master_headers
    ).json()["user_id"]
    minted = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers=master_headers,
    ).json()

    listed = client.get(f"/v1/users/{user_id}/keys", headers=master_headers)

    assert listed.status_code == 200
    body = listed.json()
    assert [k["key_id"] for k in body["keys"]] == [minted["key_id"]]
    assert body["keys"][0]["status"] == "active"
    # Exact field set, not two literal greps: a future field added under a
    # different name (e.g. `fingerprint = secret_hash[:16]`) must fail this,
    # since neither "not in" check below would catch it.
    assert set(body["keys"][0]) == {"key_id", "status", "created_at"}
    assert minted["key"] not in listed.text
    assert "secret_hash" not in listed.text


def test_revoking_a_key_twice_is_not_found_the_second_time(client, master_headers, tenant):
    user_id = client.post(
        "/v1/users", json={}, headers=master_headers
    ).json()["user_id"]
    key_id = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers=master_headers,
    ).json()["key_id"]
    path = f"/v1/users/{user_id}/keys/{key_id}"

    assert client.delete(path, headers=master_headers).status_code == 204
    second = client.delete(path, headers=master_headers)
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "KEY_NOT_FOUND"


def test_a_key_of_another_user_cannot_be_revoked_through_this_user(
    client, master_headers, tenant
):
    """The key id is addressed under a user id; the pair must match.

    Otherwise the user segment is decoration and any key id revokes.
    """
    victim = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    other = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    victim_key = client.post(
        f"/v1/users/{victim}/keys", json={}, headers=master_headers
    ).json()["key_id"]

    response = client.delete(
        f"/v1/users/{other}/keys/{victim_key}", headers=master_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KEY_NOT_FOUND"


def test_listing_users_is_master_only(client, user_key):
    _, key = user_key
    response = client.get("/v1/users", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403


def test_listing_one_users_keys_does_not_show_anothers(client, master_headers, tenant):
    """The LIST twin of test_a_key_of_another_user_cannot_be_revoked_through_this_user.

    With a single user and a single key in the transaction, deleting the
    ApiKey.user_id filter from list_keys would still pass every other test in
    this file -- there is no second key around to leak. Two users, two keys.
    """
    a = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    b = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    a_key = client.post(
        f"/v1/users/{a}/keys", json={}, headers=master_headers
    ).json()["key_id"]
    b_key = client.post(
        f"/v1/users/{b}/keys", json={}, headers=master_headers
    ).json()["key_id"]

    listed = client.get(f"/v1/users/{a}/keys", headers=master_headers).json()

    assert [k["key_id"] for k in listed["keys"]] == [a_key]
    assert b_key not in [k["key_id"] for k in listed["keys"]]


def test_listing_users_returns_the_tenants_users_and_no_others(
    client, master_headers, tenant, session
):
    """A handler that returns [] forever (e.g. an accidental filter on
    principal.user_id, which is None for a master call) would 200 with an
    empty list on every test that only checks status codes -- including the
    403-only test above, the sole other test that touches this route.
    """
    from memory.models import Tenant, User

    a = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    b = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    session.add(Tenant(id="other"))
    session.flush()
    session.add(User(id="usr_elsewhere", tenant_id="other", bank_id="bnk_elsewhere"))
    session.flush()

    body = client.get("/v1/users", headers=master_headers).json()

    ids = {u["user_id"] for u in body["users"]}
    assert {a, b} <= ids
    assert "usr_elsewhere" not in ids
    # Field-set pin (I-6), mirroring the KeySummary fix (M-1) eight lines away
    # in this file: a field added to UserSummary under any name -- most
    # dangerously `bank_id`, the one identifier that must never cross the API
    # boundary (SPEC invariant 29) -- would slip past the two checks above,
    # which only look at user_id, but not this one.
    assert set(body["users"][0]) == {"user_id", "created_at"}
    assert not any(
        "user_" in str(v) for u in body["users"] for k, v in u.items() if k != "user_id"
    )


def test_revoking_a_key_for_a_nonexistent_user_is_user_not_found(
    client, master_headers, tenant
):
    """revoke_key must check user existence first, like get_user/create_key/
    list_keys already do. Without this check a typoed user id falls straight
    through to the key predicate and reads as KEY_NOT_FOUND ("already gone")
    even though the key is still live under the real user -- the wrong signal
    for a route whose whole purpose is killing a leaked credential.
    """
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key_id = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key_id"]

    response = client.delete(
        f"/v1/users/does-not-exist/keys/{key_id}", headers=master_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_a_revoked_keys_row_is_still_listed_and_reachable_by_id(
    client, master_headers, tenant
):
    """Revoke is a status flip, not a delete (SPEC audit trail intent).

    An operator auditing a leak must still find the row -- with status
    'revoked' -- not have it vanish, which would look like the key never
    existed and hide when the revocation happened.
    """
    user_id = client.post(
        "/v1/users", json={}, headers=master_headers
    ).json()["user_id"]
    key_id = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers=master_headers,
    ).json()["key_id"]

    client.delete(f"/v1/users/{user_id}/keys/{key_id}", headers=master_headers)

    listed = client.get(f"/v1/users/{user_id}/keys", headers=master_headers).json()
    row = next(k for k in listed["keys"] if k["key_id"] == key_id)
    assert row["status"] == "revoked"


# --- I-4: master gating, one test per route -------------------------------
#
# All four mirror test_listing_users_is_master_only. Each proves the route
# depends on Depends(require_master), not merely Depends(current_principal):
# swap the dependency and the corresponding test below turns red because a
# plain authenticated user key would then pass where it must get 403.


def test_creating_a_key_requires_the_master_key(client, master_headers, user_key, tenant):
    """The most urgent of the I-4 gaps (do this one first): create_key is a
    mint route. Replacing Depends(require_master) with
    Depends(current_principal) on POST /v1/users/{id}/keys would let ANY
    user key mint a fresh key that authenticates AS ANY OTHER USER -- full
    horizontal privilege escalation, and every other test in the suite stays
    green because none of them ever attack a *different* user's id.
    """
    victim = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    _, attacker_key = user_key

    response = client.post(
        f"/v1/users/{victim}/keys",
        json={},
        headers={"Authorization": f"Bearer {attacker_key}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_getting_a_user_requires_the_master_key(client, user_key):
    """Deleting Depends(require_master) here (swapped for current_principal)
    would let any user key read another user's record via GET
    /v1/users/{id}."""
    user_id, key = user_key
    response = client.get(f"/v1/users/{user_id}", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_listing_a_users_keys_requires_the_master_key(client, user_key):
    """Deleting Depends(require_master) here would hand any authenticated
    user a credential inventory (key ids + status) of any other user in the
    tenant via GET /v1/users/{id}/keys -- no secret escapes, but the id list
    itself is exactly what an attacker needs to target revoke_key next."""
    user_id, key = user_key
    response = client.get(
        f"/v1/users/{user_id}/keys", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_revoking_a_key_requires_the_master_key(client, master_headers, user_key, tenant):
    """Deleting Depends(require_master) here would let any user key revoke
    any OTHER user's key via DELETE /v1/users/{id}/keys/{id} -- a tenant-wide
    denial of service (the audit trail would at least still name the
    attacker's own key id as the actor)."""
    victim = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    victim_key_id = client.post(
        f"/v1/users/{victim}/keys", json={}, headers=master_headers
    ).json()["key_id"]
    _, attacker_key = user_key

    response = client.delete(
        f"/v1/users/{victim}/keys/{victim_key_id}",
        headers={"Authorization": f"Bearer {attacker_key}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


# --- I-5: tenant scoping, one test per route -------------------------------
#
# Master's principal.tenant_id always comes from settings (the `tenant`
# fixture's "default"), so "another tenant" is reached the same way
# test_listing_users_returns_the_tenants_users_and_no_others does: a Tenant
# and User inserted directly through the `session` fixture, never through the
# API (there is no route that creates a second tenant).


def test_getting_a_user_in_another_tenant_is_not_found(
    client, master_headers, tenant, session
):
    """Dropping `user.tenant_id != principal.tenant_id` from get_user would
    let a default-tenant master read a user that belongs to a different
    tenant, since `user is None` alone is False for a row that exists."""
    from memory.models import Tenant, User

    session.add(Tenant(id="other"))
    session.flush()
    session.add(User(id="usr_elsewhere", tenant_id="other", bank_id="bnk_elsewhere"))
    session.flush()

    response = client.get("/v1/users/usr_elsewhere", headers=master_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_creating_a_key_for_a_user_in_another_tenant_is_not_found(
    client, master_headers, tenant, session
):
    """Dropping `user.tenant_id != principal.tenant_id` from create_key would
    let a default-tenant master mint a key for a user in a different tenant
    -- a key that would then authenticate into the wrong tenant's data."""
    from memory.models import Tenant, User

    session.add(Tenant(id="other"))
    session.flush()
    session.add(User(id="usr_elsewhere", tenant_id="other", bank_id="bnk_elsewhere"))
    session.flush()

    response = client.post(
        "/v1/users/usr_elsewhere/keys", json={}, headers=master_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_listing_keys_refuses_another_tenant(client, master_headers, tenant, session):
    """Two independent tenant guards in list_keys; this test breaks either.

    1) A user in another tenant: dropping `user.tenant_id !=
       principal.tenant_id` would let the query proceed instead of raising --
       the ApiKey.tenant_id filter would then just return zero rows, so this
       would read as 200 with an empty list instead of the 404 asserted here.
    2) A same-tenant user whose OWN key row disagrees on tenant_id (the only
       scenario that can distinguish this filter, since the user-level check
       above already blocks a genuinely cross-tenant user before the query
       runs): dropping `ApiKey.tenant_id == principal.tenant_id` from the
       query would surface that corrupted-tenant key in the listing.
    """
    from memory import ids
    from memory.auth import keys
    from memory.models import ApiKey, Tenant, User

    # (1) a user that belongs to a different tenant than the master caller.
    session.add(Tenant(id="other"))
    session.flush()
    session.add(User(id="usr_elsewhere", tenant_id="other", bank_id="bnk_elsewhere"))
    session.flush()
    elsewhere = client.get("/v1/users/usr_elsewhere/keys", headers=master_headers)
    assert elsewhere.status_code == 404
    assert elsewhere.json()["error"]["code"] == "USER_NOT_FOUND"

    # (2) a same-tenant user with one legitimate key plus one whose tenant_id
    # was corrupted to another tenant (not reachable through the API -- only
    # a direct row, exactly like the "usr_elsewhere" row above).
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    own_key_id = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key_id"]
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id="other",
            user_id=user_id,
            secret_hash=keys.hash_key("mem_corrupted_cross_tenant_key"),
        )
    )
    session.flush()

    listed = client.get(f"/v1/users/{user_id}/keys", headers=master_headers).json()

    assert [k["key_id"] for k in listed["keys"]] == [own_key_id]


def test_revoking_a_key_refuses_another_tenant(client, master_headers, tenant, session):
    """Two independent tenant guards in revoke_key, mirroring
    test_listing_keys_refuses_another_tenant above.

    1) A user in another tenant: dropping `user.tenant_id !=
       principal.tenant_id` from revoke_key's user check would let it through
       to the key predicate.
    2) A same-tenant user's key whose OWN tenant_id was corrupted (the only
       scenario that can distinguish this filter, for the same reason as
       above): dropping `ApiKey.tenant_id == principal.tenant_id` from the
       delete predicate would let that key be revoked (204) instead of 404.
    """
    from memory import ids
    from memory.auth import keys
    from memory.models import ApiKey, Tenant, User

    # (1)
    session.add(Tenant(id="other"))
    session.flush()
    session.add(User(id="usr_elsewhere", tenant_id="other", bank_id="bnk_elsewhere"))
    session.flush()
    response = client.delete(
        "/v1/users/usr_elsewhere/keys/does-not-matter", headers=master_headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"

    # (2)
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    corrupted_id = ids.new_key_id()
    session.add(
        ApiKey(
            id=corrupted_id,
            tenant_id="other",
            user_id=user_id,
            secret_hash=keys.hash_key("mem_corrupted_cross_tenant_key"),
        )
    )
    session.flush()

    revoke_response = client.delete(
        f"/v1/users/{user_id}/keys/{corrupted_id}", headers=master_headers
    )

    assert revoke_response.status_code == 404
    assert revoke_response.json()["error"]["code"] == "KEY_NOT_FOUND"


def test_created_key_authenticates_as_its_owner_and_no_other(client, master_headers, tenant):
    """M-5: create_key's identity binding (`user_id=user.id` on the new
    ApiKey row) proven end-to-end through a route, not by hand-building the
    row the way test_principal.py::test_user_key_resolves_to_its_user does.

    POST /v1/projects with no explicit owner stamps the new project with
    `principal.user_id` (SPEC §16.2) -- so if create_key bound the minted key
    to the wrong user (the wrong argument, or every new key accidentally
    bound to the first user ever created), the project created through it
    would come back owned by an id that is not the caller.
    """
    owner = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    decoy = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{owner}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.post(
        "/v1/projects",
        json={"project_slug": "proj-identity-check"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 201
    assert response.json()["owner"] == {"type": "user", "id": owner}
    assert response.json()["owner"]["id"] != decoy
