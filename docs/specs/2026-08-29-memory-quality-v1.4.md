# Memory Quality — Unified SPEC

**Status:** Draft  
**Date:** 2026-08-29  
**Scope:** ach-memory memory methodology and delivery path  
**Engine:** Hindsight 0.9.x  
**Primary use case:** long-lived coding agents

---

## 1. Purpose

ach-memory is a context compiler over historical evidence.

It must:

1. capture relevant evidence from coding sessions;
2. synthesize durable user and project profiles;
3. maintain ephemeral working state separately from durable memory;
4. preserve provenance and superseded history;
5. compile the smallest useful context for the consuming agent and host;
6. keep explicit recall as a long-tail mechanism for history and uncommon questions.

The product boundary is:

```text
AGENTS.md / host instructions = commanded policy
Repository                    = derivable truth
ach-memory                    = learned, non-derivable context
```

The system must remember what the repository cannot reliably tell the next agent.

---

## 2. Scope

### 2.1 In scope

- memory capture methodology;
- transcript extraction;
- evidence classification;
- provenance;
- user and project memory separation;
- durable profile synthesis;
- Working State;
- context compilation;
- delivery to agent hosts;
- recall behavior;
- profile budgets;
- invalidation and supersession;
- quality assertions and behavioral evaluation.

### 2.2 Out of scope

- identity, tenancy and authorization design already governed by `SPEC-v1.md`;
- engine migration away from Hindsight;
- public console exposure without SSO;
- exposing or logging the master key;
- automatic lifecycle cleanup before volume justifies it;
- general concurrent Working State merging across sessions that share the same workspace;
- general repository indexing or codebase knowledge storage;
- mirroring `CLAUDE.md` / `AGENTS.md` into memory;
- agent-declared confidence, importance or temporality;
- a multi-model profile suite before one structured model per bank has been validated.

---

## 3. Current State

### 3.1 Runtime baseline

- ach-memory: `v0.3.5`.
- Hindsight: `0.9.1`.
- Retain model: `bedrock.openai.gpt-oss-20b-1-0`.
- Reflect / mental-model model: `gemini.gemini-3.7-flash`.
- Mental-model refresh trigger:

```yaml
mode: delta
refresh_cron: "0 3 * * *"
keep_trace: true
```

- Claude Code truncates MCP server instructions at 2048 characters.

### 3.2 Current methodology

```text
agent retain calls
    ↓
Hindsight facts
    ↓
Hindsight observations
    ↓
one 400-token mental model per bank
    ↓
session brief
    ↓
MCP instructions
```

This path is insufficient because the write path is active while the read path does not reliably deliver the resulting context.

### 3.3 Measured inventory

| Bank | Facts | Raw/world | Observations | Status |
|---|---:|---:|---:|---|
| user bank | 95 | 74 | 21 | live |
| canonical project bank | 75 | 51 | 24 | live |
| slug-only duplicate project bank | 47 | 29 | 18 | unreachable split-brain |

Additional measurements:

- ~30 near-duplicate evidence/observation pairs surface together because reads flatten Hindsight layers.
- 24/95 user-bank rows are known noise from colour/styling tests and canaries.
- 92/95 user-bank rows have `proof_count=1`; the three highest rows have proof counts 24, 14 and 7.
- user identity is fragmented across multiple entity names because `entity_labels` is not configured.
- bank configuration is effectively factory-default: no missions, no tag strategy and no entity vocabulary.
- project writes are common while explicit recalls from sessions are close to zero.

### 3.4 Known defects

| Surface | Defect |
|---|---|
| `/v1/session-brief` with master key | user scope resolves with `user_id=None`; console Brief tab fails |
| Console Models tab | does not request `detail=full`; model content is not shown |
| Brief delivery | composed brief exceeds host cap; project context is dropped |
| `GET /v1/session-brief` | read path can create a mental model through `ensure_section` |
| Project identity | canonical locator bank and slug-only bank can coexist for one repository |

### 3.5 Delivery failure

Current composed brief size is approximately 6115 characters:

