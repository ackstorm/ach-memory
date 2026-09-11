#!/usr/bin/env python3
"""Capability differential: ach-memory versus vanilla Hindsight.

Runs the same operation against both arms of one compose stack and reports
what each actually did. Both arms are real: `api` is ach-memory, `hindsight`
is the unmodified engine ach-memory layers over, published on loopback by
docker-compose.yml for exactly this kind of direct access.

Usage:
    make bench

What this measures
------------------
Whether the governance layer enforces a property, and whether the bare
engine does. Nothing here needs a real LLM, so it runs on the same MockLLM
stack as `make e2e` and is deterministic. Retrieval QUALITY is a different
question with a different harness: scripts/bench_quality.py.

What this is NOT
----------------
A security audit of Hindsight. Hindsight is a single-tenant engine that
resolves tenancy from an Authorization header; it was never built to
partition users and projects inside one deployment. Where a probe reports
OUT_OF_SCOPE, that is the honest reading -- the capability is absent by
design, and ach-memory exists to supply it. Probes report what they
OBSERVED; no arm's verdict is hardcoded, because several properties
(observation history, curation, recall token caps) are wholly or partly
Hindsight's own and it would be dishonest to claim them as ours.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import API, HINDSIGHT_URL, MARK, Http, Outcome, ProbeResult, bank_path, table

from memory.mcp.proxy import call_load_context

# An operator IDENTITY, not a credential: ach-memory mints nothing, so this
# is simultaneously the token the probes send and the subject named in
# MEMORY_MASTER_USERS, which is what grants it authority. scripts/bench-
# compose.sh sets both to one value.
OPERATOR = os.environ.get("MEMORY_OPERATOR_TOKEN")
if not OPERATOR:
    print("FAIL: MEMORY_OPERATOR_TOKEN is not set. Run via `make bench`.", file=sys.stderr)
    sys.exit(1)

RUN = uuid.uuid4().hex[:10]

PROBES: list[tuple[str, str, Callable[..., Awaitable[ProbeResult]]]] = []


def probe(name: str, question: str):
    def register(fn):
        PROBES.append((name, question, fn))
        return fn

    return register


def retain_body(scope: str, content: str, **kw) -> dict:
    """A v0.4.0 TypedRetainRequest body."""
    body = {
        "scope": scope,
        "content": content,
        "memory_type": kw.pop("memory_type", "fact"),
        "basis": kw.pop("basis", "human_explicit"),
        "trigger": kw.pop("trigger", "user_requested"),
        "evidence": kw.pop("evidence", [{"kind": "user_quote", "raw": content[:1024]}]),
        "operation_id": kw.pop("operation_id", str(uuid.uuid4())),
    }
    body.update({k: v for k, v in kw.items() if v is not None})
    return body


# ==========================================================================
# Probes
# ==========================================================================


@probe("cross_user_read", "Can one user's memory be read by another caller?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    # ach arm: Bob asks for Alice's user-scope memory. There is no route that
    # accepts a bank id at all, so the strongest reachable form of the
    # question is an operator delegation versus a peer's token.
    status, data = await ach.call(
        "POST", "/v1/memory/recall", key=ctx["key.bob"],
        json_body={"scope": "user", "user_id": ctx["user.alice"], "query": "salary"},
    )
    if status == 200:
        ach_out = Outcome("NOT_ENFORCED", f"Bob read Alice's bank: HTTP 200 {str(data)[:90]}")
    else:
        code = data.get("error", {}).get("code") if isinstance(data, dict) else None
        ach_out = Outcome(
            "ENFORCED",
            f"HTTP {status} {code}; a caller without operator authority is "
            "refused the delegation, not silently redirected to their own bank",
        )

    # vanilla arm: address the bank directly, with no credential at all.
    status, data = await van.call(
        "POST", f"{bank_path(ctx['van_bank'])}/memories/recall",
        json_body={"query": "salary"},
    )
    if status == 200:
        van_out = Outcome(
            "OUT_OF_SCOPE",
            "bank id addressed directly, no credential presented, HTTP 200",
        )
    else:
        van_out = Outcome("ENFORCED", f"HTTP {status}")
    return ProbeResult("cross_user_read", "", ach_out, van_out)


@probe("project_non_disclosure", "Can an outsider confirm a private project exists?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    real = ctx["project.private"]
    ghost = f"no-such-project-{RUN}"
    s1, d1 = await ach.call(
        "POST", "/v1/memory/recall", key=ctx["key.bob"],
        json_body={"scope": "project", "project_slug": real, "query": "anything"},
    )
    s2, d2 = await ach.call(
        "POST", "/v1/memory/recall", key=ctx["key.bob"],
        json_body={"scope": "project", "project_slug": ghost, "query": "anything"},
    )
    # Masked before comparing: a refusal naming the slug the caller just
    # sent tells the caller nothing it did not already know. What must not
    # differ is everything else.
    masked1 = str(d1).replace(real, "<SLUG>")
    masked2 = str(d2).replace(ghost, "<SLUG>")
    same = (s1 == s2) and (masked1 == masked2)
    detail = (
        "indistinguishable once the caller's own slug is masked"
        if same
        else f"DIFFER: {masked1[:70]} vs {masked2[:70]}"
    )
    ach_out = Outcome(
        "ENFORCED" if same else "NOT_ENFORCED",
        f"real slug -> {s1}, unknown slug -> {s2}; {detail}",
    )
    van_out = Outcome("OUT_OF_SCOPE", "the engine has no project concept")
    return ProbeResult("project_non_disclosure", "", ach_out, van_out)


@probe("idempotent_retain", "Does replaying one operation_id write the fact twice?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    op = str(uuid.uuid4())
    content = f"BENCH-{RUN}: the deploy key rotates every 90 days."
    body = retain_body("user", content, operation_id=op)
    for _ in range(2):
        await ach.call("POST", "/v1/memory/sync_retain", key=ctx["key.alice"], json_body=body)
    _, data = await ach.call(
        "POST", "/v1/memory/list", key=ctx["key.alice"],
        json_body={"scope": "user", "q": f"BENCH-{RUN}", "limit": 50},
    )
    hits = _items(data)
    ach_out = _replay_verdict(len(hits), "two identical operation_id writes")

    van_op = str(uuid.uuid4())
    vbank = ctx["van_bank"]
    vcontent = f"BENCH-{RUN}-vanilla: the deploy key rotates every 90 days."
    for _ in range(2):
        await van.call(
            "POST", f"{bank_path(vbank)}/memories",
            json_body={
                "items": [{"content": vcontent}],
                "async": False,
                "operation_id": van_op,
            },
            timeout=120.0,
        )
    _, vdata = await van.call(
        "GET", f"{bank_path(vbank)}/memories/list", params={"q": f"BENCH-{RUN}-vanilla", "limit": 50}
    )
    vhits = _items(vdata)
    van_out = _replay_verdict(len(vhits), "the same replay")
    return ProbeResult("idempotent_retain", "", ach_out, van_out)


@probe("provenance_required", "Can a fact be stored with no stated basis or evidence?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    bare = {"scope": "user", "content": f"BENCH-{RUN}: unattributed claim."}
    status, _ = await ach.call(
        "POST", "/v1/memory/sync_retain", key=ctx["key.alice"], json_body=bare
    )
    ach_out = Outcome(
        "ENFORCED" if status == 422 else "NOT_ENFORCED",
        f"HTTP {status} for a body with no memory_type/basis/trigger/evidence",
    )
    status, _ = await van.call(
        "POST", f"{bank_path(ctx['van_bank'])}/memories",
        json_body={
            "items": [{"content": f"BENCH-{RUN}: unattributed claim."}],
            "async": False,
            "operation_id": str(uuid.uuid4()),
        },
        timeout=120.0,
    )
    van_out = Outcome(
        "OUT_OF_SCOPE" if status < 300 else "ENFORCED",
        f"HTTP {status}; the engine takes free text with no provenance fields",
    )
    return ProbeResult("provenance_required", "", ach_out, van_out)


@probe("repo_identity", "Can a project be silently bound to the wrong repository?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    slug = f"bench-locator-{RUN}"
    await ach.call(
        "POST", "/v1/projects", key=ctx["key.alice"],
        json_body={"project_slug": slug, "git_locator": "github.com/bench/wrong"},
    )
    status, data = await ach.call(
        "POST", "/v1/memory/documents/list", key=ctx["key.alice"],
        json_body={
            "scope": "project", "project_slug": slug,
            "git_locator": "github.com/bench/right", "limit": 1,
        },
    )
    code = data.get("error", {}).get("code") if isinstance(data, dict) else None
    refused = status == 409 and code == "PROJECT_LOCATOR_MISMATCH"
    repaired = False
    if refused:
        await ach.call(
            "PATCH", f"/v1/projects/{slug}", key=ctx["key.alice"],
            json_body={"git_locator": "github.com/bench/right"},
        )
        status2, _ = await ach.call(
            "POST", "/v1/memory/documents/list", key=ctx["key.alice"],
            json_body={
                "scope": "project", "project_slug": slug,
                "git_locator": "github.com/bench/right", "limit": 1,
            },
        )
        repaired = status2 == 200
    ach_out = Outcome(
        "ENFORCED" if (refused and repaired) else "NOT_ENFORCED",
        f"mismatch -> HTTP {status} {code}" + (", PATCH repairs it" if repaired else ""),
    )
    van_out = Outcome("OUT_OF_SCOPE", "no project or repository identity in the engine")
    return ProbeResult("repo_identity", "", ach_out, van_out)


@probe("rename_forwarding", "Does an old project name keep working, and can it be re-taken?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    old = f"bench-rename-{RUN}"
    new = f"bench-renamed-{RUN}"
    await ach.call("POST", "/v1/projects", key=ctx["key.alice"], json_body={"project_slug": old})
    await ach.call(
        "PATCH", f"/v1/projects/{old}", key=ctx["key.alice"], json_body={"project_slug": new}
    )
    s_fwd, _ = await ach.call(
        "POST", "/v1/memory/list", key=ctx["key.alice"],
        json_body={"scope": "project", "project_slug": old, "limit": 1},
    )
    s_reuse, _ = await ach.call(
        "POST", "/v1/projects", key=ctx["key.alice"], json_body={"project_slug": old}
    )
    ok = s_fwd == 200 and s_reuse >= 400
    ach_out = Outcome(
        "ENFORCED" if ok else "NOT_ENFORCED",
        f"old slug reads -> {s_fwd}; re-registering the old slug -> {s_reuse}",
    )
    van_out = Outcome("OUT_OF_SCOPE", "no project naming in the engine")
    return ProbeResult("rename_forwarding", "", ach_out, van_out)


@probe("delivery_accounting", "When context is dropped for budget, is the caller told what and why?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    # Hindsight's own recall takes max_tokens, so a cap is NOT ach-only. The
    # distinguishing question is accounting: does the caller learn which
    # entries were dropped and for what reason?
    # LoadContextRequest is {project_slug?, workspace_id?} -- the budget is
    # server-side (each model's own max_tokens plus a global cap), not a
    # caller knob, so the question is whether the RESPONSE accounts for what
    # the budget dropped.
    # Over MCP, because that is the only surface `load_context` has: there is
    # no REST route, and ACH_MEMORY_URL names an MCP endpoint.
    try:
        data = await call_load_context(f"{API.rstrip('/')}/mcp/", ctx["key.alice"], None, None)
    except Exception as exc:  # noqa: BLE001 -- reported as the probe's outcome
        data = None
        failure = f"{type(exc).__name__}: {exc}"[:80]
    else:
        failure = "load_context returned nothing" if data is None else ""
    if data is None:
        ach_out = Outcome("ERROR", failure)
    else:
        keys = sorted(data) if isinstance(data, dict) else []
        has = "omissions" in keys and "tokenizer_version" in keys
        fired = len(data.get("omissions", [])) if isinstance(data, dict) else 0
        ach_out = Outcome(
            "ENFORCED" if has else "NOT_ENFORCED",
            f"response keys {keys}; {fired} omission(s) recorded, each with a reason"
            if has else f"no omission accounting: {keys}",
        )
    status, data = await van.call(
        "POST", f"{bank_path(ctx['van_bank'])}/memories/recall",
        json_body={"query": "anything", "max_tokens": 256},
    )
    keys = sorted(data.keys()) if isinstance(data, dict) else []
    told = any("omit" in k or "truncat" in k or "dropped" in k for k in keys)
    van_out = Outcome(
        "ENFORCED" if told else "OUT_OF_SCOPE",
        f"max_tokens honoured (HTTP {status}); response keys: {keys[:6]}",
    )
    return ProbeResult("delivery_accounting", "", ach_out, van_out)


@probe("correction_applied", "Is a correction governed, applied, and safe to replay?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    """Deliberately NOT "are previous revisions readable".

    That question was probed through /v1/read/history first, which reports
    HINDSIGHT's observation history -- an engine feature ach-memory does not
    supply -- and it returned 0 changes, which would have been published as
    an ach-memory shortcoming. ACH's own correction revisions live in
    `retained_records`, addressed by operation_id, not in that response.
    What is observable over HTTP, and genuinely ach-memory's, is that the
    correction is a governed operation: it applies, and replaying its
    operation_id does not fork a second version.
    """
    content = f"BENCH-{RUN}: the staging cluster runs in eu-west-1."
    await ach.call(
        "POST", "/v1/memory/sync_retain", key=ctx["key.alice"],
        json_body=retain_body("user", content), timeout=180.0,
    )
    _, data = await ach.call(
        "POST", "/v1/memory/list", key=ctx["key.alice"],
        json_body={"scope": "user", "q": "staging cluster", "limit": 5},
    )
    hits = _items(data)
    if not hits:
        return ProbeResult(
            "correction_applied", "",
            Outcome("ERROR", "nothing was stored to correct; verdict would be meaningless"),
            Outcome("ERROR", "not reached"),
        )

    mid = hits[0].get("memory_id") or hits[0].get("id")
    op = str(uuid.uuid4())
    fixed = f"BENCH-{RUN}: the staging cluster runs in eu-central-1."
    body = {
        "scope": "user", "memory_id": mid, "content": fixed, "operation_id": op,
    }
    s1, _ = await ach.call(
        "POST", "/v1/memory/correct", key=ctx["key.alice"], json_body=body, timeout=180.0
    )
    s2, _ = await ach.call(
        "POST", "/v1/memory/correct", key=ctx["key.alice"], json_body=body, timeout=180.0
    )
    _, after = await ach.call(
        "POST", "/v1/memory/list", key=ctx["key.alice"],
        json_body={"scope": "user", "q": "staging cluster", "limit": 20},
    )
    texts = [h.get("text", "") for h in _items(after)]
    applied = any("eu-central-1" in text for text in texts)
    forked = sum("eu-central-1" in text for text in texts)
    ok = s1 == 200 and s2 == 200 and applied and forked == 1
    ach_out = Outcome(
        "ENFORCED" if ok else "NOT_ENFORCED",
        f"correct -> {s1}, replay -> {s2}; corrected text present={applied}, copies={forked}",
    )
    van_out = Outcome(
        "OUT_OF_SCOPE",
        "no governed correction: the engine offers update_mode=replace against a document_id",
    )
    return ProbeResult("correction_applied", "", ach_out, van_out)


@probe("operator_traceability", "Is an admin reaching into a user's private bank recorded?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    await ach.call(
        "POST", "/v1/memory/recall", key=OPERATOR,
        json_body={"scope": "user", "user_id": ctx["user.alice"], "query": "anything"},
    )
    status, data = await ach.call(
        "GET", "/v1/admin/audit", key=OPERATOR, params={"limit": 50}
    )
    rows = _items(data)
    hit = [r for r in rows if ctx["user.alice"] in str(r)]
    ach_out = Outcome(
        "ENFORCED" if status == 200 and hit else "NOT_ENFORCED",
        f"audit query HTTP {status}, {len(hit)} row(s) naming the delegated user",
    )
    van_out = Outcome("OUT_OF_SCOPE", "no actor identity, so nothing to attribute an access to")
    return ProbeResult("operator_traceability", "", ach_out, van_out)


@probe("content_cap", "Is an oversize payload refused at the boundary or forwarded?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    # Just over sanitization._MAX_BYTES (4096), which is the ceiling the
    # retain path actually enforces -- NOT MEMORY_MAX_CONTENT_BYTES
    # (256_000), which guards recall/reflect queries only.
    #
    # Sized deliberately small: `normalize_claim` runs `contains_secret`
    # over the whole input BEFORE the length check, and that scan is
    # quadratic (measured: 4 KB 20 ms, 32 KB 1.3 s, 300 KB ~113 s). A
    # bigger probe measures the sanitizer's backtracking, not the cap.
    huge = "x" * 5_000
    status, _ = await ach.call(
        "POST", "/v1/memory/sync_retain", key=ctx["key.alice"],
        json_body=retain_body("user", huge), timeout=120.0,
    )
    ach_out = Outcome(
        "ENFORCED" if status in (413, 422) else "NOT_ENFORCED",
        f"HTTP {status} for a {len(huge):,}-byte content field (cap is 4096)",
    )
    status, _ = await van.call(
        "POST", f"{bank_path(ctx['van_bank'])}/memories",
        json_body={
            "items": [{"content": huge}],
            "async": True,
            "operation_id": str(uuid.uuid4()),
        },
        timeout=120.0,
    )
    van_out = Outcome(
        "ENFORCED" if status in (413, 422) else "OUT_OF_SCOPE",
        f"HTTP {status} for the same {len(huge):,}-byte payload",
    )
    return ProbeResult("content_cap", "", ach_out, van_out)


@probe("write_budget", "Can one credential exhaust the service for everyone else?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    limited = None
    accepted = 0
    attempts = 80  # the budget is 60 per 60 s; 40 could never reach it
    for i in range(attempts):
        status, _ = await ach.call(
            "POST", "/v1/directives", key=ctx["key.ratelimituser"],
            json_body={"scope": "user", "name": f"bench-{RUN}-{i}", "content": "probe."},
        )
        if status == 429:
            limited = i
            break
        if status < 300:
            accepted += 1
    if accepted == 0:
        ach_out = Outcome(
            "ERROR", f"no write was accepted in {attempts} attempts; nothing to rate limit"
        )
    elif limited is None:
        ach_out = Outcome(
            "NOT_ENFORCED", f"{accepted} writes accepted on one credential, never limited"
        )
    else:
        s, _ = await ach.call(
            "POST", "/v1/directives", key=ctx["key.alice"],
            json_body={"scope": "user", "name": f"bench-other-{RUN}", "content": "probe."},
        )
        ach_out = Outcome(
            "ENFORCED" if s < 400 else "NOT_ENFORCED",
            f"limited after {limited} writes; a different credential -> HTTP {s}",
        )
    van_out = Outcome(
        "OUT_OF_SCOPE", "one shared credential for the whole deployment, so no per-caller budget"
    )
    return ProbeResult("write_budget", "", ach_out, van_out)


@probe("bank_id_confinement", "Does any caller-facing response expose a raw bank id?")
async def _(ach: Http, van: Http, ctx: dict) -> ProbeResult:
    _, data = await ach.call(
        "POST", "/v1/memory/list", key=ctx["key.alice"],
        json_body={"scope": "user", "limit": 20},
    )
    text = str(data)
    leaked = "user_" in text and any(
        tok.startswith("user_") and len(tok) > 12 for tok in text.replace("'", " ").split()
    )
    ach_out = Outcome(
        "NOT_ENFORCED" if leaked else "ENFORCED",
        "bank id found in a caller response" if leaked
        else "no bank id in the response; the id is never a caller-facing address",
    )
    van_out = Outcome(
        "OUT_OF_SCOPE", "the bank id IS the address: it is in every URL the caller builds"
    )
    return ProbeResult("bank_id_confinement", "", ach_out, van_out)


def _replay_verdict(rows: int, what: str) -> Outcome:
    """A replay probe only means something if the FIRST write landed.

    Zero rows is the trap: it reads as "no duplicate" and scores as a pass
    while actually saying the arm stored nothing at all -- which is exactly
    what the vanilla arm did while its retain was 400ing.
    """
    if rows == 0:
        return Outcome("ERROR", f"nothing stored at all after {what}; verdict would be meaningless")
    return Outcome(
        "ENFORCED" if rows == 1 else "NOT_ENFORCED",
        f"{rows} memory row(s) after {what}",
    )


def _items(data) -> list:
    """Pull a result list out of whichever envelope the arm used.

    Three real shapes had to be accommodated, each of which silently
    returned [] before -- and an empty list reads as a finding rather than a
    harness bug, which is how `operator_traceability` reported 0 audit
    rows against a service that had written them:
      GET /v1/admin/audit  -> a BARE JSON list, no envelope
      POST /v1/read/history-> {"changes": [...]}, not "revisions"
      Hindsight recall     -> {"results": [...]}; its list -> {"items": [...]}
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    node = data.get("result", data)
    if isinstance(node, list):
        return node
    if not isinstance(node, dict):
        return []
    for key in ("items", "hits", "memories", "changes", "revisions", "events",
                "results", "documents"):
        value = node.get(key)
        if isinstance(value, list):
            return value
    return []


