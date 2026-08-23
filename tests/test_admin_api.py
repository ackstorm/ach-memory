import httpx
import pytest
import respx

BASE = "http://hindsight.test"


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


# ---------------------------------------------------------------------------
# Task 2: GET /v1/admin/audit
# ---------------------------------------------------------------------------


def test_the_audit_read_requires_the_master_key(client, juan, tenant):
    response = client.get("/v1/admin/audit", headers=juan["headers"])

    assert response.status_code == 403


def test_it_returns_events_newest_first(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers)

    events = client.get("/v1/admin/audit", headers=master_headers).json()

    # Not actions[:2] == ["key.create", "user.create"]: created_at is now
    # `func.now()`, the DB's ONE clock (2026-08-23 review, finding 2) --
    # Postgres's `now()` is transaction-scoped, constant for every statement
    # in one transaction, and this fixture's savepoint architecture runs
    # both requests in one outer transaction. So the two rows legitimately
    # TIE on created_at here (they would not in production, where each
    # request commits its own transaction), and which one sorts first is
    # exactly what the id DESC tiebreak decides -- see
    # test_the_id_desc_tiebreak_is_deterministic_not_recency. This still
    # pins that both events land in the top 2, just not their relative order.
    actions = [e["action"] for e in events]
    assert set(actions[:2]) == {"key.create", "user.create"}


def test_the_id_desc_tiebreak_is_deterministic_not_recency(
    client, master_headers, tenant, session
):
    """The `id DESC` tiebreak buys determinism across repeated calls, not
    recency: `AuditEvent.id` is `ids.new_audit_id()`, a random uuid4 hex
    uncorrelated with insertion order. Pins that the tiebreak clause is
    actually wired by forcing two rows to share one `created_at` and
    asserting the higher id sorts first regardless of insertion order --
    without `AuditEvent.id.desc()` in the `order_by`, this would be flaky
    (Postgres gives no ordering guarantee for tied sort keys)."""
    from datetime import UTC, datetime

    from memory.models import AuditEvent

    same_ts = datetime.now(UTC)
    session.add(
        AuditEvent(
            id="aud_low",
            tenant_id=tenant,
            actor_key_id=None,
            on_behalf_of=None,
            action="tiebreak.probe",
            resource="r1",
            created_at=same_ts,
        )
    )
    session.add(
        AuditEvent(
            id="aud_zzz_high",
            tenant_id=tenant,
            actor_key_id=None,
            on_behalf_of=None,
            action="tiebreak.probe",
            resource="r2",
            created_at=same_ts,
        )
    )
    session.flush()

    events = client.get(
        "/v1/admin/audit?action=tiebreak.probe", headers=master_headers
    ).json()

    assert [e["id"] for e in events] == ["aud_zzz_high", "aud_low"]


def test_it_filters_by_action_and_by_actor(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post("/v1/groups", json={"id": "grp_a"}, headers=master_headers)

    only = client.get(
        "/v1/admin/audit?action=user.create", headers=master_headers
    ).json()

    assert [e["action"] for e in only] == ["user.create"]
    assert only[0]["resource"] == user_id


def test_an_audit_row_never_carries_a_bank_id(client, master_headers, tenant, session):
    """The table is a disclosure surface the moment it is readable."""
    from memory.models import AuditEvent

    client.post("/v1/users", json={}, headers=master_headers)

    body = client.get("/v1/admin/audit", headers=master_headers).text
    assert "bank_id" not in body
    assert "prj_" not in body
    assert not [
        e
        for e in session.query(AuditEvent).all()
        if "user_" in (e.resource or "") and "-" in (e.resource or "")
    ]


def test_it_is_scoped_to_the_callers_tenant(client, master_headers, tenant, session):
    from memory.models import AuditEvent, Tenant

    session.add(Tenant(id="other"))
    session.flush()
    session.add(
        AuditEvent(
            id="aud_other",
            tenant_id="other",
            actor_key_id=None,
            on_behalf_of=None,
            action="user.create",
            resource="usr_elsewhere",
        )
    )
    session.flush()

    events = client.get("/v1/admin/audit", headers=master_headers).json()

    assert all(e["resource"] != "usr_elsewhere" for e in events)


def test_the_page_size_is_bounded(client, master_headers, tenant):
    response = client.get("/v1/admin/audit?limit=100000", headers=master_headers)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Task 3: the admin destructive plane
# ---------------------------------------------------------------------------


def test_clear_memories_refuses_a_user_key_even_the_banks_own_owner(
    client, juan, tenant
):
    """SPEC §11.7's whole point: a user key that OWNS this very bank must
    still be refused. require_master gates on the credential alone, before
    scope/ownership is ever resolved."""
    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=juan["headers"],
    )

    assert response.status_code == 403


