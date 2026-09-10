"""Provision the exact, no-extraction retain strategy ACH requires."""

from __future__ import annotations

from typing import Any

from memory.errors import HindsightError
from memory.hindsight.client import HindsightClient

EXACT_RETAIN_STRATEGY_NAME = "ach-exact-v1"
EXACT_RETAIN_STRATEGY: dict[str, Any] = {
    "retain_extraction_mode": "chunks",
    "retain_chunk_size": 4096,
    "retain_structured_chunk_size": 4096,
}


def _strategies(response: dict) -> dict[str, Any]:
    config = response.get("config")
    if not isinstance(config, dict):
        raise HindsightError("memory backend retain strategy could not be verified")
    strategies = config.get("retain_strategies")
    if strategies is None:
        return {}
    if not isinstance(strategies, dict):
        raise HindsightError("memory backend retain strategy could not be verified")
    return strategies


def ensure_exact_retain_strategy(client: HindsightClient, bank_id: str) -> None:
    """Ensure the ACH-owned strategy exists and verify it before any retain.

    The resolved strategy map is preserved when the ACH entry is added or
    repaired. Verification after PATCH prevents Hindsight's documented
    unknown-strategy fallback from silently changing typed-retain semantics.

    Deliberately NOT memoised per process, however hot this path is. A
    "verified" verdict is only true until the bank stops existing, and
    `DELETE /v1/admin/memory/{scope}` tears one down while deliberately
    keeping the id (SPEC §12.3) for the next retain to auto-create. Auto-
    creation brings the bank back with Hindsight's DEFAULT config, so a
    surviving verdict means the repair is skipped and every later retain is
    stored under the default extraction strategy instead of `ach-exact-v1` --
    silently, since the unknown-strategy fallback does not error. `client.
    ensure_bank` carries this same warning from the last time a per-process
    cache here was found and removed; with replicaCount>1 a delete served by
    one pod leaves every other pod's verdict live.
    """
    client.ensure_bank(bank_id)
    current = _strategies(client.get_bank_config(bank_id))
    if current.get(EXACT_RETAIN_STRATEGY_NAME) == EXACT_RETAIN_STRATEGY:
        return

    updated = {**current, EXACT_RETAIN_STRATEGY_NAME: dict(EXACT_RETAIN_STRATEGY)}
    client.update_bank_config(bank_id, {"retain_strategies": updated})
    verified = _strategies(client.get_bank_config(bank_id))
    if verified.get(EXACT_RETAIN_STRATEGY_NAME) != EXACT_RETAIN_STRATEGY:
        raise HindsightError("memory backend retain strategy could not be verified")
