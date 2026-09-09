# Caller Tags on retain / recall / reflect Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a caller attach its own tags when retaining a claim, and filter by them when recalling or reflecting — so an agent serving many repositories from one project bank can separate them with a `repo:<path>` convention.

**Architecture:** Tags already exist end to end; ach-memory just never exposed them. Retain derives four tags server-side (`retention._tags`); recall builds a filter from a fixed tag plus the caller's `memory_types` (`read_models.resolve_filters`). Both grow an additive caller-supplied list, validated and normalised by one shared helper so a tag written and a tag searched are byte-identical. Reflect needs its client method extended. The derived tags stay server-owned and non-overridable.

**Why it is safe:** Hindsight's `all_strict` is AND with extras allowed — *"'any'/'all' both also include untagged memories; 'any_strict'/'all_strict' exclude untagged; 'exact' matches the tag set exactly"* (`hindsight-api-slim/hindsight_api/mcp_tools.py:3487`). So a memory carrying `repo:x` in addition to the four derived tags still satisfies every existing recall filter, and still feeds `project-context`, whose source filter is `tags_match="all"` over `{schema:ach-retain-v1, validity:indefinite}`.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, MCP (`mcp.server.mcpserver`), pytest, ruff, uv.

**Testing discipline:** every task names ONE test file. Run only that file. The full suite (`make test`) is FORBIDDEN until Task 6.

---

## Before you start

**Check the base.** This plan was written against `main` at `c45f2ed`, before the built-in-only-standing-context work merged. That work touched the mental-model and context files, not the retain/recall path this plan edits — the one file both touch is `tests/test_mcp_tools.py`, whose `TOOL_CONTRACT_SHA256` it already moved. Every task here that changes a tool schema moves it again; each says to read the new digest out of the assertion failure rather than guess it.

**Read first:**
- `src/memory/retention.py:69-77` — `_tags`, where the four derived tags are built.
- `src/memory/read_models.py:265-283` — `resolve_filters`, where the recall filter is built. Note `tags_match="all_strict"`.
- `src/memory/hindsight/client.py:350-362` — `recall` already accepts `tags`, `tags_match`, `tag_groups`. Nothing calls them with tags.
- `src/memory/hindsight/client.py:441` — `reflect` sends only `{"query": query}`. Upstream `ReflectRequest` supports `tags`/`tags_match`.
- `src/memory/mcp/compact.py:31-50` — `_RECALL_FACT` and `_MEMORY_UNIT` both strip `tags`, on the stated grounds that v1 writes none.

**The trade this implements, so you recognise a bug from a design choice:** tag scoping is *convention-enforced*. Nothing rejects a retain that omits `repo:`, and nothing rejects a recall that forgets to filter. That is deliberate — it is the price of not resolving a project per event. Do not add enforcement that was not asked for.

---

## Task 1: The shared tag helper

One function used by every write and every read, so a tag written and a tag searched normalise identically. Symmetry is the whole point: convention-based scoping fails silently when `Repo:Foo` is stored and `repo:foo` is searched.

**Files:**
- Create: `src/memory/tags.py`
- Test: `tests/test_tags.py` (new)

**Step 1: Write the failing tests**

```python
import pytest

from memory.errors import InvalidTag
from memory.tags import RESERVED_PREFIXES, normalize_caller_tags


def test_tags_are_lowercased_stripped_deduped_and_sorted():
    """Normalisation is total and applied on BOTH write and read, so the
    agent cannot desynchronise what it stored from what it searches."""
    assert normalize_caller_tags([" Repo:Group/App ", "repo:group/app", "kind:ci"]) == (
        "kind:ci", "repo:group/app",
    )


def test_none_and_empty_normalize_to_no_tags():
    assert normalize_caller_tags(None) == ()
    assert normalize_caller_tags([]) == ()


@pytest.mark.parametrize("prefix", sorted(RESERVED_PREFIXES))
def test_a_server_owned_prefix_is_refused(prefix):
    """type:, basis:, schema: and validity: are derived server-side. A caller
    tag in those namespaces would corrupt typed curation and the mental-model
    source filter, so it is refused rather than merged."""
    with pytest.raises(InvalidTag):
        normalize_caller_tags([f"{prefix}anything"])


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "a" * 65, "repo:a b", "repo:a\nb", "repo:ünïcode", "x" * 3 + ":" * 2],
)
def test_a_malformed_tag_is_refused(bad):
    with pytest.raises(InvalidTag):
        normalize_caller_tags([bad])


def test_too_many_tags_are_refused():
    with pytest.raises(InvalidTag):
        normalize_caller_tags([f"kind:{n}" for n in range(9)])
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_tags.py -v
```
Expected: FAIL, module does not exist.

