# Brief Delivery (SPEC Phase 0 + Phase 1) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the memory the service already holds actually arrive at the start of a coding session, and clean the three known data defects out of the live banks first.

**Architecture:** Today `/v1/session-brief` composes two Hindsight mental models into one string, and that string reaches an agent through exactly one channel — the MCP server's `instructions` field, which the host truncates at 2048 characters. Measured: the composed brief is 6115 chars, 626 chars survive, and the project half is discarded every session. This plan splits delivery into two tiers compiled from one snapshot: a short **index tier** that rides MCP `instructions` under a per-host budget, and a **full tier** that the SessionStart hook fetches over HTTP with a bounded timeout and a last-good disk cache. Both tiers carry `brief_revision` and `memory_protocol` so a consumer can tell which of the two it holds is newer. Along the way three defects are fixed: a master-key 400 that kills the console Brief tab, a console Models tab that renders the prompt instead of the synthesis, and a GET that mints state.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 + Alembic (Postgres), FastMCP (stdio proxy), pytest + respx, vanilla JS console (`src/memory/static/dashboard.html`), bash SessionStart hook.

---

## Before you start

**Source of truth:** `SPEC-memory-quality.md` v1.4.1. At the time of writing it lives at `/tmp/ach-memory-SPEC-memory-quality-v1.4a.md`; once the adoption commit lands it is at the repo root. Section references below (§11, §15, §16, D-numbers) are to that document. The companion rationale is `/tmp/ach-memory-DESIGN-NOTES-memory-quality.md`.

