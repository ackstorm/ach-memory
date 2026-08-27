# Session Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject a digest of user and project memory into every session's MCP instructions, so an agent starts oriented instead of having to know to ask.

**Architecture:** A new `GET /v1/session-brief` composes the existing policy text plus one digest section per resolved bank, reading Hindsight mental models that the endpoint provisions itself. The stdio proxy fetches it once at startup and advertises the result as its own MCP instructions; on any failure it advertises nothing and FastMCP forwards the server's policy text exactly as today.

**Tech Stack:** FastAPI, SQLAlchemy, FastMCP 4.0.0b4, httpx, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-08-27-session-brief-design.md`

## Global Constraints

- No new MCP tool. Mental-model management must stay unreachable by a model (SPEC §14.2/§14.4, enforced by `tests/test_mcp_tools.py::test_the_advertised_tool_surface_is_exactly_the_spec_set`).
- Session start never blocks on, and never fails because of, memory. The proxy's fetch timeout is 2 seconds and every failure path is silent apart from one stderr line.
- The brief is orientation, not authority. The composed text says so and names `recall` as the authority.
- Mental models are created with `max_tokens=400` and `trigger={"mode": "delta", "refresh_cron": "0 3 * * *"}`.
- The mental model is named `ach-memory-session-brief` in every bank.
- A section is omitted when its bank has no material, when its content is still the upstream placeholder `Generating content...`, or when the model is stale AND has not refreshed for 7 days.
- Each section is hard-truncated at 2000 characters, because upstream treats `max_tokens` as advisory (measured: ~850 tokens returned for a 512 request).
- Bank resolution and authorization go through the existing `_resolve_bank` pipeline unchanged. The project bank resolves with `create=False`: a brief must never mint a project.
- No credentials in the composed text. No API key as a command-line argument anywhere (argv is world-readable).
- Tests: write only the tests each task names. Do not run the full suite; run the named file.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/memory/brief.py` (new) | The source queries, provisioning, per-section trust rules, composition. No FastAPI, no HTTP — pure functions plus Hindsight client calls, so it tests without a server. |
| `src/memory/api/brief.py` (new) | The `GET /v1/session-brief` route: scope resolution, then `brief.py`. |
| `src/memory/api/app.py` (modify) | Include the new router. |
| `src/memory/mcp/server.py` (modify) | Extract the instructions string to a module constant so `brief.py` can compose with it. |
| `src/memory/mcp/proxy.py` (modify) | Fetch the brief at startup and set it as the proxy's instructions. |
| `src/memory/cli.py` (modify) | `ach-memory brief` — print exactly what would be injected. |
| `tests/test_brief.py` (new) | Tasks 1 and 2. |
| `tests/test_mcp_proxy.py` (modify) | Task 3. |
| `tests/test_cli.py` (modify) | Task 4. |

---

### Task 1: Composition and provisioning

**Files:**
- Create: `src/memory/brief.py`
- Modify: `src/memory/mcp/server.py` (extract the instructions constant)
- Test: `tests/test_brief.py`

**Interfaces:**
- Consumes: `memory.hindsight.client.get_client()`, whose relevant methods are `list_mental_models(bank_id, *, detail=None, limit=None, offset=None) -> dict`, `create_mental_model(bank_id, *, name, source_query, max_tokens=None, trigger=None) -> dict`, `update_mental_model(bank_id, mental_model_id, *, name=None, source_query=None, max_tokens=None, trigger=None) -> dict`.
- Produces, for Task 2:
  - `@dataclass(frozen=True) class Section: text: str; refreshed_at: str | None`
  - `ensure_section(client, bank_id: str, source_query: str, now: datetime) -> Section | None`
  - `compose(policy: str, user: Section | None, project: Section | None, project_slug: str | None) -> str`
  - `USER_QUERY: str`, `PROJECT_QUERY: str`, `BRIEF_MODEL_NAME: str`, `MAX_SECTION_CHARS: int`
  - `memory.mcp.server.INSTRUCTIONS: str`

`Section` carries the timestamp because the endpoint reports `generated_at`, and by then the mental model itself is out of reach.

- [ ] **Step 1: Extract the instructions constant**

In `src/memory/mcp/server.py`, move the string currently passed to `MCPServer(instructions=...)` into a module-level constant, and pass the constant. Nothing else changes.

```python
INSTRUCTIONS = (
    "Durable memory across sessions and context resets: the system of "
    # ... the existing text, unchanged, character for character ...
    "supply a bank id."
)
```

