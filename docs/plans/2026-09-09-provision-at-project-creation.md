# Provision a Project Bank at Creation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make a project fully usable the moment it is created, whichever path created it — so `POST /v1/bootstrap` becomes a pre-warm rather than a prerequisite nobody can be relied on to call.

**Architecture:** Two things make a bank usable: the `ach-exact-v1` retain strategy and the scope's built-in mental model. Today both are applied *only* by `bootstrap`. `projects.create` mints a `bank_id` on a row and stops there, so a project created through `POST /v1/projects` has no retain strategy and no `project-context` model — and therefore delivers empty standing context forever, until somebody happens to call bootstrap with that slug. This plan extracts one provisioning helper and calls it from both creation paths, leaving `projects.create` itself free of network I/O.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Pydantic v2, FastAPI, pytest, ruff, uv.

**Testing discipline:** every task names ONE test file. Run only that file. The full suite (`make test`) is FORBIDDEN until Task 5.

---

## The bug, stated precisely

`projects.create` (`src/memory/projects.py:215-263`) builds the row:

```python
project = Project(
    internal_id=ids.new_project_internal_id(),
    bank_id=ids.new_project_bank_id(),   # a generated id, nothing more
    ...
)
```

No `ensure_exact_retain_strategy`, no `reconcile_builtin`. Those live only in `bootstrap` (`src/memory/bootstrap.py:105-110`).

`POST /v1/projects` (`src/memory/api/projects.py:155`) calls `domain.create` directly and commits. So every project created that way is half-provisioned. `load_context` for it finds no `project-context` registration and returns nothing, permanently.

The repo's own test fixture takes this path — `tests/test_mcp_tools.py::_seed_project` posts to `/v1/projects` — so the shape is already exercised; nothing asserts the consequence.

**What is NOT broken, and must stay that way.** Lazy creation is currently unreachable: `bootstrap.py:85` is the only `create=True` call site in the service. Retain (`retention.py:59`), reads (`read_context.py:113`), context load (`context_service.py:143`), working state and directives all pass `create=False` explicitly. Do not change that in this plan — reads that auto-vivify a project would let any authenticated caller squat a slug, and would put a mental-model round trip inside `load_context`'s 2s deadline.

---

## Task 1: A failing test that names the bug

Write the regression first, so the fix is proven rather than assumed.

**Files:**
- Test: `tests/test_bootstrap.py`

**Step 1: Write the failing test**

Follow the file's existing fixture and `respx` conventions.

```python
@respx.mock
def test_a_project_created_through_the_control_plane_is_fully_provisioned(
    client, session, master_headers
):
    """A project is usable when created, not when someone remembers to
    bootstrap it. Before this, POST /v1/projects minted a bank id and
    nothing else: no retain strategy, no project-context model, so
    load_context delivered empty standing context for that project for ever."""
    key = _make_user_key(client, master_headers)

    response = client.post(
        "/v1/projects",
        json={"project_slug": "acme/app"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 201

    project = _project_by_slug(session, "acme/app")
    registration = model_registry.get_registered_model(
        session, _project_bank_ref(session, project), "project-context"
    )
    assert registration is not None, "project-context must exist at creation"
    assert registration.origin == "builtin"
    assert registration.lifecycle_state in {"creating", "active"}
```

Assert the retain strategy too — but note the `app` fixture stubs `ensure_exact_retain_strategy` to a no-op on both `retention` and `bootstrap_service` (`tests/conftest.py:205-208`). Whichever module ends up owning the helper needs the same stub, or this test will try to reach Hindsight. Adding the stub is part of this task; **do not** work around it by asserting less.

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_bootstrap.py -k control_plane_is_fully_provisioned -v
```
Expected: FAIL — `registration is None`.

**Step 3-5:** No implementation in this task. Commit the failing test on its own branch commit so the defect is recorded before the fix:

```bash
git add tests/test_bootstrap.py
git commit -m "test(bootstrap): pin that control-plane project creation must provision"
```

If the repo's hooks refuse a red commit, skip this commit and carry the test into Task 2 instead.

---

## Task 2: Extract the provisioning helper and call it from both creators

**Files:**
- Modify: `src/memory/bootstrap.py`
- Modify: `src/memory/api/projects.py:155-165`
- Test: `tests/test_bootstrap.py`

**Step 1: Design decision, made here so the executor does not have to**

`projects.create` stays free of network I/O. It is a DB-layer function; threading a Hindsight client into it would make a row insert fail when Hindsight is unreachable, and would force the client through `resolve`/`_create`/`create` and every caller of them.

Instead, put the helper in `bootstrap.py` — which already owns both calls — and call it from the two places that create a project:

```python
def provision_project_bank(
    db: Session, principal: Principal, project: Project, *, client
) -> MentalModelView | None:
    """Everything a project bank needs before it can serve: the exact retain
    strategy, and its built-in model.

    Idempotent by construction -- `ensure_exact_retain_strategy` and
    `reconcile_builtin` are both no-ops on an already-provisioned bank -- so
    calling it again from bootstrap repairs anything a failed creation left
    half-done.
    """
    bank = LogicalBankRef(
        principal.tenant_id, "project", None, project.internal_id, project.bank_id
    )
    ensure_exact_retain_strategy(client, bank.bank_id)
    return mental_model_service.reconcile_builtin(db, bank, PROJECT_CONTEXT, client=client)