- ~1422 chars static policy;
- ~2018 chars user memory;
- ~2397 chars project memory;
- headers and caveats.

Under Claude Code's 2048-character instruction cap, only ~626 characters of actual memory survive. The project section does not reach the agent.

**Invariant:** quality is measured on the delivered payload, not the server-rendered payload.

---

## 4. Target Model

### 4.1 Logical layers

The target system has four memory/context layers.

#### Evidence

Historical, source-backed material retained for consolidation and long-tail recall.

Properties:

- may be noisy;
- may conflict with newer evidence;
- preserves provenance;
- may never become active context;
- may contain inferred claims;
- is not equivalent to durable truth.

#### User Profile

Durable, private, cross-project understanding of how the user works.

Typical contents:

- interaction and communication preferences;
- engineering philosophy;
- review expectations;
- stable tooling preferences;
- durable personal constraints;
- repeated corrections.

Personal attribution is allowed.

#### Project Profile

Durable, shareable, impersonal project knowledge that is not cheaply derivable from the repository.

Typical contents:

- non-obvious architectural constraints;
- decisions and rationale;
- workflow;
- testing conventions;
- project conventions;
- operational gotchas and causes;
- rejected or superseded approaches when their rationale prevents future mistakes.

Personal attribution is not allowed.

#### Working State

Ephemeral continuity for the current work.

It is not a Hindsight fact and does not participate in the durable memory ladder.

```yaml
objective:
current_direction:
recent_decisions:
open_questions:
next_steps:
updated_at:
session_id:
session_epoch:
checkpoint_seq:
```

Properties:

- one record per `(user, project, workspace)`;
- `workspace` is a deterministic identity derived from the current git worktree root when available; branch name is not workspace identity;
- replace-on-write;
- no history;
- no TTL;
- age is always visible to the consumer;
- ordered by `(session_epoch, checkpoint_seq)`;
- a lower ordering pair cannot overwrite a higher pair.

### 4.2 Project Metadata

Project orientation is deterministic metadata, not learned memory.

```yaml
name:
locator:
canonical_spec:
purpose:
```

It is created from the canonical git locator, may consume explicit project metadata such as front matter, is editable, and is not stored as a Hindsight fact.

### 4.3 Target architecture

```text
                         ach-memory

                     transcript slice
                 (Stop / PreCompact hook)
                            │
                     local sanitize
                            │
                            ▼
                    BACKGROUND PASS
                  origin · kind · provenance
                            │
             ┌──────────────┴──────────────┐
             │                             │
             ▼                             ▼
       WORKING STATE                   CANDIDATE
       replace-only                  one claim each
   (session_epoch, seq)                    │
             │                 kind × origin → eligibility
             │                             │
             │                  ┌──────────┴──────────┐
             │                  ▼                     ▼
             │           evidence_only          profile_eligible
             │                  │                     │
             │                  └──── HINDSIGHT ──────┘
             │                     facts + metadata
             │                     tags define scope
             │                             │
             │                    scoped consolidation
             │                             │
             │                             ▼
             │                     DURABLE PROFILES
             │                   user · project
             │                             │
             └──────────────┬──────────────┘
                            │
                     project metadata
                            │
                            ▼
                    CONTEXT COMPILER
                    brief_revision N
             ┌──────────────┴──────────────┐
             ▼                             ▼
        INDEX tier                     FULL tier
     MCP instructions             SessionStart hook
     host-bounded                cached, age visible
                            │
                     explicit recall
                 evidence · history · rationale
```

---

## 5. Core Invariants

1. Evidence may be noisy; active context may not.
2. Current truth and historical truth are separate.
3. User and project memory are separate.
4. Project memory must be shareable and impersonal.
5. Working State can never silently become durable project truth.
6. Repository-derivable information is excluded unless there is a specific non-derivable finding attached to it.
7. Host policy is authoritative and is never rewritten by synthesized memory.
8. Important synthesized statements preserve enough provenance to explain why they exist.
9. Context is compiled for a host-specific budget.
10. Explicit recall is not required for normal orientation.
11. Reads must never create state.
12. Two banks for one repository are a defect.
13. `profile_eligible` is computed structurally by the harness, never declared by an agent or trusted to a prompt.
14. Transcript extraction is at-least-once and idempotent.
15. The background pass is the single semantic extractor.
16. One retained candidate equals one semantic claim.
17. An inferred claim never reaches a durable profile.
18. Quality is evaluated at the consuming agent.
19. Explicit human correction overrides conflicting prior current truth.

