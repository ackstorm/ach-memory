"""Select a retained fact that can be passed to the curation endpoints."""

import json
import sys
from typing import Any

CURATABLE_FACT_TYPES = ("world", "experience")


class SelectionError(ValueError):
    """The listing did not contain a matching curatable memory."""


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


def _summary(items: list[Any], marker: str) -> str:
    marker = marker.casefold()
    summary = []
    for item in items:
        if not isinstance(item, dict):
            summary.append({"id": None, "fact_type": None, "matches_marker": False})
            continue
        try:
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
        except (TypeError, ValueError):
            encoded = ""
        summary.append(
            {
                "id": item.get("id"),
                "fact_type": item.get("fact_type"),
                "matches_marker": bool(marker and marker in encoded),
            }
        )
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def select_curatable_memory(payload: dict[str, Any], marker: str) -> str:
    """Return the id of a matching world or experience fact."""
    items = _items(payload)
    marker = marker.casefold()
    candidates = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        fact_type = item.get("fact_type")
        memory_id = item.get("id")
        if (
            fact_type in CURATABLE_FACT_TYPES
            and isinstance(memory_id, str)
            and memory_id
            and marker
        ):
            try:
                encoded = json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
            except (TypeError, ValueError):
                continue
            if marker in encoded:
                candidates.append((CURATABLE_FACT_TYPES.index(fact_type), index, memory_id))

    if candidates:
        return min(candidates)[2]

    raise SelectionError(
        "no matching curatable memory (fact_type must be world or experience); "
        f"listing summary: {_summary(items, marker)}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1]:
        print(f"usage: {argv[0]} MEMORY_MARKER", file=sys.stderr)
        return 2

    try:
        payload = json.load(sys.stdin)
        memory_id = select_curatable_memory(payload, argv[1])
    except (json.JSONDecodeError, SelectionError, TypeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(memory_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
