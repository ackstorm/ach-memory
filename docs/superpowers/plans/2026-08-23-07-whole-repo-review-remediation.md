# Whole-Repo Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding from the 2026-08-23 whole-repo review — 3 Critical, 35 Important, ~40 Minor — across the core domain, the REST surface, the MCP surface, the Hindsight client, the test suite, the operational scripts, and packaging.

**Architecture:** Five independent reviewers each took one slice of the repo. Their findings collapse into one recurring shape: **a fix that was reasoned about carefully in one place and never carried to its siblings.** `create()` got a savepoint and `rename()` did not; `errors.py` became the §18 registry except for the one class that escaped it; content was capped on `retain`/`correct` and not on the fields beside them; the substring bank-id redaction is pinned on one route out of twenty-eight. So this plan is organised by *root cause*, not by reviewer: each task closes a class at its choke point and adds the test that keeps the class closed, rather than patching the one instance the review happened to name.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic, Postgres 17, pydantic v2, `mcp` 2.0 SDK, httpx, pytest + respx, Docker/compose, Helm, GitHub Actions.

## Global Constraints

- **Every task ends green.** `uv run pytest -m "not integration"` must pass before every commit. Baseline at plan time: **504 passed, 2 deselected, ~60s**.
- **`bank_id` never crosses the API boundary** (SPEC inv. 29) — no response body, no error message, no log line. This is the invariant three separate leaks have already been closed against.
- **SPEC §18's error list is CLOSED.** Any new error code must be added to `SPEC-v1.md` §18's fenced `text` block in the same commit as the code that raises it, or `tests/test_errors.py` goes red.
- **Never `COPY . .`** in a Dockerfile. Explicit paths only.
- **No naked polling loops.** Every wait needs an upper bound *and* an explicit failure path (`for i in $(seq 1 N)` + a failure `exit 1`, or `timeout`, or `docker wait`).
- **Containers run non-root.** uid 10001, numeric `USER`, matching Helm `securityContext`. Already applied — Task 1 only commits it.
- Python floor is **3.12** (`pyproject.toml:5`); both Dockerfiles pin `python:3.12-slim`.
- Rate-limit defaults stay `write_limit=60`, `write_window_seconds=60.0`.
- Commit messages: short imperative subject under 72 chars, conventional-commit prefix (`fix(scope):`, `test(scope):`, `docs(scope):`, `build:`, `ci:`).

### Distribution constraint (decided 2026-08-23)

**The Git repository is PRIVATE on GitHub. The published artifacts — the Helm
chart and both container images — are PUBLIC.** Every task that touches
packaging must hold to that split:

- **Nothing secret may be baked into an image or a chart default.** No build
  args carrying credentials, no `.env` in the build context, no real hostnames
  or registry-internal paths in `values.yaml`.
- **`masterKeySecret.value` is a plaintext path in a chart strangers will
  install.** It stays supported (some operators genuinely template it from
  their own secret store) but the chart README must say `masterKeySecret.name`
  is the recommended path and `.value` is for local use.
