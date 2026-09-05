"""Preflight check for v0.4.0's frozen `ach-exact-v1` retain strategy.

Run against a disposable bank on the target Hindsight deployment BEFORE
activating v0.4.0 retain against it (SPEC §5.7): "Provisioning and upgrades
MUST use dry-run-extract plus a live disposable-bank test to prove one exact
fact, zero extraction tokens, zero ingest-time entities and no second
chunk. ACH MUST NOT silently fall back to a bank default or to Hindsight
`verbatim` mode."

This script only calls Hindsight's read-only dry-run-extract endpoint --
`HindsightClient.dry_run_extract` -- so it stores nothing and needs no
cleanup. It never prints the retained content itself, only counts and shapes.

Usage:
    MEMORY_HINDSIGHT_URL=... MEMORY_HINDSIGHT_API_KEY=... \\
        uv run python scripts/v040-hindsight-preflight.py BANK_ID
"""

from __future__ import annotations

import os
import sys

from memory.hindsight.client import HindsightClient

_PROBE_CONTENT = "x" * 4096  # exactly the v0.4.0 canonical-claim byte ceiling

_STRATEGY_OVERRIDE = {
    "retain_extraction_mode": "chunks",
    "retain_chunk_size": 4096,
    "retain_structured_chunk_size": 4096,
}


class PreflightFailed(RuntimeError):
    """The target deployment's `chunks` extraction mode does not behave the
    way `ach-exact-v1` requires."""


def check(client: HindsightClient, bank_id: str) -> dict[str, int]:
    """Run dry-run-extract with the frozen override and verify SPEC §5.7's
    four guarantees. Returns counts only -- never the probed content."""
    result = client.dry_run_extract(
        bank_id,
        _PROBE_CONTENT,
        retain_extraction_mode=_STRATEGY_OVERRIDE["retain_extraction_mode"],
    )

    chunks = result.get("chunks") if isinstance(result, dict) else None
    chunk_count = len(chunks) if isinstance(chunks, list) else None
    entities = result.get("entities") if isinstance(result, dict) else None
    entity_count = len(entities) if isinstance(entities, list) else None
    facts = result.get("facts") if isinstance(result, dict) else None
    fact_count = len(facts) if isinstance(facts, list) else None
    usage = result.get("usage") if isinstance(result, dict) else None
    extraction_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None

    counts = {
        "chunk_count": chunk_count if chunk_count is not None else -1,
        "entity_count": entity_count if entity_count is not None else -1,
        "fact_count": fact_count if fact_count is not None else -1,
        "extraction_tokens": extraction_tokens if extraction_tokens is not None else -1,
    }

    if chunk_count != 1:
        raise PreflightFailed(f"expected exactly one chunk, saw {counts['chunk_count']}")
    if entity_count not in (0, None):
        raise PreflightFailed(f"expected zero extracted entities, saw {counts['entity_count']}")
    if extraction_tokens not in (0, None):
        raise PreflightFailed(
            f"expected zero extraction-model tokens, saw {counts['extraction_tokens']}"
        )

    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1]:
        print(f"usage: {argv[0]} BANK_ID", file=sys.stderr)
        return 2

    url = os.environ.get("MEMORY_HINDSIGHT_URL")
    api_key = os.environ.get("MEMORY_HINDSIGHT_API_KEY", "")
    tenant_id = os.environ.get("MEMORY_TENANT_ID", "default")
    if not url:
        print("MEMORY_HINDSIGHT_URL is required", file=sys.stderr)
        return 2

    client = HindsightClient(base_url=url, api_key=api_key, tenant_id=tenant_id)
    try:
        counts = check(client, argv[1])
    except PreflightFailed as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
