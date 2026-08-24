"""Verify that a memory is present in a post-restore active-set listing."""

import json
import sys
from typing import Any


class ActiveSetError(ValueError):
    """The restored memory was not present in the active set."""


def _items(payload: dict[str, Any]) -> list[Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []

    listed: list[Any] = []
    for key in ("items", "memories"):
        values = result.get(key)
        if isinstance(values, list):
            listed.extend(values)
    return listed


def verify_memory_active(payload: dict[str, Any], memory_id: str) -> None:
    """Raise if ``memory_id`` is absent from the active-set listing."""
    items = _items(payload)
    if any(isinstance(item, dict) and item.get("id") == memory_id for item in items):
        return

    summary = [
        {"id": item.get("id"), "fact_type": item.get("fact_type")}
        for item in items
        if isinstance(item, dict)
    ]
    raise ActiveSetError(
        f"memory {memory_id} is not in the active set; "
        f"listing summary: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1]:
        print(f"usage: {argv[0]} MEMORY_ID", file=sys.stderr)
        return 2

    try:
        verify_memory_active(json.load(sys.stdin), argv[1])
    except (json.JSONDecodeError, ActiveSetError, TypeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
