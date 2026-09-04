# ach-memory v0.4.0 Typed Retain and Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace transcript-derived memory writes with one exact, typed, sanitized and idempotent agent-retain path, then make correction, forgetting, expiry and current reads safe against indeterminate Hindsight outcomes.

**Architecture:** The agent submits one durable claim plus bounded evidence. ACH stores sanitized provenance and lifecycle control in PostgreSQL, retains only canonical content into Hindsight with `ach-exact-v1`, and treats Hindsight as authoritative for searchable facts and derived graph state. Curation and lazy expiry use the shared bank-currentness barrier created by the foundation plan; no daemon, transcript queue or semantic classifier is introduced.

**Tech Stack:** Python 3.12, FastAPI/Pydantic 2, SQLAlchemy 2, Hindsight 0.9.2 HTTP API, FastMCP, pytest/respx.

**Spec:** `docs/specs/2026-09-03-ach-memory-v0.4.0.md`

**Execution branch:** Create `feat/ach-memory-v0.4.0-retain-lifecycle` from the verified foundation HEAD and record that SHA in the execution notes before Task 1.

## Execution notes — foundation handoff

Pre-handoff foundation SHA: `aa7784e122bbd21127546c5051b8d6a0889e2d1a`. This is the recorded code baseline. Neither successor may create an Alembic revision or rename/edit closed enums or shared table names; stop both tracks before any such change.

## Global Constraints

- Requires the verified HEAD from `2026-09-04-ach-memory-v0-4-0-foundation.md`.
- May execute in parallel with the mental-model-governance plan; do not add migrations or edit `src/memory/models.py`, `src/memory/mcp/model_tools.py` or built-in definitions.
- One retain call contains one independently correctable claim, at most 4 KiB UTF-8 after canonical normalization.
- Evidence contains one to four items, each at most 1 KiB and at most 4 KiB total; evidence never enters any Hindsight field.
- Closed fields are exactly the `memory_types.py` literals; `agent_inferred`, caller tags, `valid_from`, `profile_eligible` and `evidence_only` are rejected.
- `recorded_at` is the database/server clock; caller `valid_until` must include an offset and be later than `recorded_at`.
- Ordinary retain never creates an unknown project. No public response or log exposes `bank_id`.
- `retain` waits only for upstream acceptance; `sync_retain` uses the same async operation and polls for at most 30 seconds.
- There is no pre-ACK eventual-delivery guarantee and no background worker.
- `recall`, `reflect`, current get/list and model delivery withhold a bank while an ACH-mediated safety mutation has unknown outcome.
- Production flags and Phase 0 cleanup remain untouched.

---

### Task 1: Build mechanical claim and evidence sanitization

**Files:**
- Create: `src/memory/sanitization.py`
- Modify: `src/memory/capture/local.py` only to import shared secret patterns temporarily; this file is removed by the final plan
- Create: `tests/test_sanitization.py`
- Modify: `tests/test_capture_local.py`

**Interfaces:**
- Consumes: raw canonical claim and `RetainEvidence` values.
- Produces: `normalize_claim(content: str) -> str`, `sanitize_evidence(items: tuple[RetainEvidence, ...]) -> tuple[SanitizedEvidence, ...]`, `SanitizedEvidence`; raises `ContentRejectedBySanitizer` when canonical content would need secret redaction or evidence has no meaningful survivor.

- [ ] **Step 1: Write failing normalization and secret tests**

```python
def test_claim_allows_only_mechanical_normalization():
    assert normalize_claim("Cafe\u0301\r\nuses   spaces  \n") == "Café\nuses spaces\n"


def test_claim_with_secret_is_rejected_not_rewritten():
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim("Deploy with API_TOKEN=secret-value")


def test_evidence_redacts_independently_and_requires_meaning():
    kept = sanitize_evidence((
        RetainEvidence(kind="tool_result", raw="PASS API_TOKEN=secret-value"),
        RetainEvidence(kind="user_quote", raw="Keep the public decision."),
    ))
    assert kept[0].raw == "PASS [redacted]"
    assert kept[1].raw == "Keep the public decision."
    with pytest.raises(ContentRejectedBySanitizer):
        sanitize_evidence((RetainEvidence(kind="tool_result", raw="API_TOKEN=secret-value"),))
```

