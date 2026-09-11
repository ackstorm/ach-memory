# Recall pipeline probes — duplicates, budgets, the cap, the reranker

Status: **MEASURED — three fixes shipped, one decision closed**

Stack: local `docker compose`, Hindsight 0.9.1, `cross-encoder/ms-marco-MiniLM-L-6-v2`.
Corpus: `benchmarks/corpus.jsonl` (34 facts, 25 questions) plus synthetic filler where noted.
Probes: `scripts/probes/` (see its README for how each was run).

Six probes, run in the order the findings forced. Each number below is one that decided a
line in `src/memory/read_service.py` or closed a queue item. Where a later probe corrected
an earlier reading, the correction is stated, not the first reading.

## 1. Duplicates accumulate, and the caller saw them (`dup_bill.py`)

13 retains of 10 distinct sentences: 13 provenance rows, **26 upstream entries**, 10 distinct
texts. Two mechanisms, both structural:

- Every retained claim exists upstream twice — the `world` fact and Hindsight's `observation`
  of it, same text plus a `(mentioned_at=…)` suffix — and `resolve_filters` asks for both.
- A re-retain of identical content is a new claim: `accept_retain` compares `payload_hash`
  only against a row already found by `operation_id`; nothing indexes the hash alone.

| query | hits | distinct claims | slots wasted |
|---|---|---|---|
| `how many approvals does a production deployment need` | 8 | 1 | 7/8 |
| `when is the staging database reset` | 6 | 3 | 3/6 |
| `how does this project work` | 6 | 3 | 3/6 |

Neither threshold can help: identical text scores identically. **Shipped in `9db3b81`:**
collapse identical normalized text locally, keeping the copy with a `document_id`. Same bank,
same queries afterwards: 1/1, 3/3, 3/3. Paraphrases are deliberately not collapsed — three
paraphrases of one fact are three claims, and deciding otherwise would be deciding meaning on
the caller's behalf.

`prefer_observations` (Hindsight's own twin-dropper) was measured and rejected: it halves the
upstream payload but the survivor is the observation, which carries no `document_id`.

## 2. Upstream's result budget, and what it cut (`budget_bill.py`, `cap_bill.py`)

`max_tokens` was never sent, so Hindsight applied its 4096 default and reported nothing about
it — its recall response has `results` and no truncation flag. On a 54-claim bank everything fit
(108 entries, constant). On a **129-claim bank it did not**:

| `max_tokens` | entries | distinct claims |
|---|---|---|
| default (4096) | 161 of 258 | **109 of 129** |
| 16384 | 258 | 129 |

38% of the bank cut in `final` (reranker) order, varying 122–167 per query because a token
budget is not a row count. **Shipped in `657f6e2`:** `_RECALL_MAX_TOKENS = 32768`.

The 200-result cap this exposed was split into a scan bound and a normalization bound so the
`semantic` floor judges every entry before anything is discarded. Measured honestly: that half
is insurance. Slicing first left 58 of 258 entries unjudged on this bank and cost no answer,
and the cap could not fire at all while the 4096 default kept responses under 200 entries.
Raising the budget is what makes it live, which is why the two are one commit.

Twenty verbatim re-retains of one fact evicted nothing (0/25 questions lost a claim) at the
54-claim size; at 129 claims the twins consume 39% of the default budget.

## 3. Clean banks: what the pipeline returns in production shape (`clean_bill.py`)

Every earlier probe seeded Alice's and Bob's facts into one bank. That is contaminated: a
`must_not` fact (Bob's) can outrank an expected one and read as a reranker failure. Re-run with
Alice's user facts in Alice's user bank and her project facts in one project bank:

**25/25 expected answers returned**, with the semantic floor (0.60) and the relative cut (1%)
as configured. Floor-only: also 25/25.

But five expected answers carry a cross-encoder score below 0.01 (`q05` 2.2e-04, `q07` 5e-03,
`q08` 1.3e-05, `q09` 2.2e-04, `q06` 0.074). They survive because on those queries the top hit
is also near zero — the cut is relative. The moment a bank holds any hit the reranker is
confident about for that query, even a wrong one, those answers are cut. The contaminated
runs were a realistic preview of a noisy bank.

## 4. What the cross-encoder actually sees (`ce_probe.py`)

Hindsight prepends `[Date: September 11, 2026 (2026-09-11)]` to the document when
`occurred_start` is set, and the observation twin carries `(mentioned_at=…)`. Logit → sigmoid,
per pair, inside the container:

| pair | sigmoid |
|---|---|
| `how are Python dependencies managed here` / bare fact | 0.348 |
| same, with `[Date]` prefix | 0.212 |
| same, query says `pinned` instead of `managed` | **0.998** |
| `q05` expected pair, Alice only | 0.000147 |
| EN query / EN fact | 0.348 |
| ES query / ES fact | 0.952 |
| EN query / ES fact | 0.002 |

The date prefix costs about 0.13. One word in the query moves a pair from 0.35 to 0.998: the
model is lexical. `q05` is a genuine miss with nobody else's facts present. On one pair,
Spanish-to-Spanish outscored English-to-English; what collapses is a language *mismatch* —
the "write memories in English" rule holds because agent queries are English.

Not reproduced: the `make smoke` case where a one-fact bank returned `final=0.000024` for a
fact answering the query. The bare pair scores 0.348 here; whatever else was in that
cross-encoder input has not been identified.

## 5. Reranker bake-off (`rerank_bakeoff.py`)

Same 25 expected pairs, negatives = every same-scope Alice fact plus the corpus `must_not`
facts, through Hindsight's `LocalSTCrossEncoder`:

| model | confident (≥0.5) | outranked by a negative | 25 queries |
|---|---|---|---|
| **MiniLM-L-6-v2** (default) | **20/25** | **8/25** | **5.2s** |
| MiniLM-L-12-v2 | 20/25 | 9/25 | 6.3s |
| mxbai-rerank-base-v1 | 15/25 | 7/25 | 78s |
| bge-reranker-base | 17/25 | 9/25 | 12.3s |

No base-size candidate beats the default. `q05`, `q07` and `q08` fail in all four: lexically
distant pairs this class of model does not resolve. mxbai is differently calibrated (does not
saturate to zero, wider separation) but less often confident and fifteen times slower.

**Decision (2026-09-11): keep MiniLM-L-6-v2.** The pipeline compensates — the floor is on
`semantic`, which sees what the reranker misses. If more is wanted the measured-open paths are
`BAAI/bge-reranker-v2-m3` (untested here, ~2.2 GB RAM) via `HINDSIGHT_API_RERANKER_LOCAL_MODEL`,
or a hosted reranker through LiteLLM via `HINDSIGHT_API_RERANKER_LITELLM_*`. Either is one
environment variable in the Hindsight deployment.

## Left open

- `_RECALL_MAX_TOKENS = 32768` covers roughly 600 claims at the measured ~27 tokens per entry;
  a larger bank is truncated again, silently, further out.
- The `[Date: …]` prefix Hindsight feeds the cross-encoder costs measurable score on at least
  one pair. It is Hindsight's rendering; nothing here changes it.
- The `make smoke` `0.000024` case is unexplained.
