# ach-memory v0.4.0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the completed memory-quality research, establish the clean v0.4.0 implementation branch, freeze shared public contracts, migrate project slugs to one namespace, and create the database/code boundaries required by the two parallel implementation tracks.

**Architecture:** This is the sole prerequisite plan. It performs no production cleanup and does not implement retain or mental-model behavior; it establishes stable models, repositories and module seams so the typed-retain/lifecycle plan and the mental-model-governance plan can run concurrently without adding separate Alembic heads or editing the same MCP monolith.

**Tech Stack:** Python 3.12, FastAPI/Pydantic 2, SQLAlchemy 2, Alembic, FastMCP, pytest, PostgreSQL.

**Spec:** `docs/specs/2026-09-03-ach-memory-v0.4.0.md`

## Global Constraints

- Hindsight `0.9.2` is the validated activation target; this plan makes no Hindsight or production-memory mutation.
- Preserve the complete Phase 3-5.9 implementation on a named archive ref before forward removal from the product branch.
- `bank_id` remains internal and never appears in public responses, identifiers, logs or test artifacts.
- Reads never create projects, banks or mental models.
- Project canonical slugs and aliases occupy one tenant-global namespace.
- There must be exactly one Alembic head when this plan completes; the two parallel successor plans add no migrations.
- The custom mental-model quota is five per physical bank; the one ACH built-in is outside that quota.
- Planning decisions frozen here: `load_context(project_slug?, workspace_id?)`; custom models receive a server-generated immutable ACH `model_key`; built-in budgets are 512 tokens for `user-context` and 1,024 for `project-context`.
- The registry quota and delivery selection are distinct: each bank may register one built-in plus five custom models, but `always_in_context` may be enabled only while the sum of declared `max_tokens` fits that scope's budget. With Hindsight's 256-token minimum, at most nine models can be selected across User and Project when built-ins are disabled, or eight with both default built-ins enabled; the release latency gate exercises the maximum legal selected set rather than twelve.
- Use test-first changes and one focused commit per task. Do not activate production flags or execute the one-off cleanup in SPEC §13.

## Execution Topology

```text
this plan
   ├── typed retain, lifecycle and expiry plan
   └── mental-model governance and bootstrap plan
              both complete
                    └── context, skill and retirement plan
```

The two middle plans may use separate worktrees after this plan is merged. They rely on the exact types and tables created here and MUST NOT rename them independently.

---

### Task 1: Archive the research branch and seed the clean implementation branch

**Files:**
- Preserve from the reviewed research tip descended from `d053fe8`: `docs/specs/2026-09-03-ach-memory-v0.4.0.md`
- Preserve from the reviewed research tip descended from `d053fe8`: `docs/results/2026-09-03-memory-quality-phase-5-8-baseline.md`
- Preserve from the reviewed research tip descended from `d053fe8`: `docs/results/2026-09-03-memory-quality-phase-5-9-delivery.md`
- Preserve from the reviewed research tip descended from `d053fe8`: `docs/results/2026-09-02-memory-quality-phase-5-5.md`
- Preserve from the reviewed research tip descended from `d053fe8`: the four exact v0.4.0 plan files listed in Step 3
- Do not copy: `docs/plans/2026-09-03-ach-memory-v0.4.0.md`
- Do not copy: worktree-only `experiments/`, `tests/test_memory_quality_baseline.py`, `tests/test_memory_quality_consumer.py`, or `src/memory/capture/extractor.py` changes

**Interfaces:**
- Consumes: clean `main` at or after `4b74efa` and a reviewed `feat/memory-quality-phase-5-8` tip descended from `d053fe8` whose post-`d053fe8` diff contains only these four planning documents.
- Produces: archive ref `archive/memory-quality-v1.4-final` and implementation branch `feat/ach-memory-v0.4.0` containing only approved product documents from the research worktree.

- [ ] **Step 1: Verify both source trees are clean and pin their identities**

Run:

```bash
rtk git -C /home/jcm/Projects/ach-memory status --short --branch
rtk git -C /home/jcm/Projects/ach-memory/.worktrees/memory-quality-phase-5-8 status --short --branch
rtk git -C /home/jcm/Projects/ach-memory rev-parse main
rtk git -C /home/jcm/Projects/ach-memory rev-parse feat/memory-quality-phase-5-8
rtk git -C /home/jcm/Projects/ach-memory merge-base --is-ancestor d053fe8 feat/memory-quality-phase-5-8
rtk git -C /home/jcm/Projects/ach-memory diff --name-only d053fe8..feat/memory-quality-phase-5-8
rtk git -C /home/jcm/Projects/ach-memory tag --list archive/memory-quality-v1.4-final
```

