# v1c — Tag Surface Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make tags usable as a real filter — on reads and on mental models — and name them for what they do on each side.

**Architecture:** Writing a tag and filtering by one are different acts, so they stop sharing a parameter name. Reads gain a closed filter mode. Mental models, which today can only ever select the whole corpus, gain the same. One rule governs both: a filter that admits untagged rows is not a filter, so any narrowing uses the strict mode.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, pytest, ruff, uv.

**Testing discipline:** every task names ONE test file. Run only that file. `make test` and the full suite are FORBIDDEN until the final task.

---

## Background the executor needs

Hindsight offers five tag modes (`hindsight/client.py:389-391`): `any`, `all`, `any_strict`, `all_strict`, `exact`. The non-strict forms **also return untagged memories**. This is confirmed against `versioned_docs/version-0.9/developer/api/recall.mdx` in the Hindsight clone, and our own read path already states it (`read_models.py:311-313`):

> *"`tags_match` stays fixed at `all_strict` — AND-with-extras-allowed — never caller-settable: `all`/`any` would also return untagged memories and silently defeat the filter."*

That single sentence is why the naive version of this feature does not work. Mental models are pinned at `tags_match: Literal["all"]` (`mental_model_service.py:105`, `:146`; `builtin_models.py:14`). Adding a tag to `source_tags` without changing the mode yields a model that **looks scoped and is not**.

There is no boost or hint parameter upstream (`hindsight/client.py:350-361`). A tag filters, or it is not sent.

---

## Decisions this plan implements

1. **Names diverge:** `retain`/`sync_retain` keep `tags`; `recall`/`reflect` take `tags_filter` and `tags_filter_mode`; `RecallHit.tags` stays `tags` (it is what the retain wrote); mental models take `source_tags` and `source_tags_mode`.
2. **The caller chooses the mode**, through a closed enum — never Hindsight syntax.
3. **The default is the strict, narrowing mode.** Loosening is an explicit opt-out, so the lazy call is the safe call.
4. **`source_tags` accepts a superset** of the required pair.
5. **Built-in models move to `all_strict`.** Settled by review: observations inherit their source memory's tags under the default `observation_scopes` (`None`), which we never override, so nothing is lost. Verified at Hindsight tag `v0.9.1`, the deployed version.

**This is a breaking rename.** `recall(tags=…)` shipped in v0.5.0 and `TOOL_CONTRACT_SHA256` will move. It is cheap now because the only consumer is mid-integration.

---

## Task 1: The closed mode enum

**Files:**
- Modify: `src/memory/tags.py`
- Test: `tests/test_tags.py`

**Step 1: Write the failing test**

```python
def test_the_default_mode_narrows():
    """A caller who does not think about the mode must get the safe one.
    `all` admits untagged memories, so a lazy call with the loose default
    would return the whole corpus while looking filtered."""
    assert default_filter_mode() == "all"
    assert to_upstream("all") == "all_strict"

def test_no_caller_mode_ever_admits_untagged():
    for mode in FILTER_MODES:
        assert to_upstream(mode).endswith("_strict")
```

**Step 2-5:** Implement a closed set — two values, `all` and `any`, mapped to `all_strict` and `any_strict`. The caller-facing names carry no `_strict` suffix because every caller-facing mode is strict; exposing the distinction would invite the unsafe one.

```bash
git commit -m "feat(tags): add a closed caller-facing filter mode"
```

---

## Task 2: Rename on the read surface

**Files:**
- Modify: `src/memory/read_models.py:110` (`RecallRequest`), `:162` (the reflect request)
- Modify: `src/memory/mcp/memory_tools.py:333` (`recall`), `:408` (`reflect`)
- Test: `tests/test_read_api.py`

`tags` becomes `tags_filter`, and `tags_filter_mode` is added. `retain` and `sync_retain` keep `tags` — do not touch them. `RecallHit.tags` keeps its name: it reports what the retain wrote.

`read_models.py:296`'s internal `caller_tags` parameter is already well named and stays.

Note `tests/test_read_api.py` asserts the recall payload by exact dict equality, on purpose — it is the test that proves a hit carries nothing beyond its declared fields. Update the expectation; do not loosen the assertion.

```bash
git commit -m "feat(recall): rename tags to tags_filter and add the mode"
```

---

## Task 3: Wire the mode through to the filters