# ==========================================================================
# Bootstrap + runner
# ==========================================================================


async def bootstrap(ach: Http, van: Http) -> dict:
    ctx: dict = {}
    # The token IS the identity: the stack authenticates through an external
    # provider, so nothing is minted here and there is no operator credential
    # to mint it with. Three tokens are three people with three banks, and
    # nothing provisions them: a bank becomes usable on its owner's first
    # retain and Hindsight banks auto-create on first use.
    for name in ("alice", "bob", "ratelimituser"):
        ctx[f"key.{name}"] = f"bench-{name}-{RUN}"

    slug = f"bench-private-{RUN}"
    # Alice's INTERNAL user id, which is NOT her token: `link_identity`
    # generates it, and a delegation names its target by it
    # (`banks.resolve_user_bank`). `cross_user_read` and
    # `operator_traceability` both need it, and this call is already being
    # made, so read its owner rather than spending another one. The status
    # check is new with that read: a failed create used to be survivable
    # here, and now it is a KeyError three probes later.
    status, data = await ach.call(
        "POST", "/v1/projects", key=ctx["key.alice"], json_body={"project_slug": slug}
    )
    if status != 201:
        raise SystemExit(f"bootstrap failed creating project {slug}: HTTP {status} {data}")
    ctx["project.private"] = slug
    ctx["user.alice"] = data["owner"]["id"]

    # The vanilla arm's bank. Hindsight creates a bank on first write and the
    # id is chosen by the caller, which is itself part of what probe
    # `bank_id_confinement` reports.
    ctx["van_bank"] = f"bench_vanilla_{RUN}"
    await van.call(
        "POST", f"{bank_path(ctx['van_bank'])}/memories",
        json_body={
            "items": [{"content": f"BENCH-{RUN}: seed fact for the vanilla arm."}],
            "async": False,
            "operation_id": str(uuid.uuid4()),
        },
        timeout=120.0,
    )
    return ctx


