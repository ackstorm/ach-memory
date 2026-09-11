"""Finding 7: single-source observations whose text is reworded, not copied --
invisible to `_collapse_duplicate_claims`'s text-based dedup.

`_collapse_duplicate_claims` (src/memory/read_service.py:137) groups recall
hits by identical normalized text and keeps the copy with a `document_id`.
Its docstring assumes a single-source observation copies its source's text
verbatim, and that "a genuine consolidation is never dropped -- it summarises
several facts, so its text differs from any of them." Two pairs measured
2026-09-11 against two live ACH banks broke that assumption: both
observations are SINGLE-source and REWORDED, so the text key never matched
and both halves reached the caller.

Upstream already ships the parent link on recall (`include.source_facts`,
disabled by default -- `IncludeOptions.source_facts` at
hindsight_api/api/http.py:283) but ach-memory never asks for it. This probe
answers, against every bank already in the local stack:

  1. With include={"source_facts": {"max_tokens": 1}}, do
     results[].source_fact_ids still arrive even though the token-limited
     source_facts map comes back empty? `response_models.py:440` claims yes
     ("results[].source_fact_ids have no entry in source_facts -- the budget
     ran out, the references are not dangling") -- measured here, not
     trusted.
  2. How many observation-type memory units are single-source vs
     multi-source?
  3. Of the single-source ones, how many have text differing from their one
     source -- i.e. invisible to the shipped collapse?

Q2 and Q3 read `memory_units` directly on Hindsight's own database
(`hindsight-db`), not through recall: the deployed Hindsight version's
`/memories/list` endpoint does not return `source_memory_ids` in its response
(checked live; the newer hindsight-api-slim checkout's code does -- this
stack runs 0.9.1, the checkout is 0.9.2), and recall is a top-K ranked
search, not an enumeration -- querying it per bank would undercount by
however many observations no probe query happens to surface.
`source_memory_ids` is a real, indexed column
(`idx_memory_units_source_memory_ids`); reading it directly is the only way
to get an exhaustive count. Q1 is measured the documented way, through
Hindsight's raw recall endpoint, because that endpoint is the surface being
decided on.

Seeds nothing. The local stack already carries dozens of banks of
observations accumulated by prior probe and test runs -- real Hindsight
consolidation output, not synthetic, and large enough to answer Q2/Q3
honestly. Run against a live local stack (`make up`), from the repository
root.
"""
import json
import re
import subprocess
from pathlib import Path

import httpx

API, HINDSIGHT = "http://localhost:8000", "http://localhost:8888"
ROOT = Path(__file__).resolve().parents[2]
PSQL = (
    "docker", "compose", "exec", "-T", "hindsight-db",
    "psql", "-U", "hindsight", "-d", "hindsight", "-tAc",
)

_MENTIONED_AT_SUFFIX = re.compile(r"\s*\(mentioned_at=[^)]*\)\s*$")


def norm(text: str) -> str:
    return _MENTIONED_AT_SUFFIX.sub("", text or "").strip()


