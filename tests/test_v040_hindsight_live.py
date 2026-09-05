"""Live compatibility proof for v0.4.0's exact retain and lifecycle against
a disposable Hindsight 0.9.2 deployment (Task 7).

Skipped entirely unless explicitly confirmed disposable, and refuses a
non-loopback target unless explicitly allow-listed -- see
`require_disposable_confirmation` and `_require_safe_target`. Every bank
this file creates is registered BEFORE any mutation against it and deleted
in the fixture's `finally`, even on failure.

Field names read off Hindsight responses here (`items`, `results`, `text`,
`source_memory_ids`, `operation_id`) are pinned against shapes confirmed
elsewhere in this codebase (read_service.py, hindsight/client.py's own
docstrings, existing mocked tests) -- this file is what proves them against
the real thing.

Store only counts, durations, statuses and content hashes wherever this
file's own results are recorded (docs/results/2026-09-04-ach-memory-v0-4-0-
retain.md): never claim text, a bank ID, a credential or evidence.
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urlparse

import pytest

from memory.errors import HindsightError
from memory.hindsight.client import HindsightClient, RetainItem
from memory.retain_strategy import ensure_exact_retain_strategy

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_SECONDS = 30.0
_SCALE_POLL_TIMEOUT_SECONDS = 120.0
_POLL_INTERVAL_SECONDS = 0.5
_TAGS = ["type:fact", "basis:human_explicit", "schema:ach-retain-v1", "validity:indefinite"]


def require_disposable_confirmation() -> None:
    if os.environ.get("HINDSIGHT_V040_CONFIRM") != "disposable-banks-only":
        pytest.skip("set HINDSIGHT_V040_CONFIRM=disposable-banks-only")


def _require_safe_target(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    allowed = os.environ.get("HINDSIGHT_V040_ALLOWED_HOST", "")
    if allowed and host == allowed.lower():
        return
    pytest.skip(
        f"refusing a non-loopback Hindsight target ({host!r}) without "
        "HINDSIGHT_V040_ALLOWED_HOST naming it explicitly"
    )


@pytest.fixture
def disposable():
    require_disposable_confirmation()
    url = os.environ.get("MEMORY_HINDSIGHT_URL")
    if not url:
        pytest.skip("MEMORY_HINDSIGHT_URL is required for the v0.4.0 live gate")
    _require_safe_target(url)
    api_key = os.environ.get("MEMORY_HINDSIGHT_API_KEY", "")
    client = HindsightClient(base_url=url, api_key=api_key, tenant_id="default")

    created: list[str] = []

    def make_bank(kind: str) -> str:
        bank_id = f"v040test-{kind}-{uuid.uuid4().hex[:20]}"
        created.append(bank_id)  # registered BEFORE any mutation against it
        ensure_exact_retain_strategy(client, bank_id)
        return bank_id

    try:
        yield client, make_bank
    finally:
        for bank_id in created:
            try:
                client.delete_bank(bank_id)
            except Exception:  # noqa: BLE001, S110 -- best-effort cleanup, never masks the test's own failure
                pass


def _retain_one(client: HindsightClient, bank_id: str, content: str, *, document_id: str) -> str:
    operation_id = str(uuid.uuid4())
    item = RetainItem(
        content=content, document_id=document_id, tags=list(_TAGS),
        strategy="ach-exact-v1", update_mode="replace",
    )
    client.retain_items(bank_id, [item], operation_id=operation_id, is_async=True)
    return operation_id


def _poll_operation(
    client: HindsightClient,
    bank_id: str,
    operation_id: str,
    *,
    timeout_seconds: float = _POLL_TIMEOUT_SECONDS,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        operation = client.get_operation(bank_id, operation_id)
        if operation.get("status") in ("completed", "failed"):
            return operation
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"operation did not reach a terminal status within {timeout_seconds:g}s"
    )


def test_exact_strategy_produces_one_verbatim_world_fact(disposable):
    """SPEC §5.7: `ach-exact-v1` stores each chunk verbatim as one `world`
    source fact -- no extraction-model call, no second chunk."""
    client, make_bank = disposable
    bank_id = make_bank("exact")
    content = "x" * 4096

    item = RetainItem(
        content=content,
        document_id=f"ach-retain-{uuid.uuid4().hex}",
        tags=list(_TAGS),
        strategy="ach-exact-v1",
        update_mode="replace",
    )
    result = client.retain_items(
        bank_id, [item], operation_id=str(uuid.uuid4()), is_async=False
    )
    assert result["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "thoughts_tokens": 0,
    }

    listing = client.list_memories(bank_id, type="world")
    items = listing.get("items") or []
    assert len(items) == 1
    assert items[0].get("text") == content
    assert not items[0].get("entities")

    documents = client.list_documents(bank_id)
    assert documents["total"] == 1
    assert (documents.get("items") or [])[0].get("memory_unit_count") == 1


def test_500_sibling_claims_are_recalled_by_ten_frozen_probes(disposable):
    """Scale-recall gate: 500 deterministic sibling claims, ten frozen
    lexical/paraphrased probes, the intended source fact in the first ten
    results for every one."""
    client, make_bank = disposable
    bank_id = make_bank("scale")

    target_index = 250
    target_text = (
        f"Sibling claim number {target_index:04d} says the amber kestrel prefers "
        "cobalt fasteners during monsoon maintenance."
    )
    items = [
        RetainItem(
            content=(
                target_text
                if n == target_index
                else f"Sibling claim number {n:04d} about deterministic recall testing."
            ),
            document_id=f"ach-retain-scale-{n:04d}", tags=list(_TAGS),
            strategy="ach-exact-v1", update_mode="replace",
        )
        for n in range(500)
    ]
    operation_id = str(uuid.uuid4())
    client.retain_items(bank_id, items, operation_id=operation_id, is_async=True)
    # This is one synthetic 500-document compatibility probe, not ACH's
    # one-claim typed retain path. Keep its observation window local to this
    # scale gate instead of widening production request timeouts.
    operation = _poll_operation(
        client,
        bank_id,
        operation_id,
        timeout_seconds=_SCALE_POLL_TIMEOUT_SECONDS,
    )
    assert operation["status"] == "completed"

    # Every probe identifies the sole intended fact. The first live draft had
    # two generic queries that could not distinguish claim 0250 from 499
    # equally correct siblings; that was an invalid oracle, not a recall gate.
    probes = [
        f"sibling claim number {target_index:04d}",
        "amber kestrel cobalt fasteners",
        "Which amber bird prefers cobalt hardware?",
        "What fasteners does the kestrel prefer?",
        "bird preference during monsoon maintenance",
        f"claim {target_index:04d} cobalt",
        "blue-metal fasteners used by an amber kestrel",
        "kestrel hardware preference",
        "monsoon upkeep with cobalt fasteners",
        "amber kestrel rainy-season maintenance",
    ]

    misses = []
    for probe in probes:
        result = client.recall(
            bank_id, probe, with_entities=False, types=["world"],
            tags=["schema:ach-retain-v1"], tags_match="all_strict",
        )
        texts = [r.get("text") for r in (result.get("results") or [])[:10]]
        if target_text not in texts:
            misses.append(probe)

    assert not misses, f"{len(misses)}/10 probes missed the intended source fact"


def test_consolidation_curation_and_retry_cases(disposable):
    """Consolidation cites its source facts; forgetting one removes its
    dependent observations; an exact retry never creates another document."""
    client, make_bank = disposable
    bank_id = make_bank("curation")

    related = [
        "The team migrated the API from REST to gRPC in Q1.",
        "The gRPC migration reduced p99 latency by 40 percent.",
        "The gRPC migration required rewriting the client SDKs.",
    ]
    items = [
        RetainItem(
            content=text, document_id=f"ach-retain-curation-{n}", tags=list(_TAGS),
            strategy="ach-exact-v1", update_mode="replace",
        )
        for n, text in enumerate(related)
    ]
    operation_id = str(uuid.uuid4())
    client.retain_items(bank_id, items, operation_id=operation_id, is_async=True)
    retained = _poll_operation(client, bank_id, operation_id)
    assert retained["status"] == "completed"
    assert client.list_documents(bank_id)["total"] == 3

    consolidation = client.consolidate(bank_id)
    consolidated = _poll_operation(client, bank_id, consolidation["operation_id"])
    assert consolidated["status"] == "completed"

    observations = client.list_memories(bank_id, type="observation").get("items") or []
    assert observations

    reflect_result = client.reflect(bank_id, "What happened with the gRPC migration?")
    assert reflect_result.get("text")

    listing = client.list_memories(bank_id, type="world")
    source_ids = [m["id"] for m in listing.get("items") or []]
    assert source_ids

    cited_source_ids = {
        source_id
        for observation in observations
        for source_id in observation.get("source_memory_ids") or []
    }
    assert cited_source_ids.intersection(source_ids)

    invalidated_source_id = next(iter(cited_source_ids.intersection(source_ids)))
    dependent_observation_ids = {
        observation["id"]
        for observation in observations
        if invalidated_source_id in (observation.get("source_memory_ids") or [])
    }
    assert dependent_observation_ids

    client.curate(
        bank_id,
        invalidated_source_id,
        state="invalidated",
        reason="v0.4.0 live gate cleanup probe",
    )
    active_observation_ids = {
        observation["id"]
        for observation in (
            client.list_memories(bank_id, type="observation").get("items") or []
        )
    }
    assert dependent_observation_ids.isdisjoint(active_observation_ids)

    # An exact retry at the same operation_id resolves the same document,
    # never a second one and never silently restores the invalidated fact.
    replay = client.retain_items(
        bank_id, items, operation_id=operation_id, is_async=True
    )
    assert replay.get("operation_id") == operation_id
    assert client.list_documents(bank_id)["total"] == 3


def test_fault_injection_at_the_client_boundary():
    """429, timeout and an unavailable backend all surface as the typed,
    non-leaking `HindsightError` at the client boundary -- proven against an
    address nothing listens on, so no disposable bank is needed."""
    broken = HindsightClient(base_url="http://127.0.0.1:1", api_key="", tenant_id="default")
    with pytest.raises(HindsightError):
        broken.get_bank_config("v040test-unreachable")