**Step 3: Implement**

`src/memory/tags.py`:

```python
"""Caller-supplied tags: one normalisation, used by every read and write.

Tag scoping is a convention (an agent is asked to pass `repo:<path>`), and a
convention breaks silently when the value stored and the value searched differ
by case or whitespace. Normalising in one place, applied identically on the
retain and the recall path, makes that class of bug unrepresentable rather
than merely unlikely.
"""

import re

from memory.errors import InvalidTag

# Derived server-side by retention._tags. A caller tag here would collide with
# typed curation and with the mental-model source filter, which selects on
# schema:ach-retain-v1 + validity:indefinite.
RESERVED_PREFIXES = frozenset({"type:", "basis:", "schema:", "validity:"})

MAX_TAGS = 8
MAX_TAG_LENGTH = 64

# One optional `namespace:` then a value. Slashes are allowed because the
# motivating tag is a forge path (`repo:group/sub/app`).
_TAG = re.compile(r"[a-z0-9][a-z0-9._-]*:?[a-z0-9][a-z0-9._/-]*")


def normalize_caller_tags(tags: list[str] | None) -> tuple[str, ...]:
    """Validate, lowercase, dedupe and sort. Raises InvalidTag on anything
    malformed, over-long, over-numerous, or in a server-owned namespace."""
    if not tags:
        return ()
    if len(tags) > MAX_TAGS:
        raise InvalidTag(f"at most {MAX_TAGS} tags")
    cleaned: set[str] = set()
    for raw in tags:
        tag = raw.strip().lower()
        if not tag or len(tag) > MAX_TAG_LENGTH or not _TAG.fullmatch(tag):
            raise InvalidTag("malformed tag")
        if any(tag.startswith(prefix) for prefix in RESERVED_PREFIXES):
            raise InvalidTag("that tag namespace is reserved")
        cleaned.add(tag)
    return tuple(sorted(cleaned))
```

Add `InvalidTag` to `src/memory/errors.py`, following the `DomainError` subclasses already there — match whatever error code / HTTP status convention the neighbours use (look at `ProjectInvalidSlug`, which is documented as "a typed 400, not a 500").

**Step 4: Run to verify**

```bash
uv run pytest tests/test_tags.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/tags.py src/memory/errors.py tests/test_tags.py
git commit -m "feat(tags): add caller-tag validation and normalisation"
```

---

## Task 2: Tags on retain

**Files:**
- Modify: `src/memory/retention.py:69-77`, the `TypedRetainRequest` model
- Modify: `src/memory/mcp/memory_tools.py:280-292` (`retain`), `:305-317` (`sync_retain`)
- Modify: `src/memory/api/memory.py` (the shared REST request model)
- Test: `tests/test_mcp_tools.py`

**Step 1: Write the failing tests**

Follow the file's existing `_retain_kwargs` / `_mock_bank` / `respx` conventions.

```python
@respx.mock
def test_retain_appends_caller_tags_to_the_derived_ones(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    key = call_tool.make_user()

    call_tool("retain", key, scope="user", content="uv, not pip",
              tags=["Repo:Group/App"], **_retain_kwargs())

    item = json.loads(route.calls.last.request.read())["items"][0]
    assert item["tags"] == [
        "type:fact", "basis:human_explicit",
        "schema:ach-retain-v1", "validity:indefinite",
        "repo:group/app",
    ]


@respx.mock
def test_retain_refuses_a_reserved_tag_namespace(call_tool):
    """The derived four are server-owned. A caller must not be able to forge
    a type:, basis:, schema: or validity: tag."""
    _mock_bank()
    key = call_tool.make_user()
    with pytest.raises(MCPToolError):
        call_tool("retain", key, scope="user", content="x",
                  tags=["schema:ach-retain-v1"], **_retain_kwargs())
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_mcp_tools.py -k "caller_tags or reserved_tag" -v
```
Expected: FAIL — `tags` is an unexpected argument.

**Step 3: Implement**