- [ ] **Step 2: Run the sanitizer tests to verify failure**

Run: `rtk uv run pytest tests/test_sanitization.py -q`

Expected: collection fails because `memory.sanitization` does not exist.

- [ ] **Step 3: Implement the mechanical normalizer and shared redactor**

```python
def normalize_claim(content: str) -> str:
    normalized = unicodedata.normalize("NFC", content).replace("\r\n", "\n")
    normalized = "\n".join(_HORIZONTAL_WS.sub(" ", line).rstrip() for line in normalized.split("\n"))
    if not normalized.strip() or contains_secret(normalized):
        raise ContentRejectedBySanitizer("canonical content cannot be stored safely")
    if len(normalized.encode("utf-8")) > 4096:
        raise ContentTooLarge("content exceeds 4096 bytes")
    return normalized


def sanitize_evidence(items: tuple[RetainEvidence, ...]) -> tuple[SanitizedEvidence, ...]:
    kept = []
    for item in items:
        raw = redact_secrets(unicodedata.normalize("NFC", item.raw).replace("\r\n", "\n"))
        if raw.strip() and raw.strip() != "[redacted]":
            kept.append(SanitizedEvidence(kind=item.kind, raw=raw, source_ref=sanitize_ref(item.source_ref)))
    if not kept:
        raise ContentRejectedBySanitizer("at least one meaningful evidence item is required")
    if len(json.dumps([item.model_dump() for item in kept]).encode("utf-8")) > 4096:
        raise ContentTooLarge("evidence exceeds 4096 bytes")
    return tuple(kept)
```

Move the fixed credential-pattern redactor out of `capture/local.py`; do not move transcript record parsing, shell classification or transcript caps.

- [ ] **Step 4: Verify sanitizer and temporary capture compatibility**

Run: `rtk uv run pytest tests/test_sanitization.py tests/test_capture_local.py -q`

Expected: all selected tests pass and capture's existing redaction cases still use the shared primitive.

- [ ] **Step 5: Commit the retained sanitizer primitive**

```bash
rtk git add src/memory/sanitization.py src/memory/capture/local.py tests/test_sanitization.py tests/test_capture_local.py
rtk git commit -m "feat(memory): sanitize typed claims and evidence"
```

### Task 2: Submit exact retains through a durable provenance record

**Files:**
- Create: `src/memory/retention.py`
- Modify: `src/memory/retained_records.py`
- Inspect: `src/memory/hindsight/client.py`
- Inspect: `src/memory/hindsight/paths.py`
- Create: `tests/test_retention.py`
- Modify: `tests/test_hindsight_client.py`

**Interfaces:**
- Consumes: `TypedRetainRequest`, `LogicalBankRef`, `normalize_claim`, `sanitize_evidence`, `accept_retain`, `HindsightClient.retain_items` and `get_operation`.
- Produces: `submit_retain(db, principal, request, *, client, wait, clock, sleeper) -> TypedRetainResponse`, deterministic `document_id_for(operation_id)`, `refresh_operation_state(db, bank, operation_id) -> TypedRetainResponse`.

- [ ] **Step 1: Write failing exact-retain and idempotency tests**

```python
def test_submit_retain_sends_only_claim_and_reserved_tags(db, principal, typed_request, client):
    result = submit_retain(db, principal, typed_request, wait=False, client=client)
    item = client.retain_items.call_args.args[1][0]
    assert item.content == "Project slugs remain stable through aliases."
    assert item.context is None
    assert item.metadata == {"ach_record_id": result.record_id}
    assert item.tags == ["type:constraint", "basis:human_explicit",
                         "schema:ach-retain-v1", "validity:indefinite"]
    assert item.observation_scopes is None
    assert item.strategy == "ach-exact-v1"
    assert "evidence" not in item.to_payload()


def test_retry_reuses_document_and_operation(db, principal, typed_request, client):
    first = submit_retain(db, principal, typed_request, wait=False, client=client)
    second = submit_retain(db, principal, typed_request, wait=False, client=client)
    assert first.record_id == second.record_id
    assert client.retain_items.call_args_list[0].kwargs["operation_id"] == str(typed_request.operation_id)
    assert client.retain_items.call_args_list[1].kwargs["operation_id"] == str(typed_request.operation_id)
```