- **A public image of this service publishes its source. ACCEPTED.**
  `Dockerfile` copies `src/`, `migrations/` and `alembic.ini` verbatim, and
  Python ships uncompiled — so anyone who can `docker pull` the service image
  can read the whole private repository's source with `docker save` and
  `tar -x`. There is no packaging trick that changes this; obfuscation and
  bytecode-only images are not meaningful protection. This was raised and
  **the repository owner accepted it on 2026-08-23** ("I know that source is
  inside container"). The repo stays private to control *write access and
  issue history*, not to hide the source. Do not re-litigate it in review; do
  not add obfuscation.
- **Public artifacts need provenance.** OCI labels naming source, revision and
  licence; images tagged by chart `appVersion`, never only `latest`; base
  images pinned by digest so a published tag is reproducible.

### Coordinates (decided 2026-08-23)

| | |
|---|---|
| Git repository | `github.com/ackstorm/ach-memory` — **private**, one flat `first commit` |
| Service image | `ghcr.io/ackstorm/ach-memory:<version>` — **public** |
| Helm chart | `oci://ghcr.io/ackstorm/charts/ach-memory:<version>` — **public** |

GHCR packages inherit the **repository's** visibility, so the first publish
from a private repo produces **private** packages. Flipping each to public is
a one-time manual step in the package settings after the first successful
release run — the pipeline cannot do it for you, and forgetting it is the most
likely way this ships "published" and unreachable.

## Reviewer finding IDs

Tasks cite the reviewer's own IDs so a finding can be traced back:
`R1` core/auth/migrations · `R2` REST · `R3` MCP+client · `R4` tests+scripts · `R5` packaging/ops/docs.

## File Structure

**New files**

| File | Responsibility |
|---|---|
| `src/memory/api/identifiers.py` | One guard for caller-supplied identifiers that reach Postgres or a URL. Today every module scrubs (or fails to scrub) its own. |
| `scripts/leakscan.py` | The single definition of "a bank id leaked". Imported by `e2e.py` and `mcp-smoke.py`, shelled out to by `smoke.sh`. Three divergent copies are the direct cause of R4-C2 and R4-I1. |
| `tests/test_bank_id_redaction.py` | One parametrised table proving the substring redaction on every response-bearing route. Replaces nine near-copies that would each have to be written by hand. |
| `migrations/versions/<rev>_index_api_keys_user_id.py` | Alembic revision for the `api_keys.user_id` index and the `audit_events.created_at` server default. |

**Modified files** — grouped by the class each change closes:

- *Parameter leak / unscreened bytes:* `src/memory/db.py`, `src/memory/api/{users,groups,admin,memory}.py`
- *Error-model closure:* `src/memory/errors.py`, `src/memory/api/users.py`, `SPEC-v1.md`, `tests/test_errors.py`
- *Validation siblings:* `src/memory/api/{memory,curation,documents,operations,directives,mental_models,projects,groups,users}.py`, `src/memory/provenance.py`
- *MCP parity:* `src/memory/mcp/tools.py`
- *Concurrency / domain:* `src/memory/{projects,ratelimit,banks,config}.py`, `src/memory/auth/principal.py`
- *Client robustness:* `src/memory/hindsight/{client,paths}.py`
- *Test coverage:* `tests/{test_governance_ratelimit,test_ratelimit,test_memory_api,test_curation_api}.py` and the seven files carrying dead respx stubs
- *Scripts:* `scripts/{smoke.sh,e2e.py,mcp-smoke.py}`
- *Packaging:* `Dockerfile`, `Dockerfile.hindsight`, `.dockerignore`, `docker-compose.yml`, `pyproject.toml`, `.github/workflows/ci.yml`, `deploy/helm/ach-memory/**`
- *Docs:* `README.md`, `docs/PROJECT-STATE.md`

---

## Phase 0 — Flatten the history, then start clean

### Task 1: Collapse 174 commits into one, and push to a private GitHub repo

Decided 2026-08-23: `github.com/ackstorm/ach-memory` starts as a **single "first
commit"**, repository **private**, artifacts **public** (MIT).

Flattening happens **now, before the rest of this plan** — so the 34 tasks that
follow become the repository's real, readable history on GitHub instead of
collapsing into one opaque blob with everything else.

One consequence has to be handled in the same move. `docs/PROJECT-STATE.md:26-27`
says, of the git-ignored task ledger:

> **After a context loss, trust that file and `git log` over recollection.**
> `git clean -fdx` would destroy it; recover from `git log` if that happens.

Squashing deletes the recovery path that sentence names. Most of the reasoning
in those 174 subjects also lives in code comments — this codebase is unusually
disciplined about that — but not all of it, and after the squash it is
unrecoverable. So the log ships **inside** the flat commit as a file.

**Files:**
- Create: `LICENSE` (MIT), `docs/HISTORY.md`
- Modify: `docs/PROJECT-STATE.md`
- Already in the working tree, folded into this commit: `Dockerfile`,
  `Dockerfile.hindsight`, `deploy/helm/ach-memory/**` (the non-root work)

**Interfaces:**
- Produces: one root commit; images running as uid 10001; chart values
  `podSecurityContext` and `securityContext` rendered into both pod templates
  (Task 9 depends on the chart still rendering).

- [ ] **Step 1: Confirm the working tree holds exactly the non-root change**

```bash
git status --short
git diff --stat
```
Expected: `?? .claude/`, `?? docs/superpowers/plans/2026-08-23-07-...md`, and
5 modified files, 48 insertions(+), 0 deletions(-) — `Dockerfile`,
`Dockerfile.hindsight`, and the three chart files. Nothing else.

- [ ] **Step 2: Verify both Dockerfiles use a numeric USER**

```bash
grep -n '^USER' Dockerfile Dockerfile.hindsight
```
Expected: `Dockerfile:USER 10001` and `Dockerfile.hindsight:USER 10001`. A
*named* USER makes the kubelet reject the pod under `runAsNonRoot: true` — it
cannot resolve a name to a uid without running the image.

- [ ] **Step 3: Write the MIT licence**

`LICENSE` at the repo root, the standard MIT text, `Copyright (c) 2026
Ackstorm`. Every artifact in Task 12 stamps this identifier, so it must exist
before the first release.

- [ ] **Step 4: Preserve the history that is about to be deleted**

```bash
{
  echo "# History before the flat first commit"
  echo
  echo "174 commits, squashed into one root commit on 2026-08-23 when this"
  echo "repository moved to github.com/ackstorm/ach-memory. Kept because"
  echo "PROJECT-STATE.md names \`git log\` as the recovery path for the"
  echo "git-ignored task ledger, and squashing removed it."
  echo
  git log --pretty='- %ad %s' --date=short --reverse
} > docs/HISTORY.md
wc -l docs/HISTORY.md
```
Expected: ~180 lines.

- [ ] **Step 5: Update PROJECT-STATE so it stops pointing at nothing**

In `docs/PROJECT-STATE.md`, replace the `git log` recovery sentence:

```markdown
The task-by-task ledger is `.superpowers/sdd/progress.md` (git-ignored scratch).
It records every task, its commit range, and every finding. **After a context
loss, trust that file over recollection.** `git clean -fdx` would destroy it.
History before 2026-08-23 is not in `git log` — it was squashed into the first
commit when this repository moved to GitHub; the 174 subject lines are kept in
`docs/HISTORY.md`, and the reasoning behind them is in the code comments.
```

- [ ] **Step 6: Make the flat commit**

```bash
git checkout --orphan flat
git add -A
git status --short | head -20        # read this before committing
git commit -m "first commit"
git branch -D main
git branch -m main
git log --oneline                    # exactly one line
```

`--orphan`, not `rebase --root`: it creates a genuinely parentless commit in
one step. The old commits survive in the local reflog until it expires, and
never reach the remote.

- [ ] **Step 7: Push to the private repository**

Create `github.com/ackstorm/ach-memory` as **private** first — pushing to a
public repo and flipping it afterwards leaves the content in forks, caches and
crawlers.

```bash
gh repo create ackstorm/ach-memory --private --source=. --remote=origin --push
git log --oneline origin/main        # one commit
```

- [ ] **Step 8: Verify nothing secret went up**

```bash
git ls-files | grep -E '^\.env|secret|\.pem$|id_rsa' || echo "no secret files tracked"
git grep -nI 'mem_local_master_change_me' -- . || echo "no published master key tracked"
```
Both must come back clean. `.env` is git-ignored and must stay untracked — if
it appears here, stop and remove it from the index before pushing.

### Task 1b (superseded)

The non-root work no longer needs its own commit; it is folded into the first
commit above. Skip to Phase 1.

<details>
<summary>Original Task 1, kept for reference</summary>

### Task 1 (original): Commit the non-root container work

Already applied to the working tree before this plan was written; it needs a commit of its own so the rest of the plan starts from a clean tree.

**Files:**
- Modify: `Dockerfile`, `Dockerfile.hindsight`
- Modify: `deploy/helm/ach-memory/values.yaml`, `deploy/helm/ach-memory/templates/deployment.yaml`, `deploy/helm/ach-memory/templates/migration-job.yaml`

**Interfaces:**
- Produces: images run as uid 10001; chart values `podSecurityContext` and `securityContext` exist and are rendered into both pod templates. Task 9 depends on the chart still rendering.

- [ ] **Step 1: Confirm the working tree holds exactly the non-root change**

```bash
git diff --stat
```
Expected: 5 files changed, 48 insertions(+), 0 deletions(-) — `Dockerfile`, `Dockerfile.hindsight`, and the three chart files. Nothing else.

- [ ] **Step 2: Verify both Dockerfiles use a numeric USER**

```bash
grep -n '^USER' Dockerfile Dockerfile.hindsight
```
Expected: `Dockerfile:USER 10001` and `Dockerfile.hindsight:USER 10001`. A *named* USER makes the kubelet reject the pod under `runAsNonRoot: true` — it cannot resolve a name to a uid without running the image.

- [ ] **Step 3: Run the suite**

Run: `uv run pytest -m "not integration" -q`
Expected: `504 passed, 2 deselected`

- [ ] **Step 4: Commit**

```bash
git add Dockerfile Dockerfile.hindsight deploy/helm/ach-memory
git commit -m "fix(deploy): run both images as uid 10001, not root"
```

</details>

---

## Phase 1 — Criticals

### Task 2: Stop bound SQL parameters reaching the logs

**R2-C1.** `create_engine` is built without `hide_parameters=True`. SQLAlchemy's `StatementError.__str__` appends `[SQL: ...]` **and `[parameters: {...}]`**, and `api/app.py:113` logs the full traceback — so `bank_id` and `internal_id` land in the application log, triggerable at will by an ordinary user key. The handler's own docstring promises "never echo the exception, which can carry SQL, a connection string, or a bank ID"; the traceback does exactly that through a different pipe. The `httpx` logger at `app.py:81` was already muted for this same reason.

**Files:**
- Modify: `src/memory/db.py:15`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `memory.db.get_engine()` returns an `Engine` whose `dialect` suppresses parameter rendering. Task 3 relies on this as defence in depth.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db.py`:

```python
def test_bound_parameters_never_reach_an_exception_string(monkeypatch):
    """A DataError's str() renders [parameters: {...}] unless the engine is
    built with hide_parameters=True -- and api/app.py logs the whole traceback
    of any unhandled exception. bank_id and internal_id are bound parameters
    on the projects INSERT, so without this the invariant "bank_id never
    crosses the boundary" (inv. 29) holds for responses and fails for logs.

    Reproduced live against the dev database before this test was written:
    hide_parameters=False -> the value appears in str(exc); True -> it does not.
    """
    from sqlalchemy import text

    from memory.db import get_engine

    engine = get_engine()
    assert engine.dialect.hide_parameters is True, (
        "create_engine must set hide_parameters=True"
    )

    # And prove it end to end, not just via the flag.
    secret = "project_00000000-0000-0000-0000-00000000dead"
    with engine.connect() as conn:
        try:
            conn.execute(text("select cast(:bank_id as int)"), {"bank_id": secret})
        except Exception as exc:  # noqa: BLE001 -- the string is the assertion
            assert secret not in str(exc)
        else:
            raise AssertionError("expected the cast to fail")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_db.py::test_bound_parameters_never_reach_an_exception_string -v`
Expected: FAIL — `AssertionError: create_engine must set hide_parameters=True`

- [ ] **Step 3: Set the kwarg**

`src/memory/db.py`, replace lines 13-15:

```python
@lru_cache
def get_engine() -> Engine:
    # hide_parameters=True: without it, any StatementError's str() carries
    # `[parameters: {...}]`, and api/app.py's catch-all logs the full
    # traceback of every unhandled exception -- so a DataError on the
    # projects INSERT prints bank_id and internal_id into the application
    # log. Reachable by an ordinary user key (a control character in
    # git_locator is enough), which makes it the same class as the httpx
    # logger muted in api/app.py, through a different pipe. The parameters
    # are not needed for diagnosis: the statement and the exception class
    # are still logged.
    return create_engine(
        get_settings().database_url, pool_pre_ping=True, hide_parameters=True
    )
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q` → `504 passed`

```bash
git add src/memory/db.py tests/test_db.py
git commit -m "fix(db): hide bound parameters, they carried bank_id into logs"
```

---

### Task 3: Screen caller-supplied identifiers before they reach Postgres

**R2-I2.** A NUL or control character in any caller-supplied identifier raises `psycopg.DataError` at parameter adaptation. SQLAlchemy wraps it as `sqlalchemy.exc.DataError`, which is **not** an `IntegrityError` — so the `except IntegrityError` guards in `users.py:81` and `groups.py:66` never see it and it walks to the catch-all as a 500. Verified live on 8 routes. `GET /v1/projects/{slug}` is the one that behaves, because `normalize_slug` scrubs it. `paths.py:53-54` already has exactly the right guard for document ids; it was never applied to our own identifiers.

**Files:**
- Create: `src/memory/api/identifiers.py`
- Modify: `src/memory/api/users.py` (4 sites), `src/memory/api/groups.py` (4 sites), `src/memory/api/admin.py` (3 sites), `src/memory/api/memory.py` (`ScopedRequest.user_id`)
- Test: `tests/test_identifiers.py` (new)

**Interfaces:**
- Produces: `memory.api.identifiers.reject_control_characters(value: str, not_found: type[DomainError]) -> None` — raises `not_found` for a value carrying a character below 0x20 or equal to 0x7F. Used by Tasks 3 only; no other task consumes it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_identifiers.py`:

```python
"""A control character in a caller-supplied identifier must be a typed 4xx.

psycopg raises DataError ("PostgreSQL text fields cannot contain NUL (0x00)
bytes") at parameter adaptation. SQLAlchemy wraps it as sqlalchemy.exc.
DataError, which is NOT an IntegrityError -- so users.py's and groups.py's
`except IntegrityError` never see it and it reaches api/app.py's catch-all as
a 500. Eight routes were verified live in the 2026-08-23 review; this table
pins all of them plus the two admin audit filters.
"""

import pytest

NUL = "a\x00b"


@pytest.mark.parametrize(
    "method,path,params,body,expected_code",
    [
        ("GET", f"/v1/users/{NUL}", None, None, "USER_NOT_FOUND"),
        ("GET", f"/v1/users/{NUL}/keys", None, None, "USER_NOT_FOUND"),
        ("POST", "/v1/users", None, {"id": NUL}, "USER_NOT_FOUND"),
        ("GET", f"/v1/groups/{NUL}", None, None, "GROUP_NOT_FOUND"),
        (
            "POST",
            f"/v1/admin/slugs/{NUL}/release",
            None,
            None,
            "RETIRED_SLUG_NOT_FOUND",
        ),
        # The two audit rows are FILTERS: they must answer 200 with an empty
        # list, not an error. Asserted separately below.
        ("GET", "/v1/admin/audit", {"actor_key_id": NUL}, None, None),
        ("GET", "/v1/admin/audit", {"on_behalf_of": NUL}, None, None),
    ],
)
def test_a_control_character_is_never_a_500(
    client, master_headers, tenant, method, path, params, body, expected_code
):
    response = client.request(
        method, path, params=params, json=body, headers=master_headers
    )
    assert response.status_code != 500, response.text
    if expected_code is not None:
        assert response.json()["error"]["code"] == expected_code, response.text


@pytest.mark.parametrize("param", ["actor_key_id", "on_behalf_of", "action"])
def test_an_unstorable_audit_filter_matches_nothing(
    client, master_headers, tenant, param
):
    """A filter is not a lookup. A value Postgres cannot store matches nothing,
    so 200 with an empty list is the correct answer -- raising some other
    route's not-found error here would be borrowing a contract that does not
    apply."""
    response = client.get(
        "/v1/admin/audit", params={param: NUL}, headers=master_headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_a_control_character_in_a_scoped_user_id_is_not_a_500(
    client, master_headers, tenant
):
    """The data plane's own identifier, reached through ScopedRequest."""
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": NUL, "query": "hi"},
        headers=master_headers,
    )
    assert response.status_code != 500, response.text
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_identifiers.py -v`
Expected: FAIL — several cases return `500`.

- [ ] **Step 3: Write the shared guard**

Create `src/memory/api/identifiers.py`:

```python
"""One guard for caller-supplied identifiers that reach Postgres.

`memory.hindsight.paths._reject_path_traversal` already does this for ids
that reach a Hindsight URL. This is the same defence for the ids that reach
our OWN database: user_id, group_id, retired_slug and the audit filters.

Why it has to exist at all: psycopg refuses a NUL byte at parameter
adaptation with `psycopg.DataError`, which SQLAlchemy wraps as
`sqlalchemy.exc.DataError`. That is NOT an `IntegrityError`, so the
`except IntegrityError` guards around every insert in this service never see
it, and it reaches `api/app.py`'s catch-all as a 500 -- a caller mistake
reported as a backend fault, which SPEC §18 exists to prevent. Measured live
on eight routes (2026-08-23 review, R2-I2).

Raises the route's own not-found error, never a 400: a malformed id cannot
name anything that exists, and answering "not found" discloses nothing about
why it was rejected -- the same reasoning `_reject_path_traversal` documents.
"""

from memory.errors import DomainError


def reject_control_characters(value: str | None, not_found: type[DomainError]) -> None:
    """Refuse an identifier Postgres cannot store.

    None and "" pass through untouched: absence is the caller's business and
    is handled by the route's own lookup, not by this guard.
    """
    if not value:
        return
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise not_found("no such object")
```

- [ ] **Step 4: Apply it at every identifier boundary**

`src/memory/api/users.py` — add the import beside the existing ones:

```python
from memory.api.identifiers import reject_control_characters
```

and guard the four sites. In `create_user`, immediately after `ensure_tenant(db, principal.tenant_id)`:

```python
    reject_control_characters(body.id, UserAlreadyExists)
```

In `get_user`, `create_key` and `list_keys`, as the first statement of each body (before `db.get(User, user_id)`):

```python
    reject_control_characters(user_id, UserNotFound)
```

In `revoke_key`, both identifiers, as the first two statements:

```python
    reject_control_characters(user_id, UserNotFound)
    reject_control_characters(key_id, KeyNotFound)
```

`src/memory/api/groups.py` — add the import, then guard `_load` (which every group route funnels through) as its first statement:

```python
def _load(db: Session, principal: Principal, group_id: str) -> Group:
    reject_control_characters(group_id, GroupNotFound)
    group = db.get(Group, group_id)
    if group is None or group.tenant_id != principal.tenant_id:
        raise GroupNotFound(group_id=group_id)
    return group
```

and in `create_group`, after `ensure_tenant(...)`:

```python
    reject_control_characters(body.id, GroupAlreadyExists)
```

and in `add_member` / `remove_member`, after the `_load(...)` call, before `db.get(User, user_id)`:

```python
    reject_control_characters(user_id, UserNotFound)
```

`src/memory/api/admin.py` — add the import, then in `release_slug` as the first statement:

```python
    reject_control_characters(retired_slug, RetiredSlugNotFound)
```

`list_audit`'s query parameters are **filters, not lookups** — and that
distinction decides the answer (resolved 2026-08-23):

- A **lookup** id names one object. Unstorable → it cannot exist → raise the
  route's own not-found. That is `get_user`, `get_group`, `release_slug`.
- A **filter** narrows a set. A value Postgres cannot store matches nothing,
  so the honest answer is an empty result — not an error, and certainly not
  an unrelated error class borrowed from another route.

So in `list_audit`, immediately before `stmt = select(AuditEvent)...`:

```python
    # Filters, not lookups: a value Postgres cannot store matches nothing, so
    # an empty result IS the correct answer. Unguarded, psycopg raises
    # DataError at parameter adaptation -- not an IntegrityError, so no
    # `except` in this service catches it and it reaches api/app.py's
    # catch-all as a 500, reporting a caller mistake as a backend fault. Same
    # call the project already made in `fix(errors): stop reporting caller
    # mistakes as backend faults`.
    if any(
        v and any(ord(c) < 0x20 or ord(c) == 0x7F for c in v)
        for v in (action, actor_key_id, on_behalf_of)
    ):
        return []
```

No new error code, no new exception class, no disclosure question — and
nothing added to SPEC §18.

`src/memory/api/memory.py` — `ScopedRequest.user_id` is the data plane's own identifier. Add a validator to the model rather than to each route:

```python
class ScopedRequest(BaseModel):
    ...
    scope: Scope
    user_id: str | None = None
    project_slug: str | None = None
    git_locator: str | None = Field(default=None, max_length=512)

    @field_validator("user_id")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        # Reaches `db.get(User, ...)` in banks.resolve_user_bank. A control
        # character there is a psycopg DataError -> 500; a typed 422 at the
        # boundary is the same treatment git_locator's max_length gets.
        if value and any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
            raise ValueError("user_id must not contain control characters")
        return value
```

and extend the import at the top of the file:

```python
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_identifiers.py -v`
Expected: PASS (8 cases)

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q` → `512 passed`

```bash
git add src/memory/api tests/test_identifiers.py
git commit -m "fix(api): screen control characters out of caller identifiers"
```

---

### Task 4: Pin the substring bank-id redaction on every route

**R4-C1.** `_strip_bank_id` does two things: drop a key literally named `bank_id`, and redact the bank id as a *substring* of any string value. The second exists only because a live `memories/list` embedded it inside `chunk_id` as `f"{bank_id}_{document_id}_{n}"`. Every caller must pass its own `bank_id` for that to work — and only **one** test in the whole suite constructs a substring-shaped response. Verified by mutation: dropping the second argument at 23 call sites across six routers left **504 passed**.

**Files:**
- Create: `tests/test_bank_id_redaction.py`
- Test only — no source change. The source is already correct; nothing was holding it correct.

**Interfaces:**
- Consumes: `conftest.py`'s `client`, `master_headers`, `tenant`, `session` fixtures.
- Produces: nothing other modules import.

- [ ] **Step 1: Find the bank id a request will actually resolve to**

Read `tests/test_curation_api.py` around line 528 for the existing single instance of this test, and copy its technique for reading the real `bank_id` off the `User` row — a placeholder string cannot exercise a substring match against the id the route actually resolved.

```bash
sed -n '515,560p' tests/test_curation_api.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_bank_id_redaction.py`:

```python
"""The substring half of _strip_bank_id, on every response-bearing route.

SPEC inv. 29 is absolute: bank_id never crosses the API boundary. A live
`memories/list` (hindsight-api 0.9.1, 2026-08-22) embedded it inside
`chunk_id` as f"{bank_id}_{document_id}_{n}" -- invisible to a key-only
filter, which is why `_strip_bank_id` also redacts it as a SUBSTRING and why
every call site must pass its own bank_id.

Before this file existed exactly ONE test constructed a substring-shaped
response, so 23 of the 28 call sites were unpinned: mutating
`_strip_bank_id(result, bank_id)` to `_strip_bank_id(result)` across
documents/operations/memory/admin/directives/mental_models left the whole
suite green (2026-08-23 review, R4-C1). Add a row here for every new route
that returns an upstream body.
"""

import httpx
import pytest
import respx

BASE = "http://hindsight.test"


@pytest.fixture
def juan(client, master_headers, tenant) -> dict:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def _bank_id(session, user_id: str) -> str:
    from memory.models import User

    return session.get(User, user_id).bank_id


# name -> (method, path, json body, query params)
ROUTES = {
    "retain": ("POST", "/v1/memory/retain", {"scope": "user", "content": "x"}, None),
    "recall": ("POST", "/v1/memory/recall", {"scope": "user", "query": "x"}, None),
    "reflect": ("POST", "/v1/memory/reflect", {"scope": "user", "query": "x"}, None),
    "list_memories": ("POST", "/v1/memory/list", {"scope": "user"}, None),
    "get_memory": (
        "POST", "/v1/memory/get",
        {"scope": "user", "memory_id": "11111111-1111-1111-1111-111111111111"}, None,
    ),
    "forget": (
        "POST", "/v1/memory/forget",
        {"scope": "user", "memory_id": "11111111-1111-1111-1111-111111111111"}, None,
    ),
    "restore": (
        "POST", "/v1/memory/restore",
        {"scope": "user", "memory_id": "11111111-1111-1111-1111-111111111111"}, None,
    ),
    "correct": (
        "POST", "/v1/memory/correct",
        {
            "scope": "user",
            "memory_id": "11111111-1111-1111-1111-111111111111",
            "content": "fixed",
        },
        None,
    ),
    "list_documents": ("POST", "/v1/memory/documents/list", {"scope": "user"}, None),
    "get_document": (
        "POST", "/v1/memory/documents/get", {"scope": "user", "document_id": "d1"}, None,
    ),
    "delete_document": (
        "POST", "/v1/memory/documents/delete",
        {"scope": "user", "document_id": "d1"}, None,
    ),
    "list_operations": ("POST", "/v1/memory/operations/list", {"scope": "user"}, None),
    "get_operation": (
        "POST", "/v1/memory/operations/get",
        {"scope": "user", "operation_id": "22222222-2222-2222-2222-222222222222"}, None,
    ),
    "cancel_operation": (
        "POST", "/v1/memory/operations/cancel",
        {"scope": "user", "operation_id": "22222222-2222-2222-2222-222222222222"}, None,
    ),
    "list_directives": ("GET", "/v1/directives", None, {"scope": "user"}),
    "list_mental_models": ("GET", "/v1/mental-models", None, {"scope": "user"}),
}


@respx.mock
@pytest.mark.parametrize("name", sorted(ROUTES))
def test_a_bank_id_embedded_in_an_upstream_string_is_redacted(
    client, juan, session, tenant, name
):
    method, path, body, params = ROUTES[name]
    bank_id = _bank_id(session, juan["user_id"])

    # Every upstream response carries the bank id INSIDE another field's
    # value, under a key no filter looks at -- the shape the live leak had.
    respx.route(url__regex=rf"^{BASE}/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "m1",
                        "chunk_id": f"{bank_id}_doc7_3",
                        "nested": {"trace": f"resolved via {bank_id} ok"},
                    }
                ]
            },
        )
    )

    response = client.request(
        method, path, json=body, params=params, headers=juan["headers"]
    )
    assert response.status_code < 400, (name, response.text)
    assert bank_id not in response.text, (
        f"{name}: bank_id survived in the response body"
    )
    assert "REDACTED" in response.text, (
        f"{name}: nothing was redacted -- the call site is not passing bank_id"
    )
```

- [ ] **Step 3: Prove the test would catch the mutation**

`src/` is tracked and clean at this point, so `git checkout` is the restore —
never `rm -rf src` followed by a `mv`, which loses the tree outright if the
move fails.

```bash
git status --short src/          # MUST be empty before mutating
sed -i 's/_strip_bank_id(result, bank_id)/_strip_bank_id(result)/' \
  src/memory/api/{documents,operations,memory,admin,directives,mental_models,curation}.py
uv run pytest tests/test_bank_id_redaction.py -q; echo "exit=$?"
git checkout -- src/
git status --short src/          # MUST be empty again
uv run pytest tests/test_bank_id_redaction.py -q
```
Expected: FAIL on every parametrised case while mutated, then PASS after the
restore. A test that cannot fail is the thing this task exists to prevent — do
not skip this step.

- [ ] **Step 4: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q` → `528 passed`

```bash
git add tests/test_bank_id_redaction.py
git commit -m "test(redaction): pin the substring bank-id strip on every route"
```

---

### Task 5: One leak scanner, and make it see the leak

**R4-C2 + R4-I1.** `scripts/e2e.py`'s `LEAK_RE` is `\b`-anchored. In a real `chunk_id` the character after the final hex group is `_` — a word character — so no boundary exists and the match fails. Verified: `e2e detects chunk-embedded: False`. `e2e.py` is the scanner that funnels *every* response across ~70 scenarios and all 15 tools, so with Task 4's gap it was invisible on both gates. Separately `e2e.py`'s comment claims `prj_` is "meant to be visible" while `mcp-smoke.py` correctly treats it as a leak and two tests assert it must never leave the API.

**Files:**
- Create: `scripts/leakscan.py`
- Modify: `scripts/e2e.py:74-84`, `scripts/mcp-smoke.py:40-43`, `scripts/smoke.sh:171-179`
- Test: `tests/test_leakscan.py` (new)

**Interfaces:**
- Produces: `scripts.leakscan.LEAK_RE` (compiled pattern) and `leakscan.find(text: str) -> str | None` returning the first offending match or None. `e2e.py` and `mcp-smoke.py` import it; `smoke.sh` calls `python3 scripts/leakscan.py` with the body on stdin, exit 1 on a hit.

- [ ] **Step 1: Write the failing test**

Create `tests/test_leakscan.py`:

```python
"""The three scripts' leak scanners were three divergent regexes.

scripts/e2e.py's was \\b-anchored, so it could not see a bank id embedded in
a chunk_id (`project_<uuid>_doc7_3`) -- the exact shape of the leak that
already shipped once. It is also the scanner with the widest coverage (~70
scenarios, all 15 tools), so that anchor made the broadest gate the blindest
one (2026-08-23 review, R4-C2).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import leakscan  # noqa: E402

BANK = "project_ba378411-348d-4eb2-9c74-ef0c9da982cc"
USER_BANK = "user_ba378411-348d-4eb2-9c74-ef0c9da982cc"


def test_a_bare_bank_id_is_caught():
    assert leakscan.find(f'{{"x": "{BANK}"}}') is not None
    assert leakscan.find(f'{{"x": "{USER_BANK}"}}') is not None


def test_a_bank_id_embedded_in_a_chunk_id_is_caught():
    """The \\b anchor made this pass. A word character follows the final hex
    group, so there is no boundary to match."""
    assert leakscan.find(f'{{"chunk_id": "{BANK}_doc7_3"}}') is not None


def test_a_literal_bank_id_key_is_caught():
    assert leakscan.find('{"bank_id": "whatever"}') is not None


def test_the_internal_project_id_is_caught():
    """prj_ is Project.internal_id (SPEC inv. 34), asserted by
    tests/test_projects_api.py and tests/test_admin_api.py to never leave the
    API -- but e2e.py's comment claimed it was "meant to be visible"."""
    assert leakscan.find('{"x": "prj_67601bd645324bfebfd161eb411a802a"}') is not None


def test_the_exposed_ids_are_not_flagged():
    """usr_/grp_/key_ ARE meant to be visible; flagging them would make every
    successful provisioning response a false positive."""
    for exposed in ("usr_00c0f7", "grp_deadbeef", "key_cafebabe"):
        assert leakscan.find(f'{{"id": "{exposed}"}}') is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_leakscan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'leakscan'`

- [ ] **Step 3: Write the shared scanner**

Create `scripts/leakscan.py`:

```python
#!/usr/bin/env python3
"""The one definition of "a bank id leaked".

There used to be three copies -- scripts/smoke.sh, scripts/e2e.py and
scripts/mcp-smoke.py -- and they had diverged. e2e.py's was \\b-anchored, so
it could not match a bank id embedded inside a chunk_id
(f"{bank_id}_{document_id}_{n}"), which is the exact shape of the leak
measured live against hindsight-api 0.9.1 on 2026-08-22 and the reason
`_strip_bank_id` redacts substrings at all. e2e.py is the broadest scanner in
the project (~70 scenarios, all 15 tools), so that anchor made the widest
gate the blindest.

Deliberately UNANCHORED: a bank id is a leak wherever it appears, including
in the middle of another field's value. That is the whole point.

Matches:
  - a literal "bank_id" key
  - user_<uuid> / project_<uuid>, the opaque bank ids (SPEC §4.7)
  - prj_<hex>, Project.internal_id (SPEC inv. 34 -- internal, never required
    by an ordinary client)

Does NOT match usr_/grp_/key_, which are exposed ids and are meant to be
visible in every provisioning response.

Usage from bash:  echo "$body" | python3 scripts/leakscan.py  # exit 1 on a hit
"""

import re
import sys

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

LEAK_RE = re.compile(
    r'"bank_id"'
    rf"|user_{_UUID}"
    rf"|project_{_UUID}"
    r"|prj_[0-9a-f]{8}"
)


def find(text: str) -> str | None:
    """The first offending match, or None."""
    match = LEAK_RE.search(text)
    return match.group(0) if match else None


if __name__ == "__main__":
    hit = find(sys.stdin.read())
    if hit:
        print(f"FAIL: a bank id reached the client: {hit}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Point all three scripts at it**

`scripts/e2e.py` — replace the comment block and `LEAK_RE` definition at lines 74-84 with:

```python
# Leak scanning -- applied to every response this script ever collects.
# The pattern lives in scripts/leakscan.py so smoke.sh, mcp-smoke.py and this
# script cannot drift apart again: this file's own copy was \b-anchored and
# therefore could not see a bank id embedded in a chunk_id, the one shape the
# scan existed to catch.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from leakscan import LEAK_RE  # noqa: E402
```

(`sys` and `Path` are already imported in `e2e.py`; confirm with `grep -n '^import\|^from' scripts/e2e.py` and add whichever is missing.)

`scripts/mcp-smoke.py` — replace the comment and `LEAK_RE` at lines 40-43 with the same two lines.

`scripts/smoke.sh` — replace the loop at lines 171-179 with:

```bash
# No bank id anywhere in any of it. Uses scripts/leakscan.py, the same pattern
# e2e.py and mcp-smoke.py use, so the three cannot drift: this loop's inline
# regex and e2e.py's disagreed on whether an embedded bank id counts (it does)
# and on whether prj_ counts (it does).
for body in "${recalled}" "${cross}" "${proj}" "${listed}" "${after}" \
            "${reflected}" "${docs_listed}" "${ops_listed}" "${op_got}" \
            "${retained}" "${retained2}" "${forgotten}" "${restored}" \
            "${op_retain}"; do
  echo "${body}" | python3 "$(dirname "$0")/leakscan.py" \
    || { echo "FAIL: leak scan rejected a response body" >&2; exit 1; }
done
echo "no bank_id in any response this script collected"
```

> The five extra variables come from Task 24, which stops `smoke.sh` discarding those bodies to `/dev/null`. Until Task 24 lands they are unset and `set -u` will abort — so **do Task 24 first, or add only the nine existing names now and the five in Task 24.** Pick one; do not leave the script un-runnable between commits.

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_leakscan.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Prove the scripts still parse**

```bash
uv run python -c "import ast,pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ('scripts/e2e.py','scripts/mcp-smoke.py','scripts/leakscan.py')]" && echo OK
bash -n scripts/smoke.sh && echo "smoke.sh syntax OK"
```
Expected: `OK` and `smoke.sh syntax OK`

- [ ] **Step 7: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q` → `533 passed`

```bash
git add scripts/leakscan.py scripts/e2e.py scripts/mcp-smoke.py scripts/smoke.sh tests/test_leakscan.py
git commit -m "fix(scripts): one leak scanner, unanchored so it sees chunk_id"
```

---

### Task 6: Stop the documented local run publishing an unauthenticated backend

**R5-C1.** Compose's port shorthand binds `0.0.0.0`. That publishes Hindsight on `:8888` — reachable with `MEMORY_HINDSIGHT_API_KEY` defaulting to empty, bypassing the entire authenticate → resolve → authorize → bank pipeline this service exists to provide — plus Postgres on `:5433` with `memory`/`memory`, plus the API on `:8000` whose master key the README tells you to set to a literal published string. Nothing needs `:8888` on the host except `scripts/e2e.py`.

**Files:**
- Modify: `docker-compose.yml:8-9, 61-62, 90-91`
- Modify: `README.md` (the quickstart around line 280)

**Interfaces:**
- Produces: `api` still reaches Hindsight in-network at `http://hindsight:8888`. `scripts/e2e.py` reaches it at `127.0.0.1:8888` as before — the binding narrows, the port stays.

- [ ] **Step 1: Bind every published port to loopback**

`docker-compose.yml`, the `postgres` service:

```yaml
    ports:
      # Loopback only. This stack is for local development: the credentials
      # below are `memory`/`memory` and the master key the README sets is a
      # published literal, so binding 0.0.0.0 hands anyone routable to this
      # host the whole tenant.
      - "127.0.0.1:5433:5432"
```

the `hindsight` service:

```yaml
    ports:
      # Loopback only, and only for scripts/e2e.py -- the `api` service
      # reaches Hindsight in-network at http://hindsight:8888 and does not
      # need this published at all. Hindsight takes MEMORY_HINDSIGHT_API_KEY,
      # which defaults to empty, so anyone who can reach this port talks to
      # every user's and project's bank with no credential.
      - "127.0.0.1:8888:8888"
```

the `api` service:

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

- [ ] **Step 2: Stop the README publishing a working master key**

In `README.md`, replace the `export MEMORY_MASTER_KEY="mem_local_master_change_me"` line with:

```bash
# Generate one. The old literal in this README was a working credential for
# every stack anyone set up by following it.
export MEMORY_MASTER_KEY="mem_local_$(openssl rand -hex 32)"
export MEMORY_MASTER_KEY_HASH=$(python3 -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")
```

and add, immediately above the quickstart block:

```markdown
> **Local development only.** `docker-compose.yml` binds every published port
> to `127.0.0.1` and ships development credentials. It is not a deployment
> topology — use `deploy/helm/ach-memory` for that.
```

- [ ] **Step 3: Verify the bindings**

```bash
grep -n -A1 'ports:' docker-compose.yml
```
Expected: all three entries start `127.0.0.1:`.

```bash
grep -rn 'mem_local_master_change_me' README.md .env docker-compose.yml || echo "no published master key left in tracked files"
```
Expected: only `.env` may still carry it (untracked, and Task 26 documents it) — README must be clean.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml README.md
git commit -m "fix(compose): bind published ports to loopback, not 0.0.0.0"
```

---

## Phase 2 — CI and packaging

### Task 7: Make CI actually gate `main`

**R5-I1 + R5-I2 + R5-I7 + minors.** `on: pull_request` only. History is linear direct commits, so the workflow has plausibly never executed — and it is an unusually good workflow, with a three-gate image job that builds the production image, runs migrations against a live Postgres, and proves an *authenticated* request answers. No `permissions:` block, so `GITHUB_TOKEN` inherits the repository default (read/write on older repos) while running `ruff`, `pytest` and `docker build`. No Python pin, so CI resolves ≥3.12 while both images pin 3.12. No `timeout-minutes`.

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: CI runs on push to `main` and on PRs; a `chart` job exists for Task 9 to extend.

- [ ] **Step 1: Add the push trigger, least-privilege token, and job timeouts**

Replace `.github/workflows/ci.yml` lines 1-9 with:

```yaml
name: CI

on:
  # push matters more than pull_request here: this repository's history is
  # linear direct commits to main, so a PR-only trigger meant the image gate
  # below -- the one that catches a mis-grouped dependency before it ships --
  # had never run on the branch that ships.
  push:
    branches: [main]
  pull_request:
    branches: [main]

# Least privilege. Without this the token inherits the repository default,
# which on older repos is read/write across contents and packages -- held by
# three jobs that all execute repository-controlled code (ruff, pytest,
# docker build).
permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

- [ ] **Step 2: Pin the interpreter and bound every job**

Add `timeout-minutes` to each of the three jobs (`lint: 10`, `test: 20`, `image: 30`), directly under `runs-on: ubuntu-latest`. And give both `setup-uv` steps an explicit interpreter:

```yaml
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          # Pinned to match both Dockerfiles (python:3.12-slim). Unpinned, uv
          # resolves whatever it finds or downloads -- so a change relying on
          # 3.13 behaviour passes here and fails inside the shipped image.
          python-version: "3.12"
```

- [ ] **Step 3: Verify the workflow parses**

```bash
uv run python -c "import yaml,pathlib; d=yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text()); print(sorted(d['jobs'])); print(d[True])"
```
Expected: `['image', 'lint', 'test']` and a trigger mapping containing both `push` and `pull_request`. (`yaml` parses the `on:` key as the boolean `True` — that is a YAML quirk, not a bug in the file.)

If `yaml` is not installed: `uv run --with pyyaml python -c ...`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run on push to main, drop the token to contents: read"
```

---

### Task 8: Declare `mcp-types`

**R5-I3.** `src/memory/mcp/tools.py:28` does `from mcp_types import ToolAnnotations`. `tools.py` is production code — `create_app` imports it at `api/app.py:63`. `mcp-types` reaches the image only as a transitive dependency of `mcp`. `pyproject.toml` carries comments about this exact failure mode **twice** (once for `mcp`, once for `httpx2`); this is the third instance and it was missed. Verified: `pyproject.toml` declares `mcp` and `httpx2`, not `mcp-types`.

**Files:**
- Modify: `pyproject.toml:15-22`
- Modify: `uv.lock` (regenerated)

- [ ] **Step 1: Prove the gap first**

```bash
grep -n 'mcp_types\|mcp-types' src/memory/mcp/tools.py pyproject.toml
```
Expected: a hit in `tools.py`, none in `pyproject.toml`.

- [ ] **Step 2: Declare it**

In `pyproject.toml`'s `[project].dependencies`, immediately after the `"mcp>=2.0.0",` line:

```toml
    # Imported directly by src/memory/mcp/tools.py (ToolAnnotations), so it is
    # production code -- but it only ever reached the image as a transitive
    # dep of `mcp`. Third instance of the failure mode the two comments above
    # already record: `mcp` mis-grouped, `httpx2` undeclared, this one
    # undeclared. A transitive dep that stops being transitive is a
    # crash-looping container, and the CI import guard only catches it once
    # CI actually runs (see .github/workflows/ci.yml's push trigger).
    "mcp-types>=2.0.0",
```

- [ ] **Step 3: Relock and verify the production set resolves without dev**

```bash
uv lock
uv export --frozen --no-dev --no-emit-project -o /tmp/prod-requirements.txt
grep -i 'mcp-types' /tmp/prod-requirements.txt
```
Expected: a `mcp-types==...` line. If it is absent, the declaration did not take.

- [ ] **Step 4: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q` → `533 passed`

```bash
git add pyproject.toml uv.lock
git commit -m "build: declare mcp-types, tools.py imports it directly"
```

---

### Task 9: Make the Helm chart installable and gate it in CI

**R5-I4 + R5-I6 + R5-I8 + probe minors.** `values.yaml:8` sets `image.repository: ach-memory` with no registry host, so the README's install verbatim resolves to `docker.io/library/ach-memory:0.1.0`, the pre-install migration Job goes `ImagePullBackOff`, and `helm install` hangs then leaves the release `pending-install`. `resources: {}` puts every pod in `BestEffort` — first evicted under node memory pressure, which for this service means a pending `retain` is lost. Liveness and readiness are the identical `/docs` probe with no `startupProbe`, so a slow first start crash-loops. And nothing in CI ever renders the chart.

**Files:**
- Modify: `deploy/helm/ach-memory/values.yaml`
- Modify: `deploy/helm/ach-memory/templates/deployment.yaml`
- Modify: `deploy/helm/README.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Task 1's `podSecurityContext` / `securityContext` values.
- Produces: a `chart` CI job that runs `helm lint` and `helm template`, including a negative case.

- [ ] **Step 1: Make the missing registry loud**

`deploy/helm/ach-memory/values.yaml`:

```yaml
image:
  # REQUIRED: no registry host. Left as a bare name, Kubernetes resolves this
  # to docker.io/library/ach-memory, which does not exist -- the pre-install
  # migration Job then sits in ImagePullBackOff and `helm install` hangs until
  # the hook times out, leaving the release in `pending-install`. Set it to
  # the registry you publish to.
  repository: ach-memory
  tag: ""    # defaults to .Chart.AppVersion
  pullPolicy: IfNotPresent
```

and in `deploy/helm/README.md`, add `--set image.repository=ghcr.io/ackstorm/ach-memory` to the documented `helm install` command, with one line saying it is required.

- [ ] **Step 2: Ship a real resource policy**

`deploy/helm/ach-memory/values.yaml`, replace `resources: {}`:

```yaml
# Non-empty on purpose. `{}` renders empty, which puts the pod in the
# BestEffort QoS class: the scheduler treats it as costing nothing and packs
# it onto a saturated node, and the kubelet evicts BestEffort pods FIRST under
# memory pressure -- so the memory service becomes the first thing killed on
# any node it lands on, losing whatever retain was in flight.
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    memory: 512Mi
```

- [ ] **Step 3: Separate the probes**

`deploy/helm/ach-memory/values.yaml`:

```yaml
# No dedicated /health route ships today; /docs (FastAPI's built-in, no auth
# required) is the same unauthenticated liveness signal scripts/smoke.sh
# already polls -- 200 once the app is serving requests. It does not check
# database or Hindsight connectivity.
#
# startupProbe exists because liveness and readiness used to be the identical
# probe with initialDelaySeconds: 5: under a slow first start (cold image,
# slow DB DNS) liveness's default failureThreshold of 3 killed the pod at
# ~35s and crash-looped it. The startup probe absorbs that window; liveness
# does not begin until it passes.
probes:
  path: /docs
  initialDelaySeconds: 5
  periodSeconds: 10
  startup:
    periodSeconds: 5
    failureThreshold: 30    # up to 150s for a first start
```

`deploy/helm/ach-memory/templates/deployment.yaml`, insert before the `livenessProbe:` block:

```yaml
          startupProbe:
            httpGet:
              path: {{ .Values.probes.path }}
              port: http
            periodSeconds: {{ .Values.probes.startup.periodSeconds }}
            failureThreshold: {{ .Values.probes.startup.failureThreshold }}
```

- [ ] **Step 4: Gate the chart in CI**

Add to `.github/workflows/ci.yml` under `jobs:`:

```yaml
  chart:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-helm@v4

      - name: helm lint
        run: helm lint deploy/helm/ach-memory

      # The chart is a shipped deliverable that README.md names as part of
      # what packages this build, but nothing rendered it: a template typo, a
      # bad nindent, or a broken required() reached a user's `helm install`
      # unverified. deployment.yaml's mcpAllowedHosts block is the trickiest
      # logic in the chart (a `:=` vs `=` scoping subtlety) and had no gate.
      - name: helm template renders
        run: |
          helm template t deploy/helm/ach-memory \
            --set image.repository=ghcr.io/ackstorm/ach-memory \
            --set config.databaseUrl=postgresql+psycopg://u:p@h:5432/m \
            --set config.hindsight.url=http://hindsight:8888 \
            --set masterKeySecret.value=deadbeef > /tmp/rendered.yaml
          grep -q 'runAsNonRoot: true' /tmp/rendered.yaml
          grep -q 'runAsUser: 10001' /tmp/rendered.yaml
          test "$(grep -c 'securityContext:' /tmp/rendered.yaml)" -ge 4

      # The chart MUST refuse to render without a master key: that credential
      # reaches every bank in the tenant, so a silent default would be the
      # worst possible failure mode.
      - name: helm template fails with no master key
        run: |
          if helm template t deploy/helm/ach-memory \
            --set image.repository=ghcr.io/ackstorm/ach-memory \
            --set config.databaseUrl=postgresql+psycopg://u:p@h:5432/m \
            --set config.hindsight.url=http://hindsight:8888 >/dev/null 2>&1
          then
            echo "FAIL: the chart rendered with no master key configured" >&2
            exit 1
          fi
          echo "correctly refused"
```

- [ ] **Step 5: Verify locally if helm is available**

```bash
command -v helm >/dev/null && helm lint deploy/helm/ach-memory || echo "helm not installed locally -- CI will gate this"
```

- [ ] **Step 6: Commit**

```bash
git add deploy/helm .github/workflows/ci.yml
git commit -m "fix(helm): require a registry, ship resources, gate the chart in CI"
```

---

### Task 10: Make `docker compose up` produce a working stack

**R5-I9.** There is no migration service. `README.md` compensates with a manual second command, but between step 1 and step 2 the `api` container is accepting traffic against an empty schema, and anyone who runs only `docker compose up -d` gets four containers `Up` and a stack that answers `INTERNAL_ERROR` to everything, with `relation "users" does not exist` visible only in `docker logs`.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `README.md`

- [ ] **Step 1: Add a one-shot migrate service**

In `docker-compose.yml`, add before the `api` service:

```yaml
  # Runs to completion before `api` starts. Without this, `docker compose up
  # -d` gives four healthy-looking containers and a stack that 500s on every
  # request against an empty schema -- the failure is only visible in
  # `docker logs`, because api/app.py's handler deliberately never echoes the
  # cause. The chart does the same thing with a pre-install hook Job.
  migrate:
    build: .
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      MEMORY_DATABASE_URL: postgresql+psycopg://memory:memory@postgres:5432/memory
    command: ["python", "-m", "alembic", "upgrade", "head"]
```

and extend the `api` service's `depends_on`:

```yaml
    depends_on:
      migrate:
        condition: service_completed_successfully
```

(keep whatever other `depends_on` entries `api` already has; add this one.)

- [ ] **Step 2: Delete the manual migration step from the README**

Remove the standalone `docker compose run --rm api python -m alembic upgrade head` line (README ~line 288) and replace it with:

```markdown
Migrations run automatically: the `migrate` service applies `alembic upgrade
head` and `api` waits for it to complete.
```

- [ ] **Step 3: Verify the compose file parses and the ordering is right**

```bash
docker compose config >/dev/null && echo "compose config OK"
docker compose config | grep -A4 'migrate:' | head -20
```
Expected: `compose config OK`, and `api`'s `depends_on` naming `migrate` with `service_completed_successfully`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml README.md
git commit -m "fix(compose): run migrations before api serves traffic"
```

---

### Task 11: Harden the build context

**R5 minors.** `.dockerignore` is missing `__pycache__/`, `*.pyc`, `.mypy_cache/` and `.env`. `.env` cannot leak today because both Dockerfiles copy explicit paths — but `COPY src/ /app/src/` will happily ship whatever `src/**/__pycache__` a local `uv run pytest` left behind: stale bytecode in the image and a busted layer cache on every local build.

**Files:**
- Modify: `.dockerignore`

- [ ] **Step 1: Add the four entries**

Append to `.dockerignore`:

```
# Not reachable today -- both Dockerfiles copy explicit paths, never `COPY . .`
# -- but these cost nothing and make the file robust to a future COPY. The
# __pycache__ entries matter now: COPY src/ ships whatever bytecode a local
# test run left behind, busting the layer cache on every build.
__pycache__/
*.pyc
.mypy_cache/
.env
.env.*
```

- [ ] **Step 2: Verify the build context shrinks**

```bash
docker build -t ach-memory:dockerignore-check . >/dev/null && echo "build OK"
docker run --rm ach-memory:dockerignore-check sh -c 'find /app -name "__pycache__" -o -name ".env" | head'
```
Expected: `build OK` and no output from the `find` — nothing named `__pycache__` or `.env` inside the image.

- [ ] **Step 3: Commit**

```bash
git add .dockerignore
git commit -m "build: exclude bytecode, caches and .env from the build context"
```

---
### Task 12: Publish the public artifacts safely

**Distribution constraint above + R5 minors (digest pinning, action pinning).** The chart and both images go to a public registry while the source repository stays private. Public artifacts need provenance and reproducibility that private ones can get away with lacking, and a public chart is installed by people who will never read this repo.

**Files:**
- Create: `LICENSE` (if absent — decide the licence first; the chart and images carry its identifier)
- Modify: `Dockerfile`, `Dockerfile.hindsight` (OCI labels, digest-pinned bases)
- Modify: `docker-compose.yml`, `.github/workflows/ci.yml` (digest-pinned bases, pinned actions)
- Modify: `deploy/helm/README.md`, `deploy/helm/ach-memory/Chart.yaml`
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: Task 7's `permissions: contents: read` at workflow level.
- Produces: `ghcr.io/ackstorm/ach-memory:<version>` and `oci://ghcr.io/ackstorm/charts/ach-memory:<version>`, both public after the one-time visibility flip in Step 5b; the release workflow holds `packages: write` **only on the publish job**.

- [ ] **Step 1: Confirm the licence**

**MIT**, decided 2026-08-23. `LICENSE` was written in Task 1 Step 3; this step
only confirms it exists and that `MIT` is the SPDX identifier every artifact
below stamps.

```bash
head -3 LICENSE
```

Note the asymmetry, which is deliberate and fine: the **repository** is private
while the **artifacts** are MIT. MIT governs what a recipient may do with what
they receive; it does not oblige you to publish the repository.

- [ ] **Step 2: Resolve the base-image digests**

```bash
docker pull python:3.12-slim >/dev/null
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
docker pull postgres:17-alpine >/dev/null
docker inspect --format='{{index .RepoDigests 0}}' postgres:17-alpine
docker pull pgvector/pgvector:pg17 >/dev/null
docker inspect --format='{{index .RepoDigests 0}}' pgvector/pgvector:pg17
```
Record the three `sha256:` values. `hindsight-api==0.9.1` is already pinned with a good comment — extend that same reasoning to the bases.

- [ ] **Step 3: Pin and label both Dockerfiles**

`Dockerfile` — pin both stages to the digest from Step 2 and add OCI labels to the runtime stage, immediately before `EXPOSE 8000`:

```dockerfile
# Digest-pinned, not just tag-pinned: this image is PUBLISHED PUBLICLY, so a
# tag that moves under us makes a released version unreproducible for everyone
# who pulled it. Same reasoning as hindsight-api==0.9.1 in Dockerfile.hindsight.
FROM python:3.12-slim@sha256:<digest from step 2> AS builder
```

```dockerfile
LABEL org.opencontainers.image.title="ach-memory" \
      org.opencontainers.image.description="Multi-tenant memory service for coding agents, over Hindsight" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ackstorm/ach-memory" \
      org.opencontainers.image.revision="${GIT_SHA:-unknown}"
```

Apply the same digest pin and an equivalent `LABEL` block to `Dockerfile.hindsight`, whose `licenses` is also `MIT` (it wraps the MIT-licensed `hindsight-api`) — labelled even though Task 12 does not publish it, so a locally built image still carries its provenance.

> **Do not add a build ARG for anything secret.** Build args are recorded in the image history and are readable with `docker history` on a public image. `GIT_SHA` is fine; a token is not.

- [ ] **Step 4: Pin the workflow actions to commit SHAs**

`actions/checkout@v4`, `astral-sh/setup-uv@v5` and `azure/setup-helm@v4` are mutable tags. Resolve each to a commit SHA and pin it:

```bash
gh api repos/actions/checkout/git/refs/tags/v4 --jq .object.sha
gh api repos/astral-sh/setup-uv/git/refs/tags/v5 --jq .object.sha
gh api repos/azure/setup-helm/git/refs/tags/v4 --jq .object.sha
```

Replace each `uses:` with `uses: owner/repo@<sha>  # v4` across `.github/workflows/ci.yml`. Standard supply-chain hygiene for a workflow that publishes public artifacts.

- [ ] **Step 5: Write the release workflow**

Create `.github/workflows/release.yml`:

```yaml
name: Release

# Tag-driven, never on push to main: a published public artifact must
# correspond to a version somebody chose, not to whatever landed last.
on:
  push:
    tags: ["v*"]

permissions:
  contents: read

jobs:
  publish:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    # Elevated HERE and nowhere else: the lint/test/image jobs in ci.yml run
    # repository-controlled code (ruff, pytest, docker build) and must never
    # hold a token that can write packages.
    permissions:
      contents: read
      packages: write
      id-token: write        # provenance attestation
    steps:
      - uses: actions/checkout@<sha>  # v4

      - name: Log in to GHCR
        run: echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u ${{ github.actor }} --password-stdin

      - name: Build and push the service image
        run: |
          version="${GITHUB_REF_NAME#v}"
          image="ghcr.io/ackstorm/ach-memory"
          docker build --build-arg GIT_SHA="${GITHUB_SHA}" -t "${image}:${version}" .
          docker push "${image}:${version}"
          echo "IMAGE_REF=${image}:${version}" >> "$GITHUB_ENV"

      # No Hindsight image published. CUT 2026-08-23 as YAGNI: it is a
      # fourteen-line wrapper around `pip install hindsight-api==0.9.1`, which
      # anyone can build in seconds from a Dockerfile that is in the chart's
      # documentation. Publishing it buys convenience nobody has asked for and
      # adds a third artifact to keep versioned, scanned and public. Add it
      # when someone actually wants to pull it.

      - name: Push the Helm chart as an OCI artifact
        run: |
          version="${GITHUB_REF_NAME#v}"
          helm package deploy/helm/ach-memory --version "${version}" --app-version "${version}"
          helm push "ach-memory-${version}.tgz" \
            "oci://ghcr.io/ackstorm/charts"

      # No provenance attestation. CUT 2026-08-23 as YAGNI: it is supply-chain
      # ceremony for an artifact with no external consumers yet, and nobody
      # has asked to verify it. Digest-pinned bases (Step 3) and SHA-pinned
      # actions (Step 4) earn their place because they make a published
      # version reproducible; an attestation nobody checks does not. Add
      # `actions/attest-build-provenance` the first time a consumer asks.
```

- [ ] **Step 5b: Flip the two packages to public (one-time, manual)**

The repository is private, and GHCR packages inherit the **repository's**
visibility — so this first release publishes two *private* packages. The
workflow cannot change that for you.

After the first successful run, in `github.com/orgs/ackstorm/packages`, set
both `ach-memory` and `charts/ach-memory` to **Public**, and confirm from a
logged-out shell:

```bash
docker logout ghcr.io
docker pull ghcr.io/ackstorm/ach-memory:<version>
helm pull oci://ghcr.io/ackstorm/charts/ach-memory --version <version>
```

Both must succeed with no credentials. If they 401, the flip did not take —
this is the single most likely way the release ships "published" and
unreachable. Record the date of the flip in `deploy/helm/README.md`.

> Publishing the service image publicly publishes `src/`. That was raised and
> accepted (see **Distribution constraint** above) — this step is the moment
> it becomes true, so do it deliberately, not by reflex.

- [ ] **Step 6: Write the chart README for strangers**

`deploy/helm/README.md` — add near the top:

```markdown
## Before you install

This chart is published publicly; the service's source repository is not.

- `image.repository` is **required** and has no registry host by default.
- `masterKeySecret.name` (an existing Secret) is the **recommended** way to
  supply `MEMORY_MASTER_KEY_HASH`. `masterKeySecret.value` puts the hash in
  your values file — fine for a local trial, wrong for anything shared. That
  credential reaches every bank in the tenant.
- The chart runs the service only. Postgres and Hindsight are dependencies you
  point it at; an in-chart database is how test data ends up in production.
- Licence: `MIT`.
```

and set `Chart.yaml`'s `annotations` so the licence travels with the artifact:

```yaml
annotations:
  artifacthub.io/license: "MIT"
```

- [ ] **Step 7: Prove no secret is reachable in the published image**

```bash
docker build -t ach-memory:public-check .
docker history --no-trunc ach-memory:public-check | grep -iE 'key|secret|token|password' || echo "no credential in image history"
docker run --rm ach-memory:public-check sh -c 'ls -a /app; find / -name ".env" -not -path "/proc/*" 2>/dev/null | head'
```
Expected: `no credential in image history`, `/app` holding only `deps`, `src`, `migrations`, `alembic.ini`, and no `.env` anywhere.

- [ ] **Step 8: Commit**

```bash
git add LICENSE Dockerfile Dockerfile.hindsight docker-compose.yml \
        .github/workflows deploy/helm
git commit -m "build: pin bases by digest and publish public artifacts with provenance"
```

---

## Phase 3 — Error model and input validation

### Task 13: Close SPEC §18 for real

**R1-#2 + R2-I7 — found independently by two reviewers.** `UserAlreadyExists` is declared in `api/users.py:19-21`, not in `errors.py`, and `USER_ALREADY_EXISTS` is absent from §18's closed list. `tests/test_errors.py` builds `declared` from `vars(errors)`, so the one code in the codebase that violates §18 is the one code the two-way test is structurally blind to — while the test's own docstring advertises exactly that guarantee.

**Files:**
- Modify: `src/memory/errors.py`, `src/memory/api/users.py`
- Modify: `SPEC-v1.md` §18
- Modify: `tests/test_errors.py`

**Interfaces:**
- Produces: `memory.errors.UserAlreadyExists` (code `USER_ALREADY_EXISTS`, status 409). `api/users.py` imports it instead of declaring it. Task 3's `reject_control_characters(body.id, UserAlreadyExists)` call continues to work.

- [ ] **Step 1: Make the test structural before fixing the instance**

Replace the `declared` computation in `tests/test_errors.py` (lines 36-40) with a subclass walk taken after the app is imported:

```python
    # Enumerate SUBCLASSES, not vars(errors). The previous version only saw
    # classes declared in errors.py itself, so `UserAlreadyExists` -- the one
    # DomainError declared in a route module -- was invisible to a test whose
    # entire purpose is catching exactly that (2026-08-23 review, R1-#2/R2-I7).
    # create_app() is called first so every route module is imported and any
    # subclass declared outside errors.py is registered before we look.
    from memory.api.app import create_app

    create_app()

    def _subclasses(cls) -> set[type]:
        found = set()
        for sub in cls.__subclasses__():
            found.add(sub)
            found |= _subclasses(sub)
        return found

    declared = {
        obj.code for obj in _subclasses(errors.DomainError) | {errors.DomainError}
    }
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL — `declared but missing from SPEC §18: ['USER_ALREADY_EXISTS']`

That failure is the finding. The test now sees what it always claimed to see.

- [ ] **Step 3: Move the class to `errors.py`**

Add to `src/memory/errors.py`, immediately after `GroupAlreadyExists`:

```python
class UserAlreadyExists(DomainError):
    """The sibling of GroupAlreadyExists, and it lived in api/users.py.

    Declaring a DomainError outside this module hid it from
    tests/test_errors.py's §18 conformance check, which enumerated
    `vars(errors)` -- so the single code in the codebase that violated §18's
    closed list was the single code the guard could not see. Every
    DomainError belongs here.
    """

    code = "USER_ALREADY_EXISTS"
    status = 409
```

In `src/memory/api/users.py`, delete the local class (lines 19-21) and extend the import:

```python
from memory.errors import KeyNotFound, UserAlreadyExists, UserNotFound
```

(`DomainError` is no longer needed there — drop it from the import if nothing else in the file uses it. Check with `grep -n DomainError src/memory/api/users.py`.)

- [ ] **Step 4: Add the code to SPEC §18**

In `SPEC-v1.md`, inside the fenced `text` block under `## 18. Error model`, add `USER_ALREADY_EXISTS` immediately after `GROUP_ALREADY_EXISTS`. Then add the prose entry beside its sibling's:

```markdown
`USER_ALREADY_EXISTS` (409): `POST /v1/users` with an explicit id that already
exists. The ACH provisioning path (§16.3) supplies its own ids and retries, so
this is an ordinary idempotent-retry outcome a client must be able to branch
on -- not a server fault.
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_errors.py tests/test_users_api.py -v`
Expected: PASS — including `tests/test_users_api.py:77`, which already pins the 409 shape.

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q` → `533 passed`

```bash
git add src/memory/errors.py src/memory/api/users.py SPEC-v1.md tests/test_errors.py
git commit -m "fix(errors): move UserAlreadyExists into the SPEC 18 registry"
```

---

### Task 14: Reject unknown fields on every request model

**R2-I4.** Only `CreateProjectRequest` / `UpdateProjectRequest` set `extra="forbid"`. Everything else is `extra="ignore"`, so a typoed field is a silent 200/201 no-op — which is Plan 6's finding I1 (*"a caller following SPEC §8.4 PATCHed git_locator, got 200 OK, and nothing changed"*) reproduced verbatim on the §14 routes. Worse on `POST /v1/users`: `{"user_id": "ach-user-82f"}` provisions a random id instead of ACH's, silently.

**Files:**
- Modify: `src/memory/api/memory.py` (`ScopedRequest` — the base of every data-plane and §14 model)
- Modify: `src/memory/api/users.py` (`CreateUserRequest`), `src/memory/api/groups.py` (`CreateGroupRequest`), `src/memory/api/projects.py` (`Owner`)
- Test: `tests/test_unknown_fields.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: every request model except `MentalModelTrigger` rejects unknown fields with a 422. `MentalModelTrigger.model_config` stays `extra="allow"` — §14.5 makes that pass-through deliberate, and inheriting `forbid` on the outer model does not touch it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_unknown_fields.py`:

```python
"""A typoed field must be a 422, never a silent no-op.

This is Plan 6's finding I1 -- "a caller following SPEC §8.4 PATCHed
git_locator, got 200 OK, and nothing changed" -- on every model that did not
get the fix. Verified before this test was written: UpdateDirectiveRequest(
scope="user", priorty=9) validated cleanly with every real field None, so the
PATCH sent an empty {} upstream and answered 200 (2026-08-23 review, R2-I4).
"""

import pytest

DIR_ID = "11111111-1111-1111-1111-111111111111"
MM_ID = "mm-1234567890abcdef1234567890abcdef"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/v1/memory/retain", {"scope": "user", "content": "x", "documnt_id": "d"}),
        ("POST", "/v1/memory/recall", {"scope": "user", "query": "x", "quer": "y"}),
        ("POST", "/v1/memory/list", {"scope": "user", "stat": "valid"}),
        ("POST", "/v1/directives", {"scope": "user", "name": "n", "content": "c", "priorty": 9}),
        ("PATCH", f"/v1/directives/{DIR_ID}", {"scope": "user", "priorty": 9}),
        ("POST", "/v1/mental-models", {"scope": "user", "name": "n", "source_query": "q", "max_token": 999}),
        ("PATCH", f"/v1/mental-models/{MM_ID}", {"scope": "user", "max_token": 999}),
    ],
)
def test_an_unknown_field_is_refused(client, master_headers, tenant, method, path, body):
    response = client.request(method, path, json=body, headers=master_headers)
    assert response.status_code == 422, response.text