---

## 6. Candidate Classification

### 6.1 Origin

Every candidate has exactly one origin:

| Origin | Meaning |
|---|---|
| `stated` | explicitly stated by the human |
| `confirmed` | proposed by the agent and explicitly accepted by the human |
| `observed` | established through execution, test, log, API response or authoritative source, with an artifact reference |
| `inferred` | agent conclusion not directly established |

An `observed` candidate without an artifact reference degrades to `inferred`.

Origin is provenance, not a confidence score.

### 6.2 Kind

Supported kinds:

- `preference`
- `decision`
- `convention`
- `gotcha`
- `technical_claim`
- `working_state`

A `gotcha` requires a failure plus a cause or reproducible condition. A failure without a cause/reproduction is a `technical_claim`.

### 6.3 Classification precedence

When a candidate fits multiple kinds:

1. `working_state`;
2. `gotcha`, only if cause/reproduction exists;
3. `preference`, `decision`, or `convention`, based on what the claim prescribes;
4. `technical_claim` as residual.

Agent plans, proposals, research output and next steps are not decisions.

Human acceptance of a proposal confirms only the accepted gist, not every detail of the proposal.

Authorization to execute, test or explore a proposal is not confirmation of a durable decision. A `confirmed` decision requires acceptance of the decision-level gist itself.

An explicit human correction of an existing claim supersedes conflicting prior current truth. It is not treated as ordinary recurrence.

### 6.4 Durability matrix

| kind | stated | confirmed | observed | inferred |
|---|---|---|---|---|
| `preference` | profile | profile | evidence | evidence |
| `decision` | profile | profile | evidence | evidence |
| `convention` | profile | profile | profile | evidence |
| `gotcha` | profile | profile | profile | evidence |
| `technical_claim` | evidence candidate | evidence candidate | evidence candidate | evidence |
| `working_state` | Working State | Working State | Working State | Working State |

For `working_state`, routing is determined by kind before bank retention; it never becomes a Hindsight fact.

### 6.5 Bank routing

The subject determines the bank.

```text
how this person works   → user bank
how this project works  → project bank
what is happening now   → Working State
```

The session's attached bank does not decide routing.

A concept may legitimately produce both a user-profile statement and a project-profile statement, but each must be independently phrased for its subject.

Corrections and overrides are scope-fenced by the same routing rule:

- a project-scoped correction can supersede only project current truth;
- a user-scoped correction can supersede only user current truth;
- local wording such as `here`, `in this repo`, `for this project` or task-specific instructions must not widen into user scope;
- a local project override does not contradict or supersede a compatible global user preference.

### 6.6 Importance

Importance is derived and is never supplied by an agent.

Ordering inputs:

1. kind;
2. distinct source/session count;
3. profile item budget.

Rules:

- `decision`, `convention`, and valid `gotcha` are durable on first qualifying evidence;
- a `stated` or `confirmed` preference is profile-eligible on first qualifying evidence; recurrence increases ordering but is not required for eligibility;
- `observed` and `inferred` preferences remain evidence;
- `technical_claim` remains evidence until it is expressed as a qualifying `decision`, `convention` or `gotcha`;
- `working_state` is never durable;
- one semantic claim consumes one profile item;
- multiple claims may not be packed into one item to bypass a budget;
- when a profile is full, a new item must displace an existing item.

Initial provisional budgets:

```yaml
user_profile:
  max_items: 15

project_profile:
  max_items: 25
```

Final values are set after delivered-context measurement.

---

## 7. Capture and Write Path

### 7.1 Primary capture path

The primary automatic write path is transcript checkpoint extraction, not mid-task `retain` drip.

