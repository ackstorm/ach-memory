# ach-memory v0.4.0 Product Specification

**Status:** Approved for implementation
**Date:** 2026-09-04
**Target release:** `0.4.0`  
**Supersedes for implementation:** `docs/specs/2026-08-29-memory-quality-v1.4.md` and the
earlier `0.4.0` drafts  
**Preserves as research evidence:** the Phase 5.5–5.9 results and all prior frozen artifacts

## 0. Changes from the previous `0.4.0` candidate

- removed transcript capture and the post-session extractor/router product path;
- made the agent skill the semantic boundary for exact, typed retain;
- added governed Hindsight mental models and explicit always-in-context delivery;
- added ACH expiry policy and exact delivery of active time-bounded claims;
- added bank/model currentness barriers with bounded repair;
- retained exact project/workspace Working State outside Hindsight.

## 1. Executive decision

ach-memory `0.4.0` is an identity, governance, delivery and memory-control plane for agents. It is
not a transcript recorder and it does not run a second semantic extraction pass after every agent
session.

The active agent decides when a durable claim has been established and submits that distilled
claim through `retain`. The agent proposes its semantic type and User/Project scope. ach-memory
authenticates the caller, authorizes the scope, resolves the opaque physical bank, sanitizes the
claim and its bounded evidence, records provenance, derives reserved metadata, and delegates memory
storage, consolidation and synthesis to Hindsight.

Hindsight remains the memory engine. ach-memory owns the boundaries Hindsight does not provide:

- user, group and project identity;
- User Bank and Project Bank separation;
- ownership and authorization;
- opaque, immutable bank resolution and project aliases/renames;
- pre-ingress sanitization;
- typed agent-submitted provenance;
- mental-model governance and context delivery;
- exact, ephemeral Working State for active project work;
- audit and product policy.

ACH is the harness Hindsight needs, not a competing memory engine. For ACH-mediated retain, the
agent supplies an already-distilled claim and the frozen no-LLM `ach-exact-v1` boundary stores it as
one source fact; neither ACH nor Hindsight runs a second semantic extraction pass. Hindsight remains
authoritative for source-fact storage, embeddings, recall, reflection, mental-model synthesis and
any derived observations, links or graph currentness. `ach-exact-v1` intentionally requests no
ingest-time entity extraction; ACH does not replace it. ACH MUST NOT reconstruct Hindsight semantics
in a parallel classifier, lineage evaluator, currentness graph or consolidation layer.

The normal write path is:

```text
human + active agent
        |
        | one distilled claim and minimal evidence
        v
ach-memory retain boundary
        | authenticate, authorize, sanitize, resolve, derive metadata
        v
one physical User or Project Bank
        |
        v
Hindsight storage, consolidation, recall and mental models
```

## 2. Product principles

1. Store durable conclusions, not conversations.
2. One retained item represents one independently correctable semantic claim.
3. The agent may retain proactively; a literal “remember this” command is not required.
4. A repository does not make personal information Project memory. Durable technical claims tied
   to the active project are Project; explicit personal facts and cross-project preferences are
   User; ambiguous ownership abstains.
5. User scope is reserved for explicitly personal information or stable cross-project preferences.
6. An agent never selects, sees or invents a physical `bank_id`.
7. Every retained claim has bounded, sanitized evidence.
8. Evidence basis is categorical and descriptive, not a numerical confidence score.
9. A mental model is a persisted Hindsight synthesis within one bank.
10. Loaded context may aggregate authorized model outputs, but does not synthesize across banks.
11. Working State is exact ephemeral state, not Hindsight memory.
12. Reads do not create projects, banks or mental models. Bootstrap is an explicit, auditable
    write.
13. Context loading fails open: memory unavailability must not prevent an agent session.
14. Every memory has server-recorded time and optional expiry. Expired, forgotten or revoked
    content is never delivered as current truth through ACH.
15. ACH provides durable, agent-mediated memory, not exhaustive conversation recall. A claim the
    agent never retains is not remembered.
16. ACH orchestrates Hindsight operations and exposes their state; it does not duplicate
    Hindsight's semantic currentness model.
17. No production data mutation is implied by shipping code or passing tests.
18. When ACH cannot prove the outcome of an upstream safety mutation, availability yields to
    currentness for that physical bank until reconciliation succeeds.

## 3. Release scope

### 3.1 Included in `0.4.0`

- activation only against Hindsight `0.9.2` or another version separately validated by the same
  dry-run and disposable-bank compatibility suite; an unvalidated version blocks activation;
- proactive, typed agent `retain` through REST and MCP;
- bounded evidence retained outside every Hindsight memory input;
- server-derived Hindsight tags and provenance metadata;
- server-recorded time and optional memory expiry with lazy, access-driven cleanup;
- existing User/Project bank resolution, ownership and authorization;
- local/server-side sanitization before Hindsight ingress;
- mental-model CRUD through REST and MCP;
- one versioned built-in User model and one versioned built-in Project model, automatically
  provisioned in their respective banks with `always_in_context=true`;
- one ACH built-in mental model outside quota and up to five ACH-registered custom mental models
  per bank;
- `always_in_context` as a mental-model delivery property;
- `uvx ach-memory context load` as the host-neutral context-loading command;
- `uvx ach-memory hook pre-compact` as a host-neutral reminder that captures no transcript;
- deterministic context aggregation of authorized User and active-Project model outputs;
- deterministic, bounded delivery of active time-bounded claims from the ACH ledger;
- deterministic Project Metadata orientation when an active project is resolved;
- explicit, agent-managed Working State for project/workspace continuity;
- idempotent retain and mental-model lifecycle behavior;
- a small, behavior-focused skill evaluation across at least two agent families;
- removal of automatic transcript capture and its runtime/deployment surface;
- a separately approved, one-off production-data cleanup after the new contracts are deployed.

### 3.2 Explicitly excluded from `0.4.0`

- automatic ingestion of complete transcripts;
- `Stop` or `PreCompact` transcript capture;
- a capture queue, capture worker, leases, cursors or transcript reconciliation;
- an ACH semantic extractor/classifier/router after a session;
- automatic Working State extraction from transcripts;
- the Phase 4 structured-profile compiler;
- profile ranking or displacement;
- INDEX/FULL dual delivery and its revision-consistency protocol;
- routine startup `reflect` calls;
- Knowledge Pages as a required delivery dependency;
- an extensible ACH model catalog, user-authored templates or arbitrary automatic model creation;
- an ACH semantic-currentness graph, causal-generation protocol or cross-document supersession
  saga;
- caller-scheduled future activation through `valid_from`;
- federated LLM reflection across multiple banks;
- generic Working State for non-project activities;
- a fixed topic/domain ontology;
- an `agent_inferred` basis or advisory-memory lifecycle;
- native bitemporal graph queries over Hindsight internals;
- production cleanup without separate, operation-by-operation approval.

## 4. Scope and identity

### 4.1 User Bank

A User Bank contains durable information about one person that may be useful across projects or
agent types: identity facts the person wants agents to know, communication preferences, stable
working preferences and other explicitly personal context.

It is private to the user under the existing tenant and credential rules. A master credential may
act on behalf of a user only through the existing explicit delegation and audit mechanism.

### 4.2 Project Bank

A Project Bank contains impersonal, shareable knowledge about one project: accepted decisions,
rationale, constraints, conventions, failure modes and non-obvious project history.

Project-specific information MUST NOT be retained in the User Bank merely because the current user
said it. Personal attribution MUST NOT be placed in shared project memory unless it is itself an
authorized, relevant project fact.

### 4.3 Project lifecycle

A project is an ACH domain resource with an immutable internal identity, a mutable slug, an owner
(`user|group`) and optional external identities such as a canonical Git locator.

Project slugs and aliases use one tenant-global logical namespace represented by a single table:

```text
project_slugs(tenant_id, slug, project_id, is_canonical)
UNIQUE (tenant_id, slug)
UNIQUE INDEX ON (tenant_id, project_id) WHERE is_canonical = true
```

A rename transaction first marks the prior canonical row as an alias and then inserts the new
canonical row; any slug collision aborts and rolls back the whole transaction. Old rows remain
resolvable aliases, so active names and renamed names cannot race through separate uniqueness
checks. Ownership transfer never changes project addressing. Authorization still hides an existing
unauthorized project; namespace uniqueness does not imply visibility.

Creation is allowed only through an authorized control-plane write:

- `POST /v1/projects` or `ach-memory project create` for direct REST/CLI clients;
- installed ACH MCP bootstrap with an explicitly configured logical project slug.

The MCP configuration is trusted product input. If it declares `project_slug="Pepe"`, ACH creates
or resolves project `Pepe`; it does not require a Git locator or a host hook to validate that
choice. A typo can therefore create a project and is repaired through rename or alias. That is an
accepted usability trade-off, not a claim that the server can distinguish typos from new projects.
Every MCP-bootstrap creation emits a structured warning and an audit event containing the logical
tenant, actor, slug and `creation_source=mcp_bootstrap`; it never logs a physical bank identifier.

