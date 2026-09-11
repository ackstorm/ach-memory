"""Finding 6: do duplicates accumulate, and does the agent see them?

Code verdict first (read, not measured): `payload_hash` is only ever compared
against a row already found by `operation_id`, and nothing indexes or queries
it alone. So N retains of identical content under N operation_ids are N rows,
N documents, N facts. This script measures what that costs at READ time --
our recall, Hindsight's own recall, and what `prefer_observations` would do.

Seeds one bank with:
  * 6 distinct facts, retained once each
  * 1 fact retained 4 times verbatim (distinct operation_ids)
  * 1 fact retained 3 times as paraphrases
"""
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

API, HINDSIGHT = "http://localhost:8000", "http://localhost:8888"
ROOT = Path(__file__).resolve().parents[2]
TOKEN = f"dupbill-{int(time.time())}"

VERBATIM = "Deployments to production require two approvals from the platform team."
PARAPHRASES = [
    "The staging database is reset every night at 03:00 UTC.",
    "Staging's database gets wiped nightly at 3am UTC.",
    "Every night at 03:00 UTC the staging database is torn down and recreated.",
]
DISTINCT = [
    "This project pins its Python dependencies with uv, never with pip.",
    "Migrations run with alembic upgrade head before the api serves traffic.",
    "Secrets come from the cluster's external-secrets operator, never from .env.",
    "The frontend is built with Vite and served from a CDN bucket.",
    "Incident severity 1 pages the on-call engineer within five minutes.",
    "All log lines are JSON and carry a trace_id field.",
]

QUERIES = {
    "verbatim": "how many approvals does a production deployment need",
    "paraphrase": "when is the staging database reset",
    "broad": "how does this project work",
}


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
    with httpx.Client(timeout=180.0) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        print(f"identity: {TOKEN}")

        print("seeding 6 distinct facts")
        for text in DISTINCT:
            retain(c, h, text)
        print("seeding the same fact 4 times, verbatim, 4 operation_ids")
        for _ in range(4):
            retain(c, h, VERBATIM)
        print("seeding 3 paraphrases of one fact")
        for text in PARAPHRASES:
            retain(c, h, text)

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

        rows = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres", "psql",
                "-U", "memory", "-d", "memory", "-tAc",
                (
                    "select count(*), count(distinct payload_hash), "
                    "count(distinct canonical_content) from retained_records r "
                    "join users u on u.id=r.user_id join external_identities e "
                    f"on e.user_id=u.id where e.subject='{TOKEN}';"
                ),
            ],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        total, distinct_hash, distinct_content = (x.strip() for x in rows.split("|"))
        print()
        print("== ACH provenance rows ==")
        print(f"  retained_records rows : {total}   (13 retains sent)")
        print(f"  distinct payload_hash : {distinct_hash}")
        print(f"  distinct content      : {distinct_content}")

        print()
        print("== our recall (floor 0.60 + relative cut applied) ==")
        for label, query in QUERIES.items():
            r = c.post(
                f"{API}/v1/read/recall",
                headers=h,
                json={"scope": "user", "query": query},
            )
            r.raise_for_status()
            hits = r.json().get("hits", [])
            contents = [hit.get("content", "")[:70] for hit in hits]
            repeats = len(contents) - len(set(contents))
            print(f"  [{label}] {len(hits)} hits, {repeats} verbatim repeat(s)")
            for text in contents:
                print(f"      - {text}")

        print()
        print("== Hindsight raw, same queries ==")
        for label, query in QUERIES.items():
            for prefer in (False, True):
                r = c.post(
                    f"{HINDSIGHT}/v1/default/banks/{bank}/memories/recall",
                    json={
                        "query": query,
                        "types": ["world", "observation"],
                        "prefer_observations": prefer,
                    },
                )
                r.raise_for_status()
                results = r.json().get("results", [])
                texts = [
                    (x.get("content") or x.get("text") or json.dumps(x))[:60]
                    for x in results
                ]
                repeats = len(texts) - len(set(texts))
                print(
                    f"  [{label}] prefer_observations={prefer!s:5} "
                    f"{len(results)} results, {repeats} verbatim repeat(s)"
                )

        print()
        print("== list_memories, whole bank ==")
        r = c.post(
            f"{API}/v1/memory/list", headers=h, json={"scope": "user", "limit": 100}
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        texts = [(i.get("content") or "")[:70] for i in items]
        print(f"  {len(items)} items, {len(texts) - len(set(texts))} verbatim repeat(s)")
        for text in sorted(texts):
            print(f"      - {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