Expected: both status outputs contain only their branch line; the ancestry command exits zero; the post-`d053fe8` diff names exactly the four v0.4.0 plan documents; the final tag lookup prints nothing. If it prints the tag, verify its target and stop instead of overwriting it.

- [ ] **Step 2: Create the immutable archive ref and isolated implementation worktree**

Run:

```bash
rtk git -C /home/jcm/Projects/ach-memory tag -a archive/memory-quality-v1.4-final feat/memory-quality-phase-5-8 -m "archive memory quality v1.4 research"
rtk git -C /home/jcm/Projects/ach-memory worktree add /home/jcm/Projects/ach-memory/.worktrees/ach-memory-v0.4.0 -b feat/ach-memory-v0.4.0 main
```

Expected: the tag resolves to the reviewed research tip identified in Step 1; the new worktree starts from the current `main` commit.

- [ ] **Step 3: Copy only approved documents from the archived tree**

Run from the new implementation worktree:

```bash
rtk git restore --source=archive/memory-quality-v1.4-final -- docs/specs/2026-09-03-ach-memory-v0.4.0.md docs/results/2026-09-02-memory-quality-phase-5-5.md docs/results/2026-09-03-memory-quality-phase-5-8-baseline.md docs/results/2026-09-03-memory-quality-phase-5-9-delivery.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-foundation.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-typed-retain-lifecycle.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-mental-model-governance.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-context-skill-retirement.md
```

Expected: no worktree-only experiment or automatic-capture implementation file changes.

- [ ] **Step 4: Verify the selective import**

Run:

```bash
rtk git status --short
rtk git diff --stat
rtk git diff --name-only
```

Expected: only the eight explicitly listed documentation files are new or modified.

- [ ] **Step 5: Commit the product reset point**

```bash
rtk git add docs/specs/2026-09-03-ach-memory-v0.4.0.md docs/results/2026-09-02-memory-quality-phase-5-5.md docs/results/2026-09-03-memory-quality-phase-5-8-baseline.md docs/results/2026-09-03-memory-quality-phase-5-9-delivery.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-foundation.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-typed-retain-lifecycle.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-mental-model-governance.md docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-context-skill-retirement.md
rtk git commit -m "docs: establish ach-memory v0.4.0 implementation"
```

### Task 2: Freeze shared v0.4.0 identifiers and request contracts

**Files:**
- Create: `src/memory/memory_types.py`
- Create: `src/memory/v040_contracts.py`
- Modify: `src/memory/ids.py`
- Modify: `src/memory/errors.py`
- Test: `tests/test_v040_contracts.py`
- Modify: `docs/specs/2026-09-03-ach-memory-v0.4.0.md`

**Interfaces:**
- Consumes: Pydantic `BaseModel`, existing `WorkspaceId`, existing `Scope` semantics.
- Produces: `MemoryType`, `EvidenceBasis`, `RetainTrigger`, `EvidenceKind`, `Lifecycle`, `ModelOrigin`, `DeliveryState`, `RetainEvidence`, `TypedRetainRequest`, `TypedRetainResponse`, `LoadContextRequest`, `new_model_key()`, and closed v0.4.0 domain errors.

- [ ] **Step 1: Write failing contract tests**

```python
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from memory.ids import new_model_key
from memory.v040_contracts import LoadContextRequest, RetainEvidence, TypedRetainRequest


def test_typed_retain_rejects_unknown_fields_and_requires_evidence():
    with pytest.raises(ValidationError):
        TypedRetainRequest(scope="project", project_slug="ach-memory", content="x",
                           memory_type="fact", basis="human_explicit",
                           trigger="agent_proactive", evidence=[], operation_id="bad",
                           privileged_tag="forbidden")


def test_typed_retain_accepts_future_expiry_with_offset(valid_uuid):
    body = TypedRetainRequest(
        scope="user", content="The user prefers concise status reports.",
        memory_type="preference", basis="human_explicit", trigger="user_requested",
        valid_until=datetime.now(UTC) + timedelta(days=1), operation_id=valid_uuid,
        evidence=[RetainEvidence(kind="user_quote", raw="Keep status reports concise.")],
    )
    assert body.valid_until is not None and body.valid_until.utcoffset() is not None


def test_load_context_requires_project_when_workspace_is_present(valid_workspace_id):
    with pytest.raises(ValidationError):
        LoadContextRequest(workspace_id=valid_workspace_id)


def test_model_key_is_public_stable_shape():
    assert new_model_key().startswith("mm_")
    assert len(new_model_key()) == 35
```

