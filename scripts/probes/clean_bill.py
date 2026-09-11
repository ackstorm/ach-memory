"""The calibration corpus the way production holds it: Alice's user facts in
Alice's user bank, Alice's project facts in one project bank, nobody else's
anywhere. Every earlier measurement this session put all 34 facts -- Bob's
included -- into one bank, so a `must_not` fact could outrank an expected one
and look like a reranker failure.

Answers, per question through OUR pipeline: is the expected fact returned with
the relative cut as configured, and would it be without the cut. Also reports
the per-stage scores of the expected hit and the top hit, so a miss can be
attributed to the cross-encoder, the floor, or the cut.
"""
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx

API, HINDSIGHT = "http://localhost:8000", "http://localhost:8888"
ROOT = Path(__file__).resolve().parents[2]
TOKEN = f"clean-{int(time.time())}"
PROJECT = f"clean-project-{int(time.time())}"
NORM = re.compile(r"\s*\(mentioned_at=[^)]*\)")
_PACE = 1.1


def norm(t): return NORM.sub("", t or "").strip()
def fid(tags): return next((t[4:] for t in (tags or []) if isinstance(t, str) and t.startswith("fid:")), None)


def retain(c, h, f):
    body = {
        "scope": f["scope"], "content": f["text"], "memory_type": "fact",
        "basis": "human_explicit", "trigger": "user_requested",
        "evidence": [{"kind": "user_quote", "raw": f["text"][:1024]}],
        "operation_id": str(uuid.uuid4()), "tags": [f"fid:{f['id']}"],
    }
    if f["scope"] == "project":
        body["project_slug"] = PROJECT
    for _ in range(3):
        r = c.post(f"{API}/v1/memory/sync_retain", headers=h, json=body)
        if r.status_code == 200:
            time.sleep(_PACE)
            return
        if r.status_code != 429:
            raise SystemExit(f"seed failed for {f['id']}: {r.status_code} {r.text[:200]}")
        time.sleep(min(float(r.json()["error"]["details"].get("retry_after_seconds", 5)), 70) + 1)
    raise SystemExit(f"seed failed for {f['id']}: rate limited")


def main():
    rows = [json.loads(l) for l in (ROOT / "benchmarks/corpus.jsonl").read_text().splitlines() if l.strip()]
    facts = [r for r in rows if r["kind"] == "fact" and r["owner"] == "alice"]
    qs = [r for r in rows if r["kind"] == "question"]
    fmap = {r["id"]: r["text"] for r in facts}
    print(f"identity {TOKEN}, project {PROJECT}: {len(facts)} alice facts, {len(qs)} questions")

    with httpx.Client(timeout=180.0) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        for i, f in enumerate(facts, 1):
            retain(c, h, f)
        print("seeded")

        sql = ("select u.bank_id from users u join external_identities e on e.user_id=u.id "
               f"where e.subject='{TOKEN}';")
        user_bank = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "memory",
                                    "-d", "memory", "-tAc", sql], cwd=ROOT, capture_output=True,
                                   text=True, check=True).stdout.strip()
        sql = ("select p.bank_id from projects p join project_slugs s on s.project_internal_id=p.internal_id "
               f"and s.tenant_id=p.tenant_id where s.slug='{PROJECT}';")
        proj_bank = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "memory",
                                    "-d", "memory", "-tAc", sql], cwd=ROOT, capture_output=True,
                                   text=True, check=True).stdout.strip()
        banks = {"user": user_bank, "project": proj_bank}

        print(f"\n{'q':4} {'ours':5} {'noCut':6} {'CE(exp)':>10} {'sem(exp)':>9} {'CE(top)':>10} {'pos':>4}  verdict")
        ours_ok = nocut_ok = 0
        for q in qs:
            body = {"scope": q["scope"], "query": q["query"]}
            if q["scope"] == "project":
                body["project_slug"] = PROJECT
            hits = c.post(f"{API}/v1/read/recall", headers=h, json=body).json().get("hits", [])
            want = {norm(fmap[f]) for f in q["expect"] if f in fmap}
            ours = bool(want & {norm(x["text"]) for x in hits})
            ours_ok += ours

            raw = c.post(f"{HINDSIGHT}/v1/default/banks/{banks[q['scope']]}/memories/recall",
                         json={"query": q["query"], "types": ["world", "observation"],
                               "max_tokens": 32768}).json()["results"]
            exp = [(i, x) for i, x in enumerate(raw) if fid(x.get("tags")) in set(q["expect"])]
            top = raw[0] if raw else None
            sc = lambda x, k: ((x or {}).get("scores") or {}).get(k)
            if exp:
                pos, e = exp[0]
                ce_e, sem_e = sc(e, "final"), sc(e, "semantic")
            else:
                pos, ce_e, sem_e = None, None, None
            ce_t = sc(top, "final")
            # no-cut = floor only, on the raw list
            nocut = any((sc(x, "semantic") or 1.0) >= 0.60 for _, x in exp)
            nocut_ok += nocut

            if ours:
                verdict = ""
            elif not nocut:
                verdict = "floor withholds it" if exp else "not in raw at all"
            else:
                verdict = f"RELATIVE CUT: {ce_e:.2e} < 1% of {ce_t:.3f}"
            print(f"{q['id']:4} {'OK' if ours else 'LOST':5} {'OK' if nocut else 'LOST':6} "
                  f"{(ce_e if ce_e is not None else float('nan')):10.2e} "
                  f"{(sem_e if sem_e is not None else float('nan')):9.4f} "
                  f"{(ce_t if ce_t is not None else float('nan')):10.3f} {pos!s:>4}  {verdict}")
        print(f"\nours (floor + cut) : {ours_ok}/{len(qs)}")
        print(f"floor only         : {nocut_ok}/{len(qs)}")


if __name__ == "__main__":
    main()