An MCP-created project initially has `owner=user`, owned by the authenticated MCP user. If the slug
already resolves to a project that user may access, bootstrap reuses it. If it exists but the user
is unauthorized, bootstrap returns `404` and reveals no ownership or existence detail. Team sharing
begins only after an authorized control-plane operation transfers ownership to a group.

Ordinary `retain`, context, recall, list and get operations never create an unknown project. A
direct REST retain against an unknown slug returns `404`. Starting an MCP without project
configuration creates or resolves only the User resources.

Invalid or unauthenticated credentials fail MCP startup. After authentication succeeds, inability
to provision or reconcile the configured Project resources does not prevent the MCP server from
starting: User-scoped operations remain available, Project-scoped operations return the current
bootstrap error, and the next bootstrap retries. `load_context` omits the unavailable Project
section. `0.4.0` does not add a persistent bootstrap-error subsystem; existing operation status and
process diagnostics carry the failure.

### 4.4 Physical resolution

The public surfaces accept `scope="user"` or `scope="project"` plus the existing logical user,
project slug and/or canonical Git locator inputs. ach-memory resolves the physical bank internally.

Resolution precedence for Project scope is: explicit internal project identity, explicit project
slug, then a registered canonical Git locator. Resolution never falls through to creation.

The following guarantees remain normative:

- `bank_id` is opaque and never returned, logged or embedded in a public identifier;
- project rename aliases resolve to the same immutable bank;
- authorization occurs before any Hindsight access;
- a retry resolves to the same bank as the original call;
- no User content is written to a Project Bank and no Project content to a User Bank;
- a read never creates a project, bank or mental model.

## 5. Agent retain contract

### 5.1 When the agent retains

The always-loaded ach-memory skill MUST instruct the agent to retain a claim when it becomes a
durable fact that will plausibly matter in another session. This includes:

- accepted decisions and useful rationale;
- durable constraints and explicit negative rules;
- stable conventions;
- verified gotchas with a failure and cause or reproduction;
- explicit corrections and supersessions;
- stable cross-project user preferences;
- personal facts the user explicitly provides for future use.

The agent MUST NOT retain:

- every turn or a session summary by default;
- greetings or transient conversational details;
- permission to explore, test or implement;
- unaccepted proposals or intermediate plans;
- quoted, hypothetical, rejected or meta-conversational content as adopted truth;
- command output or repository facts that are cheap to rediscover;
- secrets or credentials;
- its own unverified conclusions or hypotheses;
- current task progress that belongs in Working State.

### 5.2 Scope decision

The agent proposes exactly one scope per call.

```text
durable technical/project claim in active project context -> Project
explicit personal fact or stable cross-project preference -> User
current incomplete work                                  -> Working State
ambiguous ownership, unverified or non-durable            -> abstain
```

Repository context alone never turns personal information into Project memory. For example, “I
always want trade-offs explained before an architecture change” is User when framed as a general
preference, Project when adopted as a project convention, and abstained when neither is clear.
Different scopes require different retain calls.

### 5.3 Request shape

REST and MCP expose the same semantic contract:

```json
{
  "scope": "project",
  "project_slug": "ach-memory",
  "content": "Project slugs may change, but the resolved bank identifier remains immutable.",
  "memory_type": "constraint",
  "basis": "human_explicit",
  "trigger": "agent_proactive",
  "valid_until": null,
  "evidence": [
    {
      "kind": "user_quote",
      "raw": "The bank id must remain stable when a project is renamed.",
      "source_ref": "conversation:current"
    }
  ],
  "operation_id": "4f6cbd5d-4f49-4fea-a679-3f913fb7d4bc"
}
```

The accepted response returns server-authoritative `recorded_at`, nullable `valid_until`, derived
lifecycle and operation status. The response never echoes raw evidence by default.

`content` is limited to 4 KiB UTF-8 after normalization and sanitization. This is intentionally a
single distilled claim, not a document or transcript. The bound is also part of the one-source-fact
guarantee in §5.7.

Closed fields:

```text
memory_type   = preference | constraint | decision | convention | fact | gotcha
basis         = human_explicit | agent_verified
trigger       = user_requested | agent_proactive
evidence.kind = user_quote | tool_result | artifact_excerpt
```

Meanings are closed as follows:

| Field | Value | Meaning |
|---|---|---|
| `memory_type` | `preference` | Defeasible guidance about what a person prefers |
| | `constraint` | A rule or limitation that must be obeyed while current |
| | `decision` | A choice accepted from competing possibilities, with rationale when useful |
| | `convention` | A stable way a user or project normally operates |
| | `fact` | A durable claim not better represented by another type |
| | `gotcha` | A non-obvious failure mode with its symptom and cause or reproduction |
| `basis` | `human_explicit` | The human directly stated or accepted the retained claim |
| | `agent_verified` | A tool result or authoritative artifact directly establishes the claim |
| `trigger` | `user_requested` | The human explicitly requested that this claim be remembered |
| | `agent_proactive` | The agent retained a qualifying claim without a separate remember command |

`basis` is descriptive provenance. ACH MUST NOT assign a global authority order between a human
statement and a verified observation: a human establishes their preferences and accepted
decisions, while live tools establish observable current state. The consumer rules in §10.3
resolve those categories. An unverified conclusion is not durable memory; the agent verifies it,
asks for confirmation, leaves it in Working State, or abstains.

For a `gotcha`, verification applies independently to each asserted part. A reproduced symptom
with a directly verified reproduction may be retained as `agent_verified` without a cause. A cause
may be included under `agent_verified` only when tool output or an authoritative artifact directly
establishes it. A merely plausible causal explanation is omitted, or is retained later as
`human_explicit` only after the human deliberately adopts it as project knowledge; casual agreement
such as “probably” is not acceptance. A changed causal explanation uses the ordinary correction
contract.

`constraint` is imperative while current and `preference` is defeasible guidance. `trigger`
records why the tool was called; it does not alter truth or priority. Every `memory_type` MUST have
at least one real consumer in a mental-model prompt, source selection or skill rule before release.

### 5.4 Recorded time and expiry

The final ACH record has:

| Field | Authority | Meaning |
|---|---|---|
| `recorded_at` | Server generated, immutable | When ACH accepted the claim and when it becomes current |
| `valid_until` | Optional caller value | First instant at which the claim no longer applies; `null` or omission means no known expiry |

`valid_until` is an RFC 3339 timestamp with an explicit offset and is stored canonically in UTC. It
must be later than `recorded_at`; retain rejects an already expired value. ACH may store an internal
`valid_from=recorded_at` for forward schema compatibility, but callers cannot set it in `0.4.0`.
Future activation, scheduling and arbitrary bitemporal queries are out of scope.

Valid time is not event time. “The trip is 10–15 September” describes an event; “do not suggest
travel until 15 September” limits the applicability of a preference. Hindsight event dates do not
implement ACH expiry.

The agent or host resolves relative expiry expressions using the interactive user's current
session timezone and sends an exact timestamp with an RFC 3339 offset. “Host-reported local” means
that user/session timezone, never the ACH server or container timezone; a remote agent running in
UTC must not substitute its process timezone for the human's. The server validates the timestamp
and does not maintain a persistent user-timezone attribute in `0.4.0`. The agent preserves the
original wording in `evidence.raw` and asks one concise question when the session timezone is
unavailable or a material boundary remains ambiguous. The API does not accept unresolved
natural-language dates. Apparently durable claims omit `valid_until`; the agent does not ask the
user to invent an expiry.

A future-effective preference or constraint cannot be made active merely by mentioning its start
date in `content`. For example, “From next Monday, do not schedule meetings” is not retainable as an
active constraint in `0.4.0`. The agent explains the limitation and retains it only when it becomes
current. Support for future `valid_from` requires a separately specified activation consumer.

Lifecycle at time `now` is derived as:

```text
active     valid_until is null or now < valid_until
expired    valid_until is not null and now >= valid_until
```

`forgotten` and hard-deleted are separate lifecycle outcomes. Expiry removes a claim from current
recall but preserves authorized ACH history. Erasure uses hard delete; revocation uses `forget`.

### 5.5 Evidence

Every retain request contains between one and four evidence items. Each `raw` value is a minimal
verbatim excerpt, not a model-authored explanation:

- one decisive user sentence;
- one error or test-result line;
- one small authoritative artifact excerpt.

Each evidence item is limited to 1 KiB UTF-8 and all evidence in one request is limited to 4 KiB.
`source_ref` is optional, bounded and MUST NOT contain credentials. Complete transcripts, complete
files and unbounded logs are rejected.

The canonical claim and every evidence field are sanitized independently. Canonical `content` may
only be transformed mechanically by Unicode NFC normalization, CRLF-to-LF normalization,
collapsing runs of horizontal whitespace and stripping trailing whitespace. If it contains a
detected secret or any other sanitizer rule would alter it, retain fails with
`content_rejected_by_sanitizer`; ACH never persists the altered text as though it were the
submitted claim. Evidence is provenance rather than canonical memory, so individual evidence
items may be redacted or rejected independently, provided at least one surviving item remains
meaningful. A request is rejected if that minimum is not met after sanitization.