- [ ] **Step 2: Run the tests to verify the contracts do not exist**

Run: `rtk uv run pytest tests/test_v040_contracts.py -q`

Expected: collection fails because `memory.v040_contracts` and `new_model_key` do not exist.

- [ ] **Step 3: Add closed enums, exact models and identifiers**

```python
# src/memory/memory_types.py
from typing import Literal

MemoryType = Literal["preference", "constraint", "decision", "convention", "fact", "gotcha"]
EvidenceBasis = Literal["human_explicit", "agent_verified"]
RetainTrigger = Literal["user_requested", "agent_proactive"]
EvidenceKind = Literal["user_quote", "tool_result", "artifact_excerpt"]
Lifecycle = Literal["active", "expired", "forgotten"]
ModelOrigin = Literal["builtin", "user"]
DeliveryState = Literal["ready", "withheld"]
```

```python
# src/memory/v040_contracts.py
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memory.contracts import WorkspaceId
from memory.memory_types import EvidenceBasis, EvidenceKind, MemoryType, RetainTrigger


class RetainEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: EvidenceKind
    raw: str = Field(min_length=1, max_length=1024)
    source_ref: str | None = Field(default=None, max_length=512)


class TypedRetainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"]
    user_id: str | None = None
    project_slug: str | None = Field(default=None, max_length=128)
    content: str = Field(min_length=1)
    memory_type: MemoryType
    basis: EvidenceBasis
    trigger: RetainTrigger
    valid_until: datetime | None = None
    evidence: tuple[RetainEvidence, ...] = Field(min_length=1, max_length=4)
    operation_id: UUID

    @model_validator(mode="after")
    def validate_scope_and_time(self):
        if self.scope == "project" and not self.project_slug:
            raise ValueError("project_slug is required for project scope")
        if self.scope == "user" and self.project_slug is not None:
            raise ValueError("project_slug is forbidden for user scope")
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("valid_until must include an RFC 3339 offset")
        return self


class TypedRetainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    operation_id: UUID
    document_id: str
    status: Literal["pending", "accepted", "completed", "failed"]
    recorded_at: datetime
    valid_until: datetime | None


class LoadContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = Field(default=None, max_length=128)
    workspace_id: WorkspaceId | None = None

    @model_validator(mode="after")
    def workspace_requires_project(self):
        if self.workspace_id is not None and self.project_slug is None:
            raise ValueError("workspace_id requires project_slug")
        return self
```

Add `new_model_key() -> str` returning `mm_` plus 32 lowercase hex characters. Add stable errors for idempotency conflict, sanitizer rejection, model quota, context-budget overflow, bank currentness unavailable and curation-needs-operator; use the existing `DomainError` pattern and fixed HTTP statuses `409`, `422`, `409`, `409`, `503`, and `503` respectively.

- [ ] **Step 4: Pin the four planning decisions and resolve the delivery-count contradiction in the SPEC**

Amend the relevant sections so they state:

```text
load_context accepts optional project_slug and optional workspace_id; workspace_id is valid only with project_slug.
User-created models receive an immutable server-generated ACH model_key; name remains mutable.
Built-in max_tokens are 512 for user-context and 1,024 for project-context.
Each bank may register one built-in plus five custom models, but delivery selection must fit the independent User/Project budgets. Replace the claim that twelve models can be selected with the maximum legal selected-set rule: nine with Hindsight's 256-token minimum, eight in the default two-built-in configuration.
```

Run: `rtk rg -n "workspace_id|server-generated.*model_key|max_tokens=512|max_tokens=1024|maximum legal selected set|nine.*eight" docs/specs/2026-09-03-ach-memory-v0.4.0.md`

Expected: each decision is present in its owning contract section.

- [ ] **Step 5: Run and commit the closed contracts**

Run: `rtk uv run pytest tests/test_v040_contracts.py tests/test_errors.py tests/test_ids.py -q`

Expected: all selected tests pass.