- `TypedRetainRequest` gains `tags: tuple[str, ...] = ()`, populated through `normalize_caller_tags` in a validator so REST and MCP share one gate.
- `retention._tags` appends `list(request.tags)` after the derived four. **Order matters to the test above and to nothing else** — put the derived tags first so a human reading a stored memory sees the server's classification before the caller's labels.
- `retain` and `sync_retain` gain `tags: list[str] | None = None`, passed through.
- Update both tool descriptions: caller tags are additive, server tags are not overridable, and a `repo:` tag is the convention for repo-specific claims.
- Leave `strategy="ach-exact-v1"` and the derived tags exactly as they are. The existing test asserting the frozen shape must be *extended* to cover the appended tags, not deleted — it is still the guard against a caller overriding classification.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_mcp_tools.py -v
```
Expected: the contract-hash test FAILS (the retain schema changed — correct). Read the digest from the failure, update `TOOL_CONTRACT_SHA256` and its comment to say `retain/sync_retain gained caller tags`, re-run to green.

**Step 5: Commit**

```bash
git add src/memory/retention.py src/memory/mcp/memory_tools.py src/memory/api/memory.py tests/test_mcp_tools.py
git commit -m "feat(retain): accept additive caller tags"
```

---

## Task 3: Tags on recall

**Files:**
- Modify: `src/memory/read_models.py:265-283` (`resolve_filters`, `RecallRequest`)
- Modify: `src/memory/mcp/memory_tools.py:341-350` (`recall`)
- Modify: `src/memory/read_service.py` (wherever `_recall_hits` reaches `client.recall`)
- Test: `tests/test_read_api.py` — confirm the real filename first with `ls tests/ | grep -i read`

**Step 1: Write the failing tests**

```python
def test_recall_ands_caller_tags_into_the_filter():
    """all_strict is AND-with-extras-allowed, so adding a caller tag narrows
    the result set and never excludes a memory for carrying more tags."""
    filters = resolve_filters("current", None, caller_tags=("repo:group/app",))
    assert filters.tags == ("schema:ach-retain-v1", "repo:group/app")
    assert filters.tags_match == "all_strict"


def test_recall_normalizes_caller_tags_the_same_way_retain_does():
    """The symmetry that makes the convention work at all."""
    filters = resolve_filters("current", None, caller_tags=(" Repo:Group/App ",))
    assert "repo:group/app" in filters.tags


def test_recall_passes_tags_through_to_the_client(...):
    # assert client.recall was called with tags=... and tags_match="all_strict"
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/<read test file> -k tags -v
```
Expected: FAIL.

**Step 3: Implement**

- `resolve_filters` gains a `caller_tags: tuple[str, ...] = ()` parameter, extending its `tags` list after the `type:` entries. Keep `tags_match="all_strict"` — do not make it caller-settable; `all`/`any` would also return untagged memories and quietly defeat the filter.
- `RecallRequest` gains `tags`, normalised by the same helper.
- **This collides with a documented invariant. Narrow it, do not delete it.** `read_models.py`'s
  module docstring says no model there ever carries "a raw Hindsight tag/tag-group", and
  `tests/test_read_service.py:90` pins that with a forbidden-field list containing `tags`. The
  Phase 5 plan behind it is consistently about *raw* syntax — "not a raw tag field" (line 97),
  "no raw tags/tag groups" (246), "the caller never controls Hindsight filter syntax" (258) — and
  a normalised, closed-charset label ANDed into a server-owned filter is a value, not syntax. The
  security half is untouched: the bank is resolved from `scope`/`project_slug`, and `all_strict`
  means a caller tag can only narrow inside it. So:
    - Rewrite the docstring sentence to say caller `tags` are the one deliberate narrowing, and
      *why it is safe* (validated by `memory.tags`, ANDed under a server-owned `all_strict`, inside
      a server-resolved bank). Do not describe the invariant as obsolete — `tag_groups`,
      `tags_match`, `git_locator`, bank and tenant ids all stay banned.
    - Remove `tags` from the **line 90 `RecallRequest`** list ONLY. The line 98 `HistoryRequest`
      list keeps it: history addresses one memory by id and never gains a tag filter.
    - **Add `tags_match` to the line 90 list.** It is the guard that has become load-bearing:
      `all`/`any` also return untagged memories and would silently defeat the filter.
- `recall` (MCP) and the REST route gain `tags: list[str] | None = None`.
- `client.recall` already takes `tags`/`tags_match` — pass them; add nothing to the client.

**Step 4: Run to verify**

```bash
uv run pytest tests/<read test file> tests/test_mcp_tools.py -v
```
Expected: PASS, with `TOOL_CONTRACT_SHA256` moved again — same procedure.

**Step 5: Commit**

```bash
git add -A
git commit -m "feat(recall): filter by caller tags"
```

---

## Task 4: Tags on reflect

The only path needing a client change.

**Files:**
- Modify: `src/memory/hindsight/client.py:441`
- Modify: `src/memory/mcp/memory_tools.py:372-385` (`reflect`)
- Test: `tests/test_hindsight_client.py` — confirm the filename first

**Step 1: Write the failing test**

```python
@respx.mock
def test_reflect_sends_caller_tags_upstream():
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect$").mock(
        return_value=httpx.Response(200, json={"answer": "..."})
    )
    get_client().reflect("bank-1", "why?", tags=["repo:group/app"], tags_match="all_strict")

    body = json.loads(route.calls.last.request.read())
    assert body["tags"] == ["repo:group/app"]
    assert body["tags_match"] == "all_strict"


