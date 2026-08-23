# Memory Surface and Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the memory data plane — reflect, curation, documents and
asynchronous operations — behind the same authenticate → resolve → authorize →
bank pipeline, with §13 provenance metadata and the explicit IDOR tests §20.1
demands.

**Architecture:** Every new capability is a thin REST route over a Hindsight
client method, sharing the existing `_resolve_bank` pipeline unchanged. The
secondary IDs these routes introduce (`memory_id`, `document_id`,
`operation_id`) are *never* looked up globally: they only ever appear inside a
bank-scoped upstream URL that authorization already gated. Provenance is
assembled server-side and split per §13.2 — extraction metadata goes to
Hindsight, audit context stays wrapper-side.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 sync, psycopg 3,
httpx, pytest + respx, `uv` for dependency management.

## Global Constraints

- The Hindsight `bank_id` never appears in a response, an error envelope, an
  error `details` payload, or a log line. Bank ids are `user_<uuid>` and
  `project_<uuid>`; a project's `internal_id` is `prj_<hex>`. Neither crosses
  the API boundary.
- Every route authenticates, resolves scope, authorizes the bank, and only then
  resolves any secondary resource *inside* that bank (SPEC §20.1). No route may
  look up a `memory_id`, `document_id` or `operation_id` globally.
- v1 writes **no retrieval tags** (SPEC §13.6). No `tags` key is ever sent to
  Hindsight on retain, and `list_tags` is not exposed.
- Reserved metadata keys, at minimum `tenant_id`, `user_id`, `project_slug`,
  `memory_key`, `on_behalf_of`, `agent`, `client_name`: a client attempting to
  set one gets `INVALID_METADATA` and **nothing is written** (SPEC §13.4).
- Error codes come from the fixed SPEC §18 list. New ones this plan uses:
  `INVALID_METADATA` (400), `MEMORY_NOT_FOUND` (404), `DOCUMENT_NOT_FOUND` (404),
  `OPERATION_NOT_FOUND` (404).
- `get_session` never commits. Write paths commit explicitly, and — per the
  reasoning already in `api/memory.py` — they commit *before* the upstream
  Hindsight call, so a lazily created project's `bank_id` survives an upstream
  failure instead of orphaning a materialized bank.
- Hindsight paths live only in `src/memory/hindsight/paths.py`, pinned against a
  live `openapi.json`. Every path in this plan was verified against
  `hindsight-api==0.9.1` on 2026-08-21; the published docs disagree with the
  server, so the server wins.
- `uv` for dependencies. Never `pip install` outside the venv.

---

## Pinned Hindsight facts

Read off the live server's `openapi.json`, not the docs. `{t}` is the tenant
(`default`), `{b}` the bank id.

```text
POST   /v1/{t}/banks/{b}/reflect                    body {"query": ...}
GET    /v1/{t}/banks/{b}/memories/list              query type,q,state,document_id,limit,offset
GET    /v1/{t}/banks/{b}/memories/{memory_id}
PATCH  /v1/{t}/banks/{b}/memories/{memory_id}       body {"text"|"state"|"reason": ...}
GET    /v1/{t}/banks/{b}/documents                  query q,limit,offset
GET    /v1/{t}/banks/{b}/documents/{document_id}
DELETE /v1/{t}/banks/{b}/documents/{document_id}
GET    /v1/{t}/banks/{b}/operations                 query status,type,limit,offset
GET    /v1/{t}/banks/{b}/operations/{operation_id}
DELETE /v1/{t}/banks/{b}/operations/{operation_id}  <- CANCEL
```

Three traps, each of which will cost an hour if ignored:

1. **Listing memories is `GET .../memories/list`**, not `GET .../memories`.
   `DELETE .../memories` is `clear_memories`, which is admin-only and not in
   this plan.
2. **`DELETE .../operations/{id}` cancels.** `DELETE
   .../operations/{id}/delete` — a real, different path — deletes a terminal
   operation. v1 exposes cancel and has no `delete_operation` (§11.5). Do not
   wire the `/delete` suffix.
3. **Curation is one PATCH with three meanings**, driven by the body:
   `{"state": "invalidated"}` is `forget`, `{"state": "valid"}` is `restore`,
   `{"text": "..."}` is `correct`. Hindsight's own description: *"'invalidated'
   to soft-retire the memory ... or 'valid' to revert. Reversible."* There is no
   `DELETE /memories/{id}`; memory is append-only (§12).

`metadata` and `context` are **per-item** fields on `MemoryItem`, not top-level
on the retain body. `operation_id` *is* top-level, on `RetainRequest`.

**A malformed secondary id is a 400 upstream, not a 404** — measured against
the live server: `GET .../memories/ghost` → `400 {"detail":"Invalid
memory_id: 'ghost' is not a valid UUID"}`, and `GET .../operations/op_1` →
`400 {"detail":"Invalid operation_id format: op_1"}`. Only a syntactically
valid, absent UUID (e.g. `00000000-0000-0000-0000-000000000000`) 404s. The
Hindsight client (`src/memory/hindsight/client.py`) now rejects a non-UUID
`memory_id` or `operation_id` locally — before the round trip — raising the
same `MemoryNotFound` / `OperationNotFound` the 404 branch raises, via a
`_require_uuid` helper applied to `get_memory`, `curate`, `get_operation` and
`cancel_operation`. `document_id`, by contrast, is caller-managed and
arbitrary (e.g. `github:acme/api:pr:382`) and is deliberately **exempt** —
`list_documents`/`get_document`/`delete_document` never validate it. Every
test in Task 4 and Task 6 below that exercises `get_memory`/`curate` or
`get_operation`/`cancel_operation` on a success path, or asserts a 404, must
use a syntactically valid UUID (a real one for success, a valid-but-absent
one like `00000000-0000-0000-0000-000000000000` for "not found") — a bare
`"mem_1"` / `"op_1"` / `"ghost"` id now gets rejected by the client before the
mocked route is ever reached, so the mock goes uncalled. Tests where the
memory/operation id is never actually read (the §20.1 IDOR tests, which are
refused by the bank-authorization check before the id is used at all) are
unaffected and keep their placeholder ids.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/memory/hindsight/paths.py` (modify) | + reflect, memory list/get/curate, documents, operations |
| `src/memory/hindsight/client.py` (modify) | + the matching methods; 404 mapping for secondary resources |
| `src/memory/provenance.py` (create) | the §13.2 split and §13.4 reserved-key validation |
| `src/memory/api/memory.py` (modify) | + reflect; provenance wired into retain |
| `src/memory/api/curation.py` (create) | list_memories, get_memory, forget, correct, restore |
| `src/memory/api/documents.py` (create) | list, get, delete |
| `src/memory/api/operations.py` (create) | get, list, cancel |
| `src/memory/errors.py` (modify) | + `InvalidMetadata`, `MemoryNotFound`, `DocumentNotFound`, `OperationNotFound` |
| `src/memory/audit.py` (unchanged) | used by Task 7 |

`api/memory.py` keeps the shared `_resolve_bank` and `_strip_bank_id`; the three
new routers import them rather than re-deriving the pipeline.

---

### Task 1: Hindsight client — the eleven missing calls

**Files:**
- Modify: `src/memory/hindsight/paths.py`
- Modify: `src/memory/hindsight/client.py`
- Modify: `src/memory/errors.py`
- Test: `tests/test_hindsight_client.py`

**Interfaces:**
- Consumes: `memory.errors.HindsightError`, `memory.hindsight.paths`.
- Produces:
  - `paths.reflect/memory_list/memory/documents/document/operations/operation(tenant, bank_id[, id])`
  - `HindsightClient.reflect(bank_id, query) -> dict`
  - `HindsightClient.list_memories(bank_id, **filters) -> dict`
  - `HindsightClient.get_memory(bank_id, memory_id) -> dict`
  - `HindsightClient.curate(bank_id, memory_id, *, text=None, state=None, reason=None) -> dict`
  - `HindsightClient.list_documents(bank_id, **filters) -> dict`
  - `HindsightClient.get_document(bank_id, document_id) -> dict`
  - `HindsightClient.delete_document(bank_id, document_id) -> dict`
  - `HindsightClient.get_operation(bank_id, operation_id) -> dict`
  - `HindsightClient.list_operations(bank_id, **filters) -> dict`
  - `HindsightClient.cancel_operation(bank_id, operation_id) -> dict`
  - `errors.MemoryNotFound`, `errors.DocumentNotFound`, `errors.OperationNotFound`, `errors.InvalidMetadata`
  - `HindsightClient._request` gains `not_found: type[DomainError] | None = None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hindsight_client.py`:

```python
import httpx
import pytest
import respx