Evidence is provenance, not memory. It remains in the ACH ledger and MUST NOT be stored in
Hindsight `metadata`, `context`, tags or document text, so it cannot leak into recall, reflection,
mental-model grounding or a future upstream strategy, and so ACH can audit, correct or delete the
source record without promoting supporting text into durable knowledge.

ach-memory stores the sanitized canonical claim and its evidence in its own PostgreSQL provenance
record, linked by tenant, logical scope, `operation_id` and `document_id`. The row also carries the
canonical payload hash, `recorded_at`, nullable `valid_until`, upstream submission state, lifecycle
timestamps, `trigger` and calling-agent identity when known. Keeping the bounded sanitized claim
is required for idempotency, audit, hard delete and exact delivery of active time-bounded claims;
it does not authorize ACH to extract, consolidate or semantically search it. This is the ACH source
of truth for idempotency, audit and expiry eligibility. It is not a transcript queue and never runs
semantic extraction. Its only temporal maintenance is the bounded expiry hand-off in §6.4, invoked
by an authorized access or explicit maintenance request. An exact retry resolves the same row; a
different payload at the same operation identity is a conflict.
Normal data-plane responses do not expose the verbatim evidence unless an authorized
curation/audit operation explicitly requests it. Forgetting a memory marks its evidence forgotten
and removes it from normal reads; an authorized hard-delete operation purges both.

### 5.6 Server-derived Hindsight representation

Only `content` is submitted to Hindsight as the claim text. ach-memory derives, and callers cannot
override, the following tags:

```text
type:<memory_type>
basis:<basis>
schema:ach-retain-v1
validity:indefinite | validity:expiring
```

`trigger`, evidence and temporal control fields remain in ACH. They are never placed in Hindsight
document text, context or extraction metadata. ACH may add a reserved opaque lineage identifier
needed to reconcile a Hindsight document with its provenance row; callers cannot set it.

ACH-created mental models select the `schema:ach-retain-v1` namespace by default, so unknown or
legacy upstream documents are not silently incorporated. A caller may change the source selection
explicitly through the authorized mental-model contract; `always_in_context` never changes which
documents feed a model. The schema tag is constant across ACH claims and therefore does not
fragment consolidation by trigger or temporal state.

No caller-supplied privileged tag, bank identifier, ownership field or promotion flag is accepted.
No `profile_eligible` field exists in the `0.4.0` public contract.

### 5.7 Atomicity, document identity and splitting

- One call contains one independently correctable claim.
- An accepted decision and its inseparable rationale may share one claim.
- Unrelated claims, independently correctable facts and different scopes use separate calls.
- A negative rule remains negative.
- A gotcha states the observable failure and either its cause or a reproduction.
- Retained content is written in English while the deployed reranker remains English-only.

The agent has already distilled the semantic claim. Every ACH-mediated retain therefore selects
the ACH-owned named Hindsight strategy `ach-exact-v1`; callers cannot override it. Its complete
Hindsight 0.9.2 override is frozen as:

```yaml
retain_strategies:
  ach-exact-v1:
    retain_extraction_mode: chunks
    retain_chunk_size: 4096
    retain_structured_chunk_size: 4096
```

Hindsight's `chunks` mode makes no extraction-model call and stores each chunk verbatim as one
`world` source fact with no extracted entities. Because sanitized `content` is at most 4 KiB UTF-8,
it is also at most 4,096 Unicode code points and cannot split under this strategy. Bootstrap and
every ACH-mediated retain MUST idempotently verify the resolved named strategy and install the
exact definition when absent, preserving unrelated bank strategies. A failed post-write
verification aborts retain; ACH MUST NOT silently fall back to a bank default or to Hindsight
`verbatim` mode.

Activation and upgrades MUST combine a read-only preflight of the Hindsight version and resolved
bank configuration with a synchronous live retain in a disposable bank proving one exact fact,
zero extraction tokens, zero ingest-time entities and no second chunk. Hindsight 0.9.2's
`dry-run-extract` response does not expose chunks or entities, and its per-call chunks override is
not behaviorally equivalent to the installed named strategy on the validated deployment; it is
therefore not accepted as proof of this contract. A change in an upstream release blocks
activation until the read-only and behavioral checks are revalidated.

This deliberately trades ingest-time entity structure for exactness and zero extraction-model
hallucination. Semantic retrieval initially relies on Hindsight embeddings and reranking over the
exact source facts. Hindsight may later derive observations and links during consolidation, but ACH
does not require one observation per retained claim and does not manufacture missing graph
structure. The scale-recall and consolidation/curation gates in §14 verify the upstream behavior on
which `0.4.0` actually depends.

One ACH item maps to one stable Hindsight `document_id` and one source-memory ID, both recorded in
the provenance row. Hindsight owns embeddings, consolidation and any later observations, links or
derived graph state. ACH does not independently extract entities, reclassify the fact or calculate
graph currentness.

### 5.8 Corrections and replacement

`correct` repairs one independently correctable claim while preserving its stable `document_id`
and source-memory ID. ACH appends an audit revision and delegates the fact edit to Hindsight's
curation API. Hindsight owns re-embedding, removal of affected observations and links, and
reconsolidation. Every `correct` request carries a caller-visible UUID `operation_id`; MCP creates
one before transport when omitted by the agent and reuses an explicitly supplied value. Repeating
the same operation ID with the same target and canonical content is an exact retry, while reuse
with a different target or content returns `409`. Distinct corrections require distinct IDs even
when content oscillates A→B→A→B, so every overwritten canonical value receives its own immutable
revision. A clerical `valid_until` change within the same indefinite/expiring class updates
the ACH ledger; changing that class, semantic type or scope requires a new retain and forget.

If the new statement is a separate claim rather than a correction, the agent uses a new `retain`
and an explicit `forget` of the old claim. `0.4.0` does not promise an atomic transaction across two
independent documents and implements no cross-document saga. The public retain contract has no
`supersedes_document_id`.

`forget` delegates reversible invalidation of the recorded source-memory ID to Hindsight and marks
the ACH provenance row forgotten. `restore` reverses both through Hindsight's curation API. Hard
delete removes the Hindsight document and purges its ACH claim/evidence record. ACH orchestrates
these lifecycle calls but does not reproduce their graph effects.

Restore re-evaluates expiry at the server clock. A forgotten claim with `valid_until > now` or no
expiry may become active again through Hindsight restore. If `valid_until <= now`, restore removes
the forgotten marker from ACH history but the resulting lifecycle is `expired` and the Hindsight
source remains invalidated. Restore never resurrects an expired claim into current recall or
context.

ACH changes its authoritative lifecycle row only after it can prove the corresponding Hindsight
outcome. If a correction or forget may have committed upstream but its response is lost or
otherwise indeterminate, ACH records `curation_state=unknown` and marks that physical bank's
`currentness_state=withheld`. It then reconciles by inspecting the stable source-memory identity
and safely repeating the same desired mutation where Hindsight's contract permits. Until the
desired upstream state is proven, every ordinary current read for that bank is withheld. Audit,
operation-status and reconciliation surfaces remain available. This is a bounded safety state, not
a cross-document replacement saga or an ACH graph-currentness implementation.

Reconciliation has explicit terminal rules:

- if inspection proves the desired state, ACH completes the operation without repeating it;
- after ACH has resolved and authorized the physical bank, a target-level `404` for `forget`,
  expiry invalidation or hard delete proves the source cannot be active, so the desired safety
  state is satisfied and the corresponding ACH lifecycle transition completes;
- a `404` for `correct` or `restore` cannot prove the requested present-state content, so ACH marks
  `curation_state=needs_operator`, keeps the bank currentness barrier and stops automatic retries;
- if inspection proves a conflicting state and the same mutation is safely repeatable, ACH repeats
  it idempotently; otherwise it uses the same `needs_operator` terminal state.

`needs_operator` is terminal for automatic repair, not silent success. Maintenance status exposes
the logical operation and remediation class without raw content or physical bank identifiers.

The skill MUST search before a correction, contradiction or likely duplicate. It MUST NOT require
a recall before every ordinary retain.

## 6. Retain reliability

### 6.1 Idempotency identity

Every asynchronous write has a caller-visible UUID `operation_id`. The MCP adapter generates it
before sending the request and reuses it for transport retries. Direct REST clients MUST provide
it. Reusing the same ID with a different canonical payload returns `409`.

A stable `document_id` is generated from the submission identity when the caller does not provide
one. An identical retry cannot create a second document or provenance record.

`0.4.0` does not treat normalized content as record identity and does not promise suppression when
an agent submits an equivalent claim under a new `operation_id`. The skill searches before a likely
duplicate; exact-current-claim fingerprinting remains evidence-gated future work in §15.6.

### 6.2 Async and sync behavior

`retain` submits an active claim as an asynchronous Hindsight operation and returns only after
Hindsight has accepted its operation identity. Future-effective retain is rejected; there is no
scheduled submission lifecycle. The caller may inspect an accepted operation with `get_operation`.

`sync_retain` is the same idempotent submission followed by polling for at most 30 seconds until
searchable or terminal. It MUST NOT use an upstream synchronous path that ignores `operation_id`.
If the deadline expires after upstream acceptance, ACH returns the accepted `operation_id` with
`status=pending`; it does not reinterpret an accepted asynchronous write as failed.

