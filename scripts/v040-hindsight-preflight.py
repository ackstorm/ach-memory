"""Read-only preflight for v0.4.0's installed `ach-exact-v1` strategy.

Run against a prepared disposable bank on the target Hindsight deployment
before activating v0.4.0 retain (SPEC §5.7). It verifies the pinned backend
version and the resolved installed strategy without mutating the bank.

Hindsight 0.9.2's dry-run response exposes extracted facts and aggregate
usage, but not chunk or entity metadata; on the validated deployment its
per-call ``chunks`` override also does not reproduce named-strategy retain
behavior. The guarded live test therefore performs the separate behavioral
proof with a disposable bank and a synchronous retain. This script stores
nothing and needs no cleanup.

Usage:
    MEMORY_HINDSIGHT_URL=... MEMORY_HINDSIGHT_API_KEY=... \\
        uv run python scripts/v040-hindsight-preflight.py BANK_ID
"""

from __future__ import annotations

import os
import sys

from memory.hindsight.client import HindsightClient
from memory.retain_strategy import EXACT_RETAIN_STRATEGY, EXACT_RETAIN_STRATEGY_NAME


class PreflightFailed(RuntimeError):
    """The target bank does not expose ACH's exact strategy definition."""


def check(client: HindsightClient, bank_id: str) -> dict[str, int]:
    """Verify the backend version and resolved strategy; return content-free flags."""
    version = client.get_version()
    if version.get("api_version") != "0.9.2":
        raise PreflightFailed("target is not the validated Hindsight 0.9.2 release")
    response = client.get_bank_config(bank_id)
    config = response.get("config") if isinstance(response, dict) else None
    strategies = config.get("retain_strategies") if isinstance(config, dict) else None
    actual = strategies.get(EXACT_RETAIN_STRATEGY_NAME) if isinstance(strategies, dict) else None
    if actual != EXACT_RETAIN_STRATEGY:
        raise PreflightFailed("installed ach-exact-v1 strategy does not match the frozen definition")
    return {
        "hindsight_0_9_2": 1,
        "strategy_present": 1,
        "strategy_exact": 1,
    }


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