```bash
rtk git add src/memory/memory_types.py src/memory/v040_contracts.py src/memory/ids.py src/memory/errors.py tests/test_v040_contracts.py docs/specs/2026-09-03-ach-memory-v0.4.0.md
rtk git commit -m "feat(memory): freeze v0.4.0 shared contracts"
```

### Task 3: Replace dual slug tables with one namespace

**Files:**
- Modify: `src/memory/models.py`
- Modify: `src/memory/projects.py`
- Modify: `src/memory/api/projects.py`
- Modify: `src/memory/read_context.py`
- Create: `migrations/versions/e5f6a7b8c9d0_v040_project_slug_namespace.py`
- Test: `tests/test_projects.py`
- Test: `tests/test_projects_api.py`
- Test: `tests/test_read_context.py`
- Create: `tests/test_project_slug_migration.py`

**Interfaces:**
- Consumes: existing `Project.internal_id`, tenant isolation and `normalize_slug()`.
- Produces: `ProjectSlug`, `projects.canonical_slug(db, project) -> str`, and `Resolution.current_slug`; all project creation, resolution and rename operations use `project_slugs` as the only uniqueness namespace.

- [ ] **Step 1: Add failing namespace and race tests**

```python
def test_alias_and_canonical_slug_share_one_namespace(db, principal, project):
    projects.rename(db, principal, project, "renamed")
    db.commit()
    with pytest.raises(ProjectSlugConflict):
        projects.create(db, principal, "ach-memory", "user", principal.user_id)


def test_rename_collision_rolls_back_canonical_change(db, principal, project, other_project):
    original = projects.canonical_slug(db, project)
    with pytest.raises(ProjectSlugConflict):
        projects.rename(db, principal, project, projects.canonical_slug(db, other_project))
    db.rollback()
    assert projects.resolve(db, principal, original, create=False).project.internal_id == project.internal_id
```

Add a migration test that upgrades a fixture containing live `projects.project_slug` and `retired_slugs`, asserts both become `project_slugs` rows, and asserts exactly one canonical row per project.

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `rtk uv run pytest tests/test_projects.py tests/test_projects_api.py tests/test_project_slug_migration.py -q`

Expected: failures because `ProjectSlug` and `canonical_slug` do not exist.

- [ ] **Step 3: Add the single namespace model and migration**

```python
class ProjectSlug(Base):
    __tablename__ = "project_slugs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_project_slugs_tenant_slug"),
        Index("uq_project_slugs_canonical_project", "tenant_id", "project_internal_id",
              unique=True, postgresql_where=text("is_canonical")),
    )

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_internal_id: Mapped[str] = mapped_column(ForeignKey("projects.internal_id"))
    is_canonical: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

The migration must create and populate `project_slugs` before dropping `retired_slugs` and `projects.project_slug`. Abort the upgrade on duplicate `(tenant_id, slug)` or projects lacking exactly one canonical slug; do not guess a winner.

- [ ] **Step 4: Rewrite resolution and rename as namespace transactions**

```python
@dataclass(frozen=True)
class Resolution:
    project: Project
    current_slug: str
    resolved_from: str | None


def canonical_slug(db: Session, project: Project) -> str:
    return db.scalar(select(ProjectSlug.slug).where(
        ProjectSlug.tenant_id == project.tenant_id,
        ProjectSlug.project_internal_id == project.internal_id,
        ProjectSlug.is_canonical.is_(True),
    )) or _raise_missing_canonical(project.internal_id)
