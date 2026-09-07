#!/usr/bin/env python3
"""Retrieval quality: ach-memory versus vanilla Hindsight, on one corpus.

Usage:
    HINDSIGHT_LLM_BASE_URL=... HINDSIGHT_LLM_API_KEY=... make bench-quality

Why three arms, not two
-----------------------
ach-memory is a governance layer over Hindsight. The embeddings, extraction
and synthesis are Hindsight's, so a two-arm comparison that pours every
fact into one shared bank and then reports ach-memory winning is measuring
its own setup, not the product. A competent integrator using Hindsight
directly would shard banks by hand. So:

  ach              -- ach-memory routes scope -> bank for the caller
  vanilla-shared   -- one bank for everything (the naive default)
  vanilla-sharded  -- one bank per scope, partitioned by the caller

The interesting number is ach versus vanilla-sharded. If those are close,
that is the honest result: ach-memory's contribution is enforcing the
partition and making it unforgeable, not retrieving better. vanilla-shared
is included to show what the partition is worth when nobody builds it.

The corpus is adversarial by construction: benchmarks/corpus.jsonl pairs
near-identical facts across two projects with CONTRADICTORY values (24-hour
versus 7-day idempotency keys, eu-central-1 versus us-west-2), and two users
with opposing preferences (uv versus Poetry, Neovim versus VS Code). A
system that retrieves from the wrong scope does not merely return noise, it
returns a confidently wrong answer -- which is what `contamination` counts.

Metrics
-------
recall@k         a fact the question expects appears in the top k hits
contamination@k  a fact the question forbids appears in the top k hits
answer_ok        reflect's synthesis hits an expected keyword and no
                 contradicting one
tokens           delivered context size, ach's own tokenizer, both arms
latency_ms       wall clock per recall

Every metric is reported as mean +/- stdev over BENCH_REPEATS runs
(default 3). A single run of an LLM-backed retrieval benchmark is an
anecdote.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from benchlib import API, HINDSIGHT_URL, Http, Samples, bank_path, table

from memory.delivery import count_tokens

MASTER = os.environ.get("MEMORY_MASTER_KEY")
if not MASTER:
    print("FAIL: MEMORY_MASTER_KEY is not set. Run via `make bench-quality`.", file=sys.stderr)
    sys.exit(1)

REPEATS = int(os.environ.get("BENCH_REPEATS", "3"))
TOP_K = int(os.environ.get("BENCH_TOP_K", "5"))
CORPUS = Path(__file__).resolve().parents[1] / "benchmarks" / "corpus.jsonl"

RUN = uuid.uuid4().hex[:8]

# Line-buffered, the same reason scripts/e2e.py does it: this run takes tens
# of minutes against a real LLM, and under redirection a block-buffered
# stdout shows an empty log the whole time, which is indistinguishable from
# a hang.
sys.stdout.reconfigure(line_buffering=True)

ARMS = ("ach", "vanilla-shared", "vanilla-sharded")

def normalize(text: str) -> str:
    return " ".join(text.lower().replace("\u2019", "'").split())


def is_same_fact(fact: dict, hit_text: str) -> bool:
    """Did this retrieved hit come from that corpus fact?

    Matching is by the fact's own `match` terms, declared in
    benchmarks/corpus.jsonl and visible to anyone auditing a result. ALL of
    a fact's terms must be present in the hit.

    A similarity threshold was tried first and was wrong in the one place
    the corpus is designed to be hard. p01 ("payments ... idempotency keys
    for exactly 24 hours") and s01 ("search ... idempotency keys for exactly
    7 days") share five of eight content words, so containment scored 0.625
    and called them the same fact -- which would have counted every
    cross-project contamination as a successful recall and inverted the
    headline number. Explicit terms make the discriminating token ("24"
    versus "7") decide, which is the whole point of the pairing.
    """
    hit = normalize(hit_text)
    terms = fact.get("match") or []
    if not terms:
        return False
    return all(_present(term, hit) for term in terms)


def _present(term: str, hit: str) -> bool:
    """Three rules, because one rule was wrong in both directions.

    Multi-word terms match as a phrase. A term containing a digit must match
    a WHOLE token, so "7" does not match inside "2007" and "24" does not
    match inside "240". Any other word matches a token PREFIX, so "plan"
    still finds "plans" and "usd" still finds "USD-denominated" -- Hindsight
    rewrites what it stores, and a matcher that misses ordinary
    pluralisation scores every arm too low.
    """
    term = normalize(term)
    if " " in term:
        return term in hit
    tokens = {w.strip(".,:;()\"'") for w in hit.split()}
    if any(c.isdigit() for c in term):
        return term in tokens
    return any(token.startswith(term) for token in tokens)


@dataclass
class RepeatTally:
    """One repeat's raw per-question observations, for ONE arm."""

    recall: list[float] = field(default_factory=list)
    contamination: list[float] = field(default_factory=list)
    answer_ok: list[float] = field(default_factory=list)
    tokens: list[float] = field(default_factory=list)
    latency: list[float] = field(default_factory=list)


@dataclass
class ArmMetrics:
    """Per-REPEAT means. One value per repeat, never one per question.

    Pooling every question observation into these instead made the reported
    spread the Bernoulli spread of a 0/1 metric -- 96% recall printed as
    "96.0 +/- 19.7%", where 19.7 is just sqrt(0.96*0.04) and says nothing
    about whether a second run would agree. Run-to-run stability is the
    only thing the +/- is there to answer.
    """

    recall: Samples = field(default_factory=Samples)
    contamination: Samples = field(default_factory=Samples)
    answer_ok: Samples = field(default_factory=Samples)
    tokens: Samples = field(default_factory=Samples)
    latency: Samples = field(default_factory=Samples)


def record(failures: list, arm: str, repeat: int, row: dict) -> None:
    """Keep every per-question miss, so an aggregate can be explained."""
    if not row["recall"]:
        failures.append((arm, repeat, {**row, "metric": "recall"}))
    if row["contamination"]:
        failures.append((arm, repeat, {**row, "metric": "contamination",
                                       "why": "a forbidden fact appeared in the top hits"}))
    if not row["answer_ok"]:
        failures.append((arm, repeat, {**row, "metric": "answer_ok"}))


def load_corpus() -> tuple[list[dict], list[dict]]:
    facts, questions = [], []
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        (facts if row["kind"] == "fact" else questions).append(row)
    return facts, questions


def bank_for(arm: str, fact_or_q: dict, run_tag: str) -> str:
    """Which vanilla bank a fact or question belongs to, per arm."""
    if arm == "vanilla-shared":
        return f"bench_shared_{run_tag}"
    scope = fact_or_q.get("scope")
    if scope == "project":
        return f"bench_proj_{fact_or_q['project']}_{run_tag}"
    owner = fact_or_q.get("owner") or fact_or_q.get("asker")
    return f"bench_user_{owner}_{run_tag}"


# ==========================================================================
# Arms
# ==========================================================================


async def seed_ach(ach: Http, facts: list[dict], ctx: dict) -> None:
    for fact in facts:
        key = ctx[f"key.{fact['owner']}"]
        body = {
            "scope": fact["scope"],
            "content": fact["text"],
            "memory_type": "preference" if fact["scope"] == "user" else "fact",
            "basis": "human_explicit",
            "trigger": "user_requested",
            "evidence": [{"kind": "user_quote", "raw": fact["text"]}],
            "operation_id": str(uuid.uuid4()),
        }
        if fact["scope"] == "project":
            body["project_slug"] = ctx[f"project.{fact['project']}"]
        status, data = await ach.call(
            "POST", "/v1/memory/sync_retain", key=key, json_body=body, timeout=180.0
        )
        if status != 200:
            print(f"  warn: ach seed {fact['id']} -> HTTP {status} {str(data)[:120]}")


async def seed_vanilla(van: Http, facts: list[dict], arm: str, run_tag: str) -> None:
    for fact in facts:
        bank = bank_for(arm, fact, run_tag)
        status, data = await van.call(
            "POST", f"{bank_path(bank)}/memories",
            json_body={
                "items": [{"content": fact["text"]}],
                "async": False,
                "operation_id": str(uuid.uuid4()),
            },
            timeout=180.0,
        )
        if status >= 300:
            print(f"  warn: {arm} seed {fact['id']} -> HTTP {status} {str(data)[:120]}")


async def ask_ach(ach: Http, q: dict, ctx: dict) -> tuple[list[str], float, str]:
    body = {"scope": q["scope"], "query": q["query"]}
    if q["scope"] == "project":
        body["project_slug"] = ctx[f"project.{q['project']}"]
    key = ctx[f"key.{q['asker']}"]
    status, data, ms = await ach.timed(
        "POST", "/v1/memory/recall", key=key, json_body=body, timeout=180.0
    )
    hits = []
    if status == 200 and isinstance(data, dict):
        hits = [h.get("text", "") for h in data.get("result", {}).get("hits", [])]

    answer = ""
    s, d = await ach.call(
        "POST", "/v1/memory/reflect", key=key, json_body=body, timeout=300.0
    )
    if s == 200 and isinstance(d, dict):
        answer = json.dumps(d.get("result", d))
    return hits, ms, answer


async def ask_vanilla(van: Http, q: dict, arm: str, run_tag: str) -> tuple[list[str], float, str]:
    bank = bank_for(arm, q, run_tag)
    status, data, ms = await van.timed(
        "POST", f"{bank_path(bank)}/memories/recall",
        json_body={"query": q["query"]}, timeout=180.0,
    )
    hits: list[str] = []
    if status == 200 and isinstance(data, dict):
        for key in ("facts", "memories", "hits", "results", "items"):
            value = data.get(key)
            if isinstance(value, list):
                hits = [
                    (item.get("text") or item.get("content") or "")
                    if isinstance(item, dict) else str(item)
                    for item in value
                ]
                break

    answer = ""
    s, d = await van.call(
        "POST", f"{bank_path(bank)}/reflect", json_body={"query": q["query"]}, timeout=300.0
    )
    if s == 200:
        answer = json.dumps(d)
    return hits, ms, answer


# ==========================================================================
# Scoring
# ==========================================================================


def score(q: dict, facts_by_id: dict, hits: list[str], answer: str, m: RepeatTally, ms: float) -> dict:
    top = hits[:TOP_K]
    expected = [facts_by_id[i] for i in q["expect"] if i in facts_by_id]
    forbidden = [facts_by_id[i] for i in q.get("must_not", []) if i in facts_by_id]

    m.recall.append(1.0 if any(is_same_fact(e, h) for e in expected for h in top) else 0.0)
    m.contamination.append(
        1.0 if any(is_same_fact(f, h) for f in forbidden for h in top) else 0.0
    )
    m.tokens.append(float(count_tokens("\n".join(top))))
    m.latency.append(ms)

    # `_present`, not `k.lower() in answer`: raw substring matching let the
    # keyword "one" (q23, "one approval") pass on "none", "someone" or
    # "money", and "two" (q18) on "network". A metric that can pass on an
    # answer it never read is worse than no metric.
    low = normalize(answer)
    wanted = any(_present(k, low) for k in q.get("keywords", []))
    # An answer that also states the contradicting scope's value is not a
    # pass, however many expected keywords it hit.
    contradicted = any(is_same_fact(f, answer) for f in forbidden)
    ok = wanted and not contradicted
    m.answer_ok.append(1.0 if ok else 0.0)
    return {
        "id": q["id"],
        "recall": bool(m.recall[-1]),
        "contamination": bool(m.contamination[-1]),
        "answer_ok": ok,
        "why": "" if ok else ("stated the other scope's value" if contradicted
                              else f"no expected keyword {q.get('keywords', [])} in the answer"),
    }


async def main() -> int:
    facts, questions = load_corpus()
    facts_by_id = {f["id"]: f for f in facts}
    print(f"corpus: {len(facts)} facts, {len(questions)} questions, "
          f"{REPEATS} repeat(s), top-{TOP_K}")
    print(f"ach-memory : {API}")
    print(f"hindsight  : {HINDSIGHT_URL}")
    print()

    metrics = {arm: ArmMetrics() for arm in ARMS}
    failures: list[tuple[str, int, dict]] = []

    async with Http(API) as ach, Http(HINDSIGHT_URL) as van:
        for repeat in range(REPEATS):
            run_tag = f"{RUN}r{repeat}"
            print(f"repeat {repeat + 1}/{REPEATS}")

            ctx: dict = {}
            for name in ("alice", "bob"):
                uid = f"bq-{name}-{run_tag}"
                s, _ = await ach.call("POST", "/v1/users", key=MASTER, json_body={"id": uid})
                if s != 201:
                    raise SystemExit(f"could not create {uid}: HTTP {s}")
                ctx[f"user.{name}"] = uid
                s, d = await ach.call(
                    "POST", f"/v1/users/{uid}/keys", key=MASTER, json_body={}
                )
                ctx[f"key.{name}"] = d["key"]
            for project in ("payments", "search"):
                slug = f"bq-{project}-{run_tag}"
                await ach.call(
                    "POST", "/v1/projects", key=ctx["key.alice"],
                    json_body={"project_slug": slug},
                )
                ctx[f"project.{project}"] = slug

            print("  seeding ach ...")
            await seed_ach(ach, facts, ctx)
            for arm in ("vanilla-shared", "vanilla-sharded"):
                print(f"  seeding {arm} ...")
                await seed_vanilla(van, facts, arm, run_tag)

            print("  asking ...")
            tally = {arm: RepeatTally() for arm in ARMS}
            for q in questions:
                hits, ms, answer = await ask_ach(ach, q, ctx)
                record(failures, "ach", repeat,
                       score(q, facts_by_id, hits, answer, tally["ach"], ms))
                for arm in ("vanilla-shared", "vanilla-sharded"):
                    hits, ms, answer = await ask_vanilla(van, q, arm, run_tag)
                    record(failures, arm, repeat,
                           score(q, facts_by_id, hits, answer, tally[arm], ms))

            # One value per repeat per metric, so `Samples.stdev` answers
            # "would another run agree?" rather than "how binary is this
            # metric?".
            for arm in ARMS:
                for name in ("recall", "contamination", "answer_ok", "tokens", "latency"):
                    values = getattr(tally[arm], name)
                    if values:
                        getattr(metrics[arm], name).add(statistics.fmean(values))

    print()
    rows = []
    for arm in ARMS:
        m = metrics[arm]
        rows.append([
            arm,
            m.recall.render(pct=True),
            m.contamination.render(pct=True),
            m.answer_ok.render(pct=True),
            m.tokens.render(digits=0),
            m.latency.render(digits=0),
        ])
    print(table(
        ["Arm", f"recall@{TOP_K}", f"contamination@{TOP_K}", "answer ok", "tokens", "latency ms"],
        rows,
    ))
    if failures:
        print()
        print("Per-question failures")
        print("---------------------")
        seen: dict[tuple[str, str, str], list[int]] = {}
        for arm, repeat, row in failures:
            key = (arm, row["id"], row["metric"])
            seen.setdefault(key, []).append(repeat)
        for (arm, qid, metric), repeats in sorted(seen.items()):
            detail = next(r["why"] for a, _, r in failures if a == arm and r["id"] == qid)
            stable = "every repeat" if len(repeats) == REPEATS else f"repeats {repeats}"
            print(f"  {arm:<16} {qid} {metric:<14} ({stable}): {detail}")
        print()
        print("A failure in BOTH ach and vanilla-sharded is not an ach-memory")
        print("gap: those arms differ only in authorization, scope resolution")
        print("and audit, never in what is retrieved or how it is synthesized.")

    print()
    print("Lower contamination is better. `ach` versus `vanilla-sharded` is the")
    print("honest comparison; `vanilla-shared` shows the cost of no partition.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