Trigger:

- Stop hook;
- PreCompact hook;
- equivalent host hook carrying the transcript and checkpoint boundary.

Input:

- `transcript_path`;
- last processed offset;
- session identity;
- project identity;
- workspace identity;
- host identity.

The extraction unit is a transcript slice.

### 7.2 Local preprocessing

Before any network call, the hook/job must:

1. redact known secret patterns;
2. cap tool output per call;
3. exclude repository file contents that are cheaply derivable;
4. avoid raw dumps unless evidence for a non-obvious gotcha requires them.

The extractor LLM must not be the first component to see an avoidable credential or repository dump.

### 7.3 Idempotency

A slice identity is defined by:

```text
session_id
start_offset
end_offset
content_hash
```

Requirements:

- processing is at-least-once;
- submitting the same slice twice must not create duplicate evidence;
- submitting the same slice twice must not advance Working State twice;
- `document_id` is the slice key;
- asynchronous retain calls use a caller-provided `operation_id`;
- retry is used for lost acknowledgements, not casual re-submission.

A document replacement can reset observations for that document and therefore must not be used as a normal retry strategy.

### 7.4 Background pass output

The background pass is the only semantic extractor.

It emits final one-claim candidates with at least:

```yaml
text:
kind:
origin:
provenance:
profile_eligible:
bank:
host:
session_id:
session_epoch:
checkpoint_seq:
slice_hash:
```

It also emits the current Working State.

Hindsight does not re-classify the candidate. Its retain extractor is configured to store the candidate as given.

### 7.5 Extraction rules

The background pass must:

- emit one semantic claim per candidate;
- preserve claim/evidence strength;
- route by subject, not session attachment;
- use impersonal phrasing for project memory;
- keep personal preferences out of the project bank;
- treat plans and next steps as Working State, not durable decisions;
- treat permission to execute, test or explore as authorization, not as confirmation of a durable decision;
- treat explicit human correction as superseding conflicting prior current truth only within the correction's routed scope;
- preserve explicit negative constraints as negative constraints; do not weaken `do not X` into a generic positive preference;
- route negative constraints as `preference`, `convention` or `decision` according to subject and meaning;
- exclude greetings, logistics, canaries, styling/colour noise and cheap repository facts;
- keep directly observed, non-obvious findings with artifact provenance;
- prefer a coherent recap claim over multiple fragments only when the fragments form one semantic claim.

### 7.6 Explicit `retain`

`retain` remains available for explicit requests such as "remember this".

Its contract is evidence/candidate capture, not unconditional durable-memory creation.

It is not the normal automatic path for routine session events.

---

## 8. Hindsight Configuration

### 8.1 Store-as-given retain mission

For already-extracted candidates:

```text
Each item is one already-extracted claim.
Store it as one fact, as written.
Do not split, merge, rephrase, infer, or add facts.
Keep the metadata.
```

This behavior must be verified with `dry-run-extract` before the background path is considered safe.

### 8.2 Observation mission

Observations represent current truth, not the full historical journey.

Required behavior:

- merge restatements;
- count distinct sessions/sources rather than raw restatements;
- when beliefs conflict, keep the current belief;
- keep an older choice in the active observation only when a short reason prevents a likely future mistake;
- keep deeper genealogy in evidence/history.

### 8.3 Entity labels

User banks must use a canonical user entity vocabulary so aliases do not create fragmented identities.

### 8.4 Mental-model input

Durable profile models read only consolidated eligible observations:

```yaml
fact_types:
  - observation

tags:
  - profile_eligible

tags_match: exact
```

This is enabled only after invalidation/refresh behavior is verified end-to-end.

### 8.5 Structured profiles

Use one structured mental model per durable bank kind before creating multiple specialized models.

Target shape:

```yaml
user_profile:
  interaction: []
  engineering: []
  preferences: []
  constraints: []

project_profile:
  architecture: []
  decisions: []
  workflow: []
  testing: []
  conventions: []
  gotchas: []
```

Project orientation (`name`, `locator`, `canonical_spec`, `purpose`) comes from Project Metadata, not from learned memory.