and in `build_mcp()`:

```python
    return MCPServer(
        name="ach-memory",
        instructions=INSTRUCTIONS,
    )
```

- [ ] **Step 2: Run the existing instructions test to prove the extraction changed nothing**

Run: `uv run pytest tests/test_mcp_tools.py -k instructions -v`
Expected: PASS (it reads `build_mcp().instructions`, so it covers the move).

- [ ] **Step 3: Write the failing tests**

Create `tests/test_brief.py`:

```python
"""The brief's trust rules and composition.

Every rule here exists because a digest that is wrong is worse than one that
is missing: it arrives with no citation and nothing to check it against.
"""

from datetime import UTC, datetime, timedelta

import pytest

from memory import brief

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class FakeClient:
    """Records calls; returns whatever the test seeded."""

    def __init__(self, models=None):
        self.models = models if models is not None else []
        self.created = []
        self.updated = []

    def list_mental_models(self, bank_id, **kwargs):
        return {"mental_models": self.models}

    def create_mental_model(self, bank_id, **kwargs):
        self.created.append((bank_id, kwargs))
        return {"mental_model_id": "mm-new"}

    def update_mental_model(self, bank_id, mental_model_id, **kwargs):
        self.updated.append((bank_id, mental_model_id, kwargs))
        return {"mental_model_id": mental_model_id}


def _model(content, *, refreshed=NOW, stale=False, query=None):
    return {
        "id": "mm-1",
        "name": brief.BRIEF_MODEL_NAME,
        "content": content,
        "source_query": query if query is not None else brief.USER_QUERY,
        "is_stale": stale,
        "last_refreshed_at": refreshed.isoformat(),
    }


def test_a_missing_model_is_created_and_yields_no_section_yet():
    """First contact provisions and returns nothing: upstream fills `content`
    with a placeholder until the first refresh completes, and showing a
    placeholder to a model is worse than showing it nothing."""
    client = FakeClient(models=[])

    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None

    (bank_id, kwargs) = client.created[0]
    assert bank_id == "user_1"
    assert kwargs["name"] == brief.BRIEF_MODEL_NAME
    assert kwargs["source_query"] == brief.USER_QUERY
    assert kwargs["max_tokens"] == 400
    assert kwargs["trigger"] == {"mode": "delta", "refresh_cron": "0 3 * * *"}


def test_the_upstream_placeholder_is_not_a_section():
    client = FakeClient(models=[_model("Generating content...")])
    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None


@pytest.mark.parametrize("content", ["", "   \n  "])
def test_an_empty_digest_is_not_a_section(content):
    """An empty heading is an invitation to invent one."""
    client = FakeClient(models=[_model(content)])
    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None


def test_a_digest_whose_refreshes_are_failing_is_dropped():
    """Stale AND old together mean refreshes are failing, which is otherwise
    invisible: a failed refresh keeps serving the previous content and does
    not set is_stale. Measured against production 2026-08-27."""
    client = FakeClient(
        models=[_model("real content", refreshed=NOW - timedelta(days=8), stale=True)]
    )
    assert brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW) is None


def test_an_old_but_current_digest_is_kept():
    """Age alone is not failure: a user who wrote nothing for a week has a
    legitimately old digest, and the cron skips ticks when nothing is stale."""
    refreshed = NOW - timedelta(days=30)
    client = FakeClient(models=[_model("real content", refreshed=refreshed, stale=False)])

    section = brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW)

    assert section.text == "real content"
    assert section.refreshed_at == refreshed.isoformat()


def test_a_changed_source_query_updates_the_model_in_place():
    """The query is code. A deploy that improves it must reach the next
    refresh with no manual step -- upstream falls back from delta to a full
    regeneration by itself when the query changed."""
    client = FakeClient(models=[_model("real content", query="an older query")])

    brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW)

    (bank_id, model_id, kwargs) = client.updated[0]
    assert (bank_id, model_id) == ("user_1", "mm-1")
    assert kwargs["source_query"] == brief.USER_QUERY


def test_a_long_digest_is_truncated_with_a_visible_marker():
    """max_tokens is advisory upstream: measured ~850 tokens returned for a
    512 request."""
    client = FakeClient(models=[_model("x" * 5000)])

    section = brief.ensure_section(client, "user_1", brief.USER_QUERY, NOW)

    assert len(section.text) <= brief.MAX_SECTION_CHARS + 40
    assert section.text.endswith("[truncated]")


def _section(text):
    return brief.Section(text=text, refreshed_at="2026-08-27T03:00:00+00:00")


def test_compose_states_the_briefs_status_and_keeps_the_policy_first():
    text = brief.compose("POLICY", _section("user facts"), _section("project facts"), "acme-api")

    assert text.startswith("POLICY")
    assert "user facts" in text and "project facts" in text
    assert "acme-api" in text
    # The one clause that makes a wrong digest survivable.
    assert "verify with recall" in text


def test_compose_omits_a_section_it_has_no_material_for():
    text = brief.compose("POLICY", _section("user facts"), None, "acme-api")

    assert "user facts" in text
    assert "acme-api" not in text


def test_compose_with_nothing_is_exactly_the_policy():
    """The failure path must be indistinguishable from today's behaviour."""
    assert brief.compose("POLICY", None, None, None) == "POLICY"
```

