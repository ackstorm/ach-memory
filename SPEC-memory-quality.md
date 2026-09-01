# Memory Quality — SPEC dump v0

**Status:** Draft for discussion. Nothing in §11 is approved for implementation.
**Date:** 2026-08-28
**Scope:** Memory *methodology* — what gets stored, how it is synthesized, how it reaches agents. Identity, tenancy, auth and the MCP tool contract stay governed by `SPEC-v1.md`.
**Inputs:** Production measurements taken 2026-08-28 against the live deployment; Hindsight 0.9.1 docs + OpenAPI (89 endpoints); an independent audit subagent (mental models vs Claude's own knowledge); an adversarial red-team pass on a different model (Opus); field survey of mem0 / Zep-Graphiti / engram-family. A peer-agent consult over the Muster bus is pending and will be folded in when it answers.
**Data:** raw dumps under `/tmp/ach-memory-inspect/` (`mental-models.txt`, `brief.json`, `brief.txt`) and the session scratchpad (`allrows.json` — all 217 facts).

---

## §0 Why this document exists

Juan Carlos's reflections, distilled to problem statements (his words paraphrased, order preserved):

1. **The MCP tool doesn't earn its keep on the read side.** "No acabo de estar a gusto… no se usa suficiente, al menos en la parte de recall."
2. **Too much loose trivia is stored.** "Se guardan datos muy sueltos… 'el usuario ha dicho que esta variable de entorno es true' — cosas que no aportan nada."
3. **Too few important recaps.** Sessions end and the substance is not captured.
4. **Certainty and importance should be first-class.** "Medir o pedir el nivel de certeza… y el nivel de importancia. Para mí es vital." Low-importance content should be filtered — by us, at the gate.
5. **Mental models should be profiles.** From the raw stream: a user profile (development preferences, personal tastes) and project profiles (how we develop, how we test, message conventions) — *not* a map of where code lives; that is derivable from the repo and is not our job.
6. **Project memory must be shareable.** A project bank should read impersonally — "the project is defined this way," never "Juan Carlos prefers" — so it can be handed to collaborators. Personal preference lives in the user bank; a thing can be both, phrased differently in each.
7. **Agglutinate, then delete.** Once reflections/consolidation have absorbed loose facts, the loose layer should retire (possibly kept as raw archive for search).
8. **The felt result today:** "Yo hoy mirando los mental models de mi usuario, no me siento reflejado."

---

## §1 The two systems and their roles

### §1.1 Hindsight (the capability)

Self-hosted memory engine (MIT, `vectorize-io/hindsight`, deployed 0.9.1). Its design is a ladder, each rung a different answer to "what does memory know":

```
raw facts        fact_type=world/experience — evidence. Timestamped (occurred vs
                 learned), entity-linked, graph-edged (temporal/semantic/entity/causal).
observations     fact_type=observation — consolidated beliefs. Deduplicated, durable,
                 proof-counted, evolving; each backed by source facts. Built by async
                 consolidation after retain.
mental models    standing answers to named questions (source_query), auto-refreshed
                 (cron/delta), versioned, optionally schema-structured.
reflect          on-demand reasoned answer: agentic loop over the ladder (mental models
                 → observations → raw facts), cites evidence, shaped by bank
                 disposition + injected directives.
```

Steering inputs (all optional, all per-bank): `retain_mission` / `observations_mission` / `reflect_mission` (prompts injected at extraction / consolidation / synthesis), `retain_extraction_mode` (concise|verbose|custom), disposition (skepticism/literalism/empathy 1–5), bank `mission`/`background`, `entity_labels` (controlled entity vocabulary), tags (per item / per document / tag-scoped triggers and directives), directives (standing instructions injected into reflect, priority-ordered), `response_schema` on reflect and mental models, `dry-run-extract` (preview extraction under a candidate config, nothing stored), observation clearing + re-consolidation, soft invalidation (restorable) on every memory.

### §1.2 ach-memory (the methodology / harness)

Multi-tenant FastAPI service over Hindsight: user keys and master key, per-user and per-project banks resolved from identity + git locator, 15-tool MCP surface over streamable HTTP, stdio proxy for agent hosts, admin console, activity/audit trail. The harness's *deliberate* restrictions (each was a reasoned decision, recorded in code comments):

| Restriction | Where | Reason at the time |
|---|---|---|
| No bank-config passthrough ("No config PATCH: as of Plan 6 there is nothing to configure") | `hindsight/client.py:588,603` | YAGNI; keep surface minimal |
| Mental models REST-only, off the MCP surface | `api/mental_models.py`, absent from `mcp/tools.py` | keep agent surface lean |
| `recall`/`reflect` carry no `readOnlyHint` | `mcp/tools.py:348` | real incident: `create=True` resolution minted 80 junk projects in 5.1s |
| One mental model per bank, fixed name `ach-memory-session-brief`, 400 tokens | `brief.py` | single-purpose session brief |
| Session brief delivered via MCP server `instructions` | `mcp/server.py:136` | "announce, never call": SessionStart hook stays a plain `cat`, no network |

The methodology today, in one sentence: **agents drip facts in via `retain`; nightly reflection compresses them into one 400-token model per bank; the composed brief rides MCP instructions into the session; nothing else reads the bank.**

---

## §2 Deployed state (2026-08-28)

- ach-memory **v0.3.5** (chart + image, Flux-managed), after the 24h MCP outage 08-27 17:29 → 08-28 (uv pre-release resolution; fixed by declaring `fastmcp-slim` directly, `ccc6580`).
- Hindsight **0.9.1**, split LLMs: retain = `bedrock.openai.gpt-oss-20b-1-0`, reflect/mental-models = `gemini.gemini-3.7-flash` (a thinking model — refresh cost worth watching), both via LiteLLM. The gemini-provider/base_url 400 gotcha is pinned in `docker-compose.yml:93-98`.
- Brief models' trigger now deployed as `{mode: delta, refresh_cron: "0 3 * * *", keep_trace: true}`; `_reconcile` PATCHed both live models after restart.
- Claude Code truncates MCP server instructions at **2048 chars** (measured; host-side, not ours).

## §3 Measured inventory

### §3.1 Banks and layers

| Bank | Facts | world (raw) | observation (beliefs) | invalidated | edited | tags used | Fate |
|---|---|---|---|---|---|---|---|
| user (Juan Carlos) | 95 | 74 | 21 | 0 | 0 | 0 | live |
| project `github.com-ackstorm-ach-memory-cd5e3a11` | 75 | 51 | 24 | 0 | 0 | 0 | live (locator-resolved) |
| project `ach-memory` (slug-only twin) | 47 | 29 | 18 | 0 | 0 | 0 | **unreachable split-brain** — locator resolution never finds it |

Graph is dense and unused by us: user bank 1707 links (932 temporal / 668 semantic / 80 entity / 27 causal) over 95 facts.

### §3.2 Quality measurements

- **Evidence/belief twins surface together:** ~30 near-dup pairs (Jaccard ≥ 0.62), 29 of them a `world` fact and its consolidated `observation`. Our brief `source_query`, `list` and `recall` read both layers undifferentiated — the ladder exists, we flatten it.
- **Noise:** 25% of the user bank (24/95) is colour preferences + test canary phrases from old experiments. The brief's USER_QUERY says "Omit colour, styling and theme preferences entirely" — a read-time patch over a write-time failure, re-paid every nightly gemini refresh.
- **Importance signal exists and is emergent:** `proof_count`. The three highest rows (pc=24, 14, 7) are precisely the real policies (review/tests at phase end; broad verification with heavy gates once per change; the workflow rule-set). 92/95 user rows sit at pc=1.
- **Entity identity is fragmented:** the user exists as three entity nodes — `Juan Carlos` (22 mentions), `user` (7), `usuario` (4) — plus generic nouns promoted to entities (`Rule 1`, `tests`, `phase`). Cause: `entities_allow_free_form=true`, `entity_labels=None`.
- **Config is factory-default everywhere:** every bank's `overrides: {}`. All missions None, disposition 3/3/3, mission/background empty, 0 directives, 0 tags ever.
- **Write/read asymmetry:** 10 project facts retained today; ~0 recalls from sessions. `recall` requires host confirmation (no readOnlyHint — deliberate), `retain` flows freely.

### §3.3 Broken surfaces found this session

| Surface | Symptom | Root cause |
|---|---|---|
| `/v1/session-brief` with master key | 400 always → console **Brief tab dead** | `api/brief.py` resolves `scope=user` with `user_id=None`; the `on-behalf-of` header is used only for audit, never resolution |
| Console **Models tab** | shows `source_query`, italic — looks like the model has no content | console omits `detail=full` on `/v1/mental-models`; content never fetched |
| Brief composition | project section (2397 chars, 7/13 verified gotchas) **0% delivered**; only 626 chars of memory survive the 2048 cap | 1422-char static policy block first, brief rides MCP `instructions` |
| Read probe side effect | my `scope=project&project_slug=squall` probe **created** a mental model on the squall bank (one LLM generation) | `GET /v1/session-brief` creates on read (`ensure_section`) |

## §4 The delivery-channel finding (red-team, verified)

The brief is 6115 chars: 1422 static policy + headers/caveats + 2018 user + 2397 project. Claude Code keeps the first 2048. Verified surviving memory content: **626 chars** — the two genuinely-good user rules (dialogue-first; English/Spanish-STT), the Neovim line, then two of the three *misfiled* MCP-setup bullets, cut mid-word. Every verified project gotcha is discarded, every session.

Meanwhile `plugins/claude-code/scripts/session-start.sh` (and the codex twin) already runs at every session start with **no size cap**, and is a plain `cat activation.txt` by design ("a hook that talked to the service would make every session start depend on the network"). That objection is solvable with the discipline the script already shows: short timeout, last-good brief cached on disk, `|| true`, `exit 0`. The cached-brief idea is also the one recorded "aspiration stored as fact" (`Implement a disk cache…`) arriving from the latency direction, and addresses the startup-silence complaint.

**Consequence:** the system is effectively write-only today. The only consumer that has ever read the banks end-to-end is the nightly refresh — an LLM paid to summarize facts into a digest that is 87% thrown away. Every write-side improvement is unobservable until the channel works.

## §5 Mental models vs Claude's own knowledge (independent audit)

Audit compared `/tmp/ach-memory-inspect/mental-models.txt` against `~/.claude/CLAUDE.md` (+`RTK.md`), Claude Code's persistent memory dir for this project, and the repo.

| Model | Bullets | Confirmed | Plausible-unverified | Noise/residue | Stale/wrong | Critical omissions |
|---|---|---|---|---|---|---|
| USER | 18 | 2 | 13 | 3 | 0 | 7 |
| PROJECT | 13 | 7 | 5 | 0 | 1 | 3 |

- **USER retention gap (the headline):** only 2 of ~12 standing CLAUDE.md rules present (English-output+Spanish-STT; dialogue-before-plan, softened). Missing entirely: the hard approval gate, bash polling ban, Docker rules, venv discipline, rtk, the coding-discipline quartet, commit conventions. Not a synthesis failure — those rules **never enter the bank**; the source_query honestly reports what memories say.
- **USER misfiling:** 3 bullets are ach-memory MCP setup decisions (`--url` explicit, published install, keys via env block) recorded as universal user preferences — project residue in the personal profile (the inverse of the shareability leak feared in §0.6).
- **PROJECT model:** a genuinely good gotcha sheet (7/13 verified to exact file:line, causes attached) with **zero orientation** — never says what the project is, never names `SPEC-v1.md` while citing its §8.2 — and one aspiration recorded in the same voice as fact ("Implement a disk cache", unbuilt).
- **Cross-cutting:** Claude Code's own persistent memory dir for this project is **empty**. Hindsight is currently the only cross-session store either system has. The stakes on its quality are higher than assumed.

## §6 Hindsight capability gap

| Capability (0.9.1, verified in OpenAPI/docs) | Purpose | Used by harness |
|---|---|---|
| `retain_mission` / extraction modes | write-time inclusion/exclusion policy ("ignore logistics, greetings…") | **No** (surface deleted in Plan 6) |
| `observations_mission` | steer what consolidation considers durable | No |
| `reflect_mission`, disposition, bank mission/background | shape synthesis voice and caution | No |
| Directives (priority, tags, active flag) | standing instructions injected into every reflect | **Plumbed end-to-end in our API (`api/directives.py`), 0 in prod** |
| `entity_labels` vocabulary | stop free-form entity spawn; canonical identities | No |
| Tags (items, documents, triggers, directives) | scoping, filtered recall, tag-scoped models | No (0 uses) |
| Mental-model trigger `fact_types` | e.g. feed a model **only** consolidated observations | No (we send 5 of ~13 trigger fields) |
| Mental-model / reflect `response_schema` | structured profiles instead of prose | No |
| `dry-run-extract` | A/B a retain config with nothing stored | No |
| Soft invalidation + restore; observation clear + re-consolidate | the "agglutinate then retire" lifecycle | Wrapped (`forget`/`restore`) but **never used** (0/217) |
| Recall `types=["observation"]`, budgets, `include` | read beliefs, not evidence+belief twins | No — we flatten |

## §7 Field comparison (why not another engine)

| System | Core idea | What it would give us | Verdict |
|---|---|---|---|
| **mem0** | LLM write-gate: ADD/UPDATE/DELETE/NOOP per candidate fact | The §0.4 importance decision, at write time | The *pattern* is right; the engine is flat memories + optional graph. Hindsight's missions + consolidation already cover the mechanism; adopt the write-gate **idea**, not the engine |
| **Zep / Graphiti** | Bi-temporal knowledge graph; contradictions invalidate, never delete | Point-in-time queries; §3.2's "aspiration vs fact" as validity intervals | Hindsight already has occurred-vs-learned and soft invalidation — unused by us. Graph build cost is real (Zep: ~600k tokens/long conversation) |
| **engram family** | Agent decides what's worth remembering; storage deliberately dumb (SQLite/FTS5) | Radical simplicity; no nightly LLM bill | The opposite pole. Useful as a null hypothesis: if agent judgment must carry quality anyway, the engine matters less than the gate |
| **Hindsight** (ours) | Biomimetic ladder, one Postgres, strong benchmarks (89.6 LoCoMo) | Everything in §6, already deployed | **Keep.** Every missing capability exists behind the API we wrap; migration buys nothing measurable |

## §8 Premises (settled unless Juan Carlos reopens them)

- P1. Hindsight stays; improvements are methodology + harness, not engine swap (§7).
- P2. The user approves every code change before it is written; this SPEC is discussion material.
- P3. Standing security decisions hold: no public console exposure without SSO, master key never surfaced/logged, no `dry-run-refresh` endpoint, the readOnlyHint incident is not casually reopened.
- P4. Project banks must end shareable: impersonal phrasing, personal preference only in the user bank (§0.6).
- P5. Repo-derivable structure is out of scope for memory (§0.5); memory stores what the repo cannot show.
- P6. Certainty/importance must become a write-time concern, not a read-time patch (§0.4) — mechanism open (D4).
- P7. Delivered ≠ composed: any brief work is judged by what survives at the agent, not what the server renders (§4).
- P8. Two banks for one repo is a defect, not a feature (§3.1).

## §9 Approaches catalog (10 proposed + 1 from red-team), sharpest known failure mode each

| # | Approach | Sharpest failure mode / hidden cost |
|---|---|---|
| 1 | Native levers (missions, disposition, entity_labels, bank identity) | "Config, not code" is false — Plan 6 deleted the config surface; this re-adds API. Cannot clean the 25% already stored |
| 2 | Instruction mirror (CLAUDE.md → directives) | Directives feed *reflect*; sessions don't call reflect. Duplicates what Claude hosts already read for free; only pays off for non-Claude hosts (see D6) |
| 3 | Write-gate (importance/certainty at retain) | Throttles the only working flow; moves LLM cost from nightly to per-write; unmeasurable until reads exist |
| 4 | Read-path first (readOnlyHint split, models over MCP, reflect as Q&A) | Annotations are per-tool, not per-argument: an honest read-only recall requires a tool split or reopens the slug-squat hole |
| 5 | Multi-model suite (identity/workflow/testing/gotchas/orientation) | Strictly negative under the 2048 cap; multiplies nightly refresh bill linearly |
| 6 | Recap methodology (end-of-session structured recaps) | Depends on clean session ends; crashes//clear/compaction are exactly the sessions memory exists for |
| 7 | Lifecycle loop (scheduled invalidate/re-consolidate + console) | Automation for 217 facts and 2 users costs more than doing it by hand; "superseded" lacks a signal (0 tags) |
| 8 | Shareable project bank (impersonal attribution via missions) | Enforcement is an LLM prompt (soft); the *measured* leak runs the other way (project residue in user bank) |
| 9 | Radical thin (deliberate notes only, no drip) | Kills the working flow; unfalsifiable (fewer facts always look cleaner); silence is undetectable without an eval |
| 10 | Eval first | Probes need a delivered artifact; today's first verdict is the already-known "13% arrives" |
| 11 | **SessionStart-hook delivery** (red-team): fetch brief in the hook, short timeout, last-good cache on disk, `exit 0` | Network-at-start objection returns if cache/timeout discipline slips; cache staleness must be visible, not silent |

## §10 External consultations

- **Red-team (Opus, adversarial, read-only).** Reframe accepted after verification: *the system is write-only; fix the consumer first.* Three of three load-bearing claims verified against repo and dumps (§4; `client.py:603`; surviving-window content — with one softening: the first two good user rules do survive, not only the misfiled ones). Its ordering: hygiene-by-hand → channel → delivered-brief assertion → only then write-side quality. Its per-approach attacks are folded into §9.
- **Peer ach-memory agent (Muster bus).** Consulted with the same catalog; **no reply yet**. To be folded in; flagged if it changes anything material.
- **Independent audit subagent.** §5, delivered in full.

## §11 Proposed plan (PROPOSAL — awaiting approval, per P2)

Phased so every phase is observable before the next starts, and write-side tuning never precedes a working read path.

### Phase 0 — Hygiene by hand (no code; reversible; uses only endpoints that exist)
0.1 Retire the split-brain: soft-invalidate or archive the 47-fact `ach-memory` slug-only bank (its unique facts first checked against the live bank), so every metric thereafter has one denominator.
0.2 Soft-invalidate the 24 colour/canary rows in the user bank (restorable by design).
0.3 Delete the accidental `squall` brief model created by this session's probe (owner's call — it costs one nightly refresh otherwise).

