import pytest

from tests.conftest import create_user


@pytest.fixture
def juan(client, session, tenant) -> dict:
    """One ordinary external user. Nobody mints a user any more: it exists
    because an identity provider asserted it, so this is `create_user` and
    not the old POST /v1/users + POST /v1/users/{id}/keys pair."""
    return create_user(client, session)


def test_a_user_creates_a_project_owned_by_itself(client, juan, tenant):
    response = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    assert response.status_code == 201
    body = response.json()
    assert body["project_slug"] == "payments-api"
    assert body["owner"] == {"type": "user", "id": juan["user_id"]}


def _string_values(obj):
    """Every string leaf in a JSON-decoded body, depth-first."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _string_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _string_values(v)
    elif isinstance(obj, str):
        yield obj


def test_the_project_response_never_exposes_internals(client, juan, tenant):
    body = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    ).json()

    # R1: str(body) would also match the "project_slug" KEY, so check the
    # serialized VALUES only. This fails iff a bank id (project_<uuid>) or an
    # internal id (prj_<hex>) actually leaks into the response.
    leaked = [v for v in _string_values(body) if "prj_" in v or "project_" in v]
    assert leaked == []


def test_a_user_cannot_create_a_project_owned_by_someone_else(
    client, juan, new_user, tenant
):
    other = new_user()["user_id"]

    response = client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "owner": {"type": "user", "id": other}},
        headers=juan["headers"],
    )

    assert response.status_code == 403


def test_master_creates_a_project_owned_by_a_group(
    client, master_headers, tenant, session
):
    """Authority is exactly what lets an operator name a group it is not
    itself in -- an ordinary caller may only name a group its IdP asserts.

    The `Group` row is seeded directly because POST /v1/groups is gone:
    membership arrives in a token, and `projects._validate_owner` only
    creates the projection row for a group the CALLER holds.
    """
    from memory.models import Group

    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()

    response = client.post(
        "/v1/projects",
        json={
            "project_slug": "payments-api",
            "owner": {"type": "group", "id": "grp_payments"},
        },
        headers=master_headers,
    )

    assert response.status_code == 201
    assert response.json()["owner"] == {"type": "group", "id": "grp_payments"}


def test_creating_an_existing_slug_conflicts(client, juan, tenant):
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_SLUG_CONFLICT"


def test_rename_forwards_the_old_slug(client, juan, tenant):
    client.post(
        "/v1/projects",
        json={"project_slug": "github.com-acme-payments-api"},
        headers=juan["headers"],
    )

    renamed = client.patch(
        "/v1/projects/github.com-acme-payments-api",
        json={"project_slug": "payments-api"},
        headers=juan["headers"],
    )
    forwarded = client.get(
        "/v1/projects/github.com-acme-payments-api", headers=juan["headers"]
    )

    assert renamed.status_code == 200
    assert forwarded.status_code == 200
    assert forwarded.json()["project_slug"] == "payments-api"
    assert forwarded.json()["resolved_from"] == "github.com-acme-payments-api"
    assert forwarded.json()["notice"] == "PROJECT_RENAMED"


def test_creating_onto_a_retired_slug_conflicts(client, juan, tenant):
    """The database's unique constraint alone does not catch this: a retired
    slug is no longer live, so nothing stops a plain INSERT from succeeding
    and pointing "a" at a second, unrelated project. The point of the
    tombstone invariant is that a forward never starts pointing somewhere
    new (SPEC inv. 13) — so the pre-existing forward must survive too."""
    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])

    response = client.post(
        "/v1/projects", json={"project_slug": "a"}, headers=juan["headers"]
    )
    forwarded = client.get("/v1/projects/a", headers=juan["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_SLUG_CONFLICT"
    assert forwarded.json()["project_slug"] == "b"


def test_renaming_onto_a_retired_slug_conflicts(client, juan, tenant):
    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])
    client.post("/v1/projects", json={"project_slug": "c"}, headers=juan["headers"])

    response = client.patch(
        "/v1/projects/c", json={"project_slug": "a"}, headers=juan["headers"]
    )

    assert response.status_code == 409


def test_oversize_git_locator_is_a_typed_422(client, juan, tenant):
    response = client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "g" * 5000},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_create_rejects_an_unknown_field(client, juan, tenant):
    """Sibling of test_patch_rejects_an_unknown_field (review finding F2): a
    typoed `git_locator` at create time otherwise 201s with the field left
    null, and the caller believes the project is pinned when it is not --
    the same silent no-op I1's PATCH fix closed, entered through the create
    route instead.

    Deleting `model_config = ConfigDict(extra="forbid")` from
    `CreateProjectRequest` (reverting to pydantic's default `extra="ignore"`)
    turns this red: the typo'd field would be silently swallowed and the
    response would come back 201 with `git_locator: null`.
    """
    response = client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "gti_locator": "github.com/acme/api"},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_patch_updates_the_git_locator(client, juan, tenant):
    """SPEC §8.4's promised recovery path (review finding I1): a poisoned
    locator otherwise locks the whole owning group out of the project's
    memory with no API repair.

    Deleting the `git_locator` branch in `update_project`, or dropping the
    `body.model_fields_set` check in favor of a plain `if body.git_locator`,
    turns this red: the stored locator stays "github.com/acme/wrong".
    """
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/wrong"},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"git_locator": "github.com/acme/payments-api"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["git_locator"] == "github.com/acme/payments-api"


def test_patch_with_an_explicit_null_clears_the_git_locator(client, juan, tenant):
    """§8.4 says "clear or update". Clearing re-opens first-toucher
    enrichment, which is the escape hatch when nobody knows the right value.

    Deleting the ternary's `if body.git_locator else None` (leaving a bare
    `canonical_locator(body.git_locator)` call) turns this red: `None` would
    blow up canonicalizing, or a `body.git_locator or project.git_locator`
    style mutant would silently keep the old value instead of clearing it.
    """
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/wrong"},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/payments-api", json={"git_locator": None}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["git_locator"] is None


def test_patch_without_git_locator_leaves_it_alone(client, juan, tenant):
    """An omitted key is not the same as null -- a rename must not wipe it.

    Replacing `"git_locator" in body.model_fields_set` with a `body.git_locator
    is not None` check turns this red the other way: it would still leave a
    correct locator alone, but the null-clears test above would then fail
    instead -- the two tests together pin the field-presence check that a
    bare None-check cannot satisfy simultaneously.
    """
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/payments-api"},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/payments-api", json={"project_slug": "payments"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["project_slug"] == "payments"
    assert response.json()["git_locator"] == "github.com/acme/payments-api"


def test_project_metadata_round_trips(client, juan, tenant):
    """Derivable orientation is project metadata, not a durable memory fact."""
    client.post(
        "/v1/projects", json={"project_slug": "acme-api"}, headers=juan["headers"]
    )

    patched = client.patch(
        "/v1/projects/acme-api",
        json={
            "name": "Acme API",
            "canonical_spec": "docs/SPEC.md",
            "purpose": "Billing and entitlements for the Acme platform.",
        },
        headers=juan["headers"],
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Acme API"
    assert patched.json()["canonical_spec"] == "docs/SPEC.md"
    assert patched.json()["purpose"].startswith("Billing")

    fetched = client.get("/v1/projects/acme-api", headers=juan["headers"]).json()
    assert fetched["name"] == "Acme API"
    assert fetched["canonical_spec"] == "docs/SPEC.md"


def test_patch_with_an_explicit_null_clears_project_metadata(client, juan, tenant):
    """Same "clear or update" semantics the locator already has: a null is a
    deliberate clear, and retracted orientation must actually go away.

    Replacing any of the three `in body.model_fields_set` checks with a
    `body.<field> is not None` check turns this red -- the clear is skipped and
    the stale value survives.
    """
    client.post(
        "/v1/projects", json={"project_slug": "acme-api"}, headers=juan["headers"]
    )
    client.patch(
        "/v1/projects/acme-api",
        json={"name": "Acme API", "canonical_spec": "docs/SPEC.md", "purpose": "Billing."},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/acme-api",
        json={"name": None, "canonical_spec": None, "purpose": None},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["name"] is None
    assert response.json()["canonical_spec"] is None
    assert response.json()["purpose"] is None


def test_patch_without_the_metadata_keys_leaves_them_alone(client, juan, tenant):
    """An omitted key is not the same as null: a rename, or a console editor
    saving only the field someone touched, must not wipe the other two.

    This is the other direction of the pair above -- a `body.<field> is not
    None` check passes here and fails the clear test, so only the
    field-presence check satisfies both.
    """
    client.post(
        "/v1/projects", json={"project_slug": "acme-api"}, headers=juan["headers"]
    )
    client.patch(
        "/v1/projects/acme-api",
        json={"name": "Acme API", "canonical_spec": "docs/SPEC.md", "purpose": "Billing."},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/acme-api",
        json={"purpose": "Billing and entitlements."},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Acme API"
    assert response.json()["canonical_spec"] == "docs/SPEC.md"
    assert response.json()["purpose"] == "Billing and entitlements."


def test_an_empty_metadata_string_is_a_typed_422(client, juan, tenant):
    """min_length=1, for the reason git_locator has it: an explicit null is a
    deliberate clear, an empty string carries no orientation and almost always
    signals a caller bug. The two intents must not collapse into one.
    """
    client.post(
        "/v1/projects", json={"project_slug": "acme-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/acme-api", json={"name": ""}, headers=juan["headers"]
    )

    assert response.status_code == 422


def test_a_control_character_in_metadata_is_a_typed_422(client, juan, tenant):
    """These values reach the projects UPDATE. A NUL there is a psycopg
    DataError -> 500, the same failure git_locator's validator was added for.
    """
    client.post(
        "/v1/projects", json={"project_slug": "acme-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/acme-api",
        json={"purpose": "Billing\x00and entitlements."},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_oversize_metadata_is_a_typed_422(client, juan, tenant):
    """purpose is capped at 256 because it is one line -- a budget, not a text
    field. Without max_length the column overflow is a DB error, not a 422.
    """
    client.post(
        "/v1/projects", json={"project_slug": "acme-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/acme-api", json={"purpose": "x" * 257}, headers=juan["headers"]
    )

    assert response.status_code == 422


def test_patch_rejects_an_unknown_field(client, juan, tenant):
    """The silent no-op is the bug behind I1: a caller following the SPEC got
    200 OK and no change. An unknown key must be a typed 422.

    Deleting `model_config = ConfigDict(extra="forbid")` from
    `UpdateProjectRequest` (reverting to pydantic's default `extra="ignore"`)
    turns this red: a typo'd field name would be silently swallowed and the
    response would come back 200.
    """
    client.post("/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"])

    response = client.patch(
        "/v1/projects/payments-api", json={"gti_locator": "typo"}, headers=juan["headers"]
    )

    assert response.status_code == 422


def test_patch_canonicalizes_a_non_canonical_locator(client, juan, tenant, session):
    """SPEC §8.3/§8.4's repair path must store ONE spelling, not the caller's
    raw bytes. Every other locator test in this file already uses a canonical
    string, so a mutant that skips canonicalization entirely (`project.git_locator
    = body.git_locator`) is invisible to them -- this is the one test that
    isn't, by PATCHing a scheme-prefixed, `.git`-suffixed, mixed-case spelling.

    Deleting the `domain.canonical_locator(...)` call in `update_project`
    turns this red two ways: the response would echo the raw non-canonical
    string instead of `github.com/acme/payments-api`, and the follow-up
    `domain.resolve` call below -- which DOES canonicalize before comparing
    (SPEC §8.4) -- would then see two different spellings and raise
    `ProjectLocatorMismatch` for a repair that was supposed to have fixed
    exactly this.
    """
    from memory import projects as domain
    from memory.auth.principal import Principal

    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/wrong"},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"git_locator": "https://GitHub.com/acme/Payments-API.git"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["git_locator"] == "github.com/acme/payments-api"

    principal = Principal(
        tenant_id=tenant, user_id=juan["user_id"], subject=juan["subject"]
    )
    try:
        result = domain.resolve(
            session,
            principal,
            "payments-api",
            git_locator="github.com/acme/payments-api",
            create=False,
        )
    except Exception as exc:  # noqa: BLE001 -- the point is to prove no mismatch
        pytest.fail(f"repaired locator did not match its own canonical form: {exc!r}")
    assert result.project.git_locator == "github.com/acme/payments-api"


def test_oversize_git_locator_on_patch_is_a_typed_422(client, juan, tenant):
    """PATCH twin of `test_oversize_git_locator_is_a_typed_422`, which only
    ever exercises `CreateProjectRequest` via POST.

    Deleting `max_length=512` from `UpdateProjectRequest.git_locator` turns
    this red: the >512-char value canonicalizes fine (it has a host and a
    path) and would then hit the `projects.git_locator` column's
    `String(512)` constraint at `db.commit()`, an unhandled 500 instead of
    the typed 422 the field's own comment promises.
    """
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"git_locator": "github.com/acme/" + "a" * 5000},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_patch_with_empty_string_locator_is_a_typed_422(client, juan, tenant):
    """An empty string carries no locator information and almost always
    signals a caller bug -- unlike an explicit `null`, which is SPEC §8.4's
    deliberate "clear" (see `test_patch_with_an_explicit_null_clears_the_git_locator`,
    its sibling). The two intents must not collapse into the same
    clear-the-column branch.

    Deleting `min_length=1` from `UpdateProjectRequest.git_locator` turns
    this red: `""` would 200 and silently clear the column instead of 422ing.
    """
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/payments-api"},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/payments-api", json={"git_locator": ""}, headers=juan["headers"]
    )

    assert response.status_code == 422


def test_patch_rename_and_locator_together_apply_both_and_audit_both(
    client, juan, tenant, session
):
    """A rename and a locator update in the same request must both apply,
    in the right order: `update_project` reads the current canonical slug for
    the locator's audit event AFTER the rename branch already mutated it, so
    the locator event's resource is the NEW slug, not the old one.

    Reordering the two `if` blocks (locator before rename) would still pass
    the response-body assertions below, but would flip the audit resource
    back to the old slug, turning this red on the `events` assertion.
    """
    from memory.models import AuditEvent

    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments", "git_locator": "github.com/acme/payments-api"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["project_slug"] == "payments"
    assert response.json()["git_locator"] == "github.com/acme/payments-api"

    # Filtered to project-scoped events: this test pins the ORDER of the two
    # events this PATCH writes, not whatever the `juan` fixture's first-sight
    # provisioning happens to record. `project.create` IS pinned: every
    # creation is audited, not only an operator's.
    events = [
        (e.action, e.resource)
        for e in session.query(AuditEvent).all()
        if e.action.startswith("project.")
    ]
    assert events == [
        ("project.create", "payments-api"),
        ("project.rename", "payments-api -> payments"),
        ("project.locator.update", "payments"),
    ]


def test_an_outsider_cannot_rename_a_project(client, juan, new_user, tenant):
    bob = new_user()
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "renamed"},
        headers=bob["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert "owner_type" not in str(response.json())


def test_an_outsider_cannot_patch_the_locator_before_any_write(
    client, juan, new_user, tenant, session
):
    """A locator-only PATCH (no rename) from a non-owner must be hidden, and
    must not mutate the column or write an audit event first --
    `domain.resolve(..., create=False)` calls `authorize()` unconditionally
    before either `if` branch in `update_project` runs.

    Moving that `authorize()` call to after the git_locator branch (or
    dropping it) would let this outsider's locator through, turning either
    the stored-value or the audit-emptiness assertion below red.
    """
    from memory.models import AuditEvent

    bob = new_user()
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/payments-api"},
        headers=juan["headers"],
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"git_locator": "github.com/acme/hijacked"},
        headers=bob["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert "owner_type" not in str(response.json())
    stored = client.get("/v1/projects/payments-api", headers=juan["headers"]).json()
    assert stored["git_locator"] == "github.com/acme/payments-api"
    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "project.locator.update" not in actions


def test_an_outsider_cannot_transfer_a_project(client, juan, new_user, tenant):
    bob = new_user()
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "user", "id": bob["user_id"]},
        headers=bob["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert "owner_type" not in str(response.json())


def test_transfer_to_a_group_lets_a_member_in(client, juan, new_user, tenant, session):
    """Alice holds the group because her IdP asserts it, not because a
    `group_members` row says so. The `Group` projection is seeded here
    because juan -- who performs the transfer -- is not in the group, and
    `_validate_owner` only creates the row for a group the caller holds."""
    from memory.models import Group

    alice = new_user(groups=("grp_payments",))
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    transferred = client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=juan["headers"],
    )
    for_alice = client.get("/v1/projects/payments-api", headers=alice["headers"])

    assert transferred.status_code == 200
    assert for_alice.status_code == 200


def test_an_outsider_sees_the_same_error_as_an_unknown_project(
    client, juan, new_user, tenant
):
    bob = new_user()
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.get(
        "/v1/projects/payments-api", headers=bob["headers"]
    )
    unknown = client.get(
        "/v1/projects/no-such-project", headers=bob["headers"]
    )

    assert response.status_code == unknown.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert response.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert "owner_type" not in str(response.json())
    assert juan["user_id"] not in str(response.json())


def test_listing_shows_only_projects_the_caller_can_reach(
    client, juan, new_user, master_headers, tenant
):
    bob = new_user()
    client.post("/v1/projects", json={"project_slug": "mine"}, headers=juan["headers"])
    client.post(
        "/v1/projects",
        json={"project_slug": "theirs"},
        headers=bob["headers"],
    )

    listed = client.get("/v1/projects", headers=juan["headers"]).json()
    for_master = client.get("/v1/projects", headers=master_headers).json()

    assert [p["project_slug"] for p in listed] == ["mine"]
    # An operator sees the whole tenant. Asserted here because the listing
    # gets its answer from authorize(), so every principal kind it handles
    # needs a test or the delegation is only as good as a reading.
    assert [p["project_slug"] for p in for_master] == ["mine", "theirs"]


def test_listing_includes_a_project_owned_by_the_callers_group(
    client, new_user, master_headers, tenant, session
):
    """The group branch of the listing. Deleting it from authorize() used to
    leave the whole suite green: no test listed as a member.

    The member holds `grp_payments` because the IdP asserts it in her token
    -- there is no membership row to insert any more."""
    from memory.models import Group

    member = new_user(groups=("grp_payments",))
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    client.post(
        "/v1/projects",
        json={
            "project_slug": "shared",
            "owner": {"type": "group", "id": "grp_payments"},
        },
        headers=master_headers,
    )

    listed = client.get("/v1/projects", headers=member["headers"]).json()

    assert [p["project_slug"] for p in listed] == ["shared"]


def test_listing_is_scoped_to_the_callers_tenant(client, juan, tenant, session):
    """Mirrors test_a_slug_in_another_tenant_is_invisible in test_projects.py,
    whose docstring notes this exact hole was found and closed one layer down
    (resolve()'s _live/_forwarded) — it has reopened one layer up, in the
    listing query. owner_id carries no FK to a tenant-scoped row, so a
    project belonging to a DIFFERENT tenant, whose owner_id happens to equal
    this caller's own user_id, must still be invisible: authorize() alone
    cannot tell tenants apart, so the query's tenant filter is the only thing
    that can."""
    from memory import ids
    from memory.models import Project, ProjectSlug, Tenant

    session.add(Tenant(id="ten_other"))
    session.flush()
    other_project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id="ten_other",
        owner_type="user",
        owner_id=juan["user_id"],
        bank_id=ids.new_project_bank_id(),
    )
    session.add(other_project)
    session.flush()
    session.add(
        ProjectSlug(
            tenant_id="ten_other",
            slug="not-mine",
            project_internal_id=other_project.internal_id,
            is_canonical=True,
        )
    )
    session.flush()

    listed = client.get("/v1/projects", headers=juan["headers"]).json()

    assert listed == []


def test_transfer_writes_an_audit_event(client, juan, tenant, session):
    from memory.models import AuditEvent, Group

    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )
    client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=juan["headers"],
    )

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "project.transfer" in actions


def test_patch_locator_writes_an_audit_event(client, juan, tenant, session):
    """Sibling of `test_transfer_writes_an_audit_event`: the only durable
    record of who repaired a shared project's locator (forensics if the
    "wrong" locator was an attempted memory-poisoning vector, SPEC §20.2) is
    this audit row -- nothing else observes the repair happening.

    Deleting the `audit.record(..., "project.locator.update", ...)` call in
    `update_project`, moving it below `db.commit()` (where an uncommitted add
    is discarded on session close, same as every other route in this
    module), or changing its action string all turn this red.
    """
    from memory.models import AuditEvent

    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    client.patch(
        "/v1/projects/payments-api",
        json={"git_locator": "github.com/acme/payments-api"},
        headers=juan["headers"],
    )

    events = [(e.action, e.resource) for e in session.query(AuditEvent).all()]
    assert ("project.locator.update", "payments-api") in events


def test_an_operator_create_writes_an_audit_event(
    client, master_headers, tenant, session
):
    """R4 / SPEC §20 MUST: record an operator's actions, same as
    rename/transfer -- the create here is one only authority allows, since
    the operator names a group it does not itself hold."""
    from memory.models import AuditEvent, Group

    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    client.post(
        "/v1/projects",
        json={
            "project_slug": "payments-api",
            "owner": {"type": "group", "id": "grp_payments"},
        },
        headers=master_headers,
    )

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "project.create" in actions


def test_an_external_caller_may_create_a_project_owned_by_an_asserted_group(
    app, client, session, tenant
):
    """An ordinary caller may only create a project it owns itself. A caller
    the IdP places in a group owns that group's projects too -- otherwise it
    could reach a group project by transfer but never make one."""
    from memory import ids
    from memory.api.app import current_principal
    from memory.auth.principal import Principal
    from memory.models import User

    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    app.dependency_overrides[current_principal] = lambda: Principal(
        tenant_id=tenant,
        user_id=user.id,
        subject="external@test",
        groups=frozenset({"grp_platform"}),
        credential_id="ext_test",
    )

    response = client.post(
        "/v1/projects",
        json={"project_slug": "acme-api", "owner": {"type": "group", "id": "grp_platform"}},
    )

    assert response.status_code == 201, response.text
    assert response.json()["owner"]["id"] == "grp_platform"