Use `retain` normally. Use `sync_retain` only when the immediately following action depends on the
new memory being searchable.

### 6.3 Guarantee boundary

```text
before Hindsight accepts operation_id -> no eventual-delivery guarantee
after Hindsight accepts operation_id  -> durable and observable in Hindsight
```

ach-memory has no retain outbox, background replay worker, scheduled activation or transcript
fallback. Before upstream ACK, the client retries with the same `operation_id`. After ACK, the
client observes or explicitly retries the upstream operation through the supported operation
lifecycle.

ach-memory MUST NOT report success without a Hindsight-accepted operation. Timeouts, `429`, backend
unavailability, conflicts and terminal failures remain distinguishable. A failure after project
resolution MUST NOT orphan a Hindsight bank: the logical resolver is committed before ingress and
a retry reaches the same bank.

### 6.4 Hindsight-owned currentness and lazy expiry

Hindsight 0.9.2 owns currentness of its facts, observations, graph and mental-model grounding. Its
curation path removes invalidated facts from active recall, deletes dependent observations and
triggers reconsolidation and retraction-aware mental-model refresh. ACH verifies that upstream
contract with a live test and does not reproduce it with lineage filtering, bank generations or a
parallel currentness graph.

These guarantees apply to writes mediated by ACH. Out-of-band mutation of an ACH-owned Hindsight
bank is an unsupported operator action; ACH does not attempt to discover and repair arbitrary
changes made behind its control plane.

ACH adds only the expiry Hindsight does not provide. Claims with `valid_until` receive
`validity:expiring`; claims without it receive `validity:indefinite`. Every persisted mental model,
built-in or user-defined, MUST select only `validity:indefinite` in `0.4.0`. Expiring claims remain
available through task-specific `recall` and `reflect` while active, but never feed a persisted
synthesis. This restriction is what keeps expiry from creating a second mental-model currentness
system inside ACH.

Before a `recall` or `reflect` operation, ACH checks for due source-memory IDs in the requested
logical bank. One access claims at most 32 rows, ordered by `valid_until`, then `recorded_at`, then
stable `document_id`, and invalidates that batch through Hindsight's reversible curation API. An
upstream `404` satisfies expiry because an absent source cannot be active. If another batch outcome
is unknown, or more due rows remain, ACH withholds the affected current read and returns
machine-readable maintenance status. The next authorized access continues idempotently with at
most one further batch. Hindsight 0.9.2 has no source-memory-ID exclusion contract for `reflect`;
ACH therefore does not claim that post-filtering raw recall hits makes derived observations or
reflection safe. No daemon, schedule, lineage evaluator or background reconciler is introduced,
and no work runs merely because wall clock time passed.

The public maintenance states used by these paths are
`expiry_cleanup_pending`, `upstream_outcome_unknown`, `model_refresh_pending` and
`model_refresh_failed`, plus terminal `curation_needs_operator`; responses may add diagnostic
operation identifiers but never raw content or physical bank identifiers.

Safety-relevant correction or invalidation of an indefinite claim can affect a persisted model.
Hindsight's curation call is synchronous, but a transport failure can leave its outcome unknown.
After ACH proves the desired upstream state, it identifies affected registered models from their
static source tags and tag expression. In `0.4.0`, every persisted model whose static selection
admits the changed source is affected. If exclusion cannot be proven from that static selection,
the model is treated as affected; semantic wording in `source_query` is never proof of exclusion.
Given the bounded maximum of six ACH-governed models per bank, correctness wins over refresh
minimization. ACH requests an explicit refresh for every affected model and tracks each operation.

The model registry has only `delivery_state=ready|withheld`. A withheld model records the exact
refresh operation identity and `pending|failed` status. `load_context` and ordinary model get
return content only when ready. While a model is withheld, `reflect` excludes it through
Hindsight's supported exclusion option. Exact operation state, rather than timestamps or a new
causal-generation system, is the proof of progress.

The common currentness barrier is therefore:

```text
upstream safety mutation outcome unknown -> withhold current reads for the physical bank
upstream mutation proven, refresh pending -> permit safe bank reads but withhold affected models
all affected refresh operations succeeded -> affected models become ready
```

While the bank barrier is active, ACH neither starts a user-requested model refresh nor accepts an
automatic refresh result as proof of currentness. Reconciliation of the upstream safety mutation
must complete first; only refresh operations requested against that proven state may make a model
ready.

Recovery is access-driven and rate-bounded. An authorized `bootstrap`, `recall`, `reflect` or
`load_context` against a withheld bank may, when its repair backoff has elapsed, claim at most one
repair step for that bank. Bank-state reconciliation takes precedence over model repair. A
successful bank-reconciliation step may enqueue its bounded initial affected-model set—at most the
six ACH-governed models—without further confirmation because those refreshes maintain the safety
mutation that was already authorized. A terminal refresh failure sets
`repair_not_before=now+60 seconds`; the next eligible access may enqueue one retry for the oldest
failed affected model. Each failed retry resets the same backoff. No access waits for a model
refresh to finish, performs more than one repair action, or enters a retry loop.

The 32-row cleanup bound and 60-second repair backoff are fixed internal `0.4.0` operational
constants, not public API compatibility promises or operator-tunable product settings. A later
release may tune them only with the same bounded-work and currentness tests; the five-custom-model
quota and delivery token ceilings remain separate product contracts.

`load_context` remains fail-open at the session boundary: it omits a withheld bank or model and
returns the other authorized, provably current sections with machine-readable omission status. It
never turns an unknown upstream outcome into current content.

## 7. Mental models as a governed product surface

### 7.1 Primitive

A mental model is one persisted Hindsight synthesis built from one physical bank. REST and MCP
both expose authorized create, list, get, update, refresh and delete operations.

Creation accepts:

```json
{
  "scope": "user",
  "name": "travel-preferences",
  "source_query": "Describe this user's durable travel preferences.",
  "source_tags": ["schema:ach-retain-v1", "validity:indefinite"],
  "tags_match": "all",
  "max_tokens": 400,
  "always_in_context": false,
  "trigger": {
    "mode": "delta",
    "refresh_after_consolidation": true,
    "min_refresh_interval_seconds": 300
  }
}
```

The example is user-defined. Its name and prompt are caller-authored and bounded by the existing
content-size limit. `always_in_context` is required on creation so delivery is a conscious choice;
it is never inferred from the name, prompt or scope. Every model must select
`validity:indefinite`; ACH rejects a persisted model that can consume expiring claims. Hindsight
trigger fields are exposed only through the validated subset supported by the trusted client
boundary. Mutation tools carry host confirmation and normal write authorization.

User-created models receive an immutable server-generated ACH `model_key`; name remains mutable.

`memory_type` describes how a claim should be consumed; it is not a topic ontology. A mental
model's `source_query` selects a domain such as collaboration, engineering or travel semantically.
`0.4.0` adds controlled domain tags only if product evaluation demonstrates unacceptable
cross-domain contamination.

### 7.2 ACH registry

`origin`, built-in definition version, `always_in_context`, refresh status and the five-custom-model
quota are ACH product metadata, not Hindsight memory tags. Hindsight mental-model tags and source
queries scope which memories feed a model and MUST NOT be reused as delivery labels.

ach-memory maintains a registry keyed by tenant, logical bank and stable model key. It records:

- upstream mental-model ID;
- logical name/key;
- logical scope and bank reference;
- `origin=builtin|user`;
- built-in key and definition version when `origin=builtin`;
- source query, source selection and validated Hindsight options;
- `always_in_context`;
- declared `max_tokens` and trigger policy;
- last successful refresh timestamp for observability;
- delivery state and exact blocking refresh operation details;
- lifecycle timestamps.

Upstream IDs and physical bank IDs remain implementation details. Public callers address a model
by logical scope and model key.

The upstream mental-model NAME Hindsight stores is the stable internal locator `ach:{model_key}`,
never the caller-authored display `name`: that display name is mutable ACH product metadata and
lives only in the registry and the public `MentalModelView`. This split is what makes crash
recovery possible after a create's Hindsight response is lost -- ACH lists upstream models by the
exact internal locator and adopts only an exact match (§7.3) -- and it is also why an update to the
display name alone never calls Hindsight at all. Public responses and logs still never expose the
upstream mental-model ID itself, only the ACH `model_key`.

### 7.3 Limit

Each bank may register one built-in plus five custom models. The built-in is outside the custom
quota, so a normally bootstrapped User or Project Bank may contain six ACH-governed models. Delivery
selection must fit the independent User/Project budgets. The custom quota is a `0.4.0` product
contract, not an operator-tunable default.

The five-custom quota is enforced transactionally in the ACH registry. Concurrent ACH creates
cannot exceed it or produce duplicate logical keys. ACH inventories and reports unknown upstream
models but never silently adopts, mutates or counts them as ACH-registered custom models.
Out-of-band creation is unsupported and ACH does not claim transactional exclusion against an
external writer. Before an ACH create, an upstream logical-key collision or independent Hindsight
resource limit causes a clear conflict rather than deletion of another model.

### 7.4 Closed built-in models and context selection

