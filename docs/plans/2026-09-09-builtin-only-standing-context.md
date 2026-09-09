# Built-in-Only Standing Context Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make "delivered in every context load" a property of being a built-in model, not a caller-settable flag — so no MCP or REST caller can ever put a custom mental model into standing context.

**Architecture:** Today `MentalModelRegistration.always_in_context` is a boolean any caller sets through `create_mental_model`/`update_mental_model`, and `load_context` selects on it. Two `BuiltinModelDefinition`s (`user-context`, `project-context`) set it `True` at 2048 tokens each, and `_check_budget` guards a 2560-token-per-scope ceiling at every create/update. We delete the column. `load_context` selects `origin == "builtin"` and `lifecycle_state == "active"` instead. Custom models keep their full create/list/get/update/refresh/delete lifecycle and are reached only by an explicit `get_mental_model`. The budget stops being a runtime API check and becomes an invariant over the two built-in definitions.

**Consequence, accepted deliberately:** standing delivery and authorship become the same property. Adding a third *built-in* standing model stays easy (add a `BuiltinModelDefinition`). Promoting a *custom* model to standing becomes impossible without re-adding a column — which is the rule we want enforced structurally.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, Pydantic v2, FastAPI, MCP (`mcp.server.mcpserver`), pytest, ruff, uv.

**Testing discipline for this plan:** every task names ONE test file. Run only that file. The full suite (`make test`) is FORBIDDEN until Task 7.

---

## Background reading (do this first, ~10 minutes)

- `src/memory/builtin_models.py` — the two built-in definitions. `key` is a `Literal["user-context", "project-context"]`; this is the stable internal name.
- `src/memory/context_service.py:133-260` — `ContextService.load`, the only reader of `always_in_context`.
- `src/memory/mental_model_service.py:43-49, 167-193` — the budget constants and `_check_budget`.
- `src/memory/model_registry.py:15, 54-120, 155-172` — quota, `register_model`, and the lifecycle transitions (`creating` → `active` → `deleted`; `disabled` is set out of band).
- `src/memory/api/mental_models.py:55-85` — the request contracts **shared by REST and MCP**. `src/memory/mcp/model_tools.py:25` imports them, so a field removed here disappears from both surfaces at once.

Lifecycle states seen in code: `creating` (set at registration), `active` (`model_registry.py:158`), `deleted` (`:170`), `disabled` (referenced by `reconcile_builtin`, never set by application code — an operator state).

---

## Task 1: `load_context` delivers built-ins only

The behaviour change, made first and on its own. The column still exists and is still written after this task; nothing selects on it any more.

Note the second half of the new predicate. Today's query filters `lifecycle_state != "deleted"`, so a built-in an operator set to `disabled` still carries `always_in_context=True` and is still delivered. Once the flag is gone, `active` is the only opt-out left, so the filter has to enforce it.

**Files:**
- Modify: `src/memory/context_service.py:170-182`
- Test: `tests/test_context_service.py`

**Step 1: Write the failing tests**

Read the existing fixtures in `tests/test_context_service.py` first and follow their construction style for banks and registrations — do not invent a new harness.

```python
def test_a_custom_model_is_never_delivered_even_with_the_flag_set(...):
    """Standing context is built-ins only. A custom model with
    always_in_context=True (still settable at this point in the refactor)
    must not appear in a context load."""
    # register one origin="builtin" model and one origin="user" model,
    # BOTH always_in_context=True, both lifecycle_state="active",
    # both delivery_state="ready"
    payload = ContextService(db, principal).load(LoadContextRequest())
    keys = {section.key for section in payload.sections}  # match the real attribute
    assert any("user-context" in key for key in keys)
    assert not any("custom" in key for key in keys)


def test_a_disabled_builtin_is_not_delivered(...):
    """`disabled` is the only opt-out left once the flag is gone."""
    # register the built-in with lifecycle_state="disabled",
    # always_in_context=True, delivery_state="ready"
    payload = ContextService(db, principal).load(LoadContextRequest())
    assert payload.sections == []
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_context_service.py -k "never_delivered or disabled_builtin" -v
```
Expected: both FAIL — the custom model is delivered, and the disabled built-in is delivered.

**Step 3: Change the predicate**

In `src/memory/context_service.py`, replace the `registration_scope` query block (lines 170-182). Current:

```python
        registration_scope = (
            ((MentalModelRegistration.scope == "user") & (MentalModelRegistration.user_id == self.principal.user_id))
            | ((MentalModelRegistration.scope == "project") & (MentalModelRegistration.project_internal_id == (project.internal_id if project else None)))
        )
        jobs = []
        if remaining() > 0:
            self._set_statement_timeout(remaining())
            rows = list(self.db.scalars(select(MentalModelRegistration).where(
                MentalModelRegistration.tenant_id == self.principal.tenant_id,
                registration_scope,
                MentalModelRegistration.lifecycle_state != "deleted",
                MentalModelRegistration.always_in_context.is_(True),
            )))
```

New — same shape, two predicates swapped:

```python
        registration_scope = (
            ((MentalModelRegistration.scope == "user") & (MentalModelRegistration.user_id == self.principal.user_id))
            | ((MentalModelRegistration.scope == "project") & (MentalModelRegistration.project_internal_id == (project.internal_id if project else None)))
        )
        jobs = []
        if remaining() > 0:
            self._set_statement_timeout(remaining())
            # Standing context is built-ins and nothing else: being a built-in
            # IS the delivery decision, so there is no flag to read. `active`
            # rather than `!= "deleted"` because an operator-disabled built-in
            # must stay out, and disabling is now the only way to opt one out.
            rows = list(self.db.scalars(select(MentalModelRegistration).where(
                MentalModelRegistration.tenant_id == self.principal.tenant_id,
                registration_scope,
                MentalModelRegistration.origin == "builtin",
                MentalModelRegistration.lifecycle_state == "active",
            )))
```

**Step 4: Run to verify they pass**

```bash
uv run pytest tests/test_context_service.py -v
```
Expected: PASS, whole file. Other tests in this file may assert on custom models being delivered — those assertions encoded the old rule and must be updated to the new one, not worked around.

**Step 5: Commit**

```bash
git add src/memory/context_service.py tests/test_context_service.py
git commit -m "feat(context): deliver built-in models only in standing context"
```

---

## Task 2: Remove `always_in_context` from the request contracts

`src/memory/api/mental_models.py` holds the contracts **both** REST and MCP use (`mcp/model_tools.py:25` imports them). One removal closes both surfaces.

**Files:**
- Modify: `src/memory/api/mental_models.py:61, 80, 165, 227`
- Modify: `src/memory/mcp/model_tools.py:95-155` (create), `:204-262` (update)
- Test: `tests/test_mcp_tools.py`

**Step 1: Write the failing tests**

```python
@respx.mock
def test_create_mental_model_rejects_always_in_context(call_tool):
    """Standing delivery is not a caller's choice: the argument is gone,
    and passing it is an unknown-field error, not a silent no-op."""
    key = call_tool.make_user()
    with pytest.raises(TypeError):
        call_tool(
            "create_mental_model", key, scope="user", name="Ops",
            source_query="?", source_tags=MM_REQUIRED_TAGS, tags_match="all",
            max_tokens=512, trigger={}, always_in_context=True,
        )


@pytest.mark.anyio
async def test_no_model_tool_advertises_always_in_context():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    for tool in await mcp.list_tools():
        assert "always_in_context" not in json.dumps(tool.input_schema), tool.name
        assert "always_in_context" not in (tool.description or ""), tool.name
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mcp_tools.py -k "rejects_always_in_context or advertises_always" -v
```
Expected: FAIL — the parameter is accepted and appears in both schema and description.

**Step 3: Remove the field**

In `src/memory/api/mental_models.py`:
- delete `always_in_context: bool` from `CreateMentalModelRequest` (line 61)
- delete `always_in_context: bool | None = None` from `UpdateMentalModelRequest` (line 80)
- delete the `always_in_context=body.always_in_context,` arguments at lines 165 and 227

In `src/memory/mcp/model_tools.py`:
- `create_mental_model` (line 104): delete the `always_in_context: bool,` parameter and the `always_in_context=always_in_context,` argument
- `update_mental_model` (line 224): delete the `always_in_context: bool | None = None,` parameter and its argument
- Rewrite both descriptions. Create (line 96) drops the whole `always_in_context is a conscious delivery choice…` sentence; update (line 205) drops `budget or always_in_context delivery choice` → `or budget` and the trailing `Enabling always_in_context on a User model exposes…` sentence.

`ScopedRequest` and the API models use `extra="forbid"` / `extra="allow"` inconsistently — check `CreateMentalModelRequest`'s effective config. If it inherits `extra="allow"`, an unknown `always_in_context` would be silently swallowed by REST. Make sure the REST models forbid extras so the first test's intent holds on both surfaces; if that is a wider change than this task, note it and raise it rather than widening silently.