Split a profile into multiple models only if measurement shows a need for a different:

- refresh cadence;
- token/item budget;
- evidence scope;
- retrieval quality;
- refresh cost profile.

---

## 9. Storage Mapping

### 9.1 Hindsight mapping

| Field | Storage |
|---|---|
| `origin`, `kind`, `provenance`, `host` | memory metadata |
| `session_id`, `session_epoch`, `checkpoint_seq`, `slice_hash` | memory metadata |
| transcript slice key | `document_id` |
| `profile_eligible` or `evidence_only` | exactly one eligibility tag |
| `kind:<x>` | tag |
| observation scope | custom eligibility scope |
| host context | fixed Hindsight `context` |

Observation scopes:

```yaml
profile:
  - ["profile_eligible"]

evidence:
  - ["evidence_only"]
```

User id, session id and origin must never become Hindsight tags because they would fragment consolidation.

### 9.2 Reader behavior

- profile mental models and profile synthesis read `profile_eligible` observations only;
- `recall` may read all evidence and may filter by `kind:<x>` or `evidence_only`;
- `inferred` evidence remains recoverable through recall but structurally invisible to profile synthesis.

---

## 10. Working State Contract

### 10.1 Record

One record exists per `(user, project, workspace)`.

`workspace` is a deterministic identity derived from the current git worktree root when available. The current branch name is not workspace identity.

```yaml
objective:
current_direction:
recent_decisions:
open_questions:
next_steps:
updated_at:
session_id:
session_epoch:
checkpoint_seq:
```

### 10.2 Writers

Phase 2:

- `set_working_state(...)` is available only for explicit human handoff.

Phase 3 onward:

- the background pass is the canonical automatic writer;
- `set_working_state(...)` remains the explicit handoff/override path;
- agents do not update Working State on their own initiative outside the background pass.

### 10.3 Ordering

`session_epoch` is server-assigned, monotonic per `(user, project, workspace)` at session start.

`checkpoint_seq` is monotonic within a session.

Comparison is lexicographic:

```text
(session_epoch, checkpoint_seq)
```

Lower pairs cannot overwrite higher pairs.

Concurrent sessions that share one workspace are not merged in v1. The later `session_epoch` wins within that workspace, and the delivered context exposes the source session.

### 10.4 Staleness

Working State:

- does not expire silently;
- has no decay logic;
- is delivered with age;
- is context, not an agenda;
- must be checked against current repository state before acting on stale details.

---

## 11. Context Compiler and Delivery

### 11.1 Compiler inputs

The compiler resolves context from:

```text
host policy pointer
project metadata
user profile
project profile
Working State
workspace identity
host identity
context budget
```

Authority order:

```text
host policy
    ↓
project metadata
    ↓
durable memory
    ↓
Working State
```

Host policy is not copied into the memory payload.

### 11.2 Snapshot revision

Every compiled `(user, project, workspace)` snapshot has:

```yaml
brief_revision:
generated_at:
```

`brief_revision` is monotonic and increments when any compiler input changes, including:

- profile refresh;
- Working State update;
- project metadata edit.

Index and full tiers compiled from the same snapshot have the same revision.

A cached tier preserves the revision at which it was compiled.

When two tiers differ, the newer revision is authoritative for overlapping content. The older tier is supplemental only for content absent from the newer tier.

### 11.3 Index tier

Transport: MCP `instructions`.

Budget is host-specific.

For Claude Code:

```yaml
budget_chars: 1800
host_cap_chars: 2048
```

Unknown hosts use the smallest configured safe budget.

Contents, in order:

1. up to 5 high-impact User Profile items;
2. project orientation from Project Metadata;
3. one high-risk project constraint;
4. Working State headline: `objective`, `next_steps`, age;
5. one-line index of available memory sections.

Only callable surfaces that are safe and actually available are advertised.

Until the read-only recall redesign, `recall(scope, query)` may be named as gated/confirmation-requiring. Mental models are not advertised as directly callable until the MCP surface supports that path.