**Approval state — read this before touching anything.** The SPEC header says *Draft — Phase 0–1 proposed for approval*. No phase is approved. This plan is the "what it would take" for Phase 0 and Phase 1 only; Phases 2–5 are deliberately out of scope because their inputs do not exist yet (Phase 2's budgets come from a 2.2 measurement, Phase 3 is gated on probe O6). **Do not execute any task until Juan Carlos approves it.** Task 4 in particular mutates live production memory.

**Two orderings differ from §11, on purpose:**

1. **Task 3 (SPEC 1.5, "reads never write") comes before Task 4 (SPEC 0.3, "delete the accidental `squall` model").** `brief.ensure_section` creates a mental model on a GET. Delete the `squall` model while that is still true and the next brief read for `squall` recreates it, spending another LLM generation. Fix the cause, then delete the artefact.
2. **SPEC 1.6 (project metadata) comes before SPEC 1.2 (tiers),** because the index tier's "Project orientation" line reads the metadata record. Building the compiler first would mean building it twice.

**Environment:**

```bash
cd /home/jcm/Projects/ach-memory
make testdb              # starts the test Postgres on :5434 (NOT the compose one on :5433)
uv sync
uv run pytest -q         # baseline: everything green before you start
uv run ruff check src tests
```

If the baseline is not green, stop and report — do not build on a red suite.

**Conventions in this codebase you must match:**

- Comments explain *why*, usually citing the incident that caused the rule. Match that density; do not add narration.
- `ScopedRequest` is `extra="forbid"`. A typoed field is a 422 at the boundary, never a silent None.
- Every caller of `_resolve_bank` must `db.commit()` afterwards or audit rows are silently dropped.
- Tests are named as sentences (`test_a_missing_model_is_created_and_yields_no_section_yet`), with a docstring saying why the rule exists.
- Never log, echo or persist the master key or any API key. Cache filenames are derived from the URL and git locator, never from a credential.
- **Addressing, settled in Task 1:** `GET /v1/session-brief` resolves `scope=user` header-first — `on_behalf_of or user_id` — because the console's Brief tab addresses a user by `On-Behalf-Of` and never by query param. It is the only route that does. Everything else, `/v1/admin/*` included, resolves from the query param or body and treats `On-Behalf-Of` as audit-only. Both forms work on the brief read, and a master key sending both gets the header's target. Do not "harmonise" the siblings in either direction — that is not this plan's scope.

---

## Task 1: Fix the master-key 400 on `/v1/session-brief` (SPEC 1.3)

**Why:** `session_brief` builds `ScopedRequest(scope="user")` with `user_id` left at `None`. `banks.resolve_user_bank` then raises `InvalidScope("master-key requests with scope=user must set user_id")` for every master-key call. The console's Brief tab always shows "Pick a user first", and no operator can read a brief. The endpoint already receives the target in the `On-Behalf-Of` header (`current_on_behalf_of`), it just never passes it to bank resolution — `_resolve_bank` uses `on_behalf_of` only for the rate limiter and the audit row.

**Files:**
- Modify: `src/memory/api/brief.py:51-54`
- Test: `tests/test_brief.py` — it has the `client` + `two_users` + `respx` machinery. (An earlier draft of this plan named `tests/test_app.py`; that file is 153 lines of route-surface pins and platform-auth tests, with no user seeding and no Hindsight stub. Corrected after Task 1 hit it.)

**Step 1: Write the failing test**

Add to `tests/test_brief.py`, pinning the respx mock to the named user's bank id read from the DB — the pattern at `tests/test_curation_api.py:343-354`. A loose `banks/[^/]+/...` regex proves only that *some* bank was read, which is not what the test's name claims:

```python
def test_a_master_key_reads_a_brief_for_the_user_it_names(client, master_headers):
    """The Brief tab was dead for every operator: scope=user was resolved with
    user_id=None, so a master key -- which has no identity of its own -- got
    InvalidScope on a route whose whole purpose is reading somebody else's
    memory on their behalf."""
    response = client.get(
        "/v1/session-brief",
        params={"scope": "user"},
        headers={**master_headers, "On-Behalf-Of": "u_juancarlos"},
    )

    assert response.status_code == 200
```

Seed `u_juancarlos` the way the neighbouring tests in `tests/test_app.py` seed a user; copy the closest existing fixture rather than inventing one.

**Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_app.py::test_a_master_key_reads_a_brief_for_the_user_it_names -v
```

Expected: FAIL, 400, body naming `must set user_id`.

**Step 3: Make it pass**

`src/memory/api/brief.py`, replace lines 51-54:

```python
    # `on_behalf_of` is the only identity a master key has here: the route is
    # read-on-behalf-of by construction (§16.5), and `_resolve_bank` uses the
    # header for the audit row, never for resolution. A user key never sees the
    # header (`current_on_behalf_of` blanks it) and its `?user_id` reaches
    # `resolve_user_bank`, where naming somebody else is a 403, not a silent
    # redirect.
    user_bank, _, _ = _resolve_bank(
        ScopedRequest(scope="user", user_id=on_behalf_of or scoped.user_id),
        db, principal, on_behalf_of, "brief.get",
        create=False,
    )
```

**Step 4: Run the test and the neighbours**

```bash
uv run pytest tests/test_app.py tests/test_brief.py tests/test_admin_ui.py -v
```

Expected: PASS. A user key must still be unable to name another user (that is `Forbidden` in `resolve_user_bank`, unchanged) — confirm the existing test covering it still passes.

**Step 5: Commit**

```bash
git add src/memory/api/brief.py tests/test_app.py
git commit -m "fix(brief): resolve scope=user from On-Behalf-Of for master keys"
```

---

## Task 2: Console Models tab renders content, not the prompt (SPEC 1.4)

**Why:** `dashboard.html:834` calls `/v1/mental-models?${qs}` without `detail=full`, so the API never returns `content`. The tab then falls back to rendering `model.source_query` — the *prompt*. What looked like "the summary isn't showing" was the console showing the question instead of the answer. `src/memory/brief.py:73` already gets this right (`detail="full"`), which is why the endpoint works and the console does not.

**Files:**
- Modify: `src/memory/static/dashboard.html` (around `:820` and `:834`)
- Test: `tests/test_admin_ui.py`

**Step 1: Write the failing test**

`tests/test_admin_ui.py` asserts against the served HTML. Add:

```python
def test_the_models_tab_asks_for_content(client):
    """Without detail=full the API returns no `content`, and the tab fell back
    to rendering `source_query` -- the prompt shown where the synthesis
    belongs. brief.py:73 already asks for it; the console did not."""
    page = client.get("/admin/").text  # match the path the other tests use

    assert "detail=full" in page
```

**Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_admin_ui.py::test_the_models_tab_asks_for_content -v
```

Expected: FAIL, `assert 'detail=full' in page`.

**Step 3: Make it pass**

In `src/memory/static/dashboard.html`, at the Models tab handler (`:834`):

```js
      // detail=full or the API returns no `content` and this tab renders the
      // prompt where the synthesis belongs.
      const qs = new URLSearchParams({ ...scopeParams(value), detail: "full" }).toString();
      const listed = await api(`/v1/mental-models?${qs}`);
```

Then, in the row renderer (`:820`), render the content and demote the query. Keep the query visible — it is what a refresh is answering — but under the content and marked as such:

```js
      ${model.content ? `<div class="mm-content">${esc(model.content)}</div>` : `<div class="empty">No content yet — never refreshed, or cleared.</div>`}
      ${model.source_query ? `<div class="mm-query">query: ${esc(model.source_query)}</div>` : ""}
```

Add a `.mm-content` rule beside the existing `.mm-query` rule; `white-space: pre-wrap` so the model's line breaks survive.

**Step 4: Run the test, then look at it**

```bash
uv run pytest tests/test_admin_ui.py -v
```

Expected: PASS. Then open the console against the live service and confirm the Models tab shows synthesis text for both banks. If it shows "No content yet" for a bank, that is a real finding, not a UI bug — record it.

**Step 5: Commit**

```bash
git add src/memory/static/dashboard.html tests/test_admin_ui.py
git commit -m "fix(console): request detail=full and render mental-model content"
```

---

## Task 3: Reads never write (SPEC 1.5)

**Why:** `brief.ensure_section` provisions a mental model when it does not find one, on the GET path. A single exploratory `GET /v1/session-brief?scope=project&project_slug=squall` during investigation created a model on the `squall` bank and spent an LLM generation. This is the same incident class as the `readOnlyHint` slug-squat measured at 80 projects in 5.1s: a read minting state. Split provisioning out of the read.

**Consequence you are accepting, state it in the code:** `_reconcile` also leaves the read path. Today a deploy that changes `USER_QUERY`, `PROJECT_QUERY` or `TRIGGER` silently repairs existing models on the next brief read — `brief.py`'s own docstring says that is why `_reconcile` exists. After this task, a changed constant reaches deployed models only when someone calls the new provision route. That is the price of "reads never write", and it is worth paying: self-healing on read is precisely the mechanism that spent a generation on the `squall` bank. But it makes provisioning an explicit post-deploy step for anyone changing those constants, so say so in the docstring rather than leaving the next person to find out from a stale model.

**Files:**
- Modify: `src/memory/brief.py` (split `ensure_section`)
- Modify: `src/memory/api/brief.py` (call the read-only half)
- Modify: `src/memory/api/admin.py` (add the explicit provisioning route)
- Test: `tests/test_brief.py`, `tests/test_admin_api.py`

**Step 1: Write the failing tests**

In `tests/test_brief.py`:

```python
def test_reading_a_bank_with_no_model_creates_nothing():
    """A GET that provisions is a read minting state -- the same class as the
    readOnlyHint slug-squat. One exploratory brief read against an unrelated
    project created a model there and spent a generation on it."""
    client = FakeClient(models=[])

    assert brief.get_section(client, "user_1", NOW) is None
    assert client.created == []
    assert client.updated == []


def test_provisioning_is_explicit_and_reconciles():
    """Creation moved out of the read path, not out of the system: the query
    and trigger are versioned in code and a deploy still has to reach models
    that already exist."""
    client = FakeClient(models=[])

    brief.provision_section(client, "user_1", brief.USER_QUERY)

    (bank_id, kwargs) = client.created[0]
    assert bank_id == "user_1"
    assert kwargs["source_query"] == brief.USER_QUERY
```

**Step 2: Run them and watch them fail**

```bash
uv run pytest tests/test_brief.py -v
```

Expected: FAIL, `module 'memory.brief' has no attribute 'get_section'`.

**Step 3: Split the function**

In `src/memory/brief.py`, replace `ensure_section` with two functions. `get_section` is the whole of the current body *after* the create branch, minus `_reconcile`; `provision_section` is the create branch plus `_reconcile`.

```python
def get_section(client, bank_id: str, now: datetime) -> Section | None:
    """The bank's digest, or None when there is nothing worth showing.

    Reads only. Provisioning used to live here, which meant one exploratory
    GET against an unrelated project minted a mental model there and spent a
    generation on it -- a read creating state, the same class as the
    readOnlyHint slug-squat (SPEC 1.5). Creation moved to
    `provision_section`, called from the admin route.
    """
    model = _find(client, bank_id)
    if model is None:
        return None

    content = (model.get("content") or "").strip()
    if not content or content == PLACEHOLDER:
        return None

    refreshed_at = model.get("last_refreshed_at")
    if model.get("is_stale") and _older_than(refreshed_at, now):
        return None

    # Served whole. A digest was hard-cut at 2000 characters here, which
    # measured live meant every section lost its last line mid-word.
    # `max_tokens` already bounds this upstream.
    return Section(text=content, refreshed_at=refreshed_at)


def provision_section(client, bank_id: str, source_query: str) -> str:
    """Create the bank's brief model, or bring an existing one back in line.

    Returns "created" or "reconciled" so the admin route can say which.
    """
    model = _find(client, bank_id)
    if model is None:
        client.create_mental_model(
            bank_id,
            name=BRIEF_MODEL_NAME,
            source_query=source_query,
            max_tokens=MAX_TOKENS,
            trigger=dict(TRIGGER),
        )
        return "created"

    _reconcile(client, bank_id, model, source_query)
    return "reconciled"
```

Keep `_find`, `_reconcile`, `_older_than` and `compose` exactly as they are. Delete `ensure_section`.

**Step 4: Repoint the endpoint**

In `src/memory/api/brief.py`, replace both `brief.ensure_section(...)` calls with `brief.get_section(client, <bank>, now)` and drop the now-unused `brief.USER_QUERY` / `brief.PROJECT_QUERY` arguments at those call sites (the queries are still used by `provision_section`).

**Step 5: Add the explicit provisioning route**

In `src/memory/api/admin.py`, beside the other master-key routes:

Copy the shape of `clear_memories` (`src/memory/api/admin.py:165`) exactly — same `require_master` guard, same `_admin_scope(scope, user_id, project_slug, body)` addressing, same `MemoryResponse(result=..., resolved_from=..., project_slug=...)` return, same `create=False`, same `Query(pattern=r"^[^\x00-\x1f\x7f]*$")` on `user_id`:

```python
@router.post("/brief/{scope}/provision", response_model=MemoryResponse)
def provision_brief_model(
    scope: Scope,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
    user_id: Annotated[str | None, Query(pattern=r"^[^\x00-\x1f\x7f]*$")] = None,
    project_slug: str | None = None,
    body: AdminScopeBody | None = None,
) -> MemoryResponse:
    """Create a bank's brief model, or reconcile the one it has.

    The read path no longer provisions (SPEC 1.5), so this is the moment a
    model comes into existence and the moment a changed source query reaches
    one that already exists. Master key only, for the same reason `clear` is:
    the first refresh spends an LLM generation, which is not a decision a
    read -- or an agent -- gets to make.
    """
```

`create=False`, for `clear_memories`' reason: an admin must not conjure a bank into existence by provisioning a brief for one that never existed. Commit after the upstream call, as `clear_memories` does — an audit row saying a model was provisioned must not survive a 502 that meant it was not.

**Addressing:** master key names the target with `?user_id=`, exactly like every other `/v1/admin/*` route; `On-Behalf-Of` stays audit-only there. This is *not* the header-first form the read path uses — `GET /v1/session-brief` is header-first because the console's Brief tab addresses a user that way, and no console caller for provisioning exists. Do not harmonise them.

**Step 6: Run the suite**

```bash
uv run pytest tests/test_brief.py tests/test_app.py tests/test_admin_api.py -v
uv run ruff check src tests
```

Expected: PASS. One existing test — `test_a_missing_model_is_created_and_yields_no_section_yet` — asserts the old behaviour. Rewrite it against `provision_section`; do not delete the assertions about `max_tokens` and `trigger`, they pin values that reached production once by accident.

**Step 7: Commit**

```bash
git add src/memory/brief.py src/memory/api/brief.py src/memory/api/admin.py tests/
git commit -m "fix(brief): split provisioning out of the read path"
```

**Step 8: Deploy before Task 4.** Task 4 deletes a model this bug would recreate.

---

## Task 4: Phase 0 hygiene — by hand, against live data (SPEC 0.1–0.5)

**Why:** Every later phase reads these banks. Cleaning them costs no code and is reversible: `forget` is a soft invalidation with a `restore` counterpart, which is why §11 says "no export ceremony".

> **STOP.** This task mutates production memory. It runs only with Juan Carlos's explicit go-ahead, one sub-step at a time. Nothing here is a `clear` or a `DELETE` of memory content; the single hard delete is one mental model (0.3), which is regenerable.

**Files:** none. Endpoints only:

| Operation | Endpoint |
|---|---|
| list a bank's facts | `POST /v1/memory/list` |
| soft-invalidate | `POST /v1/memory/forget` |
| undo an invalidation | `POST /v1/memory/restore` |
| re-add a fact | `POST /v1/memory/retain` |
| delete a mental model | `DELETE /v1/mental-models/{id}` |
| force a refresh | `POST /v1/mental-models/{id}/refresh` |

**Step 0: Get the bank ids.** Console → Fleet tab, or `POST /v1/memory/list` per scope. The three known banks (SPEC §3.1): the user bank (95 facts), project `github.com-ackstorm-ach-memory-cd5e3a11` (75 facts, live, locator-resolved), project `ach-memory` (47 facts, the slug-only twin that locator resolution never reaches).

**Step 1 (0.1): Retire the split-brain twin.** Dump both project banks with `POST /v1/memory/list`. Diff them. For each fact unique to the twin, decide keep-or-drop *by hand* — this is the one moment a human reads all 47 — and `retain` the keepers into the live bank. Then `forget` the twin's rows wholesale. Record how many were unique and how many were kept; the ratio is evidence for whether split-brain twins matter at all.

**Step 2 (0.2): Soft-invalidate the colour/canary rows.** 24 rows in the user bank, 25% of it. `POST /v1/memory/forget` with their ids. Verify with a `list` that the user bank is 71 live facts.

**Step 3 (0.3): Delete the accidental `squall` brief model.** `DELETE /v1/mental-models/{id}` on the `squall` project bank (`ach-memory-session-brief`, created 2026-08-28T10:07 by the exploratory GET). Then re-issue that same GET and confirm — with Task 3 deployed — that **nothing is recreated**. That confirmation is the real test of Task 3.

**Step 4 (0.4): Lifecycle probe — this gates SPEC 3.3.** In the live project bank: `forget` the "Implement a disk cache" fact, `POST /v1/mental-models/{id}/refresh`, then read the model with `detail=full` and check whether the line derived from that fact is gone. Record the answer plainly, including "no change" if that is what happens. This is the only end-to-end test of `delta` refresh + invalidation available without writing code, and 3.3 (`fact_types: ["observation"]` on the trigger) depends on the result.

**Step 5 (0.5): `proof_count` audit.** For the three high-`proof_count` rows (24, 14, 7), pull their proofs and count **distinct sessions** and **distinct human utterances** behind each. If the proofs are mostly an agent re-retaining the same CLAUDE.md rule, `proof_count` is not an importance signal, and P12's distinct-source count must be computed by the harness for migrated facts (§17). Write the counts down.

**Step 6: Write the results up.** Append a short "Phase 0 results" section to the SPEC — five sub-steps, what was done, what was measured, and specifically the 0.4 and 0.5 answers. Do not print a number you cannot source.

**Step 7: Commit** (the SPEC edit only; no code changed).

```bash
git add SPEC-memory-quality.md
git commit -m "docs(spec): record Phase 0 hygiene results"
```

---

## Task 5: Project metadata record — data and API (SPEC 1.6, part 1)

**Why:** Orientation ("what is this project, where is its spec, what is it for") is *derivable*. Today it either costs a memory fact or is missing entirely — the independent audit found the project mental model had zero orientation. Deriving it means the memory budget spends only on what cannot be derived (P5).

**Files:**
- Modify: `src/memory/models.py` (`Project`, around `:100`)
- Create: `migrations/versions/a1b2c3d4e5f6_project_metadata.py`
- Modify: `src/memory/api/projects.py` (request/response models, PATCH handler)
- Test: `tests/test_projects_api.py`

**Step 1: Write the failing test**

```python
def test_project_metadata_round_trips(client, master_headers):
    """Orientation is derivable, so it must not cost a memory fact (P5). The
    project profile had zero orientation because nothing else could carry it."""
    client.post("/v1/projects", json={"project_slug": "acme-api"}, headers=master_headers)

    patched = client.patch(
        "/v1/projects/acme-api",
        json={
            "name": "Acme API",
            "canonical_spec": "docs/SPEC.md",
            "purpose": "Billing and entitlements for the Acme platform.",
        },
        headers=master_headers,
    )

    assert patched.status_code == 200
    assert patched.json()["purpose"].startswith("Billing")
    assert client.get("/v1/projects/acme-api", headers=master_headers).json()["name"] == "Acme API"
```

**Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_projects_api.py::test_project_metadata_round_trips -v
```

Expected: FAIL, 422 — `extra="forbid"` rejects `name`.

**Step 3: Add the columns**

`src/memory/models.py`, on `Project`:

```python
    # Orientation, not memory: name, spec pointer and one-line purpose are
    # derivable facts about a project, so they are a record here rather than
    # three Hindsight facts competing for a profile item budget (P5, SPEC 1.6).
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_spec: Mapped[str | None] = mapped_column(String(512), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(256), nullable=True)
```

`purpose` is capped at 256 because it is *one line* — a budget, not a text field.

**Step 4: Write the migration**

`migrations/versions/a1b2c3d4e5f6_project_metadata.py`:

```python
"""project metadata: name, canonical_spec, purpose

Revision ID: a1b2c3d4e5f6
Revises: b7d99d980665
"""

revision = "a1b2c3d4e5f6"
down_revision = "b7d99d980665"

def upgrade() -> None:
    op.add_column("projects", sa.Column("name", sa.String(length=128), nullable=True))
    op.add_column("projects", sa.Column("canonical_spec", sa.String(length=512), nullable=True))
    op.add_column("projects", sa.Column("purpose", sa.String(length=256), nullable=True))

def downgrade() -> None:
    op.drop_column("projects", "purpose")
    op.drop_column("projects", "canonical_spec")
    op.drop_column("projects", "name")
```

All three nullable: every existing project has none of them, and a NOT NULL with a default would invent orientation nobody wrote.

**Step 5: Extend the API models**

In `src/memory/api/projects.py`, add the three fields to `CreateProjectRequest` (all optional), to the PATCH request model, and to `ProjectResponse`. Reuse the existing `_no_control_characters` validator pattern for each — they reach the same INSERT and the same 500 that `git_locator` was hardened against.

At creation, seed `name` from the slug and `canonical_spec`/`purpose` from nothing. Do **not** guess a purpose from the slug; §16's "level of the claim = level of the evidence" applies to derived orientation too.

**Step 6: Run**

```bash
uv run alembic upgrade head
uv run pytest tests/test_projects_api.py tests/test_projects.py tests/test_unknown_fields.py -v
```

Expected: PASS. `test_unknown_fields.py` guards `extra="forbid"` — check it still rejects a typoed `nam`.

**Step 7: Commit**

```bash
git add src/memory/models.py migrations/versions/a1b2c3d4e5f6_project_metadata.py src/memory/api/projects.py tests/
git commit -m "feat(projects): name, canonical_spec and purpose as a metadata record"
```

---

## Task 6: Project metadata in the console (SPEC 1.6, part 2)

**Why:** The record is useless if nobody can set it, and the console currently has no `/v1/projects` surface at all (five tabs: Fleet, Activity, Peek, Models, Brief).

**Files:**
- Modify: `src/memory/static/dashboard.html`
- Test: `tests/test_admin_ui.py`

**Step 1: Write the failing test**

```python
def test_the_console_can_edit_project_metadata(client):
    """A metadata record nobody can set is a column, not a feature."""
    page = client.get("/admin/").text

    assert "/v1/projects/" in page
    assert 'data-panel="projects"' in page
```

**Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_admin_ui.py::test_the_console_can_edit_project_metadata -v
```

**Step 3: Add the tab**

Add a sixth tab button beside `:255` and a matching panel. The panel: a project picker (reuse `scopeParams`'s project selector), then three inputs — `name`, `canonical_spec`, `purpose` — populated from `GET /v1/projects/{slug}` and saved with `PATCH /v1/projects/{slug}`. Show a character counter on `purpose` against its 256 cap so the boundary is visible before the 422, not after.

Match the existing tab wiring exactly (`aria-selected`, `data-panel`, the same `api()` helper and `esc()` on every rendered value).

**Step 4: Run and look**

```bash
uv run pytest tests/test_admin_ui.py -v
```

Then set real metadata for `github.com-ackstorm-ach-memory-cd5e3a11`: name `ach-memory`, canonical spec `SPEC-v1.md`, purpose one line describing what the service is for. This is the first real orientation the project brief will carry.

**Step 5: Commit**

```bash
git add src/memory/static/dashboard.html tests/test_admin_ui.py
git commit -m "feat(console): edit project metadata"
```

---

## Task 7: `brief_revision`, `memory_protocol`, and the two-tier compiler (SPEC 1.2 / §15)

**Why:** Two channels deliver the brief, and they will disagree — the MCP proxy serves a cached index tier, so it is routinely one session behind the hook's full tier. §15's answer is not reconciliation logic in the agent: it is one monotonic revision per (user, project), stamped on both tiers, so a consumer reading "INDEX rev 42 / FULL rev 39" knows they diverge and which is authoritative (D21). `memory_protocol` makes a contract/brief mismatch visible so a cached brief cannot carry instructions the current contract has replaced.

**Scope note, state it in the code comments:** §15 describes the tiers in *items* ("at most 5 items"). Item-level structure arrives with Phase 4's `response_schema`. In Phase 1 the profiles are free text, so the compiler budgets at **line** level, dropping whole lines and never cutting mid-word — the failure `brief.py:137-144` already documents. Working State (item 3 of both tiers) is Phase 2; Phase 1 emits that section only if present, which today is never.

**Files:**
- Modify: `src/memory/models.py` (new `ContextRevision`)
- Create: `migrations/versions/b2c3d4e5f6a7_context_revisions.py`
- Create: `src/memory/revisions.py`
- Modify: `src/memory/brief.py` (tier composition)
- Modify: `src/memory/api/brief.py` (`tier`, `host`, `format` params; new response fields)
- Test: `tests/test_brief.py`, `tests/test_app.py`

**Step 1: Write the failing compiler tests**

In `tests/test_brief.py`:

```python
def test_the_index_tier_fits_the_host_budget_without_cutting_a_word():
    """Claude Code truncates MCP instructions at 2048 chars (measured). The
    brief was 6115 and 626 survived -- the project half was discarded every
    session. Dropping whole lines is the only honest way to fit: a half
    sentence arrives with nothing marking it incomplete."""
    text = brief.compose_index(
        revision=42,
        user=brief.Section("\n".join(f"user rule {i}" for i in range(40)), NOW.isoformat()),
        orientation=brief.Orientation("ach-memory", "SPEC-v1.md", "Memory for coding agents."),
        project=brief.Section("\n".join(f"project rule {i}" for i in range(40)), NOW.isoformat()),
        working_state=None,
        budget=1800,
    )

    assert len(text) <= 1800
    assert not text.endswith("...")
    assert brief.INDEX_SECTION.strip() in text  # the affordance list is reserved, never dropped
    assert "rev 42" in text


def test_both_tiers_carry_the_same_revision():
    """A consumer holding two tiers must be able to tell which is newer
    without reconciliation logic (D21)."""
    args = dict(revision=7, user=None, orientation=None, project=None, working_state=None)

    assert "rev 7" in brief.compose_index(**args, budget=1800)
    assert "rev 7" in brief.compose_full(**args)
```

**Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_brief.py -v
```

**Step 3: Add the revision store**

`src/memory/models.py`:

```python
class ContextRevision(Base):
    """One monotonic revision per (user, project) compiled-context snapshot.

    Bumped when any compiler input changes -- profile refresh, project
    metadata edit, and from Phase 2 a Working State write. Both tiers
    compiled from the same snapshot carry the same value; a cached tier keeps
    the value it was compiled at, which is exactly what makes "INDEX rev 42 /
    FULL rev 39" readable (D21).
    """

    __tablename__ = "context_revisions"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # "" rather than NULL: this is a primary key, and NULL never equals NULL.
    project_slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Migration `b2c3d4e5f6a7`, `down_revision = "a1b2c3d4e5f6"`, `op.create_table` with the composite PK.

`src/memory/revisions.py`:

```python
def fingerprint(*inputs: str | None) -> str:
    """A stable hash of every compiler input, so a bump means a real change.

    The harness never observes Hindsight's nightly refresh directly; it sees
    the refreshed_at that comes back with the model. Hashing the inputs is
    what makes "bumped whenever any compiler input changes" implementable
    without a webhook.
    """
    joined = "\x1f".join(value or "" for value in inputs)
    return hashlib.sha256(joined.encode()).hexdigest()


def current(db, tenant_id, user_id, project_slug, digest) -> int:
    """The revision for this snapshot, bumping it if the inputs moved.

    SELECT FOR UPDATE: two hosts starting a session at once would otherwise
    both bump, and the two tiers they cache would disagree by one forever.
    """
    row = db.execute(
        select(ContextRevision)
        .where(...)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        db.add(ContextRevision(..., revision=1, fingerprint=digest, updated_at=now))
        return 1
    if row.fingerprint != digest:
        row.revision += 1
        row.fingerprint = digest
        row.updated_at = now
    return row.revision
```

**Step 4: Write the compiler**

In `src/memory/brief.py`:

```python
MEMORY_PROTOCOL = 1

# Per host, resolved from the identity the hook or MCP client reports.
# Claude Code truncates at 2048 (measured, ours); 1800 leaves headroom for a
# host that counts differently than we do. An unknown host gets the smallest
# known budget rather than the benefit of the doubt.
HOST_BUDGETS = {"claude-code": 1800}
SMALLEST_BUDGET = 1800

# Reserved: it is never dropped to make room. An agent cannot call what it
# does not know exists -- and must not be told to call what it cannot (D18).
# `recall` is listed with its confirmation prompt because that prompt is the
# reason it goes unused; mental models are named without a call until Phase 5
# puts them on MCP.
INDEX_SECTION = (
    "-- What else memory holds --\n"
    "user profile: this user's standing working preferences.\n"
    "project profile: this project's conventions, constraints and gotchas.\n"
    "facts and observations: recall(scope, query) — your host will ask you to "
    "confirm the call.\n"
)


@dataclass(frozen=True)
class Orientation:
    name: str | None
    canonical_spec: str | None
    purpose: str | None
```

`compose_index(revision, user, orientation, project, working_state, budget)`:

1. Header: `f"-- ach-memory brief rev {revision} / protocol {MEMORY_PROTOCOL} --"`, plus the age line when the caller stamps one.
2. Reserve `len(header) + len(INDEX_SECTION)`.
3. Fill the remainder in §15 priority order — user core (at most 5 lines), project orientation from `Orientation` (deterministic, cheap), the first project profile line, Working State headline — appending a whole line only if it still fits.
4. Assemble header + filled sections + `INDEX_SECTION`.

`compose_full(revision, user, orientation, project, working_state)`: same header, then every section whole (per-section budgets are Phase 3.4's `max_items`; there is nothing to budget against yet), then `INDEX_SECTION` — the full tier is a strict superset so "newer tier only" never drops the affordance list (§15 item 4).

Keep the existing `compose()` until Task 8 and 9 have moved both callers, then delete it in Task 9.

**Step 5: Wire the endpoint**

`src/memory/api/brief.py`: add `tier: Literal["index", "full"] = "full"`, `host: str | None = None`, `format: Literal["json", "text"] = "json"` as `Query` params (they sit beside `scoped_query_params`, not inside `ScopedRequest`, which is `extra="forbid"` and shared by every data-plane route).

- Load the project row for the resolved slug and build `Orientation` from it.
- `digest = revisions.fingerprint(user_section.refreshed_at, project_section.refreshed_at, project_row.updated_at.isoformat())`.
- `revision = revisions.current(db, ...)`, then `db.commit()`.
- Compose per `tier`; budget = `HOST_BUDGETS.get(host, SMALLEST_BUDGET)`.
- `format=text` returns `PlainTextResponse(instructions)`. This exists so the SessionStart hook stays a `curl` and a `cat` — no `jq`, no `node`, no runtime that has to be installed before memory works. `format=json` keeps the existing `BriefResponse`, now with `brief_revision`, `memory_protocol` and `tier`.

Default `tier="full"` keeps the existing proxy caller working until Task 8 changes it.

**Step 6: Run**

```bash
uv run alembic upgrade head
uv run pytest tests/test_brief.py tests/test_app.py -v
uv run ruff check src tests
```

Add an endpoint test asserting `len(response.text) <= 1800` for `tier=index&host=claude-code&format=text`, and one asserting both tiers report the same `brief_revision` when fetched back to back.

**Step 7: Commit**

```bash
git add src/memory/models.py migrations/versions/b2c3d4e5f6a7_context_revisions.py src/memory/revisions.py src/memory/brief.py src/memory/api/brief.py tests/
git commit -m "feat(brief): two delivery tiers stamped with brief_revision and memory_protocol"
```

---

## Task 8: MCP index tier, shrunk static policy, proxy last-good cache (SPEC 1.2)

**Why:** Three things in one place because they are one change to one channel. (a) `cli.cmd_mcp:655` fetches the brief *before* `server.run()`, so a slow or down service delays every session start — the §4 startup-silence complaint. (b) The 1428-char static `INSTRUCTIONS` block occupies most of the 2048-char window with policy that never changes, crowding out memory that does. (c) With (a) fixed by a cache, the proxy is routinely one session behind, which is precisely why Task 7 stamped the revision.

**Files:**
- Modify: `src/memory/mcp/server.py` (`INSTRUCTIONS`)
- Modify: `src/memory/mcp/proxy.py` (cache + `startup_instructions`)
- Modify: `src/memory/cli.py:653-658`
- Modify: `plugins/*/activation.txt` (the policy that leaves MCP has to land somewhere — see Task 9)
- Test: `tests/test_mcp_proxy.py`, `tests/test_mcp_server.py`, `tests/test_cli.py`

**Step 1: Write the failing tests**

```python
def test_the_static_policy_leaves_room_for_memory():
    """1428 of the host's 2048 characters were static policy that never
    changes. Measured: 626 chars of a 6115-char brief survived, and the
    project half was discarded every session."""
    from memory.mcp.server import INSTRUCTIONS

    assert len(INSTRUCTIONS) <= 600


def test_the_proxy_serves_a_cached_index_without_waiting(tmp_path, monkeypatch):
    """Startup must not depend on the network. It fetched before
    server.run(), so a slow service delayed every session start."""
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    proxy.store_cached_index("https://memory.test", "acme-api", "INDEX rev 42")

    def _never_called(*args, **kwargs):
        raise AssertionError("startup must not block on a fetch when a cache exists")

    monkeypatch.setattr(proxy.httpx, "get", _never_called)

    assert "rev 42" in proxy.startup_instructions(
        "https://memory.test", "k", "acme-api", None, refresh=False
    )


def test_with_no_cache_and_no_service_the_proxy_still_starts(tmp_path, monkeypatch):
    """A broken memory service must cost a session its brief and nothing
    else."""
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(proxy, "fetch_brief", lambda *a, **k: None)

    text = proxy.startup_instructions("https://memory.test", "k", None, None, refresh=False)

    assert "unavailable" in text.lower()
```

**Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_mcp_proxy.py tests/test_mcp_server.py -v
```

**Step 3: Shrink `INSTRUCTIONS`**

Cut the 1428-char block to its load-bearing sentences — what this server is, and the one thing a client cannot discover from the tool list. Everything about *when to read and when to write* is the consumer contract (§16) and moves to host policy in Task 9 (D5).

```python
INSTRUCTIONS = (
    "Durable memory across sessions and context resets: the system of record "
    "for what was decided, preferred or learned. `scope` selects whose memory: "
    "'user' is your own, 'project' is the shared memory of the project named by "
    "project_slug. You never supply a bank id. Never store credentials, tokens "
    "or keys. Write in English whatever language the conversation uses: "
    "retrieval reranks in English only."
)
```

Keep the existing comment block above it explaining why `instructions` is the delivery that reaches every caller, and add one line saying the index tier now overrides this string in the proxy — a direct HTTP client, which no hook reaches, still gets this static text.

**Step 4: Add the cache to the proxy**

In `src/memory/mcp/proxy.py`:

```python
def _cache_path(base_url: str, slug: str | None, locator: str | None) -> Path:
    """One file per (service, project), under the OS user's own cache dir.

    Keyed on the URL and locator and never on the API key: a cache filename
    is world-readable metadata, and a key is not. The file itself is 0600 --
    it holds this user's memory.
    """
    root = Path(os.environ.get("ACH_MEMORY_CACHE_DIR") or
                Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ach-memory")
    digest = hashlib.sha256(f"{base_url}|{slug or ''}|{locator or ''}".encode()).hexdigest()[:16]
    return root / f"index-{digest}.txt"
```

`load_cached_index` / `store_cached_index` — both swallow every `OSError`: a cache that cannot be written must not cost a session its startup. Write to a temp file in the same directory and `os.replace`, so a killed process never leaves a half-written brief that the next session then serves.

`startup_instructions(base_url, api_key, slug, locator, *, refresh=True)`:

1. `cached = load_cached_index(...)`. If present: start a daemon thread that fetches `tier=index` and calls `store_cached_index` for *next* session, and return `cached` now.
2. If absent: one bounded `fetch_brief(..., tier="index")` at `BRIEF_TIMEOUT_SECONDS`. Store and return it.
3. If that fails too: return a stub that says so — memory is configured, the brief is unavailable, `recall` still works. An agent told nothing assumes nothing is there.

Extend `fetch_brief` with a `tier` parameter (default `"index"` for this caller) and pass `host` through where the caller knows it.

**Step 5: Repoint `cmd_mcp`**

`src/memory/cli.py:653-658`:

```python
    server = proxy.build_proxy(url, key)
    slug, locator = proxy.resolve_project_context()
    # Cache first, network in the background: this used to fetch before
    # run(), so a slow service delayed every session start (SPEC §4). The
    # cached tier is routinely one session behind, which is what
    # `brief_revision` is for (D21).
    server.instructions = proxy.startup_instructions(_base_url(base), key, slug, locator)
    server.run()
    return 0
```

**Step 6: Run**

```bash
uv run pytest tests/test_mcp_proxy.py tests/test_mcp_server.py tests/test_cli.py tests/test_agent_bundle.py -v
uv run ruff check src tests
```

`tests/test_cli.py` monkeypatches `memory.mcp.proxy.fetch_brief` in three places — repoint them at `startup_instructions` or keep them and let the new function call through; either is fine, but the assertion that startup does not raise when the service is down must survive.

**Step 7: Smoke it against the live service**

```bash
uv run python scripts/mcp-smoke.py
```

Confirm `initialize` returns the index tier and that its length is under 1800.

**Step 8: Commit**

```bash
git add src/memory/mcp/server.py src/memory/mcp/proxy.py src/memory/cli.py tests/
git commit -m "feat(mcp): serve the index tier from a last-good cache and shrink the static policy"
```

---

## Task 9: SessionStart hook delivers the full tier (SPEC 1.1) and carries the consumer contract (§16)

**Why:** This is the multiplier the whole plan exists for. The hook has no 2048-char cap — it is the only channel that can carry the project half at all. The current script is a deliberate `cat` with a comment saying a hook that talked to the service would make every session start depend on the network; D3 reverses that decision, and the timeout plus last-good cache is the price of reversing it. `activation.txt` becomes the §16 consumer contract — the static rules for reading a brief, which is exactly the policy that left MCP in Task 8.

**Files:**
- Modify: `plugins/claude-code/scripts/session-start.sh`
- Modify: `plugins/claude-code/activation.txt` (and the codex/opencode/pi copies)
- Modify: `plugins/claude-code/hooks/hooks.json` (timeout)
- Test: `tests/test_agent_bundle.py`

**Step 1: Write the failing test**

`tests/test_agent_bundle.py` already asserts things about the shipped plugin files (it is what measured that codex's SessionStart hook never runs). Add:

```python
def test_the_session_start_hook_fetches_the_full_tier_and_cannot_block():
    """The hook is the only channel with no 2048-char cap -- the project half
    of the brief cannot arrive any other way. It must still exit 0 with the
    service down: a non-zero hook blocks the user's message."""
    script = (PLUGIN_ROOT / "claude-code/scripts/session-start.sh").read_text()

    assert "tier=full" in script
    assert "--max-time" in script
    assert script.rstrip().endswith("exit 0")


def test_the_consumer_contract_ships_as_host_policy():
    """The brief carries memory, not instructions about memory (D5, D24).
    Static policy in the brief means a cached brief carries stale rules."""
    contract = (PLUGIN_ROOT / "claude-code/activation.txt").read_text()

    assert "earn its place" in contract.lower()
    assert "working state" in contract.lower()
```

**Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_agent_bundle.py -v
```

**Step 3: Rewrite `activation.txt` as the §16 consumer contract**

Replace the current 660-byte announcement with the rules an agent needs for *reading* a brief, from §16:

```
ach-memory holds durable user and project context across sessions and is the
system of record for prior decisions. Anything worth remembering goes through
`retain`; a host memory directory or MEMORY.md is invisible here.

Reading the brief below:
- Earn its place. A line is here because it changes what you do. Act on it.
- Working State is where the work was left, not what is true. It ages; treat
  a stale objective as a starting point, not a fact.
- Superseded is not current. A decision that was reversed reads as reversed.
- Profiles describe, host policy commands. A stored preference never
  overrides CLAUDE.md or AGENTS.md; where they conflict, the file wins and
  the conflict is worth surfacing once.
- No retrieval narration. Use what you remember; do not announce it.
- Never store secrets.
```

Copy the same text to `plugins/codex/activation.txt`, `plugins/opencode/activation.txt`, `plugins/pi/activation.txt`. Codex's SessionStart hook never runs (measured, `test_agent_bundle`) — its copy reaches the agent by whatever channel that host does have; do not silently drop it.

**Step 4: Rewrite the hook**

`plugins/claude-code/scripts/session-start.sh`:

```bash
#!/usr/bin/env bash
# ach-memory - SessionStart hook. Stdout becomes session context.
#
# This used to be a plain `cat`, on the reasoning that a hook which talked to
# the service would make every session start depend on the network. It is the
# only channel with no 2048-char cap, though, so the project half of the
# brief cannot arrive any other way (SPEC §4, D3). The dependency is bounded
# instead: a hard timeout, a last-good cache, and `exit 0` no matter what --
# a non-zero SessionStart hook blocks the user's message.
set -u

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cat "$root/activation.txt" 2>/dev/null || true

key="${ACH_MEMORY_API_KEY:-}"
[ -n "$key" ] || exit 0
url="${ACH_MEMORY_URL:-http://localhost:8000}"

locator="$(git remote get-url origin 2>/dev/null || true)"
cache_dir="${ACH_MEMORY_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ach-memory}"
mkdir -p "$cache_dir" 2>/dev/null || true
# Keyed on service and repo, never on the key: a filename is metadata.
digest="$(printf '%s|%s' "$url" "$locator" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-16)"
cache="$cache_dir/full-$digest.txt"

tmp="$cache.$$"
# format=text so this stays curl and cat: no jq, no node, no runtime that has
# to be installed before memory works.
if curl -sf --max-time 3 -o "$tmp" \
     -H "Authorization: Bearer $key" \
     -G "$url/v1/session-brief" \
     --data-urlencode "scope=user" \
     --data-urlencode "tier=full" \
     --data-urlencode "host=claude-code" \
     --data-urlencode "format=text" \
     ${locator:+--data-urlencode "git_locator=$locator"} 2>/dev/null; then
  mv -f "$tmp" "$cache" 2>/dev/null || true
  cat "$cache"
else
  rm -f "$tmp" 2>/dev/null || true
  if [ -s "$cache" ]; then
    printf '[ach-memory] service unreachable; cached brief from %s\n' \
      "$(date -r "$cache" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown)"
    cat "$cache"
  fi
fi

exit 0
```

Set `chmod 0600` on the cache file after `mv` — it holds the user's memory. Raise the `hooks.json` timeout from 5 to 6 so the 3s curl plus `git remote` has room, and the hook is still bounded well under it.

**Step 5: Run the tests, then run it for real**

```bash
uv run pytest tests/test_agent_bundle.py -v
ACH_MEMORY_API_KEY=<a real user key> ACH_MEMORY_URL=http://localhost:8000 \
  bash plugins/claude-code/scripts/session-start.sh | head -50
```

Then the case that matters most:

```bash
ACH_MEMORY_API_KEY=k ACH_MEMORY_URL=http://127.0.0.1:9 \
  bash plugins/claude-code/scripts/session-start.sh; echo "exit=$?"
```

Expected: the contract text, no brief, `exit=0`, in well under 6 seconds.

**Step 6: Delete the dead composer**

Both callers now use `compose_index`/`compose_full`. Remove `brief.compose` and the `INSTRUCTIONS` import in `src/memory/api/brief.py:23` if nothing else needs it. Remove only orphans this change created.

```bash
grep -rn "brief.compose\b\|_CAVEAT" src/ tests/
uv run pytest -q
uv run ruff check src tests
```

**Step 7: Commit**

```bash
git add plugins/ src/memory/brief.py src/memory/api/brief.py tests/
git commit -m "feat(hook): deliver the full brief tier with a bounded fetch and last-good cache"
```

---

## Done when

- A session on this machine starts with the project half of the brief present — the thing that has never once happened (measured: 0% of the project section delivered, every session).
- `GET /v1/session-brief` answers a master key.
- The console Models tab shows the synthesis; a Projects tab sets orientation.
- No read creates a mental model.
- With the service down, a session starts anyway, in under six seconds, with a dated cached brief or with none.

## Tracked, raised during execution, not yet done

- **`SPEC-v1.md:1378` contradicts the Task 3 provision route.** That line reads "Normal bank authorization applies to mental-model CRUD/refresh/clear", and `POST /v1/admin/brief/{scope}/provision` makes one mental-model create/update master-key-only. The route is deliberately absent from the list at `:1381` — that list is headed "Master/admin-only **destructive** operations" and provisioning is not destructive, so appending it there would be wrong. What needs editing is the normative sentence at `:1378`, which takes judgment rather than a mechanical addition. Nothing surfaces this drift automatically: `tests/test_app.py::EXPECTED_ROUTES` is the only route contract with teeth and it does not read the SPEC. Decide with Juan Carlos whether it lands in this branch or with the SPEC v1.4.1 adoption commit.

## Deliberately not in this plan

Phase 2 (Working State) and Phase 3 (write-side quality) — including the Stop/PreCompact background pass, `retain_mission`, `entity_labels` and profile item budgets. Phase 3 is gated on probe **O6**, which has not run. Phase 2's budgets come from the 2.2 measurement, which needs Phase 1 delivering first. The one Phase 1 seam left for them is deliberate: `revisions.fingerprint()` takes varargs so a Working State `updated_at` joins the hash without a migration.

## Execution handoff

Plan complete and saved to `docs/plans/2026-08-28-brief-delivery-phase-0-1.md`. Two execution options:

**1. Subagent-Driven (this session)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Parallel Session (separate)** — a new session with superpowers:executing-plans, batch execution with checkpoints.

Neither starts before Juan Carlos approves the phases.