- [ ] **Step 2: Run tests and verify the service is absent**

Run: `rtk uv run pytest tests/test_retention.py tests/test_hindsight_client.py -q`

Expected: retention tests fail to import `submit_retain`.

- [ ] **Step 3: Implement deterministic identity and exact upstream representation**

```python
def document_id_for(operation_id: UUID) -> str:
    return f"ach-retain-{operation_id.hex}"


def _tags(request: TypedRetainRequest) -> list[str]:
    validity = "expiring" if request.valid_until is not None else "indefinite"
    return [f"type:{request.memory_type}", f"basis:{request.basis}",
            "schema:ach-retain-v1", f"validity:{validity}"]
```

`submit_retain` must resolve the logical bank with `create=False`, sanitize before persistence, call `accept_retain`, commit the pending record before the network call, submit one `RetainItem(update_mode="replace")` through the existing `HindsightClient.retain_items`, and mark `upstream_state="accepted"` only after Hindsight returns an operation identity matching the requested identity. The foundation has already frozen the required `retain_items` and `get_operation` signatures; do not edit the shared client or paths from this parallel branch. A transport failure before proof leaves the record pending and returns the existing typed Hindsight error; retry is safe because operation and document IDs are stable.

Use this exact service boundary so tests can replace time without sleeping:

```text
submit_retain(db: Session, principal: Principal, request: TypedRetainRequest, *,
              client: HindsightClient, wait: bool,
              clock: Callable[[], float] = monotonic,
              sleeper: Callable[[float], None] = sleep)
    -> TypedRetainResponse
```

No caller gets an alternate retain path.

- [ ] **Step 4: Implement bounded sync polling over the async operation**

```python
deadline = monotonic() + 30.0
while wait and monotonic() < deadline:
    operation = client.get_operation(bank.bank_id, str(request.operation_id))
    if operation["status"] in {"completed", "failed"}:
        return refresh_operation_state(db, bank, str(request.operation_id), operation=operation)
    sleep(min(0.25, max(deadline - monotonic(), 0)))
return TypedRetainResponse(
    record_id=str(row.id), operation_id=request.operation_id,
    document_id=row.document_id, status="pending",
    recorded_at=row.recorded_at, valid_until=row.valid_until,
)
```

Do not call upstream with `async=false`. Hydrate `source_memory_id` only from a terminal operation or a read-back of the stable document; never fabricate it from `document_id`.

- [ ] **Step 5: Run and commit retention service tests**

Run: `rtk uv run pytest tests/test_retention.py tests/test_retained_records.py tests/test_hindsight_client.py -q`

Expected: exact tags, no evidence leakage, stable retry and 30-second pending behavior pass.

```bash
rtk git add src/memory/retention.py src/memory/retained_records.py tests/test_retention.py tests/test_hindsight_client.py
rtk git commit -m "feat(memory): retain one exact typed claim"
```

### Task 3: Replace REST and MCP retain with the typed contract

**Files:**
- Modify: `src/memory/api/memory.py`
- Modify: `src/memory/mcp/memory_tools.py`
- Modify: `tests/test_memory_api.py`
- Modify: `tests/test_mcp_tools.py`
- Modify: `tests/test_mcp_surface_honesty.py`

**Interfaces:**
- Consumes: `TypedRetainRequest`, `submit_retain` and current principal/session pipelines.
- Produces: REST `POST /v1/memory/retain`, `POST /v1/memory/sync_retain`; MCP `retain` and `sync_retain` with matching semantic fields. MCP generates a UUID before the first network attempt; direct REST requires one.

- [ ] **Step 1: Replace old expectations with failing typed-surface tests**

```python
def test_rest_retain_requires_typed_fields_and_existing_project(client, user_headers):
    response = client.post("/v1/memory/retain", headers=user_headers, json={
        "scope": "project", "project_slug": "missing", "content": "Use PostgreSQL.",
        "memory_type": "decision", "basis": "human_explicit",
        "trigger": "agent_proactive", "operation_id": str(uuid.uuid4()),
        "evidence": [{"kind": "user_quote", "raw": "We will use PostgreSQL."}],
    })
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_mcp_retain_exposes_type_basis_trigger_expiry_and_evidence(tool_schema):
    properties = tool_schema("retain")["inputSchema"]["properties"]
    assert {"memory_type", "basis", "trigger", "valid_until", "evidence"} <= properties.keys()
    assert "metadata" not in properties and "update_mode" not in properties
```