- [ ] **Step 4: Run them and watch them fail**

Run: `uv run pytest tests/test_brief.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'memory.brief'`.

- [ ] **Step 5: Write `src/memory/brief.py`**

```python
"""The session brief: what memory already knows, composed for a session start.

Reading a mental model is a SELECT; the LLM cost is paid by its refresh. That
is what makes this affordable to send on every connect.

Every rule in here is about a digest being WRONG rather than missing. Measured
while designing this (2026-08-27): a loose query over this user's own memories
inverted one of his rules -- "after each change run a full test gate" where the
stored fact forbids exactly that -- and invented a role and a toolchain for him
that no memory contains. A missing section costs a session some context. A
confidently wrong one steers the work.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

BRIEF_MODEL_NAME = "ach-memory-session-brief"
MAX_TOKENS = 400
TRIGGER = {"mode": "delta", "refresh_cron": "0 3 * * *"}
MAX_SECTION_CHARS = 2000
# Stale AND older than this means refreshes are failing, not that the user
# went quiet.
STALE_AFTER = timedelta(days=7)
PLACEHOLDER = "Generating content..."

# Every clause below was earned against real memories. The anti-inference
# sentence removed an invented biography; the formatting rules took the output
# from 3364 characters to 1438 and un-inverted a rule. Change them with
# evidence, not taste.
USER_QUERY = (
    "List only the standing instructions this user has explicitly stated "
    "about how an agent must work with them: process rules, communication and "
    "language, and tools they named. Write each as one short imperative line "
    "an agent can follow. State only what the memories say. Do not infer "
    "their role, employer, seniority or any tooling they did not name. Omit "
    "colour, styling and theme preferences entirely. No headings, no tables, "
    "no summary paragraph."
)
PROJECT_QUERY = (
    "List only what has been learned about working in this codebase: "
    "conventions that are followed, constraints that hold, and gotchas "
    "together with their cause. Write each as one short line an agent can act "
    "on. State only what the memories say. Do not infer the project's "
    "purpose, architecture or technology from its name, and do not describe "
    "what the repository's files would already show. No headings, no tables, "
    "no summary paragraph."
)

_CAVEAT = (
    "(orientation, generated from stored facts -- verify with recall before "
    "acting on it)"
)


@dataclass(frozen=True)
class Section:
    """A digest the endpoint is willing to show, with the freshness it can
    report. The mental model is out of reach by the time `generated_at` is
    assembled, so the timestamp travels with the text."""

    text: str
    refreshed_at: str | None


def _find(client, bank_id: str) -> dict | None:
    listed = client.list_mental_models(bank_id, detail="full")
    models = listed.get("mental_models") or listed.get("items") or []
    for model in models:
        if model.get("name") == BRIEF_MODEL_NAME:
            return model
    return None


def ensure_section(
    client, bank_id: str, source_query: str, now: datetime
) -> Section | None:
    """The bank's digest, or None when there is nothing worth showing.

    Provisioning lives here rather than in a migration or a CLI step because a
    bank appears the first time somebody uses it: there is no earlier moment
    at which to create anything.
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
        return None

    if model.get("source_query") != source_query:
        client.update_mental_model(
            bank_id, model["id"], source_query=source_query
        )

    content = (model.get("content") or "").strip()
    if not content or content == PLACEHOLDER:
        return None

    refreshed_at = model.get("last_refreshed_at")
    if model.get("is_stale") and _older_than(refreshed_at, now):
        return None

    if len(content) > MAX_SECTION_CHARS:
        content = content[:MAX_SECTION_CHARS].rstrip() + "\n[truncated]"
    return Section(text=content, refreshed_at=refreshed_at)


def _older_than(timestamp: str | None, now: datetime) -> bool:
    if not timestamp:
        # Never refreshed and already stale: nothing to trust.
        return True
    try:
        refreshed = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    return now - refreshed > STALE_AFTER


def compose(
    policy: str,
    user: Section | None,
    project: Section | None,
    project_slug: str | None,
) -> str:
    """Policy first, sections after, and nothing at all when there is nothing.

    With no sections this returns the policy byte-for-byte, so a memory
    service that is down leaves the model with exactly what it gets today.
    """
    parts = [policy]
    if user:
        parts.append(f"-- What memory knows about you --\n{_CAVEAT}\n{user.text}")
    if project and project_slug:
        parts.append(
            f"-- What memory knows about {project_slug} --\n{_CAVEAT}\n{project.text}"
        )
    return "\n\n".join(parts)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_brief.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/memory/brief.py src/memory/mcp/server.py tests/test_brief.py
git add src/memory/brief.py src/memory/mcp/server.py tests/test_brief.py
git commit -m "feat(brief): compose a session brief from mental models"
```