def test_a_typoed_user_id_does_not_silently_provision_a_random_user(
    client, master_headers, tenant
):
    """SPEC §16.3: ACH supplies its own user ids. `{"user_id": ...}` instead of
    `{"id": ...}` used to 201 with a service-generated usr_<random>, and ACH's
    own id was never stored -- a provisioning failure that looks like success."""
    response = client.post(
        "/v1/users", json={"user_id": "ach-user-82f"}, headers=master_headers
    )
    assert response.status_code == 422, response.text


def test_a_typoed_group_name_is_refused(client, master_headers, tenant):
    response = client.post(
        "/v1/groups", json={"id": "grp_x", "nmae": "X"}, headers=master_headers
    )
    assert response.status_code == 422, response.text


def test_mental_model_trigger_still_passes_unknown_keys_through(client):
    """§14.5 makes MentalModelTrigger deliberate pass-through. Inheriting
    forbid on the OUTER model must not close it."""
    from memory.api.mental_models import CreateMentalModelRequest

    body = CreateMentalModelRequest(
        scope="user",
        name="n",
        source_query="q",
        trigger={"mode": "full", "refresh_cron": "0 3 * * *"},
    )
    assert body.trigger.model_dump()["refresh_cron"] == "0 3 * * *"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_unknown_fields.py -v`
Expected: FAIL — the parametrised cases return 200/201, not 422.

- [ ] **Step 3: Forbid extras at the base of the hierarchy**

`src/memory/api/memory.py` — `ScopedRequest` is the base of `RetainRequest`, `RecallRequest`, `ListMemoriesRequest`, `MemoryIdRequest`, `ForgetRequest`, `CorrectRequest`, `ListDocumentsRequest`, `DocumentIdRequest`, `ListOperationsRequest`, `OperationIdRequest`, and all four §14 models. One line covers every one of them:

```python
class ScopedRequest(BaseModel):
    """Everything `_resolve_bank` needs, and nothing else.

    ...

    extra="forbid" on the BASE, so every data-plane and §14 model inherits it:
    a typoed field used to validate cleanly with the real field left None, so
    a PATCH sent an empty body upstream and answered 200 having changed
    nothing -- Plan 6's finding I1, which was fixed on the project models and
    on no others.
    """

    model_config = ConfigDict(extra="forbid")

    scope: Scope
    ...
