"""0.12.0 rollout: a title, family and pin for every existing memory; derived views off.

  plan  --out PLAN [--show SLUG ...]   read-only. Reads every bank and the journal, drafts titles,
                                       family lists and families with the LLM, writes PLAN and
                                       prints the dry-run report, plus the full session start of
                                       each --show project.
  report PLAN                          offline: the dry-run report again.
  show  PLAN SLUG...                   offline: the full session start of each project from PLAN,
                                       by slug or by a unique part of one.
  apply PLAN                           writes. Per bank: deletes the mental models, turns
                                       consolidation off, stores the family list and one
                                       `memories` record per memory. Needs migration 0005.

--hindsight is the engine's bank API root, e.g. http://localhost:18888/api/v1/default through
`kubectl -n hindsight port-forward svc/hindsight-api 18888:8888`. --psql is a command that runs
psql on the ach-memory database and reads SQL from stdin, e.g.
"kubectl exec -i -n ach-memory ach-memory-pg-1 -c postgres -- psql -d ach_memory".
The LLM is MEMORY_LLM_URL / MEMORY_LLM_API_KEY / MEMORY_LLM_HEADER / MEMORY_LLM_MODEL (plan only);
answers are cached next to PLAN, so a second plan pays only for memories retained since.
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

# The service's settings demand these; the rollout reaches both through --hindsight and --psql.
os.environ.setdefault("MEMORY_DATABASE_URL", "unused")
os.environ.setdefault("MEMORY_HINDSIGHT_URL", "unused")

from memory import families, llm
from memory.config import Settings, get_settings
from memory.context import Bank, render
from memory.families import OTHER, THRESHOLD
from memory.models import Memory

ACTOR = "rollout-0.12.0"
PIN_TYPES = ("constraint",)  # project banks only; user banks start with nothing pinned
CAPS = {"project": 3500, "user": 1200}
ACTIVATION = Path(__file__).resolve().parents[1] / "hooks" / "activation.txt"

TITLE = """You title one memory of a coding project's shared memory, the way an index lists it.
"title": one line, at most 80 characters, specific and searchable: name the component, command, file, setting or service it is about, and the point. No trailing period. English.
Answer with JSON only: {{"title": "..."}}

Memory:
type: {type}
tags: {tags}
text: {text}"""

FAMILY = """You file one memory of a coding project's shared memory into one family of the project's closed list.
Families: {families}
Pick the family whose memories this one belongs with; "other" if none fits.
Answer with JSON only: {{"family": "..."}}