### 11.4 Full tier

Transport: SessionStart hook.

The Full tier is bounded context, not a dump of all profile content.

Initial provisional budget:

```yaml
max_tokens: 2500
```

When content exceeds the budget, the compiler keeps the highest-impact current items and leaves the remainder available through explicit recall.

Contents:

1. User Profile within its item budget;
2. Project Profile within its item budget;
3. gotchas with cause and artifact location where available;
4. full Working State with age and source session;
5. short consumer contract.

The SessionStart hook must:

- use a hard network timeout;
- keep a last-good disk cache;
- stamp cache age;
- fail open;
- never block session startup;
- exit successfully even when context fetch fails.

If live fetch fails:

- use last-good cache when available;
- make staleness visible.

If neither live context nor cache exists:

- deliver the index tier only;
- explicitly state that the full tier is unavailable.

### 11.5 API

Keep:

```http
GET /v1/session-brief
```

Add host-aware budget handling.

Do not rename to `/v1/context` until a second concrete consumer requires the abstraction.

Reads must not create models or other state.

---

## 12. Consumer Contract

The delivered full brief must include a compact version of these rules.

1. Use memory only when it changes a decision, check, question or action.
2. Do not narrate retrieval or restate memories merely to demonstrate recall.
3. Treat Working State as potentially stale context, not an instruction queue.
4. Preserve the level of the evidence; transient, observed or inferred behavior does not become a stable trait merely because it exists in history.
5. `inferred` content must not be treated as profile truth.
6. Re-observe time-sensitive `observed` findings before building critical behavior on them.
7. Host policy overrides profile memory.
8. Superseded history is not current truth.
9. When index and full tiers disagree, the newer `brief_revision` wins for overlapping state.
10. Explicit recall is for history, rationale and uncommon questions, not basic orientation.

---

## 13. Supersession and Lifecycle

### 13.1 Current truth vs history

Profiles represent current truth.

Evidence preserves historical truth and rationale.

When explicit human correction conflicts with an active profile item in the same routed scope, the correction becomes current truth for the next compiled snapshot. The superseded claim remains only as evidence/history where useful. A project-scoped correction cannot supersede User Profile truth, and a user-scoped correction cannot silently rewrite Project Profile truth.

Example:

```text
active project profile:
  PostgreSQL

evidence/history:
  DynamoDB decision
  PostgreSQL superseded DynamoDB because <reason>
```

A superseded choice remains in the active profile only when a concise "not X because Y" form prevents a likely future error.

### 13.2 Removal

If a profile item is no longer true:

- remove it from the active profile;
- keep source evidence/history where useful;
- do not preserve it as soft narrative such as "used to prefer".

### 13.3 Invalidation

Use Hindsight soft invalidation for reversible cleanup.

Automated lifecycle invalidation is deferred until data volume justifies it.

---

## 14. Security and Safety Requirements

1. Transcript sanitization runs locally before network transmission.
2. Master keys must never be surfaced or logged.
3. Console exposure requires SSO.
4. No `dry-run-refresh` endpoint is introduced.
5. Existing read-safety behavior is not weakened by simply flipping `readOnlyHint`.
6. A future truly read-only recall path requires a separate tool/API design that cannot create project state.
7. Read APIs must never create banks, models or project identities.

---

## 15. Required Operational Fixes

1. Fix master-key user resolution in `/v1/session-brief`.
2. Make Console Models request `detail=full`.
3. Remove `ensure_section` or any state-creation behavior from GET/read paths.
4. Canonicalize repository bank identity and retire the slug-only duplicate.
5. Add Project Metadata.
6. Add Working State storage and explicit handoff API/tool.
7. Add SessionStart full-context fetch with timeout and disk cache.
8. Add host-aware index-tier budgets.
9. Add narrow server-managed Hindsight bank configuration for missions, tags/scopes, entity labels and model trigger fields.
10. Preserve current auth/tenancy boundaries.

---

## 16. Quality Metrics and Acceptance Criteria

### 16.1 North-star metric

Quality is agent behavior, not memory volume or recall call count.