```

and extend the import:

```python
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
```

`src/memory/api/users.py` — `CreateUserRequest`:

```python
class CreateUserRequest(BaseModel):
    # extra="forbid": `{"user_id": "ach-user-82f"}` (SPEC §16.3's field is
    # `id`) used to 201 with a service-generated id while ACH's own id was
    # silently dropped -- a provisioning failure indistinguishable from
    # success.
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=128)
```

with `from pydantic import BaseModel, ConfigDict, Field`.

`src/memory/api/groups.py` — the same two lines on `CreateGroupRequest`, same import change.

`src/memory/api/projects.py` — `Owner` is both a request body (`PATCH /v1/projects/{slug}/owner`) and a response field:

```python
class Owner(BaseModel):
    # extra="forbid": this doubles as the PATCH .../owner request body, where
    # a typoed key would transfer ownership to a None id.
    model_config = ConfigDict(extra="forbid")

    type: str
    id: str
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_unknown_fields.py -v`
Expected: PASS (11 cases)

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -m "not integration" -q`

Expect breakage here: existing tests that send harmless extra keys now 422. Fix each by removing the extra key from the test's body — **not** by relaxing the model. If a test genuinely needs pass-through, it is a `MentalModelTrigger` case and the outer model is not the problem.

- [ ] **Step 6: Commit**

```bash
git add src/memory/api tests/test_unknown_fields.py
git commit -m "fix(api): forbid unknown fields on ScopedRequest and provisioning"
```

---

### Task 15: Bound the high side of every page size

**R2-I3 + R3-I-5.** `curation.py:34-37`'s own comment claims its bounds make "an out-of-range value a typed 422 at the boundary, not a 502 blaming the backend for the caller's typo" — but only the low side is bounded. Verified live: `{"limit": 100000000000000000000}` returns `502 HINDSIGHT_ERROR`, and `{"limit": 1000000000}` returns 200 with an unbounded page on a route that is `is_write=False` and therefore unmetered. `admin.py:56-57` is the one route that got it right. Separately, MCP and REST disagree on the *default* page size for documents and operations, so the same credential gets different counts from the two surfaces.

**Files:**
- Modify: `src/memory/api/curation.py:38-39`, `documents.py:29-30`, `operations.py:31-32`, `directives.py:122-123`, `mental_models.py:130-131`
- Test: `tests/test_pagination_bounds.py` (new)

**Interfaces:**
- Produces: `MAX_PAGE_SIZE = 500` in `memory.api.memory`, imported by all five routers so the ceiling has one definition. Task 20 (MCP typed signatures) reuses it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pagination_bounds.py`:

```python
"""An out-of-range page size must be a 422, never a 502.

curation.py's own comment claimed this was already true -- only the low side
(ge=0) was bounded. Verified live: limit=10**20 answered 502 HINDSIGHT_ERROR
(a code whose whole meaning to an agent is "retry"), and limit=10**9 answered
200 with an unbounded page, on routes that are is_write=False and therefore
unmetered (2026-08-23 review, R2-I3).
"""

import pytest

HUGE = 100000000000000000000
BIG = 1000000000


@pytest.mark.parametrize(
    "method,path,body,params",
    [
        ("POST", "/v1/memory/list", {"scope": "user"}, None),
        ("POST", "/v1/memory/documents/list", {"scope": "user"}, None),
        ("POST", "/v1/memory/operations/list", {"scope": "user"}, None),
        ("GET", "/v1/directives", None, {"scope": "user"}),
        ("GET", "/v1/mental-models", None, {"scope": "user"}),
    ],
)
@pytest.mark.parametrize("value", [HUGE, BIG])
def test_an_oversize_limit_is_refused_at_the_boundary(
    client, master_headers, tenant, method, path, body, params, value
):
    payload = dict(body or {}, limit=value) if body is not None else None
    query = dict(params or {}, limit=value) if params is not None else None
    response = client.request(
        method, path, json=payload, params=query, headers=master_headers
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "method,path,body,params",
    [
        ("POST", "/v1/memory/list", {"scope": "user"}, None),
        ("GET", "/v1/directives", None, {"scope": "user"}),
    ],
)
def test_a_zero_limit_is_refused(
    client, master_headers, tenant, method, path, body, params
):
    """limit=0 is meaningless and was forwarded upstream verbatim."""
    payload = dict(body or {}, limit=0) if body is not None else None
    query = dict(params or {}, limit=0) if params is not None else None
    response = client.request(
        method, path, json=payload, params=query, headers=master_headers
    )
    assert response.status_code == 422, response.text
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_pagination_bounds.py -v`
Expected: FAIL — most cases return 200 or 502.

- [ ] **Step 3: Define the ceiling once**

In `src/memory/api/memory.py`, beside `Scope`:

```python
# One ceiling for every paginated route. `admin.list_audit` chose le=500 and
# was the ONLY route that bounded the high side; the other five bounded only
# ge=0, so `limit=10**20` reached Hindsight and came back as a 502 blaming the
# backend for the caller's typo -- the exact outcome curation.py's own comment
# claimed to prevent. ge=1 rather than ge=0: a zero page is meaningless and
# was forwarded verbatim.
MAX_PAGE_SIZE = 500
```

- [ ] **Step 4: Apply it at all five sites**

`src/memory/api/curation.py` — extend the import from `memory.api.memory` with `MAX_PAGE_SIZE`, then:

```python
    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    offset: int | None = Field(default=None, ge=0)
```

`src/memory/api/documents.py`:

```python
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)
```

`src/memory/api/operations.py`:

```python
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)
```

`src/memory/api/directives.py` and `src/memory/api/mental_models.py` (query parameters, not body fields):

```python
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_pagination_bounds.py -v`
Expected: PASS (12 cases)

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/api tests/test_pagination_bounds.py
git commit -m "fix(api): bound the high side of every page size, not just ge=0"
```

---

### Task 16: Cap the caller text that is not `content`

**R2-I5 + R1-#4.** SPEC §20's "cap retain/content size" MUST is implemented on `retain` and `correct` only. `CreateDirectiveRequest.content`, `CreateMentalModelRequest.source_query`, both `name` fields and `RecallRequest.query` are unbounded `str`. A 50 MB directive is a standing rule prepended to every reflect for a whole project — and directives are the one thing in §14.1 that steers *other people's* agents. `provenance.build()` bounds nothing it forwards either: `{"note": "<8 MB>"}` passes `_check_content_size` (content is 1 byte) and is POSTed to Hindsight as extraction metadata, fed to the extraction LLM on a server-level key with no cost attribution (§19.4).

**Files:**
- Modify: `src/memory/api/directives.py`, `src/memory/api/mental_models.py`, `src/memory/api/memory.py`
- Modify: `src/memory/provenance.py`
- Test: `tests/test_content_caps.py` (new)

**Interfaces:**
- Consumes: `memory.api.memory._check_content_size`.
- Produces: `provenance.build(...)` raises `ContentTooLarge` for oversize metadata. Task 19 (MCP retain ordering) depends on `build` still raising *before* anything is written.

- [ ] **Step 1: Write the failing test**

Create `tests/test_content_caps.py`:

```python
"""SPEC §20's content cap, on every field that carries caller text.

It was implemented for `retain` and later `correct` and nowhere else. A
directive's `content` is the worst gap: for project scope that text is a
standing rule prepended to every reflect for everyone on the project (§14.1),
and it was unbounded (2026-08-23 review, R2-I5). `provenance.build` forwarded
unbounded metadata straight to the extraction LLM (R1-#4).
"""

import pytest

OVERSIZE = "x" * 300_000  # MEMORY_MAX_CONTENT_BYTES defaults to 256_000


@pytest.mark.parametrize(
    "path,body",
    [
        ("/v1/directives", {"scope": "user", "name": "n", "content": OVERSIZE}),
        ("/v1/directives", {"scope": "user", "name": OVERSIZE, "content": "c"}),
        (
            "/v1/mental-models",
            {"scope": "user", "name": "n", "source_query": OVERSIZE},
        ),
        (
            "/v1/mental-models",
            {"scope": "user", "name": OVERSIZE, "source_query": "q"},
        ),
    ],
)
def test_oversize_governance_text_is_refused(client, master_headers, tenant, path, body):
    response = client.post(path, json=body, headers=master_headers)
    assert response.status_code in (413, 422), response.text
    if response.status_code == 413:
        assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_an_oversize_recall_query_is_refused(client, master_headers, tenant):
    """reflect spends model tokens on a server-level key with no per-user cost
    attribution (§19.4). retain is capped; the token-spending route was not."""
    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "user", "user_id": "nobody", "query": OVERSIZE},
        headers=master_headers,
    )
    assert response.status_code in (413, 422), response.text


def test_oversize_metadata_is_refused_even_when_content_is_tiny():
    """_check_content_size sees 1 byte; the 8 MB rides alongside it."""
    import pytest as _pytest

    from memory.errors import ContentTooLarge
    from memory.provenance import build

    with _pytest.raises(ContentTooLarge):
        build({"note": "y" * 300_000}, project_slug=None)


def test_ordinary_metadata_still_passes():
    from memory.provenance import build

    assert build({"source": "cli"}, project_slug="p") == {
        "source": "cli",
        "project_slug": "p",
    }
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_content_caps.py -v`
Expected: FAIL — the governance routes 201/502 and `build` returns the 300 KB mapping.

- [ ] **Step 3: Cap the governance fields**

`src/memory/api/directives.py` — extend the import from `memory.api.memory` with `_check_content_size`, bound the models, and call the shared check:

```python
class CreateDirectiveRequest(ScopedRequest):
    # max_length on name mirrors every other bounded identifier in the
    # service; content gets the MEMORY_MAX_CONTENT_BYTES check below rather
    # than a character bound, because the ceiling is in bytes.
    name: str = Field(max_length=256)
    content: str
    # Bounded: an unbounded int overflows the upstream column the same way an
    # unbounded string does.
    priority: int | None = Field(default=None, ge=0, le=1000)
    is_active: bool | None = None


class UpdateDirectiveRequest(ScopedRequest):
    name: str | None = Field(default=None, max_length=256)
    content: str | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    is_active: bool | None = None
```

with `from pydantic import Field` added to the imports, and in both `create_directive` and `update_directive`, as the first statement of the handler body:

```python
    # Same MEMORY_MAX_CONTENT_BYTES ceiling `retain` and `correct` carry
    # (SPEC §20). For project scope this text is a standing rule prepended to
    # every reflect for everyone on the project (§14.1) -- the LAST caller
    # text in the service that should have been uncapped.
    if body.content is not None:
        _check_content_size(body.content)
```

`src/memory/api/mental_models.py` — the same shape:

```python
class CreateMentalModelRequest(ScopedRequest):
    name: str = Field(max_length=256)
    source_query: str
    max_tokens: int | None = Field(default=None, ge=256, le=8192)
    trigger: MentalModelTrigger | None = None


class UpdateMentalModelRequest(ScopedRequest):
    name: str | None = Field(default=None, max_length=256)
    source_query: str | None = None
    max_tokens: int | None = Field(default=None, ge=256, le=8192)
    trigger: MentalModelTrigger | None = None
```

and in both `create_mental_model` and `update_mental_model`:

```python
    if body.source_query is not None:
        _check_content_size(body.source_query)
```

`src/memory/api/memory.py` — bound the query that spends tokens:

```python
class RecallRequest(ScopedRequest):
    query: str
```

becomes:

```python
class RecallRequest(ScopedRequest):
    # `reflect` shares this model and spends model tokens on a server-level
    # credential with no per-user attribution (SPEC §19.4) -- which is the
    # actual thing the write limiter defends against. `retain` was capped and
    # the token-spending route was not.
    query: str = Field(max_length=8_000)
```

- [ ] **Step 4: Bound the metadata inside `provenance.build`**

`src/memory/provenance.py` — extend the import and add the check after the reserved-key loop, before the extraction mapping is built:

```python
import json

from memory.config import get_settings
from memory.errors import ContentTooLarge, InvalidMetadata
```

