#!/usr/bin/env python3
"""The one definition of "a bank id leaked".

There used to be three copies -- scripts/smoke.sh, scripts/e2e.py and
scripts/mcp-smoke.py -- and they had diverged. e2e.py's was \\b-anchored, so
it could not match a bank id embedded inside a chunk_id
(f"{bank_id}_{document_id}_{n}"), which is the exact shape of the leak
measured live against hindsight-api 0.9.1 on 2026-08-22 and the reason
`_strip_bank_id` redacts substrings at all. e2e.py is the broadest scanner in
the project (~70 scenarios, all 15 tools), so that anchor made the widest
gate the blindest.

Deliberately UNANCHORED: a bank id is a leak wherever it appears, including
in the middle of another field's value. That is the whole point.

Matches:
  - a literal "bank_id" key
  - user_<uuid> / project_<uuid>, the opaque bank ids (SPEC §4.7)
  - prj_<hex>, Project.internal_id (SPEC inv. 34 -- internal, never required
    by an ordinary client)

Does NOT match usr_/grp_/key_, which are exposed ids and are meant to be
visible in every provisioning response.

Usage from bash:  echo "$body" | python3 scripts/leakscan.py  # exit 1 on a hit
"""

import re
import sys

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

LEAK_RE = re.compile(
    r'"bank_id"'
    rf"|user_{_UUID}"
    rf"|project_{_UUID}"
    r"|prj_[0-9a-f]{8}"
)


def find(text: str) -> str | None:
    """The first offending match, or None."""
    match = LEAK_RE.search(text)
    return match.group(0) if match else None


if __name__ == "__main__":
    hit = find(sys.stdin.read())
    if hit:
        print(f"FAIL: a bank id reached the client: {hit}", file=sys.stderr)
        sys.exit(1)