@respx.mock
def test_reflect_without_tags_sends_no_tag_keys():
    """An untagged reflect must not start sending tags:[] — upstream treats a
    tagged request differently from an untagged one."""
    ...
    assert "tags" not in body
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_hindsight_client.py -k reflect -v
```
Expected: FAIL, unexpected keyword.

**Step 3: Implement**

`client.reflect` gains `tags: list[str] | None = None, tags_match: str | None = None` and includes them in the body **only when present** — Hindsight's `ReflectRequest` defaults differ between a tagged and an untagged model, so sending empty keys is not equivalent to omitting them. Then expose `tags` on the `reflect` MCP tool and its REST route, normalised by the shared helper.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_hindsight_client.py tests/test_mcp_tools.py -v
```
Expected: PASS, contract hash moved again.

**Step 5: Commit**

```bash
git add -A
git commit -m "feat(reflect): filter by caller tags"
```

---

## Task 5: Stop stripping tags from reads

`compact.py` deletes `tags` from every fact and memory unit on the stated grounds that *"SPEC §13.6: v1 writes no retrieval tags, so this is [] on every read."* Tasks 2-4 make that false. Left in place, an agent can filter by `repo:` but can never see which repo a recalled fact came from.

**Files:**
- Modify: `src/memory/mcp/compact.py:31-50`
- Test: `tests/test_compact.py` — confirm the filename first

**Step 1: Write the failing test**

```python
def test_recall_facts_keep_their_tags():
    """Tags became caller-visible the moment retain started writing them:
    filtering by repo: is useless if the answer never says which repo."""
    fact = {"text": "x", "chunk_id": "c", "tags": ["repo:group/app"], "entities": []}
    assert compact_recall_fact(fact)["tags"] == ["repo:group/app"]
    assert "chunk_id" not in compact_recall_fact(fact)
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_compact.py -k tags -v
```
Expected: FAIL — tags stripped.

**Step 3: Implement**

Remove `"tags"` from `_RECALL_FACT` and `_MEMORY_UNIT`, and replace the justifying comment with the current truth: caller tags are written from v0.4.x, so tags are how a reader tells scoped claims apart. Leave `_DOCUMENT`'s `tags` stripped unless a test shows a caller needs it — documents are not the retain path.

**Step 4: Run to verify**

```bash
uv run pytest tests/test_compact.py -v
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/mcp/compact.py tests/test_compact.py
git commit -m "feat(mcp): return tags on reads now that retain writes them"
```

---

## Task 6: Full sweep

**Step 1: Instructions and docs**

The MCP server instructions (`memory/mcp/server.py`, and whatever `test_instructions_carry_the_static_mcp_contract_to_every_caller` pins) tell every caller how to use this surface. They must now say: tags are additive, server-derived tags cannot be overridden, and `repo:<path>` is the convention for repo-specific claims. Update `SPEC-v1.md` §11 and `README.md` alongside.

**Step 2: Lint and full suite**

```bash
make lint && make test
```
Expected: clean, all pass. Expect work in `tests/test_mcp_surface_honesty.py`, `tests/test_unknown_fields.py` and `tests/test_content_caps.py` — all three assert on tool schemas that just changed.

**Step 3: Commit**

```bash
git add -A
git commit -m "docs: document caller tags on retain, recall and reflect"
```

---

## Out of scope

- **Mental models filtered by caller tags.** The obvious follow-up: a `repo:`-scoped custom model. Blocked today by the validator requiring `source_tags` to equal `{schema:ach-retain-v1, validity:indefinite}` exactly (`api/mental_models.py:68`, `mental_model_service.py:93`), and by the 5-custom-model cap. Worth doing only if someone asks.
- **Enforcing the convention.** Nothing rejects a retain without `repo:`, by design.
- **`tags_match` as a caller argument.** Fixed at `all_strict`. `all`/`any` also return untagged memories, which silently defeats the filter.
- **Proxy project-only mode.** Discussed and deferred — ach-agent does its own scope/project abstraction in its facade, and the stdio proxy cannot serve that case anyway without putting the API key in the agent's environment.
