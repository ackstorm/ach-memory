"""Does the 200-result cap blind a real answer?

The cap's own comment called 200 "generous relative to anything a real bank
returns (recall/history responses are themselves budget-limited upstream)".
This probe was written to test that, seeding a bank past the cap and asking
the calibration questions, whose expected answers are known.

What it found (2026-09-11, 129-claim bank): the comment was right and the
cap was dormant. Upstream's `max_tokens` default of 4096 truncated every
response to 122-167 entries -- a token budget, so it varied per query --
before 200 could bite, and it reports nothing about having done so. That
budget, cut in `final` (reranker) order, was the real silent truncation:
161 of 258 entries, 109 of 129 claims. See `_RECALL_MAX_TOKENS` and the
split of the cap into `_MAX_RAW_RESULTS_SCANNED`/`_MAX_HITS_NORMALIZED` in
`src/memory/read_service.py`.

Run against a live local stack (`make up`), from the repository root.
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
TOKEN = f"cap-{int(time.time())}"
CAP = 200
FLOOR = 0.60
FILLER = 95

NORM = re.compile(r"\s*\(mentioned_at=[^)]*\)")

# Unrelated, distinct, plausible. Noise is what a real bank is mostly made of.
SUBJECTS = [
    "the billing exporter", "the nightly reconciler", "the tenant router",
    "the webhook dispatcher", "the audit shipper", "the schema registry",
    "the feature-flag cache", "the image resizer", "the quota sweeper",
    "the session pruner", "the metrics relay", "the backup verifier",
    "the DNS refresher", "the cert rotator", "the log compactor",
    "the queue drainer", "the index rebuilder", "the trace sampler",
    "the config loader",
]
PREDICATES = [
    "runs every fifteen minutes and retries twice on failure",
    "writes its checkpoint to object storage before exiting",
    "is owned by the platform team and paged at severity two",
    "refuses to start without an explicit region argument",
    "logs one line per batch and nothing per item",
]


def filler_facts(n: int) -> list[str]:
    out = []
    for i in range(n):
        subject = SUBJECTS[i % len(SUBJECTS)]
        predicate = PREDICATES[(i // len(SUBJECTS)) % len(PREDICATES)]
        out.append(f"In release {2000 + i}, {subject} {predicate}.")
    return out[:n]


def norm(text: str) -> str:
    return NORM.sub("", text or "").strip()


def fid_of(tags) -> str | None:
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("fid:"):
            return tag.removeprefix("fid:")
    return None


#: MEMORY_WRITE_LIMIT is 60 writes per 60s per credential. Paced under it
#: rather than retried into it: 129 writes sent flat out earned a 429 with
#: retry_after 27.7s on the 27th.
_PACE_SECONDS = 1.1
_MAX_RETRIES = 3


def retain(c: httpx.Client, headers: dict, content: str, fid: str) -> None:
    body = {
        "scope": "user",
        "content": content,
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "user_requested",
        "evidence": [{"kind": "user_quote", "raw": content[:1024]}],
        "operation_id": str(uuid.uuid4()),
        "tags": [f"fid:{fid}"],
    }
    for attempt in range(_MAX_RETRIES):
        r = c.post(f"{API}/v1/memory/sync_retain", headers=headers, json=body)
        if r.status_code == 200:
            time.sleep(_PACE_SECONDS)
            return
        if r.status_code != 429:
            raise SystemExit(f"seed failed for {fid}: {r.status_code} {r.text[:300]}")
        wait = 5.0
        try:
            wait = float(
                r.json()["error"]["details"]["retry_after_seconds"]
            )
        except (ValueError, KeyError, TypeError):
            pass
        wait = min(max(wait, 1.0), 70.0) + 1.0
        print(f"  rate limited on {fid}, waiting {wait:.0f}s "
              f"(attempt {attempt + 1}/{_MAX_RETRIES})")
        time.sleep(wait)
    raise SystemExit(f"seed failed for {fid}: still rate limited after {_MAX_RETRIES} tries")


def main() -> int:
    rows = [
        json.loads(line)
        for line in (ROOT / "benchmarks/corpus.jsonl").read_text().splitlines()
        if line.strip()
    ]
    facts = [r for r in rows if r["kind"] == "fact"]
    questions = [r for r in rows if r["kind"] == "question"]
    fillers = filler_facts(FILLER)
    claims = len(facts) + len(fillers)
    print(f"identity: {TOKEN}")
    print(f"seeding {len(facts)} corpus facts + {len(fillers)} filler = {claims} claims")
    print(f"expected raw per query: {2 * claims} (cap is {CAP})")

    with httpx.Client(timeout=180.0) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        for i, f in enumerate(facts, 1):
            retain(c, h, f["text"], f["id"])
            if i % 20 == 0:
                print(f"  corpus {i}/{len(facts)}")
        for i, text in enumerate(fillers, 1):
            retain(c, h, text, f"filler{i:03d}")
            if i % 20 == 0:
                print(f"  filler {i}/{len(fillers)}")

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

        print("\n== per question: where the expected answer sits ==")
        blinded, at_risk, worst_pos = [], [], 0
        tail_admissible_total = 0
        raw_counts = set()
        for q in questions:
            r = c.post(
                f"{HINDSIGHT}/v1/default/banks/{bank}/memories/recall",
                json={"query": q["query"], "types": ["world", "observation"]},
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            raw_counts.add(len(results))

            expected = set(q["expect"])
            positions = [
                (i, fid_of(x.get("tags")), (x.get("scores") or {}).get("semantic"))
                for i, x in enumerate(results)
            ]
            hits = [(i, fid, sem) for i, fid, sem in positions if fid in expected]
            best = min((i for i, _, _ in hits), default=None)

            # What the cap throws away that the floor would have admitted.
            tail_admissible = [
                (i, fid, sem)
                for i, fid, sem in positions
                if i >= CAP and sem is not None and sem >= FLOOR
            ]
            tail_admissible_total += len(tail_admissible)

            # Expected answers the cap never shows the floor at all.
            survivors = [
                (i, fid, sem)
                for i, fid, sem in hits
                if i < CAP and (sem is None or sem >= FLOOR)
            ]
            if best is not None:
                worst_pos = max(worst_pos, best)
            if not survivors:
                blinded.append((q["id"], best, len(results)))
            elif best is not None and best >= CAP * 0.75:
                at_risk.append((q["id"], best))

            flag = "" if survivors else "   <-- BLINDED"
            print(
                f"  {q['id']}: {len(results):4} raw | best expected at position "
                f"{best if best is not None else '-':>4} | "
                f"{len(tail_admissible)} admissible past {CAP}{flag}"
            )

        print(f"\nraw counts seen: {sorted(raw_counts)}")
        print(f"worst 'best expected' position across all questions: {worst_pos}")
        print(
            f"hits past position {CAP} that clear the {FLOOR} floor, total: "
            f"{tail_admissible_total}"
        )
        print(f"questions blinded by the cap: {len(blinded)}/{len(questions)}")
        for qid, pos, n in blinded:
            print(f"  {qid}: expected answer at position {pos} of {n}")
        if at_risk:
            print(f"questions whose answer sits past {int(CAP * 0.75)}: {at_risk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