- [ ] **Step 2: Run surface tests and observe old schema failure**

Run: `rtk uv run pytest tests/test_memory_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -q`

Expected: failures show the old `metadata/update_mode` contract and explicit-human-only descriptions.

- [ ] **Step 3: Route both surfaces through one request model and service**

```python
@router.post("/retain", response_model=TypedRetainResponse, status_code=202)
def retain(body: TypedRetainRequest, principal=Depends(current_principal), db=Depends(get_session)):
    return submit_retain(db, principal, body, client=get_client(), wait=False)


@router.post("/sync_retain", response_model=TypedRetainResponse)
def sync_retain(body: TypedRetainRequest, principal=Depends(current_principal), db=Depends(get_session)):
    return submit_retain(db, principal, body, client=get_client(), wait=True)
```

The MCP adapter constructs the same `TypedRetainRequest`, generates `operation_id=uuid.uuid4()` when omitted, and returns it so transport middleware can reuse it. Remove `EXPLICIT_RETAIN_*`, caller metadata, `candidate_verbatim`, append mode and lazy project creation from these paths.

- [ ] **Step 4: Verify errors, annotations and response redaction**

Run: `rtk uv run pytest tests/test_memory_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py tests/test_bank_id_redaction.py tests/test_unknown_fields.py -q`

Expected: surfaces match, project misses create no row, unknown fields are 422, and neither evidence nor bank ID is echoed.

- [ ] **Step 5: Commit the only semantic write surface**

```bash
rtk git add src/memory/api/memory.py src/memory/mcp/memory_tools.py tests/test_memory_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py
rtk git commit -m "feat(memory): expose typed proactive retain"
```

### Task 4: Adapt recall and history to v0.4.0 current memories

**Files:**
- Modify: `src/memory/read_models.py`
- Modify: `src/memory/read_service.py`
- Modify: `src/memory/api/read.py`
- Modify: `src/memory/api/memory.py`
- Modify: `src/memory/mcp/memory_tools.py`
- Modify: `tests/test_read_service.py`
- Modify: `tests/test_read_api.py`
- Modify: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: server tags `type:*`, `basis:*`, `schema:ach-retain-v1`, `validity:*`; `BankCurrentness` repository.
- Produces: active-by-default recall, authorized lifecycle inspection, and `ensure_current_read_allowed(db, bank) -> None`; no eligibility/profile filtering.

- [ ] **Step 1: Write failing filter and barrier tests**

```python
def test_current_recall_uses_v040_tags(hindsight, service, bank):
    service.recall(bank, "database", view="current", memory_types=("decision",))
    body = hindsight.recall.call_args.kwargs
    assert body["tags"] == ["schema:ach-retain-v1", "type:decision"]
    assert "profile_eligible" not in repr(body)


def test_withheld_bank_returns_currentness_unavailable(db, withheld_bank, service):
    with pytest.raises(BankCurrentnessUnavailable):
        service.recall(withheld_bank, "anything", view="current")
```

- [ ] **Step 2: Run read tests and confirm old taxonomy failure**

Run: `rtk uv run pytest tests/test_read_service.py tests/test_read_api.py tests/test_mcp_tools.py -q`

Expected: tests fail because filters still use `profile_eligible`, `evidence_only` or old `ProfileKind` values.

- [ ] **Step 3: Replace eligibility filters with closed v0.4.0 filters**

```python
def resolve_filters(view: View, memory_types: tuple[MemoryType, ...] | None) -> RecallFilters:
    tags = ["schema:ach-retain-v1"]
    if memory_types:
        tags.extend(f"type:{value}" for value in memory_types)
    return RecallFilters(types=("world", "observation"), tags=tuple(tags), tags_match="all_strict")
```

For active reads, consult PostgreSQL lifecycle/expiry and the bank barrier before Hindsight. History/audit modes may include expired or forgotten rows only through explicit authorized paths and must label lifecycle; they do not assert those rows are current.