```

Creation inserts `Project` and its canonical `ProjectSlug` in the same nested transaction. Rename locks the canonical row, marks it non-canonical and inserts the new canonical row before commit. An integrity error maps to `ProjectSlugConflict` and leaves the original row canonical.

- [ ] **Step 5: Run project isolation tests and commit**

Run: `rtk uv run pytest tests/test_projects.py tests/test_projects_api.py tests/test_read_context.py tests/test_project_slug_migration.py -q`

Expected: all selected tests pass, including concurrent create/rename races.

```bash
rtk git add src/memory/models.py src/memory/projects.py src/memory/api/projects.py src/memory/read_context.py migrations/versions/e5f6a7b8c9d0_v040_project_slug_namespace.py tests/test_projects.py tests/test_projects_api.py tests/test_read_context.py tests/test_project_slug_migration.py
rtk git commit -m "feat(projects): unify slug and alias namespace"
```

### Task 4: Create the v0.4.0 persistence schema and repositories

**Files:**
- Modify: `src/memory/models.py`
- Create: `src/memory/retained_records.py`
- Create: `src/memory/model_registry.py`
- Create: `src/memory/currentness.py`
- Create: `migrations/versions/f6a7b8c9d0e1_v040_memory_control_plane.py`
- Create: `tests/test_retained_records.py`
- Create: `tests/test_model_registry_repository.py`
- Create: `tests/test_currentness_repository.py`

**Interfaces:**
- Consumes: `TypedRetainRequest`, stable logical User/Project resolution and server-generated `model_key`.
- Produces: `RetainedRecord`, `CurationOperation`, `BankCurrentness`, `MentalModelRegistration`; repository functions `accept_retain`, `get_by_operation`, `register_model`, `list_registered_models`, `withhold_bank`, `ready_bank`, `bank_is_withheld`, `withhold_model`, and `ready_model`.

- [ ] **Step 1: Write failing repository tests**

```python
def test_accept_retain_is_idempotent_and_rejects_payload_conflict(
    db, principal, typed_request, bank_ref
):
    first, created = accept_retain(db, principal, typed_request, bank=bank_ref,
                                   canonical_content="claim",
                                   sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}])
    again, repeated = accept_retain(db, principal, typed_request, bank=bank_ref,
                                    canonical_content="claim",
                                    sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}])
    assert created is True and repeated is False and again.id == first.id
    with pytest.raises(IdempotencyConflict):
        accept_retain(db, principal, typed_request.model_copy(update={"content": "different"}),
                      bank=bank_ref,
                      canonical_content="different", sanitized_evidence=[{"kind": "user_quote", "raw": "quote"}])


def test_builtin_does_not_consume_five_custom_slots(db, bank_ref):
    register_model(db, bank_ref, origin="builtin", model_key="user-context", name="User context", **BUILTIN)
    for index in range(5):
        register_model(db, bank_ref, origin="user", model_key=f"mm_{index:032x}", name=f"m{index}", **CUSTOM)
    with pytest.raises(MentalModelQuotaExceeded):
        register_model(db, bank_ref, origin="user", model_key=f"mm_{99:032x}", name="overflow", **CUSTOM)
```

- [ ] **Step 2: Run repository tests to verify failure**

Run: `rtk uv run pytest tests/test_retained_records.py tests/test_model_registry_repository.py tests/test_currentness_repository.py -q`

Expected: collection fails because the repositories and ORM models do not exist.

- [ ] **Step 3: Add the linear migration and ORM models**

Create the following tables with database constraints, timestamps from `server_default=func.now()`, and no public serialization methods:

```text
retained_records:
  id, tenant_id, scope, user_id?, project_internal_id?, operation_id, payload_hash,
  document_id, source_memory_id?, canonical_content, memory_type, basis, trigger,
  sanitized_evidence JSON, recorded_at, valid_from, valid_until?, lifecycle,
  upstream_state, calling_agent?, created_by_credential?

curation_operations:
  operation_id, retained_record_id, bank_scope identity, action, desired_content?,
  state, repair_not_before?, last_attempt_at?, created_at, completed_at?

bank_currentness:
  tenant_id, scope, user_id?, project_internal_id?, state, blocking_operation_id?,
  repair_not_before?, updated_at

mental_model_registrations:
  model_key, tenant_id, scope, user_id?, project_internal_id?, upstream_model_id?,
  name, source_query, source_tags JSON, tags_match, max_tokens, trigger JSON,
  origin, builtin_key?, definition_version?, lifecycle_state, mutation_operation_id?,
  mutation_payload_hash?, always_in_context, delivery_state,
  refresh_operation_id?, refresh_status?, repair_not_before?, last_refreshed_at?,
  created_at, updated_at

working_sessions additions:
  completed_checkpoint_seq?, completed_at?
```

Each scoped table has a check requiring exactly one of `user_id` and `project_internal_id`. Add unique constraints for retain operation identity, retain document identity within a logical bank, model key, upstream model identity within a logical bank, and built-in key within a logical bank. Use row locking in quota and idempotency repository operations.

- [ ] **Step 4: Implement repositories with stable signatures**

```text
@dataclass(frozen=True)
class LogicalBankRef:
    tenant_id: str
    scope: Literal["user", "project"]
    user_id: str | None
    project_internal_id: str | None
    bank_id: str


accept_retain(db: Session, principal: Principal, request: TypedRetainRequest, *,
              bank: LogicalBankRef, canonical_content: str,
              sanitized_evidence: list[dict[str, str | None]])
    -> tuple[RetainedRecord, bool]
