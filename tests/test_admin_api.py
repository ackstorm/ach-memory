import httpx
import pytest
import respx

from tests.conftest import OPERATOR_SUBJECT, RESOLVER_URL

BASE = "http://hindsight.test"


@pytest.fixture
def juan(new_user) -> dict:
    """An ordinary external user: authenticated, owns a bank, and NOT named in
    MEMORY_MASTER_USERS. Every "refuses a non-operator" assertion below is
    about this caller -- who now differs from `master_headers` only in what
    configuration says about their subject."""
    return new_user()


def _operator_user_id(session) -> str:
    """The `usr_` id `link_identity` minted for the operator on first sight.

    Not knowable in advance -- which is exactly why MEMORY_MASTER_USERS names
    the SUBJECT instead.
    """
    from memory.models import ExternalIdentity

    return session.get(ExternalIdentity, (RESOLVER_URL, OPERATOR_SUBJECT)).user_id


def _credential_id(subject: str) -> str:
    """The `ext_` credential id an audit row records for a subject."""
    from memory.auth.provisioning import credential_id_for

    return credential_id_for(RESOLVER_URL, subject)


# ---------------------------------------------------------------------------
# Task 2: GET /v1/admin/audit
# ---------------------------------------------------------------------------


def test_the_audit_read_requires_operator_authority(client, juan, master_headers, tenant):
    """Authority is configuration over a resolved external identity now, not a
    credential anybody mints. `juan` authenticates perfectly well and is still
    refused; the operator subject MEMORY_MASTER_USERS names is let through."""
    assert client.get("/v1/admin/audit", headers=juan["headers"]).status_code == 403
    assert client.get("/v1/admin/audit", headers=master_headers).status_code == 200


def test_a_group_named_in_master_groups_grants_the_audit_read(
    client, new_user, tenant, monkeypatch
):
    """The MEMORY_MASTER_GROUPS half: authority can arrive through a group the
    identity provider asserts, with nothing about the user configured.
    Membership is re-read from the token every request, so an IdP that stops
    asserting `sre` closes this on the very next call."""
    from memory.config import get_settings

    sre = new_user(groups=("sre",))
    assert client.get("/v1/admin/audit", headers=sre["headers"]).status_code == 403

    monkeypatch.setenv("MEMORY_MASTER_GROUPS", "sre")
    get_settings.cache_clear()

    assert client.get("/v1/admin/audit", headers=sre["headers"]).status_code == 200


def test_it_returns_events_newest_first(client, master_headers, tenant):
    # project.create + project.rename, not the old user.create + key.create:
    # neither of those routes exists any more. Any two audited actions in one
    # transaction reproduce the tie this test is about.
    client.post("/v1/projects", json={"project_slug": "a"}, headers=master_headers)
    client.patch(
        "/v1/projects/a", json={"project_slug": "b"}, headers=master_headers
    )

    events = client.get("/v1/admin/audit", headers=master_headers).json()

    # Not actions[:2] == ["project.rename", "project.create"]: created_at is now
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
    assert set(actions[:2]) == {"project.create", "project.rename"}


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


def test_it_filters_by_action_and_by_actor(client, juan, master_headers, tenant):
    """Both filters, on two DIFFERENT actors. `actor_key_id` used to be NULL
    for every operator action, so an actor filter could not tell an operator
    apart from anyone else; an operator is an ordinary external identity now,
    so their rows carry a real `ext_` credential and this filter means
    something."""
    client.post("/v1/projects", json={"project_slug": "juans"}, headers=juan["headers"])
    client.post("/v1/projects", json={"project_slug": "ops"}, headers=master_headers)

    only = client.get(
        "/v1/admin/audit?action=project.create", headers=master_headers
    ).json()
    assert [e["action"] for e in only] == ["project.create"] * 2
    assert {e["resource"] for e in only} == {"juans", "ops"}

    by_actor = client.get(
        f"/v1/admin/audit?actor_key_id={_credential_id(juan['subject'])}",
        headers=master_headers,
    ).json()
    assert [e["resource"] for e in by_actor] == ["juans"]

    by_operator = client.get(
        f"/v1/admin/audit?actor_key_id={_credential_id(OPERATOR_SUBJECT)}",
        headers=master_headers,
    ).json()
    assert [e["resource"] for e in by_operator] == ["ops"]


