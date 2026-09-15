# Lessons from ach-memory (2026-08-23 → 2026-09-14)

This repository is deprecated. What follows is what it cost to learn, so the
successor does not pay for it twice.

## The one sentence

> Every layer traces to the spec; every defence traces to a failure reproduced
> against the real backend. ach-memory (17k lines, 22 days) died of accretion:
> a thin harness grown layer on layer outside its spec, defending against
> enemies it never met.

Burned into `~/.claude/CLAUDE.md` (Coding discipline → "YAGNI is law").

## How we got here

`src/` line count at each tag (626 commits in 22 days):

| Tag | Date | Lines | What was added |
|---|---|---|---|
| v0.1.0 | 08-24 | 5,488 | identity, projects, retain/recall over Hindsight, MCP |
| v0.3.5 | 08-28 | 8,655 | admin plane, observability, activity trail, REST mirror grows |
| memory-quality-v1.4 | 09-04 | **17,377** | "memory quality" phases: own ledger (`retained_records`, revisions, currentness, expiry), retain strategies, read-side shaping, curation state machine, mental-model registry |
| v0.7.1 | 09-11 | 16,094 | QA loop: 29 findings, most of them in code that fought the engine |
| v0.8.0 | 09-14 | 14,531 | thin-harness cut: adapter contract, ledger deleted (−4,255), still 3.5× the target |