from memory.errors import HindsightError, MemoryNotFound
from memory.hindsight.client import HindsightClient

BASE = "http://hindsight.test"
BANK = "project_abc"


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="", tenant_id="default")


@respx.mock
def test_reflect_posts_the_query(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "use uv"})
    )

    result = client.reflect(BANK, "how do we manage dependencies")

    assert result == {"answer": "use uv"}
    assert route.calls.last.request.read() == b'{"query":"how do we manage dependencies"}'


@respx.mock
def test_list_memories_uses_the_list_subpath_and_drops_empty_filters(client):
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/list").mock(
        return_value=httpx.Response(200, json={"memories": []})
    )

    client.list_memories(BANK, q="alembic", state=None, limit=10)

    url = route.calls.last.request.url
    assert url.path == f"/v1/default/banks/{BANK}/memories/list"
    assert dict(url.params) == {"q": "alembic", "limit": "10"}


@respx.mock
def test_forget_and_restore_are_one_patch_with_different_states(client):
    route = respx.patch(f"{BASE}/v1/default/banks/{BANK}/memories/mem_1").mock(
        return_value=httpx.Response(200, json={"id": "mem_1"})
    )

    client.curate(BANK, "mem_1", state="invalidated", reason="wrong")
    forget_body = route.calls.last.request.read()
    client.curate(BANK, "mem_1", state="valid")
    restore_body = route.calls.last.request.read()
    client.curate(BANK, "mem_1", text="uv, not pip")
    correct_body = route.calls.last.request.read()

    assert forget_body == b'{"state":"invalidated","reason":"wrong"}'
    assert restore_body == b'{"state":"valid"}'
    assert correct_body == b'{"text":"uv, not pip"}'


@respx.mock
def test_cancel_operation_does_not_use_the_delete_suffix(client):
    """DELETE .../operations/{id} cancels; .../{id}/delete is a different
    endpoint that removes a terminal operation, and v1 does not expose it."""
    route = respx.delete(f"{BASE}/v1/default/banks/{BANK}/operations/op_1").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    client.cancel_operation(BANK, "op_1")

    assert route.calls.last.request.url.path.endswith("/operations/op_1")


@respx.mock
def test_an_upstream_404_becomes_the_supplied_not_found_error(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/ghost").mock(
        return_value=httpx.Response(404, json={"detail": "no such memory"})
    )

    with pytest.raises(MemoryNotFound):
        client.get_memory(BANK, "ghost")