`0.4.0` ships exactly two compiled built-in definitions:

| Key | Instantiated in | Delivery | Purpose |
|---|---|---|---|
| `user-context` | Each User Bank | `always_in_context=true` | Stable personal identity, relationships, preferences, constraints, conventions, facts and accepted decisions useful across agents |
| `project-context` | Each Project Bank | `always_in_context=true` | Durable project decisions, constraints, conventions, facts and gotchas that are not cheap to rediscover |

Both select `schema:ach-retain-v1` AND `validity:indefinite` with all-tag matching. Their prompts
distinguish the six public `memory_type` values where relevant and preserve the difference between
human statements and tool-verified observations without inventing a global authority score. This
gives every public type a concrete consumer and a behavioral test.

Built-in `max_tokens` are 512 for `user-context` and 1,024 for `project-context`: the compiled
definitions set `max_tokens=512` and `max_tokens=1024`, respectively.

The initial built-in definition version is `1`. Both request delta refresh after consolidation with
a five-minute minimum interval. Additive retains may therefore make a ready model temporarily
incomplete, but never unsafe; Hindsight owns that normal refresh lifecycle.

#### 7.4.1 Frozen version-1 definitions

The following source queries and token budgets are immutable in `0.4.0`:

**user-context (version 1)**
- **source_query:** "Summarize durable user context that an authorized agent should always know. Include explicitly retained identity, relationships, preferences, constraints, conventions, facts, and accepted decisions useful across agents. Distinguish imperative constraints from defeasible preferences and preserve whether support is human-explicit or agent-verified. Omit project-specific material, speculation, transient work, secrets, unsupported inference, and duplicate statements. Present only current indefinite knowledge."
- **max_tokens:** 512
- **source_tags:** `schema:ach-retain-v1`, `validity:indefinite` (all-tag matching)
- **trigger:** delta refresh, refresh after consolidation, 300-second minimum interval

**project-context (version 1)**
- **source_query:** "Summarize durable, impersonal project context that is not cheaply rediscoverable from the repository: accepted decisions and useful rationale, constraints, conventions, non-obvious facts and history, and verified gotchas. Preserve prohibitions and distinguish rejected or superseded alternatives from active decisions. Omit personal user context, current task progress, secrets, unsupported inference, duplicate statements, and cheap source-code facts. Present only current indefinite knowledge."
- **max_tokens:** 1,024
- **source_tags:** `schema:ach-retain-v1`, `validity:indefinite` (all-tag matching)
- **trigger:** delta refresh, refresh after consolidation, 300-second minimum interval

Built-in prompts are ACH-owned and cannot be edited through ordinary model CRUD. Their definitions
carry a version and may be updated by an ACH release. This is a closed two-definition mechanism,
not a user-extensible catalog, inheritance system or general template framework. An upgrade changes
only the prompt/source definition and requests a new Hindsight refresh; it preserves the user's
delivery choice and never re-enables a disabled `always_in_context` flag.

An updated built-in is withheld from current delivery from the moment its new definition is
accepted until that model's exact refresh operation succeeds. A release that changes a built-in
definition MUST call out this temporary first-bootstrap omission in its release notes; deployment
must not describe the old output as a last-good fallback.

Bootstrap creates built-ins by default. Configuration may opt out before creation. After creation,
the authorized user may set `always_in_context=false`; the model remains registered and future
definition upgrades do not reverse that choice. A built-in does not consume one of the five custom
slots and cannot be deleted while enabled as a built-in. Disabling its built-in lifecycle is an
explicit, confirmed operation and prevents recreation.

A user or agent may create additional user-defined models through the authorized REST or MCP
operation and supplies their name, prompt, source selection, refresh policy, budget and
`always_in_context` value.

`always_in_context` filters model outputs for standing context delivery. It does not filter the
memories inside a model, change the model's prompt, confer greater truth or priority, or create a
new kind of mental model. A model with the flag set to false remains available through ordinary
list, get, refresh and task-specific use.

Different consumers may create different additional models. ACH assigns no semantics to their
names. The creator is responsible for their domain and purpose; ACH enforces scope, authorization,
expiry eligibility and delivery policy.

For a user-defined model, the creator chooses manual refresh or a supported automatic trigger.
Manual refresh is represented by an empty `trigger` object in the ACH contract; ACH omits the
trigger on upstream creation because Hindsight 0.9.2 has no literal `manual` mode. The registry
stores the empty object and crash recovery treats Hindsight's returned inactive default trigger as
the same policy. Changing an automatic model to manual explicitly clears both upstream automatic
refresh mechanisms; an empty trigger PATCH alone is not sufficient because Hindsight applies
trigger updates field by field. `mode: manual` is invalid and rejected before an upstream request.
Automatic delta refresh may use a minimum interval to coalesce bursts of retains. Correction,
forget, restore and hard delete of indefinite sources bypass that interval: an affected model is
withheld while its explicit Hindsight refresh operation is pending or failed. Authorization and
delivery-selection changes follow §8.6 and do not make model content semantically stale.

### 7.5 Bootstrap

Automatic bootstrap is enabled by default for installed MCP integrations and may be disabled by
configuration. The CLI exposes an equivalent explicit opt-out. No credential is accepted through
argv.

Bootstrap is an explicit, idempotent, auditable control-plane write operation:

1. At MCP/user initialization, ensure the User Bank and its enabled `user-context` built-in.
2. When the authenticated MCP configuration includes a logical `project_slug`, create or resolve
   that project, ensure its Project Bank and ensure its enabled `project-context` built-in.
3. Reconcile a built-in only when its compiled definition version changed; never alter a
   user-defined model.
4. Request built-in synthesis asynchronously and expose its operation status.
5. Do nothing when bootstrap or the relevant built-in is disabled.

The bootstrap authorization covers these subordinate built-in creates and upgrades; an installed
MCP does not ask for a second host confirmation for each model. User-defined model mutations retain
the normal confirmation policy.

Ordinary list and get endpoints never create or reconcile models. Context loading never provisions
or updates definitions; it has only the narrow safety-repair exception in §6.4. A first context
load may therefore omit a newly bootstrapped built-in until its asynchronous synthesis completes.
If no model is marked `always_in_context`, context loading returns only other applicable sections
such as Active Time-Bounded Claims, Project Metadata and Working State, or an empty context when
none apply.

The REST boundary separates the mutation and read explicitly:

```text
POST /v1/bootstrap       # idempotent MCP/user/project provisioning
POST /v1/context/load    # context assembly; may repair a prior safety mutation
```

The MCP server performs bootstrap once at initialization when enabled. Context loading never
creates a project, bank or model.

## 8. Always-in-context delivery

### 8.1 Meaning

`always_in_context=true` means:

> Whenever an authorized consumer asks ACH to load its standing context, include this mental
> model. If a project is supplied, also include that project's selected models.

It does not mean “inject on every prompt” and it is not specific to coding agents. Selection is a
conscious user/configuration choice, never inferred from a model's name or prompt. A User model may
contain identity, family or location facts if the user deliberately wants every authorized agent
acting as them to receive that information. Enabling the flag on a User model returns
`delivery_exposure=all_authorized_user_consumers`. Interactive control-plane clients and hosts MUST
surface that exposure before confirming the mutation; ordinary reads need not repeat the warning.

### 8.2 Host commands

The consumer-neutral entry point is:

```bash
uvx ach-memory context load
uvx ach-memory hook pre-compact
```

`ach-memory mcp` continues to mean “run the stdio MCP server.” `load_context` accepts optional
`project_slug` and optional `workspace_id`; `workspace_id` is valid only with `project_slug`. Callers
do not enumerate model names; ACH selects every authorized model marked `always_in_context=true`.
Models retain stable keys for CRUD, provenance and labelled output.

Claude Code, OpenCode or another host may invoke the same command from a lifecycle event, but that
event is adapter plumbing rather than the ACH API. A host without such an event can have its agent
call `load_context`; lack of a hook never enables polling or transcript capture as a substitute.

### 8.3 Context-loading flow

```text
load_context(project_slug?, workspace_id?)
  -> resolve authenticated user
  -> resolve the supplied existing project/workspace when available
  -> list ready always-in-context User models
  -> list ready always-in-context Project models
  -> fetch all selected User and Project model outputs concurrently
  -> select active time-bounded User/Project claims from the ACH ledger
  -> omit pending, failed or unavailable model outputs
  -> add deterministic Project Metadata when a project is active
  -> append current Working State when available
  -> return deterministic, labelled context
```

The order is User models by logical key, Project Metadata, Project models by logical key, Active
Time-Bounded Claims, then Working State. Scope and model names are visible headings. Project
Metadata contains only the stored name, purpose and canonical-spec pointer; it is never synthesized
and the pointer is never fetched implicitly. `load_context` performs no relevance `reflect`,
semantic ranking or expiry cleanup. It never mixes model source documents between banks. Its only
permitted write is the bounded, backoff-gated repair of a previously authorized safety mutation or
required model refresh defined in §6.4; it never creates a project, bank, model, claim or new
semantic mutation.

