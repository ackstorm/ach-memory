# Memory Quality Phase 1 Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Memory Quality Phase 1 delivery gaps by bounding the Full tier, exposing revision and cache age on every cached tier, and making the no-Full fallback explicit without moving host policy into synthesized memory.

**Architecture:** Keep `/v1/session-brief` as the single compiler for live Index and Full payloads. Give every compiled header a fixed-width cache-age field: live responses carry zero, and cache readers replace only its digits, so cached Index delivery remains inside the same 1800-character envelope and Full delivery remains inside its configured 2500-token upper bound. The stdio proxy stores timestamped JSON records; the Claude SessionStart hook stores a timestamped shell-readable record and accepts the previous owner-plus-body format during migration.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, Bash, pytest/respx.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§11.2–§11.5, §12, §16.2); sequencing and phase gate: `docs/plans/2026-08-29-memory-quality-program.md`.

## Global Constraints

- The delivered Index tier, including cache metadata, must remain at or below `1800` characters for Claude Code; its measured host cap remains `2048` characters.
- The dynamic Full tier must use the SPEC's provisional `max_tokens: 2500` budget and must drop whole semantic lines rather than truncate one.
- `brief_revision` remains monotonic; Index and Full compiled from one snapshot carry the same revision.
- A cached payload preserves its compiled revision and exposes its own age; `generated_at` remains profile freshness and must not be repurposed as cache age.
- Host-commanded policy remains in `plugins/*/activation.txt`; the SessionStart-delivered Full payload is activation policy plus the dynamic brief.
- Startup remains fail-open. A fetch or cache failure must not block the host and every SessionStart hook must exit zero.
- Cache content remains isolated by an exact API-key owner fingerprint; neither credentials nor fingerprints enter filenames or delivered context.
- Reads must not create banks, models, projects or other state.
- Existing cache files are migration input, not fatal errors: valid legacy entries remain readable for one refresh cycle and corrupt entries remain cache misses.
- Working State storage, transcript extraction, structured profiles and production-data hygiene are out of scope.

## File Map

- `src/memory/brief.py`: Full-tier selection, provisional token upper bound, fixed-width cache-age header field, and pure cache-age stamping helper.
- `src/memory/api/brief.py`: Pass the configured Full budget through the endpoint without changing the response shape.
- `src/memory/mcp/proxy.py`: Versioned Index-cache record, timestamp loading, legacy migration, and age stamping at delivery.
- `plugins/claude-code/scripts/session-start.sh`: Timestamped Full cache, legacy cache reader, age stamping, and explicit Index-only fallback.
- `tests/test_brief.py`: Compiler budget, whole-line selection, fixed header, revision and endpoint assertions.
- `tests/test_mcp_proxy.py`: Fresh/cached Index metadata, migration, owner isolation and corrupt-cache behavior.
- `tests/test_agent_bundle.py`: Live, cached, legacy and unavailable SessionStart payloads.

---

### Task 1: Bound the Full tier and reserve cache age in the compiled header

**Files:**

- Modify: `src/memory/brief.py:82-108, 278-289, 360-383, 490-511`
- Modify: `src/memory/api/brief.py:140-157`
- Test: `tests/test_brief.py:224-241, 453-497, 584-620, 658-732, 1054-1093`

**Interfaces:**

- Produces: `FULL_MAX_TOKENS: int = 2500`.
- Produces: `token_upper_bound(text: str) -> int` returning a conservative UTF-8 byte-token upper bound.
- Produces: `stamp_cache_age(instructions: str, age_seconds: int) -> str`, replacing a fixed-width header field without changing payload length.
- Changes: `compose_full(..., max_tokens: int = FULL_MAX_TOKENS) -> str`.
- Preserves: `compose_index(...) -> str`, `brief_revision`, `memory_protocol`, section headings and response fields.

- [ ] **Step 1: Write failing compiler tests**

Add tests beside the existing budget and tier tests in `tests/test_brief.py`:

```python
def test_the_full_tier_respects_its_provisional_token_upper_bound():
    user = _long("user")
    project = _long("project")

    text = brief.compose_full(
        revision=9,
        project_slug="acme-api",
        user=user,
        orientation=_orientation(),
        project=project,
        working_state=None,
        max_tokens=brief.FULL_MAX_TOKENS,
    )

    assert brief.token_upper_bound(text) <= 2500
    assert "user rule 0" in text
    assert "project rule 0" in text


def test_the_full_tier_drops_an_over_budget_line_whole():
    impossible = "sentinel-" + ("x" * 3000)

    text = brief.compose_full(
        revision=3,
        project_slug="acme-api",
        user=brief.Section(impossible, NOW.isoformat()),
        orientation=None,
        project=None,
        working_state=None,
        max_tokens=2500,
    )

    assert impossible not in text
    assert impossible[:100] not in text
    assert brief.token_upper_bound(text) <= 2500


def test_cache_age_can_change_without_changing_the_compiled_size():
    live = brief.compose_full(4, "acme-api", _long("user"), None, None, None)

    cached = brief.stamp_cache_age(live, 93)

    assert "cache-age 0000000093s" in cached
    assert len(cached) == len(live)
    assert "brief rev 4" in cached
```

Replace `test_the_full_tier_carries_every_line_the_index_tier_does`; strict superset behavior is not a SPEC invariant once both tiers are independently bounded:

```python
def test_bounded_tiers_keep_the_same_snapshot_identity():
    args = {
        "revision": 9,
        "project_slug": "acme-api",
        "user": _long("user"),
        "orientation": _orientation(),
        "project": _long("project"),
        "working_state": None,
    }

    index = brief.compose_index(**args, budget=1800)
    full = brief.compose_full(**args, max_tokens=2500)

    for text in (index, full):
        assert "brief rev 9 / protocol 2" in text
        assert "cache-age 0000000000s" in text
    assert brief.token_upper_bound(full) <= 2500
```

- [ ] **Step 2: Run the compiler tests and verify the intended failures**

Run:

```bash
uv run pytest \
  tests/test_brief.py::test_the_full_tier_respects_its_provisional_token_upper_bound \
  tests/test_brief.py::test_the_full_tier_drops_an_over_budget_line_whole \
  tests/test_brief.py::test_cache_age_can_change_without_changing_the_compiled_size -v
```

Expected: FAIL because `FULL_MAX_TOKENS`, `token_upper_bound`, `stamp_cache_age` and the `max_tokens` argument do not exist; the current Full tier includes every line.

- [ ] **Step 3: Add the fixed-width cache-age field and its pure stamper**

In `src/memory/brief.py`, bump `MEMORY_PROTOCOL` because the delivered header contract changes, then add:

```python
MEMORY_PROTOCOL = 2
FULL_MAX_TOKENS = 2500
_CACHE_AGE_WIDTH = 10
_CACHE_AGE_PREFIX = "cache-age "
_CACHE_AGE_RE = re.compile(r"cache-age [0-9]{10}s")


def _cache_age_field(age_seconds: int) -> str:
    bounded = min(max(age_seconds, 0), (10**_CACHE_AGE_WIDTH) - 1)
    return f"{_CACHE_AGE_PREFIX}{bounded:0{_CACHE_AGE_WIDTH}d}s"


def stamp_cache_age(instructions: str, age_seconds: int) -> str:
    """Stamp a compiled payload without changing its budgeted length."""
    return _CACHE_AGE_RE.sub(_cache_age_field(age_seconds), instructions, count=1)
```

Add `import re`. Extend `_header` so every live tier reserves the same field:

```python
return (
    f"-- ach-memory brief rev {revision} / protocol {MEMORY_PROTOCOL} / "
    f"{_cache_age_field(0)} / {scope} --"
)
```

Keep revision parsing human-readable and keep project scope in the same header.

- [ ] **Step 4: Implement conservative, whole-line Full selection**

Add the explicit upper-bound counter:

```python
def token_upper_bound(text: str) -> int:
    """Safe upper bound for byte-level host tokenizers.

    Counting UTF-8 bytes deliberately under-fills the provisional budget: a
    byte-level tokenizer cannot emit more tokens than the bytes it consumes.
    Replace this only when a concrete host tokenizer is available and measured.
    """
    return len(text.encode("utf-8"))
```

Change `compose_full` to reserve `_header(...)` and `INDEX_SECTION`, keep orientation whole, give each remaining section a first-line floor in authority order, then share remaining budget one whole line per section per round:

```python
def compose_full(
    revision: int,
    project_slug: str | None,
    user: Section | None,
    orientation: Orientation | None,
    project: Section | None,
    working_state: Section | None,
    max_tokens: int = FULL_MAX_TOKENS,
) -> str:
    header = _header(revision, project_slug)
    tail = INDEX_SECTION.rstrip("\n")
    separator_cost = token_upper_bound(_SEPARATOR)

    def part_cost(lines: list[str]) -> int:
        return token_upper_bound("\n".join(lines)) + separator_cost

    remaining = max_tokens - part_cost([header]) - part_cost([tail])
    sections = _sections(user, orientation, project, working_state)
    chosen: dict[str, list[str]] = {}

    orientation_section = next(item for item in sections if item[0] == "orientation")
    _, orientation_prefix, orientation_body = orientation_section
    orientation_lines = [*orientation_prefix, *orientation_body]
    orientation_cost = part_cost(orientation_lines)
    if orientation_body and orientation_cost <= remaining:
        chosen["orientation"] = list(orientation_body)
        remaining -= orientation_cost

    dynamic = [item for item in sections if item[0] != "orientation" and item[2]]
    for name, prefix, body in dynamic:
        floor_cost = part_cost([*prefix, body[0]])
        if floor_cost > remaining:
            break
        chosen[name] = [body[0]]
        remaining -= floor_cost

    while True:
        spent = False
        for name, _, body in dynamic:
            taken = len(chosen.get(name, []))
            if not taken or taken >= len(body):
                continue
            line_cost = token_upper_bound("\n" + body[taken])
            if line_cost <= remaining:
                chosen[name].append(body[taken])
                remaining -= line_cost
                spent = True
        if not spent:
            break

    parts = [header]
    parts += [
        "\n".join([*prefix, *chosen[name]])
        for name, prefix, _ in sections
        if name in chosen
    ]
    parts.append(tail)
    return _SEPARATOR.join(parts)
```

If the fixed header plus `INDEX_SECTION` alone exceeds a caller-supplied test budget, return those structural parts whole; do not truncate either. The production constant is large enough for both and tests must pin that assumption.

- [ ] **Step 5: Pass the configured Full budget through the endpoint**

In `src/memory/api/brief.py`, make the Full branch explicit:

```python
instructions = brief.compose_full(
    revision,
    project_slug,
    user_section,
    orientation,
    project_section,
    None,
    max_tokens=brief.FULL_MAX_TOKENS,
)
```

Do not add query-controlled Full budgets; the provisional limit is service policy, not caller input.

- [ ] **Step 6: Add endpoint-level budget and header assertions**

Extend the existing live Index/Full endpoint test in `tests/test_brief.py`:

```python
full = client.get(
    "/v1/session-brief",
    params={"scope": "user", "tier": "full", "format": "text"},
    headers=_headers(two_users),
)

assert full.status_code == 200
assert brief.token_upper_bound(full.text) <= brief.FULL_MAX_TOKENS
assert "cache-age 0000000000s" in full.text
```

Also assert the Index response, including the expanded header, remains within `brief.HOST_BUDGETS["claude-code"]`.

- [ ] **Step 7: Run the focused suite and commit**

Run:

```bash
uv run pytest tests/test_brief.py -v
uv run ruff check src/memory/brief.py src/memory/api/brief.py tests/test_brief.py
```

Expected: PASS.

Commit:

```bash
git add src/memory/brief.py src/memory/api/brief.py tests/test_brief.py
git commit -m "fix(brief): bound the full delivery tier"
```

---

### Task 2: Timestamp and visibly age the cached MCP Index

**Files:**

- Modify: `src/memory/mcp/proxy.py:167-276`
- Test: `tests/test_mcp_proxy.py:195-283`

**Interfaces:**

- Produces: `CachedIndex(instructions: str, stored_at: datetime)`.
- Changes: `load_cached_index(...) -> CachedIndex | None`.
- Changes: `store_cached_index(..., instructions: str, *, stored_at: datetime | None = None) -> None`.
- Consumes: `brief.stamp_cache_age(instructions, age_seconds)` from Task 1.
- Preserves: owner fingerprint comparison, atomic `0600` replacement, immediate cache-first startup and daemon refresh.

- [ ] **Step 1: Write failing cache-age and migration tests**

In `tests/test_mcp_proxy.py`, use a real protocol-2 header fixture:

```python
INDEX = (
    "-- ach-memory brief rev 42 / protocol 2 / "
    "cache-age 0000000000s / project acme-api --\n\n"
    "-- What else memory holds --"
)
```

Add:

```python
def test_a_cached_index_exposes_its_revision_and_age(tmp_path, monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    stored = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    now = datetime(2026, 8, 29, 10, 2, 3, tzinfo=UTC)
    proxy.store_cached_index(
        "https://memory.test", "k", "acme-api", None, INDEX, stored_at=stored
    )

    text = proxy.startup_instructions(
        "https://memory.test",
        "k",
        "acme-api",
        None,
        refresh=False,
        now=now,
    )

    assert "brief rev 42" in text
    assert "cache-age 0000000123s" in text
    assert len(text) == len(INDEX)


def test_a_legacy_index_cache_uses_its_file_mtime_for_age(tmp_path, monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    path = proxy._cache_path("https://memory.test", "acme-api", None)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"owner": proxy._cache_owner("k"), "instructions": INDEX}))
    legacy_time = datetime(2026, 8, 29, 10, 0, tzinfo=UTC).timestamp()
    os.utime(path, (legacy_time, legacy_time))

    cached = proxy.load_cached_index("https://memory.test", "k", "acme-api", None)

    assert cached is not None
    assert cached.instructions == INDEX
    assert cached.stored_at.timestamp() == legacy_time
```

Update existing tests that compare `load_cached_index(...)` directly with a string to compare `.instructions`.

- [ ] **Step 2: Run the cache tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_mcp_proxy.py::test_a_cached_index_exposes_its_revision_and_age \
  tests/test_mcp_proxy.py::test_a_legacy_index_cache_uses_its_file_mtime_for_age -v
```

Expected: FAIL because the cache has no timestamp model and `startup_instructions` has no injectable clock.

- [ ] **Step 3: Add a versioned cache record and legacy reader**

In `src/memory/mcp/proxy.py`, import `dataclass`, `UTC`, `datetime` and `memory.brief`, then add:

```python
@dataclass(frozen=True)
class CachedIndex:
    instructions: str
    stored_at: datetime
```

Write cache records as:

```python
{
    "version": 2,
    "owner": _cache_owner(api_key),
    "stored_at": (stored_at or datetime.now(UTC)).isoformat(),
    "instructions": instructions,
}
```

`load_cached_index` must:

1. keep the existing JSON/type/owner validation;
2. parse a version-2 `stored_at` as an aware datetime;
3. for the current unversioned `{owner, instructions}` record, use `datetime.fromtimestamp(path.stat().st_mtime, UTC)`;
4. return `None` for a malformed timestamp only after the mtime fallback also fails;
5. return `CachedIndex`, never expose the owner marker.

- [ ] **Step 4: Stamp age only when a cached Index is served**

Extend `startup_instructions` with a keyword-only injectable clock:

```python
def startup_instructions(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    *,
    refresh: bool = True,
    now: datetime | None = None,
) -> str:
```

On a cache hit:

```python
instant = now or datetime.now(UTC)
age_seconds = int(max((instant - cached.stored_at).total_seconds(), 0))
return brief.stamp_cache_age(cached.instructions, age_seconds)
```

Keep live fetched instructions at `cache-age 0000000000s`. The refresh thread stores the new response with its local receipt time; it does not alter the response's `brief_revision`.

- [ ] **Step 5: Preserve security and fail-open behavior in regression tests**

Update the existing owner-isolation, corrupt-cache and async-refresh tests to use `CachedIndex.instructions`. Add assertions that:

- a different API key never receives either the cached instructions or their revision;
- a corrupt/non-UTF-8 record remains a miss;
- a cache hit does not make a synchronous HTTP call;
- the stamped Index length remains `<= brief.HOST_BUDGETS["claude-code"]`.

- [ ] **Step 6: Run the focused suite and commit**

Run:

```bash
uv run pytest tests/test_mcp_proxy.py tests/test_cli.py -v
uv run ruff check src/memory/mcp/proxy.py tests/test_mcp_proxy.py
```

Expected: PASS.

Commit:

```bash
git add src/memory/mcp/proxy.py tests/test_mcp_proxy.py tests/test_cli.py
git commit -m "fix(mcp): expose cached index age"
```

---

### Task 3: Age the Full cache and state the Index-only fallback

**Files:**

- Modify: `plugins/claude-code/scripts/session-start.sh:19-69`
- Test: `tests/test_agent_bundle.py:199-328`

**Interfaces:**

- Consumes: protocol-2 Full text containing `cache-age 0000000000s` from Task 1.
- Produces cache record lines: owner fingerprint, Unix receipt timestamp, then Full instructions.
- Reads legacy cache record lines: owner fingerprint followed immediately by Full instructions.
- Preserves: three-second hard timeout, `curl -f`, credential isolation, no Python/Node/jq dependency and unconditional exit zero.

- [ ] **Step 1: Write failing cached, legacy and unavailable hook tests**

Add deterministic shell tests in `tests/test_agent_bundle.py`. Fake `date` through `PATH` alongside fake `curl` so age does not depend on wall-clock time.

For the cached path, first make fake `curl` write:

```text
-- ach-memory brief rev 17 / protocol 2 / cache-age 0000000000s / project acme-api --
```

Run once successfully, replace fake `curl` with `exit 1`, advance fake epoch by 65 seconds, then assert:

```python
assert "brief rev 17" in result.stdout
assert "cache-age 0000000065s" in result.stdout
```

For legacy migration, prewrite the existing format (`owner\nFULL TEXT`) and set its mtime. Assert it is served, carries a visible age when its protocol-2 header has the reserved field, and is replaced with the three-line format after the next successful fetch.

For no cache plus failed fetch, assert exact meaning rather than only the word “unavailable”:

```python
assert "full tier unavailable" in result.stdout.lower()
assert "only the mcp index" in result.stdout.lower()
assert "memory is absent" not in result.stdout.lower()
assert result.returncode == 0
```

- [ ] **Step 2: Run the hook tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_agent_bundle.py::test_the_full_cache_exposes_its_revision_and_age \
  tests/test_agent_bundle.py::test_a_legacy_full_cache_remains_a_safe_fallback \
  tests/test_agent_bundle.py::test_no_full_context_says_the_session_has_only_the_mcp_index -v
```