```

**Step 2: Wire it into `bootstrap`**

Replace the inline block at `bootstrap.py:105-110` with a call to the helper. Behaviour must not change — the same test file's existing bootstrap tests are the guard.

**Step 3: Wire it into the control-plane route**

In `api/projects.py:155`, after `domain.create` and before `db.commit()`:

```python
    project = domain.create(...)
    try:
        provision_project_bank(db, principal, project, client=get_client())
    except Exception:
        # The project row is real and the caller gets its 201: provisioning is
        # idempotent, so a later bootstrap or the next creation attempt repairs
        # it. Failing the request here would leave a committed project the
        # caller was told did not exist.
        logger.warning(
            "project created but not provisioned", extra={"project_slug": body.project_slug}
        )
    db.commit()
```

Match the module's existing logger and error-handling idiom rather than importing a new one.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_bootstrap.py -v
```
Expected: PASS, including Task 1's test and every pre-existing bootstrap test.

**Step 5: Commit**

```bash
git add src/memory/bootstrap.py src/memory/api/projects.py tests/test_bootstrap.py
git commit -m "fix(projects): provision the bank when a project is created"
```

---

## Task 3: Same for the user bank

A user bank is materialised by `banks.resolve_user_bank` (`src/memory/banks.py:14`), which reads `User.bank_id` off a row created with the user. `bootstrap` is again the only thing that provisions it.

**Files:**
- Modify: `src/memory/bootstrap.py`, and whichever module creates the `User` row (find it: `grep -rn "bank_id=ids.new_user_bank_id\|new_user_bank_id" src/memory/`)
- Test: `tests/test_bootstrap.py`

**Step 1: Write the failing test**

```python
@respx.mock
def test_a_new_user_gets_user_context_without_a_bootstrap_call(client, session, master_headers):
    """Same rule as projects: a bank is provisioned when it is created."""
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    registration = model_registry.get_registered_model(
        session, _user_bank_ref(session, user_id), "user-context"
    )
    assert registration is not None
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_bootstrap.py -k user_context_without_a_bootstrap -v
```

**Step 3: Implement**

A `provision_user_bank` sibling of Task 2's helper, called from user creation, same fail-soft posture.

**If user creation has no access to a Hindsight client** — check before writing code; the user routes may be pure control-plane — do **not** thread one in just for this. Instead leave user provisioning to `bootstrap` and record why in a comment on the helper. Report that outcome rather than forcing the symmetry; a project is created by many paths, a user by one.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_bootstrap.py -v
```

**Step 5: Commit**

```bash
git add -A
git commit -m "fix(users): provision the user bank when the user is created"
```

---

## Task 4: Retire the dead `create=True` default

`banks.resolve_project_bank` defaults `create: bool = True` and its docstring claims *"the first-touch creation SPEC §16.2 blesses for retain/recall/reflect, which keep the default."*

Both halves are now false. Typed retain went existing-only in v0.4.0 (`retention.py:49`: *"Existing-only resolution (create=False): never mints an unknown…"*), reads go through `read_context` (`create=False`), and `bootstrap.py:85` is the only `create=True` call site left in the service. So the default is unreachable — and one careless new caller away from silent slug squatting.

**Files:**
- Modify: `src/memory/banks.py:49-79`
- Test: `tests/test_banks.py` — confirm the filename first

**Step 1: Write the failing test**

```python
def test_project_bank_resolution_does_not_create_by_default(db, principal):
    """The only caller that may create a project says so explicitly
    (bootstrap). A default that creates is one careless caller away from
    letting any authenticated principal squat an arbitrary slug."""
    with pytest.raises(ProjectNotFound):
        banks.resolve_project_bank(db, principal, "never/seen")
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_banks.py -k does_not_create_by_default -v
```
Expected: FAIL — a project is created and the call succeeds.

**Step 3: Implement**

Flip the default to `create: bool = False` and rewrite the docstring to state the truth: creation is explicit and belongs to bootstrap; every read, write and curation path resolves existing-only. Then check every caller — `grep -rn "resolve_project_bank" src/` — and make sure none was relying on the old default. None should be; if one is, that is a bug this task just found, so report it rather than passing `create=True` to preserve it.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_banks.py tests/test_bootstrap.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/banks.py tests/test_banks.py
git commit -m "refactor(banks): stop defaulting project resolution to create"
```

---

## Task 5: Full sweep

**Step 1: Reframe bootstrap in the docs**

`POST /v1/bootstrap` is now a pre-warm, not a prerequisite. Say so where it is described — `SPEC-v1.md` §7.5, `README.md`, and `src/memory/api/bootstrap.py`'s module docstring. `src/memory/mcp/proxy.py:83-95` still calls it at startup and should keep doing so: pre-warming before the first prompt is exactly what it is for now.

**Step 2: Lint and full suite**

```bash
make lint && make test
```
Expected: clean, all pass. Expect work in `tests/test_projects*.py` and any test that created a project and asserted no mental model existed.

**Step 3: Commit**

```bash
git add -A
git commit -m "docs: bootstrap is a pre-warm, not a prerequisite"
```

---

## Out of scope

- **Lazy creation on the read path.** Reads keep `create=False`. Auto-vivifying from a model-supplied `project_slug` would let a hallucinated slug create a project, contradicts `load_context`'s advertised `readOnlyHint=True` and its "creates no project, bank, model or claim" description, and would put a `list_mental_models` round trip plus a refresh trigger inside its 2s deadline.
- **Removing `POST /v1/bootstrap`.** It stays: the proxy pre-warms with it, and ach-agent's boot path uses it so an agent's first event is not cold.
- **Backfilling existing half-provisioned projects.** If any exist in a deployment, one `POST /v1/bootstrap` per slug repairs them — `reconcile_builtin` is idempotent. Worth a one-off script only if the deployment turns out to have many.
- **Moving `load_context` to MCP.** It already is an MCP tool (`mcp/context_tools.py`); only the SessionStart hook uses the REST route, deliberately — one POST instead of an `initialize` handshake plus `tools/call`, on a path that runs before the first prompt.