After authorization and registry selection, all selected model-output reads are issued concurrently
and settle independently; deterministic ordering is applied only when assembling the response. The
maximum legal selected set is nine with Hindsight's 256-token minimum, eight in the default
two-built-in configuration. One slow or failed read does not cancel successful peers, although every
read remains subject to the single end-to-end deadline.

`Active Time-Bounded Claims` is the one deterministic delivery surface for active
`validity:expiring` claims. It reads the already-authorized ACH ledger, includes the exact sanitized
claim rather than a synthesis, and selects only rows for which `recorded_at <= now < valid_until`
and upstream retain/curation state is proven current. User and active-project entries are labelled
separately. Entries are ordered by `valid_until`, then `recorded_at`, then stable `document_id`; they
are included whole until the section budget is exhausted, followed by a count of omitted active
entries when necessary. This fixed ordering is not semantic ranking or displacement. If a bank's
currentness is withheld, its entries are omitted with the same machine-readable bank omission.
Expired claims are never emitted. The section exists because expiry is ACH policy; it does not make
the ledger a search or synthesis engine.

The 256-token section is deliberately a small standing-orientation surface, not a personal agenda
or a substitute for recall. Its omission marker tells the consumer how many active claims remain
and to use task-specific `recall` when the current task may depend on them. `0.4.0` does not enlarge
the global context envelope without measured product need.

The operation has a two-second hard end-to-end deadline and fails open for agent startup:
unavailable, unauthorized or stale model output is omitted rather than replayed. A permitted repair
is only enqueued inside that deadline; `load_context` never waits for it to finish. `0.4.0` has no
persistent last-good context cache. Model content is included whole, never cut mid-line. Selection
is never silently changed by an allocator; enabling a flag that would exceed the separately
configured User or Project budget is rejected. The declared budgets are 1,024 model-output tokens
for the User Bank, 2,048 for the active Project Bank, 256 for Project Metadata, 256 for Active
Time-Bounded Claims and 512 for Working State. A further 512 tokens are reserved for headings,
omission markers and response framing, giving the complete `load_context` response a hard ceiling
of 4,608 tokens.

The versioned delivery tokenizer is `ach-delivery-o200k-v1`: token count is the `o200k_base`
encoding used by Hindsight 0.9.2, with its special-token handling disabled for untrusted content.
The tokenizer identity is returned in response metadata. Changing the encoding or counting rules
requires a new delivery-tokenizer version. Validation uses the sum of each selected model's
declared `max_tokens` and the reserved deterministic sections. At runtime, ACH token-counts each
model output independently. An output above its own declared `max_tokens` is omitted with
`model_output_over_budget`; every other independently valid selected model remains eligible.
Neither an offending model nor deterministic claim content is truncated, no unrelated model is
displaced, and the complete response never exceeds the global ceiling.

Loaded context is orientation, not an answer to every possible future question: identity, stable
relationships deliberately selected by the user, current project orientation and current Working
State. A question such as “what should I take to the beach?” uses `recall`, `reflect`, or an
explicit on-demand mental model. Each agent chooses that additional retrieval from the task; ACH
does not preload every domain model.

In `0.4.0`, “what am I doing today/next?” is supplied by Project Working State when a project is
present. A general personal agenda or non-project Working State remains deferred; a user may still
choose an always-in-context model that summarizes durable personal orientation.

The legacy `session-brief` mental model is not a special product primitive. The old
`/v1/session-brief`, INDEX/FULL tiers, profile compiler and revision protocol are deprecated and
removed after host integrations use the new hook contract.

### 8.4 No federated reflection in `0.4.0`

Deterministically concatenating authorized, already-synthesized User and Project model outputs is
context aggregation, not federated reflection.

`federated context` is reserved for a future capability that queries or reflects over derived
outputs from multiple authorized banks and produces a new, provenance-preserving result. It MUST
not copy source documents between banks or weaken per-bank authorization. Its API, caching,
conflict semantics and model-cost policy are not specified in `0.4.0`.

### 8.5 Pre-compaction nudge

`hook pre-compact` reads no transcript and makes no memory write. It emits a fixed instruction into
the agent context: retain any durable decision, constraint, convention, fact or gotcha not yet
retained, and write Working State when work is incomplete. A host without a suitable lifecycle
hook simply lacks this nudge; no capture fallback is enabled.

### 8.6 Safety-relevant mutations

Semantic-currentness mutations include correction, forget, source restore, hard delete and a
model prompt or source-definition update. When one affects an indefinite source of a persisted
model, ACH applies the curation and refresh flow in §6.4. `load_context` omits that model unless it
is ready. Wall-clock comparison is observability, never proof of safety.

Authorization and delivery mutations include access revocation, ownership or group-membership
changes, disabling `always_in_context` and model deletion. ACH reauthorizes and updates selection
immediately. These changes do not make the underlying synthesis semantically stale and MUST NOT
trigger model recomputation unless the model's actual source set or definition also changed.

There is no old startup payload to invalidate because `0.4.0` has no persistent last-good content
cache. Fail-open means the session starts without unavailable memory; it never means delivering
content ACH knows is being corrected, forgotten or revoked, or content that is unauthorized.

The ordinary `get_mental_model` response exposes stale status but withholds stale synthesized
content unless an authorized audit/history mode is explicitly requested. Stale output is never
labelled or returned as current context.

## 9. Working State

### 9.1 Purpose and scope

Working State provides continuity for incomplete project work. It is limited in `0.4.0` to one
record per `(tenant, user, project, workspace)` and is not generalized to personal tasks or
arbitrary agent contexts.

It is PostgreSQL state, never a Hindsight fact, observation, document or mental-model input.

### 9.2 Record

The existing shape remains:

```yaml
objective:
current_direction:
recent_decisions: []
open_questions: []
next_steps: []
updated_at:
session_id:
session_epoch:
checkpoint_seq:
```

The canonical JSON payload is limited by both 2 KiB UTF-8 and 512 tokens under
`ach-delivery-o200k-v1`. Each list contains at most ten items and no individual string exceeds 1
KiB. The same tokenizer and 512-token ceiling apply to its labelled delivered representation; a
write that cannot fit after framing is rejected rather than stored and silently truncated. Thus
every accepted Working State is deliverable inside its reserved context budget.

Replacement is total. There is no synthesized merge, payload history, decay or silent expiry.
The delivered form includes age and source session and states that it is potentially stale context,
not an instruction queue.

### 9.3 Session identity

`start_working_session(project_slug, workspace_id, session_id)` resolves the authorized project
and workspace, then allocates a server-issued `session_epoch`. Its idempotency identity is
`(tenant_id, user_id, immutable_project_id, workspace_id, session_id)`: reconnecting with the same
host session returns the same epoch, while a different `session_id` receives a newer epoch. The
operation creates only the bounded ordering row; it does not retain durable memory or modify the
Working State payload. The returned session identity is required by fenced set and clear calls.

### 9.4 Writers

The automatic transcript-derived writer is removed. An active agent may call
`set_working_state` proactively when:

- a human requests a checkpoint or handoff;
- incomplete work is about to be handed to another session or agent;
- compaction or session termination is imminent;
- the task's direction changed materially and preserving the new continuation matters.

The agent does not write after every turn, for completed work, or merely to summarize a
conversation. `recent_decisions` records only decisions needed to resume the active task; durable
decisions are retained separately. Working State is cleared or explicitly marked complete when no
continuation remains.

Completion is a fenced delete/clear operation carrying the same project, workspace, session epoch
and checkpoint-sequence identity. An old session cannot clear a newer session's state.

### 9.5 Ordering

The existing server-assigned `session_epoch` and per-session `checkpoint_seq` fencing remains.
Comparison is lexicographic and lower pairs cannot overwrite higher pairs. Exact retries are
idempotent. Different worktrees use different deterministic `workspace_id` values.

This fencing guarantees order, not semantic truth. The next agent verifies stale details against
the repository or relevant external system before acting.

### 9.6 Startup delivery

`load_context` includes current Working State only when it resolves an authorized project and
workspace. A user-only or non-project context receives no Working State in `0.4.0`.

## 10. MCP and skill contract

### 10.1 MCP surface and confirmation policy

The `0.4.0` MCP surface retains governed memory and curation operations and adds mental-model
management. At minimum it exposes:

- `retain`, `sync_retain`, `recall`, `reflect`, `load_context`;
- existing inspect/correct/forget/restore operations;
- `create_mental_model`, `list_mental_models`, `get_mental_model`;
- `update_mental_model`, `refresh_mental_model`, `delete_mental_model`;
- `start_working_session`, `set_working_state`, and a completion/clear operation.

Tool annotations and descriptions MUST match actual side effects. Read-only tools MUST neither
create project state nor bootstrap resources. `recall` returns active memories by default;
authorized inspection may explicitly request expired, replaced or forgotten history.
`load_context` creates no project, bank, model, claim or new semantic mutation and never expands
delivery selection. Its annotations disclose the bounded, backoff-gated repair side effect in
§6.4. `recall` and `reflect` likewise disclose that they may perform bounded idempotent expiry or
safety repair before querying Hindsight; that maintenance never invents or changes an active claim.