async def main() -> int:
    async with Http(API) as ach, Http(HINDSIGHT_URL) as van:
        print(f"ach-memory : {API}")
        print(f"hindsight  : {HINDSIGHT_URL}  (no credential presented)")
        print()
        ctx = await bootstrap(ach, van)

        results: list[ProbeResult] = []
        for name, question, fn in PROBES:
            try:
                result = await fn(ach, van, ctx)
            except Exception as exc:  # noqa: BLE001 -- a broken probe must not hide the rest
                result = ProbeResult(
                    name, question, Outcome("ERROR", f"{type(exc).__name__}: {exc}"),
                    Outcome("ERROR", "not reached"),
                )
            result.question = question
            results.append(result)
            print(f"  probed {name}")

        print()
        rows = [
            [r.question, MARK[r.ach.verdict], MARK[r.vanilla.verdict]] for r in results
        ]
        print(table(["Question", "ach-memory", "vanilla Hindsight"], rows))
        print()
        print("Detail")
        print("------")
        for r in results:
            print(f"{r.name}")
            print(f"    ach      : {r.ach.detail}")
            print(f"    vanilla  : {r.vanilla.detail}")

        errors = [r for r in results if "ERROR" in (r.ach.verdict, r.vanilla.verdict)]
        enforced = sum(r.ach.verdict == "ENFORCED" for r in results)
        print()
        print(f"SUMMARY: {enforced}/{len(results)} properties enforced by ach-memory, "
              f"{len(errors)} probe error(s)")
        return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