**Step 4: Run to verify they pass**

```bash
uv run pytest tests/test_mcp_tools.py -v
```
Expected: `test_serialized_tool_contract_is_stable_after_module_split` FAILS on the SHA — that is correct and expected, the tool contract genuinely changed. Read the actual digest out of the assertion output and replace `TOOL_CONTRACT_SHA256` at `tests/test_mcp_tools.py:1358`, updating the comment above it to say what moved (`always_in_context left the create/update surface`). Re-run; the file must then be fully green.

**Step 5: Commit**

```bash
git add src/memory/api/mental_models.py src/memory/mcp/model_tools.py tests/test_mcp_tools.py
git commit -m "feat(models): drop always_in_context from the create/update surface"
```

---

## Task 3: Move the budget check onto the built-in definitions

`_check_budget` fires today from `create_custom_model` (`:342`) and `update_model` (`:478`). With customs never standing, neither can ever trip it. The check still has a job — stopping a future `BuiltinModelDefinition` version from raising `max_tokens` past the scope ceiling — so it moves rather than dies.

**Files:**
- Modify: `src/memory/mental_model_service.py:173-193, 341-342, 475-478`
- Test: `tests/test_builtin_models.py`

**Step 1: Write the failing test**

```python
import pytest

from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT
from memory.mental_model_service import (
    PROJECT_ALWAYS_IN_CONTEXT_BUDGET,
    USER_ALWAYS_IN_CONTEXT_BUDGET,
)


@pytest.mark.parametrize(
    ("definition", "limit"),
    [
        (USER_CONTEXT, USER_ALWAYS_IN_CONTEXT_BUDGET),
        (PROJECT_CONTEXT, PROJECT_ALWAYS_IN_CONTEXT_BUDGET),
    ],
)
def test_a_builtin_definition_fits_its_scope_delivery_budget(definition, limit):
    """Every built-in is standing by definition, so its max_tokens is spent
    on every context load. This is the only place the budget can now be
    exceeded -- by us, raising a definition's max_tokens, not by a caller."""
    assert definition.max_tokens <= limit
```

**Step 2: Run to verify it fails**

It will not fail on the current definitions (2048 ≤ 2560) — that is fine and expected; this is a guard, not a red test. Prove it bites instead:

```bash
uv run pytest tests/test_builtin_models.py -k fits_its_scope -v
```
Expected: PASS. Then temporarily edit `builtin_models.py` to `max_tokens=4096`, re-run, confirm FAIL, and revert. Do not commit the temporary edit.

**Step 3: Move the check**