def test_an_audit_row_never_carries_a_bank_id(
    client, juan, master_headers, tenant, session
):
    """The table is a disclosure surface the moment it is readable."""
    from memory.models import AuditEvent

    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])

    body = client.get("/v1/admin/audit", headers=master_headers).text
    assert "bank_id" not in body
    assert "prj_" not in body
    # `user_<uuid>` / `prj_<uuid>` are the two bank-id shapes (memory/ids.py);
    # neither may ever appear as an audited resource.
    assert not [
        e
        for e in session.query(AuditEvent).all()
        if ("user_" in (e.resource or "") or "prj_" in (e.resource or ""))
        and "-" in (e.resource or "")
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


def test_clear_memories_refuses_an_ordinary_user_even_the_banks_own_owner(
    client, juan, tenant
):
    """SPEC §11.7's whole point: a caller who OWNS this very bank must still be
    refused. `require_master` gates on authority alone, before scope or
    ownership is ever resolved."""
    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=juan["headers"],
    )

    assert response.status_code == 403


def test_delete_bank_refuses_an_ordinary_user_even_the_banks_own_owner(client, juan, tenant):
    response = client.delete(
        "/v1/admin/memory/user",
        params={"user_id": juan["user_id"]},
        headers=juan["headers"],
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "upstream_pattern"),
    [
        (
            "POST",
            "/v1/admin/memory/user/clear",
            rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)",
        ),
        (
            "DELETE",
            "/v1/admin/memory/user",
            rf"{BASE}/v1/default/banks/[^/]+$",
        ),
    ],
)
@respx.mock
def test_operator_destructive_routes_are_rate_limited_before_hindsight(
    client,
    juan,
    master_headers,
    tenant,
    monkeypatch,
    method,
    path,
    upstream_pattern,
):
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()

    import uuid

    warmup = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$"
    ).mock(return_value=httpx.Response(200, json={"status": "pending"}))
    destructive = respx.delete(url__regex=upstream_pattern).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    response = client.post(
        "/v1/memory/retain",
        json={
            "scope": "user",
            "user_id": juan["user_id"],
            "content": "warmup",
            "memory_type": "fact",
            "basis": "human_explicit",
            "trigger": "agent_proactive",
            "evidence": [{"kind": "user_quote", "raw": "warmup"}],
            "operation_id": str(uuid.uuid4()),
        },
        headers=master_headers,
    )
    assert response.status_code == 202, response.text
    assert warmup.called

    response = client.request(
        method,
        path,
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert not destructive.called


def test_release_slug_requires_operator_authority(client, juan, tenant):
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
    create=False)`'s outcome: an unknown slug is a 404 and no Project row is
    minted by an erase attempt. `create=False` is now the only thing refusing
    it -- an operator has an identity of their own and could otherwise own a
    lazily created project, so the flag stopped being redundant here."""
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
    would cascade into that user's external identity link and project
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
















@respx.mock
def test_clear_without_a_target_clears_the_operators_own_bank(
    client, master_headers, tenant, session
):
    """The rule that REPLACED "master-key requests with scope=user must set
    user_id". Authority and identity are separate now, so an operator is an
    ordinary user who also has authority: with no `user_id` they address
    THEMSELVES, exactly like anybody else, and naming somebody else is the
    part authority buys.

    Also pins that this self-directed call writes no audit row: `_resolve_bank`
    records only `principal.is_master and body.user_id`, i.e. reaching into
    SOMEBODY ELSE's bank. Auditing an operator touching their own memory would
    drown the log the delegation records live in.
    """
    from memory.models import AuditEvent, User

    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))
    before = session.query(AuditEvent).count()

    response = client.post("/v1/admin/memory/user/clear", headers=master_headers)

    assert response.status_code == 200, response.text
    operator = (
        session.query(User)
        .filter(User.id == _operator_user_id(session))
        .one()
    )
    assert f"banks/{operator.bank_id}/" in str(route.calls.last.request.url)
    assert session.query(AuditEvent).count() == before


def test_clear_still_refuses_an_ordinary_user_addressing_someone_else(
    client, juan, new_user, tenant
):
    """The other side of the same rule, and the one that must never soften:
    only authority lets a caller name somebody else. Without operator
    authority this is a 403 on the admin gate before ownership is even
    resolved."""
    victim = new_user()

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": victim["user_id"]},
        headers=juan["headers"],
    )

    assert response.status_code == 403


def test_release_slug_frees_the_name_and_leaves_the_project_alone(
    client, juan, master_headers, tenant, session
):
    from memory.models import ProjectSlug

    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])

    response = client.post("/v1/admin/slugs/a/release", headers=master_headers)

    assert response.status_code == 204
    assert session.get(ProjectSlug, (tenant, "a")) is None
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
    from memory.models import Project, ProjectSlug, Tenant

    session.add(Tenant(id="other"))
    session.flush()
    other_project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id="other",
        owner_type="user",
        owner_id="usr_whoever",
        bank_id=ids.new_project_bank_id(),
    )
    other_project.slug_rows.append(
        ProjectSlug(tenant_id="other", slug="b", is_canonical=True)
    )
    session.add(other_project)
    session.flush()
    session.add(
        ProjectSlug(
            tenant_id="other",
            slug="a",
            project_internal_id=other_project.internal_id,
            is_canonical=False,
        )
    )
    session.flush()

    response = client.post("/v1/admin/slugs/a/release", headers=master_headers)

    assert response.status_code == 404
    # And it's still there, untouched, for its own tenant.
    assert session.get(ProjectSlug, ("other", "a")) is not None


# --- scope in the body, not only the query string -----------------------------
# These two routes took user_id/project_slug ONLY as query parameters while every
# data-plane route takes them in a JSON body. A caller who followed the house
# style got `INVALID_SCOPE: ... must set user_id` -- naming the field they had
# just set, in the body that was ignored. That particular refusal is gone (an
# operator with no user_id now addresses their own bank), but the
# body-or-query symmetry it forced is what these tests pin.


@respx.mock
def test_clear_accepts_the_scope_in_the_body(client, juan, master_headers, tenant):
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    response = client.post(
        "/v1/admin/memory/user/clear",
        json={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200, response.text
    assert route.called


@respx.mock
def test_delete_bank_accepts_the_scope_in_the_body(
    client, juan, master_headers, tenant
):
    route = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    response = client.request(
        "DELETE",
        "/v1/admin/memory/user",
        json={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200, response.text
    assert route.called


@respx.mock
def test_clear_still_accepts_the_scope_in_the_query(
    client, juan, master_headers, tenant
):
    """The old spelling keeps working -- this is additive, not a migration."""
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200
    assert route.called


@respx.mock
def test_clear_refuses_a_contradiction_between_query_and_body(
    client, juan, master_headers, tenant
):
    """Silently preferring one source would make the target of an irreversible
    erase depend on a precedence rule nobody read."""
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        json={"user_id": "usr_somebody_else"},
        headers=master_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCOPE"
    assert "given twice" in response.json()["error"]["message"]
    assert not route.called, "must not reach Hindsight on a contradictory target"


@respx.mock
def test_clear_agreeing_query_and_body_is_not_a_contradiction(
    client, juan, master_headers, tenant
):
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": juan["user_id"]},
        json={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200, response.text
    assert route.called


def test_clear_rejects_a_control_character_in_the_body_as_422_not_500(
    client, master_headers, tenant
):
    """Validated as a route parameter, so FastAPI answers before the route body
    runs. Raised inside `_admin_scope` instead, pydantic's ValidationError
    escapes as a 500 through app.py's catch-all."""
    response = client.post(
        "/v1/admin/memory/user/clear",
        json={"user_id": "usr_\x00bad"},
        headers=master_headers,
    )

    assert response.status_code == 422


def test_clear_rejects_an_unknown_body_field(client, juan, master_headers, tenant):
    response = client.post(
        "/v1/admin/memory/user/clear",
        json={"user_id": juan["user_id"], "scope": "user"},
        headers=master_headers,
    )

    assert response.status_code == 422, "scope comes from the path, not the body"
