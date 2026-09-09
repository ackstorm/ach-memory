# ach-memory v1.0 — Decision Handover for Independent Validation

**Date:** 2026-09-09
**Baseline:** v0.5.0 (`8c42ed8`), plus `b718b0f` on `main` and unreleased.
**Status:** decisions taken in conversation, nothing implemented, no spec written yet.

---

## What this document is

A record of design decisions taken today for a v1.0 of ach-memory, written so a
reviewer **with access to this codebase** can check them against the code rather
than take them on trust.

It is not a plan. There are no tasks, no file-by-file changes and no ordering.
Those come after validation.

## How to validate it

Every claim below carries a `file:line` so it can be checked. Please treat the
reasoning as adversarially as the code:

1. **Check the evidence.** Where a decision rests on a quoted comment or a
   docstring, confirm the quote is real and still current.
2. **Attack the reasoning, not the conclusion.** Several decisions were taken
   *against* my recommendation; the owner's call stands, but the reasoning on
   both sides should survive scrutiny.
3. **Go straight to "Unverified assumptions"** near the end. Those are the
   claims I could not confirm in-session, and each one, if wrong, changes a
   decision.
4. **Say what breaks.** The largest block deletes three tables and the entire
   local credential system. Missed dependencies are the most valuable thing you
   can find.

---

## Origin

Seven findings from wiring ach-agent (the ACH agent harness) to ach-memory
against a live docker-compose stack. Verified against the code, one by one:

| # | Finding | Verdict after checking the code |
|---|---|---|
| 1 | No lazy project creation; PROJECT_NOT_FOUND indistinguishable from an authz failure | Confirmed. The request to make them distinguishable was **refused** |
| 2 | Project ownership pinned to the minting user | Confirmed. The ownership model asked for already exists |
| 3 | `MEMORY_MCP_ALLOWED_HOSTS` default is asymmetric | Confirmed bug. Cause is one line further than reported |
| 4 | The `mem_` prefix rejection is misleading | Confirmed, and worse than reported. Now moot |
| 5 | `/mcp` costs a 307 on every call | Confirmed, measured |
| 6 | `retain` is not read-your-writes | **Already solved.** `sync_retain` exists and `retain`'s own description says so |
| 7 | Custom mental models cannot use their own tags | Confirmed, and v0.5.0 made it incoherent |

---

## Decision 1 — Lazy creation on the first `retain`

**Decision.** `retain` creates the project when it does not exist, accepting any
slug, and provisions it in the same act — both the project bank and the user
bank. Reads never create.

**Why.** Today `retain` (`retention.py:59`) and every read (`read_context.py:113`)
resolve with `create=False`; `bootstrap.py` is the only `create=True` call site.
An agent whose project nobody created gets PROJECT_NOT_FOUND for ever. The hole
is wider than the report says: a user arriving through platform auth never
passes through `POST /v1/users` either — `link_identity`
(`auth/provisioning.py`) creates the `User` row and **deliberately does not
provision the bank**, to keep a Hindsight round trip off the authentication path.
The proxy's bootstrap pre-warm covered that; ach-agent uses its own facade and
so nothing provisions at all in their deployment.