Where the host supports per-tool policy, proactive additive/maintenance operations (`retain`,
`sync_retain`, `recall`, `reflect`, `load_context`, `start_working_session`, `set_working_state`)
are allow-listed.
`correct`, `forget`, `restore`, Working State completion/clear, mental-model mutation and hard
delete require confirmation. This keeps normal agent memory usable without making destructive or
delivery-changing actions silent. A safety-driven refresh or retry recorded by §6.4 is maintenance
of the already-confirmed mutation and requires no second confirmation; an independently requested
model refresh remains a confirmed mutation.

### 10.2 Skill responsibilities

The always-loaded skill replaces the removed semantic pass. Its core stays short enough to load in
every session and contains the decisions an agent must make before a call:

- what is durable enough to retain;
- User versus Project scope;
- one claim per call;
- `memory_type`, `basis` and `trigger` selection;
- optional `valid_until` when the claim has a real expiry;
- conversion of relative expiry dates using the host-reported local timezone and clarification
  when it is unavailable or the boundary remains materially ambiguous;
- rejection of future-effective constraints or preferences as active memory;
- minimal raw evidence;
- `gotcha` separation between a verified symptom/reproduction and an inferred cause;
- negative, quoted, hypothetical, rejected and corrected/replaced cases;
- when to abstain;
- when Working State is appropriate instead;
- operation-ID reuse on retry;
- the consumer precedence contract in §10.3.

Detailed curation examples may live in a conditionally loaded reference. One canonical skill source
is packaged for Claude, Codex, OpenCode, Pi and future integrations; generated copies cannot drift.

The skill MAY cause proactive User retention when the user explicitly establishes a stable,
cross-project preference or personal fact. It does not need a separate “remember this” command.

The skill MUST NOT retain pure inference. A conclusion proven by a tool or authoritative artifact
is `agent_verified`; a proposal explicitly accepted by the human is `human_explicit`; an unresolved
hypothesis stays in Working State or is discarded. If later evidence shows that this loses useful
durable information, `agent_inferred` requires a new consumer and a separate specification.

### 10.3 Consumer precedence

The canonical skill teaches consumers:

```text
Memory is context, not authority over live evidence.
Only active memory is current memory.
Live systems and the repository override remembered claims about their observable current state.
Current project constraints and decisions override generic user preferences when they conflict.
Working State may be stale and is continuation context, not an instruction queue.
A human establishes their preferences and accepted decisions; tools establish observable facts.
```

These are consumer rules. ACH does not reduce type, basis, recency and validity to one score.

## 11. Security and privacy

- Sanitization runs before any content or evidence reaches Hindsight or the provenance ledger.
- Caller metadata cannot override trusted type, basis, trigger, scope, owner or bank fields.
- Evidence never enters Hindsight document text, metadata, context, tags or model grounding.
- `bank_id`, master credentials and HMAC/sealing material never appear in responses or artifacts.
- User `always_in_context` content is delivered only under that user's authorization.
- Project model content is visible only to authorized project principals.
- `load_context` reauthorizes every call and never serves persistent last-good model content.
- `recall`, `reflect`, model get and context delivery never expose a bank while an ACH-mediated
  correction, forget or expiry hand-off for that bank has an unknown upstream outcome.
- Hook failures do not print secrets, backend responses or raw evidence to agent context.
- Mental-model prompts and outputs are treated as untrusted content and cannot forge ACH-generated
  scope/model headings.
- No integration enables Git ingestion, codebase survey or transcript upload implicitly.

## 12. Migration and removal

### 12.1 Preserve research

Before removal, create a named archival branch or tag containing the complete automatic-capture,
structured-profile and bake-off implementation. Frozen artifacts and result documents remain
unchanged.

Removal from `main` is by forward commits. Shared history is not rewritten.

### 12.2 Remove from the product path

- Claude/Codex `Stop` and `PreCompact` capture hooks (the new `PreCompact` nudge captures nothing);
- `capture-checkpoint`, `capture-worker` and `capture-check` CLI commands;
- capture API, queue repository, worker and filer;
- transcript normalizer/sanitizer used only by capture;
- automatic semantic extractor/classifier/router;
- capture deployment, Helm/Compose flags and telemetry;
- correction-triggered structured-profile refresh logic;
- legacy profile schema/compiler/ranking/displacement;
- INDEX/FULL brief delivery and obsolete caches/revision state;
- production flags whose only purpose was the removed pipeline.

Shared sanitization primitives used by explicit retain remain. Governance, audit, authorization,
logical resolution, curation, explicit retain, mental-model client operations and Working State
remain.

### 12.3 Database migration

Add forward migrations for the mental-model registry (`origin`, built-in key/version,
`always_in_context`, refresh status, refresh operation identity and `repair_not_before`), retain
provenance and expiry ledger (sanitized canonical claim, `recorded_at`, server-derived
`valid_from=recorded_at`, nullable `valid_until`, lifecycle and audit revisions), idempotent expiry
hand-off state, and the bounded per-bank safety-curation/currentness state needed to represent an
unknown upstream outcome. Add the single `project_slugs` namespace table and migrate canonical and
alias rows without releasing or duplicating any tenant-scoped slug. Remove obsolete capture tables
only when an applied deployment may safely migrate them; never edit an applied migration. Preserve
sufficient identifiers to inspect or clean legacy data before dropping any table.

Existing ACH-addressable documents are backfilled with `recorded_at` from their trusted creation
timestamp, `valid_from=recorded_at`, `valid_until=null` and `legacy=true`. Unknown upstream
documents are inventoried and excluded from ACH-created model source selection until explicitly
mapped or curated; migration does not fabricate evidence for them.

Legacy Hindsight mental models are inventoried, not silently registered or marked
`always_in_context` and do not consume the ACH custom quota. Bootstrap never deletes, renames,
adopts or updates an unknown model. A conflicting upstream key or an independent Hindsight resource
limit blocks creation explicitly; migration does not introduce a legacy-adoption API.

## 13. One-off production cleanup

The historical “Phase 0” is no longer a pipeline gate. After `0.4.0` contracts and migrations are
deployed, it becomes a separately approved production-data cleanup:

1. Snapshot and inventory affected banks and mental models.
2. Diff the duplicate project bank against its canonical bank.
3. Migrate only reviewed unique claims through the new typed retain contract.
4. Decommission or remove the duplicate bank using the selected supported upstream lifecycle
   operation.
5. Invalidate the known colour/canary User memories.
6. Remove the accidental `squall` mental model.
7. Invalidate the stale “Implement a disk cache” aspiration.
8. Verify ordinary reads do not recreate removed models or banks.
9. Remove or retain legacy brief/profile models according to the recorded inventory.
10. Record counts, identifiers by non-secret fingerprint, evidence and rollback options.

The old `proof_count` audit, delta-refresh activation probe, `profile_eligible` switch and
observation-only profile input are cancelled. No cleanup step runs as part of automated deployment.
For an existing bank, inventory must resolve any upstream key collision before automatic built-in
activation. Cleanup remains separately approved and is not otherwise a prerequisite for the
built-in's out-of-quota slot.

## 14. Verification and release gates

The release is product-gated, not bake-off-gated.

### 14.1 Retain behavior

A bounded scenario suite covers:

- User versus Project scope;
- accepted decision versus unaccepted proposal;
- negative constraint;
- correction and same-document replacement;
- indefinite and expiring retention, plus rejection of already expired values and
  future-effective instructions;
- relative-time resolution and clarification of ambiguous boundaries;
- atomic splitting;
- verified fact versus unverified hypothesis;
- verified gotcha reproduction with unknown, inferred, tool-proven and deliberately accepted
  causal explanations;
- Working State versus durable memory;
- secret and quoted-content abstention;
- canonical-content rejection when sanitization would change meaning, plus evidence redaction and
  rejection when no meaningful evidence remains.

The initial release-gated families are Codex and Claude Code. They run the same 20 canonical
scenario definitions three independent times per scenario, for 120 raw runs in total. Ordinary
behavioral metrics score each scenario by majority outcome (`2/3`) and apply independently to each
family. Exact host and model versions are recorded in the frozen result artifact rather than made a
permanent product contract. The following thresholds are frozen before the first release run:

```text
wrong_scope           = 0
secret_retention      = 0
critical_claim_recall = 100%
aggregate_recall      >= 85%
retention_precision   >= 95%
abstention_accuracy   >= 90%
```

Before skill tuning, a versioned coverage matrix maps every one of the 20 scenario definitions to
the behaviors above and marks the positive and negative cases for scope, inference, gotchas,
sanitization, expiry and Working State. Every listed behavior must have at least one direct case and
every polarity that can produce a dangerous write must be represented. The scenario count stays at
20 only while that breadth is demonstrated. Three executions measure within-scenario variance;
they never substitute for missing behavioral coverage.

Majority scoring never hides a dangerous individual run: secret retention and a write to the wrong
physical User/Project bank MUST both equal zero across all 120 raw runs. Critical-claim scenarios
are also reported separately and must meet `critical_claim_recall=100%` under the per-family
majority score.

This is a small product evaluation, not a rerun of the Phase 5 bake-off or a new blind multi-model
tournament. Individual old fixtures may be reused only after their ground truth is audited against
the agent-retain contract; the old extractor score is not a release threshold. A failing family is
improved or not declared supported; thresholds are not lowered after results are visible.