- [ ] **Step 4: Preserve legacy `/v1/memory/recall` as a safe compatibility alias**

Keep the released route for `0.4.0`, mark it deprecated and route it through the same existing-only resolver and currentness checks as `/v1/read/recall`. It must never request project creation. Document the successor route in its `Link` header and in the release notes; removal is deferred beyond `0.4.0`.

Run: `rtk uv run pytest tests/test_read_service.py tests/test_read_api.py tests/test_memory_api.py tests/test_mcp_tools.py -q`

Expected: all selected tests pass and an unknown-project read leaves project count unchanged.

- [ ] **Step 5: Commit the read contract transition**

```bash
rtk git add src/memory/read_models.py src/memory/read_service.py src/memory/api/read.py src/memory/api/memory.py src/memory/mcp/memory_tools.py tests/test_read_service.py tests/test_read_api.py tests/test_memory_api.py tests/test_mcp_tools.py
rtk git commit -m "feat(memory): read active typed memories"
```

### Task 5: Make correction and forgetting outcome-safe

**Files:**
- Create: `src/memory/curation_service.py`
- Modify: `src/memory/currentness.py`
- Modify: `src/memory/retained_records.py`
- Modify: `src/memory/api/curation.py`
- Modify: `src/memory/api/documents.py`
- Modify: `src/memory/mcp/memory_tools.py`
- Modify: `src/memory/hindsight/client.py`
- Create: `tests/test_curation_service.py`
- Modify: `tests/test_curation_api.py`

**Interfaces:**
- Consumes: `CurationOperation`, stable retained record/source-memory identity, Hindsight get/curate/delete/refresh operations and mental-model registry rows.
- Produces: `correct_record`, `forget_record`, `restore_record`, `delete_record`, `reconcile_bank_once`; terminal states `completed` and `needs_operator`.

- [ ] **Step 1: Write failing lost-response and terminal-rule tests**

```python
def test_lost_forget_response_withholds_bank(db, retained, hindsight):
    hindsight.curate.side_effect = HindsightOutcomeUnknown()
    with pytest.raises(BankCurrentnessUnavailable):
        forget_record(db, retained, reason="obsolete", client=hindsight)
    assert currentness_for(db, retained.bank).state == "withheld"


@pytest.mark.parametrize("action", ["forget", "expire", "delete"])
def test_absent_target_satisfies_removal_action(db, unknown_operation, hindsight, action):
    unknown_operation.action = action
    hindsight.get_memory.side_effect = MemoryNotFound()
    assert reconcile_bank_once(db, unknown_operation.bank, client=hindsight).state == "completed"


@pytest.mark.parametrize("action", ["correct", "restore"])
def test_absent_target_needs_operator_for_present_state(db, unknown_operation, hindsight, action):
    unknown_operation.action = action
    hindsight.get_memory.side_effect = MemoryNotFound()
    result = reconcile_bank_once(db, unknown_operation.bank, client=hindsight)
    assert result.state == "needs_operator"
    assert currentness_for(db, unknown_operation.bank).state == "withheld"
```

- [ ] **Step 2: Run curation tests to verify missing orchestration**

Run: `rtk uv run pytest tests/test_curation_service.py tests/test_curation_api.py -q`

Expected: collection or assertions fail because the outcome-safe service is absent.

- [ ] **Step 3: Implement mutation ordering and unknown-outcome barrier**

For each curation call:

```text
authorize and resolve existing bank
lock retained record
create idempotent CurationOperation with desired outcome
commit intent
call Hindsight using stable source-memory/document identity
on proven success: update ACH lifecycle/revision and mark affected models withheld
on response known not committed: leave prior lifecycle and return typed upstream failure
on response indeterminate: state=unknown, withhold physical bank, return maintenance status
```

Add an internal `HindsightOutcomeUnknown` raised only when the request may have reached upstream but no response proves the outcome. Do not expose request URLs or chained httpx exceptions.

- [ ] **Step 4: Implement one-step reconciliation and model invalidation**

`reconcile_bank_once` locks the bank row, performs at most one inspection or mutation, applies the SPEC §5.8 terminal rules, and never loops. After a proven indefinite-source change, select registered models whose static tags admit the source; if exclusion cannot be proven, withhold the model and request refresh. Record the exact refresh operation ID. Expiring records never feed registered models and therefore do not trigger model refresh.

