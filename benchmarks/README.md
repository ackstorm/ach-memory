# Benchmarks

The one-off harness that produced `results/` (`scripts/bench.py`, `corpus.jsonl`)
was removed once its decision landed -- MiniLM stays, 2026-09-11. Recover it from
git history if a reranker swap ever needs re-measuring.

## Reading the numbers

`recall@1`/`recall@5`: expected fact ranked 1st, or anywhere in the top 5.
`MRR`: mean reciprocal rank of the first expected hit (0 if never found).
`must_not violations`: how often a forbidden decoy was also returned.