---

### Task 2: The endpoint

**Files:**
- Create: `src/memory/api/brief.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_brief.py` (append)

**Interfaces:**
- Consumes: Task 1's `ensure_section`, `compose`, `USER_QUERY`, `PROJECT_QUERY`; `memory.mcp.server.INSTRUCTIONS`; and from `memory.api.memory`: `ScopedRequest`, `scoped_query_params`, `_resolve_bank`.
- Produces, for Tasks 3 and 4: `GET /v1/session-brief` returning `{"instructions": str, "generated_at": str | None, "sections": {"user": bool, "project": bool}}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brief.py`:

```python
import httpx
import respx

BASE = "http://hindsight.test"


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


@respx.mock
def test_the_brief_carries_the_policy_and_the_user_section(client, two_users):
    """The endpoint returns the WHOLE instructions payload, not the brief
    alone: the proxy replaces the server's instructions with whatever it
    advertises, so composing anywhere else would drop the policy."""
    from memory.mcp.server import INSTRUCTIONS

    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {
                        "id": "mm-1",
                        "name": "ach-memory-session-brief",
                        "content": "Ask before planning.",
                        "source_query": __import__(
                            "memory.brief", fromlist=["x"]
                        ).USER_QUERY,
                        "is_stale": False,
                        "last_refreshed_at": "2026-08-27T03:00:00+00:00",
                    }
                ]
            },
        )
    )

    response = client.get(
        "/v1/session-brief", headers=_headers(two_users[0]["key"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["instructions"].startswith(INSTRUCTIONS)
    assert "Ask before planning." in body["instructions"]
    assert body["sections"] == {"user": True, "project": False}
    assert body["generated_at"] == "2026-08-27T03:00:00+00:00"


@respx.mock
def test_a_project_that_does_not_exist_is_not_created_by_asking_for_a_brief(
    client, two_users
):
    """create=False. A session start must never mint a project -- an agent
    opening any directory would otherwise squat a slug."""
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )

    response = client.get(
        "/v1/session-brief?scope=user&project_slug=never-created",
        headers=_headers(two_users[0]["key"]),
    )

    assert response.status_code == 200
    assert response.json()["sections"]["project"] is False
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_brief.py -k brief_carries -v`
Expected: FAIL with 404 — the route does not exist.

- [ ] **Step 3: Write the route**

Create `src/memory/api/brief.py`:

```python
"""GET /v1/session-brief -- the instructions payload for one session.

Composed here rather than in the client because the source queries and the
output format are what decide whether a digest is useful or misleading, and
they must be changeable with a deploy. Putting them in the proxy would mean a
release, a tag and a plugin update on every host to fix a hallucination.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from memory import brief
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import ScopedRequest, _resolve_bank, scoped_query_params
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import DomainError
from memory.hindsight.client import get_client
from memory.mcp.server import INSTRUCTIONS

router = APIRouter(prefix="/v1/session-brief", tags=["session-brief"])


class BriefResponse(BaseModel):
    instructions: str
    generated_at: str | None
    sections: dict[str, bool]


@router.get("", response_model=BriefResponse)
def session_brief(
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> BriefResponse:
    now = datetime.now(UTC)
    client = get_client()

    user_bank, _, _ = _resolve_bank(
        ScopedRequest(scope="user"), db, principal, on_behalf_of, "brief.get",
        create=False,
    )
    user_section = brief.ensure_section(client, user_bank, brief.USER_QUERY, now)

    project_section = None
    project_slug = None
    if scoped.project_slug or scoped.git_locator:
        try:
            project_bank, _, project_slug = _resolve_bank(
                ScopedRequest(
                    scope="project",
                    project_slug=scoped.project_slug,
                    git_locator=scoped.git_locator,
                ),
                db, principal, on_behalf_of, "brief.get", create=False,
            )
        except DomainError:
            # A project this caller cannot reach, or one that does not exist
            # yet, is a missing section -- never an error. The session starts
            # either way, and the agent is told nothing rather than something
            # wrong.
            project_slug = None
        else:
            project_section = brief.ensure_section(
                client, project_bank, brief.PROJECT_QUERY, now
            )
    db.commit()

    return BriefResponse(
        instructions=brief.compose(
            INSTRUCTIONS, user_section, project_section, project_slug
        ),
        generated_at=_oldest(user_section, project_section),
        sections={
            "user": user_section is not None,
            "project": project_section is not None,
        },
    )
```

With the `_oldest` helper above the route in the same file. It reports the least fresh timestamp among the sections that were actually included, so a reader learns the floor rather than the best case:

```python
def _oldest(*sections: brief.Section | None) -> str | None:
    """ISO-8601 UTC strings from one source sort lexicographically, and every
    section here came from the same upstream field, so `min` is the oldest."""
    stamps = [s.refreshed_at for s in sections if s and s.refreshed_at]
    return min(stamps) if stamps else None
```

and in the response, `generated_at=_oldest(user_section, project_section)`.

- [ ] **Step 4: Register the router**

In `src/memory/api/app.py`, next to the other `app.include_router(...)` calls:

```python
    from memory.api.brief import router as brief_router

    app.include_router(brief_router)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_brief.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 6: Prove no MCP tool appeared**

Run: `uv run pytest tests/test_mcp_tools.py -k advertised -v`
Expected: PASS — the advertised tool set is unchanged.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/memory/api/brief.py src/memory/api/app.py src/memory/brief.py tests/test_brief.py
git add src/memory/api/brief.py src/memory/api/app.py src/memory/brief.py tests/test_brief.py
git commit -m "feat(brief): serve the session brief over REST"
```

---

### Task 3: The proxy fetches it

**Files:**
- Modify: `src/memory/mcp/proxy.py`
- Modify: `src/memory/cli.py` (call it from `_serve_mcp`)
- Test: `tests/test_mcp_proxy.py`

**Interfaces:**
- Consumes: Task 2's endpoint.
- Produces, for Task 4: `fetch_brief(base_url: str, api_key: str, slug: str | None, locator: str | None, timeout: float = 2.0) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_proxy.py`:

```python
import httpx
import respx

from memory.mcp.proxy import fetch_brief


@respx.mock
def test_fetch_brief_sends_the_resolved_project_context():
    route = respx.get("https://memory.test/v1/session-brief").mock(
        return_value=httpx.Response(
            200,
            json={
                "instructions": "POLICY + BRIEF",
                "generated_at": None,
                "sections": {"user": True, "project": False},
            },
        )
    )

    result = fetch_brief("https://memory.test", "k", "acme-api", "git@host:acme/api.git")

    assert result["instructions"] == "POLICY + BRIEF"
    request = route.calls.last.request
    assert request.url.params["project_slug"] == "acme-api"
    assert request.url.params["git_locator"] == "git@host:acme/api.git"
    assert request.headers["authorization"] == "Bearer k"


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(200, text="not json"),
        httpx.ConnectError("down"),
    ],
)
def test_a_brief_that_cannot_be_fetched_is_simply_absent(failure):
    """Every failure is silent and returns None. The proxy then advertises no
    instructions of its own, FastMCP forwards the server's, and the session is
    exactly as good as it is today."""
    if isinstance(failure, Exception):
        respx.get("https://memory.test/v1/session-brief").mock(side_effect=failure)
    else:
        respx.get("https://memory.test/v1/session-brief").mock(return_value=failure)

    assert fetch_brief("https://memory.test", "k", None, None) is None
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_mcp_proxy.py -k brief -v`
Expected: FAIL, `ImportError: cannot import name 'fetch_brief'`.