Run: `rtk uv run pytest tests/test_curation_service.py tests/test_curation_api.py tests/test_documents_api.py tests/test_currentness_repository.py -q`

Expected: unknown outcome withholds reads, removal 404 completes, correction/restore 404 needs operator, and no physical IDs leak.

- [ ] **Step 5: Commit safe curation**

```bash
rtk git add src/memory/curation_service.py src/memory/currentness.py src/memory/retained_records.py src/memory/api/curation.py src/memory/api/documents.py src/memory/mcp/memory_tools.py src/memory/hindsight/client.py tests/test_curation_service.py tests/test_curation_api.py tests/test_documents_api.py
rtk git commit -m "feat(memory): fence indeterminate curation"
```

### Task 6: Add bounded access-driven expiry

**Files:**
- Create: `src/memory/expiry.py`
- Modify: `src/memory/retained_records.py`
- Modify: `src/memory/read_service.py`
- Modify: `src/memory/api/memory.py`
- Modify: `src/memory/mcp/memory_tools.py`
- Create: `tests/test_expiry.py`
- Modify: `tests/test_read_service.py`

**Interfaces:**
- Consumes: active expiring retained records, Hindsight reversible invalidation, curation/currentness state.
- Produces: `expire_due_once(db, bank, client, now) -> ExpiryResult`; at most 32 ordered rows per authorized recall/reflect access.

- [ ] **Step 1: Write failing boundary, overflow and outage tests**

```python
def test_expiry_is_exact_at_valid_until(db, expiring_record, clock, hindsight):
    clock.set(expiring_record.valid_until)
    result = expire_due_once(db, expiring_record.bank, client=hindsight, now=clock.now())
    assert result.expired == 1
    assert expiring_record.lifecycle == "expired"


def test_expiry_claims_only_first_32_in_stable_order(db, forty_due_records, hindsight, clock):
    result = expire_due_once(db, forty_due_records[0].bank, client=hindsight, now=clock.now())
    assert result.expired == 32 and result.remaining == 8
    assert [call.args[1] for call in hindsight.curate.call_args_list] == [r.source_memory_id for r in forty_due_records[:32]]


def test_unknown_expiry_outcome_withholds_requested_read(db, due_record, hindsight):
    hindsight.curate.side_effect = HindsightOutcomeUnknown()
    with pytest.raises(BankCurrentnessUnavailable):
        recall_after_maintenance(db, due_record.bank, "query", client=hindsight)
```

- [ ] **Step 2: Run expiry tests to verify failure**

Run: `rtk uv run pytest tests/test_expiry.py -q`

Expected: collection fails because `expire_due_once` does not exist.

- [ ] **Step 3: Implement ordered, claimed, one-batch expiry**

```python
rows = db.scalars(
    select(RetainedRecord)
    .where(*retained_record_scope_clause(bank),
           RetainedRecord.lifecycle == "active",
           RetainedRecord.valid_until.is_not(None),
           RetainedRecord.valid_until <= now)
    .order_by(RetainedRecord.valid_until, RetainedRecord.recorded_at,
              RetainedRecord.document_id)
    .with_for_update(skip_locked=True)
    .limit(32)
).all()
```

Invalidate each stable source-memory ID. Upstream 404 completes expiry. Unknown outcome creates/updates the curation operation and bank barrier. If more due rows remain after the batch, return `expiry_cleanup_pending` and withhold that recall/reflect; the next authorized access performs at most one batch.

- [ ] **Step 4: Wire maintenance only into recall and reflect**

`load_context` is owned by the final plan and must not run expiry cleanup. Current list/get calls honor an existing barrier but do not claim new expiry batches. Recall and reflect disclose the bounded maintenance side effect in MCP annotations.

Run: `rtk uv run pytest tests/test_expiry.py tests/test_read_service.py tests/test_mcp_surface_honesty.py -q`

Expected: all selected tests pass, including exact boundary and 32/8 overflow.

- [ ] **Step 5: Commit lazy expiry**

