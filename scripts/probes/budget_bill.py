"""Do duplicates evict real answers from Hindsight's own result budget?

Hindsight caps a recall response at `max_tokens` (4096 by default; we never
set it). Twins and repeated retains are spent inside that cap before ACH sees
anything, so the question is whether a claim that WAS returned stops being
returned once duplicates crowd the bank.

Measures the same bank twice: once seeded with the calibration corpus alone,
then again after 20 verbatim re-retains of one single fact. Same queries, same
everything. Reports which claims were present before and absent after.
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
TOKEN = f"budget-{int(time.time())}"
DUPES = 20

NORM = re.compile(r"\s*\(mentioned_at=[^)]*\)")


def norm(text: str) -> str:
    return NORM.sub("", text or "").strip()


def retain(c: httpx.Client, headers: dict, content: str) -> None:
    r = c.post(
        f"{API}/v1/memory/sync_retain",
        headers=headers,
        json={
            "scope": "user",
            "content": content,
            "memory_type": "fact",
            "basis": "human_explicit",
            "trigger": "user_requested",
            "evidence": [{"kind": "user_quote", "raw": content[:1024]}],
            "operation_id": str(uuid.uuid4()),
        },
    )
    if r.status_code != 200:
        raise SystemExit(f"seed failed: {r.status_code} {r.text[:300]}")


def main() -> int:
    rows = [
        json.loads(line)
        for line in (ROOT / "benchmarks/corpus.jsonl").read_text().splitlines()
        if line.strip()
    ]
    facts = [r for r in rows if r["kind"] == "fact"]
    questions = [r for r in rows if r["kind"] == "question"]
    print(f"identity: {TOKEN} | {len(facts)} facts, {len(questions)} questions")

    with httpx.Client(timeout=180.0) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        for i, f in enumerate(facts, 1):
            retain(c, h, f["text"])
            if i % 20 == 0:
                print(f"  seeded {i}/{len(facts)}")

        bank = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres", "psql",
                "-U", "memory", "-d", "memory", "-tAc",
                (
                    "select u.bank_id from users u join external_identities e "
                    f"on e.user_id=u.id where e.subject='{TOKEN}';"
                ),
            ],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()

        def probe() -> dict[str, dict]:
            out = {}
            for q in questions:
                r = c.post(
                    f"{HINDSIGHT}/v1/default/banks/{bank}/memories/recall",
                    json={"query": q["query"], "types": ["world", "observation"]},
                )
                r.raise_for_status()
                results = r.json().get("results", [])
                texts = [norm(x.get("text") or "") for x in results]
                out[q["id"]] = {
                    "n": len(results),
                    "distinct": len(set(texts)),
                    "texts": set(texts),
                    "chars": sum(len(t) for t in texts),
                }
            return out

        before = probe()
        n_before = sum(v["n"] for v in before.values())
        print(f"\nBEFORE: {n_before} results over {len(questions)} questions, "
              f"mean {n_before / len(questions):.1f}/question")

        victim = facts[0]["text"]
        print(f"\nre-retaining ONE fact {DUPES}x verbatim:\n  {victim[:70]}")
        for i in range(DUPES):
            retain(c, h, victim)
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{DUPES}")

        after = probe()
        n_after = sum(v["n"] for v in after.values())
        print(f"\nAFTER: {n_after} results over {len(questions)} questions, "
              f"mean {n_after / len(questions):.1f}/question")

        print("\n== eviction: claims present BEFORE and gone AFTER ==")
        evicted_total = 0
        for qid in sorted(before):
            lost = before[qid]["texts"] - after[qid]["texts"]
            lost = {t for t in lost if t and norm(victim) not in t}
            if lost:
                evicted_total += len(lost)
                print(f"  [{qid}] {len(lost)} lost "
                      f"(n {before[qid]['n']}->{after[qid]['n']}, "
                      f"distinct {before[qid]['distinct']}->{after[qid]['distinct']})")
                for t in sorted(lost)[:3]:
                    print(f"       - {t[:72]}")
        if not evicted_total:
            print("  none -- the budget was not the binding constraint here")

        print("\n== per-question distinct-claim change ==")
        worse = [
            (qid, before[qid]["distinct"], after[qid]["distinct"])
            for qid in sorted(before)
            if after[qid]["distinct"] < before[qid]["distinct"]
        ]
        print(f"  {len(worse)}/{len(questions)} questions lost distinct claims")
        for qid, b, a in worse[:10]:
            print(f"    {qid}: {b} -> {a}")
        caps = sorted({v["n"] for v in after.values()})
        print(f"\n  result counts seen after: {caps[:12]}{' ...' if len(caps) > 12 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
