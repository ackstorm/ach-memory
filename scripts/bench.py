#!/usr/bin/env python3
"""Capability benchmark: seed the corpus, measure recall quality and tool latency.

Usage: uv run python scripts/bench.py --url http://localhost:8000/mcp/ --key alice \
    --header Authorization --label "local mock-LLM"

Seeds every `fact` row under one principal (`--key`), project-scoped facts each
in their own per-project bank, runs every `question` through `recall`, times
the read tools, writes one markdown report, then cleans up. See
benchmarks/README.md for the corpus shape and how to read the metrics.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "benchmarks" / "corpus.jsonl"


def load_corpus() -> tuple[list[dict], list[dict]]:
    facts, questions = [], []
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        (facts if row["kind"] == "fact" else questions).append(row)
    return facts, questions


def scoped(row: dict, project: str) -> dict:
    """Tool args for one row: project scope gets its own per-project bank."""
    args = {"scope": row["scope"]}
    if row["scope"] == "project":
        args["project_slug"] = f"{project}-{row['project']}"
    return args


def bank(row: dict) -> tuple[str, str]:
    """Logical bank: per-project for project scope, per-owner for user scope (one --key)."""
    if row["scope"] == "project":
        return ("project", row["project"])
    return ("user", row.get("owner") or row["asker"])


def pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    rule = "|" + "|".join("---" for _ in headers) + "|"
    lines = [head, rule, *("| " + " | ".join(row) + " |" for row in rows)]
    return "\n".join(lines)


def latency_table(latencies: dict[str, list[float]]) -> str:
    rows = []
    for op in ("retain(wait)", "recall", "list", "get"):
        vals = latencies[op]
        rows.append([op, str(len(vals)), f"{pctl(vals, 0.5):.1f}", f"{pctl(vals, 0.95):.1f}",
                     f"{max(vals):.1f}" if vals else "0.0"])
    return md_table(["operation", "n", "p50 ms", "p95 ms", "max ms"], rows)


async def call(s: ClientSession, latencies: list[float], name: str, args: dict) -> dict:
    t0 = time.perf_counter()
    res = await s.call_tool(name, args)
    latencies.append((time.perf_counter() - t0) * 1000.0)
    assert not res.is_error, res.content[0].text if res.content else "<no content>"
    return res.structured_content["result"]


async def seed(s: ClientSession, facts: list[dict], project: str, lat: list[float]) -> dict[str, str]:
    fact_to_mem = {}
    for fact in facts:
        args = {**scoped(fact, project), "content": fact["text"], "memory_type": "fact",
                 "basis": "human_explicit", "operation_id": str(uuid.uuid4()), "wait": True}
        body = await call(s, lat, "retain", args)
        fact_to_mem[fact["id"]] = body["memory_id"]
    return fact_to_mem


async def ask(s: ClientSession, questions: list[dict], project: str, lat: list[float],
              mem_to_fact: dict[str, str], by_id: dict[str, dict]) -> list[dict]:
    results = []
    for q in questions:
        args = {**scoped(q, project), "query": q["query"], "max_results": 5}
        body = await call(s, lat, "recall", args)
        items = body["items"]
        q_bank = bank(q)
        rank, violation, leak = None, False, False
        for i, item in enumerate(items, start=1):
            fid = mem_to_fact.get(item["memory_id"])
            fact = by_id.get(fid) if fid else None
            if fid in q["expect"] and rank is None:
                rank = i
            same_bank = fact is not None and bank(fact) == q_bank
            if fact is not None and not same_bank:
                leak = True
            elif fid in q["must_not"]:
                violation = True
        results.append({"q": q, "rank": rank, "violation": violation, "leak": leak, "items": items})
    return results


def quality_metrics(results: list[dict]) -> tuple[list[list[str]], list[dict]]:
    n = len(results)
    recall_at_1 = sum(1 for r in results if r["rank"] == 1) / n
    recall_at_5 = sum(1 for r in results if r["rank"] is not None) / n
    mrr = sum(1.0 / r["rank"] if r["rank"] else 0.0 for r in results) / n
    violations = sum(1 for r in results if r["violation"])
    leaks = sum(1 for r in results if r["leak"])
    top1_scores = [r["items"][0]["score"] for r in results if r["items"] and r["items"][0]["score"] is not None]
    all_scores = [it["score"] for r in results for it in r["items"] if it["score"] is not None]
    mean_top1 = sum(top1_scores) / len(top1_scores) if top1_scores else 0.0
    score_range = f"{min(all_scores):.3f} / {max(all_scores):.3f}" if all_scores else "n/a"
    rows = [["recall@1", f"{recall_at_1:.2f}"], ["recall@5", f"{recall_at_5:.2f}"], ["MRR", f"{mrr:.2f}"],
            ["must_not violations (same bank)", f"{violations} / {n}"], ["cross-bank leaks", f"{leaks} / {n}"],
            ["mean top-1 score", f"{mean_top1:.3f}"], ["min / max score", score_range]]
    misses = [r for r in results if r["rank"] is None or r["violation"] or r["leak"]]
    return rows, misses


def misses_table(misses: list[dict]) -> str:
    rows = []
    for r in misses:
        top1 = r["items"][0] if r["items"] else None
        text60 = (top1["content"][:60] + "...") if top1 and len(top1["content"]) > 60 else (top1["content"] if top1 else "")
        score = f"{top1['score']:.3f}" if top1 and top1["score"] is not None else "n/a"
        rows.append([r["q"]["query"], ",".join(r["q"]["expect"]), text60, score])
    return md_table(["question", "expected", "top-1 memory text", "top-1 score"], rows)


def build_report(*, label: str, url: str, n_facts: int, cleaned: bool, latencies: dict,
                  quality_rows: list[list[str]], misses: list[dict]) -> str:
    host = urlsplit(url).hostname or url
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    describe = subprocess.run(["git", "describe", "--tags", "--always"], cwd=REPO,
                               capture_output=True, text=True, check=False)
    version = describe.stdout.strip() if describe.returncode == 0 else "unknown"
    lines = [
        f"# ach-memory bench: {label}", "",
        f"host: `{host}`  |  when: {ts}  |  repo: `{version}`", "",
        "## Latency", "", latency_table(latencies), "",
        "## Recall quality", "", md_table(["metric", "value"], quality_rows), "",
        "## Misses", "", misses_table(misses) if misses else "(none)", "",
        f"Seeded {n_facts} facts; cleanup {'ran' if cleaned else 'skipped (--keep)'}.",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> None:
    facts, questions = load_corpus()
    if args.project_only:  # a real user bank must not be seeded with the corpus personas
        facts = [f for f in facts if f["scope"] == "project"]
        questions = [q for q in questions if q["scope"] == "project"]
    value = f"Bearer {args.key}" if args.header.lower() == "authorization" else args.key
    latencies: dict[str, list[float]] = {"retain(wait)": [], "recall": [], "list": [], "get": []}

    async with (
        create_mcp_http_client(headers={args.header: value}) as http,
        streamable_http_client(args.url, http_client=http) as (r, w),
        ClientSession(r, w) as s,
    ):
        await s.initialize()

        by_id = {f["id"]: f for f in facts}
        fact_to_mem = await seed(s, facts, args.project, latencies["retain(wait)"])
        mem_to_fact = {v: k for k, v in fact_to_mem.items()}

        results = await ask(s, questions, args.project, latencies["recall"], mem_to_fact, by_id)

        list_slug = scoped(next(f for f in facts if f["scope"] == "project"), args.project)["project_slug"]
        for _ in range(5):
            await call(s, latencies["list"], "list_memories",
                       {"scope": "project", "project_slug": list_slug, "limit": 20})

        for fid, mid in list(fact_to_mem.items())[:5]:
            fact = by_id[fid]
            await call(s, latencies["get"], "get_memory", {**scoped(fact, args.project), "memory_id": mid})

        cleaned = False
        if not args.keep:
            for fid, mid in fact_to_mem.items():
                fact = by_id[fid]
                await s.call_tool("delete_memory", {**scoped(fact, args.project), "memory_id": mid,
                                                       "reason": "bench cleanup"})
            cleaned = True

    quality_rows, misses = quality_metrics(results)
    report = build_report(label=args.label, url=args.url, n_facts=len(facts), cleaned=cleaned,
                           latencies=latencies, quality_rows=quality_rows, misses=misses)

    out = args.out or REPO / "benchmarks" / "results" / f"{datetime.now(UTC):%Y-%m-%d}-{urlsplit(args.url).hostname}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n")
    print(report)
    print(f"\nwritten to {out}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--header", required=True)
    parser.add_argument("--project", default=f"github.com-ackstorm-bench-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    parser.add_argument("--project-only", action="store_true", help="skip user-scope rows (safe against a real user bank)")
    parser.add_argument("--label", default="", help="free text for the report header")
    asyncio.run(run(parser.parse_args()))