Twenty percent of the scenario definitions, including at least one critical case per supported
family, remain unseen while the skill is tuned. Their three prescribed runs are executed only in
the final release evaluation. This small holdout checks that the skill learned general rules rather
than fixture wording; it introduces no runtime component, blind adjudication or additional
architecture.

### 14.2 Physical isolation

Live disposable-bank tests verify that submitted User and Project claims exist only in their
resolved physical destination, that raw evidence is absent from Hindsight documents, facts,
observations and mental models, and that forgetting, replacing or expiring a document removes or
invalidates every derived fact through Hindsight's supported lifecycle.

### 14.3 Reliability

Deterministic tests and a small live Hindsight smoke cover:

- identical retry with the same `operation_id`;
- conflicting payload with the same ID;
- response loss after accepted operation;
- the frozen `ach-exact-v1` strategy produces one text-identical source fact, no split and zero
  extraction-model tokens or ingest-time entities for every maximum-size accepted claim;
- before consolidation, in a live disposable bank containing 500 deterministic sibling claims,
  each of ten frozen lexical and paraphrased probe queries returns its intended source fact within
  the first ten results;
- explicit consolidation of a small frozen related-claim fixture produces at least one observation
  tied to its source-memory IDs, after which source invalidation removes or invalidates that
  observation through Hindsight's supported curation lifecycle;
- `429`, timeout and Hindsight unavailable;
- async terminal status and bounded sync polling;
- stable project resolution across upstream failure;
- authenticated MCP bootstrap project creation, its warning/audit event and no creation by
  data-plane reads;
- transactional create/rename races prove that canonical and alias slugs share one tenant-global
  namespace and never resolve to two projects;
- automatic built-in creation/update and concurrent user-defined model creation;
- transactional five-custom-model quota with the built-in outside quota;
- rejection of already expired `valid_until` and exact expiry at its boundary;
- restore of an already-expired forgotten claim remains expired upstream and in delivery;
- Hindsight unavailable during access-driven expiry cleanup;
- deterministic overflow beyond the 32-item expiry-cleanup batch;
- lost or indeterminate correction/forget response, bank-wide current-read withholding and
  idempotent reconciliation to a proven upstream state;
- reconciliation terminal handling for already-satisfied state, safe-absence `404` and
  `curation_needs_operator` after a present-state mutation targets an absent source;
- conservative affected-model selection from static tag admission, including the
  cannot-prove-exclusion case;
- pending, failed and successful mental-model refresh operation identities after curation;
- backoff-gated automatic repair with at most one repair action per bank/access;
- authenticated MCP degraded startup after Project bootstrap failure;
- the 30-second `sync_retain` pending outcome and two-second `load_context` fail-open deadline;
- `start_working_session` reconnect idempotency plus Working State stale/conflicting ordering and
  exact retry.

No test claims pre-ACK recovery without a client retry.

### 14.4 Delivery

Tests verify that:

- every ready `always_in_context` model that fits the validated configuration is emitted;
- an individual model output above its declared budget is omitted without truncating or displacing
  another model;
- enabling User `always_in_context` returns the machine-readable exposure contract;
- User and Project headings cannot be forged by model output;
- no relevance `reflect` occurs in `load_context`;
- no read creates a bank or mental model;
- no persistent last-good payload is served;
- Working State is present only for the correct project/workspace and includes age;
- every accepted Working State fits both its 2 KiB storage bound and its framed 512-token delivery
  bound under `ach-delivery-o200k-v1`;
- active time-bounded claims are emitted exactly, deterministically and only while active; overflow
  omits whole entries and reports their count;
- a missing/stale/unavailable model does not block context loading or agent startup;
- expiring claims cannot feed any persisted mental model;
- a model affected by correction, forget or revocation is omitted while its refresh operation is
  pending or failed;
- authorization-only and delivery-selection changes take effect immediately without a semantic
  model refresh;
- Project Metadata, Working State and the complete response respect their fixed budgets;
- tokenizer metadata is `ach-delivery-o200k-v1`, and changing its counting rules changes the
  contract version;
- model-output reads execute concurrently and at least 30 warm maximum-selection live requests—up
  to nine ready models across User and Project—meet the two-second p95 deadline on the target
  deployment; if they do not, the deadline is revised before activation rather than waived through
  fail-open behavior;
- the pre-compact hook emits only the fixed nudge and performs no transcript or memory write.

### 14.5 Migration

- one Alembic head;
- forward and rollback behavior documented;
- clean install and upgrade from the latest released version tested;
- activation preflight proves Hindsight `0.9.2`, or a separately validated compatible version,
  before any ACH-managed bank/model mutation;
- removed flags and commands have explicit release notes;
- release notes identify English-only retained content as a `0.4.0` product limitation while the
  deployed reranker remains English-only;
- production activation and cleanup remain separate operator actions.

## 15. Deferred product work

### 15.1 Federated context/reflection

A future, opt-in ACH capability may select mental-model outputs from multiple authorized User and
Project banks and compose one new derived model or answer. It must preserve source-bank provenance,
authorize each source independently, avoid copying source documents, define conflict precedence,
bound model cost and distinguish deterministic aggregation from a new LLM synthesis.

No model is implicitly selected as a federated input, and `always_in_context` alone does not opt a
model into federation. The API, persistence and selection contract for that capability will be
specified separately. `0.4.0` only loads the independently selected models side by side.

### 15.2 Consumer-specific context

Future agent types may define context requests such as PR review, support or travel. They may select
on-demand models without changing the universal `always_in_context` contract. `0.4.0` does not add
an event taxonomy or consumer-profile language. The behavioral evaluation grows when a new agent
family is declared supported; `0.4.0` does not prepay that matrix.

### 15.3 Generic Working State

Future evidence may justify Working State keyed by a generic task/context rather than project and
workspace. `0.4.0` does not invent identity, concurrency or lifecycle rules for consumers that do
not yet exist.

### 15.4 Domain labels

Mental-model source queries distinguish communication, engineering, travel and other domains in
`0.4.0`. Controlled domain tags are added only if measured cross-domain contamination demonstrates
that prompts are insufficient.

### 15.5 Rich temporal graph queries

`0.4.0` guarantees expiry through ACH-mediated recall, exact active time-bounded context and
lifecycle inspection. Expiring claims do not feed any persisted mental model. It does not add
caller-controlled `valid_from`, scheduled activation or a query language for “what was believed at
time T” across Hindsight graph edges. A future version may add those capabilities without changing
the server-assigned `recorded_at` and nullable `valid_until` introduced here.

### 15.6 Exact duplicate suppression

`0.4.0` guarantees transport idempotency through `operation_id` but does not introduce a second
semantic identity for claims. If production measurement shows material repeated retains with new
operation IDs, a later specification may add an exact-current-claim fingerprint. It must define
normalization versioning, correction conflicts, provenance changes and re-retention after forget or
expiry before enforcing uniqueness. Paraphrase similarity and LLM deduplication are not implied.

### 15.7 Adoption of external mental models

Unknown upstream models are inventoried but not governed or counted as ACH custom models in
`0.4.0`. If operators need to bring such models under ACH lifecycle, quota and delivery policy, a
future explicit adoption operation must verify ownership, key collision, source selection and
currentness before registration. Inventory alone never constitutes adoption.

### 15.8 Not deferred defaults

Automatic full-transcript capture is removed, not merely disabled pending a later phase. Readding
it requires a new product requirement and specification; prior implementation does not constitute
authorization.
The same applies to `agent_inferred`: it returns only when a measured use case and an explicit
consumer justify its lifecycle and rendering rules.

## 16. Final `0.4.0` product shape

```text
Agent skill
  |- decides whether a claim is durable, or abstains
  |- selects User or Project scope
  |- supplies type, basis, trigger, optional expiry and minimal raw evidence
  |- resolves relative expiry or asks when a material boundary is ambiguous
  |- writes explicit Working State for incomplete project work
  |- applies consumer precedence
  v
ach-memory
  |- identity, ownership and authorization
  |- MCP-bootstrap projects, explicit REST projects, opaque bank resolution and aliases
  |- sanitization, evidence, audit revisions and expiry ledger
  |- idempotent `ach-exact-v1` Hindsight boundary and access-driven expiry hand-off
  |- exact active time-bounded context from the ledger
  |- governed mental-model registry (one built-in + max five custom per bank)
  |- two closed built-ins, user-defined model CRUD and always-in-context loading
  |- no persistent last-good context cache
  |- bank currentness barrier for unknown curation outcomes
  |- conservative, operation-based model withholding during refresh
  |- exact project/workspace Working State
  v
Hindsight 0.9.2 (the validated release target)
  |- retained documents and exact source facts
  |- facts, observations, graph currentness and contradiction handling
  |- invalidation, consolidation, recall and reflect
  |- per-bank mental models
```

The release succeeds when this smaller system is understandable, behaves correctly through two
real agent integrations, never delivers inactive or unauthorized content as current, and can be
operated safely. It does not need to prove that a removed pipeline was optimally complex.