- [ ] **Step 3: Implement**

Add to `src/memory/mcp/proxy.py`:

```python
import httpx

BRIEF_TIMEOUT_SECONDS = 2.0


def fetch_brief(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    timeout: float = BRIEF_TIMEOUT_SECONDS,
) -> dict | None:
    """The session brief, or None -- never an exception.

    Bounded and silent on purpose: this runs before the host's first prompt,
    so a slow or broken memory service must cost a session its brief and
    nothing else. Returning None leaves the proxy advertising no instructions
    of its own, which makes FastMCP forward the server's policy text verbatim.
    """
    params = {"scope": "user"}
    if slug:
        params["project_slug"] = slug
    if locator:
        params["git_locator"] = locator
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/v1/session-brief",
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) and body.get("instructions") else None
```

- [ ] **Step 4: Wire it into the proxy's startup**

In `src/memory/cli.py`'s `_serve_mcp`, after building the proxy and before `.run()`:

```python
    server = proxy.build_proxy(url, key)
    slug, locator = proxy.resolve_project_context()
    brief = proxy.fetch_brief(_base_url(url_argument), key, slug, locator)
    if brief:
        server.instructions = brief["instructions"]
    server.run()
```

`_base_url` is the endpoint without the `/mcp/` suffix `_mcp_url` adds — implement it beside `_mcp_url` in the same file, taking the same input `_mcp_url` takes and returning it normalized with no trailing slash.

- [ ] **Step 5: Repair the existing `_serve_mcp` test, which this step breaks**

`tests/test_cli.py::test_mcp_builds_proxy_from_env_and_runs_stdio` patches only `memory.mcp.proxy.build_proxy`. Once `_serve_mcp` also calls `fetch_brief`, that test would make a real HTTP request to `https://mem.example.com` — a unit test reaching the network, hanging for the timeout, and failing wherever DNS says something different. Patch it there too, and assert the brief is applied:

```python
    monkeypatch.setattr("memory.mcp.proxy.build_proxy", fake_build)
    monkeypatch.setattr(
        "memory.mcp.proxy.fetch_brief",
        lambda base, key, slug, locator: {"instructions": "POLICY + BRIEF"},
    )
    assert cli.main(["mcp"]) == 0
    assert calls == [("https://mem.example.com/mcp/", "mem_secret"), ("run", "stdio")]
```

`FakeProxy` in that test needs an `instructions` attribute for the assignment to land on; give it `instructions = None` and assert it ends as `"POLICY + BRIEF"`.

Add one test beside it for the path that matters most:

```python
def test_mcp_still_runs_when_there_is_no_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory service that cannot answer costs the session its brief and
    nothing else -- the proxy then advertises no instructions of its own and
    FastMCP forwards the server's policy text."""
    monkeypatch.setenv("ACH_MEMORY_URL", "https://mem.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "mem_secret")
    ran = []

    class FakeProxy:
        instructions = None

        def run(self) -> None:
            ran.append(True)

    monkeypatch.setattr("memory.mcp.proxy.build_proxy", lambda _u, _k: FakeProxy())
    monkeypatch.setattr("memory.mcp.proxy.fetch_brief", lambda *_a, **_k: None)

    assert cli.main(["mcp"]) == 0
    assert ran == [True]
    assert FakeProxy.instructions is None
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_mcp_proxy.py tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/memory/mcp/proxy.py src/memory/cli.py tests/test_mcp_proxy.py tests/test_cli.py
git add src/memory/mcp/proxy.py src/memory/cli.py tests/test_mcp_proxy.py tests/test_cli.py
git commit -m "feat(brief): advertise the session brief as MCP instructions"
```

---

### Task 4: `ach-memory brief`