list_registered_models(db: Session, bank: LogicalBankRef)
    -> list[MentalModelRegistration]
withhold_bank(db: Session, bank: LogicalBankRef, operation_id: str)
    -> BankCurrentness
ready_bank(db: Session, bank: LogicalBankRef, operation_id: str)
    -> BankCurrentness
bank_is_withheld(db: Session, bank: LogicalBankRef) -> bool
```

`payload_hash` is SHA-256 over a versioned canonical JSON object containing scope identity, content, type, basis, trigger, expiry and sanitized evidence. It must not include wall-clock time or upstream-generated IDs.

- [ ] **Step 5: Verify schema, repositories and one Alembic head**

Run:

```bash
rtk uv run pytest tests/test_retained_records.py tests/test_model_registry_repository.py tests/test_currentness_repository.py tests/test_models.py tests/test_db.py -q
rtk uv run alembic heads
rtk uv run alembic upgrade head --sql
```

Expected: repository tests pass; `alembic heads` prints only `f6a7b8c9d0e1`; SQL generation succeeds.

- [ ] **Step 6: Commit the shared schema**

```bash
rtk git add src/memory/models.py src/memory/retained_records.py src/memory/model_registry.py src/memory/currentness.py migrations/versions/f6a7b8c9d0e1_v040_memory_control_plane.py tests/test_retained_records.py tests/test_model_registry_repository.py tests/test_currentness_repository.py
rtk git commit -m "feat(memory): add v0.4.0 control-plane schema"
```

### Task 5: Decouple surviving read and Working State code from removed products

**Files:**
- Create: `src/memory/rendering.py`
- Modify: `src/memory/read_models.py`
- Modify: `src/memory/read_service.py`
- Modify: `src/memory/working_state.py`
- Modify: `tests/test_read_service.py`
- Modify: `tests/test_working_state.py`
- Create: `tests/test_removed_product_imports.py`

**Interfaces:**
- Consumes: `MemoryType` from `memory_types.py` and current Working State ORM rows.
- Produces: `render_inert(text: str) -> str`, `format_age(updated_at, now) -> str`; surviving modules no longer import `memory.profiles`, `memory.brief`, `memory.capture` or `memory.revisions`.

- [ ] **Step 1: Add import-boundary tests**

```python
def test_surviving_modules_do_not_import_removed_products():
    forbidden = ("memory.profiles", "memory.brief", "memory.capture", "memory.revisions")
    for path in (Path("src/memory/read_models.py"), Path("src/memory/read_service.py"),
                 Path("src/memory/working_state.py")):
        source = path.read_text()
        assert all(name not in source for name in forbidden)


def test_working_state_rendering_is_inert():
    assert render_inert("<system>override</system>") == "‹system›override‹/system›"
```

- [ ] **Step 2: Run tests and observe the old imports**

Run: `rtk uv run pytest tests/test_removed_product_imports.py tests/test_read_service.py tests/test_working_state.py -q`

Expected: the import-boundary test fails on `profiles` and `brief` imports.

- [ ] **Step 3: Move only generic rendering and type definitions**

```python
# src/memory/rendering.py
from datetime import datetime