A fresh coding agent with months of history must:

- know durable project constraints;
- respect durable user working/review preferences;
- understand current Working State;
- see relevant operational gotchas;
- not revive superseded or rejected approaches;
- not treat temporary historical state as current truth;
- not leak personal preference into a shareable project context;
- use explicit recall only when deeper history or rationale is required.

### 16.2 Delivered-payload assertion

The regression test operates on the payload that the host actually receives, including cache behavior.

It must assert:

- user core is present;
- project orientation is present;
- at least a configurable minimum number of project gotchas are present;
- known colour/canary noise is absent;
- Working State is present with age and source session when available;
- fresh index and full tiers carry the same `brief_revision`;
- cached tiers expose their own revision and age;
- no `inferred` item appears in a profile;
- no bare `technical_claim` appears in a durable profile;
- an explicit human correction removes a conflicting prior item from current context in the next compiled snapshot only when both claims resolve to the same scope;
- a project-local override does not remove a compatible global User Profile preference;
- an explicit negative constraint remains negative after extraction and synthesis;
- index and full context are within their configured budgets;
- reads did not create state.

### 16.3 Behavioral suite

Start with the delivered-payload assertion.

Grow to approximately 10–20 curated real-world behavioral cases only when real failures justify them.

Each case must represent a failure that would materially affect an agent's work.

---

## 17. Implementation Plan

Each phase must be observable before the next phase changes memory quality upstream.

### Phase 0 — Data hygiene

1. Diff the 47-fact duplicate project bank against the canonical bank.
2. Re-retain only unique, relevant facts into the canonical bank.
3. Soft-invalidate the duplicate bank.
4. Soft-invalidate the 24 known colour/canary rows.
5. Delete the accidental `squall` brief model.
6. Invalidate the stale "Implement a disk cache" claim, refresh, and verify it disappears from the project model.
7. Audit the highest `proof_count` rows against distinct sessions/human utterances.

**Gate:** verify that invalidation + delta refresh produces the expected synthesized result.

### Phase 1 — Fix delivery

1. Fetch the full brief in SessionStart with timeout, last-good cache, age and fail-open behavior.
2. Implement Index and bounded Full delivery tiers.
3. Remove static host policy from the memory payload.
4. Fix master-key `/v1/session-brief`.
5. Fix Console Models full-content rendering.
6. Make all reads side-effect free.
7. Add Project Metadata.

**Gate:** verify that project context and user context actually reach the consuming agent.

### Phase 2 — Working State

1. Add one replace-on-write Working State record per `(user, project, workspace)`, deriving workspace identity from the git worktree root when available.
2. Add server-assigned `session_epoch`.
3. Add checkpoint ordering.
4. Add explicit `set_working_state(...)` handoff.
5. Include Working State and age in compiled context.
6. Add delivered-payload regression assertion.

**Gate:** verify explicit handoff appears correctly in delivered context and two independent worktrees do not overwrite each other's Working State.

### Phase 3 — Write quality

1. Implement Stop/PreCompact transcript checkpoint extraction.
2. Add local sanitization.
3. Add slice offsets, hash, `document_id` and caller `operation_id`.
4. Make the background pass the single semantic extractor.
5. File one-claim candidates into Hindsight.
6. Automatically rewrite Working State from the background pass.
7. Configure store-as-given retain mission.
8. Configure observation mission.
9. Configure eligibility tags and observation scopes.
10. Configure canonical entity labels.
11. Feed profile models from `profile_eligible` observations only after the Phase 0 invalidation probe passes.
12. Refresh the affected durable profile when an explicit human correction supersedes current truth in the same routed scope so the next compiled snapshot cannot serve the old item as current.
13. Enforce profile item budgets.
14. Keep explicit `retain` for deliberate memory requests.

**Gate:** after a session with no explicit handoff, Working State is written automatically and appears in delivered context without duplicate evidence; scope-fenced corrections and explicit negative constraints survive extraction with their intended semantics.

### Phase 4 — Structured profiles

