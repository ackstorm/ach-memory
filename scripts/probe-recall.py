#!/usr/bin/env python3
"""Reproduced-failure probe for recall (SPEC-0.1 §8.3 asks for these before touching the engine).

Ground truth is the live corpus itself: every expectation below was read out of
`list_memories`, so a MISS is a real retrieval failure, not a labelling artefact.
Probes carry exact technical identifiers and Spanish phrasings on purpose -- the two
failure rows §8.3 names.

Expectations pin live production memory ids: a `forget` or `delete_memory` of one of
them turns its probe into a false MISS. Re-dump the banks and fix the id, do not
lower the bar. Credentials come from the env, exactly like the stdio proxy.

Usage (from the repo):  uv run python scripts/probe-recall.py
"""
import asyncio
import os

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from memory.mcp.proxy import auth_headers, resolve_project_context

# (scope, query, expected memory id prefix)
PROBES = [
    ("project", "how do I cut a release and roll it to production", "mem_cd8e82ec"),
    ("project", "can I delete the old hindsight banks", "mem_b8a10145"),
    ("project", "why was working state removed", "mem_5ac7ab29"),
    ("project", "mental models never refresh after consolidation", "mem_027c6d1b"),
    ("project", "how are project bank ids minted", "mem_9144e29d"),
    ("project", "does plugin install exit 0 when already installed", "mem_076169dc"),
    ("project", "where is the production postgres pod", "mem_e729ab4a"),
    ("project", "which environment variables point at production", "mem_be11c201"),
    ("project", "how are operators matched to an identity", "mem_f9c70a1e"),
    ("project", "custom user-defined mental models", "mem_df1db27c"),
    ("project", "ach-memory init for the four hosts", "mem_1c12a47c"),
    ("project", "¿por qué se quitó el working state?", "mem_5ac7ab29"),
    ("project", "¿cómo se corta una release?", "mem_cd8e82ec"),
    ("user", "can I pip install packages globally", "mem_cfd58679"),
    ("user", "should I pin images by sha256 digest", "mem_b1b15429"),
    ("user", "until cmd do sleep done polling loop", "mem_424c2e21"),
    ("user", "what language do I answer in", "mem_2204f16d"),
    ("user", "may I start writing code immediately", "mem_ef121663"),
    ("user", "are slide numbers 1-based", "mem_60833f64"),
    ("user", "what is rtk", "mem_a286fa3c"),
    ("user", "alpine base images", "mem_7f74d5e3"),
    ("user", "¿puedo instalar paquetes de python con pip?", "mem_cfd58679"),
    ("user", "¿puedo escribir código antes de que apruebe?", "mem_ef121663"),
]


async def main() -> None:
    url = os.environ["ACH_MEMORY_URL"]
    project = resolve_project_context()
    client = create_mcp_http_client(auth_headers())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        ranks: list[int | None] = []
        for scope, query, expected in PROBES:
            args = {"scope": scope}
            if scope == "project":
                args["project_slug"] = project
            res = await session.call_tool(
                "recall", {**args, "query": query, "max_results": 10})
            items = res.structured_content["result"]["items"]
            hit = next((pair for pair in enumerate(items, 1)
                        if pair[1]["memory_id"].startswith(expected)), None)
            ranks.append(hit[0] if hit else None)
            # The target's own score is what a floor change is decided on: set the floor to 0
            # on the deployment, read these, then pick a value with a margin under the lowest.
            mark = f"@{hit[0]} {hit[1]['score']:.3f}" if hit else "MISS"
            print(f"{mark:>10}  {len(items):>2} hits  [{scope}] {query}")

        hit = [r for r in ranks if r]
        n = len(ranks)
        print(f"\nrecall@1={sum(1 for r in hit if r == 1)}/{n}  "
              f"recall@3={sum(1 for r in hit if r <= 3)}/{n}  "
              f"recall@10={len(hit)}/{n}")
        es = [r for (s, q, e), r in zip(PROBES, ranks, strict=True) if q.startswith("¿")]
        print(f"spanish probes: {sum(1 for r in es if r)}/{len(es)} found")


asyncio.run(main())