def psql_json(query: str) -> list[dict]:
    r = subprocess.run([*PSQL, query], cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(r.stdout.strip()) or []


def main() -> int:
    # ---- ground truth: every observation on the stack, direct from Hindsight's db ----
    observations = psql_json(
        "select json_agg(row_to_json(t)) from ("
        "  select o.id, o.bank_id,"
        "    coalesce(array_length(o.source_memory_ids,1),0) as n_sources,"
        "    o.source_memory_ids[1]::text as src_id,"
        "    o.text, s.text as src_text"
        "  from memory_units o"
        "  left join memory_units s on s.id = o.source_memory_ids[1]"
        "  where o.fact_type = 'observation'"
        ") t;"
    )
    print(f"{len(observations)} observation rows across the stack (hindsight-db, direct)")

    by_bank: dict[str, list[dict]] = {}
    for row in observations:
        by_bank.setdefault(row["bank_id"], []).append(row)

    # ---- Q2: single-source vs multi-source, per bank ----
    print()
    print("== Q2: observations by source count, per bank ==")
    print(f"  {'bank_id':<48} {'obs':>5} {'single':>7} {'multi':>6} {'zero':>5}")
    tot_obs = tot_single = tot_multi = tot_zero = 0
    for bank_id in sorted(by_bank, key=lambda b: -len(by_bank[b])):
        rows = by_bank[bank_id]
        single = sum(1 for r in rows if r["n_sources"] == 1)
        multi = sum(1 for r in rows if r["n_sources"] > 1)
        zero = sum(1 for r in rows if r["n_sources"] == 0)
        tot_obs += len(rows)
        tot_single += single
        tot_multi += multi
        tot_zero += zero
        print(f"  {bank_id:<48} {len(rows):>5} {single:>7} {multi:>6} {zero:>5}")
    print(f"  {'TOTAL':<48} {tot_obs:>5} {tot_single:>7} {tot_multi:>6} {tot_zero:>5}")

    # ---- Q3: of the single-source ones, text identical to source or reworded? ----
    print()
    print("== Q3: single-source observations -- text vs their one source, per bank ==")
    print(f"  {'bank_id':<48} {'single':>7} {'identical':>10} {'reworded':>9}")
    tot_id = tot_rw = 0
    twins = []
    for bank_id in sorted(by_bank, key=lambda b: -len(by_bank[b])):
        singles = [r for r in by_bank[bank_id] if r["n_sources"] == 1]
        if not singles:
            continue
        identical = [r for r in singles if r["src_text"] is not None and norm(r["text"]) == norm(r["src_text"])]
        reworded = [r for r in singles if r["src_text"] is not None and norm(r["text"]) != norm(r["src_text"])]
        tot_id += len(identical)
        tot_rw += len(reworded)
        twins.extend(reworded)
        print(f"  {bank_id:<48} {len(singles):>7} {len(identical):>10} {len(reworded):>9}")
    print(f"  {'TOTAL':<48} {tot_id + tot_rw:>7} {tot_id:>10} {tot_rw:>9}")
    if twins:
        print()
        print("  single-source paraphrased twins found (invisible to the shipped collapse):")
        for t in twins[:10]:
            print(f"    [{t['bank_id']}] obs={t['id']}")
            print(f"        observation: {norm(t['text'])[:90]}")
            print(f"        source     : {norm(t['src_text'])[:90]}")

    # ---- Q1: does results[].source_fact_ids survive a token-starved source_facts map? ----
    print()
    print("== Q1: source_fact_ids at max_tokens=1 vs default vs disabled ==")
    print(f"  {'bank_id':<48} {'source_facts':>14} {'results':>7} {'w/ids':>6} {'map_sz':>6} {'trunc':>6}")
    candidates = sorted(
        (bank_id for bank_id, rows in by_bank.items() if any(r["n_sources"] == 1 for r in rows)),
        key=lambda b: len(by_bank[b]),
    )
    if not candidates:
        print("  no bank has a single-source observation to query -- Q1 not measured")
    else:
        sample = {candidates[0], candidates[len(candidates) // 2], candidates[-1]}
        with httpx.Client(timeout=60.0) as c:
            for bank_id in sorted(sample, key=lambda b: len(by_bank[b])):
                probe_row = next(r for r in by_bank[bank_id] if r["n_sources"] == 1)
                query = norm(probe_row["text"])
                for label, include in (
                    ("max_tokens=1", {"source_facts": {"max_tokens": 1}}),
                    ("default (4096)", {"source_facts": {}}),
                    ("disabled", None),
                ):
                    body = {"query": query, "types": ["observation"]}
                    if include is not None:
                        body["include"] = include
                    r = c.post(f"{HINDSIGHT}/v1/default/banks/{bank_id}/memories/recall", json=body)
                    r.raise_for_status()
                    data = r.json()
                    results = data.get("results", [])
                    with_ids = sum(1 for x in results if x.get("source_fact_ids"))
                    map_sz = len(data.get("source_facts") or {})
                    trunc = data.get("source_facts_truncated")
                    print(
                        f"  {bank_id:<48} {label:>14} {len(results):>7} {with_ids:>6} "
                        f"{map_sz:>6} {trunc!s:>6}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