**Rejected — making PROJECT_NOT_FOUND distinguishable from an authz failure.**
This was finding 1's actual request. A foreign project and a non-existent one
must answer identically, or the error becomes an existence oracle: probe slugs,
learn what exists in other tenants. The reporters praised exactly this
containment in the same message ("a caller naming a foreign project_slug gets
PROJECT_NOT_FOUND rather than crossing tenants, which is exactly what we need").
They cannot have both. Their operational need is met instead by the REST layer
still returning the error (see Decision 2).

**Rejected — creation on any resolve, reads included.** Would have been one
place covering everything, but `load_context` advertises, verbatim
(`mcp/context_tools.py:14`): *"Load authorized bounded standing context. It
creates no project, bank, model or claim, and retains nothing."* with
`readOnlyHint=True, idempotentHint=True`. Owner chose to keep that promise true.

**Accepted risk.** `project_slug` is a model-supplied parameter on the MCP tool
(`mcp/memory_tools.py:293`). A hallucinated slug therefore creates a real
project, a Hindsight bank, a retain strategy and a built-in model. Mitigated by
the rate limit below, not eliminated.

**Rate limit.** At most 10 project creations per user per hour. **Counted in the
database**, not in `ratelimit.Limiter`. The existing limiter is in-process and
its own docstring says so: *"With N replicas the effective limit is N times the
configured one. Say so before relying on it as a quota."*

**Counted from the audit trail, not from `projects.created_at`.** Counting live
rows by `owner_id` is bypassable: transfer a project to a group and the owner
changes, so the count drops and the budget refills. Counting `project.create`
audit events by actor is not. **This requires a change:** `project.create` is
audited only for a master key today (`projects.py:261-263`, inside
`if principal.is_master`), so the lazy path records nothing at all. It must be
recorded for every caller.

**Atomicity, and its physical limit.** The owner's rule is "no bank without its
provisioning". Strict transactional atomicity is impossible and deliberately so:
`_create_builtin` (`mental_model_service.py:596-643`) commits the registration
**before** calling Hindsight, so that a lost response can never orphan an
upstream model nobody recorded. What is achievable, and what was decided:
if provisioning fails the `retain` fails with a real error, and the intermediate
state self-repairs on the next touch — `b718b0f` added exactly that repair for a
built-in stuck in `creating`.

**Open consequence for the reviewer.** This inverts a decision shipped in v0.5.0
hours earlier: `POST /v1/projects` and `POST /v1/users` currently provision
*best-effort* inside a savepoint and return 201 regardless
(`api/projects.py:168-189`, `api/users.py:104-123`). If the rule is universal,
those must fail too. **This was not decided.** Flagging it as an open question.

---

## Decision 2 — MCP reads return empty, REST keeps the error

**Decision.** All seven MCP read tools map PROJECT_NOT_FOUND to an empty result:
`load_context`, `recall`, `memory_history`, `list_memories`, `get_memory`,
`list_documents`, `reflect`. The service layer and REST keep returning
PROJECT_NOT_FOUND.

**Why.** An agent does not know whether today is its first day in the office. Its
first call is almost always `load_context` from a SessionStart hook, or `recall`
— never `retain`. If reads error, the first session is a wall of failures and the
agent learns to stop calling. Empty is also true: for a project with no memories,
"nothing" is the honest answer.

**Why it does not open the oracle.** The collapse happens one layer earlier,
but NOT in `authorize` — that raises `ProjectAccessDenied` carrying `owner_type`,
a deliberately actionable 403 (`projects.py:137-141`). The collapse is in
`_authorize_resolution` (`projects.py:143-156`): *"Direct mutations still use
`authorize` and its actionable 403. Slug resolution is the discovery boundary, so
its denial deliberately has the same code, message and public details as an
absent mapping."*

**So the invariant is narrower than it first appears.** The mapping is oracle-safe
only while every read reaches the project **through slug resolution** and never
calls `authorize` directly. Two consequences:

- Exposing `transfer` as an MCP tool (Decision 3) must resolve first, then
  authorize. The REST route does this today (`api/projects.py:302`); the MCP tool
  must not take a shortcut.
- This belongs in a test, not in prose: no read surface may return
  `ProjectAccessDenied` to a caller.

**Which tools.** Not "the seven read tools" — that was wrong, and the natural
rule ("everything with `readOnlyHint=True`") is wrong too, because `recall` and
`reflect` are both `readOnlyHint=False`: they enqueue access maintenance. The set
is the ten annotated read-only tools — `load_context`, `memory_history`,
`list_memories`, `get_memory`, `list_documents`, `get_document`, `get_operation`,
`list_operations`, `list_mental_models`, `get_mental_model` — **plus `recall` and
`reflect`. Twelve.** There is no working-state read tool; that read happens inside
`load_context`.

Consistency, not security, is the reason for taking all of them: if `recall`
returns empty and `list_memories` returns NOT_FOUND, the agent sees two surfaces
for one state and the failing one teaches it exactly what the mapping was meant
to avoid.

**Side benefit.** The facade still sees PROJECT_NOT_FOUND over REST, so an
operator can distinguish "memory is empty" from "memory is misconfigured" and log
it. That is finding 1's real need, met without the oracle.

**Verified, no change needed.** The SessionStart hook already fails open on its
own — `plugins/shared/scripts/session-start.sh:10` is
`ach-memory context load 2>/dev/null || true`, followed by `exit 0`.

---

## Decision 3 — Ownership stays first-toucher; the group model already exists

**Decision.** A lazily created project is owned by the calling user
(`projects._create`, `projects.py:267-286`, *"always owned by the calling
user"*). Moving a project to a group is exposed as an MCP tool.

**What already exists and needs no design.** The ownership model the owner
described — a project is private, only the owner may move it to a group they
belong to, and once in a group any member may take it private — is already
implemented:

- `projects.transfer` (`projects.py:352-381`) authorizes the caller, validates
  the new owner, and records an audit event.
- Its docstring already states the accepted consequence, and it matches the
  owner's words today: *"Accepted consequence, stated in SPEC §6.1: a single
  group member can transfer a group-owned project to themselves and lock the
  group out. The alternative is a group-admin role, and v1 has no permission
  model. The audit event is the mitigation."*
- `_validate_owner` (`projects.py:79-90`) refuses a group the caller does not
  belong to, and creates the `Group` row on demand from an IdP assertion.
- `authorize` reads membership from `principal.groups` — the IdP — on every
  request, independently of any local row.

So the work is exposing `PATCH /v1/projects/{slug}/owner` as an MCP tool. Not a
redesign.

**Objection raised and overruled.** First-toucher ownership combined with
Decision 2 produces a silent split for a shared agent: user A retains first and
owns the project; user B's reads map to empty (silence for a whole session) and
only their first *write* fails. The owner accepted this. The mitigation, which
does not require a service change, is that the harness creates the project once,
group-owned, with an operator identity — lazy creation only fires when nobody
created it first, so the two compose.

---

## Decision 4 — Delete the internal identity system

**Decision.** ach-memory core has no user system, no group system and no admin
credential. Identity is always external, in the two shapes that already exist:

1. **Header forward** — a configured header (`x-litellm-api-key`, `x-ach-key`, …)
   is sent to an endpoint that returns JSON with user and groups. This is
   `auth/providers/platform.py`.
2. **JWT** — a token the service can verify itself, carrying user and groups.
   This is `auth/providers/jwt_provider.py`.

Both stay. The "sidecar" is a **deployment artifact** that speaks shape 1 when
there is no LiteLLM or ACH in front — not a third code path. This distinction
matters and I got it wrong first: there is no new authentication mechanism.

**Operator authority** is configuration over the already-resolved identity:

```
MEMORY_MASTER_GROUPS=aaa,bbb
MEMORY_MASTER_USERS=juancarlos@example.com,id_232323232
```

**Deleted:** `auth/providers/local_key.py`, `auth/keys.py`, the `mem_` prefix
discriminator (`auth/principal.py:78`), the `ApiKey` model and table, the
`/v1/users` router, the `/v1/groups` router, `Group`/`GroupMember` tables,
`master_key_hash` config, and `ratelimit.MASTER_KEY_ID`.

**Kept:** the `User` table. It is the bank anchor, not an identity record —
`link_identity`'s docstring: *"An IdP tells us who someone is; it cannot tell us
where their memory lives. `User.bank_id` is what makes memory exist."* Populated
only by `link_identity` from now on.

**The master key was not a hack.** The owner's initial premise was that it
existed as an ACH workaround. It does not: 49 references across 19 files, and it
is the operator plane —

| Route | Purpose |
|---|---|
| `GET /v1/admin/audit` | the audit log |
| `POST /v1/admin/memory/{scope}/clear` | empty a bank |
| `DELETE /v1/admin/memory/{scope}` | delete a bank |
| `POST /v1/admin/slugs/{slug}/release` | release a retired slug |
| `GET /v1/activity`, `/summary` | fleet view |
| `on_behalf_of` header | delegation provenance in the audit trail |

Config-based operator identity preserves all of it while removing the shared
secret. It also improves the audit trail: `actor_key_id` stops being `None` and
becomes a person, which is the only thing that matters when reviewing why
somebody touched another user's bank.

**This simplifies three branches rather than complicating them.** Today
`is_master` means *authority without identity*, written into the code in three
places:

- `banks.py:20` — *"A master key has no identity of its own, so it must name its
  target"* — so `scope=user` requires an explicit `user_id`.
- `projects.py:184` — *"A master key has no identity, so there is no owner to
  assign"* — so a master can never lazily create a project.
- `api/projects.py:138` — *"a master-key create must name an owner"*.

An operator under the new model **is** an external user with a bank who also has
authority. The two concepts separate and all three exceptions disappear: the
operator reaches their own bank like anyone else, creates projects they own like
anyone else, and naming someone else's target becomes the authority part rather
than a requirement.

**Breaks every shipped client.** This was missed in the first draft and is the
largest practical risk in the whole change. `cli.py:82` sends
`Authorization: Bearer {api_key}`; `cli.py:356` and `:380` write that form into
host configuration; `plugins/claude-code/.mcp.json:15` and both session-start
hooks gate on `ACH_MEMORY_API_KEY`. Deleting minted keys breaks the CLI and both
plugins until their credential changes.

Nuance that softens it: `Authorization: Bearer` still works — a JWT travels the
same header. So `ACH_MEMORY_API_KEY` becomes "whatever token your identity
provider issues" rather than a minted `mem_` key. It is a credential migration
plus documentation, not a client rewrite. But it breaks every existing install
and must be planned, not discovered.

**Group access semantics change.** `authorize` today grants on an IdP assertion
**or** a local row: `projects.py:132` is
`or db.get(GroupMember, (project.owner_id, principal.user_id))`, checked
independently and deliberately never merged. Dropping `group_members` makes
access IdP-only. That is the intended direction, but it is a behaviour change to
name, not a no-op.

**Scale.** Drops three tables, needs a migration, and breaks the surface. This is
why it is v1.0 and not v0.5.1.

**Accepted risk.** Compromising the identity provider compromises every bank,
including operator authority. This is not a regression — whoever holds the master
key hash today has the same reach — but it is now a deliberate choice. The
`MEMORY_MASTER_*` defaults must be **empty**, so a misconfiguration grants
nobody rather than everybody. A reviewer should check that this is airtight.

**Local development.** With no local credential, the compose stack cannot be
driven without an external identity source. The decision is a minimal sidecar
container in compose that answers the identity JSON — no `if dev:` branch in the
auth code.

---

## Decision 5 — Tag naming and the filter mode

**Decision.** Names diverge because the semantics diverge — on write you label,
on read you filter:

| Surface | Today | Becomes |
|---|---|---|
| `retain`, `sync_retain` | `tags` | `tags` |
| `recall`, `reflect` | `tags` | `tags_filter` + `tags_filter_mode` |
| `RecallHit` (output) | `tags` | `tags` |
| mental model | `source_tags` + `tags_match` | `source_tags` + `source_tags_mode` |

`tags_match` is Hindsight's own vocabulary; the external surface avoids upstream
vocabulary deliberately (`read_models.py:8`).

**This is a breaking rename.** `recall(tags=…)` shipped this morning in v0.5.0
and moves `TOOL_CONTRACT_SHA256`. Cheap now — ach-agent is the only consumer and
is mid-integration — expensive in a month.

**Decision.** The model chooses the filter mode through a closed enum on
`recall` and `reflect`. Mental models get the equivalent `source_tags_mode`, and
`source_tags` accepts a superset of the required pair.

**Background the reviewer needs.** Hindsight offers five modes
(`hindsight/client.py:389-391`): `any`, `all`, `any_strict`, `all_strict`,
`exact`. The non-strict forms **also return untagged memories**, which is why the
read path pins `all_strict` and forbids caller control
(`read_models.py:311-313`): *"`tags_match` stays fixed at `all_strict` —
AND-with-extras-allowed — never caller-settable: `all`/`any` would also return
untagged memories and silently defeat the filter."*

That sentence is why finding 7's literal request does not work. Mental models are
pinned at `tags_match: Literal["all"]` (`mental_model_service.py:105`, `:146`;
`builtin_models.py:14`). Adding `repo:x` to `source_tags` without changing the
mode yields a model that **looks scoped and is not** — it still synthesizes over
everything untagged. The flight-search example in the report would still read the
whole corpus.

**Also relevant:** there is no boost or hint parameter upstream
(`hindsight/client.py:350-361`). A tag either filters or it is not sent. "Orient
without excluding" can only be implemented as query expansion — appending the tag
terms to the query text.

**Built-ins could move to `all_strict`.** The first draft left them on `all` to
avoid silently discarding Hindsight's own consolidations. Review settled that
observations inherit the source tags, so the risk does not exist and the move is
safe — and arguably more correct, since `all` also admits genuinely untagged
rows, which in an ACH bank are rows we did not write. **Owner to decide.**

**Objection raised and overruled.** Letting the model choose means `repo:` can
arrive as a hint, so containment inside a shared project bank becomes dependent
on the model's judgement per call. The two failure directions are asymmetric:
over-filtering returns empty and the agent says "I have no memory of that" (a
false negative wearing the face of an answer); under-filtering returns another
repo's facts and the agent treats them as its own (a silent false positive). The
alternative offered was deriving the mode from the tag's shape — namespaced
(`repo:`, `env:`) filters hard, bare (`sre`) orients — which makes containment
structural and needs no new parameter. The owner chose the explicit enum.
Mitigation: `RecallHit.tags` (shipped today) lets the agent verify which repo
each hit belongs to instead of assuming.

---

## Decision 6 — Small confirmed fixes

**Allowed hosts.** `config.py:51` defaults to `"127.0.0.1,localhost"`;
`docker-compose.yml:148` overrides with `127.0.0.1,localhost,localhost:8000`.
Adding `localhost:8000` is what created the asymmetry: it gave one loopback form
a port and not the other. The SDK matcher
(`mcp/server/transport_security.py:43-63`) is exact on the full Host header, with
one wildcard form:

```python
if host in self.settings.allowed_hosts: return True
for allowed in self.settings.allowed_hosts:
    if allowed.endswith(":*") and host.startswith(allowed[:-2] + ":"): return True
```

So a portless entry never matches a Host carrying a port. **Removing the ports
would have broken every non-standard port**, which was the owner's first
instinct and was corrected. The fix is the wildcard, in **both** defaults —
`config.py`'s shipped default has the same latent bug, since the service listens
on 8000:

```
127.0.0.1,localhost,127.0.0.1:*,localhost:*
```

Plus a startup log line naming the allowed hosts. The report asked for the 421
body to name the rejected Host; that body comes from inside the MCP SDK and is
not ours to set. A startup line is strictly better — it fires before the first
failed call.

**`/mcp` redirect.** Measured, not assumed: `POST /mcp` → `307` to
`http://localhost/mcp/`. With `stateless_http=True` every tool call is a fresh
POST, so the redirect is **per call**, doubling request count. Cause:
`app.mount("/mcp", …)` (`api/app.py:319`) over an app whose own route is `/`.
Decision: document `/mcp/` as canonical. `cli.py` already builds it correctly.

**`sync_retain`.** Nothing to build. It exists as a REST route
(`api/memory.py:333`) and an MCP tool (`mcp/memory_tools.py:310`), and `retain`'s
own description already says (`mcp/memory_tools.py:280-282`): *"Returns
immediately with an operation you can follow with get_operation; use sync_retain
when you need to read it back straight away."* Their 20-second retry loop is
unnecessary. The likely real bug is on their side — a facade tool allowlist that
omits `sync_retain` and `get_operation`. Action here: the README should name a
recommended minimal tool set for a facade.

**`mem_` prefix (finding 4) is moot.** The prefix exists to discriminate local
keys from JWTs on `Authorization` (`auth/principal.py:70-79`). With local keys
deleted there is nothing to discriminate. Worth recording what the investigation
found anyway, in case the deletion is reconsidered: `local_key.authenticate`
does **not** check the prefix — only the dispatcher does, and only on the Bearer
branch. So the same prefix-less master key **works via `x-ach-key` and is refused
via `Authorization: Bearer`**. That asymmetry, not the constant's name, is what
cost them a code read. And the service only ever holds `master_key_hash`
(`config.py:36`), never the plaintext, so it could not have validated the prefix
at startup even if we wanted to.

---

## Unverified assumptions — please dig here

1. **Whether Hindsight's own consolidations carry our source tags.** RESOLVED:
   they do, so the assumption is **false**. Verified at tag `v0.9.1`, the deployed
   version, not at clone head. `consolidator.py` writes
   `tags=observation_scope_tags if observation_scope_tags is not None else
   (m.get("tags") or [])` — byte-identical at v0.9.1 and v0.9.2.
   `observation_scope_tags` is only populated for the non-default
   `observation_scopes` modes (`per_tag`, `all_combinations`, `shared`, explicit
   list); the default is `None` (`engine/retain/types.py`), and we never send the
   field (`hindsight/client.py:140` guards on `is not None`). So observations
   inherit the memory's own tags. The "shared mode produces untagged
   observations" text quoted in the first draft is real but describes an explicit
   opt-in mode that does not apply to us.

   **Consequence:** built-in mental models can move to `all_strict` without losing
   consolidations. Consolidation groups by exact tag set, so each observation
   carries the full source set including `schema:ach-retain-v1` and `type:*`; since
   `all_strict` is AND-with-extras-allowed, a built-in whose `source_tags` are a
   subset still matches. **Whether to move them is the owner's call** — see the
   open decisions.

2. **The semantics of `any`/`all` regarding untagged memories.** CONFIRMED,
   against the version in play. The comment at `read_models.py:311-313` is
   accurate; `versioned_docs/version-0.9/developer/api/recall.mdx` states *"`any`
   — OR matching, includes untagged (default)"* and that the strict modes exclude
   them, and `versions.json` lists 0.9 as current. The version caveat in the first
   draft came from citing the 0.6 docs by mistake and does not apply.

3. **Platform `whoami` returns a stable per-user id.** If LiteLLM returns
   anything per-key, the `subject` rotates with the key and the bank orphans —
   the exact failure the `mem_` model had. This is on the ach-agent side and
   needs verifying there, not here.
4. **Whether mounting the MCP app differently would shadow other routes.** Not
   investigated, which is part of why documenting `/mcp/` was chosen over
   restructuring the mount.
5. **Whether any read path lets a foreign project and a non-existent one produce
   different observable results.** Decision 2's safety depends on this being
   false. I reasoned about it; I did not exhaustively audit it.

## What I most want challenged

- **Decision 2's oracle argument.** If the reviewer can construct any sequence of
  MCP or REST calls that distinguishes "project does not exist" from "project
  exists and is not yours", the mapping is unsafe as specified.
- **The claim that the master key is the operator plane.** Route list above. If
  it is wrong, Decision 4 gets simpler.
- **Whether first-toucher ownership plus empty reads is acceptable** for a shared
  agent, given the failure is silent for a whole session. Overruled, but a second
  opinion is worth having.
- **Whether letting the model choose the tag filter mode is safe** for `repo:`,
  which exists purely for containment inside a shared bank. Overruled, but the
  false-positive direction is silent.
- **The v0.5.0 inversion left open in Decision 1** — should `POST /v1/projects`
  and `POST /v1/users` also fail when provisioning fails, instead of returning
  201 best-effort? Review answered this by symmetry rather than by preference;
  carried into the open decisions below as item 1.

## Review outcome (2026-09-09, independent model with repo access)

**Confirmed by review:** `create=False` everywhere but `bootstrap.py:118`; the
`all_strict` rationale; built-ins pinned to `Literal["all"]`; the allowed-hosts
asymmetry and the wildcard fix; the SDK matcher logic; the `/mcp` mount; the
best-effort savepoint on `POST /v1/projects`; the `transfer` docstring;
`_validate_owner`; the session-start hook failing open.

**Corrected, and folded into the text above:** the Decision 2 citation and the
narrower invariant it implies; the read-tool count (12, not 7); the shipped-client
breakage in Decision 4; the local `GroupMember` read; the rate-limit counting
source.

**Second-pass review (same reviewer, evidence at tag `v0.9.1`):** settled the
consolidation question against my position — observations inherit source tags,
the default `observation_scopes` is `None`, and we never send the field. My
"unresolved" verdict came from generalising a docstring about the opt-in
`shared` mode without checking the default. Assumptions 1 and 2 are now closed.

**First-pass review claims that did not survive checking:**

- *"`projects.py:263` already records `project.create`"* — it does not. The call
  is inside `if principal.is_master`, so the lazy path records nothing. The
  underlying point (count audit events, not live rows) is right; the fix needs an
  extra change, now recorded in Decision 1.
- *"every tool with `readOnlyHint=True` plus `reflect`"* — misses `recall`, which
  is also `readOnlyHint=False` for the same maintenance reason. And there is no
  working-state read tool.
- *"assumption 1 is likely false"* — see the assumptions section. Unresolved, not
  settled; the evidence cuts both ways depending on a default we never set.

**Open decisions the review reopened, for the owner:**

1. **Decision 1's atomicity, resolved by symmetry.** The review points out that
   `api/projects.py`'s comment — *"Failing the request here would leave a
   committed project the caller was told did not exist"* — applies identically to
   lazy `retain`. So either provisioning failure rolls back the project row on
   both paths, or it fails neither. One rule, not two. **Undecided.**
2. **Decision 5's default.** The reviewer disagrees with the owner's call on
   model-chosen filter mode, on the same containment grounds raised in
   conversation, and proposes a compromise: keep the enum, default it to
   `all_strict`, and make loosening an explicit opt-out — so the lazy call is the
   safe call. **Undecided.**
3. **A cheap mitigation for Decision 3.** `load_context` could carry
   `project_status: "absent"`. Still empty, still oracle-safe (a foreign project
   and an absent one both report absent), but a facade can log the difference.
   One field. **Undecided.**
4. **Built-in mental models: stay on `all`, or move to `all_strict`?** Reopened
   by review settling the consolidation question above. Moving them excludes
   genuinely untagged rows from standing context, which in an ACH bank are rows we
   did not write. **Undecided.**
5. **`MEMORY_MASTER_*` must be provably closed by default** — enforced with a
   startup assertion that an unset variable can never grant, not merely an empty
   default. Agreed by both sides; belongs in the spec.

## What is not in scope here

Migration mechanics, task ordering, and file-by-file changes. Those go into a
spec and execution plans once these decisions survive review.