@respx.mock
def test_other_upstream_failures_stay_hindsight_errors(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/mem_1").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    with pytest.raises(HindsightError):
        client.get_memory(BANK, "mem_1")


@respx.mock
def test_a_not_found_error_never_carries_the_bank_id(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/ghost").mock(
        return_value=httpx.Response(404, json={"detail": f"bank {BANK} lacks it"})
    )

    with pytest.raises(MemoryNotFound) as caught:
        client.get_memory(BANK, "ghost")

    rendered = f"{caught.value!r} {caught.value.details} {caught.value.__context__!r}"
    assert BANK not in rendered
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_hindsight_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'MemoryNotFound'`.

- [ ] **Step 3: Add the error classes**

Append to `src/memory/errors.py`:

```python
class InvalidMetadata(DomainError):
    code = "INVALID_METADATA"
    status = 400


class MemoryNotFound(DomainError):
    code = "MEMORY_NOT_FOUND"
    status = 404


class DocumentNotFound(DomainError):
    code = "DOCUMENT_NOT_FOUND"
    status = 404


class OperationNotFound(DomainError):
    code = "OPERATION_NOT_FOUND"
    status = 404
```

- [ ] **Step 4: Add the paths**

Append to `src/memory/hindsight/paths.py`:

```python
def reflect(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/reflect"


def memory_list(tenant: str, bank_id: str) -> str:
    # NOT `/memories`: that is retain (POST) and clear_memories (DELETE).
    return f"{bank(tenant, bank_id)}/memories/list"


def memory(tenant: str, bank_id: str, memory_id: str) -> str:
    return f"{bank(tenant, bank_id)}/memories/{memory_id}"


def documents(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/documents"


def document(tenant: str, bank_id: str, document_id: str) -> str:
    return f"{bank(tenant, bank_id)}/documents/{document_id}"


def operations(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/operations"


def operation(tenant: str, bank_id: str, operation_id: str) -> str:
    # DELETE on this path CANCELS. `{path}/delete` removes a terminal
    # operation and is deliberately not exposed in v1 (SPEC §11.5).
    return f"{bank(tenant, bank_id)}/operations/{operation_id}"
```

- [ ] **Step 5: Teach `_request` about 404 and add the methods**

In `src/memory/hindsight/client.py`, replace `_request` with this and add the
methods below it. Note `params` is new, and the signature keeps `payload`
optional so GET and DELETE callers do not invent empty bodies:

```python
    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        not_found: type[DomainError] | None = None,
    ) -> dict:
        response = None
        try:
            response = self._http.request(method, path, json=payload, params=params)
        except httpx.HTTPError as exc:
            # Logged here and never attached to the raised error: the httpx
            # exception holds .request.url, which contains the bank ID.
            logger.warning("hindsight transport failure: %s", type(exc).__name__)

        if response is None:
            # Raised OUTSIDE the except block on purpose. Inside it, Python sets
            # __context__ to the httpx error even with `from None`, and anything
            # walking __context__ would reach the bank ID.
            raise HindsightError("memory backend unreachable")

        if response.status_code == 404 and not_found is not None:
            # A secondary resource that is absent from an ALREADY-AUTHORIZED
            # bank is an ordinary 404, not a backend fault. The upstream body is
            # never echoed: it can name the bank.
            raise not_found("no such object in this memory")

        if response.status_code >= 400:
            raise HindsightError(
                "memory backend rejected the request",
                upstream_status=response.status_code,
            )

        return response.json()

    def reflect(self, bank_id: str, query: str) -> dict:
        return self._request(
            "POST", paths.reflect(self._tenant, bank_id), {"query": query}
        )

    def list_memories(self, bank_id: str, **filters: Any) -> dict:
        return self._request(
            "GET",
            paths.memory_list(self._tenant, bank_id),
            params=_present(filters),
        )

    def get_memory(self, bank_id: str, memory_id: str) -> dict:
        return self._request(
            "GET",
            paths.memory(self._tenant, bank_id, memory_id),
            not_found=MemoryNotFound,
        )

    def curate(
        self,
        bank_id: str,
        memory_id: str,
        *,
        text: str | None = None,
        state: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """One PATCH, three meanings (SPEC §12).

        state="invalidated" is forget, state="valid" is restore, text=... is
        correct. Hindsight has no DELETE for a memory; it is append-only, and
        invalidation is reversible on purpose.
        """
        body = _present({"text": text, "state": state, "reason": reason})
        return self._request(
            "PATCH",
            paths.memory(self._tenant, bank_id, memory_id),
            body,
            not_found=MemoryNotFound,
        )

    def list_documents(self, bank_id: str, **filters: Any) -> dict:
        return self._request(
            "GET", paths.documents(self._tenant, bank_id), params=_present(filters)
        )

    def get_document(self, bank_id: str, document_id: str) -> dict:
        return self._request(
            "GET",
            paths.document(self._tenant, bank_id, document_id),
            not_found=DocumentNotFound,
        )

    def delete_document(self, bank_id: str, document_id: str) -> dict:
        return self._request(
            "DELETE",
            paths.document(self._tenant, bank_id, document_id),
            not_found=DocumentNotFound,
        )

    def get_operation(self, bank_id: str, operation_id: str) -> dict:
        return self._request(
            "GET",
            paths.operation(self._tenant, bank_id, operation_id),
            not_found=OperationNotFound,
        )

    def list_operations(self, bank_id: str, **filters: Any) -> dict:
        return self._request(
            "GET", paths.operations(self._tenant, bank_id), params=_present(filters)
        )

    def cancel_operation(self, bank_id: str, operation_id: str) -> dict:
        return self._request(
            "DELETE",
            paths.operation(self._tenant, bank_id, operation_id),
            not_found=OperationNotFound,
        )
```

Add this module-level helper above the class, and extend the imports:

```python
def _present(values: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the caller left unset.

    Sending `state=None` as a query parameter is not the same as omitting it —
    httpx renders it as an empty string and Hindsight filters on that.
    """
    return {k: v for k, v in values.items() if v is not None}
```

```python
from memory.errors import (
    DocumentNotFound,
    DomainError,
    HindsightError,
    MemoryNotFound,
    OperationNotFound,
)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_hindsight_client.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -m "not integration"`
Expected: PASS, count is the previous total + 7.

- [ ] **Step 8: Commit**

```bash
git add src/memory/hindsight/paths.py src/memory/hindsight/client.py \
        src/memory/errors.py tests/test_hindsight_client.py
git commit -m "add the reflect, curation, document and operation client calls"
```

---

### Task 2: Provenance — the §13.2 split and reserved keys

**Files:**
- Create: `src/memory/provenance.py`
- Test: `tests/test_provenance.py`

**Interfaces:**
- Consumes: `memory.errors.InvalidMetadata`.
- Produces:
  - `provenance.RESERVED_KEYS: frozenset[str]`
  - `provenance.EXTRACTION_KEYS: frozenset[str]`
  - `provenance.build(client_metadata: dict[str, str] | None, *, project_slug: str | None, user_id: str | None, on_behalf_of: str | None) -> tuple[dict[str, str], dict[str, str]]`
    returning `(extraction_metadata, audit_context)`
  - `provenance.context_line(metadata: dict[str, str]) -> str | None`

**Why a whole module for this.** SPEC §13.2 says "the wrapper owns this
mapping", and it is the only place where client-supplied data and
server-authoritative data are merged. Getting it wrong means either a client
overwrites `user_id` in the extraction record (§13.4) or audit-only fields such
as `on_behalf_of` leak into the memory engine's extraction input.

- [ ] **Step 1: Write the failing test**

Create `tests/test_provenance.py`:

```python
import pytest

from memory import provenance
from memory.errors import InvalidMetadata


def test_runtime_fields_split_between_extraction_and_audit():
    extraction, audit = provenance.build(
        {"agent": "codex", "source": "pull-request", "git_branch": "feature/auth"},
        project_slug="payments-api",
        user_id="usr_juan",
        on_behalf_of=None,
    )

    assert extraction == {
        "agent": "codex",
        "source": "pull-request",
        "git_branch": "feature/auth",
        "project_slug": "payments-api",
    }
    # user_id is audit context, never extraction input (SPEC §13.2).
    assert audit == {"user_id": "usr_juan", "project_slug": "payments-api"}


def test_client_metadata_survives_when_it_is_not_reserved():
    extraction, _ = provenance.build(
        {"profile": "security", "pr": "382"},
        project_slug="payments-api",
        user_id="usr_juan",
        on_behalf_of=None,
    )

    assert extraction["profile"] == "security"
    assert extraction["pr"] == "382"


@pytest.mark.parametrize(
    "key", ["tenant_id", "user_id", "project_slug", "memory_key", "on_behalf_of"]
)
def test_a_reserved_key_is_refused_and_nothing_is_written(key):
    with pytest.raises(InvalidMetadata) as caught:
        provenance.build(
            {key: "attacker"},
            project_slug="payments-api",
            user_id="usr_juan",
            on_behalf_of=None,
        )

    assert caught.value.details["key"] == key


def test_agent_and_client_name_are_reserved_against_overwrite_but_settable_once():
    """§13.4 reserves them so client metadata cannot overwrite an
    authoritative value. When the server has none, the client's is the only
    value there is and it is kept."""
    extraction, _ = provenance.build(
        {"agent": "codex"},
        project_slug="payments-api",
        user_id="usr_juan",
        on_behalf_of=None,
    )

    assert extraction["agent"] == "codex"


def test_on_behalf_of_is_audit_only_and_never_reaches_extraction():
    extraction, audit = provenance.build(
        None,
        project_slug="payments-api",
        user_id=None,
        on_behalf_of="usr_alice",
    )

    assert "on_behalf_of" not in extraction
    assert audit["on_behalf_of"] == "usr_alice"


def test_user_scope_carries_no_project_slug():
    extraction, audit = provenance.build(
        None, project_slug=None, user_id="usr_juan", on_behalf_of=None
    )

    assert extraction == {}
    assert audit == {"user_id": "usr_juan"}


def test_context_line_reads_like_a_sentence():
    line = provenance.context_line(
        {"source": "interactive-coding", "agent": "codex", "git_branch": "feature/auth"}
    )

    assert line == "interactive-coding via codex on feature/auth"


def test_context_line_is_none_without_provenance():
    assert provenance.context_line({}) is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_provenance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.provenance'`.

- [ ] **Step 3: Write `src/memory/provenance.py`**

```python
from memory.errors import InvalidMetadata

# SPEC §13.4, "reserved at minimum". A client may not set any of these: the
# server is authoritative for them, and a memory that lies about who wrote it
# or which project it belongs to is worse than no provenance at all.
RESERVED_KEYS = frozenset(
    {
        "tenant_id",
        "user_id",
        "project_slug",
        "memory_key",
        "on_behalf_of",
        "agent",
        "client_name",
    }
)

# SPEC §13.2. Only these influence extraction; everything else the runtime
# knows is audit context and stays wrapper-side.
EXTRACTION_KEYS = frozenset(
    {"agent", "source", "git_branch", "git_commit", "pr", "workspace"}
)

# The subset the server itself is authoritative for and therefore fills in.
# `agent` and `client_name` are reserved against OVERWRITE, but the server has
# no value of its own for them in v1 — the client's is the only one there is.
_SERVER_OWNED = frozenset({"tenant_id", "user_id", "project_slug", "on_behalf_of"})


def build(
    client_metadata: dict[str, str] | None,
    *,
    project_slug: str | None,
    user_id: str | None,
    on_behalf_of: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Split provenance into what Hindsight sees and what stays here.

    Returns (extraction_metadata, audit_context). Raises InvalidMetadata
    before anything is written if the client tried to set a server-owned key.
    """
    supplied = dict(client_metadata or {})

    for key in supplied:
        if key in _SERVER_OWNED:
            # Refused before the split, so §13.4's "nothing is written" holds
            # for the whole request, not just for the offending key.
            raise InvalidMetadata(
                "that metadata key is reserved by the server", key=key
            )

    extraction = {k: v for k, v in supplied.items() if k not in _SERVER_OWNED}
    audit: dict[str, str] = {}

    if project_slug:
        # Authoritative on both sides: extraction may use it, audit records it.
        extraction["project_slug"] = project_slug
        audit["project_slug"] = project_slug
    if user_id:
        audit["user_id"] = user_id
    if on_behalf_of:
        audit["on_behalf_of"] = on_behalf_of

    return extraction, audit


def context_line(metadata: dict[str, str]) -> str | None:
    """Hindsight's short free-text context field (SPEC §13.5).

    "interactive-coding via codex on feature/auth". Built only from parts that
    are present, and None when there is nothing to say — an empty or
    half-formed sentence is worse than no context.
    """
    source = metadata.get("source")
    agent = metadata.get("agent")
    branch = metadata.get("git_branch")

    parts = [source or agent]
    if source and agent:
        parts.append(f"via {agent}")
    if branch:
        parts.append(f"on {branch}")

    if parts[0] is None:
        return None
    return " ".join(p for p in parts if p)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_provenance.py -v`
Expected: PASS — 12 passed (the reserved-key case is parametrized over 5).

- [ ] **Step 5: Commit**

```bash
git add src/memory/provenance.py tests/test_provenance.py
git commit -m "split provenance into extraction metadata and audit context"
```

---

### Task 3: Wire provenance into retain, and add reflect

**Files:**
- Modify: `src/memory/api/memory.py`
- Test: `tests/test_memory_api.py`

**Interfaces:**
- Consumes: `memory.provenance.build/context_line`, `HindsightClient.reflect`.
- Produces: `POST /v1/memory/reflect`; `metadata` on retain now validated and
  merged; `RetainRequest.operation_id` passed through.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_api.py`:

```python
@respx.mock
def test_retain_sends_extraction_metadata_and_a_context_line(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.post(
        "/v1/memory/retain",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "content": "x",
            "metadata": {"agent": "codex", "source": "interactive-coding"},
        },
        headers=juan["headers"],
    )

    item = json.loads(route.calls.last.request.read())["items"][0]
    assert item["metadata"]["agent"] == "codex"
    assert item["metadata"]["project_slug"] == "payments-api"
    assert item["context"] == "interactive-coding via codex"


@respx.mock
def test_retain_never_sends_audit_only_fields_to_hindsight(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=juan["headers"],
    )

    item = json.loads(route.calls.last.request.read())["items"][0]
    assert "user_id" not in item.get("metadata", {})


@respx.mock
def test_a_reserved_metadata_key_is_refused_and_nothing_is_retained(
    client, juan, tenant
):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "metadata": {"user_id": "usr_someone"}},
        headers=juan["headers"],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_METADATA"
    assert route.call_count == 0


@respx.mock
def test_a_custom_operation_id_is_passed_through(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_mine"})
    )

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "operation_id": "op_mine"},
        headers=juan["headers"],
    )

    assert json.loads(route.calls.last.request.read())["operation_id"] == "op_mine"


@respx.mock
def test_reflect_reaches_the_reflect_endpoint_of_the_right_bank(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "use uv"})
    )

    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"answer": "use uv"}
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_reflect_is_denied_on_someone_elses_project(client, juan, alice, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "leaked"})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )

    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
```

If `tests/test_memory_api.py` does not already have `juan` and `alice` fixtures
returning `{"user_id", "headers"}`, add them in the same shape
`tests/test_projects_api.py` uses, and `import json` at the top.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_memory_api.py -v`
Expected: FAIL — 404 on `/v1/memory/reflect`, and the metadata assertions fail
because retain does not merge provenance yet.

- [ ] **Step 3: Wire provenance and add reflect**

In `src/memory/api/memory.py`, add `operation_id` to `RetainRequest`:

```python
class RetainRequest(BaseModel):
    scope: Scope
    content: str
    user_id: str | None = None
    project_slug: str | None = None
    git_locator: str | None = Field(default=None, max_length=512)
    document_id: str | None = None
    update_mode: str = "replace"
    metadata: dict[str, str] | None = None
    # SPEC §15: a caller may supply its own operation id for safe retries.
    # Passed through verbatim; the wrapper assigns no meaning to it.
    operation_id: str | None = None
```

Add a `ReflectRequest` beside `RecallRequest`:

```python
class ReflectRequest(BaseModel):
    scope: Scope
    query: str
    user_id: str | None = None
    project_slug: str | None = None
    git_locator: str | None = Field(default=None, max_length=512)
```

Replace the body of `_retain` between the size check and the client call:

```python
    bank_id, resolved_from, project_slug = _resolve_bank(body, db, principal)

    extraction, _audit = provenance.build(
        body.metadata,
        project_slug=project_slug,
        user_id=principal.user_id,
        on_behalf_of=None,
    )

    # Commit BEFORE the upstream call: resolution may have created the project
    # that owns this bank_id, and rolling that back after ensure_bank has
    # materialized the bank upstream orphans it unreachably.
    db.commit()

    client = get_client()
    client.ensure_bank(bank_id)

    result = client.retain(
        bank_id,
        body.content,
        document_id=body.document_id,
        metadata=extraction or None,
        context=provenance.context_line(extraction),
        update_mode=body.update_mode,
        is_async=is_async,
        operation_id=body.operation_id,
    )
```

`HindsightClient.retain` needs the new argument. In
`src/memory/hindsight/client.py`, add the parameter and place it top-level on
the body, where `RetainRequest` carries it — **not** on the item:

```python
        operation_id: str | None = None,
```

```python
        body: dict[str, Any] = {"items": [item], "async": is_async}
        if operation_id is not None:
            # Top-level on RetainRequest, not per-item: it identifies the
            # whole async operation.
            body["operation_id"] = operation_id
        return self._request("POST", paths.retain(self._tenant, bank_id), body)
```

Add the reflect route at the end of `src/memory/api/memory.py`:

```python
@router.post("/reflect", response_model=MemoryResponse)
def reflect(
    body: ReflectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _resolve_bank(body, db, principal)
    db.commit()
    result = get_client().reflect(bank_id, body.query)
    return MemoryResponse(
        result=_strip_bank_id(result),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
```

`_resolve_bank` is typed against `RetainRequest | RecallRequest`; widen the
annotation to include `ReflectRequest`.

Import `provenance` and `Field` at the top of the module.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_memory_api.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -m "not integration"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/memory/api/memory.py src/memory/hindsight/client.py tests/test_memory_api.py
git commit -m "attach provenance to retain and expose reflect"
```

---

### Task 4: Curation routes, and the IDOR tests §20.1 demands

**Files:**
- Create: `src/memory/api/curation.py`
- Modify: `src/memory/api/app.py`
- Modify: `src/memory/api/memory.py` (export the shared pipeline)
- Test: `tests/test_curation_api.py`

**Interfaces:**
- Consumes: `memory.api.memory._resolve_bank`, `_strip_bank_id`, `MemoryResponse`,
  `Scope`; `HindsightClient.list_memories/get_memory/curate`.
- Produces: `POST /v1/memory/list`, `POST /v1/memory/get`, `POST /v1/memory/forget`,
  `POST /v1/memory/correct`, `POST /v1/memory/restore`.

**Why POST for reads.** Every one of these carries a `scope` plus a
`project_slug` and optional `git_locator` in its body; the existing data plane
is uniformly POST-with-a-body for exactly that reason. Mixing GET-with-query
into the same router would make the pipeline two shapes instead of one.

**The IDOR property to test (SPEC §20.1).** `memory_id` is meaningful only
inside an already-authorized bank. A caller who supplies a `memory_id` belonging
to a bank they cannot reach must be stopped by the *bank* check, before the id
is used at all — and the failure must be indistinguishable from asking for an id
that does not exist. Concretely: the request never reaches Hindsight.

- [ ] **Step 1: Write the failing test**

Create `tests/test_curation_api.py`:

```python
import httpx
import pytest
import respx

BASE = "http://hindsight.test"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": _headers(key)}


@pytest.fixture
def alice(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": _headers(key)}


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/config").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_list_memories_reaches_the_list_subpath(client, juan, tenant):
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        return_value=httpx.Response(200, json={"memories": [{"id": "mem_1"}]})
    )

    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "q": "alembic", "limit": 5},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"]["memories"] == [{"id": "mem_1"}]
    assert dict(route.calls.last.request.url.params) == {"q": "alembic", "limit": "5"}


@respx.mock
def test_forget_invalidates_rather_than_deleting(client, juan, tenant):
    # memory_id must be a syntactically valid UUID: the client now rejects a
    # non-UUID memory_id locally (a malformed id is a 400 upstream, not a
    # 404 — see "Pinned Hindsight facts"), so the mock would never be reached.
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/forget",
        json={"scope": "user", "memory_id": mem_id, "reason": "wrong"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.method == "PATCH"
    assert b'"state":"invalidated"' in route.calls.last.request.read()


@respx.mock
def test_restore_reverts_an_invalidated_memory(client, juan, tenant):
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    client.post(
        "/v1/memory/restore",
        json={"scope": "user", "memory_id": mem_id},
        headers=juan["headers"],
    )

    assert b'"state":"valid"' in route.calls.last.request.read()


@respx.mock
def test_correct_edits_the_text(client, juan, tenant):
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    client.post(
        "/v1/memory/correct",
        json={"scope": "user", "memory_id": mem_id, "content": "uv, not pip"},
        headers=juan["headers"],
    )

    assert b'"text":"uv, not pip"' in route.calls.last.request.read()


@respx.mock
def test_a_missing_memory_is_a_404_not_a_backend_error(client, juan, tenant):
    # A syntactically valid but absent UUID: "ghost" would now be rejected by
    # the client's local UUID guard before the request is ever sent, so the
    # mocked route would never be hit.
    absent_id = "00000000-0000-0000-0000-000000000000"
    _mock_bank()
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{absent_id}").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    response = client.post(
        "/v1/memory/get",
        json={"scope": "user", "memory_id": absent_id},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEMORY_NOT_FOUND"


@respx.mock
def test_idor_a_memory_id_cannot_be_used_to_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """SPEC §20.1: authorization is by bank, never by object id. Alice naming a
    memory_id from juan's project must be refused BEFORE the id is used, so the
    request never reaches Hindsight at all."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    curate = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": "mem_1"}))

    response = client.post(
        "/v1/memory/forget",
        json={"scope": "project", "project_slug": "payments-api", "memory_id": "mem_1"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert curate.call_count == 0


@respx.mock
def test_idor_a_user_key_cannot_curate_another_users_memory(
    client, juan, alice, tenant
):
    _mock_bank()
    curate = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": "mem_1"}))

    response = client.post(
        "/v1/memory/forget",
        json={"scope": "user", "user_id": juan["user_id"], "memory_id": "mem_1"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert curate.call_count == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_curation_api.py -v`
Expected: FAIL — 404 on every new route.

- [ ] **Step 3: Extract the shared request base**

In `src/memory/api/memory.py`, add a base model above `RetainRequest` and have
the existing request models inherit it, so the pipeline has exactly one shape:

```python
class ScopedRequest(BaseModel):
    """Everything `_resolve_bank` needs, and nothing else.

    Every data-plane request carries this. `user_id` is meaningful only under
    scope=user (a master key naming its target); it is ignored under
    scope=project, where the project slug selects the bank.
    """

    scope: Scope
    user_id: str | None = None
    project_slug: str | None = None
    git_locator: str | None = Field(default=None, max_length=512)
```

`RetainRequest`, `RecallRequest` and `ReflectRequest` become
`class X(ScopedRequest):` carrying only their own extra fields. Widen
`_resolve_bank`'s annotation to `ScopedRequest`.

- [ ] **Step 4: Write `src/memory/api/curation.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from memory.api.app import current_principal
from memory.api.memory import (
    MemoryResponse,
    ScopedRequest,
    _resolve_bank,
    _strip_bank_id,
)
from memory.auth.principal import Principal
from memory.db import get_session
from memory.hindsight.client import get_client

router = APIRouter(prefix="/v1/memory", tags=["curation"])


class ListMemoriesRequest(ScopedRequest):
    q: str | None = None
    type: str | None = None
    state: str | None = None
    document_id: str | None = None
    limit: int = 100
    offset: int = 0


class MemoryIdRequest(ScopedRequest):
    memory_id: str


class ForgetRequest(MemoryIdRequest):
    reason: str | None = None


class CorrectRequest(MemoryIdRequest):
    content: str


def _bank(body: ScopedRequest, db: Session, principal: Principal) -> tuple:
    """Authorize first, always.

    The memory_id on these requests is meaningless outside the bank this
    resolves to (SPEC §20.1): it is never looked up globally, and a caller who
    cannot reach the bank is refused before their id is read at all.
    """
    bank_id, resolved_from, project_slug = _resolve_bank(body, db, principal)
    db.commit()
    return bank_id, resolved_from, project_slug


@router.post("/list", response_model=MemoryResponse)
def list_memories(
    body: ListMemoriesRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(body, db, principal)
    result = get_client().list_memories(
        bank_id,
        q=body.q,
        type=body.type,
        state=body.state,
        document_id=body.document_id,
        limit=body.limit,
        offset=body.offset,
    )
    return MemoryResponse(
        result=_strip_bank_id(result),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/get", response_model=MemoryResponse)
def get_memory(
    body: MemoryIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(body, db, principal)
    result = get_client().get_memory(bank_id, body.memory_id)
    return MemoryResponse(
        result=_strip_bank_id(result),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/forget", response_model=MemoryResponse)
def forget(
    body: ForgetRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Soft retirement, not deletion (SPEC §12.1).

    An agent that invalidates a fact must not be able to destroy the evidence,
    and a wrong invalidation is recoverable with /restore.
    """
    bank_id, resolved_from, project_slug = _bank(body, db, principal)
    result = get_client().curate(
        bank_id, body.memory_id, state="invalidated", reason=body.reason
    )
    return MemoryResponse(
        result=_strip_bank_id(result),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/restore", response_model=MemoryResponse)
def restore(
    body: MemoryIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(body, db, principal)
    result = get_client().curate(bank_id, body.memory_id, state="valid")
    return MemoryResponse(
        result=_strip_bank_id(result),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/correct", response_model=MemoryResponse)
def correct(
    body: CorrectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(body, db, principal)
    result = get_client().curate(bank_id, body.memory_id, text=body.content)
    return MemoryResponse(
        result=_strip_bank_id(result),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
```

- [ ] **Step 5: Wire the router**

In `create_app()`, beside the existing includes:

```python
    from memory.api import curation as curation_routes

    app.include_router(curation_routes.router)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_curation_api.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -m "not integration"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/memory/api/curation.py src/memory/api/memory.py src/memory/api/app.py \
        tests/test_curation_api.py
git commit -m "add memory curation routes with bank-scoped authorization"
```

---

### Task 5: Document routes

**Files:**
- Create: `src/memory/api/documents.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_documents_api.py`

**Interfaces:**
- Consumes: the same shared pipeline as Task 4;
  `HindsightClient.list_documents/get_document/delete_document`.
- Produces: `POST /v1/memory/documents/list`, `POST /v1/memory/documents/get`,
  `POST /v1/memory/documents/delete`.

**The rule that is easy to get wrong.** `document_id` is **caller-managed inside
the bank** and must NOT be namespaced by user or agent (SPEC §11.4). Two
authorized agents deliberately writing to `github:acme/payments-api:pr:382` is
the intended behavior, not a collision. `delete_document` is destructive and
irreversible — it removes the document and every memory derived from it — and is
intentionally available to any caller authorized for the bank, because its blast
radius is one document inside one already-authorized bank (§12.2).

- [ ] **Step 1: Write the failing test**

Create `tests/test_documents_api.py`. Reuse the `juan`, `alice`, `_headers` and
`_mock_bank` helpers exactly as written in `tests/test_curation_api.py`
(Task 4, Step 1) — copy them in; a shared conftest fixture for two arbitrary
users would be indirection for two files.

```python
@respx.mock
def test_list_documents(client, juan, tenant):
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents$").mock(
        return_value=httpx.Response(200, json={"documents": []})
    )

    response = client.post(
        "/v1/memory/documents/list",
        json={"scope": "user", "limit": 5},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params) == {"limit": "5", "offset": "0"}


@respx.mock
def test_a_document_id_is_not_namespaced_by_the_caller(client, juan, tenant):
    """SPEC §11.4: two authorized agents deliberately writing to the same
    logical source is the point. The id reaches Hindsight verbatim."""
    _mock_bank()
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/github:acme/api:pr:382"
    ).mock(return_value=httpx.Response(200, json={"id": "github:acme/api:pr:382"}))

    response = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": "github:acme/api:pr:382"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.url.path.endswith(
        "/documents/github:acme/api:pr:382"
    )


@respx.mock
def test_delete_document_is_reachable_by_any_authorized_caller(
    client, juan, master_headers, tenant
):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post("/v1/groups", json={"id": "grp_pay"}, headers=master_headers)
    client.put(
        f"/v1/groups/grp_pay/members/{juan['user_id']}", headers=master_headers
    )
    client.post(
        "/v1/projects",
        json={"project_slug": "shared", "owner": {"type": "group", "id": "grp_pay"}},
        headers=master_headers,
    )
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/doc_1"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.post(
        "/v1/memory/documents/delete",
        json={"scope": "project", "project_slug": "shared", "document_id": "doc_1"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.call_count == 1


@respx.mock
def test_a_missing_document_is_a_404(client, juan, tenant):
    _mock_bank()
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/ghost").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    response = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": "ghost"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


@respx.mock
def test_idor_a_document_id_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    delete = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.post(
        "/v1/memory/documents/delete",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "document_id": "doc_1",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert delete.call_count == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_documents_api.py -v`
Expected: FAIL — 404 on every new route.

- [ ] **Step 3: Write `src/memory/api/documents.py`**

Same shape as `curation.py`: import `ScopedRequest`, `MemoryResponse`,
`_resolve_bank` and `_strip_bank_id` from `memory.api.memory`, and define

```python
router = APIRouter(prefix="/v1/memory/documents", tags=["documents"])


class ListDocumentsRequest(ScopedRequest):
    q: str | None = None
    limit: int = 100
    offset: int = 0


class DocumentIdRequest(ScopedRequest):
    document_id: str
```

with three routes — `POST ""` → `list_documents`, `POST "/get"` →
`get_document`, `POST "/delete"` → `delete_document` — each authorizing through
`_resolve_bank`, committing, calling the matching client method, and returning
`MemoryResponse` with `resolved_from` and `project_slug`, exactly as
`curation.py` does. Give `delete_document` this docstring:

```python
    """Destructive and irreversible (SPEC §12.2).

    Removes the document and every memory derived from it. Deliberately
    available to any caller authorized for the bank: a document belongs to the
    shared bank namespace, not to whoever created it, and the blast radius is
    one document inside one already-authorized bank.
    """
```

The router prefix already ends in `/documents`, so the three decorators are
`POST "/list"`, `POST "/get"` and `POST "/delete"` — matching the paths the
tests call.

- [ ] **Step 4: Wire the router in `create_app()`**

```python
    from memory.api import documents as document_routes

    app.include_router(document_routes.router)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_documents_api.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 6: Run the whole suite and commit**

Run: `uv run pytest -m "not integration"`

```bash
git add src/memory/api/documents.py src/memory/api/app.py tests/test_documents_api.py
git commit -m "add document list, get and delete routes"
```

---

### Task 6: Operation routes

**Files:**
- Create: `src/memory/api/operations.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_operations_api.py`

**Interfaces:**
- Consumes: the shared pipeline; `HindsightClient.get_operation/list_operations/cancel_operation`.
- Produces: `POST /v1/memory/operations/list`, `POST /v1/memory/operations/get`,
  `POST /v1/memory/operations/cancel`.

**Three tools, not one.** SPEC §11.5 is explicit: do not collapse these into a
`manage_operations(action=...)`. `get` and `list` are read-only, `cancel`
mutates, and separate routes keep the eventual MCP annotations honest. There is
no `delete_operation` and no `retry_operation` in v1, even though Hindsight
offers both.

- [ ] **Step 1: Write the failing test**

Create `tests/test_operations_api.py`, reusing the same `juan`/`alice`/
`_mock_bank` helpers:

```python
@respx.mock
def test_list_operations_filters_by_status(client, juan, tenant):
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations$").mock(
        return_value=httpx.Response(200, json={"operations": []})
    )

    response = client.post(
        "/v1/memory/operations/list",
        json={"scope": "user", "status": "pending"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params)["status"] == "pending"


@respx.mock
def test_get_operation_returns_its_status(client, juan, tenant):
    # operation_id must be a syntactically valid UUID: the client now rejects
    # a non-UUID operation_id locally (see "Pinned Hindsight facts"), so a
    # bare "op_1" would never reach the mocked route.
    op_id = "33333333-3333-3333-3333-333333333333"
    _mock_bank()
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}").mock(
        return_value=httpx.Response(200, json={"id": op_id, "status": "completed"})
    )

    response = client.post(
        "/v1/memory/operations/get",
        json={"scope": "user", "operation_id": op_id},
        headers=juan["headers"],
    )

    assert response.json()["result"]["status"] == "completed"


@respx.mock
def test_cancel_uses_the_bare_delete_path_not_the_delete_suffix(client, juan, tenant):
    """DELETE .../operations/{id} cancels. .../{id}/delete removes a terminal
    operation and v1 does not expose it (SPEC §11.5)."""
    op_id = "33333333-3333-3333-3333-333333333333"
    _mock_bank()
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}$"
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))

    response = client.post(
        "/v1/memory/operations/cancel",
        json={"scope": "user", "operation_id": op_id},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.url.path.endswith(f"/operations/{op_id}")


@respx.mock
def test_a_missing_operation_is_a_404(client, juan, tenant):
    # A syntactically valid but absent UUID: "ghost" would now be rejected by
    # the client's local UUID guard before the request is ever sent.
    absent_id = "00000000-0000-0000-0000-000000000000"
    _mock_bank()
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{absent_id}"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    response = client.post(
        "/v1/memory/operations/get",
        json={"scope": "user", "operation_id": absent_id},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "OPERATION_NOT_FOUND"


@respx.mock
def test_idor_an_operation_id_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    cancel = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))

    response = client.post(
        "/v1/memory/operations/cancel",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "operation_id": "op_1",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert cancel.call_count == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_operations_api.py -v`
Expected: FAIL — 404 on every new route.

- [ ] **Step 3: Write `src/memory/api/operations.py`**

Same shape as `documents.py`:

```python
router = APIRouter(prefix="/v1/memory/operations", tags=["operations"])


class ListOperationsRequest(ScopedRequest):
    status: str | None = None
    type: str | None = None
    limit: int = 20
    offset: int = 0


class OperationIdRequest(ScopedRequest):
    operation_id: str
```

with `POST "/list"`, `POST "/get"` and `POST "/cancel"`. Put this on the cancel
route:

```python
    """Cancels a pending operation.

    Maps to DELETE .../operations/{id}. Hindsight also offers
    .../operations/{id}/delete (remove a terminal operation) and .../retry;
    v1 exposes neither (SPEC §11.5).
    """
```

- [ ] **Step 4: Wire the router in `create_app()` and run the tests**

Run: `uv run pytest tests/test_operations_api.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Run the whole suite and commit**

```bash
git add src/memory/api/operations.py src/memory/api/app.py tests/test_operations_api.py
git commit -m "add operation get, list and cancel routes"
```

---

### Task 7: Audit the rest of the master-key surface

**Files:**
- Modify: `src/memory/api/users.py`
- Modify: `src/memory/api/groups.py`
- Modify: `src/memory/api/memory.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: `memory.audit.record`.
- Produces: `current_on_behalf_of` dependency reading the `On-Behalf-Of` header;
  audit events `user.create`, `key.create`, `group.create`, `group.add_member`,
  `group.remove_member`, `memory.read_as_user`.

**Why this is here and not in Plan 2.** SPEC §20's MUST is "record master-key
actions, ownership changes and renames". Plan 2 delivered the ownership and
rename half. The master-key half needs a wire format for `on_behalf_of`
(SPEC §16.5: ACH "may call operations directly with the master key plus
`on_behalf_of` when acting for a human"), and that format is defined here.

The gap this closes, concretely: **a master key reading any user's private
memory bank currently leaves no trace at all.** That is §20.3's delegation case,
and it is the single most sensitive unaudited action in the service.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_audit.py`:

```python
def test_master_key_user_and_key_creation_are_audited(
    client, master_headers, tenant, session
):
    from memory.models import AuditEvent

    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers)

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "user.create" in actions
    assert "key.create" in actions


def test_group_membership_changes_are_audited(client, master_headers, tenant, session):
    from memory.models import AuditEvent

    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post("/v1/groups", json={"id": "grp_pay"}, headers=master_headers)
    client.put(f"/v1/groups/grp_pay/members/{user_id}", headers=master_headers)
    client.delete(f"/v1/groups/grp_pay/members/{user_id}", headers=master_headers)

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "group.create" in actions
    assert "group.add_member" in actions
    assert "group.remove_member" in actions


@respx.mock
def test_a_master_key_reading_a_users_bank_is_audited(
    client, master_headers, tenant, session
):
    """SPEC §20.3. A master key can reach any user's private memory; that must
    not be traceless."""
    from memory.models import AuditEvent

    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]

    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": user_id, "query": "anything"},
        headers=master_headers,
    )

    events = [e for e in session.query(AuditEvent).all() if e.action == "memory.read_as_user"]
    assert len(events) == 1
    assert events[0].resource == user_id


@respx.mock
def test_a_user_key_reading_its_own_bank_is_not_audited(client, juan, tenant, session):
    """Audit records delegated and privileged access, not ordinary use. A user
    reading their own memory on every agent start would drown the log."""
    from memory.models import AuditEvent

    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "anything"},
        headers=juan["headers"],
    )

    assert session.query(AuditEvent).count() == 0


def test_the_on_behalf_of_header_is_recorded(client, master_headers, tenant, session):
    from memory.models import AuditEvent

    client.post(
        "/v1/users",
        json={},
        headers={**master_headers, "On-Behalf-Of": "usr_alice"},
    )

    event = session.query(AuditEvent).filter_by(action="user.create").one()
    assert event.on_behalf_of == "usr_alice"


def test_a_user_key_cannot_claim_to_act_on_behalf_of_someone(
    client, juan, master_headers, tenant, session
):
    """on_behalf_of is delegation, and only the master key delegates. A user
    key sending the header must not have it recorded as fact."""
    from memory.models import AuditEvent

    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api"},
        headers={**juan["headers"], "On-Behalf-Of": "usr_alice"},
    )

    events = session.query(AuditEvent).all()
    assert all(e.on_behalf_of is None for e in events)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_audit.py -v`
Expected: FAIL — no `user.create` events exist.

- [ ] **Step 3: Add the `On-Behalf-Of` dependency**

In `src/memory/api/app.py`, beside `current_principal`:

```python
def current_on_behalf_of(
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Header()] = None,
) -> str | None:
    """The subject a master key is acting for (SPEC §16.5).

    Ignored for a user key. Delegation is a master-key capability, and a user
    key that sets the header would otherwise write an unverified claim into the
    audit trail — which is the one place a claim must not be taken on trust.
    It is provenance, never authorization evidence.
    """
    return on_behalf_of if principal.is_master else None
```

FastAPI maps the parameter name `on_behalf_of` to the `On-Behalf-Of` header
automatically.

- [ ] **Step 4: Record the events**

In each handler, add one `audit.record(...)` call before the existing
`db.commit()`. In `src/memory/api/users.py`:

```python
    audit.record(db, principal, "user.create", user.id, on_behalf_of=on_behalf_of)
```
```python
    audit.record(db, principal, "key.create", key.user_id, on_behalf_of=on_behalf_of)
```

In `src/memory/api/groups.py`, `group.create` on `group.id`,
`group.add_member` and `group.remove_member` on `f"{group_id}/{user_id}"`. Note
`remove_member` and `add_member` currently commit only when they actually
change something; record the event inside that same branch, so the log reflects
changes rather than requests.

Each handler gains
`on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)]`.

In `src/memory/api/memory.py`, inside `_resolve_bank`, after
`resolve_user_bank` succeeds:

```python
    if principal.is_master and body.user_id:
        # A master key reaching into a user's private bank. §20.3's delegation
        # case, and the only bank access in the service that is not the
        # caller's own — so it is the one that must not be traceless.
        audit.record(
            db, principal, "memory.read_as_user", body.user_id, on_behalf_of=on_behalf_of
        )
```

`_resolve_bank` needs `on_behalf_of` threaded through from each route; add it
as a parameter and pass the dependency's value at each call site.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_audit.py -v`
Expected: PASS.

- [ ] **Step 6: Run the whole suite and commit**

Run: `uv run pytest -m "not integration"`

```bash
git add src/memory/api/ tests/test_audit.py
git commit -m "audit master-key actions and record the On-Behalf-Of subject"
```

---

### Task 8: Prove it against a live Hindsight, and document it

**Files:**
- Modify: `scripts/smoke.sh`
- Modify: `README.md`
- Modify: `docs/PROJECT-STATE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a smoke run that exercises the full round trip against real
  Hindsight, and documentation that matches the shipped surface.

**Why the smoke test is not optional here.** Every route in this plan is tested
against `respx` mocks that assert *our* understanding of Hindsight's contract.
The mocks cannot tell us that understanding is wrong. Two of this plan's three
pinned traps — `memories/list`, cancel-vs-delete — are exactly the kind of thing
a mock will happily confirm forever.

- [ ] **Step 1: Extend `scripts/smoke.sh`**

Before the final `echo "PASS: ..."`, add a real curation round trip. This must
use `sync_retain`, not `retain`: the memory has to exist before it can be
listed, and async extraction has not finished when the call returns.

```bash
# Curation against real Hindsight: retain synchronously, find the memory,
# invalidate it, confirm it leaves the active set, restore it.
curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","content":"The deploy runbook lives in docs/runbooks/deploy.md."}' \
  >/dev/null

listed=$(curl -sf -X POST "${API}/v1/memory/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","limit":50}')
mem_id=$(echo "${listed}" | python3 -c \
  'import json,sys; m=json.load(sys.stdin)["result"]; print((m.get("memories") or m.get("items") or [{}])[0].get("id",""))')
[ -n "${mem_id}" ] \
  || { echo "FAIL: no memory listed after sync_retain" >&2; echo "${listed}" >&2; exit 1; }
echo "listed a memory: ${mem_id}"

curl -sf -X POST "${API}/v1/memory/forget" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d "{\"scope\":\"user\",\"memory_id\":\"${mem_id}\",\"reason\":\"smoke\"}" >/dev/null

after=$(curl -sf -X POST "${API}/v1/memory/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","limit":50}')
echo "${after}" | grep -q "${mem_id}" \
  && { echo "FAIL: forget left the memory in the active set" >&2; exit 1; }
echo "forget retired it from the active set"

curl -sf -X POST "${API}/v1/memory/restore" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d "{\"scope\":\"user\",\"memory_id\":\"${mem_id}\"}" >/dev/null
echo "restore brought it back"

# reflect, which is a different Hindsight endpoint from recall.
reflected=$(curl -sf -X POST "${API}/v1/memory/reflect" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","query":"where is the deploy runbook"}')
echo "${reflected}" | grep -qi "runbook" \
  || { echo "FAIL: reflect did not use the retained fact" >&2; echo "${reflected}" >&2; exit 1; }
echo "reflect answered from memory"

# No bank id anywhere in any of it.
for body in "${listed}" "${after}" "${reflected}"; do
  echo "${body}" | grep -qE '"bank_id"|user_[0-9a-f]{8}-|project_[0-9a-f]{8}-' \
    && { echo "FAIL: a bank id reached the client" >&2; exit 1; }
done
echo "no bank_id in any curation response"
```

and change the final line to:

```bash
echo "PASS: user and project memory, curation, reflect, isolated, no bank_id leak"
```

- [ ] **Step 2: Run it against the live stack**

```bash
docker compose up -d --build
docker compose run --rm api python -m alembic upgrade head
./scripts/smoke.sh
```

Expected: `PASS: user and project memory, curation, reflect, isolated, no bank_id leak`

If the memory-list response shape differs from what the `python3` extraction
expects, **that is a real finding, not a script bug** — the mocks in Tasks 4-6
encode the same assumption. Fix the code and the mocks together, and say so in
the report.

- [ ] **Step 3: Update `README.md`**

Extend the route table with the reflect, curation, document and operation
routes, each with its credential column. Add a short section stating that
`forget` invalidates rather than deletes and is reversible with `restore`, that
`delete_document` is irreversible and removes every memory derived from the
document, and that whole-bank clear and delete are deliberately absent from this
surface (admin API, Plan 4).

- [ ] **Step 4: Update `docs/PROJECT-STATE.md`**

Update the state table and "What works today". Add to the traps section
anything the live smoke run taught that the mocks did not — at minimum the three
pinned Hindsight traps from the top of this plan, since they are exactly the
"docs contradict the server" class the file already tracks.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke.sh README.md docs/PROJECT-STATE.md
git commit -m "prove curation and reflect against live Hindsight, update the docs"
```

---

## Done when

- `uv run pytest -m "not integration"` is green with no warnings.
- `./scripts/smoke.sh` prints
  `PASS: user and project memory, curation, reflect, isolated, no bank_id leak`.
- Every secondary-id route has an IDOR test proving the upstream call is never
  made for an unauthorized bank (SPEC §20.1's "explicit IDOR tests").
- A reserved metadata key returns `INVALID_METADATA` and nothing is retained.
- `grep -rn "bank_id" src/memory/api/` shows it only in the strippers, the
  pipeline locals and comments.

## Deliberately not in this plan

The MCP server and its 17 tools · directives and mental models (REST, API-only,
SPEC §14) · the admin plane: `clear_memories`, `delete_bank`, `GET /v1/admin/audit`,
retired-slug release (§16.4) · Helm packaging · the Memory Defense tier
verification (§25 item 13) · rate limiting (a §20 MUST no plan has claimed yet) ·
retrieval tags, which §13.6 excludes from v1 on purpose · `dry-run-refresh`,
`list_banks`, `create_bank`, `get_bank_stats`, `retry_operation` and
`delete_operation`, all excluded by §11.7.