```bash
rtk git add src/memory/expiry.py src/memory/retained_records.py src/memory/read_service.py src/memory/api/memory.py src/memory/mcp/memory_tools.py tests/test_expiry.py tests/test_read_service.py tests/test_mcp_surface_honesty.py
rtk git commit -m "feat(memory): expire claims on authorized access"
```

### Task 7: Prove the exact strategy and lifecycle against disposable Hindsight

**Files:**
- Create: `tests/test_v040_hindsight_live.py`
- Create: `scripts/v040-hindsight-preflight.py`
- Create: `docs/results/2026-09-04-ach-memory-v0-4-0-retain.md`

**Interfaces:**
- Consumes: live Hindsight URL from `MEMORY_HINDSIGHT_URL`, explicit disposable-bank confirmation and all services from this plan.
- Produces: content-free compatibility result proving Hindsight version, exact one-fact retain, 500-sibling recall, consolidation/curation and cleanup.

- [ ] **Step 1: Add a guarded live test harness**

```python
pytestmark = pytest.mark.integration


def require_disposable_confirmation():
    if os.environ.get("HINDSIGHT_V040_CONFIRM") != "disposable-banks-only":
        pytest.skip("set HINDSIGHT_V040_CONFIRM=disposable-banks-only")
```

The fixture creates namespaced disposable User/Project banks, registers every created bank before mutation, polls operations to terminal state, and deletes all registered banks in `finally`. Reject non-loopback URLs unless an explicit allow-list environment variable names the host.

- [ ] **Step 2: Write exact-strategy and scale-recall tests**

The exact-strategy test retains one 4,096-byte accepted claim and asserts one text-identical `world` fact, zero extracted entities, one chunk and zero extraction-model tokens. The scale test retains 500 deterministic sibling claims, then runs the ten frozen lexical/paraphrased probes and asserts the intended source fact is in the first ten results for every query.

Run: `rtk uv run pytest tests/test_v040_hindsight_live.py -q`

Expected before configuration: skipped with the exact confirmation message, not silently passed.

- [ ] **Step 3: Add consolidation, curation and fault cases**

The live fixture must also:

```text
retain a frozen related-claim set
request explicit consolidation
assert at least one observation cites the source IDs
forget one source through ACH
assert Hindsight removes or invalidates the dependent observation
simulate response loss after accepted operation and retry with the same operation_id
simulate 429, timeout and unavailable backend at the client boundary
```

Store only counts, durations, statuses and content hashes in the result document; no claim text, bank ID, credential or evidence.

- [ ] **Step 4: Run non-live and live gates**

Run non-live:

```bash
rtk uv run pytest tests/test_sanitization.py tests/test_retention.py tests/test_memory_api.py tests/test_read_service.py tests/test_curation_service.py tests/test_expiry.py -q
rtk uv run ruff check src tests scripts/v040-hindsight-preflight.py
```

Then, against the explicitly confirmed disposable deployment:

```bash
rtk env HINDSIGHT_V040_CONFIRM=disposable-banks-only uv run pytest tests/test_v040_hindsight_live.py -q
```

Expected: every created bank is removed even on failure; the live assertions pass or activation remains blocked with the exact failed gate recorded.

- [ ] **Step 5: Commit compatibility evidence**

```bash
rtk git add tests/test_v040_hindsight_live.py scripts/v040-hindsight-preflight.py docs/results/2026-09-04-ach-memory-v0-4-0-retain.md
rtk git commit -m "test(memory): prove v0.4.0 retain lifecycle"
```

## Completion Gate

- REST and MCP expose the same typed proactive retain contract.
- Canonical content and evidence obey their separate sanitizer rules and evidence never reaches Hindsight.
- Every accepted write uses one async `ach-exact-v1` operation with stable retry identity.
- Recall/current history uses v0.4.0 types and never creates an unknown project.
- Correction, forget, restore, delete and expiry have proven/unknown/needs-operator outcomes.
- Unknown safety outcomes withhold current bank reads; affected indefinite models are withheld by exact refresh operation.
- Expiry is access-driven, ordered and bounded to 32 rows; no daemon or scheduled activation exists.
- The non-live tests pass and the disposable live report records exact-strategy, scale-recall, curation and cleanup results.

Merge this plan's branch only after review. Do not execute the final context/retirement plan until this branch and the independently reviewed mental-model-governance branch are both integrated.