1. Add one response schema for User Profile.
2. Add one response schema for Project Profile.
3. Compile delivered context from the structured profiles.
4. Audit nightly refresh cost before enabling the final schemas broadly.
5. Split profiles only on measured need.

**Gate:** structured profiles improve delivered-context quality without increasing noise or violating item budgets.

### Phase 5 — Long-tail recall

1. Design a truly read-only recall surface without state creation.
2. Expose deep history/evidence lookup safely.
3. Add reflect-style Q&A only where useful.
4. Evaluate success by behavior, not call frequency.

---

## 18. Open Technical Items

1. **Profile budgets:** `15` user / `25` project are provisional until delivered-context measurement.
2. **Codex host parity:** confirm which Codex hook/event provides equivalent transcript and checkpoint information before Phase 3.
3. **Store-as-given fidelity:** validate Hindsight extraction on at least ten one-claim candidates with `dry-run-extract`; if one item does not reliably become one fact, assert the stored item count through the memories API.
4. **Distinct-source importance:** if `proof_count` is inflated by repeated agent re-retains, compute distinct-source/session count in the harness instead.
5. **Full-tier budget:** `2500` tokens is provisional until delivered-context measurement validates or changes it.

---

## 19. Phase 2 Candidates

These are not v1 requirements. Implement them only when v1 behavior demonstrates the corresponding need.

### 19.1 Recovery checkpoints

If real sessions lose unprocessed transcript because Stop/PreCompact hooks do not run, add a checkpoint trigger based on unprocessed transcript size or another deterministic boundary.

The trigger must reuse the existing slice identity and the same background semantic extractor. It must not create a second extraction path.

### 19.2 Fresh handoff synchronization

If asynchronous extraction causes a newly started session to miss the immediately preceding Working State, add the smallest synchronization mechanism needed to make the latest completed checkpoint visible.

Do not expose unclassified candidate claims as active context and do not use Working State as a bypass around profile eligibility or consolidation.

### 19.3 Explainable profile items

If incorrect synthesized memories become difficult to debug operationally, give durable profile items stable identifiers and expose a read-only explanation path from a profile item to the evidence that supports or superseded it.

Do not add provenance detail to normal compiled context unless it changes agent behavior.

### 19.4 Context-aware profile compilation

If bounded Full context still causes irrelevant-memory anchoring or demonstrably omits relevant profile items, let the compiler distinguish a small always-delivered core from profile items that remain available through recall.

If static ordering remains insufficient, the compiler may use current workspace/session signals to rank non-core items for delivery. Do not add dynamic relevance machinery before delivered-context measurements show the need.

This is a compiler concern, not a new memory storage layer.

### 19.5 Memory-harm evaluation

If delivered-payload assertions and curated behavioral cases fail to catch regressions caused by memory itself, extend evaluation with explicit harm cases such as:

- stale-memory interference;
- superseded-choice revival;
- irrelevant-memory anchoring;
- unnecessary memory narration;
- asking again for information that durable memory should already provide.

Use A/B evaluation with and without memory only when needed to attribute the regression to memory rather than the base agent.

---
## 20. Explicitly Deferred

- direct `readOnlyHint` flip on the current recall design;
- automated lifecycle cleanup;
- engine migration;
- radical "manual notes only" memory mode;
- `CLAUDE.md` / `AGENTS.md` directive mirroring;
- `/v1/context` rename;
- standalone temporality field;
- agent-declared confidence or importance;
- multiple mental models per profile before structured profiles are measured;
- full provenance UI;
- general concurrent Working State merging within one workspace.

---

## 21. Final Target

The target operational loop is:

```text
session
  ↓
sanitized transcript checkpoint
  ↓
one semantic extractor
  ├─→ Working State
  └─→ classified evidence
        ↓
     Hindsight
        ↓
eligible observations
        ↓
structured user/project profiles
        ↓
context compiler
  ├─→ bounded MCP index
  └─→ cached SessionStart full context
        ↓
agent behavior
```

The system is successful when the next agent receives the right durable constraints, the right current state and the right historical affordances automatically, without duplicating repository truth or requiring routine manual recall.