Expected: FAIL because the cache stores no receipt timestamp, fallback age is only a filesystem date, and the no-cache branch emits no limitation message.

- [ ] **Step 3: Write timestamped Full cache records atomically**

In `plugins/claude-code/scripts/session-start.sh`, keep the owner as line 1 and write the receipt epoch as line 2:

```bash
stored_at="$(date '+%s' 2>/dev/null || printf '0')"
{
  printf '%s\n' "$owner"
  printf '%s\n' "$stored_at"
  cat "$tmp"
} > "$cache_tmp" 2>/dev/null && mv -f "$cache_tmp" "$cache" 2>/dev/null || true
```

Keep the existing `0600` chmod, temporary-file cleanup and owner check.

- [ ] **Step 4: Read both cache formats and stamp the fixed-width age**

Replace `show_cache` with these helpers:

```bash
cache_body_start() {
  second="$(sed -n '2p' "$cache" 2>/dev/null || true)"
  case "$second" in
    ''|*[!0-9]*) printf '2\n' ;;
    *) printf '3\n' ;;
  esac
}

cache_epoch() {
  second="$(sed -n '2p' "$cache" 2>/dev/null || true)"
  case "$second" in
    ''|*[!0-9]*) date -r "$cache" '+%s' 2>/dev/null || printf '0\n' ;;
    *) printf '%s\n' "$second" ;;
  esac
}

show_cache() {
  [ -s "$cache" ] || return 1
  [ "$(sed -n '1p' "$cache" 2>/dev/null || true)" = "$owner" ] || return 1
  start="$(cache_body_start)"
  stored="$(cache_epoch)"
  now="$(date '+%s' 2>/dev/null || printf '0')"
  age=$((now > stored ? now - stored : 0))
  [ "$age" -le 9999999999 ] || age=9999999999
  padded="$(printf '%010d' "$age")"
  if tail -n "+$start" "$cache" | grep -Eq 'cache-age [0-9]{10}s'; then
    tail -n "+$start" "$cache" | \
      sed -E "s/cache-age [0-9]{10}s/cache-age ${padded}s/"
  else
    printf '[ach-memory] cached Full tier; age unknown\n'
    tail -n "+$start" "$cache"
  fi
}
```

Use the portable timestamp command already available in the test environment. Clamp the displayed age to `9999999999`, format it with `printf '%010d'`, and replace only `cache-age [0-9]{10}s` using `sed`. If a legacy protocol-1 payload has no reserved field, prepend a single line stating `cached full tier; age unknown` rather than altering or interpreting its memory content. That compatibility line is temporary and disappears after the background/live refresh writes protocol 2.

- [ ] **Step 5: Emit an explicit no-Full limitation**

In the failed-fetch branch, after `show_cache` fails, emit exactly:

```bash
printf '%s\n' \
  '[ach-memory] Full tier unavailable; this session has only the MCP Index if the MCP server started.'
```

The activation text has already been printed. Do not say that memory is empty, unavailable globally, or that recall cannot work.

- [ ] **Step 6: Run all bundle tests and commit**

Run:

```bash
uv run pytest tests/test_agent_bundle.py -v
uv run ruff check tests/test_agent_bundle.py
```

Expected: PASS. Confirm the script remains executable.

Commit:

```bash
git add plugins/claude-code/scripts/session-start.sh tests/test_agent_bundle.py
git commit -m "fix(hook): expose full cache age and index-only fallback"
```

---

### Task 4: Pin the Phase 1 delivered-payload gate

**Files:**

- Modify: `tests/test_brief.py`
- Modify: `tests/test_mcp_proxy.py`
- Modify: `tests/test_agent_bundle.py`
- Modify only if behavior changed: `docs/plans/2026-08-29-memory-quality-program.md`

**Interfaces:**

- Consumes: bounded compiler, versioned/stamped Index cache and timestamped/stamped Full cache.
- Produces: one regression gate covering live, cached and unavailable host payloads.

- [ ] **Step 1: Add the host-facing gate test**

Extend these existing tests instead of creating a second test harness:

- `test_the_index_tier_reaches_a_host_as_plain_text_inside_its_budget` owns live Index budget, revision and zero age;
- `test_a_cached_index_exposes_its_revision_and_age` owns cached Index revision, positive age and final 1800-character budget;
- `test_the_session_start_hook_fetches_the_full_tier_and_cannot_block` owns live Full revision, zero age and fail-open behavior;
- `test_the_full_cache_exposes_its_revision_and_age` owns cached Full revision and positive age;
- `test_no_full_context_says_the_session_has_only_the_mcp_index` owns unavailable behavior.

Use these exact assertions across those tests:

```python
assert len(delivered_index) <= 1800
assert brief.token_upper_bound(delivered_full) <= 2500
assert index_revision == full_revision
assert "cache-age 0000000000s" in live_index
assert "cache-age 0000000000s" in live_full
assert cached_index_age > 0
assert cached_full_age > 0
assert cached_index_revision == compiled_index_revision
assert cached_full_revision == compiled_full_revision
assert unavailable_hook.returncode == 0
assert "only the mcp index" in unavailable_hook.stdout.lower()
```

Parse revision and age only from the stable protocol-2 header format. Do not infer cache age from `generated_at`; that field remains source-profile freshness.

- [ ] **Step 2: Verify the static consumer contract is in delivered Full context**

Keep `test_activation_carries_the_brief_consumer_contract` and strengthen the hook-delivery assertion so it concatenates exactly what SessionStart emits: activation text first, dynamic Full second. Assert the compact rules required by SPEC §12 are present in that delivered stdout while `compose_full(...)` itself contains no host-commanded policy phrases.

- [ ] **Step 3: Run the complete Phase 1 gate**

Run:

```bash
uv run pytest tests/test_brief.py tests/test_mcp_proxy.py tests/test_agent_bundle.py tests/test_cli.py -v
uv run ruff check src tests
```

Expected: PASS.

- [ ] **Step 4: Run the repository gate and record unavailable integrations separately**

Run:

```bash
uv run pytest -q
```

Expected: PASS. If a test requires a live service or unavailable secret, report that test and prerequisite explicitly; do not silently waive it and do not mutate production data.

- [ ] **Step 5: Reconcile the HLD gate and commit**

Re-read the Phase 1 gate in `docs/plans/2026-08-29-memory-quality-program.md`. Edit it only if a delivered contract name changed during implementation; do not add Phase 2 scope.

Commit:

```bash
git add src tests plugins/claude-code/scripts/session-start.sh docs/plans/2026-08-29-memory-quality-program.md
git commit -m "test(brief): enforce the phase 1 delivery gate"
```

## Self-Review

- Spec coverage: Full budget (§11.4, §16.2), cached revision/age (§11.2, §16.2), fail-open and Index-only fallback (§11.4), delivered consumer contract (§12), and host-observed testing (§16.2) each map to a task.
- Explicit exclusions: Working State, capture, structured profiles, read-only recall and production hygiene remain in later phases.
- Migration: unversioned JSON Index caches use mtime; owner-plus-body Full caches use mtime and remain readable; corrupt records remain safe misses.
- Type consistency: Task 1 produces `stamp_cache_age`; Task 2 consumes it. `CachedIndex` is introduced before tests use `.instructions` and `.stored_at`.
- Placeholder scan: every implementation and test step names its concrete behavior and expected result.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-29-memory-quality-phase-1-closure.md`. Implement it only after review; it makes no production-memory mutation and does not authorize Phase 0 hygiene.