**Files:**
- Modify: `src/memory/read_models.py:289-320` (`resolve_filters`)
- Test: `tests/test_read_service.py`

`resolve_filters` currently hard-codes `tags_match="all_strict"`. It now takes the caller's mode and maps it. The docstring at `:311-313` must be rewritten: its claim that the mode is "never caller-settable" stops being true, and the reason it gives — that the loose modes defeat the filter — is now enforced by the enum instead. Say that.

```bash
git commit -m "feat(recall): honour the caller's filter mode"
```

---

## Task 4: `source_tags` accepts a superset

**Files:**
- Modify: `src/memory/mental_model_service.py:90-97` (`_exact_required_tags`), `:105`, `:146`
- Modify: `src/memory/api/mental_models.py:67`
- Test: `tests/test_mental_model_service.py`

**Step 1: Write the failing test**

```python
def test_a_custom_model_may_narrow_to_a_caller_tag():
    """Without this a custom model differs from the built-in only by
    source_query and budget: it synthesizes over the whole scope corpus.
    A per-agent model over one repo, or one subject, was impossible."""

def test_a_narrowed_model_cannot_use_a_mode_that_admits_untagged():
    """The trap this feature exists to avoid: extra source_tags with a loose
    mode produce a model that looks scoped and reads everything."""
```

**Step 3: Implement**

`_exact_required_tags` becomes a superset check: the required pair must be present, extras allowed, reserved prefixes still refused (`tags.RESERVED_PREFIXES`). `tags_match: Literal["all"]` becomes `source_tags_mode` over the same closed enum as Task 1, defaulting to the narrowing mode.

Rename the field to `source_tags_mode` in the request models. `tags_match` is Hindsight's vocabulary and the external surface avoids upstream vocabulary deliberately (`read_models.py:8`).

```bash
git commit -m "feat(models): let a custom mental model narrow by caller tags"
```

---

## Task 5: Built-ins move to `all_strict`

**Files:**
- Modify: `src/memory/builtin_models.py:14`, `:34`, `:58`
- Test: `tests/test_mental_model_service.py`

**Before writing code, confirm the premise still holds** in the deployed Hindsight:

```bash
grep -n "observation_scope_tags if observation_scope_tags is not None" \
  /home/jcm/Projects/hindsight/hindsight-api-slim/hindsight_api/engine/consolidate/consolidator.py
grep -n "observation_scopes" /home/jcm/Projects/hindsight/hindsight-api-slim/hindsight_api/engine/retain/types.py
grep -rn "observation_scopes" src/memory/
```

Expected: the observation inherits `m["tags"]` when the override is `None`; the default is `None`; and nothing in `src/memory/` ever sets it. **If any of those three is false, stop and report** — moving the built-ins would then silently discard every consolidation Hindsight makes, which is precisely what standing context exists to carry.

Bump `definition_version` on both built-ins so `reconcile_builtin` upgrades existing banks (`mental_model_service.py:591`). Without the bump, deployed banks keep the old mode for ever.

```bash
git commit -m "feat(models): narrow built-in source selection to tagged memories"
```

---

## Task 6: Regenerate the tool contract, then full sweep

The renames change four tool signatures, so `TOOL_CONTRACT_SHA256` moves. Regenerate it **after** the code is final, never to make a red test green.

```bash
make lint && make test
```

First and only full-suite run.

```bash
git commit -m "chore(mcp): regenerate the tool contract hash for the tag rename"
```

---

## Out of scope

- **REST `/v1/memory/reflect` stays untagged.** It uses its own local
  `RecallRequest` (`api/memory.py:127`, no `tags` field) and calls
  `get_client().reflect(bank_id, body.query)` with no tag forwarding at all
  -- unlike the MCP `reflect` tool, which has taken tags since v0.5.0. This
  plan renames the MCP tool's parameter to `tags_filter`/`tags_filter_mode`
  and leaves the legacy REST route as-is, so after this plan MCP `reflect`
  filters by tags and REST `reflect` still does not. Pre-existing asymmetry,
  not introduced here.
- **Tags as a semantic path.** A tag is metadata; the reranker never sees it. Making `sre` findable by searching "SRE" would mean appending tags to the retained text, so the stored memory stops being what the caller wrote. Rejected.
- **`tag_groups`.** Hindsight's compound and/or/not shape (`client.py:413`) stays unexposed. The closed two-value enum covers the known cases.
- **`exact` mode.** No caller need for it.
