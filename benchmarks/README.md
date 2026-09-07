# Benchmarks: ach-memory versus vanilla Hindsight

Two harnesses answering two different questions. They are separate because
they have different costs, different determinism, and different honest
readings.

| | `make bench` | `make bench-quality` |
|---|---|---|
| Question | What does the governance layer enforce? | Does memory retrieve the right thing? |
| Script | `scripts/bench.py` | `scripts/bench_quality.py` |
| LLM | MockLLM, none external | **Real**, costs tokens |
| Deterministic | Yes | No -- reported as mean +/- stdev |
| In `make verify` | No | No |

Both stand up one isolated compose stack and drive **both arms of it**:
`api` is ach-memory, `hindsight` is the unmodified engine it layers over,
published on loopback by `docker-compose.yml`.

## Read this before quoting a number

**ach-memory is a governance layer over Hindsight, not a competing
retrieval engine.** The embeddings, the fact extraction and the synthesis
are Hindsight's. That shapes what these benchmarks can honestly claim:

- Isolation, identity, provenance, idempotency, delivery accounting and
  lifecycle are ach-memory's. `make bench` measures them.
- Raw retrieval quality on a single bank is Hindsight's. ach-memory does not
  improve it and this repository should never claim it does.
- What ach-memory contributes to *quality* is *routing*: the right bank is
  queried, so a contradicting fact from another user or project is not a
  candidate in the first place. `make bench-quality` measures that, and
  measures it against an integrator who shards banks by hand.

**Hindsight is not insecure.** It is a single-tenant engine that resolves
tenancy from an Authorization header. It was never built to partition users
and projects inside one deployment. Where `make bench` prints
`out of scope for the engine`, that is the accurate reading: the capability
is absent by design, which is the reason ach-memory exists. A table that
called those results "failures" would be rigged, and anyone who knows
Hindsight would discard the whole thing.

## `make bench` -- capability differential

Twelve probes. Each runs the same operation against both arms and reports
what it **observed**; no arm's verdict is hardcoded. That matters, because
several properties are wholly or partly Hindsight's own -- it ships
observation history and a `max_tokens` recall cap -- and assuming otherwise
would have produced a table that was wrong in ach-memory's favour.

Verdicts: `enforced`, `not enforced`, `out of scope for the engine`,
`probe error`.

## `make bench-quality` -- retrieval quality

Three arms, and the third one is the point:

| Arm | Setup |
|---|---|
| `ach` | ach-memory routes scope to bank for the caller |
| `vanilla-shared` | one bank holds everything -- the naive default |
| `vanilla-sharded` | one bank per scope, partitioned by the caller |

**The honest comparison is `ach` versus `vanilla-sharded`.** If they come
out close, that is the correct result and should be reported as such:
ach-memory's contribution is enforcing the partition and making it
unforgeable, not retrieving better. `vanilla-shared` shows what the
partition is worth to someone who never builds it.

`benchmarks/corpus.jsonl` is adversarial on purpose. It pairs
near-identical facts across two projects with **contradictory** values
(24-hour versus 7-day idempotency keys, `eu-central-1` versus `us-west-2`,
100 versus 1000 requests per minute) and two users with opposing
preferences (uv versus Poetry, Neovim versus VS Code, English versus
Spanish commits). Retrieving from the wrong scope therefore does not return
noise, it returns a **confidently wrong answer**. That is what
`contamination` counts, and why it matters more than `recall` here.

Metrics: `recall@k`, `contamination@k` (lower is better), `answer ok`
(reflect hit an expected keyword and did not state the contradicting
scope's value), delivered `tokens`, and `latency`.

### Running it

```sh
export HINDSIGHT_LLM_BASE_URL=...   # required, no ambient fallback
export HINDSIGHT_LLM_API_KEY=...
make bench-quality
```

Both arms use the identical model pair, which is not optional: retain wants
message content and reflect wants a tool call, and no single available model
does both (see `docker-compose.yml`). Defaults are
`bedrock.openai.gpt-oss-20b-1-0` for retain and `gemini.gemini-3.7-flash`
for reflect. Different models per arm would make the comparison meaningless.

`BENCH_REPEATS` (default 3) sets the repeat count; `BENCH_TOP_K` (default 5)
the cutoff. Do not report a single run.

## Matching retrieved hits to corpus facts

Hindsight extracts facts rather than storing submitted sentences verbatim,
so exact string matching would score zero for every arm and prove nothing.
Each fact therefore declares its own `match` terms in `corpus.jsonl`, all of
which must appear in a hit for it to count. The terms are in the corpus file
where anyone auditing a result can read them, rather than hidden in a
similarity threshold.

Three matching rules, because one was wrong in both directions:

- multi-word terms match as a phrase;
- a term containing a digit must match a **whole token**, so `7` does not
  match inside `2007`;
- any other word matches a token **prefix**, so `plan` finds "plans" and
  `usd` finds "USD-denominated".

A similarity threshold was tried first and failed exactly where the corpus is
designed to be hard. `p01` ("payments ... idempotency keys for exactly 24
hours") and `s01` ("search ... idempotency keys for exactly 7 days") share
five of eight content words, so containment scored 0.625 and called them the
same fact. That would have counted every cross-project contamination as a
successful recall -- inverting the headline number in ach-memory's favour.

`tests/test_bench.py` pins this: no fact's terms may fire on any other
fact's text, and every fact must match its own. That check found four real
corpus flaws where the contrasting facts each mentioned the other's
distinctive term ("Bob ... considers uv unstable" collided with Alice's uv
preference); those facts were reworded to stay opposed without sharing a
token. The rules apply identically to every arm, which is what keeps the
comparison fair even though absolute numbers move with them.