Memory:
type: {type}
title: {title}
text: {text}"""


# ---------- LLM: memory.llm's call, cached on disk, with the gateway's reported cost ----------
_settings: Settings = get_settings()
_lock = threading.Lock()
_cache: dict[str, dict] = {}
_cache_file: Path | None = None
COST = {"usd": 0.0, "calls": 0, "cached": 0}
_stamps: list[float] = []
RPM = 80


def _complete_json(settings: Settings, prompt: str) -> dict:
    key = hashlib.sha1(f"{settings.llm_model}\n{prompt}".encode()).hexdigest()
    with _lock:
        if key in _cache:
            COST["cached"] += 1
            return _cache[key]
    header = settings.llm_header.strip() or "Authorization"
    headers = ({"Authorization": f"Bearer {settings.llm_api_key}"} if header.lower() == "authorization"
               else {header: settings.llm_api_key})
    body = {"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
            "reasoning_effort": "low", "response_format": {"type": "json_object"}}
    for attempt in range(6):
        _wait_slot()
        try:
            r = httpx.post(f"{settings.llm_url.rstrip('/')}/chat/completions", headers=headers, json=body,
                           timeout=settings.llm_timeout_seconds)
            if r.status_code == 429:
                time.sleep(15)
                continue
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            answer = json.loads(text[text.find("{"): text.rfind("}") + 1])
            break
        except (httpx.HTTPError, ValueError, KeyError):
            if attempt == 5:
                raise
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError("the LLM rate-limited every attempt")
    with _lock:
        COST["usd"] += float(r.headers.get("x-litellm-response-cost") or 0)
        COST["calls"] += 1
        _cache[key] = answer
        with _cache_file.open("a") as f:
            f.write(json.dumps({"k": key, "v": answer}) + "\n")
    return answer


def _wait_slot() -> None:
    while True:
        with _lock:
            now = time.time()
            _stamps[:] = [t for t in _stamps if now - t < 60]
            if len(_stamps) < RPM:
                _stamps.append(now)
                return
            delay = 60.5 - (now - _stamps[0])
        time.sleep(delay)


# ---------- reading production ----------
def psql_rows(psql: list[str], sql: str) -> list[dict]:
    out = subprocess.run([*psql, "-At", "-v", "ON_ERROR_STOP=1"], input=f"set default_transaction_read_only=on;\n{sql};",
                         capture_output=True, text=True, timeout=120, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def units(hs: httpx.Client, bank_id: str, state: str) -> list[dict]:
    out, offset = [], 0
    while True:
        r = hs.get(f"/banks/{bank_id}/memories/list",
                   params={"type": "world", "state": state, "limit": 500, "offset": offset})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        items = r.json().get("items") or []
        out += items
        offset += len(items)
        if not items or offset >= (r.json().get("total") or 0):
            return out


def memories(hs: httpx.Client, bank_id: str, first_retain: dict[str, str]) -> list[dict]:
    """One entry per memory id; a memory split into several units is joined back."""
    by_id: dict[str, dict] = {}
    for state in ("valid", "invalidated"):
        for unit in units(hs, bank_id, state):
            mid = (unit.get("metadata") or {}).get("memory_id") or unit["document_id"]
            tags = unit.get("tags") or []
            m = by_id.setdefault(mid, {
                "memory_id": mid, "state": "valid" if state == "valid" else "invalid", "texts": [],
                "memory_type": next((t[5:] for t in tags if t.startswith("type:")), "fact"),
                "tags": [t for t in tags if not t.startswith(("type:", "basis:", "schema:"))],
                "created_at": first_retain.get(mid) or unit.get("mentioned_at") or unit.get("date")})
            m["texts"].append(unit.get("text") or "")
    for m in by_id.values():
        m["text"] = "\n".join(m.pop("texts"))
    return sorted(by_id.values(), key=lambda m: m["created_at"] or "", reverse=True)


# ---------- plan ----------
def plan(args: argparse.Namespace) -> None:
    global _cache_file
    if not llm.configured(_settings):
        sys.exit("MEMORY_LLM_URL is not set")
    llm.complete_json = _complete_json  # families.propose goes through the cache too
    _cache_file = Path(args.out).with_suffix(".llm-cache.jsonl")
    if _cache_file.exists():
        for line in _cache_file.read_text().splitlines():
            row = json.loads(line)
            _cache[row["k"]] = row["v"]
    psql = shlex.split(args.psql)
    hs = httpx.Client(base_url=args.hindsight, timeout=60)
    first = {r["resource"]: r["created_at"] for r in psql_rows(psql, (
        "select row_to_json(e) from (select resource, min(created_at) as created_at from audit_events "
        "where action = 'memory.retain' group by resource) e"))}
    banks = [{"kind": "project", **p} for p in psql_rows(psql, (
        "select row_to_json(p) from (select p.internal_id as id, p.bank_id, p.owner_type, p.owner_id, "
        "(select slug from project_slugs s where s.project_internal_id = p.internal_id and s.is_canonical) "
        "as slug from projects p) p"))]
    banks += [{"kind": "user", "id": u["id"], "bank_id": f"user_{u['id']}", "slug": None}
              for u in psql_rows(psql, "select row_to_json(u) from (select id from users) u")]
    for bank in banks:
        bank["memories"] = memories(hs, bank["bank_id"], first)
    banks = [b for b in banks if b["memories"]]

    every = [(b, m) for b in banks for m in b["memories"]]
    with ThreadPoolExecutor(8) as pool:
        titles = list(pool.map(lambda bm: _complete_json(_settings, TITLE.format(
            type=bm[1]["memory_type"], tags=", ".join(bm[1]["tags"]) or "-", text=bm[1]["text"][:4000])), every))
    for (_, m), answer in zip(every, titles, strict=True):
        m["title"] = _title(str(answer.get("title") or m["text"]))

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(_draft, [b for b in banks if b["kind"] == "project"]))
    for bank in banks:
        for m in bank["memories"]:
            m.setdefault("family", None)
            m["chars"] = len(m["text"])
            m["pinned"] = False
        bank["pin_overflow"], used = [], 0
        if bank["kind"] != "project":
            continue
        for m in bank["memories"]:  # newest first
            if m["state"] != "valid" or m["memory_type"] not in PIN_TYPES:
                continue
            if used + m["chars"] <= CAPS["project"]:
                m["pinned"], used = True, used + m["chars"]
            else:
                bank["pin_overflow"].append(m["memory_id"])
    Path(args.out).write_text(json.dumps({"banks": banks, "llm": COST}, indent=1, ensure_ascii=False))
    report(banks)
    for slug in args.show or ():
        show(banks, slug)


def _title(text: str) -> str:
    """One line, at most 80 characters, cut at a word boundary."""
    title = " ".join(text.split())
    return title if len(title) <= 80 else title[:81].rsplit(" ", 1)[0].rstrip(",;:-")


def _file(bank: dict, valid: list[dict]) -> list[str]:
    """One family per memory from the bank's closed list, with the memory's text."""
    with ThreadPoolExecutor(4) as pool:
        answers = list(pool.map(lambda m: _complete_json(_settings, FAMILY.format(
            families=", ".join(bank["families"]), type=m["memory_type"], title=m["title"],
            text=m["text"][:4000])), valid))
    picked = []
    for answer in answers:
        family = str(answer.get("family") or "").strip().lower().replace(" ", "-")
        picked.append(family if family in bank["families"] else OTHER)
    return picked


def _draft(bank: dict) -> None:
    """The family list and filing for one project bank, redrafted with the reason while a family
    is named after the project or holds over a third (families.problems)."""
    valid = [m for m in bank["memories"] if m["state"] == "valid"]
    bank["families"], bank["redrafts"] = None, []
    if len(valid) < THRESHOLD:
        return
    feedback: list[str] = []
    for _ in range(families.ATTEMPTS):
        bank["families"], _ = families.propose([f"[{m['memory_type']}] {m['title']}" for m in valid],
                                               bank["slug"], feedback)
        picked = _file(bank, valid)
        if not (feedback := families.problems(picked, bank["slug"])):
            break
        bank["redrafts"].append(feedback)
    for m, family in zip(valid, picked, strict=True):
        m["family"] = family


def _rows(bank: dict) -> list[Memory]:
    return [Memory(memory_id=m["memory_id"], scope=bank["kind"], owner_id=bank["id"], title=m["title"],
                   family=m["family"], pinned=m["pinned"], memory_type=m["memory_type"], state="valid",
                   chars=m["chars"], created_at=datetime.fromisoformat(m["created_at"]))
            for m in bank["memories"] if m["state"] == "valid"]


def _bank(bank: dict | None, kind: str) -> Bank:
    if bank is None:
        return Bank([], CAPS[kind], lambda row: None)
    texts = {m["memory_id"]: m.get("text") for m in bank["memories"]}
    return Bank(_rows(bank), CAPS[kind], lambda row: texts.get(row.memory_id))


def report(banks: list[dict]) -> None:
    print(f"LLM: {COST['calls']} calls, {COST['cached']} cached, ${COST['usd']:.2f}\n")
    for bank in banks:
        valid = [m for m in bank["memories"] if m["state"] == "valid"]
        pinned = [m for m in valid if m["pinned"]]
        name = bank["slug"] or f"user {bank['id'][:12]}"
        print(f"## {name} ({bank['kind']})")
        print(f"memories: {len(valid)} valid, {len(bank['memories']) - len(valid)} invalid")
        if bank.get("families"):
            counts = {f: sum(m["family"] == f for m in valid) for f in bank["families"]}
            print("families: " + ", ".join(f"{f} {n}" for f, n in counts.items()))
        else:
            print("families: none (below 30)" if bank["kind"] == "project" else "families: none (user)")
        for attempt, found in enumerate(bank.get("redrafts") or [], 1):
            print(f"redrafted after attempt {attempt}: {'; '.join(found)}")
        cap = CAPS[bank["kind"]]
        print(f"pinned: {len(pinned)}, {sum(m['chars'] for m in pinned)} chars against a cap of {cap}")
        by_id = {m["memory_id"]: m for m in bank["memories"]}
        for mid in bank.get("pin_overflow") or []:
            print(f"  not pinned, over the cap: {mid} ({by_id[mid]['chars']} chars) {by_id[mid]['title']}")
        if bank["kind"] == "user":
            for m in (m for m in valid if m["memory_type"] == "constraint"):
                print(f"  constraint: {m['memory_id']} ({m['chars']} chars) {m['title']}")
        if bank["kind"] == "project":
            rendered = render(user=_bank(None, "user"), project=_bank(bank, "project"), purpose="-")
            text = rendered.text
            index = text[text.find("### Memory index"):] if "### Memory index" in text else ""
            print(f"index: {len(index)} chars; session start without user pins: {len(text)} chars")
        print()


def show(banks: list[dict], slug: str) -> None:
    matches = [b for b in banks if b["slug"] == slug] or [b for b in banks if slug in (b["slug"] or "")]
    if len(matches) != 1:
        sys.exit(f"{slug}: matches {len(matches)} projects")
    project = matches[0]
    slug = project["slug"]
    user = next((b for b in banks if b["kind"] == "user" and b["id"] == project["owner_id"]), None)
    purpose = next((m.get("text") for m in project["memories"]
                    if m["state"] == "valid" and "purpose" in m["tags"]), None)
    context = render(user=_bank(user, "user"), project=_bank(project, "project"), purpose=purpose).text
    output = ACTIVATION.read_text() + "\n" + context
    print(f"===== session start: {slug} ({len(output)} chars) =====\n{output}===== end {slug} =====\n")


# ---------- apply ----------
def _q(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def apply(args: argparse.Namespace) -> None:
    banks = json.loads(Path(args.plan).read_text())["banks"]
    psql = shlex.split(args.psql)
    hs = httpx.Client(base_url=args.hindsight, timeout=60)
    known = {r["memory_id"] for r in psql_rows(psql, "select row_to_json(m) from (select memory_id from memories) m")}
    for bank in banks:
        planned = {m["memory_id"] for m in bank["memories"]}
        live = {(u.get("metadata") or {}).get("memory_id") or u["document_id"] for u in units(hs, bank["bank_id"], "valid")}
        if missing := live - planned - known:
            sys.exit(f"{bank['slug'] or bank['id']}: {len(missing)} memories are not in the plan; run plan again")

    for bank in banks:
        path = f"/banks/{bank['bank_id']}/mental-models"
        for item in (hs.get(path).raise_for_status().json().get("items") or []):
            hs.delete(f"{path}/{item['id']}").raise_for_status()
        hs.patch(f"/banks/{bank['bank_id']}/config",
                 json={"updates": {"enable_observations": False}}).raise_for_status()
        cfg = hs.get(f"/banks/{bank['bank_id']}/config").raise_for_status().json().get("config") or {}
        if cfg.get("enable_observations") is not False:
            sys.exit(f"{bank['bank_id']}: enable_observations did not turn off")

    sql = ["BEGIN;"]
    for bank in banks:
        if bank["families"]:
            sql.append(f"UPDATE projects SET families = {_q(json.dumps(bank['families']))} "
                       f"WHERE internal_id = {_q(bank['id'])} AND families IS NULL;")
            sql.append(_audit("project.families", bank["id"],
                              {"before": None, "after": bank["families"], "drafted": True}))
        for m in bank["memories"]:
            if m["memory_id"] in known:
                continue
            sql.append(
                "INSERT INTO memories (memory_id, scope, owner_id, title, family, pinned, memory_type, state, "
                f"chars, created_at) VALUES ({_q(m['memory_id'])}, {_q(bank['kind'])}, {_q(bank['id'])}, "
                f"{_q(m['title'])}, {_q(m['family'])}, {_q(m['pinned'])}, {_q(m['memory_type'])}, "
                f"{_q(m['state'])}, {m['chars']}, {_q(m['created_at'])}) ON CONFLICT (memory_id) DO NOTHING;")
            if m["pinned"]:
                sql.append(_audit("memory.pin", m["memory_id"],
                                  {"scope": bank["kind"], "owner": bank["id"], "reason": "rollout: briefing and constraint memories start pinned"}))
    sql.append("COMMIT;")
    subprocess.run([*psql, "-v", "ON_ERROR_STOP=1", "-q"], input="\n".join(sql), text=True, timeout=600, check=True)
    print(f"applied: {len(banks)} banks, {sum(len(b['memories']) for b in banks)} memories planned, "
          f"{len(known)} already recorded")


def _audit(action: str, resource: str, details: dict) -> str:
    return ("INSERT INTO audit_events (id, actor_user_id, action, resource, operation_id, details) VALUES "
            f"({_q('aud_' + hashlib.sha1((action + resource).encode()).hexdigest()[:32])}, {_q(ACTOR)}, "
            f"{_q(action)}, {_q(resource)}, NULL, {_q(json.dumps(details))}) ON CONFLICT (id) DO NOTHING;")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hindsight", default="")
    parser.add_argument("--psql", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--out", required=True)
    p.add_argument("--show", action="append")
    a = sub.add_parser("apply")
    a.add_argument("plan")
    s = sub.add_parser("show")
    s.add_argument("plan")
    s.add_argument("slugs", nargs="+")
    r = sub.add_parser("report")
    r.add_argument("plan")
    r.set_defaults(slugs=[])
    args = parser.parse_args()
    if args.command in ("show", "report"):
        banks = json.loads(Path(args.plan).read_text())["banks"]
        if args.command == "report":
            report(banks)
        for slug in args.slugs:
            show(banks, slug)
        return
    {"plan": plan, "apply": apply}[args.command](args)


if __name__ == "__main__":
    main()