The doubling on 09-01 → 09-04 is the story. None of it was in SPEC-v1; each
layer answered a hypothetical ("what if the engine loses a write", "what if two
claims conflict", "what if a claim expires") that was never reproduced against
Hindsight first. Hindsight already had three dates, tags, document upserts,
observations and mental models. We rebuilt half of it beside it, then spent a
QA cycle reconciling the two.

## Anti-patterns this repo exhibits (with evidence)

- **Shadow ledger.** Four tables mirroring engine state (`retained_records`,
  `retained_record_revisions`, `curation_operations`, `bank_currentness`).
  Deleted in 0.8.0; nothing missed them.
- **Defence without a reproduced failure.** `valid_until` (0 of 1,144 memories
  used it), `HindsightOutcomeUnknown` (nothing caught it), tenants table (one
  tenant ever), rate limiting (never tripped), budget checks dead by
  construction (one built-in per bank ≤ budget).
- **REST mirror of the engine.** `/v1/memory/*`, `/v1/read/*`, `/v1/directives`
  — no production caller; only our scripts. 2.9k lines of `api/` for one
  real consumer (the MCP proxy) that never used REST.
- **Hand-rolled protocol.** `mcp/proxy.py` (736 lines) re-implements the
  JSON-RPC stdio loop, SSE parsing and version tracking the `mcp` SDK ships.
- **Rationale essays in code.** 24% of `api/`+`mcp/` is docstrings and
  comments (1,376 lines); `_resolve_bank` alone carries 44 lines of why.
  One line of why plus `git log` is the ceiling.
- **Parameters without callers.** `HindsightClient.recall` exposes five
  options only ever passed as `False`; `RetainItem` carries a retired
  "Phase 3" pipeline; `redact_secrets`/`sanitize_ref` have zero callers.
- **Installer inside the server.** `cli.py` (899 lines) installs the plugin
  into four hosts. That is a script, not part of a memory harness.
- **Same helper ×N.** Atomic-write ×3, `_run` ×3, tag-group matcher ×2,
  retain-body builder ×6 across scripts, MCP session bootstrap ×6.
- **Features nobody read.** A 47 KB dashboard whose Models tab calls a route
  that does not exist.

Rule derived: the single test for any line is *"engine behaviour, or
governance/delivery that must survive an engine replacement?"* Anything
else does not get written.

## What was measured (keep the numbers, drop the code)

- Extraction mode: `verbatim` stores the claim byte-identical plus LLM
  entities/dates; `concise` reworded 13/34 claims. Banks are provisioned
  `verbatim` (`benchmarks/experiments/2026-09-14-extraction/`).
- Observations are twins of world facts and were delivered twice (QA
  F-02/F-12). Recall with `types=["world"]`; leave observations on for
  mental models.
- Reranker bake-off (2026-09-11): base-size cross-encoders measured no better
  than MiniLM; upgrade is one Hindsight env var.
- Our QA corpus saturates recall@5 (all 1.00); the one "miss" was a matcher
  artefact. Do not tune retrieval against it.
- Dedup: idempotency covers the same `operation_id` only; N retains of the
  same text under N ids are N memories (probe `dup_bill`). Hindsight recall
  copes; no ACH dedup was worth its lines.
- Only read-side shaping that earned its place: a semantic-score floor.

## Verified gotchas

Hindsight 0.9.2:
- Production API path is `/api/v1/...` (in-cluster
  `http://hindsight-api.hindsight.svc:8888/api`); the local compose stack is
  `http://localhost:8888/v1/...` with no prefix.
- `PATCH /v1/banks/{id}/config` body is `{"updates": {...}}`, not the bare
  dict (422 otherwise).
- `document_id` upsert replaces the document, its units and the observation
  twin; the old unit ids 404. Idempotent in content, not in ids — which is
  why the ACH memory id equals the document id.
- No expiry. Three dates only: `occurred_*`, `mentioned_at`, `invalidated_at`.
- List filters: `?document_id=&type=&state=&tags=&tags_match=any|all|any_strict|all_strict|exact`.
- Unit history is empty after a replace; a governance journal must live in
  ACH if anyone needs "who changed what".
- The local compose stack uses a mock LLM: verbatim extraction works,
  consolidation and mental-model refresh do not. Measure on production.
- `retain_extraction_mode` is per bank; set and verify it on provision.

MCP / hosts:
- LiteLLM (mcp SDK 1.28.1) sends `initialize` without a protocol-version
  header; a gate demanding the newest revision returned 400 and broke every
  tool listing. Any new server must accept a header-less `initialize`
  (`NegotiatedProtocolMCP` in `api/app.py` is the measured fix).
- Standing context never reached the agent in 0.7.1 (QA F-24): the proxy
  unwrapped a shape the server never sent. Test the hook end to end against
  a real server, not a mock of the reply.
- The remote server cannot see the caller's cwd; project resolution
  (git origin → slug) must run in the stdio child on the host.

Release:
- gitleaks from a git worktree scans 0 commits; mount the main `.git`.
- GitHub push protection matches credential-shaped test fixtures; shape them
  so they do not.
- `make release-cut` leaves the marker commit local if the push fails.
- EKS creds: `AWS_PROFILE=ack-nglz-genai`, `saml2aws login --force --skip-prompt`.
- Flux roll: patch the HelmRelease chart version, reconcile with source;
  GitOps re-pins later.

Security:
- Agents paste credentials. The sanitizer let AWS key pairs and `sk-live-…`
  through until QA F-06. Scrub at the retain boundary, test with real
  shapes, and run a leak scan over smoke output.

## Decisions that stand

- ACH is a governed, engine-neutral access and delivery layer. It never
  re-implements memory (spec v3 §1, invariants I1–I8).
- Public memory id = engine document id, minted by ACH (`mem_<32hex>`).
- Retain carries `content`, `memory_type`, `basis`, `operation_id`, tags —
  no evidence, trigger or `valid_until`. Vocabulary as tags:
  `type:`, `basis:`, `schema:ach-retain-v1`.
- Mental models are native to the engine; ACH only names the built-ins and
  provisions them at first retain.
- MCP is the only delivery. No REST mirror of engine operations.
- Corrections are synchronous and serialised per memory; first retains are
  async with the engine operation ref exposed.
- Working State is not memory: transient, per workspace, overwritten. Removed in
  0.2.0 (2026-09-15): nothing ever read it, and the host's compaction summary covers it.