- Delete the `if request.always_in_context:` / `_check_budget(...)` block at `mental_model_service.py:341-342`.
- Delete the `new_always` computation and `if new_always: _check_budget(...)` at `:475-478` (keep `new_tokens` if the surrounding code still uses it; delete it if it becomes orphaned — an orphan this change created is ours to remove).
- Call `_check_budget` from `_create_builtin` and `_upgrade_builtin` instead, passing `definition.max_tokens`. Both already hold the locked row set (`live` / the equivalent), so no extra locking.
- Update `_check_budget`'s message: `"enabling always_in_context would exceed…"` no longer describes anything a caller did. Make it `f"built-in {definition.key!r} would exceed the {bank.scope} delivery budget"` or similar.
- Keep `ContextBudgetExceeded` (`errors.py:257`). Its meaning narrows from a caller error to an internal invariant; say so in its docstring.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_builtin_models.py tests/test_mental_model_service.py -v
```
Expected: PASS. Tests asserting `ContextBudgetExceeded` from create/update encoded the old rule — delete them, do not weaken them.

**Step 5: Commit**

```bash
git add src/memory/mental_model_service.py src/memory/errors.py tests/test_builtin_models.py tests/test_mental_model_service.py
git commit -m "refactor(models): make the delivery budget a built-in definition invariant"
```

---

## Task 4: Remove the field from the service layer

**Files:**
- Modify: `src/memory/mental_model_service.py:108, 128, 151, 211, 239, 283, 360, 475-517, 592-597, 636, 680-681`
- Modify: `src/memory/model_registry.py:72, 118`
- Modify: `src/memory/builtin_models.py:18`
- Test: `tests/test_mental_model_service.py`

**Step 1: Write the failing test**

```python
def test_a_custom_model_registers_without_a_delivery_flag(db, bank):
    """Nothing in the custom-model path carries a delivery choice any more."""
    from memory.mental_model_service import CustomModelCreateRequest

    assert "always_in_context" not in CustomModelCreateRequest.model_fields
    assert "always_in_context" not in CustomModelUpdateRequest.model_fields
    assert "always_in_context" not in MentalModelView.model_fields
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_mental_model_service.py -k registers_without_a_delivery_flag -v
```
Expected: FAIL on the first assertion.

**Step 3: Remove it everywhere in the service layer**

- `CustomModelCreateRequest` (`:108`), `CustomModelUpdateRequest` (`:128`), `MentalModelView` (`:151`) — drop the field.
- `_payload_hash` (`:211` and `:239`) — drop the key from both digest payloads. **Note:** this changes the idempotency digest, so an in-flight retry carrying an old `mutation_payload_hash` will not match after deploy. Acceptable — retries are short-lived and a mismatch surfaces as `IdempotencyConflict`, not corruption. Record it in the commit body.
- `_to_view` (`:283`) — drop the argument.
- `create_custom_model` (`:360`) and `_create_builtin` (`:636`) — drop the `always_in_context=` argument to `register_model`.
- `update_model` (`:516-517`) — drop the assignment. Re-read the comment at `:493-497` about "a display-name-only or `always_in_context`-only update"; it must stop naming a field that no longer exists.
- `reconcile_builtin` docstring (`:595`) and `_upgrade_builtin` comment (`:680-681`) — both describe "preserving the user's `always_in_context` choice". Replace with the truth: an upgrade never re-enables an operator-disabled built-in, and `lifecycle_state` is what carries that now.
- `model_registry.register_model` (`:72`) — drop the `always_in_context: bool = False` parameter and its use at `:118`.
- `builtin_models.BuiltinModelDefinition` (`:18`) — drop `always_in_context: bool = True` and the `always_in_context=True` line from both `USER_CONTEXT` and `PROJECT_CONTEXT`.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_mental_model_service.py tests/test_model_registry_repository.py tests/test_builtin_models.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/mental_model_service.py src/memory/model_registry.py src/memory/builtin_models.py tests/
git commit -m "refactor(models): remove always_in_context from the service layer

The mutation payload hash no longer includes the field, so an in-flight
retry issued before this deploy will not match its recorded digest and
surfaces as IdempotencyConflict rather than resuming."
```

---

## Task 5: Drop the column

**Files:**
- Modify: `src/memory/models.py:623`
- Create: `migrations/versions/<rev>_drop_always_in_context.py`
- Test: `tests/test_bootstrap.py`

Current Alembic head is `c9d0e1f2a3b4` (verify with `uv run alembic heads` before writing the file — do not trust this number if the repo has moved).

**Step 1: Generate the migration**

```bash
uv run alembic revision -m "drop always_in_context"
```

Edit the generated file. `down_revision` must be the head you just verified.

```python
def upgrade() -> None:
    op.drop_column("mental_model_registrations", "always_in_context")


def downgrade() -> None:
    # Restored non-null with a server default so an existing row is valid:
    # under the old rule every built-in was standing and every custom was
    # not, which is exactly what this default cannot express -- a downgrade
    # therefore needs the follow-up UPDATE below, not just the column.
    op.add_column(
        "mental_model_registrations",
        sa.Column(
            "always_in_context",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        "UPDATE mental_model_registrations "
        "SET always_in_context = true WHERE origin = 'builtin'"
    )
    op.alter_column(
        "mental_model_registrations", "always_in_context", server_default=None
    )
```

Confirm the real table name against `src/memory/models.py` (`__tablename__` near line 623) before running anything.

**Step 2: Remove the mapped column**

`src/memory/models.py:623` — delete `always_in_context: Mapped[bool] = mapped_column(Boolean)`. Check whether `Boolean` is still imported for another column before removing the import.

**Step 3: Verify the migration round-trips**

```bash
make testdb
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: three clean runs, no error.

**Step 4: Run the tests**

```bash
uv run pytest tests/test_bootstrap.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/models.py migrations/versions/
git commit -m "feat(db): drop the always_in_context column"
```

---

## Task 6: `load_context` scope filter

Standing context is user + project today with no way to ask for one. An agent whose user bank is a bot's bank wants project only.

**Files:**
- Modify: `src/memory/v040_contracts.py:55-64` (`LoadContextRequest`)
- Modify: `src/memory/context_service.py:158-160`
- Modify: `src/memory/mcp/context_tools.py:16-27`
- Test: `tests/test_context_service.py`

**Step 1: Write the failing tests**

```python
def test_scope_project_omits_the_user_section(...):
    payload = ContextService(db, principal).load(
        LoadContextRequest(project_slug="acme/app", scope="project")
    )
    assert all(not section.key.startswith("0:") for section in payload.sections)