def test_delete_bank_refuses_a_user_key_even_the_banks_own_owner(client, juan, tenant):
    response = client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=juan["headers"],
    )

    assert response.status_code == 403


def test_release_slug_requires_the_master_key(client, juan, tenant):
    response = client.post("/v1/admin/slugs/whatever/release", headers=juan["headers"])

    assert response.status_code == 403


@respx.mock
def test_clear_reaches_hindsight_and_passes_type_through(
    client, juan, master_headers, tenant
):
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"], "type": "world"},
        headers=master_headers,
    )

    assert response.status_code == 200
    assert route.called
    assert route.calls.last.request.url.params["type"] == "world"


@respx.mock
def test_clear_omits_type_when_not_given(client, juan, master_headers, tenant):
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200
    assert "type" not in route.calls.last.request.url.params


@respx.mock
def test_delete_reaches_the_delete_bank_endpoint(client, juan, master_headers, tenant):
    route = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    response = client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200
    assert route.called


# --- Bank-id redaction: directives.py and mental_models.py both already had
# a test pinning `_strip_bank_id(result, bank_id)` on their responses;
# admin.py did not, even though its `delete_bank` upstream body is literally
# `{"message": "Bank 'user_<uuid>' ... deleted successfully"}` -- redaction
# here is load-bearing right now, not defense in depth. Same shape as
# test_bank_id_is_stripped_from_a_directive_response.


@respx.mock
def test_bank_id_is_stripped_from_the_clear_response(client, juan, master_headers, tenant):
    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)
    assert "user_leaked_nested" not in str(body)


@respx.mock
def test_bank_id_is_stripped_from_the_delete_response(client, juan, master_headers, tenant):
    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": "Bank 'user_leaked' and all associated data deleted successfully",
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked_nested" not in str(body)


@respx.mock
def test_clear_on_an_unknown_project_slug_404s_without_creating_it(
    client, master_headers, tenant
):
    """No respx route is registered on purpose: if this ever reached
    Hindsight, respx's own AllMockedAssertionError would fire (a 500), not
    the 404 asserted below. Pins the admin route to `_resolve_bank(...,
    create=False)`'s outcome for a master key -- note that `resolve()`
    itself already refuses lazy creation for ANY master-key caller
    regardless of `create`, since a master key has no identity to own the
    new project; `create=False` here is the same defensive convention
    curation.py/documents.py use, kept for consistency even though today it
    is not this specific flag doing the refusing for scope=project."""
    response = client.post(
        "/v1/admin/memory/project/clear",
        params={"project_slug": "ghost"},
        headers=master_headers,
    )

    assert response.status_code == 404
    listed = client.get("/v1/projects", headers=master_headers).json()
    assert listed == []


@respx.mock
def test_delete_on_an_unknown_project_slug_404s_without_creating_it(
    client, master_headers, tenant
):
    response = client.delete(
        "/v1/admin/memory/project",
        params={"project_slug": "ghost"},
        headers=master_headers,
    )

    assert response.status_code == 404
    listed = client.get("/v1/projects", headers=master_headers).json()
    assert listed == []


@respx.mock
def test_clear_writes_an_audit_event(client, juan, master_headers, tenant, session):
    from memory.models import AuditEvent

    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "admin.memory.clear" in actions


@respx.mock
def test_delete_bank_writes_an_audit_event(
    client, juan, master_headers, tenant, session
):
    from memory.models import AuditEvent

    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "admin.memory.delete" in actions


