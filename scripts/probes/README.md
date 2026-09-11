# Probes

One-off measurements, each answering one question about the recall pipeline
against a running local stack. Not gates, not benchmarks: `make verify` never
runs them, `make bench` is the comparative harness. A probe is kept because its
number decided something in `src/` -- the decision and the number are in the
commit that cites it, and the probe is how to re-measure when the premise
changes (a Hindsight upgrade, a different reranker, a bigger bank).

Every probe seeds a fresh identity through the real retain path and reads
either through `POST /v1/read/recall` (what a caller gets) or Hindsight's own
`/memories/recall` on loopback (what the engine returned before ach-memory
narrowed it). Writes are paced under `MEMORY_WRITE_LIMIT` (60/60s).

Results as measured on 2026-09-11 are in
`docs/results/2026-09-11-recall-pipeline-probes.md`.

| Probe | Question | Runs |
|---|---|---|
| `dup_bill.py` | Do duplicates accumulate, and does the caller see them? | host: `uv run python scripts/probes/dup_bill.py` |
| `twin_bill.py` | Does `source_fact_ids` survive a token-starved `source_facts` map, and are this stack's observations single-source and verbatim? (Reads `memory_units` on `hindsight-db` directly for the census.) | host |
| `budget_bill.py` | Do duplicates evict answers from upstream's result budget? | host |
| `cap_bill.py` | Does the 200-result cap blind an admissible hit? | host |
| `clean_bill.py` | Production-shaped banks (Alice only): is every expected answer returned, and if not, which stage withheld it? | host |
| `ce_probe.py` | What does the cross-encoder score for specific pairs -- date prefix, `mentioned_at` suffix, paraphrase, language? | container |
| `rerank_bakeoff.py` | Does another local reranker beat the default on this corpus? | container |

Host probes need `make up` first. Container probes run inside the hindsight
service, where the model and Hindsight's own loader are:

```bash
docker compose cp benchmarks/corpus.jsonl hindsight:/tmp/corpus.jsonl
docker compose cp scripts/probes/rerank_bakeoff.py hindsight:/tmp/rerank_bakeoff.py
docker compose exec -T hindsight python /tmp/rerank_bakeoff.py            # default candidates
docker compose exec -T hindsight python /tmp/rerank_bakeoff.py BAAI/bge-reranker-v2-m3
```

`rerank_bakeoff.py` downloads each candidate from the HF Hub on first use;
the container's RAM has to hold the largest one you name.

Seeding all 34 corpus facts into one bank -- Bob's included -- is a
contaminated measurement: a `must_not` fact competes with an expected one
and looks like a reranker failure. `clean_bill.py` is the production shape;
`cap_bill.py` and `budget_bill.py` accept the contamination because they
measure budgets, not relevance.
