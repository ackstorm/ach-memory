# Benchmarks

`corpus.jsonl`: 34 `fact` rows (`id, owner, scope, text, match`) and 25
`question` rows (`id, asker, scope, query, expect, must_not, keywords`).
Adversarial by construction: each `must_not` decoy shares vocabulary with
its question's expected fact, so a scope leak returns a confidently wrong
answer instead of noise.

`scripts/bench.py` seeds every fact under one principal (`--key`; `owner`/
`asker` are just labels), runs every question through `recall`, times
`retain`/`recall`/`list_memories`/`get_memory`, writes one markdown report
to `benchmarks/results/`, and cleans up what it seeded.

## Run it

```
# local
uv run python scripts/bench.py --url http://localhost:8000/mcp/ \
    --key alice --header Authorization --label local

# deployment (never commit the key)
uv run python scripts/bench.py --url https://<host>/mcp/ \
    --key "$KEY" --header Authorization --label prod
```

## Reading the numbers

`recall@1`/`recall@5`: expected fact ranked 1st, or anywhere in the top 5.
`MRR`: mean reciprocal rank of the first expected hit (0 if never found).
`must_not violations`: how often a forbidden decoy was also returned.