**Files:**
- Modify: `src/memory/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 3's `fetch_brief`, and `resolve_project_context`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_brief_prints_what_would_be_injected(monkeypatch, capsys):
    """The escape hatch. A subtly wrong brief shows up as an agent behaving
    oddly for no visible reason; without this, diagnosing it means guessing at
    text nobody can see."""
    from memory import cli

    monkeypatch.setenv("ACH_MEMORY_API_KEY", "k")
    monkeypatch.setattr(
        "memory.mcp.proxy.resolve_project_context", lambda: ("acme", None)
    )
    monkeypatch.setattr(
        "memory.mcp.proxy.fetch_brief",
        lambda *_args, **_kwargs: {
            "instructions": "POLICY\n\n-- What memory knows about you --\nAsk first.",
            "generated_at": "2026-08-27T03:00:00+00:00",
            "sections": {"user": True, "project": False},
        },
    )

    assert cli.main(["brief", "--url", "https://memory.test"]) == 0

    captured = capsys.readouterr()
    assert "Ask first." in captured.out
    assert "generated_at: 2026-08-27T03:00:00+00:00" in captured.err
    assert "project: absent" in captured.err


def test_brief_says_so_when_there_is_none(monkeypatch, capsys):
    from memory import cli

    monkeypatch.setenv("ACH_MEMORY_API_KEY", "k")
    monkeypatch.setattr(
        "memory.mcp.proxy.resolve_project_context", lambda: (None, None)
    )
    monkeypatch.setattr("memory.mcp.proxy.fetch_brief", lambda *_a, **_k: None)

    assert cli.main(["brief", "--url", "https://memory.test"]) == 1
    assert "no brief" in capsys.readouterr().err
```

Both patch the module by dotted path, the same way `test_mcp_builds_proxy_from_env_and_runs_stdio` already patches `memory.mcp.proxy.build_proxy`: `_print_brief` imports the module lazily inside the function, so patching an attribute on the module object works and patching a name in `cli` would not.

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_cli.py -k brief -v`
Expected: FAIL — `brief` is not a valid choice for `command`.

- [ ] **Step 3: Add the subcommand**

In `_parser()`, beside the `mcp` subparser:

```python
    brief = commands.add_parser(
        "brief", help="print the session brief this host would receive"
    )
    brief.add_argument(
        "--url",
        default=None,
        help="memory service base URL (default: $ACH_MEMORY_URL). The API key "
        "is read from $ACH_MEMORY_API_KEY and never taken as an argument, "
        "because argv is world-readable",
    )
```

In `main()`, beside the `mcp` dispatch:

```python
    if args.command == "brief":
        return _print_brief(args.url)
```

And the implementation, beside `_serve_mcp`, reusing its key handling:

```python
def _print_brief(url_argument: str | None) -> int:
    """Print exactly the text a session would receive, metadata to stderr.

    The text goes to stdout alone so it can be diffed or piped; everything a
    human needs to explain a missing section goes to stderr.
    """
    from memory.mcp import proxy

    key = os.environ.get("ACH_MEMORY_API_KEY", "")
    if not key:
        print(
            "ach-memory: ACH_MEMORY_API_KEY must be set to read the brief",
            file=sys.stderr,
        )
        return 1
    base = _base_url(
        url_argument or os.environ.get("ACH_MEMORY_URL") or "http://localhost:8000"
    )
    slug, locator = proxy.resolve_project_context()
    brief = proxy.fetch_brief(base, key, slug, locator)
    if not brief:
        print(f"ach-memory: no brief from {base}", file=sys.stderr)
        return 1
    print(brief["instructions"])
    sections = brief.get("sections") or {}
    for name in ("user", "project"):
        state = "present" if sections.get(name) else "absent"
        print(f"  {name}: {state}", file=sys.stderr)
    print(f"  generated_at: {brief.get('generated_at')}", file=sys.stderr)
    return 0
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Document it**

Add `ach-memory brief` to the command list in `README.md`, one line, in the same style as the neighbouring entries: what it prints and that it is the way to see what a session actually received.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/memory/cli.py tests/test_cli.py
git add src/memory/cli.py tests/test_cli.py README.md
git commit -m "feat(brief): add ach-memory brief"
```

---

## Final verification (once, after Task 4)

- [ ] **Run the affected files together**

Run: `uv run pytest tests/test_brief.py tests/test_mcp_proxy.py tests/test_cli.py tests/test_mcp_tools.py -q`
Expected: PASS.

- [ ] **Lint the whole tree**

Run: `uv run ruff check src tests`

- [ ] **End-to-end against the live service**

Requires `HINDSIGHT_API_REFLECT_LLM_MODEL: gemini.gemini-3.7-flash` deployed; without it the mental model is provisioned and never generates.

```bash
uv run ach-memory brief --url "$ACH_MEMORY_URL"
```

Expected on first run: the policy text alone, `user: absent`, because the model was just created. Expected after its first refresh: the policy text plus a user section holding real standing rules.

The full suite belongs to CI, not to this plan.