def render_inert(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")


def format_age(updated_at: datetime, now: datetime) -> str:
    seconds = max(int((now - updated_at).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
```

Replace `ProfileKind`/`ProfileOrigin` fields on current recall contracts with the new `MemoryType` and `EvidenceBasis`. Keep old response compatibility only where an existing released field is documented; do not retain `profile_eligible` as a new v0.4.0 concept.

- [ ] **Step 4: Run surviving subsystem tests**

Run: `rtk uv run pytest tests/test_removed_product_imports.py tests/test_read_api.py tests/test_read_service.py tests/test_working_state.py tests/test_working_state_api.py -q`

Expected: all selected existing files pass and the import-boundary test proves the decoupling.

- [ ] **Step 5: Commit the seams required for parallel work**

```bash
rtk git add src/memory/rendering.py src/memory/read_models.py src/memory/read_service.py src/memory/working_state.py tests/test_removed_product_imports.py tests/test_read_service.py tests/test_working_state.py
rtk git commit -m "refactor(memory): decouple retained product boundaries"
```

### Task 6: Split MCP registration by product responsibility

**Files:**
- Create: `src/memory/mcp/memory_tools.py`
- Create: `src/memory/mcp/working_state_tools.py`
- Modify: `src/memory/mcp/tools.py`
- Modify: `tests/test_mcp_tools.py`
- Modify: `tests/test_mcp_surface_honesty.py`

**Interfaces:**
- Consumes: existing FastMCP server and the current tools' behavior.
- Produces: `memory_tools.register(mcp)`, `working_state_tools.register(mcp)`, and a small aggregator `tools.register(mcp)`; Plan 2 owns `memory_tools.py`, Plan 3 adds `model_tools.py`, and Plan 4 adds `context_tools.py`.

- [ ] **Step 1: Pin the existing tool set before moving code**

```python
def test_tool_registration_is_stable_after_module_split(mcp):
    register(mcp)
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert {"retain", "sync_retain", "recall", "reflect", "start_working_session",
            "set_working_state"}.issubset(names)
```

- [ ] **Step 2: Run the surface test before refactoring**

Run: `rtk uv run pytest tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -q`

Expected: existing tests pass; record the exact tool count in the test rather than relying on memory.

- [ ] **Step 3: Move tools without changing schemas or annotations**

```python
# src/memory/mcp/tools.py
from memory.mcp import memory_tools, working_state_tools


def register(mcp):
    memory_tools.register(mcp)
    working_state_tools.register(mcp)
```

Move retain/recall/reflect/curation/document/operation helpers with their tools into `memory_tools.py`. Move the two Working State helpers and tools into `working_state_tools.py`. Keep shared response compaction and session/auth helpers in `tools.py` only if both modules use them; otherwise move them with their sole consumer.

- [ ] **Step 4: Verify byte-for-byte tool schemas where behavior has not changed**

Run: `rtk uv run pytest tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py tests/test_mcp_server.py -q`

Expected: all selected tests pass with the same pre-v0.4 tool schemas.

- [ ] **Step 5: Commit the conflict-reduction refactor**

```bash
rtk git add src/memory/mcp/tools.py src/memory/mcp/memory_tools.py src/memory/mcp/working_state_tools.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py
rtk git commit -m "refactor(mcp): split tools by memory responsibility"
```

### Task 7: Establish the parallel execution baseline

**Files:**
- Modify only if verification exposes a defect: files already owned by Tasks 2-6.

**Interfaces:**
- Consumes: all outputs from this plan.
- Produces: one green local baseline commit from which Plans 2 and 3 may create independent worktrees.

- [ ] **Step 1: Run formatting and static checks**

```bash
rtk uv run ruff check src tests
rtk git diff --check
```

Expected: both commands exit zero.

- [ ] **Step 2: Run the non-live full suite**

Run:

```bash
rtk uv run pytest -q --ignore=tests/test_integration_hindsight.py --ignore=tests/test_append_integration.py
```

Expected: zero failures and zero errors. Skips must be listed in the handoff.

- [ ] **Step 3: Verify migration shape from clean and upgraded databases**

Run the repository's PostgreSQL migration fixture twice: once from empty schema and once from the latest released `0.3.5` schema, then run:

```bash
rtk uv run alembic current
rtk uv run alembic heads
```

Expected: both reach `f6a7b8c9d0e1`; exactly one head is reported.

- [ ] **Step 4: Record the shared interface handoff**

Add the foundation commit SHA to the top of both successor plan execution notes. State explicitly that neither successor may create an Alembic revision or edit the closed enums and shared table names without stopping both tracks.

- [ ] **Step 5: Commit only if verification required corrections**

Run `rtk git status --short`. If the tree is clean, do not create an empty commit; the handoff is the verified HEAD SHA. If verification required a correction, return to the task that owns the failing file, stage only that task's exact file list, amend that task's commit, and repeat Steps 1-3. Do not create an unscoped gate-fix commit.

## Completion Gate

- The archive ref exists and the implementation branch contains only selected research documents.
- The three planning decisions are present in the SPEC and shared contracts.
- `project_slugs` is the sole active/alias namespace with transactional create/rename tests.
- All v0.4.0 ledger, currentness and mental-model tables exist under one Alembic head.
- Surviving recall and Working State code no longer imports products scheduled for removal.
- MCP tools are split so Plans 2 and 3 can edit disjoint modules.
- The non-live suite, Ruff and `git diff --check` pass.

After this gate, create two worktrees from the same verified HEAD and execute the typed-retain/lifecycle and mental-model-governance plans in parallel.