```python
    # Bounded here, not at each route: both surfaces funnel through this one
    # function, the same argument `_check_content_size`'s docstring makes.
    # `_check_content_size` sees only `content`, so `{"note": "<8 MB>"}` rode
    # alongside a 1-byte content straight into Hindsight's extraction input --
    # unbounded spend on a server-level key (SPEC §19.4), against an
    # append-only store (§12) with no cheap undo.
    limit = get_settings().max_content_bytes
    if len(json.dumps(supplied, default=str).encode("utf-8")) > limit:
        raise ContentTooLarge(f"metadata exceeds {limit} bytes")
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_content_caps.py -v`
Expected: PASS (7 cases)

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory tests/test_content_caps.py
git commit -m "fix(api): cap directive, mental-model, query and metadata text"
```

---

### Task 17: Return `resolved_from` from the two PATCH project routes

**R2-I6 + R2 minor.** `projects.py:182` and `:198` both return `_response(project)` with no second argument, so `resolved_from` is `None` and `RenameForwarding._derive_notice` sets `notice=None`. `get_project` passes it correctly; these two are the only forwarding-capable routes that discard it. SPEC §8.6 specifies the opposite. A client that keys off `notice` to update its pinned `MEMORY_PROJECT` — the entire point of the tombstone — never learns it followed one. Separately, `release_slug` is the only slug lookup in the service that does not `normalize_slug` its path parameter, on a route whose whole purpose is an operator typing a name by hand.

**Files:**
- Modify: `src/memory/api/projects.py:164-182`, `:193-198`
- Modify: `src/memory/api/admin.py` (`release_slug`)
- Test: `tests/test_projects_api.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_projects_api.py`:

```python
def test_a_patch_through_a_tombstone_reports_the_forward(client, master_headers, tenant):
    """SPEC §8.6: resolution of a retired slug "succeeds, forwards to the
    project, and annotates the response". get_project did; the two PATCH
    routes dropped it, so a client keying off `notice` to update its pinned
    MEMORY_PROJECT never learned it had followed a tombstone -- which is the
    entire point of the tombstone (2026-08-23 review, R2-I6).
    """
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    client.post("/v1/projects", json={"project_slug": "old-name"}, headers=headers)
    client.patch(
        "/v1/projects/old-name", json={"project_slug": "new-name"}, headers=headers
    )

    patched = client.patch(
        "/v1/projects/old-name",
        json={"git_locator": "github.com/acme/repo"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["resolved_from"] == "old-name", patched.text
    assert patched.json()["notice"] == "PROJECT_RENAMED", patched.text

    transferred = client.patch(
        "/v1/projects/old-name/owner",
        json={"type": "user", "id": user_id},
        headers=headers,
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["resolved_from"] == "old-name", transferred.text
    assert transferred.json()["notice"] == "PROJECT_RENAMED", transferred.text
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_projects_api.py::test_a_patch_through_a_tombstone_reports_the_forward -v`
Expected: FAIL — `assert None == 'old-name'`

- [ ] **Step 3: Pass the resolution through**

`src/memory/api/projects.py`, `update_project`'s last two lines:

```python
    db.commit()
    # result.resolved_from, not a bare _response(project): SPEC §8.6 says a
    # request that followed a tombstone is annotated, and this route is
    # forwarding-capable exactly like get_project.
    return _response(project, result.resolved_from)
```

and `transfer_project`'s:

```python
    db.commit()
    return _response(project, result.resolved_from)
```

- [ ] **Step 4: Normalize the released slug**

`src/memory/api/admin.py`, in `release_slug`, before the `db.get`:

```python
    # normalize_slug like every other slug lookup in the service. Without it
    # `POST /v1/admin/slugs/Payments-API/release` 404s against a tombstone
    # stored as `payments-api` -- on the one route whose whole purpose is an
    # operator typing a name by hand.
    retired_slug = normalize_slug(retired_slug)
```

with `from memory.slugs import normalize_slug` added to the imports.

- [ ] **Step 5: Run the tests and commit**

Run: `uv run pytest tests/test_projects_api.py tests/test_admin_api.py -q` → PASS
Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/api/projects.py src/memory/api/admin.py tests/test_projects_api.py
git commit -m "fix(projects): annotate PATCH responses that followed a tombstone"
```

---
## Phase 4 — MCP surface parity

The reviewer built a per-tool parity table and found **no Criticals**: all fifteen tools match their REST twins on authorization, on the `create`/`is_write` rate-limit flags, and on error mapping. What is left is a layer of honesty problems — the tool surface tells the calling model things that are not true.

### Task 18: Stop advertising `recall` and `reflect` as read-only

**R3-I-1.** `tools.py:209` and `:243` carry `annotations=ToolAnnotations(readOnlyHint=True)`, and the comment *immediately below* the `recall` annotation contradicts it: `create=True` means an unseen `project_slug` mints a `Project` row that permanently squats a tenant-unique slug (invariant 8: unique across live **and** retired names, never recoverable — "measured live at 80 projects in 5.1s against one key"). `readOnlyHint` is defined in MCP as "the tool does not modify its environment" and is the flag clients use to skip user confirmation and auto-approve tools inside agent loops.

Failure scenario: an agent with auto-approval for read-only tools — or one steered by injected text in a `get_document` result, which this codebase documents as retrievable — calls `recall(scope="project", project_slug="acme-payments", query="x")`. No confirmation is requested. The slug is now owned by that key forever; the real team gets `403 PROJECT_ACCESS_DENIED` on their own project name and can never reclaim it, not even by renaming.

**Files:**
- Modify: `src/memory/mcp/tools.py` (`recall`, `reflect`, `forget`, `restore`, `correct` annotations)
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Produces: a `MCP_READONLY_TOOLS` table in `tests/test_mcp_tools.py` alongside the existing `MCP_IS_WRITE_TABLE` / `MCP_CREATE_TABLE`, covered by the same "every registered tool has a row" guard.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools.py`, next to the existing security tables:

```python
# tool -> readOnlyHint it may advertise. The rule: a tool may claim
# readOnlyHint only if it neither writes upstream NOR creates local state.
# `recall`/`reflect` claimed it while running with create=True, which mints a
# Project row per unseen slug -- permanently, since invariant 8 makes slugs
# unique across live AND retired names. readOnlyHint is what an MCP client
# uses to skip confirmation and auto-approve inside an agent loop, so the
# annotation was actively inviting the squat (2026-08-23 review, R3-I-1).
MCP_READONLY_TABLE = {
    "retain": False,
    "sync_retain": False,
    "recall": False,
    "reflect": False,
    "list_memories": True,
    "get_memory": True,
    "forget": False,
    "correct": False,
    "restore": False,
    "list_documents": True,
    "get_document": True,
    "delete_document": False,
    "get_operation": True,
    "list_operations": True,
    "cancel_operation": False,
}


def test_the_readonly_table_covers_every_registered_tool():
    from memory.mcp.tools import REGISTRY

    assert set(MCP_READONLY_TABLE) == set(REGISTRY)


def test_no_tool_claims_readonly_while_creating_or_writing(mcp_server):
    """readOnlyHint must never be advertised by a tool that runs with
    create=True or is_write=True -- the two flags MCP_CREATE_TABLE and
    MCP_IS_WRITE_TABLE already pin."""
    tools = {t.name: t for t in mcp_server.list_tools()}
    for name, may_be_readonly in MCP_READONLY_TABLE.items():
        annotations = tools[name].annotations
        advertised = bool(annotations and annotations.readOnlyHint)
        assert advertised == may_be_readonly, (
            f"{name}: readOnlyHint={advertised}, expected {may_be_readonly}"
        )
        if advertised:
            assert not MCP_CREATE_TABLE[name], f"{name} creates but claims read-only"
            assert not MCP_IS_WRITE_TABLE[name], f"{name} writes but claims read-only"
```

> `mcp_server` is however `tests/test_mcp_tools.py` already builds a server for its existing surface tests — reuse that fixture rather than inventing one. Find it with `grep -n 'def mcp_server\|build_mcp' tests/test_mcp_tools.py`; if the file builds the server inline, follow that pattern instead.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_mcp_tools.py -k readonly -v`
Expected: FAIL — `recall: readOnlyHint=True, expected False`

- [ ] **Step 3: Drop the lie, and fix the annotations that disagree with their descriptions**

`src/memory/mcp/tools.py` — delete the `annotations=ToolAnnotations(readOnlyHint=True),` line from **`recall`** and from **`reflect`**, leaving the `description=` argument. Add above each:

```python
    @mcp.tool(
        # No readOnlyHint. `create=True` below mints a Project row for an
        # unseen slug -- permanent, since invariant 8 makes a slug unique
        # across live AND retired names. readOnlyHint is what a client uses
        # to skip confirmation and auto-approve inside an agent loop, so
        # advertising it here was an invitation to squat every slug an
        # injected prompt could name.
        description="Search memory and return the matching facts.",
    )
```

While in here, fix the three annotations whose semantics disagree with their own descriptions (**R3-M-3**):

- `forget` carries `destructiveHint=True` though its description says it is reversible and `restore` brings it back → change to `ToolAnnotations(idempotentHint=True)`.
- `restore` sets only `idempotentHint`, leaving `destructiveHint` at its spec default of **true** for a purely additive operation → `ToolAnnotations(idempotentHint=True, destructiveHint=False)`.
- `correct` — the one memory operation that irreversibly overwrites caller text — carries no annotations at all → `ToolAnnotations(destructiveHint=True)`.

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS

- [ ] **Step 5: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/mcp/tools.py tests/test_mcp_tools.py
git commit -m "fix(mcp): recall and reflect are not read-only, they mint projects"
```

---

### Task 19: Authenticate before validating, and never return outside the guard

**R3-I-6 + R3-M-2.** `_run` calls `body = body_factory()` **outside** `with tool_session(ctx)`, and `tool_session` is the only thing that reads the `Authorization` header. So every pydantic bound *and* `_check_content_size` executes for an unauthenticated caller. Verified: `retain(scope="user", content=<300 KB>)` with no header answers `CONTENT_TOO_LARGE: content exceeds 256000 bytes` where REST answers `401` — handing an unauthenticated party the exact configured value of `MEMORY_MAX_CONTENT_BYTES` plus an oracle for every request-model shape. Separately, `ToolResult(...)` is constructed *outside* the `try`, so a non-dict upstream body raises a pydantic `ValidationError` the SDK wraps verbatim as `f"Error executing tool {name}: {e}"` — including pydantic's `input_value=` repr of the upstream payload.

**Files:**
- Modify: `src/memory/mcp/tools.py:124-157`
- Test: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools.py`:

```python
def test_an_unauthenticated_tool_call_is_refused_before_validation():
    """REST resolves current_principal before any handler body runs. MCP built
    and validated the request model first, so an unauthenticated caller learned
    the configured MEMORY_MAX_CONTENT_BYTES value and got a free oracle for
    every request-model shape (2026-08-23 review, R3-I-6)."""
    from memory.mcp.tools import REGISTRY, MCPToolError

    class NoAuth:
        headers: dict = {}

    with pytest.raises(MCPToolError) as excinfo:
        REGISTRY["retain"](scope="user", content="x" * 300_000, ctx=NoAuth())

    assert excinfo.value.code == "UNAUTHORIZED", excinfo.value.code
    assert "256000" not in str(excinfo.value), (
        "the configured content limit leaked to an unauthenticated caller"
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_mcp_tools.py -k unauthenticated -v`
Expected: FAIL — the raised code is `CONTENT_TOO_LARGE`, not `UNAUTHORIZED`.

- [ ] **Step 3: Move validation inside the session, and the return inside the try**

`src/memory/mcp/tools.py`, replace the body of `_run` from `try:` to the final `return`:

```python
    try:
        with tool_session(ctx) as tc:
            # body_factory runs INSIDE the session, not before it:
            # tool_session is the only thing that reads the Authorization
            # header, so building the model first meant every pydantic bound
            # and _check_content_size executed for an unauthenticated caller.
            # REST resolves current_principal before any handler body runs;
            # this is the same ordering. It stays inside the same try, so the
            # ValidationError/DomainError mapping below is unchanged.
            body = body_factory()
            bank_id, resolved_from, slug = _resolve_bank(
                body, tc.db, tc.principal, None, action,
                create=create, is_write=is_write,
            )
            # Commit before the upstream call: resolution may have created the
            # project that owns this bank_id, and rolling that back after the
            # bank is materialized upstream orphans it unreachably.
            tc.db.commit()
            result = call(bank_id, tc.db, tc.principal, slug)
            # Constructed inside the try as well: ToolResult.result is typed
            # dict[str, Any], so an upstream 200 whose body is a JSON array or
            # scalar raises a pydantic ValidationError that the SDK dispatcher
            # would wrap verbatim -- including pydantic's `input_value=` repr
            # of the upstream payload.
            return ToolResult(
                result=_strip_bank_id(result, bank_id),
                project_slug=slug,
                resolved_from=resolved_from,
                notice="PROJECT_RENAMED" if resolved_from else None,
            )
    except DomainError as exc:
        raise MCPToolError(exc.code, exc.message, exc.details) from None
    except ValidationError as exc:
        raise MCPToolError("INVALID_REQUEST", _validation_message(exc)) from None
    except Exception as exc:
        logger.error("unhandled MCP tool error", exc_info=exc)
        raise MCPToolError("INTERNAL_ERROR", "internal error") from None
```

(keep the existing explanatory comments on the three `except` branches — they are accurate and load-bearing.)

- [ ] **Step 4: Make the validation message name its fields (R3-M-8)**

Replace `_validation_message`:

```python
def _validation_message(exc: ValidationError) -> str:
    """A pydantic error's own messages are safe to surface verbatim: they
    describe the shape of the caller's OWN input, never server state.

    `loc` is included for the same reason: it names the caller's own fields.
    Without it a multi-field failure read "Input should be 'user' or
    'project'; Input should be greater than or equal to 0" with no indication
    of WHICH arguments were wrong, so INVALID_REQUEST was unactionable.
    """
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in exc.errors()
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS. Some existing tests assert on `INVALID_REQUEST` message text — update those assertions to the new `field: message` shape rather than reverting the change.

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/mcp/tools.py tests/test_mcp_tools.py
git commit -m "fix(mcp): authenticate before validating a tool's arguments"
```

---

### Task 20: Build provenance before committing the project row

**R3-I-2.** REST orders it `_resolve_bank` → `provenance.build` → `db.commit()`. MCP's `_run` orders it `_resolve_bank` → `commit` → `call`, and `provenance.build` is the first statement inside `_retain`'s `call`. SPEC §13.4 says an attempted reserved-key overwrite "returns `INVALID_METADATA` and **nothing is written**". Over MCP, the project row is already committed when `build` raises — so a rejected `retain` still permanently creates and owns the project. The existing regression test uses `scope="user"`, which has no row to create, so it cannot see this.

**Files:**
- Modify: `src/memory/mcp/tools.py` (`_run` signature, `_retain`)
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 19's `_run` body.
- Produces: `_run(..., before_commit=None)` — an optional callable taking the resolved `slug` and running after `_resolve_bank` but before `db.commit()`. Only `_retain` passes one.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools.py`:

```python
def test_a_reserved_metadata_key_creates_no_project(mcp_headers, session, tenant):
    """SPEC §13.4: INVALID_METADATA and NOTHING IS WRITTEN. Over REST that
    holds -- provenance.build runs before db.commit(). Over MCP the commit came
    first, so a refused retain still created and permanently owned the project
    (2026-08-23 review, R3-I-2). The existing regression test uses scope="user",
    which has no row to create, so it could not see this."""
    from memory.mcp.tools import REGISTRY, MCPToolError
    from memory.models import Project

    slug = "reserved-key-probe"

    with pytest.raises(MCPToolError) as excinfo:
        REGISTRY["retain"](
            scope="project",
            project_slug=slug,
            content="x",
            metadata={"user_id": "someone"},
            ctx=mcp_headers,
        )
    assert excinfo.value.code == "INVALID_METADATA"

    assert (
        session.query(Project).filter(Project.project_slug == slug).one_or_none()
        is None
    ), "the refused retain committed a project row anyway"
```

> `mcp_headers` is whatever context object `tests/test_mcp_tools.py` already uses to carry a user key into a tool call — reuse it, do not invent one.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_mcp_tools.py -k reserved_metadata_key_creates_no_project -v`
Expected: FAIL — the project row exists.

- [ ] **Step 3: Add a pre-commit hook to `_run`**

`src/memory/mcp/tools.py`, extend the signature and call it in place:

```python
def _run(
    ctx: Context,
    body_factory,
    action: str,
    call,
    *,
    create: bool,
    is_write: bool = False,
    before_commit=None,
) -> ToolResult:
    """...

    `before_commit`, when given, receives the resolved project slug and runs
    after bank resolution and BEFORE the commit. Only `_retain` uses it, for
    `provenance.build`: SPEC §13.4 requires a reserved metadata key to raise
    with NOTHING written, and committing first meant a refused retain still
    permanently created and owned the project it named. REST gets this for
    free by calling build() between resolution and commit; this is the MCP
    equivalent of that ordering.
    """
```

and inside the `with tool_session(ctx) as tc:` block, between `_resolve_bank` and `tc.db.commit()`:

```python
            precomputed = before_commit(slug) if before_commit is not None else None
            tc.db.commit()
            result = call(bank_id, tc.db, tc.principal, slug, precomputed)
```

Update the `call` contract in the docstring: every closure now takes `(bank_id, db, principal, slug, precomputed)`. Add `precomputed` (unused) to all fourteen other `lambda bank, db, p, slug: ...` closures — `lambda bank, db, p, slug, _: ...`.

- [ ] **Step 4: Move `provenance.build` into the hook**

In `_retain`, replace the `call` closure:

```python
    def before_commit(slug):
        # `slug` is `_resolve_bank`'s RESOLVED project slug, not the raw
        # `project_slug` argument: None for scope=user (the argument is
        # meaningless there and must never be stamped into extraction
        # metadata), and the project's current, live slug for scope=project
        # even when the caller named a retired one.
        #
        # Runs BEFORE the commit so a reserved key raises with nothing
        # written (SPEC §13.4).
        return provenance.build(metadata, project_slug=slug)

    def call(bank_id, db, principal, slug, extraction):
        return get_client().retain(
            bank_id, content, document_id=document_id,
            metadata=extraction or None, context=provenance.context_line(extraction),
            update_mode=update_mode, is_async=is_async, operation_id=operation_id,
        )

    return _run(
        ctx, body_factory, "memory.retain", call,
        create=True, is_write=True, before_commit=before_commit,
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/mcp/tools.py tests/test_mcp_tools.py
git commit -m "fix(mcp): reject reserved metadata before committing the project"
```

---

### Task 21: Advertise the constraints the models already enforce

**R3-I-4 + R3-I-5 + R3-M-1.** The SDK derives each tool's JSON Schema from the *function signature*, and the bounds live on the pydantic models — so REST's OpenAPI publishes `update_mode` as `{"enum": ["replace","append"]}` while MCP publishes `{"type":"string","default":"replace"}`, and `state`'s enum and every `ge=0` vanish. The values are still enforced server-side, so this is not a hole; it is a correctness problem for the calling model, which is the audience the schema exists for. SPEC §11.4 blesses `update_mode="append"` for interactive coding sessions, and **nothing in the advertised schema tells a model `append` exists at all.** Separately, every tool validates a request model and then calls the client with the *raw arguments* (`state=state`, not `body.state`) — harmless today because no validator normalizes, and a live landmine the moment one does.

**Files:**
- Modify: `src/memory/mcp/tools.py` (all fifteen signatures + the `call` closures)
- Test: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tools.py`:

```python
def test_the_advertised_schema_carries_the_same_bounds_rest_publishes(mcp_server):
    """The SDK derives a tool's schema from the SIGNATURE, so bounds living
    only on the pydantic model never reach the model calling the tool. SPEC
    §11.4 blesses update_mode="append" for interactive coding sessions and
    nothing in the advertised schema said `append` existed (R3-I-4)."""
    schemas = {t.name: t.inputSchema for t in mcp_server.list_tools()}

    retain = schemas["retain"]["properties"]["update_mode"]
    assert set(retain.get("enum", [])) == {"replace", "append"}, retain

    state = schemas["list_memories"]["properties"]["state"]
    assert "valid" in str(state) and "invalidated" in str(state), state

    for tool in ("list_memories", "list_documents", "list_operations"):
        limit = schemas[tool]["properties"]["limit"]
        assert "1" in str(limit.get("minimum", limit)), (tool, limit)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_mcp_tools.py -k advertised_schema -v`
Expected: FAIL — `update_mode` has no `enum`.

- [ ] **Step 3: Type the signatures**

`src/memory/mcp/tools.py` — add the imports:

```python
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from memory.api.memory import MAX_PAGE_SIZE
```

then define the shared aliases beside `Scope`:

```python
# The tool SIGNATURE is what the SDK turns into the advertised JSON Schema,
# so a bound that lives only on the pydantic model is invisible to the model
# calling the tool. These aliases keep the two in step: same vocabulary REST's
# OpenAPI publishes for the identical operation.
UpdateMode = Literal["replace", "append"]
MemoryState = Literal["valid", "invalidated"]
FactType = Literal["world", "experience", "observation"]
PageLimit = Annotated[int | None, Field(ge=1, le=MAX_PAGE_SIZE)]
PageOffset = Annotated[int | None, Field(ge=0)]
```

Apply them:

- `retain` and `sync_retain`: `update_mode: UpdateMode = "replace"`
- `list_memories`: `state: MemoryState | None = None`, `type: FactType | None = None`, `limit: PageLimit = None`, `offset: PageOffset = None`
- `list_documents`, `list_operations`: `limit: PageLimit = None`, `offset: PageOffset = None`
- `list_operations`: give `status` a description in the tool's `description=` text — its vocabulary is Hindsight's and this service does not model it, so an enum here would be a guess.

- [ ] **Step 4: Thread the validated body into every call (R3-M-1)**

`_run`'s `call` closures currently receive `(bank_id, db, principal, slug, precomputed)` and read the raw arguments from the enclosing scope. Change the contract to pass `body` as well, and read every field off it:

```python
            result = call(bank_id, tc.db, tc.principal, slug, precomputed, body)
```

then in each tool, e.g. `list_memories`:

```python
            lambda bank, db, p, slug, _, body: get_client().list_memories(
                bank, q=body.q, type=body.type, state=body.state,
                document_id=body.document_id, limit=body.limit, offset=body.offset,
            ),
```

REST always passes `body.*`; MCP passed the raw arguments at every site. Today the models are non-coercing so behaviour is identical — but the moment any validator normalizes a value (lowercases a `state`, canonicalizes a `document_id`, strips `content`), MCP would silently keep the un-normalized original while REST used the normalized one. This project's own history (`fix(mcp): route correct through CorrectRequest`) is that class of bug; threading `body` closes the class rather than the instance.

- [ ] **Step 5: Fix the page-size divergence (R3-I-5)**

`_list_documents` and `_list_operations` omit unset `limit`/`offset` from the client call, so Hindsight's own default applies — while REST's models default to concrete `100`/`20` and always send them. Same credential, same bank, different counts on the two surfaces. With Step 4 done, the fix falls out: the closures now read `body.limit`/`body.offset`, which carry the model's resolved defaults. Delete the `kwargs`-building blocks in both helpers and construct the models directly:

```python
    def body_factory() -> ListDocumentsRequest:
        # No conditional kwargs: the model's own defaults (100/0) are what
        # REST sends, and reading them back off `body` in `call` is what makes
        # the two surfaces agree. Omitting them here meant MCP got Hindsight's
        # default page size and REST got ours -- an agent paginating by "did I
        # get fewer than the page size" drew opposite conclusions per surface.
        return ListDocumentsRequest(
            scope=scope, project_slug=project_slug, git_locator=git_locator,
            q=q, **({"limit": limit} if limit is not None else {}),
            **({"offset": offset} if offset is not None else {}),
        )
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS. Tests asserting the old omit-when-unset wire behaviour need updating to the REST default — that is the point of the change; state it in the commit message.

- [ ] **Step 7: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/mcp/tools.py tests/test_mcp_tools.py
git commit -m "fix(mcp): advertise the enums and bounds the models enforce"
```

---

## Phase 5 — Concurrency and domain rules

### Task 22: Give `rename()` the savepoint `create()` already has

**R1-#1.** `create()` wraps its check-then-act in `db.begin_nested()` and maps `IntegrityError` to `ProjectSlugConflict`, with a comment explaining exactly why. `rename()` does neither — yet it mutates through **two** unique constraints: `projects (tenant_id, project_slug)` and the `retired_slugs` composite primary key. Nothing catches `IntegrityError`, so `api/app.py`'s catch-all turns a lost race into `500 INTERNAL_ERROR` where §18 requires `PROJECT_SLUG_CONFLICT`. Worse: `get_session` rolls the whole request back, so a PATCH that also carried a `git_locator` loses the locator repair too.

**Files:**
- Modify: `src/memory/projects.py:231-268`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `tests/test_projects.py`'s existing `_force_race` helper (around line 445) — it already exists and can drive this deterministically.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_projects.py`:

```python
def test_a_lost_rename_race_is_a_conflict_not_a_500(session, tenant):
    """create() got a savepoint and an IntegrityError -> ProjectSlugConflict
    mapping; rename() got neither, though it mutates through TWO unique
    constraints (projects and retired_slugs). SPEC §18 names "rename to an
    existing live or retired slug" as PROJECT_SLUG_CONFLICT, and it was a 500
    (2026-08-23 review, R1-#1)."""
    import pytest

    from memory import projects
    from memory.errors import ProjectSlugConflict

    alice = _principal(session, tenant)  # reuse this file's own helper
    projects.create(session, alice, "payments-api", "user", alice.user_id)
    session.commit()

    project = projects.resolve(session, alice, "payments-api").project

    # Simulate the winner: another session already took the target slug.
    _force_race(session, tenant, taken_slug="payments")

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, alice, project, "payments")
```

> `_principal` / `_force_race` are this file's existing helpers — read `tests/test_projects.py` around line 445 first and match their real signatures rather than the sketch above.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_projects.py -k lost_rename_race -v`
Expected: FAIL — `sqlalchemy.exc.IntegrityError` escapes instead of `ProjectSlugConflict`.

- [ ] **Step 3: Wrap the mutation**

`src/memory/projects.py`, replace the body of `rename()` from `old_slug = ...` to the `db.add(...)` call:

```python
    old_slug = project.project_slug

    # A savepoint, exactly like create() four functions up, and for the same
    # reason: `_slug_taken` above is a check-then-act, and TWO unique
    # constraints can lose the race here -- projects (tenant_id,
    # project_slug) and retired_slugs' composite PK. Unguarded, the loser's
    # IntegrityError reached api/app.py's catch-all as a 500 where SPEC §18
    # requires PROJECT_SLUG_CONFLICT, and get_session's rollback also
    # discarded any git_locator repair carried in the same PATCH.
    try:
        with db.begin_nested():
            project.project_slug = new_slug
            # Nothing repoints existing tombstones, and nothing needs to: a
            # rename mutates the slug on the same Project row, so internal_id
            # never changes. Every tombstone already points at the row, so
            # resolution after a chain of renames is still ONE lookup -- no
            # transitive walk, no cycles.
            db.add(
                RetiredSlug(
                    tenant_id=principal.tenant_id,
                    retired_slug=old_slug,
                    project_internal_id=project.internal_id,
                )
            )
            db.flush()
    except IntegrityError as exc:
        raise ProjectSlugConflict("that slug is taken", project_slug=new_slug) from exc
```

The `db.flush()` inside the savepoint is required: without it the constraint violation surfaces at the caller's `commit()`, outside the `except`.

- [ ] **Step 4: Run the test and the suite**

Run: `uv run pytest tests/test_projects.py -v` → PASS
Run: `uv run pytest -m "not integration" -q`

- [ ] **Step 5: Commit**

```bash
git add src/memory/projects.py tests/test_projects.py
git commit -m "fix(projects): savepoint the rename, a lost race was a 500"
```

---

### Task 23: Make the master key's rate limit fair, and bound its configuration

**R1-#3 + R1 minors.** `check()` keys on `principal.key_id or MASTER_KEY_ID`, collapsing *all* master traffic into one 60-writes/60s window. But SPEC §16.5 says ACH "may call operations directly with the master key plus `on_behalf_of` when acting for a human" — the master key is the shared credential for every ACH-mediated user. Twenty developers × 4 writes/min = 80/min against a 60/min ceiling, and every one of them gets `429` with no offending credential to point at. The delegated path is 20× stricter than the direct one, which is backwards. Separately: `MEMORY_WRITE_LIMIT=0` — the natural spelling of "block all writes" — makes `len(hits) >= 0` true on an empty deque, then `hits[0]` raises `IndexError` → 500 on every write instead of 429. And a master hash pasted from `sha256sum` carries a trailing `  -`, one read from a mounted Secret carries `\n`, PowerShell's is uppercase — each silently authenticating nothing, on the one credential whose failure blocks all provisioning.

**Files:**
- Modify: `src/memory/ratelimit.py`, `src/memory/config.py`
- Modify: `src/memory/api/memory.py` (`_resolve_bank`'s `ratelimit.check` call)
- Test: `tests/test_ratelimit.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `ratelimit.check(principal, on_behalf_of=None) -> None`. `_resolve_bank` already holds `on_behalf_of` at the call site, so this is a parameter and a format string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ratelimit.py`:

```python
def test_delegated_master_traffic_is_bucketed_per_subject(monkeypatch):
    """SPEC §16.5: ACH calls with the master key plus On-Behalf-Of when acting
    for a human, so the master key is the SHARED credential for every
    ACH-mediated user -- not one operator's key. One bucket for all of it means
    20 developers share a 60/min ceiling while each direct user key gets its
    own, making the delegated path 20x stricter than the direct one and letting
    one runaway agent 429 everybody (2026-08-23 review, R1-#3)."""
    import pytest

    from memory import ratelimit
    from memory.auth.principal import Principal
    from memory.errors import RateLimited

    limiter = ratelimit.Limiter(limit=1, window_seconds=60)
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: limiter)

    master = Principal(tenant_id="default", user_id=None, is_master=True, key_id=None)

    ratelimit.check(master, on_behalf_of="alice")
    with pytest.raises(RateLimited):
        ratelimit.check(master, on_behalf_of="alice")

    # Bob is a different human behind the same master key.
    ratelimit.check(master, on_behalf_of="bob")


def test_the_limiter_is_keyed_per_credential_through_a_route(
    client, master_headers, tenant, monkeypatch
):
    """tests above exercise Limiter directly and never touch check()'s key
    derivation. Mutating `principal.key_id or MASTER_KEY_ID` to a constant
    survived the whole suite: every credential in the tenant would then share
    one bucket -- a trivial tenant-wide DoS contradicting §20's "per
    credential" MUST (2026-08-23 review, R4-I3)."""
    import httpx
    import respx

    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()

    def _key() -> dict[str, str]:
        uid = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
        secret = client.post(
            f"/v1/users/{uid}/keys", json={}, headers=master_headers
        ).json()["key"]
        return {"Authorization": f"Bearer {secret}"}

    alice, bob = _key(), _key()

    with respx.mock:
        respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
            return_value=httpx.Response(200, json={})
        )
        body = {"scope": "user", "content": "x"}
        assert client.post("/v1/memory/retain", json=body, headers=alice).status_code == 200
        assert client.post("/v1/memory/retain", json=body, headers=alice).status_code == 429
        # Bob's own bucket must be untouched.
        assert client.post("/v1/memory/retain", json=body, headers=bob).status_code == 200
```

Append to `tests/test_config.py`:

```python
def test_a_zero_write_limit_is_refused(monkeypatch):
    """MEMORY_WRITE_LIMIT=0 is the natural spelling of "block all writes" and
    made Limiter.check evaluate len(hits) >= 0 -> True on an empty deque, then
    IndexError on hits[0] -> 500 on every write instead of 429."""
    import pytest
    from pydantic import ValidationError

    from memory.config import Settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_a_master_hash_with_stray_whitespace_still_authenticates(monkeypatch):
    """`echo -n k | sha256sum` appends "  -"; a hash read from a mounted Secret
    carries "\\n"; PowerShell's Get-FileHash is uppercase. Each silently
    produced a master key that authenticates nothing, indistinguishable from a
    wrong key -- on the one credential whose failure blocks all provisioning."""
    from memory.auth import keys
    from memory.config import Settings

    real = keys.hash_key("some-master-key")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", f"  {real.upper()}\n")
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/m")
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://h:8888")

    assert keys.verify_key("some-master-key", Settings().master_key_hash)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_ratelimit.py tests/test_config.py -v`
Expected: FAIL on all four new tests.

- [ ] **Step 3: Key the master bucket by its delegated subject**

`src/memory/ratelimit.py`:

```python
def check(principal: Principal, on_behalf_of: str | None = None) -> None:
    """Rate-limit one write attributed to `principal`.

    A user key is its own bucket. The master key is NOT one operator's
    credential: SPEC §16.5 has ACH calling with the master key plus
    On-Behalf-Of when acting for a human, so one shared bucket meant N
    developers behind ACH split a single per-credential ceiling while each
    direct user key got a whole one -- the delegated path Nx stricter than
    the direct one, and one runaway agent 429ing every ACH user.

    `on_behalf_of` is unverified provenance and never authorization evidence
    -- but the master key is trusted wholesale by §20.3 anyway, so using it
    for FAIRNESS costs nothing: the worst a forged value can do is give the
    forger their own bucket, which is what an honest value does too.
    """
    if principal.key_id:
        get_limiter().check(principal.key_id)
    elif on_behalf_of:
        get_limiter().check(f"{MASTER_KEY_ID}:{on_behalf_of}")
    else:
        get_limiter().check(MASTER_KEY_ID)
```

`src/memory/api/memory.py`, in `_resolve_bank`:

```python
    if is_write:
        ratelimit.check(principal, on_behalf_of)
```

- [ ] **Step 4: Bound the configuration**

`src/memory/config.py`:

```python
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
```

```python
    # ge=1: MEMORY_WRITE_LIMIT=0 is the natural spelling of "block all
    # writes" and instead made Limiter.check evaluate `len(hits) >= 0` as
    # True on an empty deque, then IndexError on `hits[0]` -- a 500 on every
    # write rather than the 429 the operator asked for.
    write_limit: int = Field(default=60, ge=1)
    write_window_seconds: float = Field(default=60.0, gt=0)

    @field_validator("master_key_hash")
    @classmethod
    def _normalize_hash(cls, value: str) -> str:
        """A hex digest compared verbatim was a whole class of silent outage.

        `echo -n k | sha256sum` appends "  -"; a value read from a mounted
        Secret carries a trailing newline; PowerShell's Get-FileHash is
        uppercase. Each produced a master key that never authenticates,
        indistinguishable from a wrong key -- on the one credential whose
        failure blocks all provisioning. Normalizing once here removes the
        class; `keys.verify_key` still does the constant-time compare.
        """
        return value.strip().split()[0].lower() if value.strip() else value
```

- [ ] **Step 5: Run the tests and the suite**

Run: `uv run pytest tests/test_ratelimit.py tests/test_config.py -v` → PASS
Run: `uv run pytest -m "not integration" -q`

- [ ] **Step 6: Commit**

```bash
git add src/memory tests/test_ratelimit.py tests/test_config.py
git commit -m "fix(ratelimit): bucket delegated master writes per subject"
```

---

### Task 24: Two small authorization-shape corrections

**R1 minors.** `banks.resolve_user_bank` answers `403` for a master key with a typo'd `user_id`. The "no existence signal either way" rationale is right for a *user* key and backwards for a *master* key, which §20.3 says bypasses ownership inside its tenant and which §18 gives `USER_NOT_FOUND` for precisely this case. And `principal.py:25`'s `Bearer` prefix match is case-sensitive, while RFC 7235 makes the auth scheme case-insensitive — a client sending `bearer <key>` gets "missing or malformed Authorization header" with no way to tell that from a bad key.

**Files:**
- Modify: `src/memory/banks.py:27-30`, `src/memory/auth/principal.py:25-28`
- Test: `tests/test_principal.py`, `tests/test_memory_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_principal.py`:

```python
def test_the_bearer_scheme_is_case_insensitive(session, tenant):
    """RFC 7235 makes the auth scheme case-insensitive. `bearer <key>` got
    "missing or malformed Authorization header", indistinguishable from a bad
    key."""
    from memory.auth.principal import resolve_principal

    # Build a real key however this file's existing tests do, then:
    principal = resolve_principal(f"bearer {plaintext}", session)
    assert principal.user_id == user_id
```

Append to `tests/test_memory_api.py`:

```python
def test_a_master_key_naming_an_unknown_user_gets_user_not_found(
    client, master_headers, tenant
):
    """"No existence signal either way" is right for a USER key and backwards
    for a master key: §20.3 gives it tenant-wide bypass and §18 gives
    USER_NOT_FOUND for exactly this case, so 403 sent an operator with a typo
    looking for a permissions problem that does not exist."""
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": "usr_definitely_not_here", "query": "x"},
        headers=master_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_principal.py tests/test_memory_api.py -k "case_insensitive or unknown_user" -v`
Expected: FAIL — `401` and `403` respectively.

- [ ] **Step 3: Split the branch and relax the prefix**

`src/memory/banks.py`:

```python
    user = db.get(User, target_id)
    if user is None or user.tenant_id != principal.tenant_id:
        if principal.is_master:
            # A master key already bypasses ownership inside its tenant
            # (§20.3), so there is no existence fact to withhold from it --
            # and §18 names USER_NOT_FOUND for exactly this case. Answering
            # 403 sent an operator with a typo hunting a permissions problem
            # that does not exist.
            raise UserNotFound(user_id=target_id)
        # For a user key the shape stays: same as a cross-tenant miss, no
        # existence signal either way.
        raise Forbidden("no accessible memory for the requested user")
```

with `UserNotFound` added to the `memory.errors` import.

`src/memory/auth/principal.py`:

```python
BEARER = "bearer "


def resolve_principal(authorization: str | None, db: Session) -> Principal:
    # RFC 7235 makes the auth scheme case-insensitive. A client sending
    # `bearer <key>` used to get "missing or malformed Authorization header",
    # which is indistinguishable from a bad key.
    if not authorization or not authorization.lower().startswith(BEARER):
        raise Unauthorized("missing or malformed Authorization header")

    plaintext = authorization[len(BEARER) :].strip()
```

- [ ] **Step 4: Run the tests and the suite**

Run: `uv run pytest -m "not integration" -q`

Note: `tests/test_users_api.py` has a block asserting the cross-tenant *user-key* shape — those must still be `403`. If any now expects 404 for a user key, the branch is wrong way round.

- [ ] **Step 5: Commit**

```bash
git add src/memory/banks.py src/memory/auth/principal.py tests
git commit -m "fix(auth): 404 for a master key's unknown user, accept lowercase bearer"
```

---

## Phase 6 — Hindsight client robustness

### Task 25: Give the LLM-bound calls their own timeout

**R3-I-3.** One 30 s `httpx` timeout covers both a cheap GET and `sync_retain`, which blocks until Hindsight has run fact extraction through an LLM. `docs/PROJECT-STATE.md` records `kubeai.gpt-oss-20b` as timing out outright, so 30 s is not comfortably above the ceiling. A `ReadTimeout` is caught as an `httpx.HTTPError`, `response` is `None`, and the caller gets `HINDSIGHT_ERROR (502) "memory backend unreachable"` — a code whose entire meaning to an agent is "the backend is unwell, retry". Hindsight's in-process worker completes the original write anyway, the agent retries, and the bank holds the document twice. `retain` has `operation_id` for exactly this; `sync_retain`'s description gives the model no reason to supply one.

**Files:**
- Modify: `src/memory/config.py`, `src/memory/hindsight/client.py`
- Modify: `src/memory/mcp/tools.py` (`sync_retain`'s description)
- Test: `tests/test_hindsight_client.py`

**Interfaces:**
- Produces: `Settings.hindsight_timeout_seconds` (default 30.0) and `Settings.hindsight_llm_timeout_seconds` (default 180.0). `HindsightClient.retain(..., is_async=False)` and `.reflect(...)` use the latter.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hindsight_client.py`:

```python
def test_the_llm_bound_calls_get_a_longer_read_timeout():
    """sync_retain blocks until Hindsight has run extraction through an LLM and
    reflect is a full synthesis call, but both shared the 30s timeout a cheap
    GET uses. A ReadTimeout surfaces as HINDSIGHT_ERROR 502 -- "retry" -- while
    the upstream worker completes the original write anyway, so the retry
    duplicates it (2026-08-23 review, R3-I-3)."""
    import respx

    from memory.hindsight.client import get_client

    get_client.cache_clear()
    client = get_client()

    with respx.mock:
        route = respx.post(url__regex=r".*/memories$").respond(200, json={})
        client.retain("user_x", "content", is_async=False)
        assert route.calls.last.request.extensions["timeout"]["read"] >= 180

        route2 = respx.post(url__regex=r".*/reflect$").respond(200, json={})
        client.reflect("user_x", "q")
        assert route2.calls.last.request.extensions["timeout"]["read"] >= 180

    with respx.mock:
        route3 = respx.post(url__regex=r".*/memories/list$").respond(200, json={})
        client.list_memories("user_x")
        assert route3.calls.last.request.extensions["timeout"]["read"] <= 30
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_hindsight_client.py -k llm_bound -v`
Expected: FAIL — every read timeout is 30.

- [ ] **Step 3: Add the settings**

`src/memory/config.py`:

```python
    # Two timeouts, not one. A cheap GET and `sync_retain` are not the same
    # call: sync_retain blocks until Hindsight has run fact extraction through
    # an LLM, and `reflect` is a full synthesis. PROJECT-STATE records one
    # model as "works, slower" and another as timing out outright, so a shared
    # 30s ceiling turned a slow-but-succeeding write into a 502 -- a code that
    # means "retry" to an agent, while the upstream worker finished the
    # original write anyway and the retry duplicated it.
    hindsight_timeout_seconds: float = Field(default=30.0, gt=0)
    hindsight_llm_timeout_seconds: float = Field(default=180.0, gt=0)
```

- [ ] **Step 4: Wire them into the client**

`src/memory/hindsight/client.py`:

```python
    def __init__(self, base_url: str, api_key: str, tenant_id: str) -> None:
        ...
        settings = get_settings()
        self._default_timeout = httpx.Timeout(
            settings.hindsight_timeout_seconds, connect=5.0
        )
        # Connect stays short: an unreachable backend must fail fast whatever
        # the call is. Only the READ side is extended, and only for the calls
        # that actually wait on a model.
        self._llm_timeout = httpx.Timeout(
            settings.hindsight_llm_timeout_seconds, connect=5.0
        )
        self._http = httpx.Client(
            base_url=base_url, headers=headers, timeout=self._default_timeout
        )
```

Extend `_request` with a `timeout` keyword and forward it:

```python
    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        not_found: type[DomainError] | None = None,
        bad_request: type[DomainError] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict:
        response = None
        try:
            response = self._http.request(
                method, path, json=payload, params=params,
                timeout=timeout or self._default_timeout,
            )
```

then pass `timeout=self._llm_timeout` from `retain(...)` when `is_async is False`, and from `reflect(...)`.

- [ ] **Step 5: Tell the model how to retry safely**

`src/memory/mcp/tools.py`, `sync_retain`'s description:

```python
        description=(
            "Store something and wait until it is searchable. Slower than "
            "retain because it blocks on extraction. Pass the same "
            "operation_id if you retry: without one, a retry after a timeout "
            "stores the content twice."
        ),
```

- [ ] **Step 6: Run the tests and commit**

Run: `uv run pytest tests/test_hindsight_client.py -v` → PASS
Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory tests/test_hindsight_client.py
git commit -m "fix(hindsight): longer read timeout for the LLM-bound calls"
```

---

### Task 26: Stop assuming every success carries a JSON body

**R3-M-5 + R2's out-of-slice note.** `_request` calls `.json()` unconditionally on any status below 400, with a return type annotated `-> dict`. A `204 No Content` (plausible for `DELETE .../documents/{id}`, `DELETE .../operations/{id}`, `PUT .../banks/{id}`), a `307` (the client is built with the default `follow_redirects=False`, so a trailing-slash redirect surfaces as a bodiless 3xx), or an HTML error page from an intermediary all raise `json.JSONDecodeError` — which is **not** an `httpx.HTTPError`, so it walks past the handler and becomes `INTERNAL_ERROR`. Not reachable against today's Hindsight, so this is an upgrade hazard rather than a live bug — which is exactly the kind that surfaces during an incident.

Also **R3-M-6**: `HindsightError.details` carries `upstream_status` to the caller, so an upstream `401`/`403` (our own API key misconfigured) is reported to an untrusted MCP client as `HINDSIGHT_ERROR: ... {'upstream_status': 401}`. No bank id leaks, so §18's constraint holds — but the backend's auth state is not the caller's business.

**Files:**
- Modify: `src/memory/hindsight/client.py:145-159`
- Test: `tests/test_hindsight_client.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize(
    "status,body,content_type",
    [
        (204, b"", None),
        (200, b"", "application/json"),
        (307, b"", None),
        (200, b"<html>gateway</html>", "text/html"),
    ],
)
def test_a_bodiless_or_non_json_success_is_not_an_internal_error(
    status, body, content_type
):
    """`.json()` was called unconditionally on anything below 400. A
    JSONDecodeError is not an httpx.HTTPError, so it walked past _request's
    handler and became INTERNAL_ERROR -- a code outside every branch a caller
    could reasonably handle (2026-08-23 review, R3-M-5)."""
    import respx

    from memory.errors import HindsightError
    from memory.hindsight.client import get_client

    get_client.cache_clear()
    client = get_client()

    headers = {"content-type": content_type} if content_type else {}
    with respx.mock:
        respx.delete(url__regex=r".*/documents/.*").respond(
            status, content=body, headers=headers
        )
        try:
            result = client.delete_document("user_x", "d1")
        except HindsightError:
            pass  # a typed refusal is acceptable
        else:
            assert isinstance(result, dict)


def test_an_upstream_auth_failure_does_not_report_its_status_to_the_caller():
    """An upstream 401 means OUR MEMORY_HINDSIGHT_API_KEY is misconfigured.
    Reporting `{'upstream_status': 401}` to an untrusted MCP client tells it
    about the backend's auth state, which is not the caller's business."""
    import pytest as _pytest
    import respx

    from memory.errors import HindsightError
    from memory.hindsight.client import get_client

    get_client.cache_clear()
    with respx.mock:
        respx.post(url__regex=r".*/memories$").respond(401, json={"detail": "nope"})
        with _pytest.raises(HindsightError) as excinfo:
            get_client().retain("user_x", "c")

    assert "401" not in str(excinfo.value.details), excinfo.value.details
```

- [ ] **Step 2: Run and watch fail**

Run: `uv run pytest tests/test_hindsight_client.py -k "bodiless or upstream_auth" -v`
Expected: FAIL

- [ ] **Step 3: Narrow the success path**

`src/memory/hindsight/client.py`, replace the tail of `_request`:

```python
        if response.status_code >= 400:
            # upstream_status is deliberately NOT in details any more: an
            # upstream 401/403 means OUR MEMORY_HINDSIGHT_API_KEY is
            # misconfigured, and that is not something an untrusted MCP client
            # should learn. It is logged instead, where operators can see it.
            logger.warning(
                "hindsight rejected the request: %s", response.status_code
            )
            raise HindsightError("memory backend rejected the request")

        if response.status_code >= 300:
            # follow_redirects is False by default, so a trailing-slash
            # redirect arrives here as a bodiless 3xx. Treating it as success
            # meant .json() raised JSONDecodeError -- not an httpx.HTTPError,
            # so it walked past the handler above and became INTERNAL_ERROR.
            logger.warning("hindsight redirected: %s", response.status_code)
            raise HindsightError("memory backend rejected the request")

        if not response.content:
            # 204 No Content is a legitimate success for a DELETE. An empty
            # body is an empty result, not a parse failure.
            return {}

        try:
            return response.json()
        except ValueError:
            # An HTML error page from an intermediary, or any other non-JSON
            # 2xx. Same reasoning as above: a decode failure is a backend
            # problem, and it must arrive as HINDSIGHT_ERROR rather than as
            # the catch-all's INTERNAL_ERROR.
            logger.warning("hindsight returned a non-JSON success body")
            raise HindsightError("memory backend returned an unreadable response")
```

- [ ] **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_hindsight_client.py -v` → PASS

Some existing tests assert `details == {"upstream_status": ...}` — update them; that disclosure is what this change removes.

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/hindsight/client.py tests/test_hindsight_client.py
git commit -m "fix(hindsight): handle bodiless and non-JSON successes"
```

---

### Task 27: Reject percent-encoded separators in path ids

**R3-M-4.** `_reject_path_traversal` splits on a literal `/` and checks for `.`/`..`, so `document_id = "..%2f..%2fmemories"` passes. Verified: httpx preserves `%2f` in the merged path, and Starlette's `{document_id}` route regex is `[^/]+` with no dot-segment removal after uvicorn decodes it — so this is **not** exploitable against the current upstream, only against a future proxy or router that normalizes. The docstring's claim that the value resolves to "exactly one opaque path segment" is nonetheless stronger than the check.

**Files:**
- Modify: `src/memory/hindsight/paths.py:47-54`
- Test: `tests/test_hindsight_paths.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize(
    "document_id",
    ["..%2f..%2fmemories", "%2e%2e/memories", "a%2Fb", "%2E%2E%2Fx"],
)
def test_percent_encoded_separators_are_refused(document_id):
    """The guard split on a literal "/" and checked for "."/"..", so an
    encoded separator sailed through. Not exploitable against hindsight-api
    0.9.1 (httpx preserves %2f and Starlette does no dot-segment removal), but
    the docstring promises "exactly one opaque path segment" and the check was
    weaker than the promise (2026-08-23 review, R3-M-4)."""
    import pytest as _pytest

    from memory.errors import DocumentNotFound
    from memory.hindsight.paths import reject_document_traversal

    with _pytest.raises(DocumentNotFound):
        reject_document_traversal(document_id)
```

- [ ] **Step 2: Run and watch fail**

Run: `uv run pytest tests/test_hindsight_paths.py -k percent_encoded -v`
Expected: FAIL — no exception raised.

- [ ] **Step 3: Add the two patterns**

`src/memory/hindsight/paths.py`, inside `_reject_path_traversal`, after the existing control-character check:

```python
    # Percent-encoded separators and dots. The checks above split on a LITERAL
    # "/" and match literal "."/"..", so "..%2f..%2fmemories" passed them all.
    # Not exploitable against hindsight-api 0.9.1 -- httpx preserves %2f and
    # neither Starlette nor httpx applies dot-segment removal after decoding --
    # but this function's docstring promises the value resolves to exactly one
    # opaque path segment, and a future proxy or router that normalizes would
    # make the promise load-bearing. Two patterns, no behaviour change for any
    # legitimate id (SPEC §11.4's examples are colon-separated, never encoded).
    lowered = value.lower()
    if "%2f" in lowered or "%2e" in lowered:
        raise not_found("no such object in this memory")
```

and extend the docstring's charset paragraph to say so.

- [ ] **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_hindsight_paths.py -v` → PASS
Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/hindsight/paths.py tests/test_hindsight_paths.py
git commit -m "fix(paths): refuse percent-encoded separators in path ids"
```

---
## Phase 7 — Test hardening

The reviewer ran **17 real source mutations** against an isolated copy of the repo with its own database. Ten died exactly where the docstrings said they would — this suite's architecture is genuinely strong. Five survived. Task 4 closed the worst; these are the rest.

### Task 28: Extend the governance table to the three routers it never covered

**R4-I2.** `tests/test_governance_ratelimit.py` exists precisely to close this class — its module docstring records that `sed -i 's/is_write=True/is_write=False/g'` across `directives.py`/`mental_models.py` left 437 tests passing. The same technique was never applied to curation/documents/operations. Verified: mutating those three files' five `is_write=True` flags left **504 passed**. Unmetered as a result: `forget`, `restore`, `correct`, `documents/delete`, `operations/cancel`. `correct` writes caller-supplied text into the bank; `delete_document` is SPEC §12.2's only hard-delete lever. Their MCP twins *are* covered by `MCP_IS_WRITE_TABLE`, which is what made this easy to overlook.

**Files:**
- Modify: `tests/test_governance_ratelimit.py`

**Interfaces:**
- Consumes: the module's existing `juan` fixture and `GOVERNANCE_ROUTES` shape `(method, path, json_body, params, expected_is_write)`.

- [ ] **Step 1: Widen the coverage guard first**

The guard at `tests/test_governance_ratelimit.py:79` only looks at `/v1/directives` and `/v1/mental-models`. Widen it so a new write route in any of the five routers cannot land unverified:

```python
def test_the_governance_table_covers_every_route_in_all_five_files(client):
    """A new route landing in any of these files without an entry here must
    fail loudly, not be silently unverified by the test below. Originally
    scoped to directives + mental-models only, which is why curation,
    documents and operations went uncovered -- five is_write=True flags were
    individually deletable with the suite green (2026-08-23 review, R4-I2)."""
    schema = client.get("/openapi.json").json()
    prefixes = (
        "/v1/directives",
        "/v1/mental-models",
        "/v1/memory/list",
        "/v1/memory/get",
        "/v1/memory/forget",
        "/v1/memory/restore",
        "/v1/memory/correct",
        "/v1/memory/documents",
        "/v1/memory/operations",
    )
    routes = {
        f"{method.upper()} {path}"
        for path, ops in schema["paths"].items()
        if path.startswith(prefixes)
        for method in ops
    }
    covered = {
        f"{method} {path}"
        for method, path, _, _, _ in GOVERNANCE_ROUTES.values()
    }
    assert routes == covered, (
        f"uncovered: {sorted(routes - covered)}; stale: {sorted(covered - routes)}"
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_governance_ratelimit.py -k covers_every_route -v`
Expected: FAIL, listing the nine uncovered curation/document/operation routes.

- [ ] **Step 3: Add the nine rows**

Append to `GOVERNANCE_ROUTES`:

```python
    # curation, documents, operations -- the three routers the original table
    # never reached. `correct` writes caller text into the bank and
    # `delete_document` is SPEC §12.2's only hard-delete lever; both were
    # unmetered.
    "memory.list": ("POST", "/v1/memory/list", {"scope": "user"}, None, False),
    "memory.get": (
        "POST", "/v1/memory/get",
        {"scope": "user", "memory_id": MEM_ID}, None, False,
    ),
    "memory.forget": (
        "POST", "/v1/memory/forget",
        {"scope": "user", "memory_id": MEM_ID}, None, True,
    ),
    "memory.restore": (
        "POST", "/v1/memory/restore",
        {"scope": "user", "memory_id": MEM_ID}, None, True,
    ),
    "memory.correct": (
        "POST", "/v1/memory/correct",
        {"scope": "user", "memory_id": MEM_ID, "content": "fixed"}, None, True,
    ),
    "documents.list": (
        "POST", "/v1/memory/documents/list", {"scope": "user"}, None, False,
    ),
    "documents.get": (
        "POST", "/v1/memory/documents/get",
        {"scope": "user", "document_id": "d1"}, None, False,
    ),
    "documents.delete": (
        "POST", "/v1/memory/documents/delete",
        {"scope": "user", "document_id": "d1"}, None, True,
    ),
    "operations.list": (
        "POST", "/v1/memory/operations/list", {"scope": "user"}, None, False,
    ),
    "operations.get": (
        "POST", "/v1/memory/operations/get",
        {"scope": "user", "operation_id": OP_ID}, None, False,
    ),
    "operations.cancel": (
        "POST", "/v1/memory/operations/cancel",
        {"scope": "user", "operation_id": OP_ID}, None, True,
    ),
```

with the two ids beside the existing `DIR_ID` / `MM_ID`:

```python
MEM_ID = "33333333-3333-3333-3333-333333333333"
OP_ID = "44444444-4444-4444-4444-444444444444"
```

- [ ] **Step 4: Tighten the read assertion (R4 minor)**

The read branch at the end of `test_rest_is_write_flags_match_the_governance_table` asserts only `!= 429`, so a route regressing to 500 passes:

```python
        else:
            assert response.status_code != 429, (name, response.text)
            # != 429 alone let a route that regressed to 500 pass as "not
            # rate limited". A read route must actually answer.
            assert response.status_code < 500, (name, response.text)
```

- [ ] **Step 5: Prove the table would catch the mutation**

```bash
git status --short src/          # MUST be empty before mutating
sed -i 's/is_write=True/is_write=False/' src/memory/api/{curation,documents,operations}.py
uv run pytest tests/test_governance_ratelimit.py -q; echo "exit=$?"
git checkout -- src/
git status --short src/          # MUST be empty again
uv run pytest tests/test_governance_ratelimit.py -q
```
Expected: FAIL while mutated, PASS after the restore.

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add tests/test_governance_ratelimit.py
git commit -m "test(governance): cover curation, document and operation writes"
```

---

### Task 29: Pin the cross-tenant bank guard

**R4-I4.** Mutating `banks.py:28`'s `if user is None or user.tenant_id != principal.tenant_id:` to `if user is None:` survived the full suite. A master key in tenant A could then address a user in tenant B's private bank. `tests/test_users_api.py:499-536` builds exactly this fixture shape for `get_user`/`create_key`/`list_keys`/`revoke_key` — the data-plane equivalent was never written. Mono-tenant in v1, so the *exposure* is theoretical today; the *coverage asymmetry* is not, and this guard becomes load-bearing the moment `MEMORY_TENANT_ID` is used for real.

**Files:**
- Modify: `tests/test_memory_api.py`

- [ ] **Step 1: Write the test**

```python
def test_a_master_key_cannot_reach_another_tenants_user_bank(
    client, master_headers, session
):
    """Mutating banks.py's `user.tenant_id != principal.tenant_id` away
    survived the whole suite: a master key in tenant A could then address a
    user in tenant B's private bank via scope=user&user_id=... . The control
    -plane equivalent of this test exists (tests/test_users_api.py:499-536);
    the data-plane one never did (2026-08-23 review, R4-I4)."""
    import respx

    from memory.models import Tenant, User

    session.add(Tenant(id="other"))
    session.add(
        User(id="usr_elsewhere", tenant_id="other", bank_id="user_other-bank-id")
    )
    session.flush()

    with respx.mock:
        route = respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
            return_value=httpx.Response(200, json={})
        )
        response = client.post(
            "/v1/memory/recall",
            json={"scope": "user", "user_id": "usr_elsewhere", "query": "x"},
            headers=master_headers,
        )

    # 404, not 403 (resolved 2026-08-23, after Task 24). From tenant A's
    # point of view a user that lives only in tenant B does not exist, and
    # saying USER_NOT_FOUND discloses nothing -- whereas 403 would imply "it
    # exists but you may not have it", which is a cross-tenant existence
    # signal. Task 24's branch already answers not-found for both `user is
    # None` and a tenant mismatch, so this asserts exactly what it does.
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "USER_NOT_FOUND", response.text
    assert route.call_count == 0, "the request reached Hindsight before being refused"
```

- [ ] **Step 2: Prove it catches the mutation**

```bash
git status --short src/memory/banks.py     # MUST be empty before mutating
sed -i 's/if user is None or user.tenant_id != principal.tenant_id:/if user is None:/' src/memory/banks.py
uv run pytest tests/test_memory_api.py -k another_tenants_user_bank -q; echo "exit=$?"
git checkout -- src/memory/banks.py
uv run pytest tests/test_memory_api.py -k another_tenants_user_bank -q
```
Expected: FAIL while mutated, PASS after the restore.

- [ ] **Step 3: Commit**

```bash
git add tests/test_memory_api.py
git commit -m "test(banks): pin the cross-tenant guard on the data plane"
```

---

### Task 30: Delete the mocks that lie

**R4 minors.** Seven test files register a `PATCH /banks/{id}/config` respx stub for a call the client has not issued since Plan 6 — and `tests/test_hindsight_client.py:97` pins that it never will. A stub for a call that cannot happen is noise that makes the next reader assume it can. Separately, four `memories/list` mocks return `{"memories": ...}` where the live server sends `{"items": ...}` (`PROJECT-STATE.md:262`) — harmless today because the routes are pure passthrough, and it is exactly the drift that produced the C1 incident. `tests/test_curation_api.py:540` already uses the correct shape.

**Files:**
- Modify: `tests/test_ratelimit.py`, `test_curation_api.py`, `test_directives_api.py`, `test_documents_api.py`, `test_mcp_tools.py`, `test_memory_api.py`, `test_operations_api.py`

- [ ] **Step 1: Find every dead stub**

```bash
grep -rn 'banks/.*\/config\|/config"' tests/ | grep -i patch
grep -rn '"memories":' tests/
```

- [ ] **Step 2: Delete the config stubs**

Remove the `PATCH .../config` route registration from each `_mock_bank()` helper. Do not delete the helper — only the dead route. Verify the client really cannot issue it:

```bash
grep -rn 'config' src/memory/hindsight/client.py || echo "no config call in the client -- stubs confirmed dead"
```

- [ ] **Step 3: Correct the response shape**

Replace `{"memories": [...]}` with `{"items": [...]}` in the four `memories/list` mocks, with a one-line comment at each:

```python
        # "items", not "memories": that is what hindsight-api 0.9.1 actually
        # sends (PROJECT-STATE.md:262). A mock whose shape has drifted from
        # the real upstream is how the chunk_id bank-id leak went unseen.
```

- [ ] **Step 4: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add tests
git commit -m "test: drop the dead config stubs, match the live list shape"
```

---

## Phase 8 — Operational scripts

### Task 31: Fix the e2e assertions that cannot fail

**R4-I5, I6, I11, I12.** Four separate ways a scenario passes without testing anything:

- `e2e.py:1373` asserts `not text.startswith("INTERNAL_ERROR")`. The SDK renders a tool error as `f"Error executing tool {name}: {e}"`, so the client sees `Error executing tool cancel_operation: INTERNAL_ERROR: ...` — never a string *starting with* it. This is the only guard on the MCP cancel path.
- `e2e.py:607-615`'s cross-user isolation check gates on `need("key.alice", "key.bob")` but depends on a *write* scenario that it does not gate on. The runner continues past failures by design, so if the write failed, bob correctly sees nothing and **the single most important security property in the SPEC passes vacuously.** `smoke.sh:45-46` gets this right.
- `e2e.py:147-159`'s `acceptable_race_outcome` accepts any error code except `None`/`INTERNAL_ERROR` — so `403`, `404` and `429` all pass a check whose documented race is a `502` with `upstream_status == 409`.
- `e2e.py:810-812`'s observation-curation scenario `return`s after 5 attempts and counts as PASS. Its own docstring measures observation production at ~1 in 3, so `(2/3)^5 ≈ 13%` of runs skip the assertion — and what is skipped is that a 409 `MEMORY_NOT_CURATABLE` did not regress back into the 502 that "tells an agent to retry something that can never succeed".

**Files:**
- Modify: `scripts/e2e.py`

- [ ] **Step 1: Fix the substring assertion**

`scripts/e2e.py:1373`:

```python
    # `in`, not startswith: the SDK wraps a tool error as
    # f"Error executing tool {name}: {e}", so the text NEVER starts with the
    # code. This was the only guard on the MCP cancel path and it could not
    # fire. (e2e.py:1460 already uses the correct `in` form.)
    assert "INTERNAL_ERROR" not in text, f"mcp cancel_operation broke: {text}"
```

- [ ] **Step 2: Gate the isolation check on the write it depends on**

In the scenario that writes the user-scope fact (`memory.sync_retain_and_recall_user_scope`), after its own assert:

```python
    S["fact.user_written"] = True
```

and in the cross-user isolation scenario:

```python
    # Gate on the WRITE, not just the two keys. The runner continues past
    # failures by design, so if the write scenario failed, bob correctly sees
    # nothing and this -- the single most important security property in the
    # SPEC -- passed vacuously. smoke.sh:45-46 asserts the positive recall
    # before its cross-user check for exactly this reason.
    need("fact.user_written", "key.bob")
```

Apply the same treatment to `curation.list_and_get` (`:697`) and `memory.reflect_user_scope` (`:632`).

- [ ] **Step 3: Narrow the race tolerance**

`scripts/e2e.py:147-159`:

```python
def acceptable_race_outcome(response) -> bool:
    """The ONE documented race: cancelling an operation that reached a
    terminal state first, which Hindsight answers 409 and the client folds
    into 502 HINDSIGHT_ERROR with upstream_status 409 (see the docstring at
    :962). Accepting "any typed error except INTERNAL_ERROR" let 403
    FORBIDDEN, 404 OPERATION_NOT_FOUND and 429 RATE_LIMITED through -- three
    real defects this check would have reported as an expected race.
    """
    if response.status_code != 502:
        return False
    error = response.json().get("error", {})
    return (
        error.get("code") == "HINDSIGHT_ERROR"
        and error.get("details", {}).get("upstream_status") == 409
    )
```

> **Task 26 removes `upstream_status` from `details`.** Do Task 26 first and key this on the logged status instead, or keep the field for this one code. Decide before writing either; do not leave the two commits contradicting each other.

- [ ] **Step 4: Make the skip visible**

`scripts/e2e.py:810-812` — replace the bare `return` with a distinct outcome the SUMMARY counts separately:

```python
    # A distinct SKIP outcome, not a silent PASS. Observations are produced
    # ~1 in 3 (see this scenario's own docstring), so (2/3)^5 = 13% of runs
    # gave up here and counted as green -- while what was skipped is that a
    # 409 MEMORY_NOT_CURATABLE has not regressed into the 502 that "tells an
    # agent to retry something that can never succeed".
    raise Skipped("no observation was produced in 5 attempts")
```

with a `class Skipped(Exception)` near the top and a `skipped` counter in the runner and the SUMMARY line.

- [ ] **Step 5: Verify and commit**

```bash
uv run python -c "import ast,pathlib; ast.parse(pathlib.Path('scripts/e2e.py').read_text())" && echo "parses"
uv run ruff check scripts/
```

```bash
git add scripts/e2e.py
git commit -m "fix(e2e): four assertions that could not fail"
```

---

### Task 32: Make `smoke.sh` check what it claims to check

**R4-I7, I8, I9 + minors.** Its leak-loop comment claims "*every* response body this script collected" — five bodies go to `/dev/null` and are never scanned, including `forget` and `restore`, the curation family the live `chunk_id` leak came from. Its memory picker takes `[0]` of whatever Hindsight ordered first, and a preference-shaped `sync_retain` produces both a `world` and an `observation` fact — `forget` on an `observation` is *correctly* refused with 409, `curl -sf` exits 22, `set -e` fires, and the script dies with **no output at all**. `e2e.py:703-705` explicitly filters `fact_type == "world"` to avoid this. And `restore` is announced but never verified: a `restore` that 200s and does nothing reaches `PASS`.

**Files:**
- Modify: `scripts/smoke.sh`

**Interfaces:**
- Produces: the five captured variables Task 5's leak loop needs (`retained`, `retained2`, `forgotten`, `restored`, `op_retain`).

- [ ] **Step 1: Capture the discarded bodies**

Replace each `>/dev/null` on a `sync_retain`/`forget`/`restore` call with an assignment. E.g. line 35-38:

```bash
retained=$(curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","content":"This project pins its Python dependencies with uv, never with pip."}')
```

and likewise for the second `sync_retain` (`retained2`), `forget` (`forgotten`) and `restore` (`restored`). `op_retain` is already captured at `:139` — it was simply missing from the loop's list.

- [ ] **Step 2: Pick a curatable memory, not an arbitrary one**

Replace the picker at `:94-95`:

```bash
mem_id=$(echo "${listed}" | python3 -c '
import json, sys
# "items" first: that is what hindsight-api 0.9.1 sends (PROJECT-STATE.md:262),
# so the "memories" branch was dead. And filter for a WORLD fact: a
# preference-shaped sync_retain produces both a world and an observation, and
# `forget` on an observation is CORRECTLY refused with 409
# MEMORY_NOT_CURATABLE -- after which curl -sf exits 22, set -e fires, and
# this script dies with no output at all. e2e.py:703-705 filters the same way.
m = json.load(sys.stdin)["result"]
items = m.get("items") or m.get("memories") or []
world = [i for i in items if i.get("fact_type") == "world" and "runbook" in json.dumps(i)]
print((world or items or [{}])[0].get("id", ""))
')
```

- [ ] **Step 3: Verify the restore**

Replace the `restore` block's `echo "restore brought it back"`:

```bash
restored_list=$(curl -sf -X POST "${API}/v1/memory/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","limit":50}')
echo "${restored_list}" | grep -q "${mem_id}" \
  || { echo "FAIL: restore did not bring the memory back" >&2; echo "${restored_list}" >&2; exit 1; }
echo "restore brought it back"
```

Only a 2xx was checked before, so a `restore` that answered 200 and did nothing reached `PASS`. `mcp-smoke.py:176-186` and `e2e.py:730-742` both verify properly — this was the odd one out.

- [ ] **Step 4: Make the failures diagnosable (minor)**

`curl -sf` suppresses the error body and exits 22, so `set -e` terminates with zero diagnostics and every failure costs a manual bisect. For the twenty-odd calls, use the pattern already correct at `:77` — `-w '\n%{http_code}'` plus an assertion naming the endpoint. Do this at least for the calls whose failure is ambiguous (`forget`, `restore`, `correct`); a blanket rewrite is optional.

Also replace the wall-clock slug at `:22` — `e2e.py:32-35`'s own docstring prescribes a random id, and a clock step backwards collides into the 403 the comment at `:17-21` describes:

```bash
project_slug="smoke-project-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
```

- [ ] **Step 5: Verify and commit**

```bash
bash -n scripts/smoke.sh && echo "syntax OK"
grep -c '>/dev/null' scripts/smoke.sh   # should drop by five
```

```bash
git add scripts/smoke.sh
git commit -m "fix(smoke): scan every body, pick a curatable fact, verify restore"
```

---

### Task 33: Clean up after every run, and keep credentials off the command line

**R4-I10 + R4-I13.** No `trap` anywhere across the three scripts. Per run, permanently: `smoke.sh` leaves 2 users, 2 keys, 1 project and 2 Hindsight banks; `e2e.py` leaves ~11 users, 9 keys, 1 group, ~8 projects, plus **80 async retains from the rate-limit probe still doing LLM extraction after exit** — against the one shared LiteLLM key PROJECT-STATE's "no cost attribution" note is about. And the master key plus every minted user key ride on the `curl` command line, where `/proc/<pid>/cmdline` is world-readable for the duration of each request: on a shared CI runner any local process can scrape the plaintext master key.

**Files:**
- Modify: `scripts/smoke.sh`, `scripts/e2e.py`, `scripts/mcp-smoke.py`

- [ ] **Step 1: Move credentials off the command line**

In `scripts/smoke.sh`, write the header to a 0600 file once and pass it with `-H @`:

```bash
# The header goes in a 0600 file, not on the command line: /proc/<pid>/cmdline
# is world-readable for the duration of each request, so on a shared CI runner
# any local process could scrape the plaintext master key out of the ~20 curl
# invocations below.
hdr_dir=$(mktemp -d)
chmod 700 "${hdr_dir}"
umask 077
printf 'Authorization: Bearer %s\n' "${MASTER}" > "${hdr_dir}/master"
```

then replace every `-H "Authorization: Bearer ${MASTER}"` with `-H @"${hdr_dir}/master"`, and do the same for each minted user key as it is created.

Also replace `echo "minted key: ${user_key:0:8}..."` (`:32`) — that prints `mem_` plus four real characters into CI logs. Print the `key_id` instead:

```bash
echo "minted key: ${key_id}"
```

In `scripts/e2e.py:1523`, the failure print embeds the whole response body via `fmt` — for `POST /v1/users/{uid}/keys` that body contains the plaintext `key`. Redact before printing:

```python
        printable = re.sub(r'("key"\s*:\s*")[^"]+', r"\1<redacted>", fmt(response))
```

- [ ] **Step 2: Register cleanup traps**

`scripts/smoke.sh`, **after** the ids exist (registering before them breaks under `set -u`):

```bash
cleanup() {
  local status=$?
  # Non-fatal on purpose: a failing cleanup must not overwrite the run's exit
  # code, and a half-provisioned run has ids that do not resolve.
  set +e
  for uid in "${user_id:-}" "${user_id2:-}"; do
    [ -n "${uid}" ] || continue
    curl -s -X DELETE "${API}/v1/admin/memory/user?user_id=${uid}" \
      -H @"${hdr_dir}/master" >/dev/null
  done
  [ -n "${project_slug:-}" ] && curl -s -X DELETE \
    "${API}/v1/admin/memory/project?project_slug=${project_slug}" \
    -H @"${hdr_dir}/master" >/dev/null
  rm -rf "${hdr_dir}"
  exit "${status}"
}
trap cleanup EXIT
```

`scripts/e2e.py` — the teardown routes are already proven to work (`:1241` tears down `user.victim`'s bank). Register an `atexit` handler, or wrap the runner in `try/finally`, that deletes every bank the run created. The 80-retain rate-limit probe at `:1474` matters most: those are still spending LLM tokens after the process exits.

- [ ] **Step 3: Verify**

```bash
bash -n scripts/smoke.sh && echo "syntax OK"
grep -n 'Bearer \${MASTER}' scripts/smoke.sh && echo "STILL ON THE COMMAND LINE" || echo "no master key on any command line"
grep -c 'trap cleanup EXIT' scripts/smoke.sh
```

- [ ] **Step 4: Commit**

```bash
git add scripts
git commit -m "fix(scripts): clean up on exit, keep keys off the command line"
```

---

## Phase 9 — Schema and documentation

### Task 34: One migration for the schema minors

**R1 minors.** Two changes, both with a real query or a real deployment behind them.

`api_keys.user_id` has a foreign key but no index, while `GET /v1/users/{id}/keys` filters on exactly that column — and every other tenant-scoped FK in this schema carries `index=True`. Every `created_at` uses a Python-side `default=utcnow`, so the audit trail's order depends on each replica's clock while the chart exposes `replicaCount`; `admin.list_audit` already concedes its `id` tiebreak "buys determinism, not recency".

> **`naming_convention` on `Base.metadata` was CUT on 2026-08-23.** The reviewer suggested it so a future migration could `op.drop_constraint` by a known name. No such migration exists or is planned — and adding it makes `--autogenerate` want to rename every existing server-named constraint, so it **adds migration risk to a live database to buy a hypothetical**. That is the trade YAGNI exists to refuse. Add it in a dedicated commit the first time a migration actually needs to drop a constraint by name.

**Files:**
- Modify: `src/memory/models.py`
- Create: `migrations/versions/<rev>_index_api_keys_user_id.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Add the index and the server default**

`src/memory/models.py` — extend the import with `func` only:

```python
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
```

```python
class ApiKey(Base):
    ...
    # index=True: GET /v1/users/{id}/keys filters on exactly this column, and
    # every other tenant-scoped FK in this schema carries one.
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
```

```python
class AuditEvent(Base):
    ...
    created_at: Mapped[datetime] = mapped_column(
        # server_default: the Python-side default puts each replica's own
        # clock on the row, and the chart exposes replicaCount. One clock --
        # the database's -- is what makes list_audit's ordering mean
        # something. `default=utcnow` stays as the client-side fallback.
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        index=True,
    )
```

- [ ] **Step 2: Generate and review the migration**

```bash
uv run alembic revision --autogenerate -m "index api_keys.user_id"
```

Read the generated file before trusting it — autogenerate is a starting point, not an answer. It should contain exactly these two operations; delete anything else it invents:

```python
def upgrade() -> None:
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.alter_column(
        "audit_events",
        "created_at",
        server_default=sa.text("now()"),
        existing_type=sa.DateTime(timezone=True),
    )


def downgrade() -> None:
    op.alter_column(
        "audit_events",
        "created_at",
        server_default=None,
        existing_type=sa.DateTime(timezone=True),
    )
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
```

If autogenerate proposes constraint renames, `DROP`s, or type changes, it has drifted from `models.py` — investigate that before shipping the migration, do not just delete the lines.

- [ ] **Step 3: Verify the chain still applies cleanly**

```bash
uv run alembic heads          # exactly one head
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```
Expected: no errors, one head. (Point `MEMORY_DATABASE_URL` at the test database, not a dev one.)

- [ ] **Step 4: Run the suite and commit**

Run: `uv run pytest -m "not integration" -q`

```bash
git add src/memory/models.py migrations/versions
git commit -m "fix(models): index api_keys.user_id, one clock for the audit trail"
```

---

### Task 35: Make the documentation true

**R5-I10 + R1 minors + R3 note.** Five of nine settings are documented nowhere: `MEMORY_DATABASE_URL`, `MEMORY_HINDSIGHT_URL`, `MEMORY_HINDSIGHT_API_KEY`, `MEMORY_TENANT_ID` and `MEMORY_MAX_CONTENT_BYTES` appear only in `docker-compose.yml` and `values.yaml`. Three of them are required with no default — unannotated `str` fields, so `Settings()` raises at import of `create_app` and the process will not start. An operator deploying outside Helm and Compose has to read `config.py` to learn that.

Plus three comments that are actively wrong: `provenance.py:19-23` says `agent` and `client_name` are "let through and kept" when `client_name` is in `AUDIT_ONLY_KEYS` and is therefore let through and *discarded*; `provenance.py:97-99` describes an allowlist where the code implements a denylist (the denylist is correct per §13.3 — the comment is not); and `PROJECT-STATE.md:344-347` describes the MCP error text as `"CODE: message {...}"`, one wrapping layer short of what the installed `mcp==2.0.0` actually produces, which is what led to the unfireable assertion in Task 31.

**Files:**
- Modify: `README.md`, `SPEC-v1.md`, `docs/PROJECT-STATE.md`, `src/memory/provenance.py`, `src/memory/slugs.py`, `tests/test_slugs.py`, `alembic.ini`, `migrations/env.py`

- [ ] **Step 1: Add a configuration reference to the README**

Every variable, its default, whether it is required, one line of meaning. Lift the text from `config.py`'s comments — they are already good enough to move verbatim.

```markdown
## Configuration

All variables use the `MEMORY_` prefix.

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `MEMORY_DATABASE_URL` | — | **yes** | Postgres DSN. No default: the process will not start without it. |
| `MEMORY_MASTER_KEY_HASH` | — | **yes** | SHA-256 of the master key. Reaches every bank in the tenant. |
| `MEMORY_HINDSIGHT_URL` | — | **yes** | Hindsight base URL. |
| `MEMORY_HINDSIGHT_API_KEY` | `""` | no | Bearer token for Hindsight. Empty means unauthenticated. |
| `MEMORY_TENANT_ID` | `default` | no | Scopes our own DB rows only; never reaches Hindsight (§19.1). |
| `MEMORY_MAX_CONTENT_BYTES` | `256000` | no | Ceiling for `retain`/`correct`/directive/mental-model text and metadata. |
| `MEMORY_MCP_ALLOWED_HOSTS` | `127.0.0.1,localhost` | no | DNS-rebinding allowlist. A wrong value means 421 on every MCP call while REST keeps working. |
| `MEMORY_WRITE_LIMIT` | `60` | no | Writes per window, **per credential and per replica**. |
| `MEMORY_WRITE_WINDOW_SECONDS` | `60` | no | The window. |
| `MEMORY_HINDSIGHT_TIMEOUT_SECONDS` | `30` | no | Ordinary upstream calls. |
| `MEMORY_HINDSIGHT_LLM_TIMEOUT_SECONDS` | `180` | no | `sync_retain` and `reflect`, which block on a model. |

The three required variables have no defaults by design: `Settings()` raises at
import of `create_app`, so a misconfigured deployment fails at startup rather
than at the first request.
```

- [ ] **Step 2: Correct the three wrong comments**

`src/memory/provenance.py:18-23` — say what the code does:

```python
# Reserved keys the server enforces against client overwrite. `agent` and
# `client_name` are deliberately excluded: they are reserved (RESERVED_KEYS)
# so a client cannot clobber an authoritative value, but the server has no
# authoritative value of its own for them in v1.
#
# The two are NOT treated alike downstream, which the previous wording
# ("let through and kept") got wrong for one of them: `agent` is one of
# §13.2's extraction six and reaches Hindsight; `client_name` is in
# AUDIT_ONLY_KEYS and is therefore let through the reserved check and then
# DROPPED from extraction -- nothing in this service persists it.
```

`src/memory/provenance.py:92-99` — the code is a denylist and that is correct:

```python
    # A DENYLIST, not an allowlist: everything except AUDIT_ONLY_KEYS goes to
    # extraction. §13.3's MEMORY_PROJECT_METADATA example ({"profile":
    # "security", ...}) is exactly the unknown-key case that must reach
    # extraction, so forwarding by default is right -- but the comment above
    # used to describe an allowlist, and a maintainer who trusted it would
    # believe unknown keys were held back when they are not.
```

`docs/PROJECT-STATE.md:344-347` — correct the MCP error shape:

```markdown
An MCP tool error reaches the client as
`Error executing tool {name}: {CODE}: {message} {details}` — the SDK's
dispatcher (`mcp/server/mcpserver/tools/base.py:181`) wraps whatever the tool
raised, so `MCPToolError`'s own `"{code}: {message}"` text is nested one layer
deeper than it looks. Assertions must use `in`, never `startswith` — an
assertion that got this wrong sat in `scripts/e2e.py` unable to fire.
```

- [ ] **Step 3: Write the digest rule into the SPEC, then delete `slug_from_locator`**

**Decided 2026-08-23: delete it.** `src/memory/slugs.py:65-77` has zero callers in `src/` — only 9 assertions in `tests/test_slugs.py` reference it. Per SPEC §8.2 and §10 the Git→slug derivation is the **MCP client's** job, and this repository ships no client. Keeping it "so a future wrapper author has a reference implementation" is speculative need — exactly what YAGNI refuses. Dead code with tests is still dead code.

**But do the SPEC edit first, in the same commit.** `grep -n 'digest' SPEC-v1.md` returns nothing: the rule that a derived slug carries a hash exists **only** in the docstring of the function being deleted. And after Task 1's flat first commit, deleted code is genuinely gone — not one `git log` away. Losing that rule would let a wrapper author silently merge two repositories into one memory bank.

Add to `SPEC-v1.md` §8.2, as prose:

```markdown
A slug derived from a Git remote MUST carry a short digest of the canonical
locator, e.g. `github.com-acme-payments-api-3f2a1b9c`. The digest is not
decoration: slug normalization collapses `/`, `.` and `-` to a single
separator, so without it `acme/payments-api` and `acme-payments/api` both
normalize to `github-com-acme-payments-api` -- two unrelated repositories
sharing one memory bank. Take the digest over the CANONICAL locator (host
kept; scheme, userinfo, port and a trailing `.git` removed; lowercased) so
the same repository yields the same slug however its remote is spelled.

Deriving the slug is the CLIENT's job (§10). This service normalizes and
stores whatever slug it is given; it never derives one.
```

Then delete the function and its tests:

```bash
# src/memory/slugs.py  -- remove slug_from_locator and the now-unused
#                         `hashlib` import.
# tests/test_slugs.py  -- remove the 9 assertions that call it.
grep -rn 'slug_from_locator\|hashlib' src/ tests/ || echo "fully removed"
```

Fix the dangling cross-reference this leaves in `normalize_slug`'s docstring at `src/memory/slugs.py:52` — it currently says "see slug_from_locator", which will name nothing:

```python
    Deliberately lossy: it also normalizes human-supplied slugs like
    MEMORY_PROJECT=payments-api, where collapsing separators is what you want.
    A slug DERIVED from a Git remote must carry a digest (SPEC §8.2) precisely
    because this collapsing cannot tell a path separator from a literal
    hyphen -- and that derivation is the client's job, not this service's.
```

Net: about 25 lines removed, and one real rule written down where it belongs.

- [ ] **Step 4: Make a missing database URL loud**

`alembic.ini:89` hardcodes `postgresql+psycopg://memory:memory@localhost:5433/memory`. Harmless today — `migrations/env.py:28-30` overrides from `MEMORY_DATABASE_URL` — but it is the value that applies if that variable is ever unset in a real environment, so a migration run silently targets a developer's local database instead of failing.

```ini
# Deliberately empty. migrations/env.py sets this from MEMORY_DATABASE_URL,
# which is the single source of truth; a hardcoded fallback here meant an
# unset variable silently migrated whatever database this default named.
sqlalchemy.url =
```

Also delete the two dead re-reads of `MEMORY_DATABASE_URL` in `migrations/env.py` (`:50`, `:74-76`) — `:28-30`'s `config.set_main_option` already covers both offline and online modes — and fix the mis-indented comment at `:69`.

- [ ] **Step 5: Bring `PROJECT-STATE.md` up to date**

Add a Plan 7 row to the "Where things stand" table naming this plan, its commit range, and the finding classes it closed. Update the test count. Note the distribution decision (private repo, public artifacts, source-in-image accepted) — it is exactly the kind of thing that file exists to carry.

- [ ] **Step 6: Verify the README's commands actually run**

Work through the quickstart top to bottom on a clean checkout. Every command that fails is a defect this task owns.

```bash
uv run pytest -m "not integration" -q
uv run ruff check .
```

- [ ] **Step 7: Commit**

```bash
git add README.md SPEC-v1.md docs/PROJECT-STATE.md src/memory tests/test_slugs.py \
        alembic.ini migrations/env.py
git commit -m "docs: document every setting, drop the dead slug derivation"
```

---

## Deferred, with reasons

Not every finding earns a change. These were reviewed and left alone.

**Two tasks are kept despite being thin — say so out loud rather than pretend
they are strong.** Task 27 (`%2f` rejection) closes something the reviewer
itself called *not exploitable against the current upstream*: it survives only
because it is two lines and the existing docstring already promises the
stronger behaviour, so the alternative is weakening a comment. Task 21's
threading of `body` into the MCP call closures is prophylactic — the reviewer
graded it Minor and could not construct a failing call — and survives only
because this exact class already shipped once (`fix(mcp): route correct through
CorrectRequest`). That is history, not speculation. Cut either if you disagree;
neither is load-bearing for anything else in the plan.

| Finding | Why it stays |
|---|---|
| `hmac.compare_digest` → `==` survives the suite (R4 minor) | Realistically unexploitable against a SHA-256 digest of a 256-bit secret. Testing it needs `inspect.getsource`, which pins the implementation rather than the behaviour. The docstring already makes the argument. |
| Five byte-identical `_bank` helpers (R2 minor) | Real duplication, but each copy is where its router's `create`/`is_write` policy is stated, and those flags are what Task 28's table pins. Collapsing them makes the policy harder to audit, not easier. |
| `tools.py`'s 654 lines / dead `db`, `principal` closure params (R3-M-7) | Same reasoning: each `_run` call site is where a tool's security flags live. A table-driven loop would shorten the file and lengthen the audit. |
| Private-name imports across API modules (R2 minor) | `_resolve_bank` / `_strip_bank_id` are the module's de facto public API and the underscore now misleads — but renaming them touches every router for no behavioural gain. Do it in a dedicated commit if it ever bothers someone. |
| No pagination on the control plane (R2 minor) | `GET /v1/projects|groups|users` return the whole tenant. Real, and it needs a paging contract decision (cursor vs offset) that belongs in the SPEC first. |
| Admin destructive routes take their target as a **query parameter** (R2 minor) | `DELETE /v1/admin/memory/user?user_id=alice` puts erasure targets in access and proxy logs. Fixing it is a breaking API change; raise it as a SPEC amendment. **Worth doing — it is deferred for sequencing, not because it is wrong.** |
| **`naming_convention` on `Base.metadata`** (R1 minor) | **Cut 2026-08-23.** Buys the ability to `op.drop_constraint` by name in a migration nobody has written, and pays for it by making `--autogenerate` want to rename every existing constraint on a live database. Risk now for a hypothetical later. |
| **Publishing the Hindsight image** (Task 12) | **Cut 2026-08-23.** Fourteen lines wrapping `pip install hindsight-api==0.9.1`. Anyone can build it; nobody has asked to pull it. A third public artifact to version, scan and keep current, for convenience alone. |
| **Build-provenance attestation** (Task 12) | **Cut 2026-08-23.** Supply-chain ceremony for an artifact with no external consumers yet. Digest-pinned bases and SHA-pinned actions stay — they make a published version reproducible, which is the part that pays. |
| `INVALID_REQUEST` vs FastAPI-422 envelope difference | Already recorded as accepted in SPEC §18. |
| In-process (per-replica) rate limiter | Already documented in `ratelimit.py`, `values.yaml` and `PROJECT-STATE.md`. Redis-backing it is a real change, not a fix. |
| `store_document_text` defaults true, so stored prompt injections are retrievable | Already recorded in SPEC §19.5 and `PROJECT-STATE.md`. |

---

## Self-review

**Spec coverage.** Every Critical and Important finding from all five reviewers maps to a task: R1 #1→22, #2→13, #3→23, #4→16, minors→24/34/35. R2 C1→2, I2→3, I3→15, I4→14, I5→16, I6→17, I7→13, minors→17/35. R3 I-1→18, I-2→20, I-3→25, I-4→21, I-5→21, I-6→19, M-1→21, M-2→19, M-4→27, M-5→26, M-6→26, M-8→19. R4 C1→4, C2→5, I1→5, I2→28, I3→23, I4→29, I5–I12→31/32, I13→33, minors→30. R5 C1→6, I1→7, I2→7, I3→8, I4→9, I5→1, I6→9, I7→7, I8→9, I9→10, I10→35, minors→11/12/34/35. The rest are in **Deferred, with reasons** — none is silently dropped, and the three items cut on YAGNI grounds (`naming_convention`, the Hindsight image publish, the provenance attestation) are recorded there with the trade that decided each.

**Ordering hazards.** Three tasks depend on each other and will contradict if run out of order:
1. **Task 5 before Task 32, or Task 32 before Task 5.** Task 5's leak loop references five variables Task 32 creates. Whichever runs first must not name variables that do not exist yet — `set -u` aborts the script.
2. **Task 26 before Task 31's Step 3.** Task 26 removes `upstream_status` from `HindsightError.details`; Task 31 keys the race check on it.
3. **Task 24 before Task 29.** Task 24 changes the master-key branch in `banks.py`; Task 29's assertion must name the single resulting code, not `in (403, 404)`.

**Placeholder scan.** **No open decisions remain** — all three were resolved on 2026-08-23: the licence is **MIT** (Task 1 Step 3, stamped by Task 12); unstorable **audit filters return `[]`** while unstorable **lookup ids raise the route's own not-found** (Task 3 Step 4); and **`slug_from_locator` is deleted** after its digest rule moves into SPEC §8.2 (Task 35 Step 3). The only values still written as `<...>` are ones that do not exist until the step runs: base-image digests (Task 12 Step 2), action commit SHAs (Step 4), the Alembic revision id (Task 34), and the release version (resolved from the git tag at runtime).

**Fixture names.** Tasks 18, 20 and 22 reference fixtures (`mcp_server`, `mcp_headers`, `_principal`, `_force_race`) by the names the existing test files appear to use. Each of those steps says to read the file and match the real signature first — do that rather than trusting the sketch.

**Type consistency.** `_run`'s `call` contract changes twice — Task 20 adds `precomputed`, Task 21 adds `body`. Final signature: `call(bank_id, db, principal, slug, precomputed, body)`. All fifteen closures must match after Task 21; running Task 21 without Task 20 leaves them one argument short.

---

## Execution

Suggested order: **Phase 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9**, honouring the three ordering hazards above.

**Phase 0 is not optional and cannot be reordered.** It flattens 174 commits into one and pushes to a private `ackstorm/ach-memory`. Doing it *after* any other phase would bury that phase's commits inside the squash — the whole point is that everything from Phase 1 onward becomes real, readable history on GitHub.

Phases 1 and 2 alone close every Critical and the four most reachable Importants (unmetered writes aside), and are worth landing before anything else even if the rest waits.