def test_scope_user_omits_the_project_section(...):
    payload = ContextService(db, principal).load(
        LoadContextRequest(project_slug="acme/app", scope="user")
    )
    assert all(not section.key.startswith("2:") for section in payload.sections)


def test_scope_defaults_to_both(...):
    """The default must not change what an existing caller receives."""
    assert LoadContextRequest().scope == "both"
```

Section keys are prefixed `0:` (user), `1:` (project metadata), `2:` (project model) — see `context_service.py:232`. Confirm the prefixes before relying on them.

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_context_service.py -k scope -v
```
Expected: FAIL — `LoadContextRequest` forbids extras, so `scope=` raises.

**Step 3: Implement**

`LoadContextRequest` gains `scope: Literal["user", "project", "both"] = "both"`. Keep `extra="forbid"` and the existing `workspace_requires_project` validator; add a second validator rejecting `scope="user"` together with a `project_slug`-only request only if that combination is genuinely meaningless — if it is merely redundant, allow it.

In `ContextService.load`, the `banks` list (`:158-160`) is the single choke point:

```python
        banks = []
        if request.scope in ("user", "both"):
            banks.append(("user", user_bank))
        if project_bank is not None and request.scope in ("project", "both"):
            banks.append(("project", project_bank))
```

Two things to keep consistent: the `no_project_resolved` omission at `:155` should not fire when the caller explicitly asked for `scope="user"` — it would report a gap the caller deliberately chose. And the project-metadata section at `:257` is gated on `project is not None`, not on `banks`, so it needs the same scope condition.

Add `scope` to the `load_context` MCP tool signature (`mcp/context_tools.py:18`) and pass it through.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_context_service.py -v
```
Expected: PASS.

The MCP tool schema changed again, so `TOOL_CONTRACT_SHA256` moves a second time:

```bash
uv run pytest tests/test_mcp_tools.py -k serialized_tool_contract -v
```
Read the new digest from the failure, update `tests/test_mcp_tools.py:1358` and its comment, re-run to green.

**Step 5: Commit**

```bash
git add src/memory/v040_contracts.py src/memory/context_service.py src/memory/mcp/context_tools.py tests/
git commit -m "feat(context): add an optional scope filter to load_context"
```

---

## Task 7: Full sweep

The only place in this plan where the whole suite runs.

**Step 1: Find every straggler**

```bash
grep -rn "always_in_context" src/ tests/ docs/ migrations/ README.md SPEC-v1.md
```
Expected: hits only in `migrations/versions/f6a7b8c9d0e1_v040_memory_control_plane.py` (historical, never edit a released migration) and the new drop migration. Anything in `src/`, `tests/`, `SPEC-v1.md` or `README.md` is unfinished work — SPEC §6.4/§7.3/§7.4 and §11 describe the old rule and need updating to "built-ins are standing by definition".

**Step 2: Lint**

```bash
make lint
```
Expected: clean.

**Step 3: Full suite**

```bash
make test
```
Expected: all pass. `tests/test_mcp_surface_honesty.py`, `tests/test_unknown_fields.py`, `tests/test_governance_ratelimit.py`, `tests/test_content_caps.py`, `tests/test_v040_mental_models_live.py` and `tests/test_v040_context_live.py` all reference the field and were not touched by the per-task files above — expect work here.

**Step 4: Commit**

```bash
git add -A
git commit -m "docs: describe standing context as built-in-only"
```

---

## Out of scope

- **ach-agent (Plan B).** The `ach-memory` memory backend, the facade tool allowlist, per-channel project injection, `POST /v1/bootstrap` + `load_context` per event, and deleting `MentalModelSpec` from the ACHAgent CRD. Independent of this plan — Plan B works against ach-memory as it stands today, and only wants Task 6's `scope` filter as a refinement.
- **The 512-token headroom.** Each scope now spends 2048 of 2560. Leave it. That headroom is what lets a `project-context` v3 grow without touching a constant.
- **Migrating the `gitlab-ackstorm` bank.** Explicitly declined.
