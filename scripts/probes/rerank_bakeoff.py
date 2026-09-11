"""Reranker bake-off on our own calibration corpus, inside the hindsight
container, through Hindsight's own loader (so a winner is a one-line config
change, not a code change).

Per model: score every (query, expected fact) pair and every (query, hard
negative) pair -- hard negatives are the corpus's own `must_not` facts plus
every other same-scope fact, i.e. exactly what competes in a real bank.

Reports, per model:
  * expected pairs the model is confident about (sigmoid >= 0.5)
  * expected pairs OUTRANKED by some negative on the same query -- the number
    that actually decides whether the answer survives a relative cut
  * mean expected vs mean best-negative score (separation)
"""
import asyncio
import gc
import json
import math
import sys
import time

sig = lambda x: 1 / (1 + math.exp(-x))

MODELS = sys.argv[1:] or [
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "mixedbread-ai/mxbai-rerank-base-v1",
    "BAAI/bge-reranker-base",
]

with open("/tmp/corpus.jsonl") as corpus:
    rows = [json.loads(line) for line in corpus if line.strip()]
facts = {r["id"]: r for r in rows if r["kind"] == "fact"}
qs = [r for r in rows if r["kind"] == "question"]


def pairs_for(q):
    exp = [facts[f]["text"] for f in q["expect"]]
    same_scope_alice = [
        f["text"] for fid_, f in facts.items()
        if f["owner"] == "alice" and f["scope"] == q["scope"] and fid_ not in q["expect"]
    ]
    hard = [facts[f]["text"] for f in q.get("must_not", []) if f in facts]
    return exp, same_scope_alice + hard


async def run(model_name):
    from hindsight_api.engine.cross_encoder import LocalSTCrossEncoder
    ce = LocalSTCrossEncoder(model_name=model_name)
    r = ce.initialize()
    if asyncio.iscoroutine(r):
        await r
    t0 = time.monotonic()
    confident = outranked = 0
    exp_scores, neg_best = [], []
    worst = []
    for q in qs:
        exp, neg = pairs_for(q)
        scores = await ce.predict([(q["query"], t) for t in exp + neg])
        # Hindsight passes [0,1] scores through and sigmoids logits; mirror it.
        if min(scores) >= 0.0 and max(scores) <= 1.0:
            norm = list(scores)
        else:
            norm = [sig(s) for s in scores]
        e = max(norm[: len(exp)])
        n = max(norm[len(exp):]) if neg else 0.0
        exp_scores.append(e)
        neg_best.append(n)
        confident += e >= 0.5
        if n > e:
            outranked += 1
            worst.append((q["id"], e, n))
    dt = time.monotonic() - t0
    print(f"\n== {model_name} ==")
    print(f"  expected confident (>=0.5) : {confident}/{len(qs)}")
    print(f"  expected OUTRANKED by a negative: {outranked}/{len(qs)}"
          + (f"  {[(q, f'{e:.3f}<{n:.3f}') for q, e, n in worst]}" if worst else ""))
    print(f"  mean expected {sum(exp_scores)/len(qs):.3f} | mean best-negative "
          f"{sum(neg_best)/len(qs):.3f} | min expected {min(exp_scores):.4f}")
    print(f"  {len(qs)} queries scored in {dt:.1f}s")
    del ce
    gc.collect()


async def main():
    for m in MODELS:
        try:
            await run(m)
        except Exception as exc:  # noqa: BLE001
            print(f"\n== {m} ==\n  FAILED: {type(exc).__name__}: {str(exc)[:200]}")


asyncio.run(main())