@respx.mock
def test_a_failed_delete_leaves_no_audit_row(
    client, juan, master_headers, tenant, session
):
    """An audit row is a claim that the erasure happened (SPEC §12.3): the
    `action` string IS the compliance claim. This would go red if the
    `db.commit()` in `delete_bank` moved back to before the upstream call --
    the sibling test above mocks a 200 and asserts the row appears, so
    nothing pinned that a 502 must leave no row at all (review finding I2).
    """
    from memory.models import AuditEvent

    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(503, json={})
    )

    response = client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 502
    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "admin.memory.delete" not in actions


@respx.mock
def test_a_failed_clear_leaves_no_audit_row(
    client, juan, master_headers, tenant, session
):
    """Same claim as the delete-path sibling, for `clear_memories`: a 502
    from Hindsight must not leave an `admin.memory.clear` row behind. Would
    go red under the pre-fix commit-before-upstream ordering.
    """
    from memory.models import AuditEvent

    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)").mock(
        return_value=httpx.Response(503, json={})
    )

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 502
    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "admin.memory.clear" not in actions


@respx.mock
def test_deleting_a_users_bank_leaves_the_user_row_and_its_bank_id_intact(
    client, juan, master_headers, tenant, session
):
    """Decision: delete_bank never mutates `users`. `User.bank_id` is NOT
    NULL -- there is no schema-safe way to clear it -- and deleting the row
    would cascade into that user's API keys / group memberships / project
    ownership, far outside "erase this bank's content." A bank_id whose
    Hindsight bank was torn down is no different from one never materialized
    (SPEC §17): the next retain against it just auto-creates an empty bank
    under the same id (measured live: Hindsight creates a bank on first
    retain, no upsert needed).
    """
    from memory.models import User

    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    before = session.get(User, juan["user_id"]).bank_id

    response = client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200
    after = session.get(User, juan["user_id"])
    assert after is not None
    assert after.bank_id == before


def test_release_slug_frees_the_name_and_leaves_the_project_alone(
    client, juan, master_headers, tenant, session
):
    from memory.models import RetiredSlug

    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])

    response = client.post("/v1/admin/slugs/a/release", headers=master_headers)

    assert response.status_code == 204
    assert session.get(RetiredSlug, (tenant, "a")) is None
    # The project the tombstone pointed at keeps ITS current slug, untouched.
    projects = client.get("/v1/projects", headers=master_headers).json()
    assert [p["project_slug"] for p in projects] == ["b"]


def test_releasing_a_slug_lets_a_new_project_take_it(
    client, juan, master_headers, tenant
):
    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])

    client.post("/v1/admin/slugs/a/release", headers=master_headers)
    response = client.post(
        "/v1/projects", json={"project_slug": "a"}, headers=juan["headers"]
    )

    assert response.status_code == 201


def test_release_slug_writes_an_audit_event(
    client, juan, master_headers, tenant, session
):
    from memory.models import AuditEvent

    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])

    client.post("/v1/admin/slugs/a/release", headers=master_headers)

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "slug.release" in actions


def test_releasing_an_unknown_slug_404s(client, master_headers, tenant):
    response = client.post("/v1/admin/slugs/nope/release", headers=master_headers)

    assert response.status_code == 404


def test_release_slug_is_scoped_to_the_callers_tenant(client, master_headers, tenant, session):
    """IDOR-style: a retired slug is looked up by a bare path param. Another
    tenant's tombstone happening to share the same slug text must not be
    reachable or releasable from here."""
    from memory import ids
    from memory.models import Project, RetiredSlug, Tenant

    session.add(Tenant(id="other"))
    session.flush()
    other_project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id="other",
        project_slug="b",
        owner_type="user",
        owner_id="usr_whoever",
        bank_id=ids.new_project_bank_id(),
    )
    session.add(other_project)
    session.flush()
    session.add(
        RetiredSlug(
            tenant_id="other",
            retired_slug="a",
            project_internal_id=other_project.internal_id,
        )
    )
    session.flush()

    response = client.post("/v1/admin/slugs/a/release", headers=master_headers)

    assert response.status_code == 404
    # And it's still there, untouched, for its own tenant.
    assert session.get(RetiredSlug, ("other", "a")) is not None