### Phase 1 — Deliver the brief (small code; the multiplier)
1.1 SessionStart hook fetches the brief with hard timeout + last-good disk cache + `exit 0` (approach 11). MCP `instructions` keeps a one-paragraph pointer (fits any cap).
1.2 Shrink the 1422-char static policy to its load-bearing sentences; per-section budgets so user+project both ship even on capped channels.
1.3 Fix `/v1/session-brief` master-key 400 (`user_id=on_behalf_of or scoped.user_id` in `api/brief.py`) — un-deads the console Brief tab and master-key ops.
1.4 Console Models tab: request `detail=full`, render content (+ the history/freshness it already shows).

### Phase 2 — One assertion, not an eval suite
2.1 A test that renders the *delivered* payload and asserts: ≥N project gotchas present, 0 colour/canary content, aspiration-class lines carry their "plan, not fact" marking. This converts "is the brief good?" from taste into regression.

### Phase 3 — Write-side quality (only after 1+2 prove reads)
3.1 Re-add a *narrow* bank-config surface (server-set at bank creation; not agent-exposed): `retain_mission` + `observations_mission` per bank kind — user-bank mission excludes styling/trivia and phrases preferences personally; project-bank mission enforces impersonal, shareable phrasing (P4) and excludes repo-derivable structure (P5).
3.2 `entity_labels` for user banks (canonical `Juan Carlos`≡`user`≡`usuario`) — kills identity fragmentation for future extraction.
3.3 Retain-tool guidance rewrite (the §0.4 gate, judgment-first): what NOT to store, when to prefer one recap over N fragments; possibly a `sync_retain` prompt asking the agent for a one-line "why this matters".
3.4 Brief models: add `fact_types: ["observation"]` to the trigger so nightly synthesis reads beliefs, not evidence+belief twins. (Touches the models' trigger — explicitly a decided-plan change, per the standing agreement not to drift them casually.)

### Phase 4 — Profiles (the §0.5 vision; needs an uncapped channel first)
4.1 Additional named mental models per bank (orientation, workflow, testing/gotchas), scoped by `fact_types`/tags, some schema-structured; brief composes from them. Refresh cost estimated before enabling (gemini-3.7-flash is a thinking model).

### Explicitly deferred / rejected
- Reopening the `readOnlyHint` decision as designed (any read-path change goes through a tool-split design, not an annotation flip) — deferred to its own discussion.
- Lifecycle automation (approach 7) at current scale — by hand until volume justifies it.
- Engine migration (§7), radical thin (approach 9) — rejected.
- Nightly-refresh cost audit — fold into Phase 4 estimation.

## §12 Open decisions (Juan Carlos)

- D1. Phase 0.1: retire the 47-fact twin by invalidation, or export-then-invalidate?
- D2. Phase 0.3: delete the accidental `squall` model?
- D3. Phase 1.1: SessionStart-hook delivery replaces MCP-instructions as the primary channel — agree? (MCP instructions stay as pointer.)
- D4. Importance/certainty mechanism (P6): agent-declared level at retain (schema field), retain_mission exclusion only (no schema change), or both?
- D5. CLAUDE.md-class rules: mirror into directives for non-Claude hosts (approach 2, scoped), or accept CLAUDE.md as the sole channel for Claude hosts and let memory store only conversation-learned rules?
- D6. Do the misfiled MCP-setup bullets move to the project bank (re-retain there + invalidate in user bank), or just die with Phase 0.2's cleanup wave?

## §13 Sources

Hindsight docs: retain / mental-models / reflect / observations at `hindsight.vectorize.io/developer/*`; repo `github.com/vectorize-io/hindsight`; paper arXiv:2512.12818. Mem0 paper arXiv:2504.19413. Zep/Graphiti paper arXiv:2501.13956. Engram family: multiple OSS projects (agent-judgment pole). Live OpenAPI: `:8888/openapi.json` (89 endpoints). All measurements reproducible from the scratchpad scripts of session `da0bff12` and `/tmp/ach-memory-inspect/`.
